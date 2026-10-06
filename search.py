"""Search orchestration: name resolution → term expansion → corpus retrieval → identity checks."""
from __future__ import annotations

import re
from collections import defaultdict

from . import identity, store
from .connectors import names
from .http import Trace
from .textnorm import fold, fts_query, is_greek, stem

GENERIC_EPITHETS = set("""officinalis officinale officinarum vulgaris vulgare sativa sativum sativus major minor
media communis nigra nigrum niger alba album albus graveolens maculatum maculata perforatum lutea luteum
odorata odoratum sylvestris silvestris hortensis montana montanum arvensis maritima maritimum orientalis
occidentalis europaea europaeum indica indicum germanica pallida dioica urens crispum crispa
angustifolia latifolia lanceolata rotundifolia nobile nobilis majus verna vernum capitata chamomilla
pratensis aquatica palustris repens rupestris glabra flavum""".split())

CONF_RANK = {"secure": 4, "probable": 3, "possible": 2, "contested": 1}


def _split_names(s: str) -> list[str]:
    parts = re.split(r"[/,;()]", s)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" .'’‘\"")
        if p and not p.lower().startswith(("the ", "two ", "second", "first", "male", "female", "minor", "white", "black", "upright")):
            out.append(p)
    return out


def build_terms(q: str, prof: dict | None, curated: list[dict]) -> dict:
    """Search terms per script/period, each tagged with how specific it is."""
    terms: dict[str, dict[str, str]] = {"grc": {}, "lat": {}, "en": {}}

    def add(lang, t, kind):
        t = t.strip()
        if len(fold(t)) < 3 or len(t.split()) > 3:
            return
        terms[lang].setdefault(t, kind)

    for r in curated:
        if r["relation"] != "is":
            continue
        lang = {"grc": "grc", "lat": "lat"}.get(r["lang"], "en")
        for n in _split_names(r["name"]):
            if lang == "grc" and not is_greek(n):
                lang2 = "lat"
            else:
                lang2 = lang
            add(lang2, n, "curated")
    if prof:
        sci = prof["name"]
        parts = sci.split()
        add("lat", sci, "binomial")
        if len(parts) >= 2:
            if parts[1].lower() not in GENERIC_EPITHETS:
                add("lat", parts[1], "classical epithet")
            add("lat", parts[0], "genus")
        for s in prof.get("synonyms", [])[:40]:
            if s.get("name") and len(s["name"].split()) == 2:
                add("lat", s["name"], "synonym")
        for n in prof.get("vernacular", {}).get("la", [])[:4]:
            add("lat", n, "Latin label")
        for n in prof.get("vernacular", {}).get("grc", [])[:4]:
            add("grc", n, "Greek label")
        for n in prof.get("vernacular", {}).get("en", [])[:10]:
            add("en", n, "English vernacular")
    if q and not names.looks_scientific(q):
        add("grc" if is_greek(q) else "en", q, "query")
    return {k: [{"term": t, "kind": v} for t, v in d.items()] for k, d in terms.items()}


