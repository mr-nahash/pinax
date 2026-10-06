from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("PINAX_HOME", Path(__file__).resolve().parents[1]))
CONFIG = Path(os.environ.get("PINAX_CONFIG", ROOT / "config" / "sources.yaml"))
DATA = Path(os.environ.get("PINAX_DATA", ROOT / "data"))
DB_PATH = DATA / "pinax.db"
RAW = DATA / "raw"
CURATION = ROOT / "curation"
WEB = ROOT / "web"


@lru_cache
def config() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def tcp_entries() -> list[dict]:
    c = config()
    entries = list(c["tcp"]["core"])
    if c.get("tcp_profile") == "extended":
        entries += c["tcp"]["extended"]
    extra = CURATION / "tcp_added.yaml"
    if extra.exists():
        entries += yaml.safe_load(extra.read_text(encoding="utf-8")) or []
    seen, out = set(), []
    for e in entries:
        if e["tcp"] not in seen:
            seen.add(e["tcp"])
            out.append(e)
    return out


def llm_model() -> str:
    return os.environ.get("PINAX_LLM_MODEL") or config()["llm"]["model"]


def llm_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("PINAX_LLM", "on") != "off"
