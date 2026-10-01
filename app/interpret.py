"""Interpreta mensajes mal escritos: cualquier cosa que una maquina pueda leer.

Regex primero. Si falta auto o zip y el texto parece tener dato, una sola
pasada de LLM. Nada se guarda si no valida contra Safelite / zip USA.
"""
from __future__ import annotations

import logging
import re

from . import llm_extract
from .vehicle_norm import normalize_vehicle
from .zip_norm import US_STATES, _fuzzy_city, extract_zip, resolve_zip

log = logging.getLogger("agente.interpret")

_SKIP = re.compile(
    r"(?i)^[\W_]*(ok|okay|si|sí|no|gracias|thanks|bien|listo|dale|vale|"
    r"perfecto|hola|buenas|buenos dias|hey|hi)[\W_]*$"
)


def worth_it(text: str | None) -> bool:
    t = (text or "").strip()
    if len(t) < 4 or len(t) > 400:
        return False
    if _SKIP.fullmatch(t):
        return False
    return bool(re.search(r"[a-zA-ZáéíóúñÁÉÍÓÚÑ]{3,}|\d", t))


def _vehicle_complete(lead: dict | None) -> bool:
    lead = lead or {}
    return bool((lead.get("year") or "").strip() and (lead.get("make") or "").strip() and (lead.get("model") or "").strip())


def _zip_complete(lead: dict | None) -> bool:
    z = str((lead or {}).get("zip") or "")
    return bool(re.fullmatch(r"\d{5}", z))


def fill_from_messy(text: str, lead: dict | None = None, raw_fn=None) -> dict:
    """
    Completa year/make/model/zip/city/state desde un mensaje sucio.

    Solo rellena huecos. Valida auto contra catalogo Safelite y zip/ciudad
    contra zip USA. Si el LLM inventa, se descarta.
    """
    lead = lead or {}
    if not worth_it(text):
        return {}
    if _vehicle_complete(lead) and _zip_complete(lead):
        return {}

    raw_fn = raw_fn or llm_extract.extract_raw
    try:
        data = raw_fn(text) or {}
    except Exception:
        log.exception("interpret llm")
        return {}

    out: dict[str, str] = {}

    if not _vehicle_complete(lead):
        year = str(data.get("year") or "").strip()
        if not re.fullmatch(r"(19|20)\d{2}", year):
            year = (lead.get("year") or "").strip()
        make = str(data.get("make") or lead.get("make") or "").strip()
        model = str(data.get("model") or lead.get("model") or "").strip()
        if year or make or model:
            try:
                norm = normalize_vehicle(year, make, model)
            except Exception:
                norm = {}
            if year and not (lead.get("year") or "").strip():
                out["year"] = year
            if norm.get("make") and not (lead.get("make") or "").strip():
                out["make"] = norm["make"]
            elif make and not (lead.get("make") or "").strip() and not norm.get("make"):
                # sin catalogo no guardamos marca suelta inventada
                pass
            if norm.get("model") and not (lead.get("model") or "").strip():
                out["model"] = norm["model"]

    if not _zip_complete(lead):
        z = extract_zip(str(data.get("zip") or "")) or extract_zip(text)
        city = str(data.get("city") or "").strip()
        state = str(data.get("state") or "").strip().lower()
        if state not in US_STATES:
            state = ""
        if city:
            city = _fuzzy_city(city, state)
        if not z:
            blob = " ".join(p for p in (city, state, text) if p)
            try:
                res = resolve_zip(blob) or {}
            except Exception:
                res = {}
            z = res.get("zip") or ""
            city = city or res.get("city") or ""
            state = state or res.get("state") or ""
        if z:
            out["zip"] = z
        if city and not (lead.get("city") or "").strip():
            # "estoy en Louisiana" no es ciudad "Nissan Frontier Estoy En"
            if re.search(
                r"(?i)(estoy|nissan|toyota|honda|ford|chevy|frontier|parabrisas|cuanto)",
                city,
            ):
                city = ""
            else:
                out["city"] = city
        if state and not (lead.get("state") or "").strip():
            out["state"] = state

    if out:
        log.info("interpret messy %r -> %s", (text or "")[:70], out)
    return out
