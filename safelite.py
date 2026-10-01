"""
Cliente Safelite (digitalapi.safelite.io) — solo API de safelite.com.

Mismos endpoints que usa www.safelite.com/fmg/schedule:

  GET  /location/api/v1/location/zip/{zip}
  GET  /vehicle/api/v1/vehicle/Styles/{year}/{make}/{model}
  GET  /vehicle/api/v1/vehicle/lookup/{year}/{make}/{model}/{style}
  POST /parts/api/v1/parts/parts-or-questions
  POST /parts/api/v1/parts/parts
  POST /schedule/api/v1/schedule/mobile-time-slots   (domicilio)
  POST /schedule/api/v1/schedule/shop-time-slots     (taller)
  POST /order/api/v1/order/save-session/fmg
  POST /order/api/v1/order/load-session
  POST /order/api/v1/order/submit-order

Uso desde bot:
  from safelite import safelite_zip, safelite_vehicle, safelite_parts
  from safelite import safelite_slots, safelite_shop_slots, safelite_book, flatten_slots

CLI de prueba:
  python safelite.py zip 29349
  python safelite.py vehicle 2006 Honda CR-V
  python safelite.py slots 2006 Honda CR-V 29349
  python safelite.py shop 2006 Honda CR-V 29349
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("safelite")

# ---------------------------------------------------------------------------
# Constantes (mismo flujo cash / FixMyGlass que la web)
# ---------------------------------------------------------------------------
SAFELITE = "https://digitalapi.safelite.io"
CASH_PARENT = "167132"
CASH_BILL = "87291"
APP_NAME = "FixMyGlassNextGen"
PORTAL_BASE = "https://myaccount.safelite.com/portal/index.html"
ORIGIN = "https://www.safelite.com"
REFERER = "https://www.safelite.com/fmg/schedule"

_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_DIAS = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
]


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def _sx(timeout: float = 45) -> httpx.Client:
    """Cliente HTTP con headers iguales a la web safelite.com."""
    return httpx.Client(
        timeout=timeout,
        trust_env=False,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Origin": ORIGIN,
            "Referer": REFERER,
            "Content-Type": "application/json",
            "X-Application-Name": APP_NAME,
        },
    )


def portal_url(work_order_id: str, token: str) -> str:
    if not work_order_id or not token:
        return ""
    return f"{PORTAL_BASE}?woid={work_order_id}&token={token}"


# ---------------------------------------------------------------------------
# ZIP / ubicación
# ---------------------------------------------------------------------------
def safelite_zip(zipcode: str) -> dict | None:
    """GET location/zip — ciudad, state, provider, isServiceable.

    Esta API es auxiliar: si Safelite responde lento o devuelve JSON roto, el
    bot no debe caer ni repetirle al cliente que mande el mismo ZIP.
    """
    z = re.sub(r"\D", "", str(zipcode or ""))[:5]
    if len(z) != 5:
        return None
    url = f"{SAFELITE}/location/api/v1/location/zip/{z}"
    for attempt in range(2):
        try:
            with _sx(timeout=12) as c:
                r = c.get(url)
            if r.status_code != 200:
                log.warning("safelite zip status=%s zip=%s", r.status_code, z)
                return None
            return r.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                log.warning("safelite zip retry zip=%s err=%s", z, exc)
                time.sleep(0.25)
                continue
            log.warning("safelite zip unavailable zip=%s err=%s", z, exc)
            return None
        except Exception as exc:
            log.warning("safelite zip unexpected zip=%s err=%s", z, exc)
            return None
    return None


# ---------------------------------------------------------------------------
# Vehículo
# ---------------------------------------------------------------------------
def _model_alts(model: str) -> list[str]:
    m = (model or "").strip()
    alts = [m, m.upper(), m.title()]
    if re.search(r"cr[\s\-]?v", m, re.I):
        alts += ["CR-V", "CRV", "Cr-v"]
    if re.search(r"f[\s\-]?150", m, re.I):
        alts += ["F Series F150", "F-150", "F150"]
    if re.search(r"f[\s\-]?250", m, re.I):
        alts += ["F Series F250", "F-250", "F250"]
    if re.search(r"cx[\s\-]?5", m, re.I):
        alts += ["CX-5", "CX5"]
    if re.search(r"cx[\s\-]?9", m, re.I):
        alts += ["CX-9", "CX9"]
    if re.search(r"gladiador", m, re.I):
        alts += ["Gladiator"]
    if re.search(r"e[\s\-]?350", m, re.I):
        alts += ["E350", "E-Class"]
    return list(dict.fromkeys(a for a in alts if a))


def _make_alts(make: str) -> list[str]:
    m = (make or "").strip()
    alts = [m]
    low = m.lower().replace("-", " ")
    if "mercedes" in low:
        alts += ["Mercedes Benz", "Mercedes-Benz", "Mercedes"]
    return list(dict.fromkeys(a for a in alts if a))


def _style_name(s: Any) -> str:
    return s if isinstance(s, str) else str((s or {}).get("style") or (s or {}).get("name") or s or "")


def _pick_style(styles: list, hint: str = "") -> str:
    """Elige el estilo de Safelite. Si dijo wagon/sedan/crew, usa ESO."""
    names = [_style_name(s) for s in styles if _style_name(s)]
    if not names:
        return ""
    h = (hint or "").lower().strip()
    synonyms = {
        "wagon": ("station wagon", "wagon", "estate"),
        "station wagon": ("station wagon", "wagon"),
        "sedan": ("sedan",),
        "coupe": ("coupe",),
        "convertible": ("convertible",),
        "crew cab": ("crew cab",),
        "pickup": ("crew cab", "pickup", "cab"),
        "hatch": ("hatchback", "hatch"),
    }
    keys = synonyms.get(h, (h,) if h else ())
    for key in keys:
        for n in names:
            if key in n.lower():
                return n
    ranked = sorted(
        names,
        key=lambda n: (
            0 if "convertible" not in n.lower() else 1,
            0 if "4 door" in n.lower() else 1,
            0 if any(x in n.lower() for x in ("crew cab", "utility", "station wagon")) else 1,
            0 if "sedan" not in n.lower() else 1,
            n.lower(),
        ),
    )
    return ranked[0]


def safelite_vehicle(
    year: str | int,
    make: str,
    model: str,
    style_hint: str = "",
) -> dict | None:
    """Resuelve carId + style exacto (wagon si dijo wagon, no sedan por default)."""
    y, mk, md = str(year).strip(), (make or "").strip(), (model or "").strip()
    if not (y and mk and md):
        return None
    with _sx() as c:
        styles: list = []
        used_mk, used_md = mk, md
        for mk_alt in _make_alts(mk):
            for alt in _model_alts(md):
                r = c.get(
                    f"{SAFELITE}/vehicle/api/v1/vehicle/Styles/{y}/{quote(mk_alt)}/{quote(alt)}"
                )
                if r.status_code == 200 and r.json():
                    used_mk, used_md = mk_alt, alt
                    styles = r.json() or []
                    break
            if styles:
                break
        if not styles:
            return None
        style = _pick_style(styles, style_hint)
        path = "/".join(quote(p, safe="") for p in [y, used_mk, used_md, str(style)])
        r2 = c.get(f"{SAFELITE}/vehicle/api/v1/vehicle/lookup/{path}")
        if r2.status_code != 200:
            return None
        data = r2.json()
        data["_style"] = style
        data["_model"] = used_md
        data["_make"] = used_mk
        log.info("safelite vehicle %s %s %s -> %s / %s", y, used_mk, used_md, style, data.get("carId"))
        return data


# ---------------------------------------------------------------------------
# Parts (parabrisas / ADAS questions)
# ---------------------------------------------------------------------------
def _walk_part_questions(part_questions: list) -> list:
    """Responde el arbol de preguntas (prefiere No) y devuelve answerResults."""
    if not part_questions:
        return []
    by_seq = {q.get("questionSequence"): q for q in part_questions}
    seq = min(by_seq.keys()) if by_seq else None
    answers: list = []
    seen: set = set()
    while seq is not None and seq not in seen and seq in by_seq:
        seen.add(seq)
        q = by_seq[seq]
        opts = q.get("answers") or []
        pick = None
        for a in opts:
            if (a.get("answerText") or "").strip().lower() in ("no", "n"):
                pick = a
                break
        if not pick:
            for a in opts:
                if a.get("answerResult"):
                    pick = a
                    break
        if not pick and opts:
            pick = opts[0]
        if not pick:
            break
        answers.append(
            {
                "questionSequence": q.get("questionSequence"),
                "answerText": pick.get("answerText"),
                "answerResult": pick.get("answerResult") or "",
                "problemQuestionId": pick.get("problemQuestionId"),
            }
        )
        if pick.get("answerResult"):
            break
        seq = pick.get("nextQuestionSequence")
    return answers


def _normalize_glass_parts(data: dict) -> list:
    glass_parts = []
    for item in data.get("partsOrQuestions") or data.get("glassPieceParts") or []:
        gp = item.get("glassPiece") or {"location": "Windshield", "name": "Single"}
        parts_list = item.get("parts") or []
        if not parts_list and item.get("partNumber"):
            parts_list = [item]
        for p in parts_list:
            if not p:
                continue
            glass_parts.append(
                {
                    "partNumber": p.get("safelitePartNumber")
                    or p.get("basePartNumber")
                    or p.get("partNumber"),
                    "basePartNumber": p.get("basePartNumber") or p.get("partNumber"),
                    "partType": p.get("partType") or "GLASS",
                    "location": gp.get("location") or "Windshield",
                    "name": gp.get("name") or "Single",
                    "color": p.get("color"),
                    "requiresRecalibration": bool(p.get("requiresRecalibration", False)),
                    "isChildPart": False,
                    "childParts": [
                        {
                            "partNumber": ch.get("safelitePartNumber") or ch.get("partNumber"),
                            "basePartNumber": ch.get("partNumber"),
                            "partType": ch.get("partType"),
                            "isChildPart": True,
                        }
                        for ch in (p.get("childParts") or [])
                    ],
                }
            )
    return [g for g in glass_parts if g.get("partNumber")]


def safelite_parts(car_id: str, zipcode: str) -> list:
    """Obtiene piezas; si hay preguntas ADAS, responde No y pide /parts."""
    with _sx() as c:
        base = {
            "carId": car_id,
            "glassPieces": [{"location": "Windshield", "name": "Single"}],
            "zip": zipcode,
            "vin": "",
            "referralSeqNumber": "",
            "parentAccountNumber": CASH_PARENT,
        }
        data = None
        for st in ("Install", "Replace"):
            payload = {**base, "serviceType": st}
            r = c.post(f"{SAFELITE}/parts/api/v1/parts/parts-or-questions", json=payload)
            if r.status_code == 200:
                data = r.json()
                break
        if not data:
            log.error("parts-or-questions fail car=%s zip=%s", car_id, zipcode)
            return []

        glass_parts = _normalize_glass_parts(data)
        if glass_parts:
            return glass_parts

        for item in data.get("partsOrQuestions") or []:
            qs = item.get("partQuestions") or []
            if not qs:
                continue
            answers = _walk_part_questions(qs)
            if not answers:
                continue
            part_num = ""
            for a in reversed(answers):
                if a.get("answerResult"):
                    part_num = a["answerResult"]
                    break
            for st in ("Install", "Replace"):
                payload = {
                    **base,
                    "serviceType": st,
                    "answerResults": answers,
                    "glassPieces": [{"location": "Windshield", "name": "Single"}],
                }
                r = c.post(f"{SAFELITE}/parts/api/v1/parts/parts", json=payload)
                if r.status_code == 200:
                    glass_parts = _normalize_glass_parts(r.json())
                    if glass_parts:
                        return glass_parts
                    gpp = r.json().get("glassPieceParts") or r.json().get("parts") or []
                    if gpp:
                        return _normalize_glass_parts({"partsOrQuestions": gpp})
                if part_num:
                    return [
                        {
                            "partNumber": part_num,
                            "basePartNumber": part_num,
                            "partType": "GLASS",
                            "location": "Windshield",
                            "name": "Single",
                            "color": None,
                            "requiresRecalibration": False,
                            "isChildPart": False,
                            "childParts": [],
                        }
                    ]
        log.error("parts unresolved for %s", car_id)
        return []


def _unique_glass(glass_parts: list) -> list:
    """Una pieza de parabrisas por número. No mandar 13 alternativas juntas (Gladiator=0 slots)."""
    out, seen = [], set()
    for p in glass_parts or []:
        pn = (p.get("partNumber") or "").strip()
        if not pn or pn in seen:
            continue
        seen.add(pn)
        out.append(p)
    return out


def _line_items(glass_parts: list) -> list:
    out = []
    for gp in glass_parts:
        out.append(
            {
                "partNumber": gp["partNumber"],
                "basePartNumber": gp.get("basePartNumber"),
                "location": gp.get("location"),
                "name": gp.get("name"),
                "color": gp.get("color"),
                "partType": gp.get("partType"),
                "requiresRecalibration": gp.get("requiresRecalibration"),
            }
        )
        for ch in gp.get("childParts") or []:
            out.append(
                {
                    "partNumber": ch["partNumber"],
                    "basePartNumber": ch.get("basePartNumber"),
                    "location": gp.get("location"),
                    "name": gp.get("name"),
                    "partType": ch.get("partType"),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Helpers de formato de slots
# ---------------------------------------------------------------------------
def _fmt_day(iso_date: str) -> str:
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]}"
    except Exception:
        return iso_date


def _fmt_hour(hhmm: str) -> str:
    try:
        h, m = hhmm.split(":")[:2]
        h = int(h)
        ap = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        return f"{h12}:{m}{ap}" if m != "00" else f"{h12}{ap}"
    except Exception:
        return hhmm


def _slot_period(start: str) -> str:
    try:
        h = int(str(start).split(":")[0])
    except Exception:
        return "tarde"
    if h < 12:
        return "manana"
    if h < 17:
        return "tarde"
    return "noche"


def _clean_days_payload(days_list: list) -> list[dict]:
    """Normaliza respuesta days/timeSlots del API."""
    clean = []
    for d in days_list or []:
        slots = d.get("timeSlots") or d.get("TimeSlots") or []
        if not slots:
            continue
        clean.append(
            {
                "date": d.get("date") or d.get("Date"),
                "timeSlots": [
                    {
                        "id": s.get("id") or s.get("routeCode") or s.get("Id"),
                        "startTime": s.get("startTime") or s.get("StartTime"),
                        "endTime": s.get("endTime") or s.get("EndTime"),
                    }
                    for s in slots
                    if (s.get("id") or s.get("routeCode") or s.get("Id"))
                    and (s.get("startTime") or s.get("StartTime"))
                ],
            }
        )
    return clean


def flatten_slots(days: list, limit: int = 40) -> list[dict]:
    """Aplana huecos del API a lista plana [{date, id, startTime, endTime, ...}]."""
    out = []
    for d in days or []:
        iso = d.get("date") or ""
        for s in d.get("timeSlots") or []:
            st, et = s.get("startTime") or "", s.get("endTime") or ""
            sid = s.get("id") or ""
            if not iso or not sid or not st:
                continue
            out.append(
                {
                    "date": iso,
                    "id": sid,
                    "startTime": st,
                    "endTime": et,
                    "period": _slot_period(st),
                    "label": f"{_fmt_day(iso)}, de {_fmt_hour(st)} a {_fmt_hour(et)}",
                    "source": "safelite_live",
                }
            )
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Disponibilidad DOMICILIO (mobile-time-slots)
# ---------------------------------------------------------------------------
def safelite_slots(
    car_id: str,
    zipcode: str,
    vehicle: dict,
    glass_parts: list,
    days: int = 21,
) -> list:
    """
    Disponibilidad REAL a domicilio (mobile-time-slots).
    Misma API que www.safelite.com al elegir dia/hora mobile.
    Devuelve: [{date, timeSlots:[{id, startTime, endTime}]}]
    """
    start = (date.today() + timedelta(days=1)).isoformat()
    end = (date.today() + timedelta(days=max(7, days))).isoformat()
    body = {
        "startDate": start,
        "endDate": end,
        "applicationName": APP_NAME,
        "parentAccountNumber": CASH_PARENT,
        "carId": car_id,
        "lineItems": _line_items(_unique_glass(glass_parts)[:1] or glass_parts),
        "glassPieces": [{"location": "Windshield", "name": "Single"}],
        "eon": "0",
        "billToAccountNumber": CASH_BILL,
        "coverage": {"status": "Unknown", "deductible": 0.0, "additionalAuthFlag": "N"},
        "partSelection": {
            "hasAnsweredPartQuestions": False,
            "hasAnsweredMoldingQuestions": False,
            "hasAnsweredCapabilityQuestions": False,
            "hasManuallySelectedParts": False,
        },
        "vehicle": {
            "year": vehicle.get("year"),
            "make": vehicle.get("make"),
            "model": vehicle.get("_model") or vehicle.get("model"),
            "style": vehicle.get("_style") or vehicle.get("style"),
            "vin": "",
        },
        "zipCode": zipcode,
    }
    with _sx() as c:
        r = c.post(f"{SAFELITE}/schedule/api/v1/schedule/mobile-time-slots", json=body)
        if r.status_code != 200:
            log.error("slots API %s %s", r.status_code, r.text[:300])
            return []
        data = r.json() or {}
        days_list = data.get("days") or data.get("Days") or []
        clean = _clean_days_payload(days_list)
        total = sum(len(d["timeSlots"]) for d in clean)
        log.info(
            "SAFELITE LIVE mobile zip=%s car=%s days=%s windows=%s range=%s..%s",
            zipcode,
            car_id,
            len(clean),
            total,
            start,
            end,
        )
        return clean


# ---------------------------------------------------------------------------
# Disponibilidad TALLER (shop-time-slots / InShop)
# ---------------------------------------------------------------------------
def safelite_shop_slots(
    lead: dict | None = None,
    veh: dict | None = None,
    days: int = 14,
    *,
    zipcode: str | None = None,
    year: str | int | None = None,
    make: str | None = None,
    model: str | None = None,
) -> list[dict]:
    """
    Ventanas LIVE de TALLER (InShop). Lista aplanada (flatten_slots).

    Podes pasar lead={year,make,model,zip,address} o zipcode+year+make+model.
    """
    lead = lead or {}
    zipc = (
        (zipcode or "").strip()
        or re.sub(r"\D", "", str(lead.get("zip") or ""))[:5]
        or (re.search(r"\b(\d{5})\b", lead.get("address") or "") or [None, ""])[1]
    )
    if not zipc or len(str(zipc)) != 5:
        return []
    zipc = str(zipc)[:5]

    y = year if year is not None else lead.get("year")
    mk = (make if make is not None else lead.get("make") or "") or ""
    md = (model if model is not None else lead.get("model") or "") or ""

    try:
        zinfo = safelite_zip(zipc) or {}
        providers = [
            p for p in (
                str(zinfo.get("zipCodeCtu") or "").strip(),
                str(zinfo.get("providerNumber") or "").strip(),
            ) if p
        ]
        providers = list(dict.fromkeys(providers))
        if not providers:
            return []
        hint = (lead.get("style") or "") if lead else ""
        if not veh:
            veh = safelite_vehicle(y, mk, md, style_hint=hint)
        if not veh:
            return []
        parts = _unique_glass(safelite_parts(veh["carId"], zipc))
        if not parts:
            return []
        start = (date.today() + timedelta(days=1)).isoformat()
        end = (date.today() + timedelta(days=max(7, days))).isoformat()
        vbody = {
            "year": veh.get("year"),
            "make": veh.get("_make") or veh.get("make"),
            "model": veh.get("_model") or veh.get("model"),
            "style": veh.get("_style") or veh.get("style"),
            "vin": "",
        }
        for gp in parts[:4]:
            for provider in providers:
                body = {
                    "startDate": start,
                    "endDate": end,
                    "applicationName": APP_NAME,
                    "parentAccountNumber": CASH_PARENT,
                    "billToAccountNumber": CASH_BILL,
                    "carId": veh["carId"],
                    "ProviderNumber": provider,
                    "providerNumber": provider,
                    "ShopAppointmentType": "InShop",
                    "shopAppointmentType": "InShop",
                    "lineItems": _line_items([gp]),
                    "glassPieces": [{"location": "Windshield", "name": "Single"}],
                    "eon": "0",
                    "coverage": {"status": "Unknown", "deductible": 0.0, "additionalAuthFlag": "N"},
                    "partSelection": {
                        "hasAnsweredPartQuestions": False,
                        "hasAnsweredMoldingQuestions": False,
                        "hasAnsweredCapabilityQuestions": False,
                        "hasManuallySelectedParts": False,
                    },
                    "vehicle": vbody,
                    "zipCode": zipc,
                }
                with _sx() as c:
                    r = c.post(f"{SAFELITE}/schedule/api/v1/schedule/shop-time-slots", json=body)
                    if r.status_code != 200:
                        log.error("shop slots API %s %s", r.status_code, (r.text or "")[:200])
                        continue
                    days_list = (r.json() or {}).get("days") or []
                flat = flatten_slots(_clean_days_payload(days_list), limit=40)
                if flat:
                    log.info(
                        "SAFELITE LIVE shop zip=%s car=%s style=%s part=%s prov=%s windows=%s",
                        zipc, veh.get("carId"), veh.get("_style"), gp.get("partNumber"), provider, len(flat),
                    )
                    return flat
        log.info("SAFELITE LIVE shop empty zip=%s car=%s style=%s", zipc, veh.get("carId"), veh.get("_style"))
        return []
    except Exception:
        log.exception("safelite_shop_slots")
        return []


# ---------------------------------------------------------------------------
# Pipeline completo: zip → vehicle → parts → mobile slots (un solo call)
# ---------------------------------------------------------------------------
def fetch_availability(
    year: str | int,
    make: str,
    model: str,
    zipcode: str,
    days: int = 21,
) -> dict:
    """
    Flujo completo de disponibilidad a domicilio (lo que hace la web).

    return: {
      ok, zip_info, vehicle, parts, days (raw), options (flatten), error
    }
    """
    z = re.sub(r"\D", "", str(zipcode))[:5]
    out: dict[str, Any] = {
        "ok": False,
        "zip_info": None,
        "vehicle": None,
        "parts": [],
        "days": [],
        "options": [],
        "error": "",
    }
    zinfo = safelite_zip(z)
    out["zip_info"] = zinfo
    if zinfo is None:
        out["error"] = "zip_api_error"
        return out
    if zinfo.get("isServiceable") is False:
        out["error"] = "zip_no_servicio"
        return out

    veh = safelite_vehicle(year, make, model)
    out["vehicle"] = veh
    if not veh:
        out["error"] = "vehicle_fail"
        return out

    parts = safelite_parts(veh["carId"], z)
    out["parts"] = parts
    if not parts:
        out["error"] = "parts_fail"
        return out

    days_raw = safelite_slots(veh["carId"], z, veh, parts, days=days)
    out["days"] = days_raw
    options = flatten_slots(days_raw, limit=40)
    out["options"] = options
    if not options:
        out["error"] = "sin_ventanas"
        return out
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# Booking: save-session/fmg → load-session → submit-order  (= Continue en web)
# ---------------------------------------------------------------------------
def safelite_book(
    lead: dict,
    vehicle: dict,
    glass_parts: list,
    slot: dict,
    slot_date: str,
) -> dict:
    """
    Agenda real en Safelite.

    lead necesita: zip, address, city?, state?, name?,
                   pool_phone o phone, pool_email o email
    slot: {id, startTime, endTime}
    """
    zipcode = lead["zip"]
    zip_info = safelite_zip(zipcode) or {}
    name = (lead.get("name") or "Customer Service").strip()
    parts = name.split(None, 1)
    first, last = parts[0], (parts[1] if len(parts) > 1 else "Customer")
    # pool para la web; no el numero real del cliente si se puede
    phone = re.sub(r"\D", "", lead.get("pool_phone") or lead.get("phone") or "")
    email = (lead.get("pool_email") or lead.get("email") or f"citas.{phone or 'user'}@gmail.com").strip()
    address = lead.get("address") or ""
    city = lead.get("city") or zip_info.get("city") or ""
    state = lead.get("state") or zip_info.get("state") or ""

    base_order = {
        "vehicle": {
            "carId": vehicle["carId"],
            "year": int(vehicle["year"]),
            "make": vehicle["make"],
            "model": vehicle.get("_model") or vehicle["model"],
            "style": vehicle.get("_style") or vehicle.get("style"),
            "vin": None,
            "vehicleSubType": vehicle.get("vehicleSubType"),
            "vehicleSpecialClass": None,
            "isBigTruck": bool(vehicle.get("isBigTruck")),
            "canSafeliteService": True,
            "vinRequired": False,
            "registration": {
                "firstName": first,
                "lastName": last,
                "streetAddress": address,
                "city": city,
                "state": state,
                "zipCode": zipcode,
                "licensePlateNumber": None,
            },
        },
        "customer": {
            "emailAddress": email,
            "firstName": first,
            "lastName": last,
            "isSmsOptIn": True,
            "phoneNumber": phone,
            "address": {
                "streetAddress": address,
                "streetAddress2": None,
                "city": city,
                "state": state,
                "zipCode": zipcode,
            },
        },
        "damage": {
            "numberOfChips": 0,
            "glassToReplace": [{"location": "Windshield", "name": "Single"}],
            "isRepair": False,
            "partQuestionAnswers": None,
            "moldingQuestionAnswers": None,
            "capabilityQuestionAnswers": None,
            "installOemGlass": False,
            "dateOfLoss": None,
            "damageCause": None,
        },
        "lineItems": {
            "glassParts": glass_parts,
            "supportingItems": [],
            "vaps": [],
            "serverData": "",
            "promos": [],
        },
        "payment": {
            "InsuranceCoverage": {"isVerified": False, "claimNumber": None},
            "isInsurance": False,
            "parentAccountNumber": CASH_PARENT,
            "billToAccountNumber": CASH_BILL,
            "isClaimAndCoverage": False,
            "canScheduleOnline": True,
            "isManagedAccount": False,
            "inactivePromos": None,
            "isCreditCard": False,
            "isPaypal": False,
            "isAfterPay": False,
            "isApplePay": False,
            "paypalToken": None,
            "nextGenSettledAmount": 0,
            "ccToken": {
                "subscriptionId": None,
                "expMonth": None,
                "expYear": None,
                "cardType": None,
                "billToPostalCode": None,
                "billToFirstName": None,
                "billToLastName": None,
                "referenceNumber": None,
                "authCode": None,
                "transactionId": None,
                "transReferenceNumber": None,
                "lastFour": None,
            },
        },
        "policy": {
            "isItac": False,
            "isNoComp": False,
            "currentDeductible": 0,
            "originalDeductible": 0,
            "policyNumber": None,
        },
        "serviceLocation": {
            "streetAddress": address,
            "streetAddress2": None,
            "city": city,
            "state": state,
            "zipCode": zipcode,
            "zipCodeCtu": zip_info.get("zipCodeCtu") or "",
            "appointmentType": "Mobile",
            "isVehicleProtected": "true",
            "provider": {
                "providerNumber": zip_info.get("providerNumber") or "01501",
                "address": {
                    "streetAddress": None,
                    "city": city,
                    "state": state,
                    "zipCode": zipcode,
                    "zipCodeCtu": zip_info.get("zipCodeCtu") or "",
                },
            },
            "techNotes": (
                f"Windshield {vehicle.get('year')} {vehicle.get('make')} "
                f"{vehicle.get('model')}. Phone {phone}"
            ),
        },
        "schedule": {
            "date": slot_date,
            "startTime": slot["startTime"],
            "endTime": slot["endTime"],
            "routeCode": slot["id"],
            "jobMaxMinutes": "120",
            "jobMinMinutes": "60",
        },
        "referralCorrelationId": None,
        "referralDate": None,
        "referralNumber": None,
        "referralSequenceNumber": None,
        "eon": None,
        "createUnscheduledStatusWorkOrderForPIA": False,
        "lockToken": None,
        "isRecalAckOptIn": False,
        "isMSRFeeApplicable": False,
        "isMSRFeeCoveredByInsurance": False,
        "cashPriceSubTotal": "0",
    }

    save_payload = {
        "submitAfterSave": False,
        "userAgent": "Mozilla/5.0",
        "applicationUser": {
            "crmCustomerId": None,
            "experiments": [],
            "lastPage": "confirmation",
            "pageData": {},
            "savedSessionId": None,
            "affiliateCookies": None,
        },
        "order": base_order,
    }

    with _sx() as c:
        r = c.post(f"{SAFELITE}/order/api/v1/order/save-session/fmg", json=save_payload)
        if r.status_code != 200:
            raise RuntimeError(f"save-session {r.status_code}: {r.text[:300]}")
        sess = r.json()

        ref_date = (sess.get("referralDate") or "")[:10]
        r2 = c.post(
            f"{SAFELITE}/order/api/v1/order/load-session",
            json={
                "savedSessionId": sess["savedSessionId"],
                "referralNumber": str(sess["referralNumber"]),
                "referralDate": ref_date,
                "parentAccountNumber": str(sess.get("parentAccountNumber") or CASH_PARENT),
                "referralCorrelationId": sess.get("referralCorrelationId"),
            },
        )
        if r2.status_code != 200:
            raise RuntimeError(f"load-session {r2.status_code}: {r2.text[:300]}")
        loaded = r2.json()
        order = loaded.get("order") or base_order

        order["schedule"] = {
            "date": slot_date,
            "startTime": slot["startTime"],
            "endTime": slot["endTime"],
            "routeCode": slot["id"],
            "jobMaxMinutes": "120",
            "jobMinMinutes": "60",
        }
        order.setdefault("customer", {})
        order["customer"]["phoneNumber"] = phone
        order["customer"]["firstName"] = first
        order["customer"]["lastName"] = last
        order["customer"]["isSmsOptIn"] = True
        order["customer"].setdefault("address", {})
        order["customer"]["address"].update(
            {
                "streetAddress": address,
                "city": city,
                "state": state,
                "zipCode": zipcode,
            }
        )
        order.setdefault("serviceLocation", {})
        order["serviceLocation"].update(
            {
                "streetAddress": address,
                "city": city,
                "state": state,
                "zipCode": zipcode,
                "appointmentType": "Mobile",
            }
        )
        if isinstance(order["serviceLocation"].get("isVehicleProtected"), bool):
            order["serviceLocation"]["isVehicleProtected"] = (
                "true" if order["serviceLocation"]["isVehicleProtected"] else "false"
            )

        submit = {
            "submitAfterSave": True,
            "userAgent": "Mozilla/5.0",
            "applicationUser": {
                "crmCustomerId": str(sess.get("crmCustomerId") or ""),
                "experiments": [],
                "lastPage": "confirmation",
                "pageData": (loaded.get("applicationUser") or {}).get("pageData") or {},
                "savedSessionId": str(sess.get("savedSessionId") or ""),
                "affiliateCookies": None,
            },
            "order": order,
        }
        r3 = c.post(f"{SAFELITE}/order/api/v1/order/submit-order", json=submit)
        if r3.status_code != 200:
            raise RuntimeError(f"submit {r3.status_code}: {r3.text[:400]}")
        result = r3.json()

    woid = result.get("workOrderId") or ""
    token = result.get("customerPortalLoginToken") or ""

    return {
        "work_order": result.get("workOrderNumber") or "",
        "work_order_id": woid,
        "status": result.get("workOrderStatus") or "",
        "eon": result.get("eon") or "",
        "referral": result.get("referralNumber") or "",
        "portal": portal_url(woid, token),
        "slot_date": slot_date,
        "slot_start": slot["startTime"],
        "slot_end": slot["endTime"],
        "raw": result,
    }


# ---------------------------------------------------------------------------
# CLI de prueba
# ---------------------------------------------------------------------------
def _cli() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = args[0].lower()

    if cmd == "zip" and len(args) >= 2:
        data = safelite_zip(args[1])
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0 if data else 1

    if cmd == "vehicle" and len(args) >= 4:
        data = safelite_vehicle(args[1], args[2], " ".join(args[3:]))
        if not data:
            print("vehicle fail")
            return 1
        print(
            json.dumps(
                {
                    "carId": data.get("carId"),
                    "year": data.get("year"),
                    "make": data.get("make"),
                    "model": data.get("_model") or data.get("model"),
                    "style": data.get("_style") or data.get("style"),
                },
                indent=2,
            )
        )
        return 0

    if cmd in ("slots", "mobile", "shop") and len(args) >= 5:
        year, make = args[1], args[2]
        # ultimo arg = zip; el resto = model
        zipc = args[-1]
        model = " ".join(args[3:-1])
        if cmd == "shop":
            opts = safelite_shop_slots(
                zipcode=zipc, year=year, make=make, model=model, days=14
            )
            print(f"shop options: {len(opts)}")
            for o in opts[:15]:
                print(f"  {o.get('label')}  id={o.get('id')}")
            return 0 if opts else 1
        res = fetch_availability(year, make, model, zipc)
        print(
            json.dumps(
                {
                    "ok": res["ok"],
                    "error": res["error"],
                    "city": (res.get("zip_info") or {}).get("city"),
                    "state": (res.get("zip_info") or {}).get("state"),
                    "carId": (res.get("vehicle") or {}).get("carId"),
                    "parts": [p.get("partNumber") for p in res.get("parts") or []],
                    "n_options": len(res.get("options") or []),
                    "options": [
                        {"date": o["date"], "start": o["startTime"], "end": o["endTime"], "id": o["id"]}
                        for o in (res.get("options") or [])[:12]
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if res["ok"] else 1

    print("Uso: python safelite.py zip|vehicle|slots|shop ...")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
