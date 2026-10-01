"""
Desambiguacion de modelo: preguntar en vez de mandar al humano.

Cuando el cliente dice "Silverado" o "CX", el catalogo de Safelite tiene varias
variantes reales (Silverado C1500 / C2500 / K1500..., CX-5 / CX-7 / CX-9) y no
se puede adivinar cual es: elegir mal significa cotizar el vidrio equivocado.
Hasta ahora eso terminaba en handoff y el cliente nunca veia horarios.

Aca se arma una pregunta corta y numerada con los candidatos REALES del
catalogo, y se interpreta la respuesta (por numero o por nombre). Igual que en
los horarios, la respuesta se ancla al mensaje que el bot mando, para que el
"2" del cliente apunte a lo que tenia enfrente.
"""
from __future__ import annotations

import re
import unicodedata

MAX_CANDIDATOS = 8

_MARCA_PREGUNTA = "¿Cuál de estos es tu {make}?"
_MARCADOR = re.compile(r"(?i)cu[aá]l\s+de\s+estos\s+es\s+tu")


def _norm(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def pregunta(make: str, candidatos: list[str], limit: int = MAX_CANDIDATOS) -> str | None:
    """Mensaje numerado con las variantes reales del catalogo."""
    opts = [c for c in dict.fromkeys(candidatos or []) if c][:limit]
    if len(opts) < 2:
        return None
    lineas = "\n".join(f"{i}) {c}" for i, c in enumerate(opts, 1))
    return (
        f"{_MARCA_PREGUNTA.format(make=make)}\n{lineas}\n\n"
        f"Respóndeme con el número de la lista (1, 2, 3…) y te paso los horarios."
    )


def candidatos_mostrados(mensaje_bot: str | None) -> list[str]:
    """
    Reconstruye la lista numerada que vio el cliente.

    Se exige el marcador de la pregunta de modelo: la lista de horarios
    tambien va numerada y sin esto un "2" del cliente se guardaria como
    modelo del auto.
    """
    if not mensaje_bot:
        return []
    if not _MARCADOR.search(mensaje_bot):
        return []
    encontrados: list[tuple[int, str]] = []
    for m in re.finditer(r"^\s*(\d{1,2})\)\s*(.+?)\s*$", mensaje_bot, re.MULTILINE):
        encontrados.append((int(m.group(1)), m.group(2).strip()))
    if not encontrados:
        return []
    encontrados.sort(key=lambda x: x[0])
    if [n for n, _ in encontrados] != list(range(1, len(encontrados) + 1)):
        return []
    return [c for _, c in encontrados]


def parse_respuesta(text: str | None, candidatos: list[str]) -> str | None:
    """
    Interpreta la eleccion del cliente: por numero ("2", "el 2", "opcion 2")
    o por nombre ("cx 9", "silverado c2500"). Devuelve el candidato o None.

    Ante la duda devuelve None y el bot vuelve a preguntar: cotizar el vidrio
    del modelo equivocado es peor que preguntar otra vez.
    """
    if not candidatos:
        return None
    t = _norm(text)
    if not t:
        return None

    # 1) cualquier índice de la lista que mandamos: 1, 2, 3, 4, 5…
    #    "1500" (4 dígitos) es nombre de modelo, no opción.
    m = re.match(r"(\d{1,2})\b", t)
    if not m:
        m = re.search(r"(?:opcion|opci|numero|num|#|la|el|n)\s*#?\s*(\d{1,2})\b", t)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(candidatos):
            return candidatos[idx - 1]

    normalizados = [(c, _norm(c)) for c in candidatos]

    # 2) el cliente escribio el nombre completo ("cx 9", "cx9", "silverado c3500")
    t_junto = t.replace(" ", "")
    for c, cn in normalizados:
        if cn and (cn == t or cn.replace(" ", "") == t_junto):
            return c

    # 3) tokens que aparecen en UN SOLO candidato: son los que distinguen
    #    ("c2500" distingue, "silverado" no). Se agrega la parte numerica
    #    suelta ("c3500" -> "3500") porque el cliente casi siempre escribe eso.
    def _tokens(cn: str) -> set[str]:
        out = set()
        junto = cn.replace(" ", "")
        if len(junto) >= 3:
            out.add(junto)
        for tok in cn.split():
            out.add(tok)
            m2 = re.fullmatch(r"[a-z]+(\d{3,4})", tok)
            if m2:
                out.add(m2.group(1))
        return out

    conteo: dict[str, int] = {}
    for _, cn in normalizados:
        for tok in _tokens(cn):
            conteo[tok] = conteo.get(tok, 0) + 1

    mejor, mejor_score = None, 0
    for c, cn in normalizados:
        if not cn:
            continue
        score = 0
        if cn in t:
            score = len(cn)
        for tok in _tokens(cn):
            if conteo.get(tok, 0) != 1:
                continue
            if not re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", t):
                continue
            score = max(score, len(tok))
        # en empate gana el nombre mas corto (la variante base, no la "clsc")
        if score > mejor_score or (score and score == mejor_score and len(cn) < len(_norm(mejor))):
            mejor, mejor_score = c, score
    return mejor
