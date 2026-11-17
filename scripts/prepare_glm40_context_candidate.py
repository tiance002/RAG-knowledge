"""Prepare an offline NEW_CONTEXT arm from an allowlisted frozen runtime scope.

Does not call an API, load evaluation answers, or use catalog item_ids. Source
mirrors are read in place; source selection uses runtime citations or literal
source names/document links in runtime text only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.frozen_context_retrieval import complete_context, terms


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_source(path: Path):
    """Return deterministic extracted text; no source copies or dependency rebuilds."""
    if path.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(path) as reader:
            return "\n\n".join(f"# Page {i + 1}\n" + "\n\n".join(b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0) for i, page in enumerate(reader)), "pymupdf_sorted_text_blocks"
    if path.suffix.lower() == ".xlsx":
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        blocks = []
        with zipfile.ZipFile(path) as z:
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                shared = ["".join(x.itertext()) for x in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
            names = [x.attrib.get("name", "") for x in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", ns)]
            worksheets = sorted((x for x in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x)), key=lambda x: int(re.search(r"sheet(\d+)", x).group(1)))
            for i, name in enumerate(worksheets):
                rows = []
                for row in ET.fromstring(z.read(name)).findall("m:sheetData/m:row", ns):
                    values = []
                    for cell in row.findall("m:c", ns):
                        v = cell.find("m:v", ns)
                        value = v.text or "" if v is not None else "".join(cell.find("m:is", ns).itertext()) if cell.find("m:is", ns) is not None else ""
                        if cell.attrib.get("t") == "s" and value:
                            value = shared[int(value)]
                        if value:
                            values.append(cell.attrib.get("r", "") + "=" + value)
                    if values:
                        rows.append("\t".join(values) + "\t")
                blocks.append("# Sheet " + (names[i] if i < len(names) else name) + "\n" + "\n".join(rows))
        return "\n\n".join(blocks), "xlsx_raw_cell_values_with_cell_coordinates"
    if path.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("unsupported source format " + path.suffix)
    return path.read_text(encoding="utf-8-sig"), "utf8_source_text"


def catalog_records(catalog):
    # Deliberately project only source metadata, excluding per-item associations.
    result = []
    for raw in catalog.get("files", []) + catalog.get("additional_files", []):
        path = raw.get("path", "")
        if Path(path).suffix.lower() not in {".txt", ".md", ".pdf", ".xlsx"}:
            continue
        result.append({"source": path, "local_path": raw.get("local_path"),
                       "sha256": raw.get("local_sha256") or raw.get("remote_sha256"),
                       "name": path.rsplit("/", 1)[-1]})
    return result


def resolve_documents(*, question, messages, citations, catalog, cache):
    authorized = {}
    unresolved = []
    runtime_text = question + "\n" + "\n".join(str(m.get("content", "")) for m in messages)
    for number, citation in enumerate(citations, 1):
        metadata = citation.get("source_metadata") or {}
        anchor_hash = metadata.get("source_sha256")
        identity = str(metadata.get("qa500_source_identity", ""))
        source_id = re.search(r"dsid_[a-z0-9]+", identity)
        name = citation.get("fileName", "")
        matches = [r for r in catalog if (anchor_hash and r["sha256"] == anchor_hash)
                   or (source_id and source_id.group() in r["name"])
                   or (name and r["name"] == name)]
        if not matches:
            unresolved.append({"anchor": name or anchor_hash, "reason": "No matching local raw-source mirror"})
        for r in matches:
            authorized.setdefault(r["source"], (r, {"kind": "runtime_citation", "anchor_hash": anchor_hash, "anchor_name": name}, number))
    # Literal filename/dsid references are allowed; broad corpus lexical search is not.
    for r in catalog:
        dsid = re.search(r"dsid_[a-z0-9]+", r["name"])
        if r["name"] in runtime_text or (dsid and dsid.group() in runtime_text):
            authorized.setdefault(r["source"], (r, {"kind": "literal_runtime_document_reference", "match": dsid.group() if dsid else r["name"]}, None))
    docs = []
    if not authorized:
        # A genuine runtime retrieval fallback: rare lexical matches in raw corpus,
        # driven by prior USER questions for pronoun follow-ups, not answer history.
        user_text = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        query = terms(question + " " + user_text) - {"previous", "above", "data", "question", "preparation", "factor", "yield", "table", "answer", "please"}
        corpus = []
        for r in catalog:
            try:
                if r["source"] not in cache:
                    path = Path(r["local_path"])
                    digest = sha(path.read_bytes())
                    if r["sha256"] and digest != r["sha256"]:
                        continue
                    text, extraction = read_source(path)
                    cache[r["source"]] = (text, extraction, digest)
                corpus.append((r, query & terms(cache[r["source"]][0])))
            except Exception:
                continue
        frequency = {t: sum(t in matches for _, matches in corpus) for t in query}
        ranked = []
        for r, matches in corpus:
            salient = {t for t in matches if len(t) >= 4 and frequency[t] <= max(2, len(corpus) // 12)}
            if salient:
                score = sum(1 / frequency[t] for t in salient)
                ranked.append((score, r["source"], r, sorted(salient)))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        for score, _, r, matches in ranked[:2]:
            if score >= .5 * ranked[0][0]:
                authorized[r["source"]] = (r, {"kind": "question_prior_user_lexical_match", "salient_matches": matches, "score": score}, None)
    pending = list(authorized.values())
    seen = set()
    while pending:
        r, provenance, label = pending.pop(0)
        if r["source"] in seen:
            continue
        seen.add(r["source"])
        try:
            path = Path(r["local_path"])
            if r["source"] not in cache:
                digest = sha(path.read_bytes())
                if r["sha256"] and digest != r["sha256"]:
                    raise ValueError("source hash mismatch")
                text, extraction = read_source(path)
                cache[r["source"]] = (text, extraction, digest)
            text, extraction, digest = cache[r["source"]]
            docs.append(dict(source=r["source"], text=text, sha256=digest, provenance=provenance, original_label=label, extraction=extraction))
            # Follow only explicit source-document identifiers/names in anchored text.
            for linked in catalog:
                dsid = re.search(r"dsid_[a-z0-9]+", linked["name"])
                if linked["source"] not in seen and (linked["name"] in text or (dsid and dsid.group() in text)):
                    pending.append((linked, {"kind": "explicit_document_reference", "from_source": r["source"], "match": dsid.group() if dsid else linked["name"]}, None))
        except Exception as exc:
            unresolved.append({"source": r["source"], "reason": type(exc).__name__ + ": " + str(exc)})
    return docs, unresolved


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scope", required=True)
    p.add_argument("--runtime-root", required=True)
    p.add_argument("--catalog", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    scope = load(a.scope)
    allowed, excluded = set(scope["included_ids"]), set(scope["excluded_ids"])
    if allowed & excluded or len(allowed) != 40:
        raise ValueError("scope must contain 40 included items disjoint from excluded items")
    root, out = Path(a.runtime_root), Path(a.output)
    if out.resolve().drive.upper() != "E:":
        raise ValueError("candidate artifacts must be written to the authorized E: task root")
    out.mkdir(parents=True, exist_ok=True)
    (out / "requests").mkdir(exist_ok=True)
    # Filtering by identifier occurs before accessing any request/citation/question body.
    manifest = [e for e in load(root / "requests-manifest.json") if e.get("item_id") in allowed and e.get("item_id") not in excluded]
    seeds = {e["item_id"]: e for e in load(root / "context-seeds.json") if e.get("item_id") in allowed and e.get("item_id") not in excluded}
    if {e["item_id"] for e in manifest} != allowed or len(manifest) != 40:
        raise ValueError("runtime request manifest does not exactly cover the scoped 40")
    catalog, cache, results = catalog_records(load(a.catalog)), {}, []
    for index, entry in enumerate(manifest):
        started = time.perf_counter()
        raw = (root / entry["request_path"]).read_bytes()
        request = json.loads(raw)
        canonical_hash = sha(json.dumps(request, ensure_ascii=False, sort_keys=True).encode())
        if canonical_hash != entry["request_sha256"]:
            raise ValueError("frozen request hash mismatch: " + entry["item_id"])
        citations = entry.get("citations") or seeds.get(entry["item_id"], {}).get("citations") or []
        docs, unresolved = resolve_documents(question=entry["question"], messages=request["messages"], citations=citations, catalog=catalog, cache=cache)
        result = complete_context(question=entry["question"], messages=request["messages"], documents=docs)
        request["messages"] = result.pop("messages")
        result["unresolved"].extend(unresolved)
        dest = out / "requests" / f"{index:03d}.json"
        payload = json.dumps(request, ensure_ascii=False, indent=2).encode()
        dest.write_bytes(payload)
        results.append(dict(item_id=entry["item_id"], request_path=str(dest), request_sha256=sha(payload), baseline_request_sha256=canonical_hash, baseline_file_sha256=sha(raw),
                            preparation_seconds=time.perf_counter() - started, resolved_documents=len(docs), original_citations=citations, **result))
    report = dict(schema="glm40-new-context/v1", arm="NEW_CONTEXT", identical_baseline_input=False,
                  runtime_gold_access=False, source_selection="runtime anchors / explicit references; zero-anchor rare question+prior-user lexical fallback; no item-id mapping",
                  count=len(results), resolved_documents=sum(r["resolved_documents"] for r in results), unique_sources_read=len(cache),
                  cases_with_supplement=sum(bool(r["added_chars"]) for r in results), added_chars=sum(r["added_chars"] for r in results), cases=results)
    (out / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "cases"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
