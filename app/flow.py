from __future__ import annotations

import json
import logging
import re
from datetime import datetime

log = logging.getLogger("agente.flow")

from . import legacy
from . import llm
from . import rules
from .db import append_history, get_lead, note, upsert_lead
from .nlu import (
    analyze_client_message,
    client_accepts_taller_manana,
    detect_service_pref,
    interpret_slot_request,
    parse_when_intent,
)
from .safelite_client import safelite_shop_slots, safelite_zip

from .vehicle_norm import normalize_vehicle
from . import interpret
from .zip_norm import extract_zip as zip_extract, resolve_zip
from .chat_typos import fix_chat_typos

FORCE_SLOTS_COMMAND = "!hora"
_CMD_RE = re.compile(r"^!?(hora|horas|horario|horarios|slots)$", re.I)
_CMD_HEAD = re.compile(r"(?is)^\s*!?(hora|horas|horario|horarios|slots)\b")
_CMD_ANY = re.compile(r"(?i)!hora\b")
_LABEL_NEXT = (
    r"a[nñ]o|year|marca|make|brand|modelo|model|"
    r"zip(?:\s*code)?|c[oó]digo\s*postal|\bcp\b"
)


def _strip_hora_cmd(text: str) -> str:
    t = text or ""
    t = re.sub(r"(?is)^\s*!?(hora|horas|horario|horarios|slots)\b[:\s,\-]*", "", t, count=1)
    return re.sub(r"(?i)!hora\b", " ", t).strip()


def _clean_make(val: str) -> str:
    val = re.sub(r"\s*\([^)]*\)\s*", " ", val or "").strip()
    val = re.sub(r"(?i)mercedes[-\s]?benz", "Mercedes Benz", val)
    return re.sub(r"\s+", " ", val).strip(" ,;.-")


def parse_hora_block(text: str) -> dict[str, str]:
    """Año, marca, modelo y ZIP en CUALQUIER orden, con o sin etiquetas."""
    body = _strip_hora_cmd(text or "")
    if not body:
        return {}
    out: dict[str, str] = {}

    def _put(key: str, val: str) -> None:
        val = (val or "").strip(" \t\n,;.-")
        if not val or out.get(key):
            return
        if key == "make":
            val = _clean_make(val)
        if key == "year":
            ym = re.search(r"(19|20)\d{2}", val)
            if not ym:
                return
            val = ym.group(0)
        if key == "zip":
            z = zip_extract(val) or (val if re.fullmatch(r"\d{5}", val) else "")
            if not z:
                return
            val = z
        if val:
            out[key] = val

    for m in re.finditer(
        rf"(?is)(?P<lab>{_LABEL_NEXT})\s*[:\-]?\s*(?P<val>.+?)(?=(?:{_LABEL_NEXT})\s*[:\-]?|\Z)",
        body,
    ):
        lab = re.sub(r"\s+", "", m.group("lab").lower())
        val = m.group("val")
        if lab in ("ano", "año", "year"):
            _put("year", val)
        elif lab in ("marca", "make", "brand"):
            _put("make", val)
        elif lab in ("modelo", "model"):
            _put("model", val)
        elif lab in ("zip", "zipcode", "codigopostal", "cp"):
            _put("zip", val)

    if not out.get("zip"):
        z = zip_extract(body)
        if z:
            out["zip"] = z
    if not out.get("year"):
        ym = re.search(r"\b((?:19|20)\d{2})\b", body)
        if ym:
            out["year"] = ym.group(1)

    blob = body
    for z in list(filter(None, [out.get("zip")])):
        blob = re.sub(rf"\b{re.escape(z)}\b", " ", blob)
    veh = legacy.extract_vehicle_from_text(blob)
    for k in ("year", "make", "model"):
        if veh.get(k) and not out.get(k):
            out[k] = str(veh[k])

    if out.get("make") and not out.get("model"):
        # "Marca: Mercedes Modelo: C300" ya cubierto; si quedó "Mercedes C300" junto
        rest = re.sub(re.escape(out["make"]), " ", blob, flags=re.I)
        veh2 = legacy.extract_vehicle_from_text(f"{out.get('year') or ''} {out['make']} {rest}")
        if veh2.get("model"):
            out["model"] = str(veh2["model"])

    return {k: v for k, v in out.items() if v}


def is_force_slots_command(text: str) -> bool:
    """!hora solo, o !hora + datos en cualquier orden. No dispara con 'a qué hora'."""
    t = (text or "").strip()
    if not t:
        return False
    if _CMD_RE.match(re.sub(r"[.!?…,;:]+$", "", t)):
        return True
    if _CMD_ANY.search(t):
        return True
    if _CMD_HEAD.match(t):
        rest = _strip_hora_cmd(t)
        if rest and (parse_hora_block(t) or zip_extract(rest)):
            return True
    return False


def _history_user_texts(lead: dict) -> list[str]:
    try:
        hist = json.loads((lead or {}).get("history") or "[]")
    except Exception:
        hist = []
    return [str(i.get("content") or "") for i in hist if (i.get("role") or "") == "user" and i.get("content")]


def _messenger_user_texts(psid: str) -> list[str]:
    """Últimos mensajes del cliente en ESE chat de Messenger (por si el history local está corto)."""
    psid = str(psid or "")
    if not re.fullmatch(r"\d{5,}", psid):
        return []
    try:
        import httpx

        page_id = str(__import__("os").getenv("PAGE_ID", ""))
        r = httpx.get(
            f"{legacy.GRAPH}/me/conversations",
            params=legacy._graph_params(
                platform="messenger",
                user_id=psid,
                fields="messages.limit(40){message,from}",
            ),
            timeout=12,
            trust_env=False,
        )
        if r.status_code >= 400:
            return []
        convs = ((r.json() or {}).get("data")) or []
        out: list[str] = []
        for c in convs:
            for m in ((c.get("messages") or {}).get("data")) or []:
                frm = str(((m.get("from") or {}).get("id")) or "")
                txt = (m.get("message") or "").strip()
                if txt and frm and frm != page_id:
                    out.append(txt)
        return out
    except Exception:
        return []


def apply_pending_model_choice(psid: str, lead: dict | None = None, extra: str = "") -> dict:
    """Si el bot preguntó C1500/C2500 y el cliente (o !hora 1) dijo el número, lo fija."""
    upsert_lead(psid)
    lead = lead or get_lead(psid) or {}
    texts: list[str] = []
    raw = (extra or "").strip()
    if raw:
        texts.append(raw)
        texts.append(re.sub(r"(?i)!?hora\b", " ", raw).strip())
        m = re.search(r"(?i)!?hora\s+(\d{1,2})\b", raw)
        if m:
            texts.append(m.group(1))
    texts.extend(reversed(_history_user_texts(lead)))
    try:
        texts.extend(_messenger_user_texts(psid))
    except Exception:
        pass
    seen: set[str] = set()
    for t in texts:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        if legacy._capture_model_choice(psid, lead, t):
            return get_lead(psid)
        lead = get_lead(psid) or lead
    return get_lead(psid) or lead


