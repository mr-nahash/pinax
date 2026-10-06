"""Wellcome Collection: live full-text search inside digitised printed books (IIIF Content Search over
OCR), with page-crop images from the IIIF Image API and deep links to the online viewer."""
from __future__ import annotations

import asyncio
import re

from .. import settings
from ..http import Trace, gather_limited, get_json

CATALOGUE = "https://api.wellcomecollection.org/catalogue/v2/works"


def long_s_variant(term: str) -> str | None:
    """Early-modern OCR reads the long s as f: 'Hyssopus' is often indexed as 'Hyffopus'."""
    v = re.sub(r"(?<=\w)s(?=\w)", "f", term)
    return v if v != term else None


async def _manifests(url: str, trace: Trace) -> list[dict]:
    """Return volume manifests [{id, canvases:{canvas_id: (index, label, w, h, image_service)}, search}]."""
    top = await get_json(url, "Wellcome IIIF manifest", trace, ttl_hours=24 * 30)
    if not top:
        return []
    vols = [top] if top.get("@type") == "sc:Manifest" else []
    if top.get("@type") == "sc:Collection":
        subs = await gather_limited([get_json(m["@id"], "Wellcome IIIF manifest", trace, ttl_hours=24 * 30)
                                     for m in top.get("manifests", [])[:6]], 4)
        vols = [s for s in subs if s]
    out = []
    for v in vols:
        canv = {}
        for seq in v.get("sequences", [])[:1]:
            for i, c in enumerate(seq.get("canvases", [])):
                svc = None
                try:
                    s = c["images"][0]["resource"].get("service")
                    svc = (s[0] if isinstance(s, list) else s)["@id"]
                except Exception:
                    pass
                canv[c["@id"]] = (i, c.get("label"), c.get("width"), c.get("height"), svc)
        search = None
        services = v.get("service", [])
        for s in services if isinstance(services, list) else [services]:
            if isinstance(s, dict) and "search" in str(s.get("profile", "")):
                search = s.get("@id")
        if not search:
            search = v["@id"].replace("/presentation/v2/", "/search/v1/")
        out.append({"id": v["@id"], "canvases": canv, "search": search, "label": v.get("label")})
    return out


def _crop(svc: str | None, xywh: list[int], w: int | None, h: int | None) -> str | None:
    if not (svc and w and h):
        return None
    x, y, ww, hh = xywh
    top = max(0, int(y - 0.05 * h))
    height = min(h - top, int(hh + 0.14 * h))
    return f"{svc}/0,{top},{w},{height}/900,/0/default.jpg"


async def search_work(book: dict, terms: list[str], trace: Trace, per_book: int = 6) -> dict:
    vols = await _manifests(book["manifest"], trace)
    hits, total = [], 0
    for vi, vol in enumerate(vols):
        for term in terms:
            d = await get_json(vol["search"], f"Wellcome full-text: {book['key']}", trace, params={"q": term})
            if not d:
                continue
            total += int(d.get("within", {}).get("total", len(d.get("hits", []))))
            on = {r["@id"]: r.get("on") for r in d.get("resources", [])}
            for h in d.get("hits", []):
                ann = (h.get("annotations") or [None])[0]
                target = on.get(ann) or ""
                cid, _, frag = target.partition("#xywh=")
                info = vol["canvases"].get(cid)
                xywh = [int(float(x)) for x in frag.split(",")] if frag else None
                idx = info[0] if info else None
                hits.append({
                    "term": term, "match": h.get("match"), "before": h.get("before"), "after": h.get("after"),
                    "page_label": info[1] if info else None, "canvas_index": idx, "volume": vi + 1,
                    "image": _crop(info[4], xywh, info[2], info[3]) if (info and xywh) else None,
                    "page_image": f"{info[4]}/full/!1200,1200/0/default.jpg" if info and info[4] else None,
                    "viewer": (f"https://wellcomecollection.org/works/{book['work_id']}/items?"
                               + (f"manifest={vi + 1}&" if len(vols) > 1 else "")
                               + (f"canvas={idx + 1}" if idx is not None else "")),
                })
    # one hit per page, earliest pages first within a book
    seen, uniq = set(), []
    for h in hits:
        k = (h["volume"], h["canvas_index"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    uniq.sort(key=lambda h: (h["volume"], h["canvas_index"] if h["canvas_index"] is not None else 1e9))
    return {**{k: book.get(k) for k in ("key", "author", "title", "year", "lang", "work_id")},
            "catalogue": f"https://wellcomecollection.org/works/{book['work_id']}",
            "n_hits": len(uniq), "hits": uniq[:per_book], "discovered": book.get("discovered", False)}


async def discover(terms: list[str], trace: Trace, exclude: set[str], limit: int) -> list[dict]:
    found = []
    for t in terms[:2]:
        d = await get_json(CATALOGUE, "Wellcome catalogue", trace, params={
            "query": t, "availabilities": "online", "workType": "a", "include": "items,production", "pageSize": 20})
        for w in (d or {}).get("results", []):
            if w["id"] in exclude or re.match(r"^[A-Z]\d{6,}", w.get("title", "")):
                continue
            years = [int(y) for p in w.get("production", []) for dd in p.get("dates", [])
                     for y in re.findall(r"\b(1[4-8]\d\d)\b", dd.get("label", ""))]
            if not years or min(years) > 1800:
                continue
            man = next((loc["url"] for it in w.get("items", []) for loc in it.get("locations", [])
                        if loc.get("locationType", {}).get("id") == "iiif-presentation"), None)
            if not man:
                continue
            exclude.add(w["id"])
            found.append({"key": w["id"], "author": "", "title": w.get("title"), "year": min(years), "lang": "",
                          "work_id": w["id"], "manifest": man, "discovered": True})
    return found[:limit]


async def search(terms: list[str], discover_terms: list[str], trace: Trace) -> list[dict]:
    cfg = settings.config()["wellcome"]
    shelf = list(cfg["shelf"])
    q = []
    for t in terms:
        q.append(t)
        v = long_s_variant(t)
        if v:
            q.append(v)
    q = list(dict.fromkeys(q))[:5]
    if cfg.get("discovery"):
        shelf += await discover(discover_terms, trace, {b["work_id"] for b in shelf}, cfg.get("max_discovered", 6))
    results = await gather_limited([search_work(b, q, trace) for b in shelf], 5)
    results = [r for r in results if r["n_hits"]]
    results.sort(key=lambda r: r["year"] or 0)
    return results
