"""Pliny, Naturalis historia, books 20–27 (remedies from plants).

Latin: Mayhoff (Teubner) via Perseus canonical-latinLit (perseus-lat2), sectioned by §.
English: Bostock & Riley (1855) via Perseus (perseus-eng1), chaptered differently.

The two are aligned automatically: a monotone dynamic programme assigns each English chapter a
contiguous run of Latin sections, scoring IDF-weighted lexical anchors (proper names, Latin words
Bostock quotes in his footnotes) against a length-ratio penalty, with a bonus where Bostock's
parenthetical old chapter number “(N.)” coincides with the start of Mayhoff chapter N.
Alignments are marked as automatic in every record.
"""
from __future__ import annotations

import math
import re

from .common import Passage, load_tei_p4, local, parse_xml, ws

BOOKS = range(20, 28)
HEAD = re.compile(r"CHAP\.\s*(\d+)\.\s*(?:\((\d+)\.\))?")
STOP_EN = set("""which their there these those would could should about other being after again
before under above where while whose whom shall might every great small often first three seven
eight taken taking water times though through against between because mixed applied together
employed remedies remedy plant plants leaves leaf seeds seed juice root roots wine honey vinegar""".split())


def _latin(data: bytes) -> dict:
    root = parse_xml(data)
    out = {}
    for book in root.iter("{*}div"):
        if book.get("subtype") != "book" or not book.get("n", "").isdigit():
            continue
        bn = int(book.get("n"))
        if bn not in BOOKS:
            continue
        for ch in book:
            if local(ch.tag) != "div" or not ch.get("n", "").isdigit():
                continue
            cn = int(ch.get("n"))
            parts = []

            def walk(node):
                tag = local(node.tag)
                if tag == "note":
                    pass
                elif tag == "milestone" and node.get("unit") == "section":
                    parts.append(f" §{node.get('n')} ")
                else:
                    if node.text:
                        parts.append(node.text)
                    for c in node:
                        walk(c)
                if node.tail:
                    parts.append(node.tail)

            for c in ch:
                walk(c)
            out[(bn, cn)] = ws("".join(parts))
    return out


def _english(data: bytes) -> list[dict]:
    root = load_tei_p4(data)
    recs, page = [], None
    for book in root.iter("div1"):
        if not (book.get("n") or "").isdigit():
            continue
        bn = int(book.get("n"))
        for ch in book.iter("div2"):
            pbs = [pb.get("n") for pb in ch.iter("pb")]
            head = ws(ch.findtext("head") or "")
            if bn in BOOKS:
                notes, parts = [], []

                def walk(node):
                    tag = node.tag if isinstance(node.tag, str) else ""
                    if tag == "note":
                        notes.append(ws("".join(node.itertext())))
                        parts.append(f"⁽{len(notes)}⁾")
                    elif tag == "head":
                        pass
                    else:
                        if tag == "p":
                            parts.append("\n\n")
                        if node.text:
                            parts.append(node.text)
                        for c in node:
                            walk(c)
                    if node.tail:
                        parts.append(node.tail)

                for c in ch:
                    walk(c)
                text = "\n\n".join(ws(x) for x in "".join(parts).split("\n\n") if ws(x))
                title = re.sub(r"^CHAP\.\s*\d+\.\s*(\(\d+\.\))?\s*[—–-]*\s*", "", head)
                title = re.sub(r"^\(\d+\.\)\s*[—–-]*\s*", "", title)
                recs.append({"book": bn, "chapter": int(ch.get("n")), "head": head, "title": title,
                             "text": text, "notes": notes, "page": page})
            page = pbs[-1] if pbs else page
    return recs


def _keys(text: str, latin: bool = False) -> set[str]:
    out = set()
    for t in re.findall(r"[A-Za-zÀ-ÿæœÆŒ]{5,}", text):
        tl = t.lower().replace("æ", "ae").replace("œ", "oe")
        if not latin and (tl in STOP_EN or not (t[0].isupper() or tl.endswith(
                ("os", "on", "um", "us", "is", "ia", "ae", "es")))):
            continue
        out.add(tl[:5])
    return out


def _note_keys(notes: list[str]) -> set[str]:
    ks = set()
    for n in notes:
        for q in re.findall(r'"([^"]{3,80})"|“([^”]{3,80})”', n):
            for t in re.findall(r"[A-Za-zæœ]{5,}", q[0] or q[1]):
                ks.add(t.lower().replace("æ", "ae").replace("œ", "oe")[:5])
    return ks


