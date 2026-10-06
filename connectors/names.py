"""Live taxonomic name resolution: GBIF backbone, Kew POWO, Wikidata."""
from __future__ import annotations

import re
from urllib.parse import quote

from .. import identity
from ..http import Trace, get_json
from ..textnorm import is_greek

GBIF = "https://api.gbif.org/v1"
BACKBONE = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
POWO = "https://powo.science.kew.org/api/2"
WDQS = "https://query.wikidata.org/sparql"
LANGS = ["en", "la", "grc", "el", "ar", "fa", "he", "fr", "de", "it", "es", "pt", "nl", "tr"]
ISO3_TO_2 = {"eng": "en", "fra": "fr", "deu": "de", "ger": "de", "ita": "it", "spa": "es", "por": "pt",
             "nld": "nl", "dut": "nl", "ara": "ar", "ell": "el", "gre": "el", "lat": "la", "tur": "tr",
             "fas": "fa", "per": "fa", "heb": "he"}


def looks_scientific(q: str) -> bool:
    return bool(re.match(r"^[A-Z][a-z]+(\s+[a-z\-]+)?(\s+.*)?$", q.strip())) and not is_greek(q)


async def gbif_match(name: str, trace: Trace):
    d = await get_json(f"{GBIF}/species/match", "GBIF name match", trace,
                       params={"name": name, "kingdom": "Plantae", "verbose": "true"})
    if not d or d.get("matchType") == "NONE":
        return None
    return d


async def gbif_species(key: int, trace: Trace):
    return await get_json(f"{GBIF}/species/{key}", "GBIF taxon", trace)


async def gbif_vernacular_search(q: str, trace: Trace) -> list[dict]:
    d = await get_json(f"{GBIF}/species/search", "GBIF vernacular search", trace,
                       params={"q": q, "qField": "VERNACULAR", "datasetKey": BACKBONE, "rank": "SPECIES",
                               "status": "ACCEPTED", "limit": 30})
    return (d or {}).get("results", [])


async def gbif_synonyms(key: int, trace: Trace) -> list[dict]:
    d = await get_json(f"{GBIF}/species/{key}/synonyms", "GBIF synonyms", trace, params={"limit": 200})
    return (d or {}).get("results", [])


async def gbif_vernaculars(key: int, trace: Trace) -> list[dict]:
    d = await get_json(f"{GBIF}/species/{key}/vernacularNames", "GBIF vernacular names", trace,
                       params={"limit": 400})
    return (d or {}).get("results", [])


async def powo(name: str, trace: Trace) -> dict | None:
    d = await get_json(f"{POWO}/search", "Kew POWO search", trace, params={"q": name})
    res = [r for r in (d or {}).get("results", []) if r.get("name", "").lower() == name.lower()]
    if not res:
        return None
    hit = res[0]
    out = {"fqId": hit["fqId"], "name": hit["name"], "author": hit.get("author"), "accepted": hit.get("accepted"),
           "family": hit.get("family"), "url": f"https://powo.science.kew.org/taxon/{hit['fqId']}", "synonyms": []}
    det = await get_json(f"{POWO}/taxon/{quote(hit['fqId'], safe=':')}", "Kew POWO taxon", trace)
    if det:
        out["synonyms"] = [{"name": s.get("name"), "author": s.get("author")} for s in det.get("synonyms", [])]
        if det.get("accepted") is False and det.get("acceptedNameUsage"):
            out["accepted_name"] = det["acceptedNameUsage"].get("name")
    return out


async def sparql(query: str, label: str, trace: Trace):
    d = await get_json(WDQS, label, trace, params={"query": query, "format": "json"},
                       headers={"Accept": "application/sparql-results+json"})
    return (d or {}).get("results", {}).get("bindings", []) if d else None


