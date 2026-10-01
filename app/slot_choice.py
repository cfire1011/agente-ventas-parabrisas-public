"""
Eleccion de horario facil: opciones numeradas + respuestas flexibles.

De 14 leads que llegaron a ver horarios, 0 eligieron uno. Mirando las
conversaciones reales, el bot mostraba un bloque de dias/ventanas SIN numerar y
solo entendia frases exactas tipo "sabado en la mañana". Cuando el cliente
contestaba "1", "el 2", "la primera", "esta semana" o "el que sea", nada
matcheaba y el bot re-mandaba el MISMO bloque de horarios, palabra por palabra,
hasta que el cliente se iba.

Aca se resuelve:
  * numerar las ventanas ("1) sabado 8 de agosto, 8am-12pm")
  * entender "1", "el 2", "la primera", "#3", "opcion 2"
  * entender flexibilidad ("cualquiera", "el que sea", "esta semana")
"""
from __future__ import annotations

import re
import unicodedata

# Cuantas ventanas se numeran como maximo (mas que esto abruma)
MAX_OPCIONES = 5

_ORDINALES = {
    "primera": 1, "primero": 1, "primer": 1, "uno": 1, "una": 1,
    "segunda": 2, "segundo": 2, "dos": 2,
    "tercera": 3, "tercero": 3, "tercer": 3, "tres": 3,
    "cuarta": 4, "cuarto": 4, "cuatro": 4,
    "quinta": 5, "quinto": 5, "cinco": 5,
    "sexta": 6, "sexto": 6, "seis": 6,
    "ultima": -1, "ultimo": -1,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1,
}

# "cualquiera me sirve" — el cliente no quiere elegir, quiere lo mas pronto
_FLEXIBLE = re.compile(
    r"(?i)\b(cualquiera|cual\s*quiera|el\s+que\s+sea|la\s+que\s+sea|lo\s+que\s+sea|"
    r"como\s+sea|me\s+da\s+igual|igual\s+me\s+da|cuando\s+(puedas|pueda[ns]?|se\s+pueda)|"
    r"lo\s+m[aá]s\s+(pronto|r[aá]pido|antes)|lo\s+antes\s+posible|"
    r"esta\s+semana|entre\s+semana|el\s+que\s+tengas|cualquier\s+(dia|hora|d[ií]a)|"
    r"any(time|thing)?|whenever|asap|the\s+soonest|the\s+earliest|"
    r"el\s+mas\s+(cercano|pronto|proximo)|la\s+mas\s+(cercana|pronta|proxima)|"
    r"el\s+primero\s+que\s+tengas|cuando\s+sea|works?\s+for\s+me|whatever\s+works)\b"
)

# "dale", "ok", "esta bien" DESPUES de mostrar horarios: el cliente esta
# aceptando, no eligiendo. Un vendedor humano anota el mas pronto y ofrece
# cambiarlo. Antes esto caia en no_intent y el cliente se quedaba esperando.
_AFIRMACION = re.compile(
    r"(?i)^\W*(s[ií]+|si+p|sip|dale|ok(ay|is)?|oka|va|vale|sale|listo|perfecto|"
    r"excelente|buenisimo|genial|de\s+acuerdo|est[aá]\s+bien|esta\s+perfecto|"
    r"as[ií]\s+est[aá]\s+bien|me\s+sirve|me\s+parece|claro(\s+que\s+s[ií])?|"
    r"por\s+supuesto|confirmado|confirmo|hecho|as[ií]\s+es|ese|esa|ese\s+mismo|"
    r"esa\s+misma|est[ae]|yes|yeah|yep|sure|great|perfect|sounds?\s+good|"
    r"that\s+works|it\s+works|good|fine|deal)"
    r"([\s,.!¡]+(por\s*favor|porfa(vor)?|please|gracias|thanks|amigo|se[nñ]or|"
    r"claro|bien|est[aá]\s+bien|sounds?\s+good|then|entonces))*\W*$"
)

