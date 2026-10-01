from __future__ import annotations

import json
import re
from datetime import datetime

from . import legacy

SLOTS_REPEAT_WINDOW_HOURS = 48

# Ramon Sanchez — horarios 1 sola vez; precio/pago responden corto sin re-listar.
already_showed_slots = legacy._already_showed_slots
reply_like_human = legacy.reply_like_human
reply_price_short = legacy._reply_price_short
reply_vehicle_short = legacy._reply_vehicle_short
reply_greeting_short = legacy._reply_greeting_short
reply_location_short = legacy._reply_location_short
blocker_need = legacy._blocker_need
soft_day_cta = legacy._soft_day_cta

# Ricardo Lazo / Marcos / Adrian — techo, trasero, multipieza no siguen el flujo automático.
is_non_windshield_glass = legacy._is_non_windshield_glass

# Angel Oliva — urgencia hoy/mañana y taller antes que domicilio cuando aplica.
format_slots_message = legacy.slots_message
taller_offer_for_message = legacy._taller_offer_for_message
msg_day_unavailable = legacy._msg_day_unavailable
msg_time_unavailable = legacy._msg_time_unavailable
msg_clarify_day = legacy._msg_clarify_day

# FAQs operativas.
note_slot_for_human = legacy._note_slot_for_human
rule_fallback_reply = legacy._rule_fallback_reply
scrub_client_reply = legacy._scrub_client_reply
ensure_complete_reply = legacy._ensure_complete_reply


def cached_slots(lead: dict | None) -> list[dict]:
    lead = lead or {}
    raw = lead.get("slots_json") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def has_slots_cache(lead: dict | None) -> bool:
    return bool(cached_slots(lead))


def slots_cache_age_hours(lead: dict | None) -> float | None:
    lead = lead or {}
    stamp = (lead.get("slots_fetched_at") or "").strip()
    if not stamp:
        return None
    try:
        fetched = datetime.fromisoformat(stamp)
    except Exception:
        return None
    return (datetime.utcnow() - fetched).total_seconds() / 3600


def slots_cache_is_recent(lead: dict | None, max_hours: int = SLOTS_REPEAT_WINDOW_HOURS) -> bool:
    age = slots_cache_age_hours(lead)
    return age is not None and age <= max_hours


def client_explicitly_wants_more_slots(text: str, analysis: dict | None = None) -> bool:
    """
    Ramon Sanchez + refuerzo usuario: re-listar solo si el cliente lo pide clarísimo.
    """
    low = legacy._norm_es(text or "")
    if not low:
        return False
    patterns = [
        r"\botros?\s+dias?\b",
        r"\botras?\s+fechas?\b",
        r"\botras?\s+opciones?\b",
        r"\bque\s+mas\s+hay\b",
        r"\bque\s+otros?\s+(dias|horarios)\b",
        r"\bmas\s+horarios\b",
        r"\bmas\s+disponibilidad\b",
        r"\bhorarios?\s+de\s+nuevo\b",
        r"\bmanda(me)?\s+los\s+horarios?\s+otra\s+vez\b",
        r"\bense[nñ]a(me)?\s+otra\s+vez\b",
        r"\bshow\s+me\s+again\b",
        r"\bsend\s+the\s+times\s+again\b",
        r"\bother\s+days?\b",
        r"\bother\s+times?\b",
        r"\bwhat\s+else\s+do\s+you\s+have\b",
        r"\banything\s+else\b",
        r"\bmore\s+(days|times|availability)\b",
        r"\bnone\s+of\s+those\s+work\b",
        r"\bthose\s+don'?t\s+work\b",
        r"\bno\s+me\s+sirven?\s+esos\b",
        r"\bninguno\s+me\s+sirve\b",
        r"\bno\s+me\s+sirve\s+ninguno\b",
    ]
    return any(re.search(pat, low) for pat in patterns)


def schedule_question_after_stale_cache(
    lead: dict | None,
    text: str,
    analysis: dict | None = None,
    max_age_hours: int = SLOTS_REPEAT_WINDOW_HOURS,
) -> bool:
    low = legacy._norm_es(text or "")
    analysis = analysis or {}
    if not has_slots_cache(lead) or slots_cache_is_recent(lead, max_age_hours):
        return False
    if analysis.get("is_schedule"):
        return True
    if legacy._parse_when_intent(text or "").get("has_when"):
        return True
    return bool(
        re.search(
            r"\b(horario|horarios|disponib|que dia|qu[eé] dia|today|tomorrow|hoy|manana|mañana|asap)\b",
            low,
        )
    )


def should_show_full_slots(
    lead: dict | None,
    text: str,
    analysis: dict | None = None,
    max_age_hours: int = SLOTS_REPEAT_WINDOW_HOURS,
) -> bool:
    lead = lead or {}
    if not has_slots_cache(lead) or not already_showed_slots(lead):
        return True
    if client_explicitly_wants_more_slots(text, analysis):
        return True
    if schedule_question_after_stale_cache(lead, text, analysis, max_age_hours=max_age_hours):
        return True
    return False


def short_slots_cta(lead: dict | None, text: str = "") -> str:
    """
    Respuesta central anti-spam: jamás re-lista por accidente.
    """
    lead = lead or {}
    low = legacy._norm_es(text or "")
    ambiguous = low in {
        "",
        "ok",
        "okay",
        "dale",
        "va",
        "si",
        "sí",
        "gracias",
        "thanks",
        "thank you",
        "👍",
        "👌",
    }
    lang = legacy._client_lang(lead, "" if ambiguous else text, psid=lead.get("psid"))
    mode = (lead.get("service_mode") or "domicilio").lower()
    if mode == "sucursal":
        return legacy._t(
            lang,
            "De los horarios que te pasé, ¿cuál te acomoda en taller?",
            "Which of the times I sent works for you for the shop?",
        )
    return legacy._t(
        lang,
        "De los días que te pasé, ¿cuál te acomoda?",
        "Which of the days I sent works for you?",
    )
