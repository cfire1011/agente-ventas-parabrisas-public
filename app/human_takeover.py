"""Si el humano escribe (Inbox o dashboard), el bot se calla en ESE chat."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from threading import Lock
from typing import Any

_TTL = 120.0
_lock = Lock()
_bot_mids: dict[str, float] = {}
_bot_sends: dict[str, list[tuple[float, str]]] = {}


def reset_for_tests() -> None:
    with _lock:
        _bot_mids.clear()
        _bot_sends.clear()


def _prune(now: float) -> None:
    dead = [mid for mid, ts in _bot_mids.items() if now - ts > _TTL]
    for mid in dead:
        _bot_mids.pop(mid, None)
    for psid, rows in list(_bot_sends.items()):
        keep = [(ts, txt) for ts, txt in rows if now - ts > 0 and now - ts <= _TTL]
        if keep:
            _bot_sends[psid] = keep
        else:
            _bot_sends.pop(psid, None)


def remember_bot_send(psid: str, text: str = "", mid: str | None = None) -> None:
    """Marca un mensaje que mandó ESTE proceso, para no silenciarnos con nuestro eco."""
    now = time.time()
    psid = str(psid or "")
    with _lock:
        _prune(now)
        if psid:
            _bot_sends.setdefault(psid, []).append((now, (text or "")[:180]))
        if mid:
            _bot_mids[str(mid)] = now


def our_app_id() -> str:
    return (os.getenv("APP_ID") or os.getenv("FB_APP_ID") or "").strip()


def is_our_bot_echo(message: dict[str, Any] | None, recipient_psid: str = "") -> bool:
    msg = message or {}
    mid = str(msg.get("mid") or msg.get("id") or "")
    now = time.time()
    with _lock:
        _prune(now)
        if mid and mid in _bot_mids:
            return True
        if recipient_psid:
            text = (msg.get("text") or "")[:180]
            for ts, sent in _bot_sends.get(str(recipient_psid), []):
                if text and sent and text == sent:
                    return True
    app_id = msg.get("app_id")
    ours = our_app_id()
    if ours and app_id is not None and str(app_id) == ours:
        return True
    return False


def human_takeover_from_event(
    event: dict[str, Any] | None,
    page_id: str = "",
) -> tuple[str, str] | None:
    """Si el evento es un mensaje TUYO (página), devuelve (psid_cliente, texto)."""
    event = event or {}
    message = event.get("message") or {}
    sender = str((event.get("sender") or {}).get("id") or "")
    recipient = str((event.get("recipient") or {}).get("id") or "")
    page = str(page_id or os.getenv("PAGE_ID") or "")

    is_echo = bool(message.get("is_echo"))
    from_page = bool(page and sender == page)

    if not is_echo and not from_page:
        return None

    customer = recipient if (is_echo or from_page) else ""
    if not customer or (page and customer == page):
        return None
    if is_our_bot_echo(message, customer):
        return None
    text = str(message.get("text") or "")
    # Follow-up automatico de Meta ("Hola, Felix! Queriamos saber si tienes
    # alguna pregunta") NO es un humano. Si lo tratamos como inbox, el bot
    # se apaga y el cliente que despues da el auto nunca recibe horarios.
    if _is_meta_auto_followup(text):
        return None
    return customer, text


_META_AUTO = re.compile(
    r"(?i)quer[ií]amos saber si tienes alguna pregunta|"
    r"wanted to (know|see) if you (have|had) (any )?question|"
    r"just wanted to (see|know) if you|"
    r"todav[ií]a te interesa|sigue interesado|still interested|"
    r"we('?re| are) checking in|"
    r"thanks for (getting in touch|messaging us)|"
    r"gracias por (escribirnos|contactar)|"
    r"c[oó]mo podemos ayudarte hoy\??\s*$"
)


def is_meta_auto_followup(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_META_AUTO.search(t))


def _is_meta_auto_followup(text: str) -> bool:
    return is_meta_auto_followup(text)


def kill_meta_auto_message(mid: str) -> bool:
    """Intenta deshacer el mensaje automático de Meta (unsend)."""
    mid = str(mid or "").strip()
    if not mid:
        return False
    try:
        import httpx
        from . import legacy
        r = httpx.delete(
            f"{legacy.GRAPH}/{mid}",
            params=legacy._graph_params(),
            timeout=12,
            trust_env=False,
        )
        if r.status_code < 400:
            return True
        logging.getLogger("agente").warning("unsend meta-auto fail %s %s", mid[:40], r.text[:200])
    except Exception:
        logging.getLogger("agente").exception("unsend meta-auto")
    return False


def strip_meta_greetings() -> dict:
    """Quita greeting / ice breakers / get started de la Page (Messenger Profile)."""
    out = {"ok": False, "deleted": []}
    try:
        import httpx
        from . import legacy
        # "greeting" ya no se puede borrar por API. El "Hola X, queríamos saber…"
        # es Instant Reply del Inbox (Automations). Acá solo se quita lo que Graph permite.
        fields = ["ice_breakers", "get_started"]
        r = httpx.request(
            "DELETE",
            f"{legacy.GRAPH}/me/messenger_profile",
            params=legacy._graph_params(),
            content=json.dumps({"fields": fields}),
            headers={"Content-Type": "application/json"},
            timeout=15,
            trust_env=False,
        )
        out["status"] = r.status_code
        out["body"] = (r.text or "")[:300]
        out["ok"] = r.status_code < 400
        if out["ok"]:
            out["deleted"] = fields
    except Exception as e:
        out["error"] = str(e)
    return out


def handle_meta_auto_echo(event: dict[str, Any] | None) -> bool:
    """Si el eco es el 'Hola X, queríamos saber…' de Meta: borrar y no mutear."""
    event = event or {}
    message = event.get("message") or {}
    if not message.get("is_echo"):
        return False
    text = str(message.get("text") or "")
    if not is_meta_auto_followup(text):
        return False
    mid = str(message.get("mid") or message.get("id") or "")
    killed = kill_meta_auto_message(mid) if mid else False
    try:
        from .aprender import rec
        rec("META_AUTO_BLOCK", str((event.get("recipient") or {}).get("id") or ""), f"unsend={'ok' if killed else 'fail'} {text[:80]}")
    except Exception:
        pass
    return True
