"""Modern literature on a taxon from Europe PMC (open REST API)."""
from __future__ import annotations

from ..http import Trace, get_json

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


async def europepmc(names: list[str], trace: Trace, n: int = 12) -> dict:
    ors = " OR ".join(f'"{x}"' for x in names[:4])
    query = f"({ors}) AND (pharmacolog* OR ethnopharmacolog* OR \"essential oil\" OR phytochem* OR toxic* OR clinical)"
    d = await get_json(EPMC, "Europe PMC", trace, params={
        "query": query, "format": "json", "pageSize": n, "resultType": "lite", "sort": "CITED desc"}, ttl_hours=72)
    items = []
    for r in (d or {}).get("resultList", {}).get("result", []):
        items.append({"title": r.get("title"), "authors": r.get("authorString"), "journal": r.get("journalTitle"),
                      "year": r.get("pubYear"), "doi": r.get("doi"), "pmid": r.get("pmid"),
                      "cited": r.get("citedByCount"),
                      "url": f"https://doi.org/{r['doi']}" if r.get("doi") else
                             (f"https://europepmc.org/article/MED/{r['pmid']}" if r.get("pmid") else None)})
    return {"total": (d or {}).get("hitCount"), "query": query,
            "search_url": "https://europepmc.org/search?query=" + query.replace(" ", "%20"), "items": items}