# "el de arriba", "la de en medio", "el que sigue"
_POSICION = [
    (re.compile(r"(?i)\b(de\s+)?(en\s+medio|del\s+medio|intermedi[oa]|middle)\b"), "medio"),
    (re.compile(r"(?i)\b(de\s+)?arriba\b|\bel\s+de\s+hasta\s+arriba\b|\btop\b"), "primero"),
    (re.compile(r"(?i)\b(de\s+)?abajo\b|\bhasta\s+abajo\b|\bbottom\b"), "ultimo"),
    (re.compile(r"(?i)\b(el\s+)?que\s+sigue\b|\bel\s+siguiente\b|\bnext\s+one\b"), "segundo"),
]

_DIAS_ES = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4,
    "sabado": 5, "sabadito": 5, "domingo": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _norm(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    # "Lunes17" / "el10agosto" — la gente pega dia y numero
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip()


def ventanas_unicas(options: list[dict], limit: int = MAX_OPCIONES) -> list[dict]:
    """Ventanas sin repetir, en el mismo orden en que llegan de la API."""
    out: list[dict] = []
    seen: set = set()
    for o in options or []:
        k = (o.get("date"), o.get("startTime"), o.get("endTime"))
        if k in seen:
            continue
        seen.add(k)
        out.append(o)
        if len(out) >= limit:
            break
    return out


def lista_numerada(options: list[dict], limit: int = MAX_OPCIONES,
                   fmt=None) -> tuple[str, list[dict]]:
    """
    Devuelve (texto numerado, ventanas usadas).

    `fmt(slot) -> str` arma la etiqueta; por defecto usa slot['label'].
    """
    usadas = ventanas_unicas(options, limit=limit)
    lineas = []
    for i, o in enumerate(usadas, 1):
        etiqueta = fmt(o) if fmt else (o.get("label") or "")
        lineas.append(f"{i}) {etiqueta}")
    return "\n".join(lineas), usadas


def es_flexible(text: str | None) -> bool:
    """El cliente dice 'cualquiera' / 'lo mas pronto' / 'esta semana'."""
    t = _norm(text)
    if not t:
        return False
    return bool(_FLEXIBLE.search(t))


def opciones_mostradas(mensaje_bot: str | None, options: list[dict]) -> list[dict]:
    """
    Reconstruye la lista numerada EXACTA que vio el cliente.

    Se parsean las lineas "N) etiqueta" del ultimo mensaje del bot y se cruzan
    con las opciones reales por etiqueta. Asi el numero que escribe el cliente
    siempre apunta al horario que tenia enfrente, aunque el mensaje haya
    mostrado un subconjunto filtrado. Sin esto se corre el riesgo de anotar un
    horario distinto al que eligio, que es peor que no anotar nada.
    """
    if not (mensaje_bot and options):
        return []
    por_label = {}
    for o in options:
        lab = _norm(o.get("label"))
        if lab:
            por_label.setdefault(lab, o)

    encontrados: list[tuple[int, dict]] = []
    for m in re.finditer(r"^\s*(\d{1,2})\)\s*(.+?)\s*$", mensaje_bot, re.MULTILINE):
        n, etiqueta = int(m.group(1)), _norm(m.group(2))
        slot = por_label.get(etiqueta)
        if slot:
            encontrados.append((n, slot))
    if not encontrados:
        return []
    encontrados.sort(key=lambda x: x[0])
    # la numeracion debe ser 1..N corrida; si no, no nos fiamos
    if [n for n, _ in encontrados] != list(range(1, len(encontrados) + 1)):
        return []
    return [s for _, s in encontrados]


def parse_choice(text: str | None, options: list[dict],
                 limit: int = MAX_OPCIONES) -> dict | None:
    """
    Interpreta una eleccion por numero u ordinal sobre la lista numerada.

    Entiende "1", "el 2", "la 3", "#2", "opcion 2", "la primera", "el ultimo".
    Devuelve el slot elegido o None. Nunca adivina: si el numero no existe en
    la lista mostrada, devuelve None.
    """
    usadas = ventanas_unicas(options, limit=limit)
    if not usadas:
        return None
    t = _norm(text)
    if not t:
        return None

    # una fecha o una hora NO es una eleccion por numero ("8am", "3 de agosto")
    if re.search(r"\b\d{1,2}\s*(am|pm|:\d{2})", t) or re.search(r"\b\d{1,2}\s+de\s+\w+", t):
        return None

    idx = None

    # "opcion 2" / "numero 2" / "#2" / "la 2" / "el 2"
    m = re.search(r"(?:opcion|opci|numero|num|#|la|el|lo)\s*#?\s*(\d{1,2})\b", t)
    if m:
        idx = int(m.group(1))
    # mensaje que es SOLO un numero: "2", "2.", "2!"
    if idx is None:
        m = re.fullmatch(r"[\W_]*(\d{1,2})[\W_]*", t)
        if m:
            idx = int(m.group(1))
    # ordinales: "la primera", "el ultimo"
    if idx is None:
        for palabra, n in _ORDINALES.items():
            if re.search(rf"\b{palabra}\b", t):
                # "una" / "uno" solos son muy ambiguos en español
                if palabra in ("una", "uno") and not re.search(r"\b(la|el)\s+(una|uno)\b", t):
                    continue
                idx = n
                break

    if idx is None:
        return None
    if idx == -1:
        return usadas[-1]
    if 1 <= idx <= len(usadas):
        return usadas[idx - 1]
    return None


def es_afirmacion(text: str | None) -> bool:
    """
    'dale', 'ok', 'esta bien', 'ese', 'sounds good' — el cliente acepta.

    Solo tiene sentido cuando el bot acaba de mostrar horarios: ahi equivale a
    'el mas pronto que tengas'. Se exige que el mensaje sea SOLO la afirmacion
    (con cortesias), para no confundir 'ok pero cuanto cuesta' con un si.
    """
    t = _norm(text)
    if not t:
        return False
    return bool(_AFIRMACION.match(t))


def _hora_slot(o: dict) -> int:
    try:
        return int(str(o.get("startTime") or "12").split(":")[0])
    except Exception:
        return 12


def _dia_slot(o: dict):
    """(weekday, dia_del_mes, mes) del slot, o (None, None, None)."""
    import datetime as _dt
    try:
        d = _dt.date.fromisoformat(str(o.get("date") or "")[:10])
    except Exception:
        return None, None, None
    return d.weekday(), d.day, d.month


def _por_posicion(t: str, usadas: list[dict]) -> dict | None:
    for rx, donde in _POSICION:
        if rx.search(t):
            if donde == "medio":
                return usadas[len(usadas) // 2] if len(usadas) > 2 else None
            if donde == "primero":
                return usadas[0]
            if donde == "ultimo":
                return usadas[-1]
            if donde == "segundo":
                return usadas[1] if len(usadas) > 1 else None
    return None


def _por_fecha(t: str, usadas: list[dict]) -> dict | None:
    """'el 8', 'agosto 10', 'august 10', 'el 10 de agosto'."""
    mes = None
    for nombre, num in _MESES.items():
        if re.search(rf"\b{nombre}\b", t):
            mes = num
            break
    dias = re.findall(r"\b(\d{1,2})\b", t)
    for ds in dias:
        d = int(ds)
        if not 1 <= d <= 31:
            continue
        for o in usadas:
            _, dia, m = _dia_slot(o)
            if dia == d and (mes is None or m == mes):
                return o
    return None


def _por_dia_semana(t: str, usadas: list[dict]) -> dict | None:
    for nombre, wd in _DIAS_ES.items():
        if re.search(rf"\b{nombre}\b", t):
            for o in usadas:
                w, _, _ = _dia_slot(o)
                if w == wd:
                    return o
            return None
    return None


def _por_hora(t: str, usadas: list[dict]) -> dict | None:
    """'el de las 12', 'the one at 12', 'despues de las 3', 'al mediodia'."""
    objetivo = None
    if re.search(r"\b(medio\s*dia|mediodia|noon)\b", t):
        objetivo = 12
    if objetivo is None:
        m = re.search(r"\b(?:a\s+las?|de\s+las?|at|the\s+one\s+at|despues\s+de\s+las?|"
                      r"antes\s+de\s+las?|sobre\s+las?|como\s+a\s+las?)\s*(\d{1,2})\b", t)
        if m:
            objetivo = int(m.group(1))
    if objetivo is None:
        return None
    # 1-6 dichos a secas en horario comercial son PM ("a la 1" = 13:00)
    if 1 <= objetivo <= 6 and not re.search(r"\b(am|de\s+la\s+ma[nñ]ana)\b", t):
        objetivo += 12
    dentro = []
    for o in usadas:
        ini = _hora_slot(o)
        try:
            fin = int(str(o.get("endTime") or "17:00").split(":")[0])
        except Exception:
            fin = ini + 4
        if ini <= objetivo <= fin:
            dentro.append(o)
    if dentro:
        return dentro[0]
    return None


def _por_periodo(t: str, usadas: list[dict]) -> dict | None:
    if re.search(r"\b(temprano|early|en\s+la\s+ma[nñ]ana|por\s+la\s+ma[nñ]ana|"
                 r"in\s+the\s+morning|morning)\b", t):
        maniana = [o for o in usadas if _hora_slot(o) < 12]
        return maniana[0] if maniana else None
    if re.search(r"\b(tarde|afternoon|evening|despues\s+del\s+trabajo|"
                 r"after\s+work|saliendo\s+del\s+trabajo)\b", t):
        tarde = [o for o in usadas if _hora_slot(o) >= 12]
        return tarde[0] if tarde else None
    return None


def parse_natural(text: str | None, usadas: list[dict]) -> dict | None:
    """
    Elige un horario de la lista que el cliente TIENE DELANTE, con las palabras
    que realmente usa la gente: 'el de arriba', 'el sabado', 'el 10 de agosto',
    'el de las 12', 'en la tarde', 'monday works'.

    Trabaja solo sobre `usadas` (la lista mostrada y ya validada contra la
    disponibilidad real), asi que nunca puede devolver un hueco que el cliente
    no vio. Si nada encaja devuelve None y el flujo sigue como antes.
    """
    t = _norm(text)
    if not (t and usadas):
        return None
    # _por_hora va antes que _por_fecha: en "el de las 12" el 12 es la hora,
    # no el dia 12. El patron de hora exige "a las / de las / at", asi que
    # "el 10 de agosto" no se lo lleva por delante.
    for fn in (_por_posicion, _por_hora, _por_fecha, _por_dia_semana,
               _por_periodo, _por_tramo):
        slot = fn(t, usadas)
        if slot:
            return slot
    return None


def _por_tramo(t: str, usadas: list[dict]) -> dict | None:
    """'el finde', 'fin de semana', 'la proxima semana', 'entre semana'."""
    import datetime as _dt
    if re.search(r"\b(el\s+finde|fin(es)?\s+de\s+semana|weekend)\b", t):
        finde = [o for o in usadas if (_dia_slot(o)[0] or 0) >= 5]
        return finde[0] if finde else None
    if re.search(r"\b(la\s+)?(proxima|siguiente|que\s+viene|entrante|next)\s*(semana|week)\b", t) \
            or re.search(r"\bnext\s+week\b", t):
        hoy = _dt.date.today()
        # lunes de la semana que viene
        inicio = hoy + _dt.timedelta(days=(7 - hoy.weekday()))
        for o in usadas:
            try:
                d = _dt.date.fromisoformat(str(o.get("date") or "")[:10])
            except Exception:
                continue
            if d >= inicio:
                return o
        return None
    if re.search(r"\bentre\s+semana\b|\bweekday", t):
        habil = [o for o in usadas if (_dia_slot(o)[0] or 0) < 5]
        return habil[0] if habil else None
    return None
