"""Corpus registry and refresh (download from canonical repositories → parse → index)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .. import settings, store
from . import perseus_pliny, tcp, wellmann

PARSERS = {"wellmann": wellmann.parse, "perseus_pliny": perseus_pliny.parse, "tcp": tcp.parse}


def corpus_specs() -> list[dict]:
    c = settings.config()
    specs = [dict(x, kind=x.get("kind", "antiquity")) for x in c["corpora"]]
    for e in settings.tcp_entries():
        specs.append({
            "key": f"tcp:{e['tcp']}", "parser": "tcp", "tcp": e["tcp"], "title": e["title"],
            "author": e.get("author", ""), "year": e.get("year", 0), "lang": "enm", "kind": e.get("kind", "text"),
            "licence": c.get("tcp_licence"), "edition": f"EEBO-TCP {e['tcp']}",
            "files": {"main": c["tcp_url"].format(tcp=e["tcp"])},
        })
    return specs


def _download(url: str, dest: Path, etag: str | None, log) -> tuple[bytes, str | None, bool]:
    headers = {"User-Agent": settings.config()["http"]["user_agent"]}
    if etag and dest.exists():
        headers["If-None-Match"] = etag
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        r = c.get(url, headers=headers)
    if r.status_code == 304 and dest.exists():
        return dest.read_bytes(), etag, False
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return r.content, r.headers.get("ETag"), True


def refresh(only: list[str] | None = None, force: bool = False, log=print) -> list[dict]:
    """Pull every configured corpus; re-parse only those whose files changed (or with force)."""
    known = {r["key"]: r for r in store.source_rows()}
    report = []
    for spec in corpus_specs():
        key = spec["key"]
        if only and key not in only and key.replace("tcp:", "") not in only:
            continue
        t0 = time.time()
        old_etags = json.loads(known.get(key, {}).get("etags") or "{}")
        files, etags, changed = {}, {}, False
        try:
            for name, url in spec["files"].items():
                dest = settings.RAW / key.replace(":", "_") / f"{name}.xml"
                data, et, ch = _download(url, dest, None if force else old_etags.get(name), log)
                files[name], etags[name] = data, et
                changed = changed or ch
            if not changed and key in known and not force:
                report.append({"key": key, "status": "unchanged", "n": known[key]["n_passages"]})
                log(f"  = {key:16s} unchanged ({known[key]['n_passages']} passages)")
                continue
            parser = PARSERS[spec["parser"]]
            arg = files["main"] if list(files) == ["main"] else files
            passages = parser(arg, spec)
            store.replace_source(key, passages, dict(spec, etags=etags))
            report.append({"key": key, "status": "indexed", "n": len(passages), "s": round(time.time() - t0, 1)})
            log(f"  + {key:16s} {len(passages):6d} passages  ({time.time() - t0:.1f}s)")
        except Exception as e:
            report.append({"key": key, "status": "error", "error": f"{type(e).__name__}: {e}"})
            log(f"  ! {key:16s} {type(e).__name__}: {e}")
    return report
