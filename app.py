import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# =========================
# App
# =========================
app = FastAPI(title="Discador IA Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # depois você trava pro domínio do lovable
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Env / Config
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# Dizparos
DIZPAROS_ENDPOINT = os.getenv("DIZPAROS_ENDPOINT", "https://api.dizparos.com/v1/messaging/send").strip()
DIZPAROS_TOKEN = os.getenv("DIZPAROS_TOKEN", "").strip()

# Transfer settings
TRANSFER_DESTINATION = os.getenv("TRANSFER_DESTINATION", "").strip()

# SIP trunk (ElevenLabs)
SIP_TRUNK_ADDRESS = os.getenv("SIP_TRUNK_ADDRESS", "").strip()
SIP_TRUNK_PORT = int(os.getenv("SIP_TRUNK_PORT", "5060"))
SIP_TRUNK_USERNAME = os.getenv("SIP_TRUNK_USERNAME", "").strip()
SIP_TRUNK_PASSWORD = os.getenv("SIP_TRUNK_PASSWORD", "").strip()

# Webhook security (opcional)
DIZPAROS_WEBHOOK_SECRET = os.getenv("DIZPAROS_WEBHOOK_SECRET", "").strip()

# Render port
PORT = int(os.getenv("PORT", "10000"))

_supabase: Optional[Client] = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY no Render.")
        _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_webhook(req: Request) -> None:
    """
    Se você configurar um secret no Dizparos, ele tem que vir no header x-webhook-secret.
    Se você NÃO configurar secret no Dizparos, deixe DIZPAROS_WEBHOOK_SECRET vazio e a verificação é ignorada.
    """
    if not DIZPAROS_WEBHOOK_SECRET:
        return
    got = (req.headers.get("x-webhook-secret") or "").strip()
    if got != DIZPAROS_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Webhook secret inválido")


# =========================
# Models
# =========================
class TickBody(BaseModel):
    campaign_id: Optional[str] = None


# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"ok": True, "service": "discador-ia-backend"}


@app.get("/health")
def health():
    return {"ok": True, "ts": now_utc_iso()}


# ---------- Dizparos: start call ----------
async def dizparos_start_call(phone_e164: str) -> Dict[str, Any]:
    """
    Doc: POST https://api.dizparos.com/v1/messaging/send
    Body:
    {
      "channel": "voice",
      "details": {
        "to": "+55...",
        "type": "transfer",
        "transfer_destination": "...",
        "transfer_sip_trunk": {...} # opcional
      }
    }
    """
    if not DIZPAROS_TOKEN:
        raise RuntimeError("DIZPAROS_TOKEN não configurado no Render.")
    if not TRANSFER_DESTINATION:
        raise RuntimeError("TRANSFER_DESTINATION não configurado no Render.")
    if not DIZPAROS_ENDPOINT:
        raise RuntimeError("DIZPAROS_ENDPOINT não configurado no Render.")

    payload: Dict[str, Any] = {
        "channel": "voice",
        "details": {
            "to": phone_e164,
            "type": "transfer",
            "transfer_destination": TRANSFER_DESTINATION,
        },
    }

    # se você quiser usar SIP trunk (ElevenLabs), preenche o bloco abaixo
    if SIP_TRUNK_ADDRESS:
        payload["details"]["transfer_sip_trunk"] = {
            "address": SIP_TRUNK_ADDRESS,
            "port": SIP_TRUNK_PORT,
            "username": SIP_TRUNK_USERNAME,
            "password": SIP_TRUNK_PASSWORD,
        }

    headers = {
        "Content-Type": "application/json",
        # Dizparos costuma aceitar token no body (como no seu painel), mas muitos aceitam também Authorization.
        # Pra garantir, a gente manda NOS DOIS: body e header.
        "Authorization": f"Bearer {DIZPAROS_TOKEN}",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(DIZPAROS_ENDPOINT, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


# ---------- Tick: dispara ligações ----------
@app.post("/tick")
async def tick(body: TickBody):
    sb = get_supabase()

    q = sb.table("campaigns").select("id, concurrency").eq("status", "running")
    if body.campaign_id:
        q = q.eq("id", body.campaign_id)

    campaigns = q.execute().data
    if not campaigns:
        return {"ok": True, "message": "Nenhuma campanha running."}

    results: List[Dict[str, Any]] = []

    for camp in campaigns:
        campaign_id = camp["id"]
        concurrency = int(camp.get("concurrency") or 5)

        # quantas calls em progresso?
        in_prog = sb.table("calls").select("id", count="exact") \
            .eq("campaign_id", campaign_id) \
            .in_("status", ["created", "answered", "transferred"]) \
            .execute()

        current = in_prog.count or 0
        free_slots = max(concurrency - current, 0)

        if free_slots <= 0:
            results.append({"campaign_id": campaign_id, "started": 0, "errors": 0, "reason": "sem slots"})
            continue

        # pega contatos pendentes
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

            # cria registro de call (antes) pra rastrear
            call_row = sb.table("calls").insert({
                "campaign_id": campaign_id,
                "contact_id": contact_id,
                "status": "created",
                "created_at": now_utc_iso(),
            }).execute().data[0]

            # marca contato como calling
            sb.table("contacts").update({
                "status": "calling",
                "attempts": attempts + 1,
                "last_call_id": call_row["id"],
            }).eq("id", contact_id).execute()

            try:
                resp = await dizparos_start_call(phone)

                # doc diz: call_id e messaging_id
                diz_call_id = (
                    resp.get("call_id")
                    or resp.get("id")
                    or resp.get("data", {}).get("call_id")
                )

                if not diz_call_id:
                    raise RuntimeError(f"Resposta do Dizparos sem call_id: {resp}")

                sb.table("calls").update({
                    "dizparos_call_id": str(diz_call_id)
                }).eq("id", call_row["id"]).execute()

                started += 1

            except Exception as e:
                errors += 1
                # falhou disparo
                sb.table("calls").update({
                    "status": "failed",
                    "finished_at": now_utc_iso(),
                }).eq("id", call_row["id"]).execute()

                sb.table("contacts").update({
                    "status": "failed"
                }).eq("id", contact_id).execute()

                # salva erro em call_events
                sb.table("call_events").insert({
                    "call_id": call_row["id"],
                    "event_type": "start_failed",
                    "payload": {"error": str(e), "phone": phone},
                }).execute()

        results.append({"campaign_id": campaign_id, "started": started, "errors": errors, "free_slots": free_slots})

    return {"ok": True, "results": results}


# ---------- Webhook: recebe eventos do Dizparos ----------
@app.post("/webhooks/dizparos")
async def dizparos_webhook(req: Request):
    verify_webhook(req)
    payload = await req.json()
    sb = get_supabase()

    # doc:
    # {
    #   "webhook_event_id": "...",
    #   "type": 2000|2001|2002,
    #   "type_description": "answered|transferred|finished",
    #   "data": { "call_id": "...", ... }
    # }
    t = payload.get("type")
    desc = str(payload.get("type_description") or payload.get("event") or payload.get("type") or "unknown").lower()
    data = payload.get("data") or {}

    diz_call_id = data.get("call_id") or payload.get("call_id") or payload.get("id")
    if not diz_call_id:
        # salva evento mesmo assim pra debug
        sb.table("call_events").insert({
            "call_id": None,
            "event_type": f"unknown_no_call_id:{desc}",
            "payload": payload
        }).execute()
        return {"ok": True, "warning": "call_id ausente, evento salvo como debug"}

    # tenta achar call pelo dizparos_call_id
    call_rows = sb.table("calls") \
        .select("id, contact_id, campaign_id") \
        .eq("dizparos_call_id", str(diz_call_id)) \
        .limit(1).execute().data

    call_id = call_rows[0]["id"] if call_rows else None
    contact_id = call_rows[0]["contact_id"] if call_rows else None

    # salva evento bruto SEMPRE
    sb.table("call_events").insert({
        "call_id": call_id,
        "event_type": desc,
        "payload": payload
    }).execute()

    # se o call ainda não existe, cria placeholder
    # (acontece raro, mas é bom ter)
    if not call_rows:
        new_call = sb.table("calls").insert({
            "dizparos_call_id": str(diz_call_id),
            "status": "created",
            "created_at": now_utc_iso(),
        }).execute().data[0]
        call_id = new_call["id"]
        return {"ok": True, "warning": "call placeholder criado, aguarde o tick criar vínculo com contact"}

    # normaliza decisões por type
    # 2000 answered / 2001 transferred / 2002 finished
    if t == 2000 or "answered" in desc:
        sb.table("calls").update({"status": "answered"}).eq("id", call_id).execute()

    elif t == 2001 or "transferred" in desc:
        # doc: data.success
        sb.table("calls").update({"status": "transferred"}).eq("id", call_id).execute()

    elif t == 2002 or "finished" in desc or "completed" in desc:
        sb.table("calls").update({
            "status": "finished",
            "duration": data.get("duration"),
            "cost": data.get("cost"),
            "recording_url": data.get("recording_url"),
            "finished_at": now_utc_iso(),
        }).eq("id", call_id).execute()

        # marca contato como done se existir
        if contact_id:
            sb.table("contacts").update({"status": "done"}).eq("id", contact_id).execute()

    return {"ok": True, "call_id": call_id, "dizparos_call_id": str(diz_call_id)}
