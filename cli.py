"""pinax — command line.

  pinax refresh [--force] [--only KEY ...]   pull corpora from their repositories and re-index changed ones
  pinax m2or                                 download M2OR and build the InChIKey index
  pinax serve [--host 0.0.0.0] [--port 8000] run the web app (+ MCP at /mcp)
  pinax mcp                                  run the MCP server over stdio
  pinax find-tcp WORDS                       search the 61k-title EEBO-TCP catalogue
  pinax add-tcp ID [--kind herbal]           add a TCP text to the local corpus (then run refresh)
  pinax translate-curated                    pre-translate every curated non-English passage (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys

import httpx
import yaml

from . import settings


def _catalogue() -> list[dict]:
    url = settings.config()["tcp_catalogue"]
    cache = settings.DATA / "TCP.csv"
    if not cache.exists():
        settings.DATA.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(httpx.get(url, timeout=120, follow_redirects=True).content)
    return list(csv.DictReader(io.StringIO(cache.read_text(encoding="utf-8", errors="ignore"))))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pinax")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh"); r.add_argument("--force", action="store_true"); r.add_argument("--only", nargs="*")
    sub.add_parser("m2or")
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp")
    f = sub.add_parser("find-tcp"); f.add_argument("words", nargs="+")
    a = sub.add_parser("add-tcp"); a.add_argument("id"); a.add_argument("--kind", default="text")
    sub.add_parser("translate-curated")
    args = ap.parse_args(argv)

    if args.cmd == "refresh":
        from . import corpora
        print("Refreshing corpora from their repositories…")
        corpora.refresh(only=args.only, force=args.force)
        from . import identity
        identity.reload()
    elif args.cmd == "m2or":
        from .connectors import chemistry
        chemistry.build_m2or_index()
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("pinax.api:app", host=args.host, port=args.port)
    elif args.cmd == "mcp":
        from .mcp_server import mcp
        mcp.run()
    elif args.cmd == "find-tcp":
        words = [w.lower() for w in args.words]
        for row in _catalogue():
            hay = " ".join(str(v) for v in row.values()).lower()
            if all(w in hay for w in words):
                print(f"{row.get('TCP', '?'):8s} {row.get('Date', '')[:4]:5s} {row.get('Author', '')[:30]:30s} {row.get('Title', '')[:90]}")
    elif args.cmd == "add-tcp":
        row = next((r for r in _catalogue() if r.get("TCP") == args.id), None)
        if not row:
            sys.exit(f"{args.id} is not in the TCP catalogue")
        year = int((row.get("Date") or "0")[:4] or 0) if (row.get("Date") or "")[:4].isdigit() else 0
        entry = {"tcp": args.id, "author": row.get("Author", ""), "title": row.get("Title", "")[:120], "year": year, "kind": args.kind}
        path = settings.CURATION / "tcp_added.yaml"
        cur = yaml.safe_load(path.read_text()) if path.exists() else []
        cur = [e for e in (cur or []) if e["tcp"] != args.id] + [entry]
        path.write_text(yaml.safe_dump(cur, allow_unicode=True, sort_keys=False))
        print(f"Added {args.id}: {entry['title']} ({year}). Run `pinax refresh --only {args.id}`.")
    elif args.cmd == "translate-curated":
        from . import identity, llm, store
        ids = sorted({r["passage_id"] for r in identity.rows() if r.get("passage_id")})
        ps = [p for p in store.passages(ids) if p["lang"] in ("grc", "lat") and p["text"]]

        async def go():
            for i, p in enumerate(ps, 1):
                if llm.cached("translate", p["text"]):
                    continue
                await llm.translate(p)
                print(f"  {i}/{len(ps)} {p['cite']}")
        asyncio.run(go())


if __name__ == "__main__":
    main()
