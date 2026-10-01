"""Typos y habla real de los chats de Messenger. Se aplica ANTES de NLU."""
from __future__ import annotations

import re

# Frases vistas en leads.db. Palabra completa, no adentro de otras.
_FIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.I), r)
    for p, r in (
        (r"\bgrasias\b", "gracias"),
        (r"\btardws\b", "tardes"),
        (r"\bkiero\b", "quiero"),
        (r"\bkeria\b", "queria"),
        (r"\bsikeria\b", "si queria"),
        (r"\bpresio\b", "precio"),
        (r"\borijinal\b", "original"),
        (r"\bne[cs]e[cs]ito\b", "necesito"),
        (r"\bnecexito\b", "necesito"),
        (r"\bcheve\b", "chevy"),
        (r"\bchevi\b", "chevy"),
        (r"\bchevi\s*silberado\b", "chevy silverado"),
        (r"\bchevy\s*silberado\b", "chevy silverado"),
        (r"\bchevi\s*silverado\b", "chevy silverado"),
        (r"\boy\b", "hoy"),
        (r"\bbenir\b", "venir"),
        (r"\bpuda\b", "pueda"),
        (r"\byamar\b", "llamar"),
        (r"\byegaron\b", "llegaron"),
        (r"\besteve\b", "estuve"),
        (r"\bcanbiar\b", "cambiar"),
        (r"\badomisilio\b", "a domicilio"),
        (r"\bbodrio\b", "vidrio"),
        (r"\bpara\s*brisas\b", "parabrisas"),
        (r"\bparabriza\b", "parabrisas"),
        (r"\bparabrisa\b", "parabrisas"),
        (r"\bcotizasion\b", "cotizacion"),
        (r"\bonta\b", "donde esta"),
        (r"\beltaller\b", "el taller"),
        (r"\benqie\b", "en que"),
        (r"\b7vicados\b", "ubicados"),
        (r"\baldima\b", "altima"),
        (r"\boldina\b", "altima"),
        (r"\bnission\b", "nissan"),
        (r"\bsilberado\b", "silverado"),
        (r"\bsilverware\b", "silverado"),
        (r"\bvid[eio]o\s+de\s+[aà]?\s*delante\b", "vidrio de adelante"),
        (r"\bhodissey\b", "odyssey"),
        (r"\bhaylander\b", "highlander"),
        (r"\bpolche\b", "porsche"),
        (r"\bgymsy\b", "gmc"),
        (r"\bcierra\b", "sierra"),
        (r"\bescaley\b", "escalade"),
        (r"\bcaley\b", "escalade"),
        (r"\b4runer\b", "4runner"),
        (r"\beconolen\b", "econoline"),
        (r"\bwinstar\b", "windstar"),
        (r"\brouge\b", "rogue"),
        (r"\bvenda\b", "venza"),
        (r"\btotota\b", "toyota"),
        (r"\byundai\b", "hyundai"),
        (r"\bvosvawen\b", "volkswagen"),
        (r"\bescarabajo\b", "beetle"),
        (r"\bcuan\s+cuesta\b", "cuanto cuesta"),
        (r"\bcon\s+migo\b", "conmigo"),
    )
]


def fix_chat_typos(text: str) -> str:
    t = text or ""
    for rx, repl in _FIXES:
        t = rx.sub(repl, t)
    return t