def harvest_chat_facts(psid: str, lead: dict | None = None) -> dict:
    """Saca año/marca/modelo y ZIP de ESTE chat (historial + Messenger) y los guarda."""
    upsert_lead(psid)
    lead = lead or get_lead(psid) or {}
    found: dict[str, str] = {}
    seen: list[str] = []
    for t in list(reversed(_history_user_texts(lead))) + _messenger_user_texts(psid):
        if t in seen:
            continue
        seen.append(t)
        if not found.get("zip"):
            z = zip_extract(t)
            if not z:
                try:
                    res = resolve_zip(t)
                    z = (res or {}).get("zip") or ""
                    if res.get("city") and not found.get("city"):
                        found["city"] = res["city"]
                    if res.get("state") and not found.get("state"):
                        found["state"] = res["state"]
                except Exception:
                    z = ""
            if z:
                found["zip"] = z
        veh = legacy.extract_vehicle_from_text(t)
        for k in ("year", "make", "model", "style"):
            val = veh.get(k)
            if not val:
                continue
            if k == "model" and legacy._is_junk_model(str(val), veh.get("make") or found.get("make")):
                continue
            if k == "model" and found.get("model") and legacy._is_junk_model(found.get("model"), found.get("make")):
                found[k] = str(val)
            elif not found.get(k):
                found[k] = str(val)
        if found.get("zip") and found.get("year") and found.get("make") and found.get("model"):
            break

    for k in ("year", "make", "model", "zip"):
        if found.get(k):
            continue
        prev = (lead.get(k) or "").strip()
        if not prev:
            continue
        if k == "model" and legacy._is_junk_model(prev, found.get("make") or lead.get("make")):
            continue
        found[k] = prev

    if not (found.get("year") and found.get("make") and found.get("model") and found.get("zip")):
        blob = " | ".join(seen[:8])
        extra = interpret.fill_from_messy(blob, {**lead, **found})
        for k, v in extra.items():
            if v and not found.get(k):
                found[k] = v

    if found.get("year") or found.get("make") or found.get("model"):
        nv = normalize_vehicle(found.get("year") or "", found.get("make") or "", found.get("model") or "")
        if nv.get("make"):
            found["make"] = nv["make"]
        if nv.get("model"):
            found["model"] = nv["model"]

    if found:
        upsert_lead(psid, **found)
    return get_lead(psid)


def _with_job_header(lead: dict, user_text: str, reply: str) -> str:
    """Auto + zip + precio encima de los horarios."""
    reply = (reply or "").strip()
    label = legacy._vehicle_label(lead)
    z = legacy._lead_zip(lead)
    price_line = ""
    if "$" not in reply:
        try:
            price_line = legacy._fmt_price_num(
                legacy.quote_price(
                    lead.get("year"), lead.get("make") or "", lead.get("model") or "",
                    user_text or "", lead=lead, psid=lead.get("psid"),
                )
            )
        except Exception:
            price_line = ""
    if label and label not in reply:
        bit = label + (f" · código postal {z}" if z else "")
        if price_line:
            bit = f"{bit}\n{price_line}"
        return f"{bit}\n{reply}"
    if price_line and price_line not in reply:
        return f"{price_line}\n{reply}"
    return reply


def _format_complete_slots(lead: dict, options: list[dict], mode: str = "domicilio") -> str:
    pitch = rules.taller_offer_for_message(lead, options) if mode != "sucursal" else {}
    reply = rules.format_slots_message(
        options,
        "sucursal" if mode == "sucursal" else "domicilio",
        offer_taller=bool(pitch.get("offer_taller")),
        taller_days_earlier=pitch.get("taller_days_earlier") or 0,
        taller_first_label=pitch.get("taller_first_label") or "",
        user_text="",
    )
    reply = rules.scrub_client_reply(reply, lead)
    label = legacy._vehicle_label(lead)
    z = legacy._lead_zip(lead)
    if label:
        head = label + (f" · código postal {z}" if z else "")
        if head not in reply:
            reply = f"{head}\n{reply}"
    return reply


PHONE_CLOSE = (
    "Envíeme su número de teléfono para coordinar la cita."
)
VIN_REQUEST = (
    "Envíeme una foto clara del VIN del vehículo para confirmar "
    "las medidas exactas del cristal."
)
_VIN_TEXT = re.compile(r"(?i)\b(?:vin|n[uú]mero\s+de\s+serie)\b")
_CLIENT_CALLED = re.compile(
    r"(?i)\b("
    r"ya\s+(te\s+)?llam[eé]|te\s+llam[eé]|estoy\s+llamando|"
    r"llam[eé]\s+por\s+(facebook|aqu[ií]|aqui)|"
    r"i\s+called|already\s+called|calling\s+(you|now)"
    r")\b"
)
_WANTS_CALLBACK = re.compile(
    r"(?i)\b(para\s+(que|k)\s+me\s+llamen|ll[aá]mame|call\s+me)\b"
)
# Cliente dice que ÉL llama: "te llamo mañana", "yo los llamo", "los llamo"
_CLIENT_CALLS_US = re.compile(
    r"(?i)\b(te\s+llamo|los\s+llamo|le\s+llamo|yo\s+llamo|"
    r"i'?ll\s+call\s+(you|u)|i\s+will\s+call\s+(you|u))\b"
)
# Mensajes que solo informan que el cliente ya llamó o se quejan de que no
# contestamos. No contienen una pregunta ni un dato para cotizar; responder
# con el flujo automático (zip/precio) queda fuera de contexto.
_NO_REPLY_NEEDED = re.compile(
    r"(?i)\b("
    r"(?:ya\s+)?(?:te|les?|los?)\s*(?:llam[eé]|marqu[eé])|"
    r"(?:no\s+me\s+(?:han\s+)?contest(?:aron|an)|no\s+contestaron)|"
    r"(?:pero\s+)?(?:no\s+)?me\s+contestaron"
    r")\b"
)
# Mensaje SISTEMA de Facebook (nunca llega por webhook, solo via
# sync_from_graph): "Puedes llamar a X en los próximos 7 días" / "Tienes
# una llamada perdida de X". Luis Navas y Elfego Roblero se quedaban sin
# respuesta porque nada en el flujo detectaba este texto — no es del
# cliente, lo genera Messenger cuando hay una llamada de voz perdida.
_FB_MISSED_CALL = re.compile(
    r"(?i)(puedes\s+llamar\s+a\s+.+\s+en\s+los\s+pr[oó]ximos\s+\d+\s+d[ií]as|"
    r"tienes?\s+una\s+llamada\s+perdida\s+de\b|"
    r"missed\s+(a\s+)?call\s+from\b|you\s+can\s+call\s+.+\s+for\s+the\s+next\s+\d+\s+days)"
)