async def wikidata_taxon(name: str, trace: Trace) -> dict | None:
    langs = ",".join(f'"{x}"' for x in LANGS)
    q = f"""SELECT ?item ?label ?lang ?kind WHERE {{
      ?item wdt:P225 "{name}" .
      {{ ?item rdfs:label ?label . BIND("label" AS ?kind) }}
      UNION {{ ?item skos:altLabel ?label . BIND("alias" AS ?kind) }}
      UNION {{ ?item wdt:P1843 ?label . BIND("common" AS ?kind) }}
      BIND(LANG(?label) AS ?lang) FILTER(?lang IN ({langs})) }} LIMIT 600"""
    b = await sparql(q, "Wikidata names", trace)
    if b is None:
        return None
    q2 = f"""SELECT ?item ?img ?powo ?gbif ?syn WHERE {{
      ?item wdt:P225 "{name}" .
      OPTIONAL {{ ?item wdt:P18 ?img }} OPTIONAL {{ ?item wdt:P5037 ?powo }} OPTIONAL {{ ?item wdt:P846 ?gbif }}
      OPTIONAL {{ ?item wdt:P1420 ?s . ?s wdt:P225 ?syn }} }} LIMIT 200"""
    b2 = await sparql(q2, "Wikidata identifiers", trace) or []
    if not b and not b2:
        return None
    labels: dict[str, list[str]] = {}
    for r in b:
        labels.setdefault(r["lang"]["value"], [])
        v = r["label"]["value"]
        if v not in labels[r["lang"]["value"]]:
            labels[r["lang"]["value"]].append(v)
    item = (b or b2)[0]["item"]["value"]
    img = next((r["img"]["value"] for r in b2 if "img" in r), None)
    return {"qid": item.rsplit("/", 1)[-1], "url": item, "labels": labels, "image": img,
            "synonyms": sorted({r["syn"]["value"] for r in b2 if "syn" in r})}


async def wikidata_search(q: str, trace: Trace) -> list[str]:
    """Free-text label search (any language) restricted to items with a taxon name."""
    lang = "el" if is_greek(q) else "en"
    s = q.replace('"', "")
    query = f"""SELECT DISTINCT ?name WHERE {{
      SERVICE wikibase:mwapi {{ bd:serviceParam wikibase:endpoint "www.wikidata.org";
        wikibase:api "EntitySearch"; mwapi:search "{s}"; mwapi:language "{lang}".
        ?item wikibase:apiOutputItem mwapi:item. }}
      ?item wdt:P225 ?name . ?item wdt:P105 wd:Q7432 . }} LIMIT 8"""
    b = await sparql(query, "Wikidata label search", trace) or []
    return [r["name"]["value"] for r in b]


def _cand(m: dict, how: str) -> dict:
    return {"name": m.get("canonicalName") or m.get("scientificName"), "scientific": m.get("scientificName"),
            "key": m.get("acceptedUsageKey") or m.get("usageKey") or m.get("key") or m.get("nubKey"),
            "family": m.get("family"), "status": m.get("status") or m.get("taxonomicStatus"), "via": how}


