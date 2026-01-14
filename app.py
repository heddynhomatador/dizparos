import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI(title="Discador IA Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois a gente trava
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== ENV =====
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

DIZPAROS_ENDPOINT = os.getenv("DIZPAROS_ENDPOINT", "https://api.dizparos.com/v1/messaging/send").rstrip("/")
DIZPAROS_API_KEY = os.getenv("DIZPAROS_API_KEY", "")  # dk_live...
DIZPAROS_WEBHOOK_SECRET = os.getenv("DIZPAROS_WEBHOOK_SECRET", "")

TRANSFER_DESTINATION = os.getenv("TRANSFER_DESTINATION", "")

SIP_TRUNK_ADDRESS = os.getenv("SIP_TRUNK_ADDRESS", "")
SIP_TRUNK_PORT = int(os.getenv("SIP_TRUNK_PORT", "5060"))
SIP_TRUNK_USERNAME = os.getenv("SIP_TRUNK_USERNAME", "")
SIP_TRUNK_PASSWORD = os.getenv("SIP_TRUNK_PASSWORD", "")

supabase: Optional[Client] = None


def get_supabase() -> Client:
    global supabase
    if supabase is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY no Render.")
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return supabase


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_webhook(req: Request):
    """
    Se você configurar um segredo no Dizparos e ele enviar em header,
    valida aqui. Se não usar, deixa vazio e ele não valida.
    """
    if not DIZPAROS_WEBHOOK_SECRET:
        return
    got = req.headers.get("x-webhook-secret", "")
    if got != DIZPAROS_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Webhook secret inválido")


class TickBody(BaseModel):
    campaign_id: Optional[str] = None


class StartCallBody(BaseModel):
    to: str  # +55...
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
    DOC do Dizparos:
    POST https://api.dizparos.com/v1/messaging/send
    body: { channel:"voice", details:{ to, type:"transfer", transfer_destination, transfer_sip_trunk:{...} } }
    """
    if not DIZPAROS_API_KEY:
        raise RuntimeError("DIZPAROS_API_KEY não configurado.")
    if not TRANSFER_DESTINATION:
        raise RuntimeError("TRANSFER_DESTINATION não configurado.")

    payload: Dict[str, Any] = {
        "channel": "voice",
        "details": {
            "to": phone_e164,
            "type": "transfer",
            "transfer_destination": TRANSFER_DESTINATION,
        },
    }

    # Se você quiser que o Dizparos origine via SIP Trunk (Eleven)
    if SIP_TRUNK_ADDRESS and SIP_TRUNK_USERNAME and SIP_TRUNK_PASSWORD:
        payload["details"]["transfer_sip_trunk"] = {
            "address": SIP_TRUNK_ADDRESS,
            "port": SIP_TRUNK_PORT,
            "username": SIP_TRUNK_USERNAME,
            "password": SIP_TRUNK_PASSWORD,
        }

    headers = {
        "Content-Type": "application/json",
        # Auth mais comum (se a plataforma usar Bearer)
        "Authorization": f"Bearer {DIZPAROS_API_KEY}",
        # Fallbacks (caso use x-api-key)
        "x-api-key": DIZPAROS_API_KEY,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(DIZPAROS_ENDPOINT, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


@app.post("/start_call")
async def start_call(body: StartCallBody):
    """
    Endpoint pra testar UMA ligação.
    - Se vier campaign_id + contact_id: grava no Supabase e linka tudo.
    - Se não vier: só chama o Dizparos e retorna a resposta (sem gravar no banco).
    """
    sb = get_supabase()

    # 1) dispara a ligação primeiro (teste direto)
    resp = await dizparos_start_call(body.to)
    diz_call_id = resp.get("call_id") or resp.get("data", {}).get("call_id") or resp.get("id")

    # 2) se não vier vínculo, não tenta escrever em calls (evita erro de NOT NULL / FK)
    if not body.campaign_id or not body.contact_id:
        return {
            "ok": True,
            "mode": "direct_test_no_db",
            "dizparos_call_id": diz_call_id,
            "dizparos_response": resp
        }

    # 3) agora sim grava no banco (com FKs válidas)
    call_row = sb.table("calls").insert({
        "campaign_id": body.campaign_id,
        "contact_id": body.contact_id,
        "status": "created",
        "created_at": now_utc_iso(),
        "dizparos_call_id": diz_call_id,
    }).execute().data[0]

    return {"ok": True, "mode": "db_linked", "call": call_row, "dizparos_response": resp}



@app.post("/tick")
async def tick(body: TickBody):
    """
    Roda as campanhas que estiverem status=running.
    Pega contatos pending, cria calls, chama Dizparos, salva dizparos_call_id.
    """
    sb = get_supabase()

    q = sb.table("campaigns").select("id, concurrency").eq("status", "running")
    if body.campaign_id:
        q = q.eq("id", body.campaign_id)

    campaigns = q.execute().data
    if not campaigns:
        return {"ok": True, "message": "Nenhuma campanha running."}

    out: List[Dict[str, Any]] = []

    for camp in campaigns:
        campaign_id = camp["id"]
        concurrency = int(camp.get("concurrency") or 5)

        in_prog = sb.table("calls").select("id", count="exact") \
            .eq("campaign_id", campaign_id) \
            .in_("status", ["created", "answered", "transferred"]) \
            .execute()

        current = in_prog.count or 0
        free_slots = max(concurrency - current, 0)

        if free_slots <= 0:
            out.append({"campaign_id": campaign_id, "started": 0, "reason": "sem slots"})
            continue

        contacts = sb.table("contacts") \
            .select("id, phone_e164, attempts") \
            .eq("campaign_id", campaign_id) \
            .eq("status", "pending") \
            .limit(free_slots) \
            .execute().data

        started = 0
        errors = 0

        for c in contacts:
            contact_id = c["id"]
            phone = c["phone_e164"]
            attempts = int(c.get("attempts") or 0)

            call_row = sb.table("calls").insert({
                "campaign_id": campaign_id,
                "contact_id": contact_id,
                "status": "created",
                "created_at": now_utc_iso(),
            }).execute().data[0]

            sb.table("contacts").update({
                "status": "calling",
                "attempts": attempts + 1,
                "last_call_id": call_row["id"],
            }).eq("id", contact_id).execute()

            try:
                resp = await dizparos_start_call(phone)
                diz_call_id = resp.get("call_id") or resp.get("id") or resp.get("data", {}).get("call_id")

                sb.table("calls").update({
                    "dizparos_call_id": diz_call_id
                }).eq("id", call_row["id"]).execute()

                started += 1

            except Exception as e:
                errors += 1
                sb.table("calls").update({"status": "failed"}).eq("id", call_row["id"]).execute()
                sb.table("contacts").update({"status": "failed"}).eq("id", contact_id).execute()
                sb.table("call_events").insert({
                    "call_id": call_row["id"],
                    "event_type": "backend_error",
                    "payload": {"error": str(e)}
                }).execute()

        out.append({"campaign_id": campaign_id, "started": started, "errors": errors})

    return {"ok": True, "results": out}


@app.post("/webhooks/dizparos")
async def dizparos_webhook(req: Request):
    """
    Recebe eventos conforme doc:
    type 2000 answered
    type 2001 transferred
    type 2002 finished (duration, cost, recording_url)
    """
    verify_webhook(req)
    payload = await req.json()
    sb = get_supabase()

    event_type = payload.get("type_description") or payload.get("type") or payload.get("event") or "unknown"
    data = payload.get("data") or {}

    diz_call_id = data.get("call_id") or payload.get("call_id") or payload.get("id")
    if not diz_call_id:
        # guarda o evento mesmo assim (debug)
        sb.table("call_events").insert({
            "call_id": None,
            "event_type": str(event_type),
            "payload": payload
        }).execute()
        return {"ok": True, "warning": "call_id ausente, evento salvo mesmo assim"}

    # acha a call pelo dizparos_call_id
    call_rows = sb.table("calls").select("id, contact_id").eq("dizparos_call_id", diz_call_id).limit(1).execute().data
    call_id = call_rows[0]["id"] if call_rows else None
    contact_id = call_rows[0]["contact_id"] if call_rows else None

    # salva evento bruto
    sb.table("call_events").insert({
        "call_id": call_id,
        "event_type": str(event_type),
        "payload": payload
    }).execute()

    # Se ainda não existe call local, cria uma mínima (pra não perder rastreio)
    if not call_id:
        new_call = sb.table("calls").insert({
            "dizparos_call_id": diz_call_id,
            "status": "created",
            "created_at": now_utc_iso(),
        }).execute().data[0]
        call_id = new_call["id"]

    et = str(event_type).lower()
    t = payload.get("type")  # 2000/2001/2002

    # ANSWERED
    if et == "answered" or t == 2000:
        sb.table("calls").update({"status": "answered"}).eq("id", call_id).execute()

    # TRANSFERRED
    elif et == "transferred" or t == 2001:
        sb.table("calls").update({"status": "transferred"}).eq("id", call_id).execute()

    # FINISHED
    elif et == "finished" or t == 2002:
        sb.table("calls").update({
            "status": "finished",
            "duration": data.get("duration"),
            "cost": data.get("cost"),
            "recording_url": data.get("recording_url"),
            "finished_at": now_utc_iso(),
        }).eq("id", call_id).execute()

        if contact_id:
            sb.table("contacts").update({"status": "done"}).eq("id", contact_id).execute()

    return {"ok": True}