def _split_sections(latin_text: str):
    parts = re.split(r"\s§(\d+)\s", " " + latin_text + " ")
    secs = []
    if parts[0].strip():
        secs.append((None, parts[0].strip()))
    for i in range(1, len(parts) - 1, 2):
        if parts[i + 1].strip():
            secs.append((int(parts[i]), parts[i + 1].strip()))
    return secs


def align(eng: list[dict], lat: dict) -> None:
    for b in sorted({e["book"] for e in eng}):
        E = [e for e in eng if e["book"] == b]
        secs = []
        for (bb, cn) in sorted(k for k in lat if k[0] == b):
            for n, t in _split_sections(lat[(bb, cn)]):
                secs.append((n, cn, t))
        k, m = len(E), len(secs)
        if not m:
            continue
        first_of = {}
        for i, (_, cn, _) in enumerate(secs):
            first_of.setdefault(cn, i)
        kL = [_keys(t, True) for _, _, t in secs]
        df: dict[str, int] = {}
        for ks in kL:
            for x in ks:
                df[x] = df.get(x, 0) + 1
        kE = [_keys(re.sub(r"⁽\d+⁾", "", e["text"])) | _note_keys(e["notes"]) for e in E]
        lE = [max(40, len(re.sub(r"⁽\d+⁾", "", e["text"]))) for e in E]
        lL = [len(t) for _, _, t in secs]
        r = sum(lE) / sum(lL)
        starts = {}
        for i, e in enumerate(E):
            hm = re.search(r"\((\d+)\.\)", e["head"])
            if hm:
                starts[i] = int(hm.group(1))
        NEG, MAXRUN = -1e18, 30
        best = [[NEG] * (m + 1) for _ in range(k + 1)]
        back = [[0] * (m + 1) for _ in range(k + 1)]
        best[0][0] = 0.0
        for i in range(1, k + 1):
            for j in range(1, m + 1):
                acc, ll = set(), 0
                for t in range(j - 1, max(-1, j - 1 - MAXRUN), -1):
                    acc |= kL[t]
                    ll += lL[t]
                    if best[i - 1][t] == NEG:
                        continue
                    anchors = sum(1.0 / df[x] for x in kE[i - 1] & acc)
                    pen = abs(math.log((lE[i - 1] / ll) / r))
                    bonus = 0.0
                    if (i - 1) in starts:
                        bonus = 2.0 if first_of.get(starts[i - 1]) == t else -0.5
                    sc = best[i - 1][t] + anchors - 1.5 * pen + bonus
                    if sc > best[i][j]:
                        best[i][j], back[i][j] = sc, t
        if best[k][m] == NEG:
            continue
        j, spans = m, []
        for i in range(k, 0, -1):
            t = back[i][j]
            spans.append((t, j))
            j = t
        for e, (a, z) in zip(E, reversed(spans)):
            sl = secs[a:z]
            nums = [n for n, _, _ in sl if n is not None]
            e["latin"] = " ".join((f"§{n} " if n is not None else "") + t for n, _, t in sl)
            e["sections"] = [min(nums), max(nums)] if nums else None


def parse(files: dict[str, bytes], cfg: dict) -> list[Passage]:
    lat = _latin(files["lat"])
    eng = _english(files["eng"])
    align(eng, lat)
    out = []
    for e in eng:
        secs = e.get("sections")
        sec_txt = (f"; Latin §§{e['book']}.{secs[0]}–{secs[1]} (Mayhoff, auto-aligned)" if secs else "")
        out.append(Passage(
            id=f"pliny.{e['book']}.{e['chapter']}", source=cfg["key"], work="Naturalis historia",
            author="Pliny the Elder", year=cfg.get("year", 77), lang="lat",
            cite=f"Pliny, Naturalis historia {e['book']}.{e['chapter']} (Bostock & Riley){sec_txt}",
            title=e["title"].rstrip("."),
            text=e.get("latin") or "",
            translation=e["text"],
            translation_note="Bostock & Riley, The Natural History of Pliny (London, 1855)",
            notes=e["notes"],
            meta={"book": e["book"], "chapter": e["chapter"], "sections": secs, "bohn_page": e["page"],
                  "alignment": "automatic (monotone DP), unreviewed" if secs else None},
            url=f"http://www.perseus.tufts.edu/hopper/text?doc=Perseus:text:1999.02.0137:book={e['book']}:chapter={e['chapter']}",
        ))
    return out