def snippet(text: str, keys: list[str], width: int = 28) -> list[dict] | None:
    """Segments of the ORIGINAL text around the first match, with matched words flagged."""
    if not text:
        return None
    toks = list(re.finditer(r"\S+", text))
    fk = [k for k in keys if k]
    first, marks = None, set()
    for i, m in enumerate(toks):
        f = fold(m.group(0))
        if f and any(f.startswith(k) or k.startswith(f) and len(f) >= 4 for k in fk):
            marks.add(i)
            if first is None:
                first = i
    if first is None:
        return None
    a, b = max(0, first - width // 2), min(len(toks), first + width)
    segs = []
    if a > 0:
        segs.append({"t": "… "})
    for i in range(a, b):
        segs.append({"t": toks[i].group(0) + " ", "m": i in marks})
    if b < len(toks):
        segs.append({"t": "…"})
    return segs


def local_search(terms: dict, curated: list[dict], taxon_names: set[str], per_source: int = 8) -> list[dict]:
    hits: dict[str, dict] = {}
    for lang, items in terms.items():
        for it in items:
            t, kind = it["term"], it["kind"]
            q = fts_query([t])
            srcs = ["dioscorides"] if lang == "grc" else None
            for pid, score, _ in store.fts(q, limit=300, sources=srcs):
                h = hits.setdefault(pid, {"score": score, "terms": {}, "kinds": set()})
                h["score"] = min(h["score"], score)
                h["terms"][t] = kind
                h["kinds"].add(kind)
    # curated passages always included
    cur_by_pid = defaultdict(list)
    for r in curated:
        if r.get("passage_id"):
            cur_by_pid[r["passage_id"]].append(r)
    ids = list(dict.fromkeys(list(cur_by_pid) + list(hits)))
    rows = {p["id"]: p for p in store.passages(ids)}
    out = []
    lower_taxa = {n.lower() for n in taxon_names}
    for pid in ids:
        p = rows.get(pid)
        if not p:
            continue
        h = hits.get(pid, {"score": 0.0, "terms": {}, "kinds": set()})
        cur = [r for r in cur_by_pid.get(pid, []) if r["taxon"].lower() in lower_taxa]
        is_rows = [r for r in cur if r["relation"] == "is"]
        not_rows = [r for r in cur if r["relation"] == "is_not"]
        other = [r for r in identity.by_passage(pid) if r["relation"] == "is" and r["taxon"].lower() not in lower_taxa]
        title_f = fold(p["title"] or "")
        keys = []
        for t in h["terms"]:
            s = stem(t)
            keys.append(s.split()[0] if " " in s else s)
        in_title = any(k and k in title_f for k in keys)
        specific = any(k not in ("genus", "synonym") for k in h["kinds"])
        if is_rows:
            status = "curated"
        elif not_rows or (other and in_title):
            status = "false_friend"      # the name itself denotes another plant here
        elif other:
            status = "mention"           # a passage about another plant that names this one
        else:
            status = "name_match"
        conf = max((CONF_RANK.get(r["confidence"], 0) for r in is_rows), default=0)
        rank = (0 if status == "curated" else 3 if status == "false_friend" else 2 if status == "mention" else 1,
                -conf, 0 if specific else 1, 0 if in_title else 1, h["score"])
        snip = snippet(p["text"], keys)
        snip_field = "text"
        if snip is None and p.get("translation"):
            snip, snip_field = snippet(p["translation"], keys), "translation"
        if snip is None:
            snip = [{"t": (p["text"] or p.get("translation") or "")[:280] + " …"}]
        out.append({
            "id": pid, "source": p["source"], "work": p["work"], "author": p["author"], "year": p["year"],
            "lang": p["lang"], "cite": p["cite"], "title": p["title"], "url": p["url"],
            "status": status, "in_title": in_title,
            "identity": [{k: r[k] for k in ("taxon", "relation", "name", "confidence", "basis", "note", "curator")}
                         for r in (is_rows or not_rows)],
            "other_identity": [{k: r[k] for k in ("taxon", "name", "confidence")} for r in other],
            "matched": [{"term": t, "kind": k} for t, k in h["terms"].items()],
            "snippet": snip, "snippet_field": snip_field,
            "has_translation": bool(p.get("translation")),
            "specific": specific,
            "_rank": rank,
        })
    out.sort(key=lambda x: x["_rank"])
    # keep all curated + false friends; cap name matches per source, preferring heading matches
    per, per_genus = defaultdict(int), defaultdict(int)
    kept = []
    for x in out:
        if x["status"] == "name_match":
            per[x["source"]] += 1
            if not x["specific"]:
                per_genus[x["source"]] += 1
            if per[x["source"]] > per_source or per_genus[x["source"]] > 2:
                continue
        kept.append(x)
    for x in kept:
        x.pop("_rank", None)
    return kept


def pliny_parallels(diosc_ids: list[str]) -> dict[str, list[dict]]:
    """Wellmann's SIM apparatus → Pliny chapters whose aligned Latin sections contain the cited §."""
    if not diosc_ids:
        return {}
    pl = [p for p in store.connect().execute("SELECT id, title, meta FROM passages WHERE source='pliny'")]
    import json
    index = []
    for r in pl:
        m = json.loads(r["meta"])
        if m.get("sections"):
            index.append((m["book"], m["sections"][0], m["sections"][1], r["id"], r["title"]))
    out = {}
    for p in store.passages(diosc_ids):
        refs = p["meta"].get("pliny_parallels") or []
        found = []
        for b, sec in refs:
            for bb, a, z, pid, title in index:
                if bb == b and a <= sec <= z:
                    found.append({"id": pid, "title": title, "ref": f"{b}.{sec}"})
        out[p["id"]] = list({f["id"]: f for f in found}.values())
    return out


async def run(q: str, name: str | None = None, key: int | None = None) -> dict:
    trace = Trace()
    cands = [] if name else await names.resolve(q, trace)
    chosen = {"name": name, "key": key} if name else (cands[0] if cands else None)
    prof = await names.profile(chosen["name"], chosen.get("key"), trace) if chosen else None
    taxon_names = set()
    if prof:
        taxon_names = {prof["name"]} | {s["name"] for s in prof.get("synonyms", []) if s.get("name")}
    if chosen:
        taxon_names.add(chosen["name"])
    curated = identity.for_taxa(taxon_names)
    terms = build_terms(q, prof, curated)
    passages = local_search(terms, curated, taxon_names)
    diosc = [p["id"] for p in passages if p["source"] == "dioscorides" and p["status"] == "curated"]
    par = pliny_parallels(diosc)
    for p in passages:
        if p["id"] in par:
            p["parallels"] = par[p["id"]]
    sources = {r["key"]: {k: r[k] for k in ("kind", "title", "author", "year", "lang", "licence", "edition", "n_passages")}
               for r in store.source_rows()}
    return {
        "query": q, "candidates": cands, "plant": prof,
        "identities": [{k: r[k] for k in ("taxon", "relation", "source", "locus", "name", "lang", "confidence",
                                           "basis", "note", "curator", "passage_id")} for r in curated],
        "terms": terms, "passages": passages, "sources": sources, "trace": trace.as_list(),
    }
