"""Claude-backed translation and recipe extraction, with caching and quote grounding.

Everything produced here is labelled machine-generated in the API and UI. Extraction is grounded:
each remedy must carry a verbatim quote from the source passage; quotes that cannot be found in
the passage (after spelling folding) are flagged `grounded: false` rather than silently kept.
"""
from __future__ import annotations

import difflib
import hashlib
import json

from . import settings, store
from .textnorm import fold

PROMPT_VERSION = "2026-10-06.1"
LANG_NAMES = {"grc": "Ancient Greek", "lat": "Latin", "enm": "Early Modern English", "eng": "English",
              "la": "Latin", "en": "Early Modern English (OCR)", "de": "Early New High German", "fr": "Middle French"}

CATEGORIES = ["digestive", "respiratory", "gynaecological & obstetric", "urinary & renal", "skin, wounds & ulcers",
              "eyes", "ears, nose, mouth & teeth", "head & nervous", "fevers", "poisons, bites & stings",
              "joints & muscles", "heart & blood", "worms & parasites", "general & tonic", "cosmetic",
              "veterinary", "other"]

TRANSLATE_TOOL = {
    "name": "record_translation",
    "description": "Record the translation of the passage.",
    "input_schema": {"type": "object", "properties": {
        "translation": {"type": "string", "description": "Complete English translation (or, for early modern English, a modern-spelling rendering), paragraphs separated by blank lines."},
        "names_kept": {"type": "array", "items": {"type": "object", "properties": {
            "original": {"type": "string"}, "rendered_as": {"type": "string"}}, "required": ["original", "rendered_as"]},
            "description": "Plant and drug names as they appear in the original and how they were rendered."},
        "uncertain": {"type": "array", "items": {"type": "string"}, "description": "Readings or terms whose sense is uncertain."}},
        "required": ["translation"]}}

EXTRACT_TOOL = {
    "name": "record_remedies",
    "description": "Record every remedy (recipe or therapeutic use) stated in the passage.",
    "input_schema": {"type": "object", "properties": {"remedies": {"type": "array", "items": {
        "type": "object", "properties": {
            "involves_focus": {"type": "boolean", "description": "True if the focus plant is an ingredient or the subject."},
            "indication": {"type": "string", "description": "Condition treated, in the text's own terms (translated)."},
            "category": {"type": "string", "enum": CATEGORIES},
            "plant_part": {"type": "string"},
            "preparation": {"type": "string", "description": "e.g. decoction, juice, powder, plaster, ointment, oil, wine, honey, syrup, distilled water, fumigation, pessary, gargle, raw"},
            "ingredients": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "quantity": {"type": "string"}, "unit": {"type": "string"}}, "required": ["name"]}},
            "vehicle": {"type": "string", "description": "Carrier: wine, honey, vinegar, oil, water, oxymel…"},
            "route": {"type": "string", "enum": ["oral", "topical", "nasal", "inhaled/fumigation", "vaginal", "rectal", "ocular", "aural", "amulet", "other", "unstated"]},
            "dose_or_timing": {"type": "string"},
            "caution": {"type": "string"},
            "quote_original": {"type": "string", "description": "The exact span of the ORIGINAL text stating this remedy, copied character for character (max ~300 characters)."},
            "quote_translation": {"type": "string", "description": "The same span in English."}},
        "required": ["involves_focus", "indication", "category", "route", "quote_original"]}}},
        "required": ["remedies"]}}

SYSTEM = """You are a philologist and historian of pharmacy assisting a scholarly search tool over historical
materia medica. Translate and analyse only what the text says. Never identify plants with modern species
(identification is handled separately and must not be smuggled into translations); keep plant and drug
names in transliteration, giving the original form in brackets at first mention. Preserve editorial
signs: [ ] for editor's deletions, ⟨ ⟩ for supplements, 〈Greek〉 or • for gaps in the transcription.
Do not modernise therapeutic concepts: render humoral terms literally. Do not omit or summarise."""


def _key(task: str, text: str, extra: str = "") -> str:
    h = hashlib.sha256(f"{task}|{settings.llm_model()}|{PROMPT_VERSION}|{extra}|{text}".encode()).hexdigest()
    return f"{task}:{h}"


async def _call(tool: dict, prompt: str) -> dict:
    import anthropic
    client = anthropic.AsyncAnthropic()
    msg = await client.messages.create(
        model=settings.llm_model(), max_tokens=settings.config()["llm"]["max_tokens"], system=SYSTEM,
        tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}])
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("model returned no structured output")


def cached(task: str, text: str, extra: str = ""):
    hit = store.cache_get("llm_cache", _key(task, text, extra))
    return json.loads(hit["value"]) if hit else None


async def translate(p: dict) -> dict:
    text = p["text"]
    if not text:
        return {"translation": "", "machine": True}
    hit = cached("translate", text)
    if hit:
        return dict(hit, cached=True)
    if not settings.llm_enabled():
        raise PermissionError("Translation is off: set ANTHROPIC_API_KEY on the server.")
    lang = LANG_NAMES.get(p["lang"], p["lang"])
    task = ("Render this early modern English passage in modern spelling and punctuation, glossing obsolete words "
            "in square brackets; keep wording otherwise unchanged." if p["lang"] in ("enm", "en") else
            f"Translate this {lang} passage into accurate, readable scholarly English.")
    prompt = (f"{task}\n\nSource: {p.get('cite')}\nHeading: {p.get('title')}\n\n<passage>\n{text}\n</passage>")
    out = await _call(TRANSLATE_TOOL, prompt)
    out.update(machine=True, model=settings.llm_model(), prompt_version=PROMPT_VERSION)
    store.llm_cache_put(_key("translate", text), "translate", settings.llm_model(), out)
    return out


def _grounded(quote: str, text: str) -> bool:
    q, t = fold(quote), fold(text)
    if not q:
        return False
    if q in t:
        return True
    n = len(q)
    best = 0.0
    step = max(1, n // 4)
    for i in range(0, max(1, len(t) - n + 1), step):
        r = difflib.SequenceMatcher(None, q, t[i:i + n]).ratio()
        best = max(best, r)
        if best >= 0.9:
            return True
    return False


async def extract(p: dict, focus: list[str], translation: str | None = None) -> dict:
    text = p["text"]
    extra = "|".join(focus)
    hit = cached("extract", text, extra)
    if hit:
        return dict(hit, cached=True)
    if not settings.llm_enabled():
        raise PermissionError("Recipe extraction is off: set ANTHROPIC_API_KEY on the server.")
    lang = LANG_NAMES.get(p["lang"], p["lang"])
    trans = translation or p.get("translation")
    prompt = (
        f"Extract every remedy stated in this {lang} passage ({p.get('cite')}).\n"
        f"Focus plant (as named in this text, if recognisable): {', '.join(focus) or 'none given'}.\n"
        "List remedies that do not involve the focus plant too, with involves_focus = false.\n"
        "quote_original must be copied exactly from the ORIGINAL passage below, not from the translation.\n\n"
        f"<original>\n{text}\n</original>\n"
        + (f"\n<translation>\n{trans}\n</translation>\n" if trans else ""))
    out = await _call(EXTRACT_TOOL, prompt)
    for r in out.get("remedies", []):
        r["grounded"] = _grounded(r.get("quote_original", ""), text)
    out.update(machine=True, model=settings.llm_model(), prompt_version=PROMPT_VERSION)
    store.llm_cache_put(_key("extract", text, extra), "extract", settings.llm_model(), out)
    return out
