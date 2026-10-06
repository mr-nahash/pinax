"""Curated identity layer: which historical name, in which text, denotes which modern taxon, and how
sure we are. Loaded from curation/identities.csv (plain CSV so historians can edit it in a
spreadsheet). Loci are resolved to passage ids against the live index.

confidence: secure | probable | possible | contested
relation:   is | is_not   (is_not records a known false friend)
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache

from . import settings, store
from .textnorm import fold

RANK = {"secure": 4, "probable": 3, "possible": 2, "contested": 1}


@lru_cache
def rows() -> list[dict]:
    path = settings.CURATION / "identities.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = list(csv.DictReader(f))
    titles = {}
    for r in data:
        r["passage_id"] = _resolve(r, titles)
    return data


def reload():
    rows.cache_clear()


def _resolve(r: dict, cache: dict) -> str | None:
    src, locus = r["source"], r["locus"].strip()
    if src == "dioscorides":
        return f"diosc.{locus}"
    if src == "pliny":
        return f"pliny.{locus}"
    if locus.startswith("head:"):
        want = fold(locus[5:])
        if src not in cache:
            cache[src] = [(x["id"], fold(x["title"] or "")) for x in
                          store.connect().execute("SELECT id, title FROM passages WHERE source = ?", (src,))]
        for pid, t in cache[src]:
            if t.startswith(want):
                return pid
        for pid, t in cache[src]:
            if want in t:
                return pid
    return None


def for_taxa(names: set[str]) -> list[dict]:
    keys = {n.lower() for n in names}
    return [r for r in rows() if r["taxon"].lower() in keys]


def by_passage(pid: str) -> list[dict]:
    return [r for r in rows() if r["passage_id"] == pid]


def taxa_for_name(q: str) -> list[str]:
    """Taxa whose curated historical names match a typed name (any script, any period)."""
    f = fold(q)
    if len(f) < 3:
        return []
    out = []
    for r in rows():
        if r["relation"] != "is":
            continue
        names = [fold(x) for x in re.split(r"[/,()]|\bor\b", r["name"])]
        fj = f.replace(" ", "")
        if any(n and (n == f or n.replace(" ", "") == fj or n.startswith(f + " ") or f == n.split(" ")[0]) for n in names):
            out.append(r["taxon"])
    return list(dict.fromkeys(out))
