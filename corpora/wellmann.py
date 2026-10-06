"""Dioscorides, De materia medica — Wellmann's edition (1906–1914), First1KGreek TEI.

Keeps two parts of Wellmann's apparatus that matter for identification:
  RV  — the 'nomina alia' synonym lists (Roman, Egyptian, Dacian… names) transmitted with the text
  SIM — parallel passages, including Pliny references that we resolve to Mayhoff sections
"""
from __future__ import annotations

import re

from .common import Passage, local, parse_xml, roman_to_int, ws

SIM_PLINY = re.compile(r"Pl\.\s+([IVXLC]+)\s+([\d\s.,sq\-–]+)")


def _flatten(el, notes: list[str]) -> str:
    out: list[str] = []

    def walk(node):
        tag = local(node.tag)
        if tag == "note":
            notes.append(ws("".join(node.itertext())))
        elif tag == "del":
            out.append("[" + ws("".join(node.itertext())) + "]")
        elif tag == "add":
            out.append("⟨" + ws("".join(node.itertext())) + "⟩")
        else:
            if node.text and tag not in ("lb", "pb"):
                out.append(node.text)
            for c in node:
                walk(c)
        if node.tail:
            out.append(node.tail)

    if el.text:
        out.append(el.text)
    for c in el:
        walk(c)
    txt = ws("".join(out))
    # Wellmann's printed line numbers survive as bare digits in the OCR layer
    txt = re.sub(r"(?<![\w´ʹ])\d+(?![\w´ʹ])", "", txt)
    return ws(txt)


def _pliny_refs(note: str) -> list[tuple[int, int]]:
    refs = []
    for m in SIM_PLINY.finditer(note):
        book = roman_to_int(m.group(1))
        for num in re.findall(r"\d+", m.group(2)):
            refs.append((book, int(num)))
    return refs


def _reassign_rv(passages: list[Passage]) -> None:
    """Wellmann prints the RV synonym lists as page footnotes, so the TEI attaches each one to
    whatever chapter the page falls in. Move each list to the nearby chapter (±3, same book)
    whose lemma matches the list's headword; lists with no match stay put, flagged."""
    from ..textnorm import fold
    by_book: dict[int, list[Passage]] = {}
    for p in passages:
        by_book.setdefault(p.meta["book"], []).append(p)
    moves = []
    for book, ps in by_book.items():
        ps.sort(key=lambda x: x.meta["chapter"])
        heads = [fold(" ".join(p.text.split()[:4])) for p in ps]
        for i, p in enumerate(ps):
            keep = []
            for rv in p.meta["rv"]:
                hw = fold(re.split(r"[·,;]", rv, 1)[0]).split()
                key = hw[0][:5] if hw else ""
                target = None
                if key:
                    for d in (0, -1, 1, -2, 2, -3, 3):
                        j = i + d
                        if 0 <= j < len(ps) and any(w.startswith(key) for w in heads[j].split()):
                            target = ps[j]
                            break
                if target is None:
                    keep.append("(unplaced) " + rv)
                elif target is p:
                    keep.append(rv)
                else:
                    moves.append((target, rv))
            p.meta["rv"] = keep
    for target, rv in moves:
        target.meta["rv"].append(rv)


def parse(data: bytes, cfg: dict) -> list[Passage]:
    root = parse_xml(data)

    def is_ch(e):
        return local(e.tag) == "div" and e.get("subtype") == "chapter"

    def parent_ch(e):
        p = e.getparent()
        while p is not None and not is_ch(p) and p.get("subtype") != "book":
            p = p.getparent()
        return p if p is not None and is_ch(p) else None

    def owner(ch):
        # The TEI has unclosed chapter divs (1.67–129 nested in 1.66) and sub-chapters
        # mis-tagged as chapters. A nested chapter stands alone only if it continues the numbering.
        par = parent_ch(ch)
        if par is None:
            return ch
        n, pn = ch.get("n", ""), par.get("n", "")
        if n.isdigit() and pn.isdigit() and int(n) > int(pn):
            return ch
        return owner(par)

    merged: dict[str, Passage] = {}
    for book in root.iter("{*}div"):
        if book.get("subtype") != "book":
            continue
        bn = book.get("n")
        for ch in (c for c in book.iter("{*}div") if is_ch(c) and owner(c) is c):
            cn = ch.get("n")
            if not cn or not cn.isdigit():
                continue
            notes, paras = [], []
            for p in ch.iter("{*}p"):
                pc = parent_ch(p)
                if pc is None or owner(pc) is not ch:
                    continue
                t = _flatten(p, notes)
                if t:
                    paras.append(t)
            text = ws(" ".join(paras))
            if not text:
                continue
            rv = [re.sub(r"^\d+\s*RV:\s*", "", n) for n in notes if "RV:" in n]
            sim = [n for n in notes if " SIM." in " " + n[:12]]
            pid = f"diosc.{bn}.{cn}"
            lemma = re.sub(r"[·,.;᾿ʼ]", "", text.split(" ", 1)[0])
            if pid in merged:
                m = merged[pid]
                m.text = ws(m.text + " " + text)
                m.meta["rv"] += rv
                m.meta["sim"] += sim
                continue
            merged[pid] = Passage(
                id=pid, source=cfg["key"], work="De materia medica", author="Dioscorides",
                year=cfg.get("year", 65), lang="grc",
                cite=f"Dioscorides, De materia medica {bn}.{cn} (ed. Wellmann)",
                title=lemma, text=text,
                meta={"book": int(bn), "chapter": int(cn), "rv": rv, "sim": sim},
                url=f"https://scaife.perseus.org/reader/urn:cts:greekLit:tlg0656.tlg001.1st1K-grc1:{bn}.{cn}/",
            )
    out = list(merged.values())
    _reassign_rv(out)
    for p in out:
        refs = sorted({r for n in p.meta["sim"] for r in _pliny_refs(n)})
        p.meta["pliny_parallels"] = refs
        if p.meta["rv"]:
            p.notes = ["Nomina alia (Wellmann RV): " + x for x in p.meta["rv"]]
    return out
