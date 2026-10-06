from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from lxml import etree


@dataclass
class Passage:
    id: str
    source: str            # corpus key, e.g. "dioscorides", "pliny", "tcp:A35365"
    work: str              # short title of the work
    author: str
    year: int              # date of composition / printing (negative = BCE)
    lang: str              # grc | lat | eng | enm (early modern English)
    cite: str              # human citation
    title: str             # heading / lemma
    text: str              # original-language text as printed in the edition
    translation: str | None = None       # an existing published translation (e.g. Bostock & Riley)
    translation_note: str | None = None
    alt_text: str | None = None          # parallel text (e.g. English chapter for Pliny when text is Latin)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    url: str | None = None

    def to_row(self) -> dict:
        return asdict(self)


def ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def local(tag) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = ROMAN.get(ch, 0)
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def load_tei_p4(data: bytes) -> etree._Element:
    """Parse Perseus TEI P4 files whose DTD and HTML entities cannot be resolved offline."""
    s = data.decode("utf-8")
    s = re.sub(r"<!DOCTYPE.*?\]>", "", s, flags=re.S)

    def ent(m):
        name = m.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return m.group(0)
        u = html.unescape(f"&{name};")
        return u if u != f"&{name};" else ""

    s = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", ent, s)
    return etree.fromstring(s.encode("utf-8"), etree.XMLParser(recover=True, huge_tree=True))


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False))
