"""
Extraccion robusta de codigo postal desde el texto del cliente.

Problema que resuelve: 17 de 68 leads dieron el auto pero NUNCA quedo grabado
el zip, asi que jamas vieron horarios. El extractor viejo era
`re.search(r"\\b(\\d{5})\\b")` y ademas solo se ejecutaba cuando el mensaje se
clasificaba como "direccion", de modo que se perdian casos muy comunes:

    "29349."                  -> puntuacion pegada
    "29349-1234"              -> ZIP+4
    "2 9 3 4 9"               -> digitos separados
    "2015 Toyota Tundra 29349"-> venia junto con el auto y se descartaba
    "vivo en Gaffney SC"      -> ciudad/estado, sin zip

Aca solo se extrae/resuelve el zip. No se decide nada del flujo.
"""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from urllib.parse import quote

log = logging.getLogger("zip_norm")

# Rango real de ZIPs USA (00501 Holtsville NY es el mas bajo en uso).
_ZIP_MIN = 500
_ZIP_MAX = 99950

# Un año (1900-2099) nunca es un zip, y un telefono tampoco.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# Estados USA: abreviatura -> nombre (para armar la query de geocoding).
US_STATES: dict[str, str] = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho",
    "il": "Illinois", "in": "Indiana", "ia": "Iowa", "ks": "Kansas",
    "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska",
    "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "ny": "New York", "nc": "North Carolina",
    "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma", "or": "Oregon",
    "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah",
    "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
    "dc": "District of Columbia",
}

# Como los hispanohablantes nombran los estados donde opera el negocio.
_STATE_ES = {
    "carolina del sur": "sc",
    "carolina del norte": "nc",
    "georgia": "ga",
    "tennessee": "tn",
    "tenesi": "tn",
    "florida": "fl",
    "texas": "tx",
    "tejas": "tx",
    "virginia": "va",
    "nueva york": "ny",
    "california": "ca",
    "carolina sur": "sc",
    "carolina norte": "nc",
}

# Palabras que indican que el numero NO es un zip.
# Palabras que nunca son una ciudad (evita "todo ok" -> Todo, Oklahoma).
# Typos reales de ciudad (clave = letras pegadas, minusculas).
_CITY_ALIASES = {
    "frederikvurd": "Fredericksburg",
    "frederikburg": "Fredericksburg",
    "fredricksburg": "Fredericksburg",
    "fredericksburg": "Fredericksburg",
    "fredriksburg": "Fredericksburg",
    "loisville": "Louisville",
    "luisville": "Louisville",
    "alburquerque": "Albuquerque",
    "albukerke": "Albuquerque",
    "kennewick": "Kennewick",
    "dubuque": "Dubuque",
    "hasston": "Houston",
    "juston": "Houston",
    "dalas": "Dallas",
    "sanantono": "San Antonio",
    "sanantoni": "San Antonio",
    "shikago": "Chicago",
    "filadelfia": "Philadelphia",
    "feladelfia": "Philadelphia",
}

# Ciudades frecuentes para fuzzy (typos que no estan en el alias).
_COMMON_CITIES = [
    "Houston", "Dallas", "Austin", "San Antonio", "Fort Worth", "El Paso",
    "Fredericksburg", "Lubbock", "Amarillo", "Corpus Christi", "Laredo",
    "Albuquerque", "Santa Fe", "Las Cruces",
    "Phoenix", "Tucson", "Las Vegas",
    "Los Angeles", "San Diego", "San Jose", "Fresno", "Sacramento",
    "Garden Grove", "Hemet", "Calexico", "Riverside", "Bakersfield",
    "Chicago", "Indianapolis", "Louisville", "Nashville", "Memphis",
    "Knoxville", "Atlanta", "Charlotte", "Raleigh", "Miami", "Orlando",
    "Tampa", "Jacksonville", "New York", "Brooklyn", "Philadelphia",
    "Baltimore", "Washington", "Richmond", "Norfolk",
    "Denver", "Colorado Springs", "Oklahoma City", "Tulsa",
    "Kansas City", "St Louis", "Minneapolis", "Detroit", "Cleveland",
    "Columbus", "Cincinnati", "Pittsburgh", "Boston", "Seattle", "Portland",
    "Glen Burnie", "Inman", "Gaffney", "Spartanburg", "Greenville",
    "Kennewick", "Dubuque", "Iowa City",
]