def _attach_phone_close(msg: str) -> str:
    msg = (msg or "").rstrip()
    if not msg:
        return PHONE_CLOSE
    if re.search(r"(?i)(llamarnos por facebook|call us on facebook|marcarte ahora)", msg):
        return msg
    return f"{msg}\n\n{PHONE_CLOSE}"


def _price_and_shop_hours(lead: dict, options: list) -> str:
    """Precio + horarios de taller. Nada más."""
    n = legacy._fmt_price_num(
        legacy.quote_price(
            lead.get("year"),
            lead.get("make") or "",
            lead.get("model") or "",
            lead=lead,
            psid=lead.get("psid"),
        )
    )
    body = rules.format_slots_message(options, "sucursal", user_text="")
    body = re.sub(r"\n*¿Cuál te sirve\?.*", "", body, flags=re.I | re.S).strip()
    return _with_job_header(lead, "", f"{n}\n{body}")


def _seal_slots_with_phone(psid: str, lead: dict, reply: str) -> str:
    """Precio + taller, mutea. El humano sigue."""
    reply = (reply or "").rstrip()
    reply = re.sub(r"\n*¿Cuál te sirve\?.*", "", reply, flags=re.I | re.S).strip()
    try:
        _write_slot_preference_block(get_lead(psid) or lead)
    except Exception:
        pass
    legacy._mute_for_human(psid, reason="horarios_pide_tel")
    return _attach_phone_close(reply)


def force_send_slots(psid: str, command_text: str = "") -> str:
    """!hora (y si pegas año/marca/modelo/zip, usa ESO). El resto lo saca del chat."""
    upsert_lead(psid)
    lead0 = get_lead(psid) or {}
    st0 = (lead0.get("stage") or "").lower()
    if st0 in ("perdido", "finalizado", "ganado"):
        log.warning("force_slots skip closed psid=%s stage=%s", psid, st0)
        return ""
    try:
        hist0 = json.loads(lead0.get("history") or "[]")
    except Exception:
        hist0 = []
    last_user = ""
    for item in reversed(hist0):
        if isinstance(item, dict) and item.get("role") == "user":
            last_user = str(item.get("content") or "")
            break
    if last_user and legacy._client_wants_out(last_user):
        log.warning("force_slots skip opt-out psid=%s", psid)
        upsert_lead(psid, stage="perdido", bot_muted="1")
        return ""
    lead = harvest_chat_facts(psid, lead0)
    lead = apply_pending_model_choice(psid, lead, extra=command_text or "")
    block = parse_hora_block(command_text or "")
    if block:
        fields = {k: v for k, v in block.items() if v}
        if fields.get("year") or fields.get("make") or fields.get("model"):
            nv = normalize_vehicle(
                fields.get("year") or lead.get("year") or "",
                fields.get("make") or lead.get("make") or "",
                fields.get("model") or lead.get("model") or "",
            )
            if nv.get("make"):
                fields["make"] = nv["make"]
            if nv.get("model"):
                fields["model"] = nv["model"]
        upsert_lead(psid, **fields)
        lead = get_lead(psid)

    if not legacy._slots_ready(lead):
        falta = []
        if not legacy._has_vehicle(lead):
            falta.append("año marca modelo")
        if not legacy._lead_zip(lead):
            falta.append("código postal")
        reply = "En este chat me falta " + " y ".join(falta) + " para sacar horarios."
        append_history(psid, "assistant", reply)
        return reply

    options = safelite_shop_slots(lead, days=21) or []
    slots_err = None
    mode = "sucursal"
    if options:
        upsert_lead(
            psid,
            service_mode="sucursal",
            slots_json=json.dumps(options, ensure_ascii=False),
            slots_fetched_at=datetime.utcnow().isoformat(),
            slots_error="",
        )
        lead = get_lead(psid)

    if not options:
        log.warning("force_slots shop empty psid=%s", psid)
        return ""
    else:
        drafted = _price_and_shop_hours(lead, options)
        if legacy._already_showed_slots(lead) or legacy.should_skip_send(psid, drafted):
            log.warning("force_slots skip duplicate psid=%s", psid)
            return ""
        reply = _seal_slots_with_phone(psid, lead, drafted)
    reply = (reply or "").strip() or "Ahora no pude sacar horarios. Reintento en un momento."
    append_history(psid, "assistant", reply)
    append_history(psid, "agent", f"[force_slots_command] {FORCE_SLOTS_COMMAND}")
    # Si estamos pidiendo modelo/zip, NO silenciar (Barni: !hora no debe morir).
    if not options and legacy._is_handoff_msg(reply):
        legacy._mute_for_human(psid, reason="force_slots_command")
    return reply


def _slot_request_explicit(text: str, analysis: dict) -> bool:
    if analysis.get("is_schedule"):
        return True
    if parse_when_intent(text or "").get("has_when"):
        return True
    return bool(re.search(r"(?i)\b(horario|horarios|que dia|qu[eé] d[ií]a|disponib|today|tomorrow|hoy|mañana|manana|asap)\b", text or ""))


def _load_slot_cache(lead: dict) -> list[dict]:
    return rules.cached_slots(lead)


def _should_refresh_slot_cache(lead: dict, user_text: str, analysis: dict) -> bool:
    if not _load_slot_cache(lead):
        return True
    if rules.schedule_question_after_stale_cache(lead, user_text, analysis):
        return True
    return False


def _hard_handoff_reply(lead: dict, text: str) -> str | None:
    low = legacy._norm_es(text or "")
    lang = legacy._client_lang(lead, text, psid=lead.get("psid"))

    # Ricardo Lazo / APRENDIZAJE #2 — techo/sunroof/panorámico no es parabrisas.
    if re.search(r"\b(techo|sunroof|panoram|panor[aá]mico|moonroof)\b", low):
        return legacy._t(
            lang,
            "Eso no es parabrisas de frente. Te atiende un agente en un momento para cotizarlo bien.",
            "That isn't a front windshield. An agent will jump in shortly to quote it correctly.",
        )

    # Adrian Alfaro / Marcos — trasero multipieza o atrás de cabina se escala.
    if (
        re.search(r"\b(traser|rear|back|atras de la cabina|behind the cab|sliding rear)\b", low)
        and re.search(r"\b(3 piezas|tres piezas|3 piece|three piece|sliding|ventana)\b", low)
    ):
        return legacy._t(
            lang,
            "Ese vidrio trasero se lo paso a un agente para revisarlo bien y seguir contigo.",
            "That rear glass needs an agent to review it properly and continue with you.",
        )
    return None


