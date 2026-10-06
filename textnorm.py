"""Text folding for cross-period name search.

Every passage is stored twice: as printed, and as a *folded* form used only for matching.
Queries are folded with the same function, so “Peny-royal”, “Pennyroyall” and “pennyroyal”
meet, as do γλήχων / γλήχωνος and puleium / pulegium / pulei.
"""
from __future__ import annotations

import re
import unicodedata

_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")


def strip_marks(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def is_greek(s: str) -> bool:
    return bool(_GREEK.search(s or ""))


def fold_greek(s: str) -> str:
    s = strip_marks(s).lower().replace("ς", "σ")
    return re.sub(r"[^\w\s]", " ", s)


def fold_latin_script(s: str) -> str:
    """Latin and early-modern vernacular spelling folding.

    lower-case; ſ→s; æ/œ→e; u/v→u; i/j/y→i; vv→w (→u u→w); ph→f; th kept;
    hyphenated compounds joined; doubled letters collapsed; final -e dropped on words > 3 letters.
    """
    s = strip_marks(s.replace("ſ", "s")).lower()
    s = s.replace("æ", "e").replace("œ", "e").replace("ß", "ss")
    s = re.sub(r"(\w)-\s*(\w)", r"\1\2", s)          # Peny-royal → penyroyal
    s = s.replace("vv", "w")
    s = s.replace("v", "u").replace("j", "i").replace("y", "i").replace("ph", "f")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"([a-z])\1+", r"\1", s)               # pennyroyall → peniroial
    s = re.sub(r"\b(\w{3,})e\b", r"\1", s)            # herbe → herb
    return s


def fold(s: str) -> str:
    if not s:
        return ""
    out = []
    # fold Greek and Latin-script runs separately so mixed passages work
    for chunk in re.split(r"(\s+)", s):
        out.append(fold_greek(chunk) if is_greek(chunk) else fold_latin_script(chunk))
    return re.sub(r"\s+", " ", "".join(out)).strip()


GREEK_ENDINGS = sorted("""ος ον ου ῳ ῳ ε οι ων οις ους α ας ης η ῃ ην αι ων ες ι ιν ις εως εος ιον ιου
ιῳ ια ιων ιοις ιας ωνος ωνι ωνα ωνες ητος ητι ητα""".split(), key=len, reverse=True)
LATIN_ENDINGS = sorted("""us um i o a ae am arum is es em ibus ium ia orum os as ei ii io ium""".split(), key=len, reverse=True)


def stem(term: str) -> str:
    """Crude stem for prefix search in inflected languages. Never shorter than 4 characters."""
    f = fold(term)
    if " " in f:
        return f
    if is_greek(term) and f.endswith("ισ") and len(f) > 5:
        return f[:-1]          # -ίς/-ίδος nouns: keep the ι so λιβανωτίς does not match λιβανωτός (frankincense)
    endings = GREEK_ENDINGS if is_greek(term) else LATIN_ENDINGS
    for e in endings:
        e = fold(e)
        if f.endswith(e) and len(f) - len(e) >= 4:
            return f[: -len(e)]
    return f


def fts_query(terms: list[str], prefix: bool = True) -> str:
    """Build an FTS5 MATCH expression from user terms (OR of phrases / prefixes)."""
    parts = []
    for t in terms:
        f = fold(t)
        if not f or len(f) < 3:
            continue
        toks = f.split()
        if len(toks) > 1:
            parts.append('"' + " ".join(toks) + '"')
            joined = "".join(toks)                      # 'wall pennyroyal' ~ 'Wall-Penyroyal'
            parts.append(f'"{joined}"*' if prefix else f'"{joined}"')
        else:
            s = stem(t) if prefix else f
            parts.append(f'"{s}"*' if prefix and len(s) >= 4 else f'"{s}"')
    return " OR ".join(dict.fromkeys(parts))