def _fuzzy_city(raw: str, state: str = "") -> str:
    """Corrige 'frederikvurd' → Fredericksburg sin depender de un alias exacto."""
    compact = re.sub(r"\s+", "", (raw or "").lower())
    if not compact or len(compact) < 4:
        return (raw or "").title()
    if compact in _CITY_ALIASES:
        return _CITY_ALIASES[compact]
    pool = _COMMON_CITIES
    close = difflib.get_close_matches(raw.title(), pool, n=1, cutoff=0.70)
    if close:
        return close[0]
    close = difflib.get_close_matches(compact, [re.sub(r"\s+", "", c.lower()) for c in pool], n=1, cutoff=0.70)
    if close:
        for c in pool:
            if re.sub(r"\s+", "", c.lower()) == close[0]:
                return c
    return (raw or "").title()


_CITY_STOPWORDS = {
    "todo", "esta", "este", "esto", "eso", "muy", "bien", "mal", "gracias",
    "hola", "buenas", "bueno", "buena", "claro", "listo", "dale", "vale",
    "perfecto", "ahora", "aqui", "alla", "casa", "trabajo", "carro", "auto",
    "que", "para", "porque", "cuando", "donde", "como", "pero", "tambien",
    "thanks", "thank", "yes", "yeah", "okay", "sure", "home", "work", "car",
}

_NOT_ZIP_CTX = re.compile(
    r"(?i)(\$|d[oó]lar|dolares|pesos|precio|cuesta|cobran|millas|miles|"
    r"tel[eé]fono|celular|whatsapp|phone)"
)


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", _strip_accents(str(text or "")).lower()).strip()


def _valid_zip(z: str) -> bool:
    if not re.fullmatch(r"\d{5}", z or ""):
        return False
    if _YEAR_RE.match(z):
        return False
    return _ZIP_MIN <= int(z) <= _ZIP_MAX