async def resolve(q: str, trace: Trace) -> list[dict]:
    """Candidate accepted taxa for a typed name (scientific, vernacular, or historical)."""
    q = q.strip()
    cands: list[dict] = []
    for t in identity.taxa_for_name(q):
        m = await gbif_match(t, trace)
        cands.append(_cand(m, "curated historical name") if m else {"name": t, "key": None, "via": "curated historical name"})
    if looks_scientific(q):
        m = await gbif_match(q, trace)
        if m and m.get("rank") in ("SPECIES", "SUBSPECIES", "VARIETY", "GENUS") and m.get("confidence", 0) >= 80:
            c = _cand(m, "scientific name")
            if m.get("status") == "SYNONYM" and m.get("acceptedUsageKey"):
                acc = await gbif_species(m["acceptedUsageKey"], trace)
                if acc:
                    c.update(name=acc.get("canonicalName"), scientific=acc.get("scientificName"),
                             family=acc.get("family"), status="ACCEPTED", via=f"synonym {m.get('canonicalName')}")
            cands.append(c)
    if not is_greek(q):
        for r in await gbif_vernacular_search(q, trace):
            names = [v.get("vernacularName", "").lower() for v in r.get("vernacularNames", [])]
            exact = q.lower() in names
            c = _cand(r, "vernacular name (GBIF)" + ("" if exact else ", partial"))
            c["exact"] = exact
            cands.append(c)
    if not cands or is_greek(q):
        for n in await wikidata_search(q, trace):
            m = await gbif_match(n, trace)
            if m:
                cands.append(_cand(m, "Wikidata label"))
    # merge by accepted key / name, keep first (= best) reason
    seen, out = set(), []
    for c in cands:
        k = c.get("key") or c["name"]
        if k in seen or not c.get("name"):
            continue
        seen.add(k)
        out.append(c)
    # Vernacular names are ambiguous across continents ('pennyroyal' is also Hedeoma, Piloblephis…).
    # Break ties by historical prominence: how often the genus / classical epithet occurs in the corpora.
    from .. import store
    from ..search import GENERIC_EPITHETS
    from ..textnorm import fts_query
    for c in out:
        parts = (c.get("name") or "").split()
        words = [parts[0]] + ([parts[1]] if len(parts) > 1 and parts[1].lower() not in GENERIC_EPITHETS else [])
        c["corpus_hits"] = len(store.fts(fts_query(words), limit=500)) if words else 0
    out.sort(key=lambda c: (0 if "curated" in c["via"] else 1 if "scientific" in c["via"] or "synonym" in c["via"]
                            else 2, 0 if c.get("exact", True) else 1, -c["corpus_hits"]))
    return out[:10]


async def profile(name: str, key: int | None, trace: Trace) -> dict:
    import asyncio
    acc = await gbif_species(key, trace) if key else None
    if acc is None:
        m = await gbif_match(name, trace)
        key = (m or {}).get("acceptedUsageKey") or (m or {}).get("usageKey")
        acc = await gbif_species(key, trace) if key else None
    canonical = (acc or {}).get("canonicalName") or name
    syn, vern, pw, wd = await asyncio.gather(
        gbif_synonyms(key, trace) if key else _none(), gbif_vernaculars(key, trace) if key else _none(),
        powo(canonical, trace), wikidata_taxon(canonical, trace))
    synonyms = {}
    for s in syn or []:
        if s.get("rank") in ("SPECIES", "SUBSPECIES", "VARIETY", "FORM"):
            synonyms[s.get("canonicalName")] = {"name": s.get("canonicalName"), "author": s.get("authorship"), "src": ["GBIF"]}
    for s in (pw or {}).get("synonyms", []):
        n = s.get("name")
        if n:
            synonyms.setdefault(n, {"name": n, "author": s.get("author"), "src": []})["src"].append("POWO")
    for n in (wd or {}).get("synonyms", []):
        synonyms.setdefault(n, {"name": n, "author": None, "src": []})["src"].append("Wikidata")
    vernacular: dict[str, list[str]] = {}
    for v in vern or []:
        lang = ISO3_TO_2.get((v.get("language") or "").lower(), (v.get("language") or "?").lower())
        n = v.get("vernacularName", "").strip()
        if n and n.lower() not in [x.lower() for x in vernacular.get(lang, [])]:
            vernacular.setdefault(lang, []).append(n)
    for lang, names in (wd or {}).get("labels", {}).items():
        for n in names:
            if n.lower() != canonical.lower() and n.lower() not in [x.lower() for x in vernacular.get(lang, [])]:
                vernacular.setdefault(lang, []).append(n)
    return {
        "name": canonical, "scientific": (acc or {}).get("scientificName") or name,
        "authorship": (acc or {}).get("authorship"), "family": (acc or {}).get("family"),
        "gbif_key": key, "gbif_url": f"https://www.gbif.org/species/{key}" if key else None,
        "powo": pw, "wikidata": {k: v for k, v in (wd or {}).items() if k != "labels"} if wd else None,
        "synonyms": sorted(synonyms.values(), key=lambda s: s["name"]),
        "vernacular": vernacular,
    }


async def _none():
    return None
