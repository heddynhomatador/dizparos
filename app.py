import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI(title="Discador IA Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois você trava
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== ENV =====
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

DIZPAROS_ENDPOINT = os.getenv("DIZPAROS_ENDPOINT", "https://api.dizparos.com/v1/messaging/send").rstrip("/")
DIZPAROS_API_KEY = os.getenv("DIZPAROS_API_KEY", "") or os.getenv("DIZPAROS_TOKEN", "")

TRANSFER_DESTINATION = os.getenv("TRANSFER_DESTINATION", "")

SIP_TRUNK_ADDRESS = os.getenv("SIP_TRUNK_ADDRESS", "")
SIP_TRUNK_PORT = int(os.getenv("SIP_TRUNK_PORT", "5060"))
SIP_TRUNK_USERNAME = os.getenv("SIP_TRUNK_USERNAME", "")
SIP_TRUNK_PASSWORD = os.getenv("SIP_TRUNK_PASSWORD", "")

DIZPAROS_WEBHOOK_SECRET = os.getenv("DIZPAROS_WEBHOOK_SECRET", "")

supabase: Optional[Client] = None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_supabase() -> Optional[Client]:
    global supabase
    # Supabase é opcional (pra não travar teste de call)
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    if supabase is None:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return supabase


def verify_webhook(req: Request):
    # se você setar segredo, valida. Se não setar, ignora.
    if not DIZPAROS_WEBHOOK_SECRET:
        return
    got = req.headers.get("x-webhook-secret", "")
    if got != DIZPAROS_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Webhook secret inválido")


class StartCallBody(BaseModel):
    to: str  # +5511999999999
    # opcional: você pode mandar algo pra identificar no seu BD depois
    campaign_id: Optional[str] = None
    contact_id: Optional[str] = None


@app.get("/")
def root():
    return {"ok": True, "service": "discador-ia-backend"}


@app.get("/health")
def health():
    return {"ok": True}


async def dizparos_start_call(phone_e164: str) -> Dict[str, Any]:
    """
    Conforme doc do Dizparos:
    POST /v1/messaging/send
    {
      "channel": "voice",
      "details": {
        "to": "+55...",
        "type": "transfer",
        "transfer_destination": "...",
        "transfer_sip_trunk": {...}
      }
    }
    """
    if not DIZPAROS_API_KEY:
        raise RuntimeError("Configure DIZPAROS_API_KEY (ou DIZPAROS_TOKEN).")
    if not TRANSFER_DESTINATION:
        raise RuntimeError("Configure TRANSFER_DESTINATION (destino da transferência).")

    payload: Dict[str, Any] = {
        "channel": "voice",
        "details": {
            "to": phone_e164,
            "type": "transfer",
            "transfer_destination": TRANSFER_DESTINATION,
        },
    }

    # SIP trunk é opcional
    if SIP_TRUNK_ADDRESS:
        payload["details"]["transfer_sip_trunk"] = {
            "address": SIP_TRUNK_ADDRESS,  # ex: sip.rtc.elevenlabs.io (SEM sip:)
            "port": SIP_TRUNK_PORT,
            "username": SIP_TRUNK_USERNAME,
            "password": SIP_TRUNK_PASSWORD,
        }

    headers = {
        "Content-Type": "application/json",
        # padrão mais comum:
        "Authorization": f"Bearer {DIZPAROS_API_KEY}",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(DIZPAROS_ENDPOINT, json=payload, headers=headers)
        if r.status_code >= 400:
            # loga o erro real do Dizparos pra você enxergar o motivo do 422
            raise HTTPException(status_code=r.status_code, detail={"dizparos_error": r.text, "sent": payload})
        return r.json()


@app.post("/start_call")
async def start_call(body: StartCallBody):
    sb = get_supabase()

    # 1) chama o Dizparos
    resp = await dizparos_start_call(body.to)

    # 2) salva no Supabase (se configurado)
    if sb:
        try:
            diz_call_id = resp.get("call_id")
            sb.table("calls").insert({
                "dizparos_call_id": diz_call_id,
                "status": "created",
                "created_at": now_utc_iso(),
                # se você quiser amarrar:
                "campaign_id": body.campaign_id,
                "contact_id": body.contact_id,
            }).execute()
        except Exception as e:
            # não derruba a call por causa do banco, mas te avisa
            return {"ok": True, "resp": resp, "warning": f"Falhou ao salvar no Supabase: {str(e)}"}

    return {"ok": True, "resp️resp": resp}


@app.post("/webhooks/dizparos")
async def dizparos_webhook(req: Request):
    verify_webhook(req)
    payload = await req.json()
    sb = get_supabase()

    event_type = payload.get("type_description") or payload.get("type") or payload.get("event") or "unknown"
    data = payload.get("data") or {}

    diz_call_id = data.get("call_id") or payload.get("call_id") or payload.get("id")

    # sempre responde 2xx pro Dizparos (como a doc pede)
    if not sb:
        return {"ok": True, "warning": "Supabase não configurado", "event_type": event_type, "call_id": diz_call_id}

    try:
        # acha call no banco
        call_rows = sb.table("calls").select("id, contact_id").eq("dizparos_call_id", diz_call_id).limit(1).execute().data
        call_id = call_rows[0]["id"] if call_rows else None

        # grava evento
        sb.table("call_events").insert({
            "call_id": call_id,
            "event_type": str(event_type),
            "payload": payload
        }).execute()

        # atualiza status
        et = str(event_type).lower()
        if et in ["answered", "2000"]:
            sb.table("calls").update({"status": "answered"}).eq("dizparos_call_id", diz_call_id).execute()
        elif et in ["transferred", "2001"]:
            sb.table("calls").update({"status": "transferred"}).eq("dizparos_call_id", diz_call_id).execute()
        elif et in ["finished", "2002"]:
            sb.table("calls").update({
                "status": "finished",
                "duration": data.get("duration"),
                "cost": data.get("cost"),
                "recording_url": data.get("recording_url"),
                "finished_at": now_utc_iso(),
            }).eq("dizparos_call_id", diz_call_id).execute()

            if call_rows and call_rows[0].get("contact_id"):
                sb.table("contacts").update({"status": "done"}).eq("id", call_rows[0]["contact_id"]).execute()

    except Exception as e:
        # ainda assim responde 2xx pra não reprocessar sem parar
        return {"ok": True, "error": str(e)}

    return {"ok": True}
