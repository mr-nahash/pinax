"""HTTP API + web UI + MCP endpoint."""
from __future__ import annotations

import contextlib

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import identity, llm, search, settings, store
from .connectors import chemistry, literature, names, wellcome
from .http import Trace
from .mcp_server import mcp

mcp_app = mcp.streamable_http_app()


async def _auto_refresh(hours: float):
    """Keep the local corpora in step with their repositories (ETag checks make no-change runs cheap)."""
    import asyncio
    from . import corpora
    while True:
        await asyncio.sleep(hours * 3600)
        await asyncio.to_thread(corpora.refresh, None, False, lambda *a: None)
        identity.reload()


@contextlib.asynccontextmanager
async def lifespan(app):
    import asyncio
    import os
    task = None
    hours = float(os.environ.get("PINAX_AUTO_REFRESH_HOURS", "0") or 0)
    if hours > 0:
        task = asyncio.create_task(_auto_refresh(hours))
    async with mcp.session_manager.run():
        yield
    if task:
        task.cancel()


app = FastAPI(title="Pinax", version="0.1", lifespan=lifespan,
              description="Name-resolved search over historical materia medica.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.mount("/mcp", mcp_app)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(settings.WEB / "index.html")


@app.get("/api/status")
def status():
    rows = store.source_rows()
    return {"llm": settings.llm_enabled(), "model": settings.llm_model() if settings.llm_enabled() else None,
            "sources": len(rows), "passages": sum(r["n_passages"] or 0 for r in rows),
            "identities": len(identity.rows()), "m2or": bool(chemistry.m2or_index())}


@app.get("/api/sources")
def sources():
    return store.source_rows()


@app.get("/api/resolve")
async def resolve(q: str = Query(..., min_length=2)):
    t = Trace()
    return {"candidates": await names.resolve(q, t), "trace": t.as_list()}


@app.get("/api/search")
async def do_search(q: str = Query(..., min_length=2), name: str | None = None, key: int | None = None):
    return await search.run(q, name, key)


@app.get("/api/passage/{pid}")
def passage(pid: str):
    p = store.passage(pid)
    if not p:
        raise HTTPException(404, "No such passage")
    p["identity"] = identity.by_passage(pid)
    p["machine_translation"] = llm.cached("translate", p["text"]) if p["text"] else None
    return p


@app.post("/api/translate/{pid}")
async def translate(pid: str):
    p = store.passage(pid)
    if not p:
        raise HTTPException(404, "No such passage")
    try:
        return await llm.translate(p)
    except PermissionError as e:
        raise HTTPException(503, str(e))


@app.post("/api/translate-text")
async def translate_text(text: str = Body(...), lang: str = Body("en"), cite: str = Body("")):
    try:
        return await llm.translate({"text": text, "lang": lang, "cite": cite, "title": ""})
    except PermissionError as e:
        raise HTTPException(503, str(e))


@app.post("/api/extract/{pid}")
async def extract(pid: str, focus: str = ""):
    p = store.passage(pid)
    if not p:
        raise HTTPException(404, "No such passage")
    tr = p.get("translation")
    if not tr and p["lang"] != "enm":
        mt = llm.cached("translate", p["text"])
        tr = mt["translation"] if mt else None
    try:
        return await llm.extract(p, [f for f in focus.split("|") if f], tr)
    except PermissionError as e:
        raise HTTPException(503, str(e))


@app.get("/api/wellcome")
async def wellcome_search(terms: str, discover: str = ""):
    t = Trace()
    res = await wellcome.search([x for x in terms.split("|") if x], [x for x in discover.split("|") if x], t)
    return {"works": res, "trace": t.as_list()}


@app.get("/api/chemistry")
async def chem(names_: str = Query(..., alias="names")):
    t = Trace()
    res = await chemistry.constituents([x for x in names_.split("|") if x], t)
    return {"compounds": res, "m2or_loaded": bool(chemistry.m2or_index()), "trace": t.as_list()}


@app.get("/api/literature")
async def lit(names_: str = Query(..., alias="names")):
    t = Trace()
    res = await literature.europepmc([x for x in names_.split("|") if x], t)
    return dict(res, trace=t.as_list())