def extract_zip(text: str | None) -> str:
    """
    Devuelve el zip de 5 digitos encontrado en el texto, o "".

    Tolera puntuacion pegada, ZIP+4 y digitos separados por espacios.
    Rechaza años, telefonos y numeros con contexto de dinero.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""

    t = _norm(raw)

    # ZIP+4: 29349-1234 -> 29349
    m = re.search(r"(?<!\d)(\d{5})-\d{4}(?!\d)", t)
    if m and _valid_zip(m.group(1)):
        return m.group(1)

    # caso normal: 5 digitos aislados por cualquier separador no numerico
    digits_only = re.sub(r"\D", "", t)
    phoneish = len(digits_only) in (10, 11)
    for cand in re.findall(r"(?<!\d)(\d{5})(?!\d)", t):
        if not _valid_zip(cand):
            continue
        # descartar si el numero viene rodeado de contexto de dinero/telefono
        i = t.find(cand)
        ctx = t[max(0, i - 25): i + len(cand) + 15]
        if _NOT_ZIP_CTX.search(ctx):
            continue
        # a 10-digit phone number is not a ZIP
        if phoneish and cand in digits_only and len(t.strip()) <= 16:
            continue
        return cand

    # digitos separados: "2 9 3 4 9" / "2-9-3-4-9" / "29 349"
    m = re.search(r"(?<!\d)(\d[\s\-]){4}\d(?!\d)", t)
    if m:
        cand = re.sub(r"\D", "", m.group(0))
        if _valid_zip(cand):
            return cand
    m = re.search(r"(?<!\d)(\d[\d\s\-]{3,8}\d)(?!\d)", t)
    if m:
        cand = re.sub(r"\D", "", m.group(1))
        if len(cand) == 5 and _valid_zip(cand) and not _NOT_ZIP_CTX.search(t):
            return cand

    return ""


def extract_city_state(text: str | None) -> tuple[str, str]:
    """
    Detecta 'ciudad, estado' en el mensaje. Devuelve (ciudad, abrev_estado).

    Reconoce 'Gaffney SC', 'vivo en charlotte nc', 'Spartanburg, Carolina del Sur'.
    Si no encuentra estado devuelve ("", "").
    """
    t = _norm(text)
    if not t:
        return "", ""

    # quitar muletillas de ubicacion para quedarnos con la ciudad
    t = re.sub(
        r"^(yo\s+)?(vivo|estoy|resido|soy|me\s+queda|queda|es)\s+"
        r"(en|por|cerca\s+de|de)?\s*",
        "",
        t,
    )
    t = t.replace(",", " , ")

    state = ""
    # estado escrito en español / nombre completo
    for name, abbr in _STATE_ES.items():
        if re.search(rf"\b{re.escape(name)}\b", t):
            state = abbr
            t = re.sub(rf"\b{re.escape(name)}\b", " ", t)
            break
    if not state:
        for abbr, full in US_STATES.items():
            if re.search(rf"\b{re.escape(full.lower())}\b", t):
                state = abbr
                t = re.sub(rf"\b{re.escape(full.lower())}\b", " ", t)
                break
    if not state:
        # 'gaffney sc' — abreviatura al final. Solo en mensajes cortos, para no
        # leer "todo ok" como Todo, Oklahoma.
        cleaned = t.replace(",", " ").strip()
        if len(cleaned.split()) <= 4:
            m = re.search(r"\b([a-z]{2})\b\s*$", cleaned)
            if m and m.group(1) in US_STATES:
                state = m.group(1)
                t = t[: m.start()]

    if not state:
        # Bug real (Nestor Osorio): "Si pero vivo en Boise no se si tiene
        # servicio aca mi parabrisas..." nunca resolvia. La version vieja
        # solo reconocia un puñado fijo de ciudades ("Soy Kennewick") y
        # exigia que el mensaje fuera EXACTAMENTE ese nombre; cualquier
        # palabra extra despues de la ciudad (lo normal en una conversacion
        # real) hacia fallar la comparacion por completo.
        #
        # Estrategia nueva: tomar las palabras iniciales del texto (ya sin
        # el "vivo/estoy en" al frente si estaba al inicio) hasta toparse
        # con la primera que no parezca parte de un nombre de ciudad (muy
        # corta o en la lista de stopwords). Eso aisla "Boise" o "Denver"
        # del resto de la oracion ("no se si tiene servicio..."). Se
        # devuelve sin estado (source="") para que resolve_zip() intente
        # geocoding real (Census/Nominatim), que cubre los 50 estados y no
        # solo el puñado de ciudades de un diccionario fijo.
        blob = re.sub(r"[^a-z\s]", " ", t)
        blob = re.sub(r"\s+", " ", blob).strip()
        words = blob.split()
        lead: list[str] = []
        for w in words:
            if len(w) < 3 or w in _CITY_STOPWORDS:
                break
            lead.append(w)
            if len(lead) == 2:
                break
        candidate = " ".join(lead)
        compact = "".join(lead)

        default_state = {
            "kennewick": "wa",
            "dubuque": "ia",
            "gaffney": "sc",
            "spartanburg": "sc",
            "albuquerque": "nm",
            "alburquerque": "nm",
        }
        if compact in default_state:
            return _fuzzy_city(candidate), default_state[compact]
        if lead and lead[0] in default_state:
            return _fuzzy_city(lead[0]), default_state[lead[0]]
        if candidate:
            return _fuzzy_city(candidate), ""

        # El "vivo en" no estaba al inicio (ej: "Si pero vivo en Boise...")
        # asi que el strip anclado de arriba nunca lo quito y `lead` salio
        # vacio. Se busca el patron en cualquier parte del mensaje.
        m_en = re.search(
            r"\b(?:vivo|estoy|resido|soy|me\s+queda|queda)\s+en\s+([a-z]+)\b",
            t,
        )
        if m_en:
            candidate2 = m_en.group(1)
            if len(candidate2) >= 3 and candidate2 not in _CITY_STOPWORDS:
                return _fuzzy_city(candidate2), ""
        return "", ""

    city = re.sub(r"[^a-z\s]", " ", t)
    city = re.sub(r"\s+", " ", city).strip()
    # descartar restos de chat
    city = re.sub(
        r"^(hola|buenas|buenos dias|gracias|ok|si|sí|no|mi|el|la|zip|codigo postal)\s+",
        "",
        city,
    ).strip()
    # "ford 2016 f 150 en frederikvurd texas" → quedarse con lo que va tras "en"
    m_en = re.search(r"\ben\s+([a-z][a-z\s]{1,40})$", city)
    if m_en:
        city = m_en.group(1).strip()
    city = _fuzzy_city(city, state)
    if len(city) < 3 or city.lower() in _CITY_STOPWORDS:
        return "", state
    return city, state


def _zips_de_ciudad(city: str, state: str) -> list[str]:
    """
    Zips de una ciudad USA via api.zippopotam.us (gratis, sin key).

    Nominatim devuelve el centroide sin codigo postal para ciudades grandes
    ("Charlotte NC" -> sin zip), asi que este es el resolver primario.
    """
    if not (city and state):
        return []
    import httpx

    url = f"https://api.zippopotam.us/us/{state.lower()}/{quote(city.lower())}"
    try:
        with httpx.Client(timeout=12, trust_env=False) as c:
            r = c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return []
        data = r.json() or {}
    except Exception:
        log.exception("zippopotam %r", url)
        return []
    out = []
    for place in data.get("places") or []:
        z = str(place.get("post code") or "").strip()
        if _valid_zip(z):
            out.append(z)
    return out


def resolve_zip(text: str | None, geocoder=None, validator=None, city_lookup=None) -> dict:
    """
    Resuelve el zip del mensaje.

    1) zip explicito en el texto.
    2) si no hay, 'ciudad estado' -> catalogo de zips de la ciudad -> geocoder.

    El zip por ciudad es APROXIMADO (source='city'): sirve para poder mostrar
    horarios de la zona; la direccion exacta se confirma despues.

    `geocoder`, `validator` (safelite_zip) y `city_lookup` se inyectan para
    poder testear sin red.

    Devuelve {'zip','source','city','state'} con source en 'text'|'city'|''.
    """
    out = {"zip": "", "source": "", "city": "", "state": ""}

    z = extract_zip(text)
    if z:
        out.update(zip=z, source="text")
        return out

    city, state = extract_city_state(text)
    out["city"], out["state"] = city, state
    if not city:
        return out

    def _ok(candidate: str) -> bool:
        if not candidate:
            return False
        if validator is None:
            return True
        try:
            return bool(validator(candidate))
        except Exception:
            log.exception("resolve_zip validate %s", candidate)
            return False

    if not state:
        # Ciudad reconocida pero sin estado explicito (ej: "vivo en Boise",
        # sin "ID"). El catalogo `_zips_de_ciudad` necesita el estado, asi
        # que se salta directo al geocoder real (Census/Nominatim), que sí
        # puede resolver la ciudad sola en cualquiera de los 50 estados.
        if geocoder is None:
            from .legacy import geocode_address as geocoder  # import tardio: evita ciclo

        query = f"{city}, USA"
        try:
            hit = geocoder(query) or {}
        except Exception:
            log.exception("resolve_zip geocode (sin estado) %r", query)
            return out

        z = extract_zip(hit.get("zip") or "")
        if z and _ok(z):
            out.update(zip=z, source="city")
            hit_state = str(hit.get("state") or "").strip().lower()
            if hit_state and hit_state in US_STATES:
                out["state"] = hit_state
            log.info("resolve_zip por geocoder (sin estado) %r -> %s", query, z)
        return out

    lookup = city_lookup if city_lookup is not None else _zips_de_ciudad
    try:
        candidates = lookup(city, state) or []
    except Exception:
        log.exception("resolve_zip city_lookup %s %s", city, state)
        candidates = []

    for cand in candidates:
        if _ok(cand):
            out.update(zip=cand, source="city")
            log.info("resolve_zip por ciudad %s %s -> %s", city, state, cand)
            return out

    # respaldo: geocoding libre (tambien con el typo crudo + estado)
    if geocoder is None:
        from .legacy import geocode_address as geocoder  # import tardio: evita ciclo

    query = f"{city}, {US_STATES.get(state, state).title()}, USA"
    try:
        hit = geocoder(query) or {}
    except Exception:
        log.exception("resolve_zip geocode %r", query)
        return out

    z = extract_zip(hit.get("zip") or "")
    if z and _ok(z):
        out.update(zip=z, source="city")
        log.info("resolve_zip por geocoder %r -> %s", query, z)
    return out
