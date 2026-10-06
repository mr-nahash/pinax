"""Constituents of a taxon (Wikidata 'found in taxon', largely LOTUS-curated) joined to odorant-receptor
responses from M2OR (Lalis et al., NAR 2023) by InChIKey."""
from __future__ import annotations

import csv
import io
import json
from functools import lru_cache

import httpx

from .. import settings
from ..http import Trace
from .names import sparql

M2OR_INDEX = settings.DATA / "m2or_index.json"


async def constituents(names: list[str], trace: Trace) -> list[dict]:
    vals = " ".join(f'"{n}"' for n in names[:25])
    q = f"""SELECT ?c ?cLabel ?ik ?smi ?cid (COUNT(DISTINCT ?ref) AS ?nrefs) (SAMPLE(?ref) AS ?aref) WHERE {{
      VALUES ?tname {{ {vals} }}
      ?t wdt:P225 ?tname . ?c p:P703 ?st . ?st ps:P703 ?t .
      OPTIONAL {{ ?st prov:wasDerivedFrom/pr:P248 ?ref }}
      OPTIONAL {{ ?c wdt:P235 ?ik }} OPTIONAL {{ ?c wdt:P233 ?smi }} OPTIONAL {{ ?c wdt:P662 ?cid }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} GROUP BY ?c ?cLabel ?ik ?smi ?cid LIMIT 800"""
    b = await sparql(q, "Wikidata/LOTUS constituents", trace)
    if b is None:
        return []
    m2or = m2or_index()
    out, seen = [], set()
    for r in b:
        qid = r["c"]["value"].rsplit("/", 1)[-1]
        if qid in seen:
            continue
        seen.add(qid)
        ik = r.get("ik", {}).get("value")
        rec = {"qid": qid, "name": r.get("cLabel", {}).get("value", qid), "inchikey": ik,
               "smiles": r.get("smi", {}).get("value"), "pubchem_cid": r.get("cid", {}).get("value"),
               "n_refs": int(r.get("nrefs", {}).get("value", 0)),
               "ref": r.get("aref", {}).get("value", "").rsplit("/", 1)[-1] or None}
        if ik and m2or:
            hit = m2or.get(ik)
            match = "exact"
            if not hit:
                hit = m2or.get("~" + ik[:14])
                match = "connectivity (stereo-agnostic)"
            if hit:
                rec["m2or"] = dict(hit, match=match)
        out.append(rec)
    out.sort(key=lambda x: (-(len(x.get("m2or", {}).get("agonist_of", [])) > 0), -x["n_refs"], x["name"]))
    return out


@lru_cache
def m2or_index() -> dict:
    if M2OR_INDEX.exists():
        return json.loads(M2OR_INDEX.read_text())
    return {}


def build_m2or_index(log=print) -> int:
    """Download M2OR and reduce it to InChIKey → receptors with an agonist response / tested."""
    url = settings.config()["chemistry"]["m2or_csv"]
    with httpx.Client(timeout=180, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text), delimiter=";")
    idx: dict[str, dict] = {}
    for row in reader:
        ik = (row.get("InChI Key") or "").strip()
        if not ik or row.get("Mixture", "mono") != "mono":
            continue
        rec = idx.setdefault(ik, {"name": row.get("Name"), "tested": set(), "agonist_of": set(), "dois": set()})
        rcp = (row.get("Gene ID") or row.get("Uniprot ID") or "").strip()
        sp = (row.get("species") or "").strip()
        tag = f"{rcp}" + ("" if sp in ("homo sapiens", "") else f" ({sp})")
        rec["tested"].add(tag)
        if str(row.get("Responsive")).strip() == "1":
            rec["agonist_of"].add(tag)
        if row.get("DOI"):
            rec["dois"].add(row["DOI"].strip())
    out = {}
    for ik, v in idx.items():
        val = {"name": v["name"], "n_tested": len(v["tested"]), "agonist_of": sorted(v["agonist_of"]),
               "dois": sorted(v["dois"])[:5]}
        out[ik] = val
        out.setdefault("~" + ik[:14], val)
    settings.DATA.mkdir(parents=True, exist_ok=True)
    M2OR_INDEX.write_text(json.dumps(out))
    m2or_index.cache_clear()
    log(f"  + m2or            {len(idx):6d} molecules indexed")
    return len(idx)