def _write_slot_preference_block(lead: dict) -> None:
    note("")
    note("=" * 50)
    note(f"PREFERENCIA CLIENTE (cierre manual)  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    note(f"Cliente : {lead.get('name') or '-'} | psid {lead.get('psid') or '-'}")
    note(f"Tel     : {lead.get('client_phone') or lead.get('phone') or '-'}")
    note(f"Auto    : {(lead.get('year') or '').strip()} {(lead.get('make') or '').strip()} {(lead.get('model') or '').strip()}".strip())
    note(f"ZIP     : {lead.get('zip') or '-'}")
    note(f"Modo    : {(lead.get('service_mode') or 'domicilio').upper()}")
    note(f"Pref    : {lead.get('slot_date') or '-'}  {lead.get('slot_time') or '-'}")
    note(f"Slot ID : {lead.get('slot_id') or '-'}")
    note(f"Portal  : {lead.get('portal') or '-'}")
    note(f"WO      : {lead.get('work_order') or '-'}")
    note(f"Notas   : bot dijo llamarnos por Facebook y se apago; humano cierra")
    note("=" * 50)


def _save_slot_choice(psid: str, chosen: dict, service_mode: str) -> str:
    legacy._save_slot_choice(psid, chosen)
    upsert_lead(psid, confirm_pending="", stage="cerrando", service_mode=service_mode, bot_muted="1")
    lead = get_lead(psid)
    _write_slot_preference_block(lead)
    legacy._mute_for_human(psid, reason=f"slot_elegido_manual:{lead.get('slot_date')}_{lead.get('slot_time')}")
    return rules.scrub_client_reply(rules.note_slot_for_human(lead), lead)


def _ask_phone_and_die(psid: str, lead: dict, user_text: str, reason: str = "post_horarios_pide_tel") -> str:
    """Cierre lineal: horarios ya salieron. Pedir número o que llamen, y apagar."""
    # SIN ZIP no pedir teléfono/Facebook — primero pedir ZIP
    if not legacy._lead_zip(lead):
        return legacy._ask_zip_now(lead, user_text or "")
    try:
        options = _load_slot_cache(lead) or []
        if options:
            result = interpret_slot_request(user_text or "", options, lead=lead)
            if result.get("status") == "matched" and result.get("slot"):
                chosen = result["slot"]
                mode = (lead.get("service_mode") or "domicilio").lower()
                legacy._save_slot_choice(psid, chosen)
                upsert_lead(psid, confirm_pending="", service_mode=mode)
    except Exception:
        pass
    lead = get_lead(psid) or lead
    try:
        _write_slot_preference_block(lead)
    except Exception:
        pass
    day = (lead.get("slot_date") or "").strip()
    if day:
        try:
            when = legacy._fmt_day(day)
        except Exception:
            when = day
        reply = f"Anoté tu preferencia. {PHONE_CLOSE}"
    else:
        reply = PHONE_CLOSE
    legacy._mute_for_human(psid, reason=reason)
    return reply


def _client_called(text: str) -> bool:
    return bool(_CLIENT_CALLED.search(text or ""))


def _finish_good(psid: str, lead: dict, *, phone: str = "", reason: str = "horarios_tiene_tel") -> str:
    """Guarda el contacto y pide VIN/foto antes de dejar el cierre al humano."""
    fields: dict = {}
    if phone:
        fields["phone"] = phone
        fields["client_phone"] = phone
    if fields:
        upsert_lead(psid, **fields)
    try:
        _write_slot_preference_block(get_lead(psid) or lead)
    except Exception:
        pass
    legacy._mute_for_human(psid, reason=reason)
    upsert_lead(
        psid,
        stage="cerrando",
        bot_muted="1",
        slots_error="vin_pendiente",
        finished_at=datetime.utcnow().isoformat(),
    )
    return VIN_REQUEST


def _phone_reask_or_empty(stored: str) -> str:
    """No repetir 'mándame tu número' si el lead ya lo tiene guardado.

    Bug real (Boris Pereira, cristal trasero 2026-09-18): cliente ya había
    dado el teléfono, el bot pidió VIN, el cliente respondió "es el cristal
    trasero" (no un VIN) y el fallback de este mismo bloque volvía a pedir
    el número que ya teníamos → loop confuso, "error fatal" reportado.
    """
    return "" if stored else PHONE_CLOSE


def _linear_after_slots(psid: str, lead: dict, user_text: str) -> str:
    """Después de mandar horarios: número o que llamen → good/fin. Nada más."""
    if _CLIENT_CALLS_US.search(legacy._norm_es(user_text or "")):
        legacy._mute_for_human(psid, reason="cliente_llama")
        return ""
    phone = legacy._extract_phone(user_text or "") or ""
    stored = (lead.get("client_phone") or lead.get("phone") or "").strip()
    if phone or _client_called(user_text or "") or (stored and _WANTS_CALLBACK.search(user_text or "")):
        return _finish_good(psid, lead, phone=phone or stored, reason="horarios_tiene_tel")
    if legacy.wants_price(user_text or ""):
        p = (legacy._reply_price_short(lead, user_text or "") or "").strip()
        legacy._mute_for_human(psid, reason="horarios_pide_tel")
        if p and "número" not in p.lower() and "numero" not in p.lower():
            return f"{p} {_phone_reask_or_empty(stored)}".strip()
        return p or _phone_reask_or_empty(stored)
    if legacy._client_later(user_text or ""):
        legacy._mute_for_human(psid, reason="horarios_pide_tel")
        return "Ok, cuando estés listo me escribes."
    if legacy.is_bot_muted(lead) or (lead.get("slots_error") or "").startswith("horarios_"):
        try:
            options = _load_slot_cache(lead) or []
            if options:
                result = interpret_slot_request(user_text or "", options, lead=lead)
                if result.get("status") == "matched" and result.get("slot"):
                    return _ask_phone_and_die(psid, lead, user_text, reason="slot_elegido_manual")
        except Exception:
            pass
        low = legacy._norm_es(user_text or "")
        if re.fullmatch(
            r"(ok|okay|va|vale|gracias|grasias|perfecto|listo|si|sii+|super)[.! ]*",
            low,
        ):
            return ""
        z_only = zip_extract(user_text or "")
        if z_only and z_only == legacy._lead_zip(lead) and not re.search(r"[a-z]{4,}", low):
            # Cliente mandó el ZIP: usarlo y mostrar horarios si hay auto
            if legacy._has_vehicle(lead):
                upsert_lead(psid, zip=z_only)
                lead = get_lead(psid) or lead
                analysis = analyze_client_message(user_text or "", lead)
                reply = _show_best_slots(psid, lead, user_text, analysis)
                return reply if reply else ""
            return ""
        if re.search(r"\b(compania|compania|empresa|fraude|estafa|como se llaman)\b", low):
            return f"Parabrisas solution. {_phone_reask_or_empty(stored)}".strip()
        if re.search(r"\b(cuando|listo|vienen|van a venir|a que hora)\b", low):
            return f"Los días que te pasé. {_phone_reask_or_empty(stored)}".strip()
        return _phone_reask_or_empty(stored)
    return _ask_phone_and_die(psid, lead, user_text)


def _deliver(psid: str, lead: dict, user_text: str, reply: str, *, allow_repeat: bool = False) -> str:
    """Manda el reply. Si es la 2ª vez de lo mismo, llama por Facebook y mutea."""
    before = dict(lead or {})
    lead = get_lead(psid) or lead
    reply = rules.scrub_client_reply(reply or "", lead)
    first_slots = legacy._is_slots_outbound(reply) and not legacy._already_showed_slots(lead)
    if not allow_repeat and not first_slots and reply and legacy._would_repeat(lead, reply):
        reply = _ask_phone_and_die(psid, lead, user_text, reason="loop_break")
    incoming = user_text or "[foto]"
    last_user = ""
    try:
        for item in reversed(json.loads((get_lead(psid) or lead or {}).get("history") or "[]")):
            if isinstance(item, dict) and (item.get("role") or "") == "user":
                last_user = str(item.get("content") or "")
                break
    except Exception:
        last_user = ""
    if last_user != incoming:
        append_history(psid, "user", incoming)
    append_history(psid, "assistant", reply)
    try:
        from .aprender import observe

        observe(user_text or "", before, get_lead(psid) or lead)
    except Exception:
        pass
    return reply


def _show_mobile_slots(psid: str, lead: dict, user_text: str, analysis: dict, refresh: bool = False) -> str:
    if legacy._already_showed_slots(lead):
        log.warning("slots skip already sent psid=%s", psid)
        return ""
    options = _load_slot_cache(lead)
    slots_err = None
    if refresh or not options:
        options, slots_err = legacy.ensure_slots_for_lead(psid, lead, force=True)
        lead = get_lead(psid)
    if not options:
        try:
            shop = safelite_shop_slots(lead, days=14) or []
        except Exception:
            shop = []
        if shop:
            upsert_lead(
                psid,
                service_mode="sucursal",
                slots_json=json.dumps(shop, ensure_ascii=False),
                slots_fetched_at=datetime.utcnow().isoformat(),
                slots_error="",
            )
            return _show_shop_slots(psid, get_lead(psid), user_text, analysis, refresh=False)
        return legacy._sin_horarios_reply(psid, lead, slots_err)
    pitch = rules.taller_offer_for_message(lead, options)
    reply = rules.format_slots_message(
        options,
        "domicilio",
        offer_taller=bool(pitch.get("offer_taller")),
        taller_days_earlier=pitch.get("taller_days_earlier") or 0,
        taller_first_label=pitch.get("taller_first_label") or "",
        user_text=user_text or "",
    )
    upsert_lead(psid, stage="calificando")
    reply = _with_job_header(lead, user_text, rules.scrub_client_reply(reply, lead))
    return _seal_slots_with_phone(psid, lead, reply)


def _show_shop_slots(psid: str, lead: dict, user_text: str, analysis: dict, refresh: bool = False) -> str:
    if legacy._already_showed_slots(lead):
        log.warning("slots skip already sent psid=%s", psid)
        return ""
    options = _load_slot_cache(lead)
    if refresh or not options or (lead.get("service_mode") or "").lower() != "sucursal":
        options = safelite_shop_slots(lead, days=21) or []
    if not options:
        # Zip → solo taller. Si no hay ventanas, no mandar "no me aparece el modelo".
        log.warning("shop slots empty psid=%s %s %s %s", psid, lead.get("year"), lead.get("make"), lead.get("model"))
        return ""
    upsert_lead(
        psid,
        service_mode="sucursal",
        slots_json=json.dumps(options, ensure_ascii=False),
        slots_fetched_at=datetime.utcnow().isoformat(),
        slots_error="",
        stage="calificando",
    )
    lead = get_lead(psid)
    reply = _price_and_shop_hours(lead, options)
    return _seal_slots_with_phone(psid, lead, reply)


def _pick_from_available_slots(psid: str, lead: dict, user_text: str) -> str | None:
    mode = (lead.get("service_mode") or "domicilio").lower()
    if mode == "sucursal":
        options = _load_slot_cache(lead) or safelite_shop_slots(lead, days=14)
        if not options:
            err = (lead.get("slots_error") or "").strip()
            if err in ("vehicle_fail", "vehicle_ambiguo", "vehicle_api_error", "sin_auto"):
                return legacy._sin_horarios_reply(psid, lead, err)
            upsert_lead(psid, stage="calificando", bot_muted="")
            return (
                "En taller no hay ventanas ahora. "
                "¿Te busco a domicilio u otro día?"
            )
        result = interpret_slot_request(user_text or "", options, lead=lead)
        if result.get("status") == "matched" and result.get("slot"):
            return _save_slot_choice(psid, result["slot"], "sucursal")
        if result.get("message"):
            return rules.scrub_client_reply(result["message"], lead)
        return None

    options = _load_slot_cache(lead)
    if not options and _slot_request_explicit(user_text, analyze_client_message(user_text or "", lead)):
        options, _err = legacy.ensure_slots_for_lead(psid, lead, force=True)
        lead = get_lead(psid)
    if not options:
        return None
    result = interpret_slot_request(user_text or "", options, lead=lead)
    if result.get("status") == "matched" and result.get("slot"):
        return _save_slot_choice(psid, result["slot"], "domicilio")
    if result.get("message"):
        return rules.scrub_client_reply(result["message"], lead)
    if result.get("status") == "no_intent" and re.search(r"\b(lo mas pronto|lo antes|primer hueco|el primero|cuando sea)\b", legacy._norm_es(user_text or "")):
        return _save_slot_choice(psid, options[0], "domicilio")
    return None


def _show_best_slots(psid: str, lead: dict, user_text: str, analysis: dict) -> str:
    """
    Domicilio primero. Taller solo si:
      - el cliente lo pidió explícitamente, o
      - taller llega 1+ días ANTES que domicilio (urgencia), o
      - domicilio no tiene ventanas y taller sí.
    """
    mode_pref = (lead.get("service_mode") or "").lower()
    if mode_pref == "sucursal":
        return _show_shop_slots(psid, lead, user_text, analysis, refresh=True)
    # Domicilio explícito o default → intentar mobile primero
    options, slots_err = legacy.ensure_slots_for_lead(psid, lead, force=True)
    if options:
        lead = get_lead(psid) or lead
        # Si taller llega 1+ días antes, ofrecerlo como alternativa en el mensaje
        pitch = rules.taller_offer_for_message(lead, options)
        reply = rules.format_slots_message(
            options,
            "domicilio",
            offer_taller=bool(pitch.get("offer_taller")),
            taller_days_earlier=pitch.get("taller_days_earlier") or 0,
            taller_first_label=pitch.get("taller_first_label") or "",
            user_text=user_text or "",
        )
        upsert_lead(psid, stage="calificando")
        reply = _with_job_header(lead, user_text, rules.scrub_client_reply(reply, lead))
        return _seal_slots_with_phone(psid, lead, reply)
    # Domicilio vacío → taller como fallback
    try:
        shop = safelite_shop_slots(lead, days=14) or []
    except Exception:
        shop = []
    if shop:
        upsert_lead(
            psid,
            service_mode="sucursal",
            slots_json=json.dumps(shop, ensure_ascii=False),
            slots_fetched_at=datetime.utcnow().isoformat(),
            slots_error="",
        )
        return _show_shop_slots(psid, get_lead(psid), user_text, analysis, refresh=False)
    return legacy._sin_horarios_reply(psid, lead, slots_err)


def _vehicle_differs(lead: dict | None, vehicle: dict | None) -> bool:
    lead = lead or {}
    vehicle = vehicle or {}
    if not (vehicle.get("year") and vehicle.get("make") and vehicle.get("model")):
        return False
    current = legacy._complete_vehicle_fields(lead)
    if not (current.get("year") and current.get("make") and current.get("model")):
        return False
    return any(
        legacy._norm_es(str(current.get(k) or "")) != legacy._norm_es(str(vehicle.get(k) or ""))
        for k in ("year", "make", "model")
    )


def _reset_for_new_vehicle(psid: str, vehicle: dict, *, zip_code: str = "") -> dict:
    fields = {
        "year": str(vehicle.get("year") or ""),
        "make": str(vehicle.get("make") or ""),
        "model": str(vehicle.get("model") or ""),
        "slots_json": "",
        "slots_fetched_at": "",
        "slot_id": "",
        "slot_date": "",
        "slot_time": "",
        "slots_error": "",
        "quoted_price": "",
        "work_order": "",
        "portal": "",
        "stage": "calificando",
        "bot_muted": "",
    }
    if zip_code:
        fields["zip"] = zip_code
    upsert_lead(psid, **fields)
    return get_lead(psid) or fields


def run_agent(psid: str, text: str, image_urls: list[str] | None = None) -> str:
    upsert_lead(psid)
    lead = get_lead(psid)
    user_text = fix_chat_typos((text or "").strip())
    if image_urls:
        user_text = (user_text + " [el cliente envio foto del cristal]").strip()

    if _NO_REPLY_NEEDED.search(legacy._norm_es(user_text or "")):
        append_history(psid, "user", user_text)
        return ""

    # Llamada perdida de Messenger (mensaje de sistema, no del cliente):
    # siempre pedir el teléfono para que un humano marque ya, sin importar
    # en qué etapa/estado esté el lead.
    if _FB_MISSED_CALL.search(user_text or ""):
        append_history(psid, "user", user_text)
        lang = legacy._client_lang(lead, user_text, psid=psid)
        reply = legacy._t(
            lang,
            "Vi que se perdió tu llamada. Déjame tu número de teléfono y te marco lo antes posible.",
            "I saw we missed your call. Leave me your phone number and I'll call you back ASAP.",
        )
        append_history(psid, "assistant", reply)
        return reply

    # "1" / "2" tras "¿Cuál de estos es tu GMC?" → modelo real y horarios YA.
    if user_text and legacy._capture_model_choice(psid, lead, user_text):
        upsert_lead(
            psid,
            bot_muted="",
            slots_error="",
            slots_json="",
            slots_fetched_at="",
            slot_id="",
            slot_date="",
            slot_time="",
            stage="calificando",
        )
        lead = get_lead(psid)
        if legacy._lead_zip(lead) and legacy._has_vehicle(lead):
            analysis = analyze_client_message(user_text, lead)
            reply = _show_best_slots(psid, lead, user_text, analysis)
            return _deliver(psid, lead, user_text, reply, allow_repeat=True) if reply else ""

    if legacy.is_bot_muted(lead):
        if user_text:
            append_history(psid, "user", user_text or "[foto]")
        # Ya recibimos el VIN/foto: silencio total pase lo que pase (zip, "ok", lo que sea).
        if (lead.get("slots_error") or "") == "vin_recibido":
            return ""
        # Aunque esté callado, sigue leyendo zip/auto para el humano.
        analysis_muted = analyze_client_message(user_text or text or "", lead)
        if analysis_muted.get("is_opt_out") or legacy._client_wants_out(user_text or ""):
            upsert_lead(psid, stage="perdido", bot_muted="1", slots_error="cliente_fuera")
            reply = legacy._t(
                legacy._client_lang(lead, user_text, psid=psid),
                "Listo, sin problema. Cuando quieras me escribes.",
                "All good, no problem. Message me whenever you want.",
            )
            append_history(psid, "assistant", reply)
            return reply
        if (
            (lead.get("slots_error") or "") == "vin_pendiente"
            and (_VIN_TEXT.search(user_text or "") or "[el cliente envio foto" in (user_text or "").lower())
        ):
            upsert_lead(psid, slots_error="vin_recibido", stage="cerrando", bot_muted="1")
            return ""
        if (lead.get("stage") or "").lower() in ("perdido", "finalizado", "ganado"):
            return ""
        incoming_vehicle = analysis_muted.get("vehicle") or legacy.extract_vehicle_from_text(user_text or "")
        incoming_zip = analysis_muted.get("zip") or zip_extract(user_text or "") or legacy._extract_zip(user_text or "")
        if (
            incoming_vehicle.get("year")
            and incoming_vehicle.get("make")
            and incoming_vehicle.get("model")
            and incoming_zip
            and (not legacy._has_vehicle(lead) or _vehicle_differs(lead, incoming_vehicle))
        ):
            lead = _reset_for_new_vehicle(psid, incoming_vehicle, zip_code=incoming_zip)
            analysis = analyze_client_message(user_text or "", lead)
            reply = _show_best_slots(psid, lead, user_text, analysis)
            return _deliver(psid, lead, user_text, reply, allow_repeat=True) if reply else ""

        # Lineal: si ya mandamos horarios del mismo vehículo, solo número. Nada más.
        if legacy._already_showed_slots(lead) or str(lead.get("slots_error") or "").startswith("horarios"):
            return _linear_after_slots(psid, lead, user_text or "")
        if not legacy._lead_zip(lead):
            z = analysis_muted.get("zip") or zip_extract(user_text or "")
            if z:
                upsert_lead(psid, zip=z)
                lead = get_lead(psid) or lead
        if not legacy._has_vehicle(lead):
            veh = incoming_vehicle
            fields = {k: veh[k] for k in ("year", "make", "model") if veh.get(k) and not (lead.get(k) or "").strip()}
            if fields:
                upsert_lead(psid, **fields)
                lead = get_lead(psid) or lead
        lead = get_lead(psid) or lead
        if not legacy._slots_ready(lead):
            lead = harvest_chat_facts(psid, lead)
        # Humano pidió los datos: si ya hay auto+zip y NUNCA mandamos horarios, empujar eso.
        if lead.get("work_order"):
            return ""
        if legacy._slots_ready(lead) and not legacy._already_showed_slots(lead):
            upsert_lead(psid, bot_muted="", stage="calificando", slots_error="")
            lead = get_lead(psid) or lead
            analysis = analyze_client_message(user_text or "", lead)
            reply = _show_best_slots(psid, lead, user_text, analysis)
            return _deliver(psid, lead, user_text, reply, allow_repeat=True) if reply else ""
        # Si acaba de mandar ZIP y ya tiene auto, mostrar horarios YA
        if legacy._lead_zip(lead) and legacy._has_vehicle(lead) and not legacy._already_showed_slots(lead):
            upsert_lead(psid, bot_muted="", stage="calificando", slots_error="")
            lead = get_lead(psid) or lead
            analysis = analyze_client_message(user_text or "", lead)
            reply = _show_best_slots(psid, lead, user_text, analysis)
            return _deliver(psid, lead, user_text, reply, allow_repeat=True) if reply else ""
        if legacy.wants_price(user_text, analysis_muted) and not lead.get("work_order"):
            upsert_lead(psid, bot_muted="", stage="calificando", slots_error="")
            reply = rules.scrub_client_reply(
                legacy._reply_price_short({**lead, "psid": psid}, user_text or ""), lead
            )
            append_history(psid, "assistant", reply)
            return reply
        # Avanzar: ya hay auto, falta zip → pedirlo una vez (John Figueroa / Georgia).
        if (
            legacy._has_vehicle(lead)
            and not legacy._lead_zip(lead)
            and not legacy._already_asked(lead, "zip")
        ):
            nxt = legacy._next_missing_ask(lead, user_text or "") or ""
            if nxt and not legacy._would_repeat(lead, nxt):
                upsert_lead(psid, bot_muted="", stage="calificando", slots_error="")
                append_history(psid, "assistant", nxt)
                return nxt
        return ""

    lead = legacy._clear_garbage_address(psid, lead)
    fixed = legacy._complete_vehicle_fields(lead)
    if fixed.get("make") and not (lead.get("make") or "").strip():
        upsert_lead(psid, make=fixed["make"])
        lead = get_lead(psid)

    analysis = analyze_client_message(user_text or text or "", lead)
    hard = _hard_handoff_reply({**lead, "psid": psid}, user_text)
    if hard:
        reply = _deliver(psid, lead, user_text, hard)
        if not legacy.is_bot_muted(get_lead(psid)):
            legacy._mute_for_human(psid, reason="hard_rule_escalation")
        return reply

    if analysis.get("is_opt_out") and not lead.get("work_order"):
        lang = legacy._client_lang(lead, user_text, psid=psid)
        reply = legacy._t(lang, "Listo, sin problema. Cuando quieras me escribes.", "All good, no problem. Message me whenever you want.")
        upsert_lead(psid, stage="perdido")
        return _deliver(psid, lead, user_text, reply, allow_repeat=True)

    pref = detect_service_pref(user_text or "")
    if pref:
        upsert_lead(psid, service_mode=pref)
        lead = get_lead(psid)
    elif client_accepts_taller_manana(user_text or "", lead):
        upsert_lead(psid, service_mode="sucursal")
        lead = get_lead(psid)

    previous_vehicle = legacy._complete_vehicle_fields(lead)
    vehicle = analysis.get("vehicle") or {}
    if not (vehicle.get("year") and vehicle.get("make") and vehicle.get("model")):
        extra_v = legacy.extract_vehicle_from_text(user_text or "")
        for key in ("year", "make", "model"):
            if extra_v.get(key) and not vehicle.get(key):
                vehicle[key] = extra_v[key]
    incoming_new_vehicle = _vehicle_differs(previous_vehicle, vehicle)
    if incoming_new_vehicle:
        z_now = analysis.get("zip") or zip_extract(user_text) or legacy._extract_zip(user_text)
        lead = _reset_for_new_vehicle(psid, vehicle, zip_code=z_now or legacy._lead_zip(lead))
    elif vehicle.get("year") or vehicle.get("make") or vehicle.get("model"):
        updates = {}
        merged = legacy._complete_vehicle_fields(lead, vehicle)
        for key in ("year", "make", "model"):
            val = (merged.get(key) or "").strip()
            if val and not (lead.get(key) or "").strip():
                updates[key] = val
            elif val and key in ("make", "model"):
                if key == "model" and legacy._is_junk_model(val, updates.get("make") or lead.get("make")):
                    continue
                if key == "model" and not legacy._is_junk_model(lead.get(key), lead.get("make")):
                    if legacy._is_junk_model(val, updates.get("make") or lead.get("make")):
                        continue
                updates[key] = val
        if vehicle.get("year") and not (lead.get("year") or "").strip():
            updates["year"] = str(vehicle.get("year"))
        if updates:
            upsert_lead(psid, **updates)
            lead = get_lead(psid)

    if analysis.get("is_phone") and analysis.get("phone"):
        upsert_lead(psid, phone=analysis["phone"], client_phone=analysis["phone"])
        lead = get_lead(psid)

    # ZIP de ESTE mensaje (zip, zip code, código postal, 5 dígitos) aunque ya hubiera uno.
    z = analysis.get("zip") or zip_extract(user_text) or legacy._extract_zip(user_text)
    city = state = ""
    if not z:
        try:
            res = resolve_zip(user_text) or {}
        except Exception:
            res = {}
        z = res.get("zip") or ""
        city, state = res.get("city") or "", res.get("state") or ""
    if z:
        prev_z = legacy._lead_zip(lead)
        fields = {"zip": z}
        if city:
            fields["city"] = city
        if state:
            fields["state"] = state
        if prev_z and prev_z != z:
            fields.update({"slots_json": "", "slots_fetched_at": "", "slot_id": "", "slot_date": "", "slot_time": ""})
        upsert_lead(psid, **fields)
        lead = get_lead(psid)
        if not (lead.get("city") and lead.get("state")):
            try:
                zinfo = safelite_zip(z) or {}
            except Exception:
                zinfo = {}
            if zinfo:
                upsert_lead(psid, city=lead.get("city") or zinfo.get("city") or "", state=lead.get("state") or zinfo.get("state") or "")
                lead = get_lead(psid)

    # Si pregunta el precio, contestar el monto ANTES de intentar cargar o
    # mostrar horarios. Tener auto+ZIP no convierte "¿cuánto cobran?" en una
    # solicitud de cita; primero hay que darle el precio que pidió.
    if (analysis.get("is_price") or legacy.wants_price(user_text, analysis)) and not lead.get("work_order"):
        raw = legacy._reply_price_short({**lead, "psid": psid}, user_text)
        return _deliver(psid, lead, user_text, raw, allow_repeat=True)

    if incoming_new_vehicle and legacy._slots_ready(lead) and not lead.get("work_order"):
        analysis = analyze_client_message(user_text or "", lead)
        reply = _show_best_slots(psid, lead, user_text, analysis)
        if not reply:
            return ""
        return _deliver(psid, lead, user_text, reply, allow_repeat=True)

    # Cualquier mensaje sucio que una maquina pueda leer: LLM + validacion.
    if not (legacy._has_vehicle(lead) and legacy._lead_zip(lead)):
        extra = interpret.fill_from_messy(user_text, lead)
        if extra:
            upsert_lead(psid, **extra)
            lead = get_lead(psid)
            # Aprender: si el cliente corrigió un modelo que no encontrábamos
            try:
                from .memory import remember_alias, client_event
                dirty = (analysis.get("vehicle") or {}).get("model") or ""
                real = (extra.get("model") or "").strip()
                if dirty and real and dirty.lower() != real.lower():
                    remember_alias(lead.get("make") or "", dirty, real, psid=psid)
                    client_event(psid, "alias_learned", f"{dirty} → {real}", worked=True)
            except Exception:
                pass

    # Ya vio la lista numerada y ESTE mensaje no eligió → Facebook, no horarios.
    # Si acaba de mandar ZIP, no: solo taller.
    if legacy._already_asked_model_pick(lead) and not z:
        vistos = legacy._pregunta_modelo_reciente(lead)
        if (lead.get("model") or "") not in vistos:
            if not legacy._try_resolve_stored_model(psid, lead):
                return _deliver(
                    psid,
                    get_lead(psid) or lead,
                    user_text,
                    _ask_phone_and_die(psid, lead, user_text, reason="model_pick_no_eligio"),
                    allow_repeat=True,
                )
            lead = get_lead(psid) or lead

    # ZIP + auto → horarios del mejor modo (domicilio primero, taller si llega antes o lo pidió).
    if (
        legacy._slots_ready(lead)
        and not lead.get("slot_id")
        and not legacy._already_showed_slots(lead)
        and not lead.get("work_order")
    ):
        picked = _pick_from_available_slots(psid, lead, user_text)
        if picked:
            return _deliver(psid, lead, user_text, picked, allow_repeat=True)
        reply = _show_best_slots(psid, lead, user_text, analysis)
        if not reply:
            return ""
        return _deliver(psid, lead, user_text, reply, allow_repeat=True)

    if lead.get("work_order"):
        return _deliver(psid, lead, user_text, legacy.handle_post_booking(psid, lead, user_text or ""), allow_repeat=True)

    # Ya mandamos horarios: solo número o que llamen. El bot no sigue vendiendo.
    if legacy._already_showed_slots(lead):
        reply = _linear_after_slots(psid, lead, user_text)
        if not reply:
            return ""
        return _deliver(psid, lead, user_text, reply, allow_repeat=True)

    # Auto + ZIP: mejor modo disponible.
    if legacy._slots_ready(lead) and not lead.get("slot_id"):
        picked = _pick_from_available_slots(psid, lead, user_text)
        if picked:
            return _deliver(psid, lead, user_text, picked, allow_repeat=True)
        reply = _show_best_slots(psid, lead, user_text, analysis)
        if not reply:
            return ""
        return _deliver(psid, lead, user_text, reply, allow_repeat=True)

    # Entendimiento: "te aviso / no estoy listo / 2-3 semanas" — no hostigar.
    if analysis.get("is_later") or legacy._client_later(user_text):
        raw = legacy._t(
            legacy._client_lang(lead, user_text, psid=psid),
            "Ok, cuando estés listo me escribes.",
            "Ok, message me when you're ready.",
        )
        return _deliver(psid, lead, user_text, raw)

    # FAQ / solicitud de humano antes de caer en "faltan datos": si dice
    # "¿Hay alguien con quien pueda chatear?", seguimos el flujo en vez de
    # apagar el bot o repetir un saludo genérico.
    if legacy._faq_extra_detectar(user_text or ""):
        faq = rules.reply_like_human({**lead, "psid": psid}, user_text, analysis)
        if faq:
            return _deliver(psid, lead, user_text, faq)

    # Hasta el zip: corto. Auto o código postal. Nada de FAQ/precio/anuncio.
    if not legacy._has_vehicle(lead) or not legacy._lead_zip(lead):
        if not legacy._has_vehicle(lead):
            y = (lead.get("year") or "").strip()
            mk = (lead.get("make") or "").strip()
            md = (lead.get("model") or "").strip()
            if legacy._already_asked(lead, "vehicle") and not (y or mk or md):
                raw = _ask_phone_and_die(psid, lead, user_text, reason="auto_no_repetir")
                return _deliver(psid, lead, user_text, raw, allow_repeat=True)
            if analysis.get("is_greeting") or analysis.get("primary") == "greeting":
                if not analysis.get("is_vehicle") and not analysis.get("is_address"):
                    raw = legacy._reply_greeting_short({**lead, "psid": psid}, user_text)
                    return _deliver(psid, lead, user_text, raw)
            return _deliver(psid, lead, user_text, legacy._next_missing_ask(lead, text=user_text))
        if legacy._already_asked(lead, "zip"):
            raw = _ask_phone_and_die(psid, lead, user_text, reason="zip_no_repetir")
            return _deliver(psid, lead, user_text, raw, allow_repeat=True)
        raw = legacy._reply_vehicle_short({**lead, "psid": psid}, user_text)
        return _deliver(psid, lead, user_text, raw)

    faq = rules.reply_like_human({**lead, "psid": psid}, user_text, analysis)
    if faq:
        return _deliver(psid, lead, user_text, faq)

    if not legacy._has_vehicle(lead):
        y = (lead.get("year") or "").strip()
        mk = (lead.get("make") or "").strip()
        md = (lead.get("model") or "").strip()
        if legacy._already_asked(lead, "vehicle") and not (y or mk or md):
            raw = _ask_phone_and_die(psid, lead, user_text, reason="auto_no_repetir")
            return _deliver(psid, lead, user_text, raw, allow_repeat=True)
        return _deliver(psid, lead, user_text, legacy._next_missing_ask(lead, text=user_text))

    if not legacy._lead_zip(lead):
        if legacy._already_asked(lead, "zip"):
            raw = _ask_phone_and_die(psid, lead, user_text, reason="zip_no_repetir")
            return _deliver(psid, lead, user_text, raw, allow_repeat=True)
        raw = legacy._reply_vehicle_short({**lead, "psid": psid}, user_text)
        return _deliver(psid, lead, user_text, raw)

    action = {
        "goal": "keep moving toward real slots",
        "reply": rules.rule_fallback_reply({**lead, "psid": psid}, user_text, analysis),
        "cta": "one question max",
    }
    reply = llm.draft_reply({**lead, "psid": psid}, user_text, action)
    return _deliver(psid, lead, user_text, reply or action["reply"])
