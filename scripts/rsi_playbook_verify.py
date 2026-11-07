"""Read-only checks for the proposed RSI Playbook v1 JSONL format."""
import hashlib
import json
import argparse
from pathlib import Path

REQUIRED = ("id", "revision", "problem", "conditions", "observation", "strategy", "evidence", "counterexamples", "confidence", "status", "usage_count", "success_count", "previous_sha256")


def digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_jsonl(path):
    """Read complete records independently; report corruption without writing."""
    records, errors = [], []
    with Path(path).open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                errors.append(f"line {line_no}: incomplete trailing line")
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
                if not isinstance(obj, dict):
                    raise ValueError("expected object")
                records.append((line_no, obj))
            except (UnicodeDecodeError, ValueError) as exc:
                errors.append(f"line {line_no}: invalid JSON object: {exc}")
    return records, errors


def verify(path, trusted_sha256=None):
    if trusted_sha256 is not None:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual != trusted_sha256:
            return ["file SHA-256 does not match trusted checkpoint"]
    records, errors = read_jsonl(path)
    latest = {}
    for line, item in records:
        error_count = len(errors)
        prefix = f"line {line}: "
        missing = [key for key in REQUIRED if key not in item]
        if missing:
            errors.append(prefix + "missing fields: " + ", ".join(missing))
            continue
        ident, rev = item["id"], item["revision"]
        if not isinstance(ident, str) or not ident.strip() or type(rev) is not int or rev < 1:
            errors.append(prefix + "invalid id or revision")
            continue
        prior = latest.get(ident)
        if prior is None:
            if rev != 1 or item["previous_sha256"] is not None or item["status"] != "candidate":
                errors.append(prefix + "first revision must be candidate revision 1 with null predecessor")
        elif rev != prior["revision"] + 1 or item["previous_sha256"] != digest(prior):
            errors.append(prefix + "revision sequence or predecessor hash mismatch")
        if item.get("schema_version") != "ace-playbook/v1":
            errors.append(prefix + "invalid schema_version")
        for key in ("problem", "observation", "strategy"):
            if not isinstance(item[key], str) or not item[key].strip():
                errors.append(prefix + f"{key} must be nonempty")
        for key in ("conditions", "counterexamples"):
            if not isinstance(item[key], list) or any(not isinstance(x, str) or not x.strip() for x in item[key]):
                errors.append(prefix + f"{key} must be a list of nonempty strings")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
            errors.append(prefix + "evidence must contain references")
        status = item["status"]
        if status not in ("candidate", "validated", "deprecated"):
            errors.append(prefix + "invalid status")
        if prior and (prior["status"], status) not in (("candidate", "candidate"), ("candidate", "validated"), ("candidate", "deprecated"), ("validated", "validated"), ("validated", "deprecated"), ("deprecated", "deprecated")):
            errors.append(prefix + "invalid status transition")
        if status == "validated":
            receipt = item.get("independent_validation")
            if not isinstance(receipt, dict) or any(not isinstance(receipt.get(k), str) or not receipt[k].strip() for k in ("reviewer", "evidence_ref", "result")) or receipt.get("result") != "PASS" or not isinstance(evidence, list) or receipt.get("evidence_ref") in evidence:
                errors.append(prefix + "validated requires independent PASS receipt and distinct evidence reference")
        if item["confidence"] not in ("low", "medium", "high"):
            errors.append(prefix + "invalid confidence")
        usage, success = item["usage_count"], item["success_count"]
        if type(usage) is not int or type(success) is not int or not 0 <= success <= usage:
            errors.append(prefix + "invalid counters")
        elif prior and (type(prior["usage_count"]) is int and type(prior["success_count"]) is int) and (usage < prior["usage_count"] or success < prior["success_count"]):
            errors.append(prefix + "counters must not decrease")
        if len(errors) == error_count:
            latest[ident] = item
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--trusted-sha256", help="externally pinned whole-file SHA-256")
    args = parser.parse_args()
    try:
        errors = verify(args.path, args.trusted_sha256)
    except OSError as exc:
        parser.exit(2, f"cannot read file: {exc}\n")
    if errors:
        for error in errors:
            print(error)
        return 1
    print("PASS: Playbook format checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
