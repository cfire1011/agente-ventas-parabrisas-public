"""
Normalizador de marca/modelo contra el catalogo real de Safelite.

Problema que resuelve: los clientes escriben el auto a mano ("Hinday Accent",
"Honda Civil", "CX--5", "Rav", "F550") y `safelite_vehicle` recibe ese texto
crudo, no encuentra el vehiculo y el lead muere con slots_error='vehicle_fail'.

Aca solo se resuelve la CAPA 1 (normalizacion + fuzzy contra el catalogo).
La desambiguacion conversacional (capa 2) usara la lista `candidates` que
devuelve `normalize_vehicle`.

Endpoints usados (mismos headers que safelite.py):
  GET /vehicle/api/v1/vehicle/Makes/{year}          -> ["Acura", "Audi", ...]
  GET /vehicle/api/v1/vehicle/Models/{year}/{make}  -> ["Accord", "CR-V", ...]
"""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from urllib.parse import quote

from safelite import SAFELITE, _sx

log = logging.getLogger("vehicle_norm")

# Confianza devuelta por normalize_vehicle
EXACT = "exact"
FUZZY = "fuzzy"
AMBIGUOUS = "ambiguous"
NONE = "none"

# Cuantos candidatos como maximo devolvemos para la capa 2
MAX_CANDIDATES = 8
# Umbral de difflib. Deliberadamente alto: preferimos 'ambiguous' antes que
# mandar al cliente a un vehiculo equivocado.
FUZZY_CUTOFF = 0.78


