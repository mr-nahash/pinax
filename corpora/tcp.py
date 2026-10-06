"""Generic EEBO-TCP parser (TEI P5 as distributed by textcreationpartnership on GitHub).

Passage units are chosen structurally, so the same code serves herbals (one chapter per plant),
dispensatories and receipt books (one recipe per div): walk the <body> top-down and take the
largest <div> that carries a <head> and holds no more than MAX_CHARS of text; larger divs are
descended into, and any loose paragraphs they hold become their own unit.
"""
from __future__ import annotations

import re

from .common import Passage, local, parse_xml, ws

MAX_CHARS = 30000
GENERIC = re.compile(r"^(¶\s*)?(the\s+)?(description|place|places|time|times|names?|vertues?|virtues?|temperature|kinds?|kindes|government|use|uses|danger|dangers|nature|qualities|faculties|vertues and use|government and vertues)\b", re.I)
SKIP_DIV_TYPES = {"index", "errata", "table_of_contents", "title_page", "publishers_advertisement",
                  "list_of_herbs_arranged_by_planet", "colophon", "imprimatur"}
PLANETS = "☉☽☿♀♂♃♄♈♉♊♋♌♍♎♏♐♑♒♓"


def _text(node, notes: list[str] | None = None, skip_heads: bool = True) -> str:
    out: list[str] = []

    def walk(n, top=False):
        tag = local(n.tag)
        if tag == "g":
            if n.get("ref") != "char:EOLhyphen":
                out.append(n.text or "")
        elif tag == "gap":
            desc = ws("".join(n.itertext()))
            out.append("〈Greek〉" if "non-Latin" in desc else "•")
        elif tag == "note":
            if notes is not None:
                notes.append(ws(_text(n, None, False)))
        elif tag == "head" and skip_heads and not top:
            pass
        elif tag in ("figure", "fw"):
            pass
        else:
            if tag in ("p", "item", "l", "lg", "list", "table", "row") and out and not out[-1].endswith("\n"):
                out.append("\n\n" if tag in ("p", "lg", "list", "table") else "\n")
            if n.text:
                out.append(n.text)
            for c in n:
                walk(c)
        if n.tail and not top:
            out.append(n.tail)

    walk(node, top=True)
    raw = "".join(out)
    paras = [ws(x) for x in re.split(r"\n\s*\n", raw)]
    return "\n\n".join(p for p in paras if p)


def _head(div) -> str:
    heads = [h for h in div if local(h.tag) == "head"]
    t = " ".join(ws(_text(h, None, False)) for h in heads)
    t = ws(re.sub(f"[{PLANETS}]", "", t)).strip(" .&")
    return re.sub(r"^&?\s*Jupit;\s*", "", t)


def _first_page(div, last_page):
    for pb in div.iter("{*}pb"):
        return pb.get("n") or (pb.get("facs") or "").split(":")[-1] or last_page
    return last_page


def parse(data: bytes, cfg: dict) -> list[Passage]:
    root = parse_xml(data)
    # the main <body> (not one nested in a <floatingText> letter in the front matter)
    bodies = [b for b in root.iter("{*}body")
              if not any(local(a.tag) == "floatingText" for a in b.iterancestors())]
    if not bodies:
        return []
    units: list[tuple[list[str], object]] = []   # (heading trail, element or list of loose <p>)

    def text_len(el) -> int:
        return len(ws("".join(el.itertext())))

    def visit(div, trail):
        if local(div.tag) == "div" and div.get("type") in SKIP_DIV_TYPES:
            return
        h = _head(div) if local(div.tag) == "div" else ""
        here = trail + ([h] if h else [])
        if local(div.tag) == "div" and h and text_len(div) <= MAX_CHARS:
            units.append((here, div))
            return
        loose = []
        for c in div:
            if local(c.tag) == "div":
                if loose:
                    units.append((here, loose)); loose = []
                visit(c, here)
            elif local(c.tag) in ("p", "list", "table", "lg"):
                loose.append(c)
        if loose:
            units.append((here, loose))

    for body in bodies:
        visit(body, [])
    out: list[Passage] = []
    last_page = None
    key = cfg["key"]
    tcp = cfg["tcp"]
    for i, (trail, el) in enumerate(units, 1):
        notes: list[str] = []
        if isinstance(el, list):
            text = "\n\n".join(_text(x, notes) for x in el)
            page = None
            for x in el:
                page = _first_page(x, None) or page
            page = page or last_page
        else:
            text = _text(el, notes)
            page = _first_page(el, last_page)
        last_page = page or last_page
        if len(text) < 40:
            continue
        title = trail[-1] if trail else cfg["title"]
        if len(trail) >= 2 and GENERIC.match(title or ""):
            title = f"{trail[-2]} — {title}"
        # very long loose runs are chunked so that a hit points somewhere useful
        chunks = [text] if len(text) <= MAX_CHARS else _chunk(text, 6000)
        for j, ch in enumerate(chunks, 1):
            pid = f"{key}.{i}" + (f".{j}" if len(chunks) > 1 else "")
            out.append(Passage(
                id=pid, source=key, work=cfg["title"], author=cfg.get("author", ""),
                year=cfg.get("year", 0), lang=cfg.get("lang", "enm"),
                cite=f"{cfg.get('author', '')}, {cfg['title']} ({cfg.get('year', '')})"
                     + (f", “{title}”" if title and title != cfg["title"] else "")
                     + (f", p. {page}" if page else ""),
                title=title, text=ch, notes=notes,
                meta={"trail": trail, "page": page, "tcp": tcp, "seq": i},
                url=f"https://github.com/textcreationpartnership/{tcp}/blob/master/{tcp}.xml",
            ))
    return out


def _chunk(text: str, size: int) -> list[str]:
    paras, cur, out = text.split("\n\n"), [], []
    n = 0
    for p in paras:
        if n + len(p) > size and cur:
            out.append("\n\n".join(cur)); cur, n = [], 0
        cur.append(p); n += len(p)
    if cur:
        out.append("\n\n".join(cur))
    return out
