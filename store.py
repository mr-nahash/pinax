"""SQLite store: passages + FTS5 index over folded text, source metadata, HTTP and LLM caches."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import settings
from .textnorm import fold

SCHEMA = """
CREATE TABLE IF NOT EXISTS passages (
  id TEXT PRIMARY KEY, source TEXT, work TEXT, author TEXT, year INTEGER, lang TEXT,
  cite TEXT, title TEXT, text TEXT, translation TEXT, translation_note TEXT, alt_text TEXT,
  notes TEXT, meta TEXT, url TEXT);
CREATE INDEX IF NOT EXISTS passages_source ON passages(source);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
  id UNINDEXED, source UNINDEXED, title, body, tokenize = "unicode61 remove_diacritics 2");
CREATE TABLE IF NOT EXISTS sources (
  key TEXT PRIMARY KEY, kind TEXT, title TEXT, author TEXT, year INTEGER, lang TEXT,
  licence TEXT, edition TEXT, files TEXT, etags TEXT, fetched_at REAL, n_passages INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, fetched_at REAL, status INTEGER, body BLOB);
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, task TEXT, model TEXT, created REAL, value TEXT);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        settings.DATA.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(settings.DB_PATH, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        _local.con = con
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise


def replace_source(key: str, passages: list, meta: dict) -> None:
    with tx() as con:
        con.execute("DELETE FROM passages_fts WHERE source = ?", (key,))
        con.execute("DELETE FROM passages WHERE source = ?", (key,))
        for p in passages:
            r = p.to_row()
            con.execute(
                "INSERT OR REPLACE INTO passages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["source"], r["work"], r["author"], r["year"], r["lang"], r["cite"], r["title"],
                 r["text"], r["translation"], r["translation_note"], r["alt_text"],
                 json.dumps(r["notes"], ensure_ascii=False), json.dumps(r["meta"], ensure_ascii=False), r["url"]))
            body = " ".join(x for x in (r["text"], r["translation"] or "", " ".join(r["notes"])) if x)
            con.execute("INSERT INTO passages_fts (id, source, title, body) VALUES (?,?,?,?)",
                        (r["id"], key, fold(r["title"] or ""), fold(body)))
        con.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, meta.get("kind"), meta.get("title"), meta.get("author"), meta.get("year"), meta.get("lang"),
             meta.get("licence"), meta.get("edition"), json.dumps(meta.get("files", {})),
             json.dumps(meta.get("etags", {})), time.time(), len(passages), "ok"))


def source_rows() -> list[dict]:
    return [dict(r) for r in connect().execute("SELECT * FROM sources ORDER BY year")]


def passage(pid: str) -> dict | None:
    r = connect().execute("SELECT * FROM passages WHERE id = ?", (pid,)).fetchone()
    return _row(r) if r else None


def passages(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = {r["id"]: _row(r) for r in connect().execute(f"SELECT * FROM passages WHERE id IN ({q})", ids)}
    return [rows[i] for i in ids if i in rows]


def _row(r) -> dict:
    d = dict(r)
    d["notes"] = json.loads(d["notes"] or "[]")
    d["meta"] = json.loads(d["meta"] or "{}")
    return d


def fts(match: str, limit: int = 200, sources: list[str] | None = None) -> list[tuple[str, float, str]]:
    """Return (id, bm25 score, snippet) for a MATCH expression over folded title+body."""
    if not match:
        return []
    sql = ("SELECT id, bm25(passages_fts, 0, 0, 4.0, 1.0) AS s, "
           "snippet(passages_fts, 3, '⟦', '⟧', ' … ', 24) FROM passages_fts WHERE passages_fts MATCH ?")
    args: list = [match]
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        args += sources
    sql += " ORDER BY s LIMIT ?"
    args.append(limit)
    try:
        return [(r[0], r[1], r[2]) for r in connect().execute(sql, args)]
    except sqlite3.OperationalError:
        return []


def cache_get(table: str, key: str, ttl: float | None = None):
    col = "fetched_at" if table == "http_cache" else "created"
    keycol = "url" if table == "http_cache" else "key"
    r = connect().execute(f"SELECT * FROM {table} WHERE {keycol} = ?", (key,)).fetchone()
    if not r:
        return None
    if ttl is not None and time.time() - r[col] > ttl:
        return None
    return dict(r)


def http_cache_put(url: str, status: int, body: bytes) -> None:
    with tx() as con:
        con.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?)", (url, time.time(), status, body))


def llm_cache_put(key: str, task: str, model: str, value) -> None:
    with tx() as con:
        con.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?)",
                    (key, task, model, time.time(), json.dumps(value, ensure_ascii=False)))