# ---------------------------------------------------------------------------
# Normalizacion de texto
# ---------------------------------------------------------------------------
def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def clean_text(text: str | None) -> str:
    """Minusculas, sin acentos, guiones/espacios repetidos colapsados."""
    s = _strip_accents(str(text or "")).lower()
    s = s.replace("_", " ")
    s = re.sub(r"-{2,}", "-", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -")


def key(text: str | None) -> str:
    """Clave de comparacion: solo alfanumericos. 'CX--5' y 'cx 5' -> 'cx5'."""
    return re.sub(r"[^a-z0-9]", "", clean_text(text))


_YEAR_SUFFIX = re.compile(r"(?:19|20)\d{2}$")


def strip_glued_year(text: str | None) -> str:
    """Quita un año pegado al final: 'Hodissey2011' -> 'Hodissey'."""
    s = str(text or "").strip()
    if not s:
        return ""
    out = _YEAR_SUFFIX.sub("", s).strip(" -_")
    return out or s


def fold_key(text: str | None) -> str:
    """
    Clave anti-typo hispano, para CUALQUIER modelo (no una lista de alias).
    i/y, b/v, s/z, letras dobles, H inicial de más.
    'hodissey' y 'odyssey' -> odisei; 'siena' y 'sienna' -> siena.
    """
    s = key(text)
    s = s.replace("y", "i").replace("v", "b").replace("z", "s")
    s = re.sub(r"(.)\1+", r"\1", s)
    if len(s) > 4 and s.startswith("h"):
        s = s[1:]
    return s


# ---------------------------------------------------------------------------
# Alias
# ---------------------------------------------------------------------------
# Marcas mal escritas por hispanohablantes. Clave = key() del texto del cliente.
# Valor = texto a buscar en el catalogo (puede ser lista de intentos).
MAKE_ALIASES: dict[str, str | list[str]] = {
    "hinday": "hyundai",
    "hunday": "hyundai",
    "hyunday": "hyundai",
    "hiunday": "hyundai",
    "jundai": "hyundai",
    "chevy": "chevrolet",
    "cheve": "chevrolet",
    "chevi": "chevrolet",
    "chevi": "chevrolet",
    "chebrolet": "chevrolet",
    "chevrolete": "chevrolet",
    "chevorlet": "chevrolet",
    "shevrolet": "chevrolet",
    "chhevrolet": "chevrolet",
    "chevroelt": "chevrolet",
    "mercedes": "mercedes benz",
    "mercedez": "mercedes benz",
    "mercedesbenz": "mercedes benz",
    "mercedezbenz": "mercedes benz",
    "merz": "mercedes benz",
    "benz": "mercedes benz",
    "vw": "volkswagen",
    "volkswagon": "volkswagen",
    "wolkswagen": "volkswagen",
    "bolsvaguen": "volkswagen",
    "toyot": "toyota",
    "tollota": "toyota",
    "honada": "honda",
    "jonda": "honda",
    "nisan": "nissan",
    "nissa": "nissan",
    "nission": "nissan",
    "totota": "toyota",
    "yundai": "hyundai",
    "polche": "porsche",
    "porche": "porsche",
    "gymsy": "gmc",
    "vosvawen": "volkswagen",
    "nisssan": "nissan",
    "mitsubichi": "mitsubishi",
    "mazada": "mazda",
    "masda": "mazda",
    "fordd": "ford",
    "dodgee": "dodge",
    "dodch": "dodge",
    "ram": ["ram", "dodge"],
    "gmc": "gmc",
    "jeap": "jeep",
    "yip": "jeep",
    "kya": "kia",
    "bmv": "bmw",
    "audy": "audi",
    "subarú": "subaru",
    "subaro": "subaru",
    "folsvagen": "volkswagen",
}

# Modelos mal escritos, validos para cualquier marca.
MODEL_ALIASES: dict[str, str | list[str]] = {
    "civil": "civic",
    "cibic": "civic",
    "sivic": "civic",
    # Roberto Costigliola: "Honda hodissey2011" — Odyssey mal escrito
    "hodissey": "odyssey",
    "odissey": "odyssey",
    "odisei": "odyssey",
    "odysey": "odyssey",
    "odisey": "odyssey",
    "oddysey": "odyssey",
    "hodysey": "odyssey",
    "odisea": "odyssey",
    "odysee": "odyssey",
    "cvr": "cr-v",
    "crv": "cr-v",
    "cverre": "cr-v",
    "hrv": "hr-v",
    "brv": "br-v",
    "cx": ["cx-", "cx"],
    "cx5": "cx-5",
    "cx7": "cx-7",
    "cx9": "cx-9",
    "cx3": "cx-3",
    "cx30": "cx-30",
    "mx5": "mx-5",
    "rav": "rav4",
    "rab4": "rav4",
    "rav 4": "rav4",
    "acent": "accent",
    "elentra": "elantra",
    "corola": "corolla",
    "korola": "corolla",
    "camri": "camry",
    "carry": "camry",
    "camari": "camry",
    "aldima": "altima",
    "oldina": "altima",
    "altina": "altima",
    "altime": "altima",
    "altma": "altima",
    "piloto": "pilot",
    "pilote": "pilot",
    "tacoma": ["tacoma", "tacoma pickup"],
    "tundra": ["tundra", "tundra pickup"],
    "ranger": ["ranger", "ranger pickup"],
    "caravan": ["caravan", "grand caravan"],
    "granecaravan": "grand caravan",
    "esplorer": "explorer",
    "esprs": "express",
    "espres": "express",
    "expres": "express",
    "expresvan": "express",
    "sentra": "sentra",
    "altima": "altima",
    "versa": "versa",
    "sonata": "sonata",
    "jeta": "jetta",
    "yeta": "jetta",
    "escap": "escape",
    "expedision": "expedition",
    "sierra": ["sierra", "sierra pickup"],
    "silberado": "silverado",
    "silvrado": "silverado",
    "silverdo": "silverado",
    "silverware": "silverado",
    "hodissey": "odyssey",
    "venda": "venza",
    "haylander": "highlander",
    "gladiador": "gladiator",
    "winstar": "windstar",
    "rouge": "rogue",
    "4runer": "4runner",
    "econolen": "econoline",
    "escarabajo": "beetle",
    "cierra": "sierra",
    "escaley": "escalade",
    "caley": "escalade",
    "pickap": "pickup",
    "superduty": "f series f250",
    "kingranch": "f series f250",
    "voyager": "grand caravan",
}

# Series de camionetas: 'f550' -> 'F Series F550', '3500' -> 'Pickup 3500'.
_FORD_F_SERIES = re.compile(r"^f\s*-?\s*(1[05]0|[2-9]50)$")
# Vans Ford E-Series: 'e150'/'e-250'/'e 350' -> Econoline (el catalogo Safelite
# no lista "E150" tal cual, solo "Econoline" para toda la familia). Bug real:
# sin esto, difflib comparaba "e150" contra "F Series F150/F250/..." y
# devolvia ESOS como candidatos ("¿Cual de estos es tu Ford? F150, F750...")
# para un cliente que ya habia dicho claramente que su auto es una VAN E150.
_FORD_E_SERIES = re.compile(r"^e\s*-?\s*(1[05]0|[2-9]50)$")

# Alias de modelo dependientes de la marca (key(make) -> {key(model): intentos}).
MODEL_ALIASES_BY_MAKE: dict[str, dict[str, str | list[str]]] = {
    "ford": {
        "king": ["f series f250", "f series f350", "f series f150"],
        "kingranch": ["f series f250", "f series f350", "f series f150"],
        "superduty": ["f series f250", "f series f350"],
        "f250": "f series f250",
        "f350": "f series f350",
    },
    "chevrolet": {
        # Silverado no existe antes de 1999: el catalogo usa Pickup C/K 1500...
        "silverado": ["silverado", "pickup c", "pickup k"],
        "cheyenne": ["silverado", "pickup c", "pickup k"],
    },
    "gmc": {
        "sierra": ["sierra", "pickup c", "pickup k"],
    },
    "dodge": {
        "ram": ["pickup", "ram"],
        "ram1500": "pickup 1500",
        "ram2500": "pickup 2500",
        "ram3500": "pickup 3500",
        "long": "pickup 1500",
        "longhorn": "pickup 1500",
        "lonestar": "pickup 1500",
        "laramie": "pickup 1500",
        "bighorn": "pickup 1500",
        "rebel": "pickup 1500",
        "tradesman": "pickup 1500",
    },
    "ram": {
        # Trims que el cliente dice como si fueran modelo (Barni: long stat)
        "long": "pickup 1500",
        "longhorn": "pickup 1500",
        "lonestar": "pickup 1500",
        "longstat": "pickup 1500",
        "longstar": "pickup 1500",
        "laramie": "pickup 1500",
        "bighorn": "pickup 1500",
        "bighorn1500": "pickup 1500",
        "rebel": "pickup 1500",
        "tradesman": "pickup 1500",
        "classic": "pickup 1500",
        "limited": "pickup 1500",
        "warlock": "pickup 1500",
        "night": "pickup 1500",
        "sport": "pickup 1500",
        "slt": "pickup 1500",
        "st": "pickup 1500",
        "express": "pickup 1500",
        "hemi": "pickup 1500",
        "crew": "pickup 1500",
        "crewcab": "pickup 1500",
        "quad": "pickup 1500",
        "quadcab": "pickup 1500",
        "1500": "pickup 1500",
        "2500": "pickup 2500",
        "3500": "pickup 3500",
        "ram": "pickup 1500",
    },
    "mercedes benz": {
        "cclass": ["c300", "c-class", "c class"],
        "cclassc300": "c300",
        "c300": "c300",
        "c250": "c250",
        "eclass": ["e350", "e-class"],
        "eclassc300": "e300",
    },
}


def _alias_attempts(raw: str, table: dict[str, str | list[str]]) -> list[str]:
    val = table.get(key(raw))
    if val is None:
        return []
    return [val] if isinstance(val, str) else list(val)


def _model_alias_attempts(make: str, model: str) -> list[str]:
    attempts: list[str] = []
    by_make = MODEL_ALIASES_BY_MAKE.get(key(make), {})
    attempts += _alias_attempts(model, by_make)
    attempts += _alias_attempts(model, MODEL_ALIASES)
    try:
        from .memory import get_alias
        learned = get_alias(make, model)
        if learned:
            attempts.append(learned)
    except Exception:
        pass
    try:
        from .aprender import alias_attempts as _learned
        attempts += _learned(make, model)
    except Exception:
        pass
    k = key(model)
    m = _FORD_F_SERIES.match(clean_text(model).replace("-", ""))
    if m:
        attempts.append(f"f series f{m.group(1)}")
        attempts.append(f"f-{m.group(1)}")
    if _FORD_E_SERIES.match(clean_text(model).replace("-", "")):
        attempts.append("econoline")
    if re.fullmatch(r"[1-9][05]00", k):
        attempts.append(f"pickup {k}")
    # dedup preservando orden
    return list(dict.fromkeys(a for a in attempts if a))


# ---------------------------------------------------------------------------
# Catalogo Safelite (cacheado en memoria)
# ---------------------------------------------------------------------------
_makes_cache: dict[str, list[str]] = {}
_models_cache: dict[tuple[str, str], list[str]] = {}


def _get_list(url: str) -> list[str]:
    with _sx(timeout=20) as c:
        r = c.get(url)
        if r.status_code != 200:
            return []
        data = r.json() or []
    out: list[str] = []
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            v = item.get("make") or item.get("model") or item.get("name")
            if v:
                out.append(str(v))
    return out


def safelite_makes(year: str | int) -> list[str]:
    y = str(year).strip()
    if not re.fullmatch(r"\d{4}", y):
        return []
    if y not in _makes_cache:
        try:
            _makes_cache[y] = _get_list(f"{SAFELITE}/vehicle/api/v1/vehicle/Makes/{y}")
        except Exception:
            log.exception("safelite_makes %s", y)
            return []
    return _makes_cache[y]


def safelite_models(year: str | int, make: str) -> list[str]:
    y, mk = str(year).strip(), (make or "").strip()
    if not (re.fullmatch(r"\d{4}", y) and mk):
        return []
    ck = (y, mk.lower())
    if ck not in _models_cache:
        try:
            _models_cache[ck] = _get_list(
                f"{SAFELITE}/vehicle/api/v1/vehicle/Models/{y}/{quote(mk)}"
            )
        except Exception:
            log.exception("safelite_models %s %s", y, mk)
            return []
    return _models_cache[ck]


def clear_catalog_cache() -> None:
    _makes_cache.clear()
    _models_cache.clear()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _match(raw: str, catalog: list[str], attempts: list[str]) -> tuple[str | None, str, list[str]]:
    """
    Devuelve (valor_catalogo | None, confianza, candidatos).

    Cascada: exacto -> alias exacto -> contencion de substring -> difflib.
    El exacto va PRIMERO para que un alias no reescriba un valor que ya existe
    en el catalogo (ej: Dodge 'Caravan' si existe tal cual en 1998).
    """
    if not catalog:
        return None, NONE, []

    by_key: dict[str, str] = {}
    for c in catalog:
        by_key.setdefault(key(c), c)

    k = key(raw)
    if not k:
        return None, NONE, []
    probes = [k]
    stripped = key(strip_glued_year(raw))
    if stripped and stripped != k:
        probes.append(stripped)
    probes = list(dict.fromkeys(p for p in probes if p))

    for probe in probes:
        if probe in by_key:
            return by_key[probe], EXACT, [by_key[probe]]

    for a in attempts:
        ak = key(a)
        if ak and ak in by_key:
            return by_key[ak], EXACT, [by_key[ak]]

    # typos hispanos contra el catalogo entero (cualquier marca/modelo)
    by_fold: dict[str, list[str]] = {}
    for orig in by_key.values():
        fk = fold_key(orig)
        if len(fk) >= 4:
            by_fold.setdefault(fk, []).append(orig)
    fold_hits: list[str] = []
    for probe in probes + [key(a) for a in attempts]:
        fk = fold_key(probe)
        if len(fk) < 4:
            continue
        fold_hits += by_fold.get(fk) or []
    fold_hits = list(dict.fromkeys(fold_hits))
    if len(fold_hits) == 1:
        return fold_hits[0], FUZZY, fold_hits
    if len(fold_hits) > 1:
        return None, AMBIGUOUS, fold_hits[:MAX_CANDIDATES]

    # contencion sobre el texto crudo: 'tundra' -> 'Tundra Pickup',
    # 'f550' -> 'F Series F550', 'silverado' -> las 12 variantes (ambiguo)
    hits = list(dict.fromkeys(
        orig for ck, orig in by_key.items()
        if any(p in ck or ck in p for p in probes)
    ))
    if len(hits) == 1:
        return hits[0], FUZZY, hits
    if len(hits) > 1:
        return None, AMBIGUOUS, hits[:MAX_CANDIDATES]

    # union de contenciones de todos los intentos (ej: silverado -> pickup c/k)
    union: list[str] = []
    for probe in [key(a) for a in attempts]:
        if not probe:
            continue
        union += [orig for ck, orig in by_key.items() if probe in ck]
    union = list(dict.fromkeys(union))
    if len(union) == 1:
        return union[0], FUZZY, union
    if len(union) > 1:
        return None, AMBIGUOUS, union[:MAX_CANDIDATES]

    keys = list(by_key.keys())
    for probe in probes + [key(a) for a in attempts]:
        if not probe:
            continue
        close = difflib.get_close_matches(probe, keys, n=MAX_CANDIDATES, cutoff=FUZZY_CUTOFF)
        if len(close) == 1:
            return by_key[close[0]], FUZZY, [by_key[close[0]]]
        if len(close) > 1:
            return None, AMBIGUOUS, [by_key[c] for c in close]

    # nada: devolvemos lo mas parecido posible para la capa 2
    loose: list[str] = []
    for probe in probes:
        loose += difflib.get_close_matches(probe, keys, n=MAX_CANDIDATES, cutoff=0.45)
    loose = list(dict.fromkeys(loose))
    return None, NONE, [by_key[c] for c in loose]


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------
def normalize_vehicle(year: str | int, make: str, model: str) -> dict:
    """
    Normaliza marca/modelo contra el catalogo real de Safelite para ese anio.

    Devuelve:
      {'make': str|None, 'model': str|None,
       'candidates': [str, ...],          # modelos posibles (capa 2)
       'confidence': 'exact'|'fuzzy'|'ambiguous'|'none',
       'make_confidence': ..., 'make_candidates': [...]}

    'make'/'model' vienen con la grafia EXACTA del catalogo cuando se resuelven.
    """
    result = {
        "make": None,
        "model": None,
        "candidates": [],
        "confidence": NONE,
        "make_confidence": NONE,
        "make_candidates": [],
    }
    y = str(year or "").strip()
    if not re.fullmatch(r"\d{4}", y):
        return result

    makes = safelite_makes(y)
    mk, mk_conf, mk_cands = _match(make, makes, _alias_attempts(make, MAKE_ALIASES))
    result["make_confidence"] = mk_conf
    result["make_candidates"] = mk_cands
    if not mk:
        result["confidence"] = mk_conf if mk_conf == AMBIGUOUS else NONE
        return result
    result["make"] = mk

    models = safelite_models(y, mk)
    model_clean = strip_glued_year(model) or model
    attempts = _model_alias_attempts(mk, model_clean)
    if model_clean != (model or ""):
        attempts = list(dict.fromkeys(attempts + _model_alias_attempts(mk, model)))
    md, md_conf, md_cands = _match(model_clean or model, models, attempts)
    result["model"] = md
    result["candidates"] = md_cands
    if md and mk_conf == FUZZY and md_conf == EXACT:
        md_conf = FUZZY
    result["confidence"] = md_conf
    return result
