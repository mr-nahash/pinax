"""Shared HTTP client with a small SQLite response cache and per-connector status reporting."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from . import settings, store


@dataclass
class Status:
    name: str
    ok: bool = True
    ms: int = 0
    detail: str = ""
    cached: bool = False


@dataclass
class Trace:
    """Collects per-connector outcomes for one request so the UI can show what was live."""
    items: list[Status] = field(default_factory=list)

    def add(self, s: Status):
        self.items.append(s)

    def as_list(self):
        return [s.__dict__ for s in self.items]


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        cfg = settings.config()["http"]
        _client = httpx.AsyncClient(
            timeout=cfg["timeout_seconds"], follow_redirects=True,
            headers={"User-Agent": cfg["user_agent"], "Accept": "application/json, */*"})
    return _client


async def get_json(url: str, name: str, trace: Trace | None = None, params: dict | None = None,
                   ttl_hours: float | None = None, headers: dict | None = None):
    full = str(httpx.URL(url, params=params)) if params else url
    ttl = (ttl_hours if ttl_hours is not None else settings.config()["http"]["cache_ttl_hours"]) * 3600
    hit = store.cache_get("http_cache", full, ttl)
    if hit and hit["status"] == 200:
        if trace:
            trace.add(Status(name, True, 0, "cache", True))
        return json.loads(hit["body"])
    t0 = time.perf_counter()
    try:
        r = await client().get(full, headers=headers)
        ms = int((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        data = r.json()
        store.http_cache_put(full, 200, r.content)
        if trace:
            trace.add(Status(name, True, ms, "live"))
        return data
    except Exception as e:  # network refusal, timeout, HTTP error, bad JSON
        ms = int((time.perf_counter() - t0) * 1000)
        if hit:  # stale cache beats nothing
            if trace:
                trace.add(Status(name, True, ms, f"stale cache ({type(e).__name__})", True))
            return json.loads(hit["body"])
        if trace:
            trace.add(Status(name, False, ms, f"{type(e).__name__}: {str(e)[:160]}"))
        return None


async def gather_limited(coros, limit: int = 6):
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))
