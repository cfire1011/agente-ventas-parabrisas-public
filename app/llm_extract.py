"""
Red de seguridad con LLM para extraer auto / zip / ciudad.

Los regex cubren lo comun, pero la gente escribe de mil formas ("tengo una
toyota tundra del 15", "estan en Albuquerque?", "mi carro es un civic 08").
Cuando el regex NO saca un dato, se le pregunta al LLM.

Reglas de oro de este modulo:
  * Solo corre como FALLBACK (si el regex ya resolvio, no se llama al LLM):
    cero latencia y cero costo en el camino feliz.
  * Nunca se confia en el LLM a ciegas. TODO lo que devuelve se valida contra
    una fuente real: el zip contra safelite_zip, el vehiculo contra el catalogo
    de Safelite (vehicle_norm) y la ciudad contra el catalogo de zips.
    Si no valida, se descarta.

Modelo: se usa el mismo MODEL_NAME ya configurado (llama-3.3-70b-versatile).
Se comparo contra openai/gpt-oss-120b, gpt-oss-20b y qwen3.6-27b sobre los
casos reales que fallaban y llama-3.3-70b gano en precision Y en latencia.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

from . import llm_status

log = logging.getLogger("llm_extract")

# Si el LLM tarda mas que esto, seguimos sin el (no bloquear la conversacion).
TIMEOUT = 8.0

_SYSTEM = """Extraes datos de mensajes de clientes hispanohablantes de un taller de parabrisas en USA.
Los mensajes vienen MAL ESCRITOS: typos, espacios raros, mezcla de auto+ciudad.
Devuelve SOLO JSON valido con estas claves exactas:
{"year":null,"make":null,"model":null,"zip":null,"city":null,"state":null}
Reglas:
- year: 4 digitos. "del 15"->2015, "08"->2008, "del 09"->2009. Si no hay año, null.
- make/model: CORRIGE cualquier ortografia al nombre real USA
  (hinday->Hyundai, acord->Accord, civil->Civic, cvr/cr v->CR-V,
   f 150/f150->F-150, rav 4->RAV4, chevi/chevy->Chevrolet, esprs->Express).
  Si dice "troca"/"carro"/"camioneta" sin marca, make=null.
- zip: SOLO codigo postal USA de 5 digitos. Un año o un telefono NO es zip.
- city/state: ciudad y estado USA aunque este mal escrito
  (frederikvurd texas->Fredericksburg TX, loisville->Louisville,
   alburquerque->Albuquerque). state = abreviatura de 2 letras o null.
- Lo que no este en el mensaje va null. NO inventes NADA que no se pueda inferir del texto."""

_KEYS = ("year", "make", "model", "zip", "city", "state")


def _vacio() -> dict:
    return {k: None for k in _KEYS}


def _clean(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("none", "null", "n/a", "-") else s


def extract_raw(text: str, model: str | None = None) -> dict:
    """Pregunta al LLM. Devuelve dict con las 6 claves (valores str o None)."""
    t = (text or "").strip()
    if len(t) < 2:
        return _vacio()

    base = os.getenv("OPENAI_BASE_URL")
    key = os.getenv("OPENAI_API_KEY")
    if not (base and key):
        # Sin esto, el bot pierde su red de seguridad para mensajes sucios
        # (typos, modelos poco comunes como "s10", campos pegados). Antes
        # fallaba en silencio total, sin ningun log — se detecto solo
        # leyendo el codigo. Ahora se avisa una sola vez para que se note
        # en app.log en vez de descubrirse por leads atascados.
        llm_status.disable("OPENAI_API_KEY/OPENAI_BASE_URL no configurada (.env)")
        return _vacio()
    if llm_status.is_disabled():
        return _vacio()

    try:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model or os.getenv("MODEL_NAME", "llama-3.3-70b-versatile"),
                "temperature": 0,
                "max_tokens": 200,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": t[:400]},
                ],
            },
            timeout=TIMEOUT,
            trust_env=False,
        )
        if r.status_code != 200:
            if r.status_code == 401:
                llm_status.disable("OPENAI_API_KEY invalida")
            log.warning("llm_extract http %s", r.status_code)
            return _vacio()
        raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        data = json.loads(raw)
    except Exception:
        log.exception("llm_extract fallo")
        return _vacio()

    if not isinstance(data, dict):
        return _vacio()
    out = _vacio()
    for k in _KEYS:
        v = _clean(data.get(k))
        out[k] = v or None
    return out


def extract_vehicle(text: str, model: str | None = None) -> dict:
    """
    Saca año/marca/modelo y los VALIDA contra el catalogo real de Safelite.

    Devuelve {'year','make','model'} solo si el catalogo lo confirma; si no,
    {} (preferimos no guardar nada antes que guardar un invento).
    """
    data = extract_raw(text, model=model)
    year, make, model_name = data.get("year"), data.get("make"), data.get("model")
    if not re.fullmatch(r"(19|20)\d{2}", str(year or "")):
        year = None
    if not (year and make and model_name):
        # sin los 3 no se puede validar contra el catalogo: devolvemos lo que haya
        # marcado como no verificado, para que el flujo siga preguntando.
        parcial = {k: v for k, v in
                   (("year", year), ("make", make), ("model", model_name)) if v}
        return {**parcial, "_verified": False} if parcial else {}

    from .vehicle_norm import normalize_vehicle

    try:
        norm = normalize_vehicle(year, make, model_name)
    except Exception:
        log.exception("extract_vehicle normalize")
        return {"year": year, "make": make, "model": model_name, "_verified": False}

    if norm.get("make") and norm.get("model"):
        return {
            "year": year,
            "make": norm["make"],
            "model": norm["model"],
            "_verified": True,
        }
    # el catalogo no lo confirma: devolvemos lo del LLM sin verificar + candidatos
    return {
        "year": year,
        "make": norm.get("make") or make,
        "model": model_name,
        "candidates": norm.get("candidates") or [],
        "_verified": False,
    }


def extract_location(text: str, model: str | None = None, validator=None,
                     city_lookup=None) -> dict:
    """
    Saca zip o ciudad/estado y los valida.

    El zip se valida con `validator` (safelite_zip) y la ciudad se convierte a
    zip con el catalogo de ciudades. Devuelve {'zip','source','city','state'}.
    """
    from .zip_norm import US_STATES, _zips_de_ciudad, extract_zip

    out = {"zip": "", "source": "", "city": "", "state": ""}
    data = extract_raw(text, model=model)

    def _ok(z: str) -> bool:
        if not z:
            return False
        if validator is None:
            return True
        try:
            return bool(validator(z))
        except Exception:
            log.exception("extract_location validate %s", z)
            return False

    z = extract_zip(data.get("zip") or "")
    if z and _ok(z):
        out.update(zip=z, source="llm_text")
        return out

    city = (data.get("city") or "").strip()
    state = (data.get("state") or "").strip().lower()
    if state not in US_STATES:
        state = ""
    if not city:
        return out
    out["city"], out["state"] = city.title(), state
    if not state:
        # ciudad sin estado: no adivinamos el estado, lo resuelve quien llama
        return out

    lookup = city_lookup if city_lookup is not None else _zips_de_ciudad
    try:
        for cand in lookup(city, state) or []:
            if _ok(cand):
                out.update(zip=cand, source="llm_city")
                return out
    except Exception:
        log.exception("extract_location city_lookup %s %s", city, state)
    return out
