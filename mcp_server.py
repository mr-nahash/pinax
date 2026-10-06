"""MCP endpoint so Claude (or any MCP client) can query Pinax directly.

Served at /mcp by `pinax serve`; add the deployed URL as a custom connector in claude.ai, or run
`pinax mcp` for a local stdio server (Claude Desktop / Claude Code)."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Pinax", stateless_http=True,
              instructions="Search historical materia medica (Dioscorides, Pliny, early modern English herbals, "
                           "dispensatories and receipt books, Wellcome digitised books) by plant name. Identity "
                           "links between historical names and modern species carry a confidence grade; "
                           "name matches without a curated link are unverified.")
mcp.settings.streamable_http_path = "/"


@mcp.tool()
async def search_plant(name: str, max_passages: int = 25) -> dict:
    """Find historical passages about a plant. `name` may be a scientific name, synonym, vernacular name,
    or a historical name (Greek, Latin, early-modern English). Returns the resolved taxon, curated
    identifications with confidence, and passages (id, citation, status, snippet)."""
    from . import search
    r = await search.run(name)
    plant = r["plant"] or {}
    return {
        "taxon": {k: plant.get(k) for k in ("name", "authorship", "family", "gbif_url")},
        "candidates": [c["name"] for c in r["candidates"]][:5],
        "identities": r["identities"],
        "passages": [{"id": p["id"], "cite": p["cite"], "year": p["year"], "status": p["status"],
                      "confidence": (p["identity"][0]["confidence"] if p["identity"] else None),
                      "snippet": "".join(s["t"] for s in p["snippet"])} for p in r["passages"][:max_passages]],
    }


@mcp.tool()
def get_passage(passage_id: str) -> dict:
    """Full text of a passage (original language), any published translation, notes and citation."""
    from . import llm, store
    p = store.passage(passage_id)
    if not p:
        return {"error": "no such passage"}
    mt = llm.cached("translate", p["text"]) if p["text"] else None
    return {k: p[k] for k in ("id", "cite", "lang", "title", "text", "translation", "translation_note", "notes", "url")} | {
        "machine_translation": (mt or {}).get("translation")}


@mcp.tool()
async def translate_passage(passage_id: str) -> dict:
    """Machine-translate (or modernise) a passage with Claude; cached server-side."""
    from . import llm, store
    p = store.passage(passage_id)
    if not p:
        return {"error": "no such passage"}
    return await llm.translate(p)


@mcp.tool()
async def plant_constituents(scientific_name: str) -> dict:
    """Natural products reported from the taxon (Wikidata/LOTUS), with olfactory-receptor agonism from M2OR."""
    from .connectors import chemistry
    from .http import Trace
    t = Trace()
    res = await chemistry.constituents([scientific_name], t)
    return {"compounds": res[:150], "trace": t.as_list()}
