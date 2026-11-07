"""Disjoint A/D eligibility supplement; never reruns a primary experiment qid.

The immutable primary ledger retains every UNKNOWN reservation. Only the
remaining share of the original one-yuan budget is available to this run.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

BASE_PATH = Path(__file__).with_name("m2_preference_ab.py")
MODULE = importlib.util.spec_from_file_location("m2_supplement_base", BASE_PATH)
base = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(base)
SCHEMA = "m2-preference-supplement/v1"
ARMS = {"A": {}, "D": {"output_preferences": ["step_by_step"]}}


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Finite nonnegative monetary value required")
    return result


def verify_parent(spec):
    """Read only: verify exact original artifacts and independently total costs."""
    manifest_path = base.private_path(spec["parent_manifest_path"])
    output = base.private_path(spec["parent_output"])
    paths = {"manifest": manifest_path, "summary": output / "summary.json", "ledger": output / "provider-ledger.jsonl"}
    for role, path in paths.items():
        if base.digest(path.read_bytes()) != spec[f"parent_{role}_sha256"]:
            raise ValueError("Immutable primary artifact changed")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8-sig"))
    base.validate_manifest(manifest)
    summary = json.loads(paths["summary"].read_bytes())
    if summary.get("status") not in ("RECORDED_REQUIRES_EVIDENCE_REVIEW", "STOPPED", "FAILED"):
        raise ValueError("Primary run must have a terminal summary before supplement")
    rows = [json.loads(row) for row in paths["ledger"].read_bytes().splitlines() if row.strip()]
    latest = {}
    for row in rows:
        call_id = row.get("call_id")
        if type(call_id) is not int or call_id <= 0 or row.get("state") not in ("UNKNOWN_PENDING", "UNKNOWN", "COMPLETED"):
            raise ValueError("Malformed primary attempt ledger")
        old = latest.get(call_id)
        if old is not None and old.get("state") != "UNKNOWN_PENDING" and row != old:
            raise ValueError("Primary terminal attempt was rewritten")
        if old is None and row["state"] != "UNKNOWN_PENDING":
            raise ValueError("Primary attempt lacks durable initial reservation")
        if old and old.get("reservation_cny") != row.get("reservation_cny"):
            raise ValueError("Primary reservation changed")
        latest[call_id] = row
    if set(latest) != set(range(1, len(latest) + 1)):
        raise ValueError("Primary attempt identities are not contiguous")
    rates = manifest["pricing"]
    def cost(prompt, output):
        return (Decimal(prompt) * decimal(rates["input_cny_per_million"]) +
                Decimal(output) * decimal(rates["output_cny_per_million"])) / Decimal(1000000)
    committed, known = Decimal(0), Decimal(0)
    qids = {case["qid"] for case in manifest["cases"]}
    for row in latest.values():
        if row.get("qid") not in qids or row.get("requested_model") != manifest["model"]:
            raise ValueError("Primary request identity mismatch")
        input_bound, output_bound = row.get("input_token_bound"), row.get("output_token_bound")
        if (type(input_bound) is not int or type(output_bound) is not int or input_bound < 0
                or output_bound != manifest["max_output_tokens"]):
            raise ValueError("Primary token reservation bounds invalid")
        reserve = decimal(row["reservation_cny"])
        if reserve != cost(input_bound, output_bound):
            raise ValueError("Primary reservation is not consistent with frozen price")
        if row["state"] == "COMPLETED":
            usage = row.get("usage", {})
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if (type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0
                    or prompt > input_bound or completion > output_bound or row.get("actual_model") != manifest["model"]):
                raise ValueError("Primary completed usage invalid")
            actual = cost(prompt, completion)
            if actual != decimal(row["price_based_cost_cny"]):
                raise ValueError("Primary actual price calculation mismatch")
            known += actual
            committed += actual
        else:
            committed += reserve
    limit = decimal(manifest["budget_cny"])
    if (limit > 1 or decimal(summary["budget_cny"]) != limit
            or decimal(summary["committed_including_unknown_cny"]) != committed
            or decimal(summary["price_based_known_cost_cny"]) != known
            or summary.get("actual_http_calls") != len(latest) or committed > limit):
        raise ValueError("Primary final summary and independent ledger accounting disagree")
    return manifest, {"parent_committed_cny": str(committed), "parent_known_cost_cny": str(known),
                      "remaining_cny": str(limit - committed), "global_limit_cny": str(limit),
                      "parent_unknown_attempts": sum(row["state"] != "COMPLETED" for row in latest.values()),
                      "parent_attempts": len(latest), "parent_bill": "UNKNOWN"}


def policy_resolution(question, preferences):
    # Importing app.core.config also runs app.core.__init__, which eagerly
    # creates database.engine. Never import any app module in parent preflight.
    code = r'''
import hashlib,json,socket,sys
class ForbiddenNetwork(BaseException):pass
def deny(*a,**kw):raise ForbiddenNetwork('Policy preflight has no network permission')
socket.socket.connect=deny;socket.socket.connect_ex=deny
sys.path.insert(0,sys.argv[1])
from app.services.memory import PreferencesSnapshot,PreferenceValues
from app.services.memory_commands import resolve_display_preferences
from app.core.database import engine
assert str(engine.url)=='sqlite://'
data=json.load(sys.stdin);preferences=data['preferences']
snapshot=PreferencesSnapshot(values=PreferenceValues(**preferences),sources={
 'answer_style':'default','knowledge_level':'default','output_preferences':'explicit' if preferences else 'default'})
policy,effective,sources=resolve_display_preferences(snapshot,data['question'])
print(json.dumps({'stored':snapshot.model_dump(),'effective_values':effective,'effective_sources':sources,
 'policy':policy,'policy_active':bool(policy),'policy_sha256':hashlib.sha256(policy.encode()).hexdigest()},ensure_ascii=False))
'''
    environment = {**os.environ, 'DATABASE_URL': 'sqlite://', 'ENV': 'test',
                   'REDIS_URL': 'redis://127.0.0.1:1/0', 'MEMORY_REDIS_URL': 'redis://127.0.0.1:1/0',
                   'RAG_OBSERVABILITY_ENABLED': 'false', 'LANGFUSE_ENABLED': 'false', 'PYTHONIOENCODING': 'utf-8'}
    completed = subprocess.run([sys.executable, '-c', code, str(BASE_PATH.parents[1] / 'backend')],
                               input=json.dumps({'question':question,'preferences':preferences}),
                               capture_output=True, text=True, encoding='utf-8', timeout=30, env=environment)
    if completed.returncode != 0:
        raise ValueError('Isolated preference preflight failed; no app modules loaded in parent')
    return json.loads(completed.stdout)


def assert_runtime_isolation(output, redis_url):
    """Call only after configure_runtime and before ANY application writes."""
    from app.core.database import engine
    from app.core.config import get_settings
    from app.services.memory import get_memory_service
    settings = get_settings()
    expected_db = (Path(output) / 'isolated.db').resolve()
    if (not engine.url.drivername.startswith('sqlite') or not engine.url.database
            or Path(engine.url.database).resolve() != expected_db or settings.ENV != 'test'
            or settings.MEMORY_REDIS_URL != redis_url or settings.REDIS_URL != redis_url):
        raise RuntimeError('Actual cached database/Redis configuration is not owned by this run')
    connection = get_memory_service().client.connection_pool.connection_kwargs
    expected_redis = urlsplit(redis_url)
    if (connection.get('host') != expected_redis.hostname or connection.get('port') != expected_redis.port
            or connection.get('password') != expected_redis.password or connection.get('db', 0) != 0):
        raise RuntimeError('Actual preference Redis client is outside owned runtime')
    result = {'status':'PASS','database_path':str(expected_db),'environment':'test',
              'redis_host':connection['host'],'redis_port':connection['port'],'redis_db':0}
    (Path(output) / 'runtime-isolation.json').write_bytes(base.canonical(result))
    return result


def verify_zero_send_retry(spec):
    previous = spec.get('zero_send_retry_of')
    if not previous:
        return
    output = base.private_path(previous['output'])
    path = output / 'summary.json'
    if base.digest(path.read_bytes()) != previous['summary_sha256']:
        raise ValueError('Failed-attempt retry summary changed')
    summary = json.loads(path.read_bytes())
    old_spec = json.loads((output / 'supplement-manifest.json').read_bytes())
    if (summary.get('status') not in ('FAILED','STOPPED') or summary.get('actual_http_calls') != 0
            or decimal(summary.get('committed_including_unknown_cny',1)) != 0
            or old_spec.get('authorization_id') == spec['authorization_id']
            or old_spec.get('parent_manifest_sha256') != spec['parent_manifest_sha256']):
        raise ValueError('Retry requires a new authorization and proven zero prior sends/commitment')
    ledger = output / 'provider-ledger.jsonl'
    if ledger.exists() and ledger.read_bytes().strip():
        raise ValueError('Prior attempt ledger is nonempty; do not retry')


def validate_spec(spec):
    if (spec.get("schema_version") != SCHEMA or spec.get("authorized") is not True
            or not spec.get("authorization_id") or spec.get("purpose") != "D_ELIGIBILITY_ONLY"
            or spec.get("repetitions") != 2):
        raise ValueError("Explicitly authorized versioned D-eligibility supplement required")
    manifest, budget = verify_parent(spec)
    verify_zero_send_retry(spec)
    case = spec["case"]
    if (case.get("qid") in {row["qid"] for row in manifest["cases"]}
            or not case.get("qid", "").startswith("m2-supp:")
            or case.get("question") in {row["question"] for row in manifest["cases"]}
            or case.get("kb_scope") != [base.KB_ID] or case.get("negative") is not False
            or not case.get("gold_locator") or not case.get("expected_chunk_ids")
            or not case.get("answer_points") or not case.get("source_quote")):
        raise ValueError("A new disjoint procedural case with source/Gold evidence is required")
    chunks = {row["id"]: row for row in manifest["chunks"]}
    if not set(case["expected_chunk_ids"]) <= set(chunks):
        raise ValueError("Supplement qrel is outside frozen primary corpus")
    evidence = "\n".join(chunks[chunk_id]["content"] for chunk_id in case["expected_chunk_ids"])
    if (case["source_quote"] not in evidence
            or any(point.lower() not in case["source_quote"].lower() for point in case["answer_points"])):
        raise ValueError("Gold measurements must occur in the exact frozen source quote")
    resolutions = {arm: policy_resolution(case["question"], prefs) for arm, prefs in ARMS.items()}
    if resolutions["A"]["policy_active"] or resolutions["D"]["effective_values"]["output_preferences"] != ["step_by_step"]:
        raise ValueError("Question must activate saved D policy while baseline A remains default")
    return {"status": "PASS" if decimal(budget["remaining_cny"]) > 0 else "BUDGET_EXHAUSTED",
            "purpose": "D_ELIGIBILITY_ONLY", "planned_trials": 8, "model_calls": 0,
            "arms": ARMS, "modes": list(base.MODES), "repetitions": 2, "budget": budget,
            "case_qid": case["qid"], "policy_resolutions": resolutions,
            "scope": "Separate A/D applicability check; cannot establish the original four-arm effect",
            "runner_sha256": base.digest(Path(__file__).read_bytes()),
            "base_runner_sha256": base.digest(BASE_PATH.read_bytes())}


class ParentAwareTransport(base.BudgetTransport):
    def __init__(self, manifest, output, spec, *, transport=None):
        super().__init__(manifest, output, transport=transport)
        self.parent_spec = spec
    async def send(self, *args, **kwargs):
        try:
            _, budget = verify_parent(self.parent_spec)
            if decimal(budget["remaining_cny"]) != self.limit:
                raise ValueError("Remaining budget changed")
        except Exception:
            self.stopped = True
            raise base.GuardStop("Immutable primary evidence/budget changed; no supplemental send") from None
        return await super().send(*args, **kwargs)


def supplement_plan(manifest):
    for mode in base.MODES:
        for repetition in (1, 2):
            for arm, preferences in ARMS.items():
                yield manifest["cases"][0], mode, repetition, arm, preferences


def audit_delivery(output, spec):
    """Stored != applicable != delivered != response actually obeyed."""
    output = Path(output)
    trials_path = output / "trials.jsonl"
    trials = [json.loads(row) for row in trials_path.read_bytes().splitlines()] if trials_path.exists() else []
    rows = []
    for trial in trials:
        resolution = policy_resolution(spec["case"]["question"], ARMS[trial["arm"]])
        delivered = []
        for call_id in trial["provider_call_ids"]:
            payload = json.loads((output / f"request-{call_id:04d}.json").read_bytes())
            has_policy = bool(resolution["policy"]) and any(
                msg.get("role") == "system" and resolution["policy"] in str(msg.get("content", ""))
                for msg in payload.get("messages", []))
            delivered.append({"call_id": call_id, "policy_delivered": has_policy,
                              "tools_bound": bool(payload.get("tools")), "streaming": bool(payload.get("stream"))})
        rows.append({key: trial[key] for key in ("qid", "arm", "mode", "repetition", "state")} | {
            "stored_preferences": trial.get("preferences"), "resolved": resolution,
            "provider_deliveries": delivered,
            "any_policy_delivered": any(row["policy_delivered"] for row in delivered),
            "response_followed_steps": "REQUIRES_INDEPENDENT_ANSWER_REVIEW"})
    (output / "policy-delivery.json").write_bytes(base.canonical(rows))
    return rows


def execute(spec, output):
    preflight = validate_spec(spec)
    if preflight["status"] != "PASS":
        return preflight
    if not os.getenv("M2_AB_API_KEY"):
        raise ValueError("M2_AB_API_KEY missing; zero supplemental provider sends")
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise RuntimeError('Run in a fresh process; application settings were imported before isolation')
    parent, budget = verify_parent(spec)
    # A stable authorization lock prevents replays through a different output path.
    locks = base.private_path(base.ROOT / "var" / "preferences-acceptance" / "m2-supplement-locks")
    locks.mkdir(parents=True, exist_ok=True)
    lock = locks / (base.digest(spec["authorization_id"].encode()) + ".started")
    output.mkdir(parents=True, exist_ok=False)
    with lock.open("xb") as stream:
        stream.write(base.canonical({"authorization_id": spec["authorization_id"], "output": str(output),
                                     "spec_sha256": base.digest(base.canonical(spec))}))
        stream.flush()
        os.fsync(stream.fileno())
    manifest = {**parent, "cases": [spec["case"]], "dataset_version": parent["dataset_version"] + "+D-supp-v1",
                "budget_cny": budget["remaining_cny"]}
    (output / "supplement-manifest.json").write_bytes(base.canonical(spec))
    (output / "preflight.json").write_bytes(base.canonical(preflight))
    guard = ParentAwareTransport(manifest, output, spec)
    summary = {"status": "NOT_RUN", "purpose": "D_ELIGIBILITY_ONLY", "planned_trials": 8,
               "parent_budget": budget, "entry": "ChatService.chat_stream; no HTTP server",
               "fixed_search": base.FIXED_SEARCH}
    original_cwd, original_plan = Path.cwd(), base.trial_plan
    try:
        with base.isolated_redis(output) as redis_url:
            base.configure_runtime(manifest, output, redis_url)
            with (output / "runtime.log").open("w", encoding="utf-8") as log:
                with redirect_stdout(log), redirect_stderr(log), guard.installed():
                    assert_runtime_isolation(output, redis_url)
                    # Fresh compact state is necessary: retrieval writes logs and
                    # preferences differ. Original files are referenced, never copied.
                    base.build_index(manifest)
                    base.trial_plan = supplement_plan
                    asyncio.run(base.run_trials(manifest, output, guard))
        summary["status"] = "RECORDED_REQUIRES_EVIDENCE_REVIEW"
    except base.GuardStop as exc:
        summary.update(status="STOPPED", stop_reason=str(exc))
    except Exception as exc:
        summary.update(status="FAILED", error_type=type(exc).__name__)
    finally:
        base.trial_plan = original_plan
        os.chdir(original_cwd)
        deliveries = audit_delivery(output, spec)
        summary.update(guard.summary())
        summary.update(recorded_trials=len(deliveries),
                       global_committed_including_unknown_cny=str(decimal(budget["parent_committed_cny"]) + guard.committed),
                       original_four_arm_conclusion="NOT_ESTABLISHED_BY_SUPPLEMENT",
                       original_evidence="READ_ONLY_UNCHANGED",
                       retained="Compact fresh SQLite/BM25 state and evidence; owned Redis stopped")
        (output / "summary.json").write_bytes(base.canonical(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "execute"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        spec_path, output = base.private_path(args.manifest), base.private_path(args.output)
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
        result = validate_spec(spec) if args.mode == "preflight" else execute(spec, output)
        if args.mode == "preflight":
            output.mkdir(parents=True, exist_ok=True)
            (output / "preflight.json").write_bytes(base.canonical(result))
        # Question text and policy/source details remain in private evidence.
        public = {key: result[key] for key in ("status", "purpose", "planned_trials", "model_calls", "budget",
                                               "recorded_trials", "global_committed_including_unknown_cny") if key in result}
        print(json.dumps(public, ensure_ascii=False))
        return 0 if result["status"] in ("PASS", "RECORDED_REQUIRES_EVIDENCE_REVIEW") else 2
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
