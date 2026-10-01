from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from .db import append_history, get_lead, upsert_lead
from .flow import force_send_slots, is_force_slots_command, run_agent
from .human_takeover import handle_meta_auto_echo, human_takeover_from_event, is_meta_auto_followup
from .messenger import iter_messenger_events, parse_event_message, send_message, send_typing, user_name, verify_signature
from . import legacy

log = logging.getLogger("agente.webhook")
router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=4)
_pending: dict[str, dict] = {}
_seq: dict[str, int] = defaultdict(int)
_lock = Lock()
DEBOUNCE = 0.6
REPLY_WAIT_MIN = 4.0
REPLY_WAIT_MAX = 4.0


def _is_brand_new_lead(lead: dict | None) -> bool:
    """True si este psid nunca tuvo ni un mensaje real (user/assistant) ni mute.

    Se usa para distinguir el "Instant Reply" automatico de Meta Ads (que llega
    como eco de la Pagina en el primerisimo evento de la conversacion, antes de
    que nuestro bot o un humano hayan escrito algo) de un humano real tomando
    un chat que ya estaba en curso.
    """
    lead = lead or {}
    if not lead:
        return True
    if (lead.get("bot_muted") or 0):
        return False
    if (lead.get("slots_error") or ""):
        return False
    hist = lead.get("history") or "[]"
    try:
        import json as _json
        rows = _json.loads(hist) if isinstance(hist, str) else (hist or [])
    except Exception:
        rows = []
    return not rows


def _can_resume_after_human_greeting(lead: dict | None, text: str) -> bool:
    """Reanuda solo si el humano anterior apenas saludó y el cliente ya dio el auto."""
    lead = lead or {}
    if (lead.get("slots_error") or "") != "humano_inbox":
        return False
    previous = legacy._hist_texts(lead, "assistant")
    if not previous:
        return False
    last = legacy._norm_es(previous[-1])
    if not re.fullmatch(
        r"\s*(hola+|holi+|saludos+|buenas|buenos dias|buenas tardes|buenas noches|hi+|hello+|hey+)[!. ]*\s*",
        last,
    ):
        return False
    vehicle = legacy.extract_vehicle_from_text(text or "")
    known = sum(bool(vehicle.get(k)) for k in ("year", "make", "model"))
    return known >= 2


def _can_continue_bot_owned_mute(lead: dict | None) -> bool:
    """Estados donde el bot se silenció él mismo, pero debe leer teléfono/VIN."""
    lead = lead or {}
    err = (lead.get("slots_error") or "").strip()
    if err == "vin_recibido":
        return False
    if (lead.get("stage") or "").lower() in ("perdido", "finalizado", "ganado"):
        return False
    return err.startswith("horarios_") or err == "vin_pendiente"


def _mute_gate(psid: str, lead0: dict | None, text: str) -> tuple[bool, dict | None]:
    """Decide UNA sola vez si este mensaje entrante debe ignorarse por mute.

    Esta es LA función que decide "por qué no responde" — toda la lógica de
    mute/resume vivía repartida como if/elif sueltos dentro de _handle(). Se
    saca a una función propia, con un caso por rama y su motivo, para que la
    próxima vez que algo "no responda" sea fácil auditar/testear cada caso
    por separado en vez de tener que releer todo _handle().

    Devuelve (debe_ignorar, lead_actualizado).
    - debe_ignorar=False, lead=lead0            -> el chat no estaba muteado, seguir normal.
    - debe_ignorar=False, lead=lead_actualizado  -> estaba muteado pero se reactivó (saludo humano + auto ya dado).
    - debe_ignorar=True,  lead=None              -> se ignora este mensaje (mute sigue vigente).
    """
    if not legacy.is_bot_muted(lead0):
        return False, lead0
    if _can_continue_bot_owned_mute(lead0):
        # El propio bot se auto-silenció esperando teléfono/VIN: sigue leyendo,
        # run_agent() maneja este caso internamente (no es "mute humano" real).
        log.info("IN continue bot-owned mute %s | %s", psid, (text or "")[:80])
        return False, lead0
    if _can_resume_after_human_greeting(lead0, text):
        upsert_lead(psid, bot_muted="", slots_error="", stage="calificando")
        log.warning("IN resume after human greeting %s | %s", psid, (text or "")[:80])
        return False, get_lead(psid)
    log.info("IN muted %s | %s", psid, (text or "")[:80])
    return True, None


def _wait_before_reply(psid: str, started: float) -> None:
    """Espera al menos 4 s en total con 'escribiendo…' para no contestar antes de 4s."""
    need = random.uniform(REPLY_WAIT_MIN, REPLY_WAIT_MAX)
    left = need - (time.monotonic() - started)
    if left <= 0:
        send_typing(psid, True)
        return
    # El indicador de typing se cae; lo refresco a la mitad.
    mid = left / 2
    if mid > 0.3:
        send_typing(psid, True)
        time.sleep(mid)
        send_typing(psid, True)
        time.sleep(max(0.0, left - mid))
    else:
        send_typing(psid, True)
        time.sleep(left)
    send_typing(psid, True)


@router.get("/webhook")
def verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == os.getenv("VERIFY_TOKEN"):
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


