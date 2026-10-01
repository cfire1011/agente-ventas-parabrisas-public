"""Recordatorios a clientes que se quedaron callados.

- Sin horarios enviados: "he estado esperando tu respuesta para brindarte los horarios"
- Sin carro: el mismo ciclo, a los 2 días, pidiendo año/marca/modelo

No toca a quien ya vio horarios, eligió día, o lo está atendiendo un humano.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

log = logging.getLogger("agente.nudge")

SILENCE_DAYS = int(os.getenv("NUDGE_SILENCE_DAYS", "2") or "2")
MAX_WINDOW_DAYS = 7
INTERVAL_SEC = int(os.getenv("NUDGE_INTERVAL_SEC", "1800") or "1800")
FIRST_DELAY_SEC = 90
RETRY_FAIL_HOURS = 6

MSG_HORARIOS = (
    "Hola, he estado esperando tu respuesta para brindarte los horarios. "
    "¿Me confirmas y te los paso?"
)
MSG_AUTO = (
    "Hola, te escribo de nuevo. Para darte los horarios necesito el año, "
    "marca y modelo del carro. ¿Me los pasas?"
)

KIND_HORARIOS = "horarios"
KIND_AUTO = "auto"

_DONE = {"finalizado", "ganado", "perdido", "cerrando"}
_SKIP_REASONS = (
    "humano_inbox",
    "slot_elegido_manual",
    "horarios_pide_tel",
    "horarios_tiene_tel",
    "handle_exception",
)
_SLOTS_MSG = re.compile(
    r"(?is)("
    r"env[ií]o a domicilio|"
    r"horarios en (que podemos|taller)|"
    r"cu[aá]l te sirve|"
    r"\b\d+\)\s*.{0,40}\b(am|pm)\b|"
    r"de \d{1,2}(?::\d{2})?\s*(am|pm)\s*a\s*\d{1,2}"
    r")"
)

_stop = threading.Event()
_thread: threading.Thread | None = None


def _now() -> datetime:
    return datetime.utcnow()


def parse_dt(value) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        return None


def _history(lead: dict) -> list:
    raw = lead.get("history") or "[]"
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def horarios_enviados(lead: dict | None) -> bool:
    lead = lead or {}
    raw = (lead.get("slots_json") or "").strip()
    if raw and raw not in ("[]", "null", "None"):
        try:
            opts = json.loads(raw)
            if isinstance(opts, list) and any(opts):
                return True
        except Exception:
            if len(raw) > 8:
                return True
    if lead.get("slot_date") or lead.get("slot_id"):
        return True
    for m in _history(lead):
        if not isinstance(m, dict):
            continue
        if (m.get("role") or "") not in ("assistant", "page", "agent"):
            continue
        if _SLOTS_MSG.search(str(m.get("content") or "")):
            return True
    return False


def _reason(lead: dict) -> str:
    return str(lead.get("slots_error") or "").strip().lower()


def _nudge_kinds(lead: dict) -> set[str]:
    raw = str(lead.get("nudge_sent") or "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _humano_activo(lead: dict) -> bool:
    reason = _reason(lead)
    if any(k in reason for k in _SKIP_REASONS):
        return True
    note = str(lead.get("admin_note") or "")
    if "humano_inbox" in note:
        return True
    return False


def _tiene_charla(lead: dict) -> bool:
    if _history(lead):
        return True
    if (lead.get("name") or "").strip():
        return True
    if (lead.get("year") or lead.get("make") or lead.get("model") or "").strip():
        return True
    if (lead.get("zip") or "").strip():
        return True
    return False


def silencio_desde(lead: dict) -> datetime | None:
    return parse_dt(lead.get("last_inbound_at")) or parse_dt(lead.get("updated_at"))


def classify_nudge(lead: dict | None, now: datetime | None = None, silence_days: int | None = None) -> str | None:
    """auto | horarios | None."""
    from . import legacy

    lead = lead or {}
    now = now or _now()
    days = SILENCE_DAYS if silence_days is None else silence_days
    stage = (lead.get("stage") or "").strip().lower()
    if stage in _DONE:
        return None
    if lead.get("work_order") or lead.get("booked_at") or lead.get("finished_at"):
        return None
    if not _tiene_charla(lead):
        return None
    if horarios_enviados(lead):
        return None
    if _humano_activo(lead):
        return None

    start = silencio_desde(lead)
    if not start:
        return None
    silent_for = now - start
    if silent_for < timedelta(days=max(1, days)):
        return None
    if silent_for > timedelta(days=MAX_WINDOW_DAYS):
        return None

    already = _nudge_kinds(lead)
    failed_at = parse_dt(lead.get("nudge_at"))
    if failed_at and not already and (now - failed_at) < timedelta(hours=RETRY_FAIL_HOURS):
        return None

    if legacy._has_vehicle(lead):
        if KIND_HORARIOS in already:
            return None
        return KIND_HORARIOS
    if KIND_AUTO in already:
        return None
    return KIND_AUTO


def mensaje_nudge(kind: str) -> str:
    if kind == KIND_AUTO:
        return MSG_AUTO
    return MSG_HORARIOS


def _merge_kinds(lead: dict, kind: str) -> str:
    kinds = _nudge_kinds(lead)
    kinds.add(kind)
    order = [k for k in (KIND_AUTO, KIND_HORARIOS) if k in kinds]
    return ",".join(order)


def apply_nudge(psid: str, lead: dict, kind: str, send_fn: Callable[..., bool] | None = None) -> bool:
    from . import legacy

    text = mensaje_nudge(kind)
    sender = send_fn or (lambda p, t: legacy.send_msg(p, t, tag="HUMAN_AGENT"))
    ok = bool(sender(psid, text))
    now = _now().isoformat()
    fields = {"nudge_at": now}
    if ok:
        fields["nudge_sent"] = _merge_kinds(lead, kind)
        if (lead.get("stage") or "").lower() in ("agente", "handoff", "humano", "humano_pendiente", "nuevo"):
            fields["stage"] = "calificando"
        fields["bot_muted"] = ""
        legacy.upsert_lead(psid, **fields)
        legacy.append_hist(psid, "assistant", text)
        try:
            from .aprender import rec
            rec("NUDGE", psid, kind, lead)
        except Exception:
            pass
        log.info("nudge %s psid=%s", kind, psid)
    else:
        legacy.upsert_lead(psid, **fields)
        try:
            from .aprender import rec
            rec("NUDGE_FAIL", psid, kind, lead)
        except Exception:
            pass
        log.warning("nudge fail %s psid=%s", kind, psid)
    return ok


def run_nudges(
    now: datetime | None = None,
    send_fn: Callable[..., bool] | None = None,
    leads: list[dict] | None = None,
) -> list[dict]:
    from . import legacy

    now = now or _now()
    rows = leads if leads is not None else legacy.list_leads(official_only=True)
    out: list[dict] = []
    for lead in rows:
        psid = str(lead.get("psid") or "")
        if not legacy.is_real_messenger_psid(psid):
            continue
        kind = classify_nudge(lead, now=now)
        if not kind:
            continue
        ok = apply_nudge(psid, lead, kind, send_fn=send_fn)
        out.append({"psid": psid, "kind": kind, "ok": ok})
    return out


def _loop() -> None:
    if FIRST_DELAY_SEC > 0 and _stop.wait(FIRST_DELAY_SEC):
        return
    while not _stop.is_set():
        try:
            done = run_nudges()
            if done:
                log.info("nudge batch %s", done)
        except Exception:
            log.exception("nudge loop")
        if _stop.wait(max(60, INTERVAL_SEC)):
            return


def start_nudge_loop() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="nudge-loop", daemon=True)
    _thread.start()
    log.info("nudge loop on silence=%sd interval=%ss", SILENCE_DAYS, INTERVAL_SEC)


def stop_nudge_loop() -> None:
    _stop.set()