def _handle(psid: str, text: str, images: list[str], seq: int | None = None) -> None:
    started = time.monotonic()
    try:
        upsert_lead(psid, last_inbound_at=datetime.utcnow().isoformat())
        lead0 = get_lead(psid)
        ignore, lead0 = _mute_gate(psid, lead0, text)
        if ignore:
            return
        send_typing(psid, True)
        if not get_lead(psid).get("name"):
            n = user_name(psid)
            if n:
                upsert_lead(psid, name=n)
        reply = run_agent(psid, text, images)
        # El mute del mismo turno (llamar por Facebook y apagar) NO debe tragarse este mensaje.
        if reply and str(reply).strip():
            _wait_before_reply(psid, started)
            # El humano pudo responder mientras el bot esperaba 4 segundos.
            # Cancela la respuesta preparada si entró otro evento o el chat
            # quedó silenciado antes del POST a Messenger.
            with _lock:
                stale = seq is not None and _seq[psid] != seq
            # OJO: no cancelar solo por is_bot_muted() aqui — el propio run_agent
            # puede auto-silenciar el chat como parte de ESTA MISMA respuesta
            # (ej. dar precio+horario y luego entregarle el chat al humano).
            # La unica senal confiable de que un humano intervino DURANTE la
            # espera es que llego un evento nuevo (seq cambio).
            if stale:
                send_typing(psid, False)
                log.warning("OUT cancel human-takeover %s", psid)
                return
            send_typing(psid, False)
            ok = send_message(psid, reply)
            if ok:
                log.info("OUT %s | %s", psid, (reply or "")[:160])
            else:
                log.warning("OUT skip/fail %s | %s", psid, (reply or "")[:160])
        else:
            send_typing(psid, False)
    except Exception:
        send_typing(psid, False)
        log.exception("handle")
        try:
            if not legacy.is_bot_muted(get_lead(psid)):
                msg = legacy.handoff_to_human(psid, reason="handle_exception")
                send_message(psid, msg)
        except Exception:
            pass


async def _flush(psid: str, seq: int) -> None:
    await asyncio.sleep(DEBOUNCE)
    with _lock:
        if _seq[psid] != seq:
            return
        bucket = _pending.pop(psid, None)
    if not bucket:
        return
    texts = [t for t in bucket.get("texts", []) if t]
    images = bucket.get("images", [])
    combined = " | ".join(texts) if len(texts) > 1 else (texts[0] if texts else "")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _handle, psid, combined, images, seq)


@router.post("/webhook")
async def incoming(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256"), os.getenv("APP_SECRET")):
        log.warning("webhook firma invalida")
        return Response(status_code=403)
    try:
        data = json.loads(body.decode() or "{}")
    except Exception:
        log.warning("webhook body no JSON")
        return {"status": "ok"}

    try:
        from .fb_inbox import ingest_payload
        ingest_payload(data, source="webhook")
    except Exception:
        log.exception("fb_inbox ingest")

    page_id = os.getenv("PAGE_ID", "")
    n_msg = 0
    for event in iter_messenger_events(data):
        n_msg += 1
        message = event.get("message") or {}
        sender = (event.get("sender") or {}).get("id")
        if handle_meta_auto_echo(event):
            log.warning("META auto-saludo bloqueado / unsend | %s", ((event.get("message") or {}).get("text") or "")[:80])
            continue
        human = human_takeover_from_event(event, page_id)
        if human:
            psid, human_text = human
            with _lock:
                _pending.pop(psid, None)
                _seq[psid] += 1
            try:
                lead_echo = get_lead(psid) or {}
                prevs = []
                try:
                    prevs = legacy._hist_texts(lead_echo, "assistant")
                except Exception:
                    prevs = []
                last_out = (lead_echo.get("last_outbound") or "") or (prevs[-1] if prevs else "")
                if human_text and last_out and legacy._same_outbound(last_out, human_text):
                    log.info("echo de nuestro ultimo mensaje — no es humano psid=%s", psid)
                    continue
                if _is_brand_new_lead(lead_echo):
                    # Es el "Instant Reply" automatico de Meta para comentarios de
                    # anuncio (texto configurable: "holaa", "saludoss", "me comenta
                    # si le interesa", "¡Hola X! ¿Como podemos ayudarte?", etc). Llega
                    # como eco de la Pagina ANTES de que el bot le haya respondido a
                    # este lead ni una vez. No es un humano tomando el chat: si lo
                    # muteamos aqui, el lead nuevo se queda sin respuesta para siempre.
                    log.warning(
                        "META instant-reply de anuncio (lead nuevo, no muteo) psid=%s | %s",
                        psid, (human_text or "")[:80],
                    )
                    continue
                if is_force_slots_command(human_text):
                    log.warning("CMD !hora psid=%s — mando horarios | %s", psid, (human_text or "")[:80])
                    reply = force_send_slots(psid, command_text=human_text)
                    if reply:
                        send_message(psid, reply)
                    continue
                if human_text:
                    append_history(psid, "assistant", human_text)
                # Cualquier mensaje del humano (aunque sea "hola" o "ok") apaga
                # el bot por completo en este chat. Sin excepciones.
                legacy._mute_for_human(psid, reason="humano_inbox")
                log.warning("HUMANO inbox psid=%s — bot APAGADO en este chat | %s", psid, (human_text or "")[:80])
            except Exception:
                log.exception("humano inbox")
            continue
        if message.get("is_echo"):
            continue
        if event.get("delivery") or event.get("read") or event.get("reaction"):
            continue
        if not message and not event.get("postback"):
            continue
        psid, text, images = parse_event_message(event)
        if not text and not images:
            continue
        if text and is_meta_auto_followup(text):
            log.info("ignoro texto auto de Meta psid=%s", psid)
            continue
        if str(sender) == str(page_id):
            continue
        with _lock:
            bucket = _pending.get(psid) or {"texts": [], "images": []}
            if text:
                bucket["texts"].append(text)
            bucket["images"].extend(images)
            _pending[psid] = bucket
            _seq[psid] += 1
            seq = _seq[psid]
        background_tasks.add_task(_flush, psid, seq)

    if n_msg == 0:
        log.info("webhook sin messaging: keys=%s", list(data.keys()))
    return {"status": "ok"}
