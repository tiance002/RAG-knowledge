"""Bounded local RSI loop for registered development parents and the existing selector seam."""
import argparse
import ast
import json
from pathlib import Path
import sys

import rsi_candidates as registry
from rsi_decision import review_run
from rsi_proposal_agent import FIELDS, ProposalAgent
from run_rsi_offline import ROOT, run, save

FAILURES = {"required_fact_loss_vs_parent", "required_fact_absent", "seed_loss", "source_conflict"}


def failures(run_dir):
    feedback = registry.read_json(run_dir / "feedback.json")
    valid = {c["qid"] for c in feedback["cases"] if c["valid_pair"]}
    return [o for o in feedback["observations"] if o["qid"] in valid and o["category"] in FAILURES
            and o["details"].get("arm", "B") == "B"]


def selector(source):
    fn, = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "select_member_indices"]
    lines = source.splitlines(keepends=True)
    return fn, "".join(lines[fn.lineno - 1:fn.end_lineno])


def validate_policy(fn):
    # validate_seam verifies the whole module. Additionally confine generated code to
    # local computations on arguments; no module globals, indirect calls or writable input objects.
    args = {a.arg for a in fn.args.args}
    helpers = {"duplicate_key", "require", "any", "all", "range", "len", "enumerate", "list",
               "tuple", "set", "sorted", "min", "max", "sum", "abs"}
    nodes = [n for statement in fn.body for n in ast.walk(statement)]
    local = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    fresh_lists = {n.targets[0].id for n in nodes if isinstance(n, ast.Assign)
                   and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                   and isinstance(n.value, (ast.List, ast.ListComp))}
    allowed = (ast.Expr, ast.Constant, ast.Return, ast.Assign, ast.For, ast.If, ast.IfExp, ast.Continue,
               ast.Break, ast.Pass, ast.Name, ast.Load, ast.Store, ast.List, ast.Tuple, ast.Set, ast.Dict,
               ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.Call,
               ast.Attribute, ast.Subscript, ast.Slice, ast.BoolOp, ast.boolop, ast.Compare, ast.cmpop,
               ast.UnaryOp, ast.unaryop, ast.BinOp, ast.Add, ast.Sub)
    for n in nodes:
        if not isinstance(n, allowed) or local & (helpers | args):
            raise ValueError("Selector must only compute indices from its read-only inputs")
        if isinstance(n, ast.Name) and n.id not in args | local | helpers:
            raise ValueError("Selector references a global name")
        if isinstance(n, (ast.Attribute, ast.Subscript)) and not isinstance(n.ctx, ast.Load):
            raise ValueError("Selector may not write to objects")
        if isinstance(n, ast.Attribute) and n.attr not in {"get", "append"}:
            raise ValueError("Unsupported selector attribute")
        if isinstance(n, ast.Call):
            method = isinstance(n.func, ast.Attribute)
            if not method and not (isinstance(n.func, ast.Name) and n.func.id in helpers):
                raise ValueError("Indirect calls are prohibited")
            if method and n.func.attr == "append":
                receiver = n.func.value
                if not isinstance(receiver, ast.Name) or receiver.id not in fresh_lists or sum(
                        isinstance(x, ast.Name) and x.id == receiver.id and isinstance(x.ctx, ast.Store)
                        for x in nodes) != 1:
                    raise ValueError("Append requires a private list")


def apply_proposal(parent_bytes, proposal):
    if not isinstance(proposal, dict) or set(proposal) != set(FIELDS) or any(
            not isinstance(proposal[k], str) or not proposal[k].strip() for k in FIELDS):
        raise ValueError("Expected one old/new patch, hypothesis, change, impact and falsification")
    source = parent_bytes.decode("utf-8")
    _, original = selector(source)
    eol = "\r\n" if "\r\n" in original else "\n"
    old, new = [proposal[k].replace("\r\n", "\n").replace("\n", eol) for k in ("old", "new")]
    if original.count(old) != 1 or old == new:
        raise ValueError("Patch must change one uniquely matched selector substring")
    changed = source.replace(original, original.replace(old, new, 1), 1).encode()
    registry.validate_seam(parent_bytes, changed)
    fn, _ = selector(changed.decode())
    validate_policy(fn)
    return changed


def evidence(parent, code, run_dir, obs, review, budget, previous):
    case = next(c for c in review["cases"] if c["qid"] == obs["qid"])
    return {"task": "Repair an observed selector failure", "parent": parent, "selector_source": selector(code.decode())[1],
            "source_run": review["run_id"], "observation_id": obs["observation_id"],
            "category": obs["category"], "documented_budget": budget,
            "executed_selection": registry.read_json(run_dir / "implementation/B.json"),
            "receipt": {a: {"members_in_order": s["member_ids"], "faults": s["faults"],
                            "required_fact_status": [[f["kind"], f["status"]] for f in s["facts"]],
                            "request_count": s["request_calls"], "capture_sha256": s["capture_sha256"]}
                        for a, s in case["arms"].items()}, "previous_attempt": previous}


def next_action(decision, remaining_failures):
    if decision["next_step"] == "REGRESSION":
        return "RETAIN_PARENT"
    if not decision["eligible_development_parent"]:
        return "STOP_BLOCKED"
    if decision["next_step"] == "RECOVERED_WAIT_FOR_NEW_OBSERVATION":
        return "STOP_RECOVERED"
    if remaining_failures:
        return "CONTINUE_FROM_CHILD"
    pairs = [c for c in decision["cases"] if c["status"] == "PAIRED"]
    if pairs and all(not c["arms"]["B"]["faults"] and c["facts"]["status"] == "KNOWN"
                     and not c["facts"]["lost"] and not c["facts"]["still_missing"] for c in pairs):
        return "STOP_DEVELOPMENT_FACTS_SATISFIED"
    return "STOP_NO_LOCALIZED_FAILURE"


def execute(config_path, out, approved, preflight=False, approved_model_calls=0, execute_roles=False, reuse_completed_roles=None, resume_evaluated_baseline=None, quality_stage=None):
    cfg = registry.read_json(config_path)
    if cfg.get('kind') == 'rag_quality':
        from rsi_quality_loop import execute_quality_loop
        if quality_stage:
            cfg = {**cfg, 'stage': quality_stage}
        return execute_quality_loop(cfg, out, preflight=preflight or not execute_roles,
            approved_model_calls=approved_model_calls, resume_evaluated_baseline=resume_evaluated_baseline,
            reuse_completed_roles=reuse_completed_roles)
    if "roles" in cfg:
        from rsi_roles_loop import execute_roles_loop
        return execute_roles_loop(cfg, out, approved, preflight or not execute_roles, approved_model_calls, reuse_completed_roles)
    budget = cfg["budget"]
    if not (type(budget["max_proposals"]) is int and 1 <= budget["max_proposals"] <= 2
            and type(budget["max_cases"]) is int and 1 <= budget["max_cases"] <= 3
            and budget["external_rag_calls"] == 0):
        raise ValueError("Unsupported development budget")
    reg, source, bundle = [(ROOT / cfg[k]).resolve() for k in ("registry", "failure_run", "frozen_bundle")]
    if any(out.resolve().is_relative_to(p) for p in (reg, source, bundle)):
        raise ValueError("Output overlaps protected inputs")
    parent = cfg["parent"]
    observations = failures(source)
    obs, = [o for o in observations if o["observation_id"] == cfg["observation_id"]]
    before = review_run(source, bundle)
    manifest = registry.read_json(bundle / "manifest.json")
    if not (before["scope"] == "registered_offline" and before["child"] == parent and not before["exceptions"]
            and 0 < before["planned"] <= budget["max_cases"] and manifest["budget"]["external_calls"] == 0):
        raise ValueError("Failure does not bind to this parent and frozen development fixture")
    if cfg["approved_development_cases_sha256"] != manifest["cases"]["sha256"]:
        raise ValueError("Development inputs are outside the approved proposal scope")
    cases, rubric = [bundle / manifest[k]["path"] for k in ("cases", "evaluation")]
    config_hash = registry.sha(registry.canonical(cfg))
    agent = ProposalAgent(cfg["model"])
    if preflight:
        code = registry.get(reg, registry.candidate(reg, parent)["worker"])
        request = agent.request(evidence(parent, code, source, obs, before, budget, None))
        return {"status": "PREFLIGHT", "model": cfg["model"], "proposer_calls": 0,
                "rag_external_calls": 0, "cases": before["planned"], "first_request_bytes": len(registry.canonical(request)),
                "output_cap_per_call": request["max_tokens"], "max_request_bytes": 6000}
    if not 1 <= approved <= budget["max_proposals"]:
        raise ValueError("Set --approved-proposals within the approved experiment budget")
    out.mkdir(parents=True, exist_ok=False)
    save(out / "config.json", cfg)
    result = {"config_sha256": config_hash, "scope": cfg["scope"], "initial_parent": parent,
              "active_development_parent": parent, "proposer_calls": 0, "rag_external_calls": 0,
              "formal_promotion": "NOT_RUN", "attempts": []}
    previous = None
    try:
        result.update(provider="SiliconFlow", model=cfg["model"], approved_proposals=approved)
        for i in range(1, approved + 1):
            step = out / f"iteration-{i:02}"
            step.mkdir()
            p = registry.candidate(reg, parent)
            code = registry.get(reg, p["worker"])
            info = evidence(parent, code, source, obs, before, budget, previous)
            request = agent.request(info)
            save(step / "agent-input.json", request)
            receipt = {"parent": parent, "source_run_id": before["run_id"], "observation": obs["observation_id"],
                       "input_sha256": registry.sha(registry.canonical(request)), "agent_call": i,
                       "agent_status": "UNKNOWN", "input_tokens": None, "output_tokens": None,
                       "candidate_archived": False, "accepted_as_development_parent": False}
            result["attempts"].append(receipt)
            save(step / "dispatch.json", dict(receipt))
            result["proposer_calls"] += 1
            wire = agent.propose(request)
            (step / "agent-response.json").write_bytes(wire["body"].encode())
            receipt.update(http_status=wire["http_status"], provider_trace_id=wire["trace_id"])
            if wire["http_status"] != 200:
                receipt.update(agent_status="PROVIDER_ERROR")
                raise ValueError(f"Provider HTTP {wire['http_status']}")
            response = json.loads(wire["body"])
            choice = response["choices"][0]
            usage = response.get("usage") or {}
            receipt.update(agent_status="RETURNED", model=response.get("model"), provider_id=response.get("id"),
                           input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                           usage=usage, finish=choice.get("finish_reason"))
            if response.get("model") != cfg["model"] or receipt["finish"] != "stop":
                raise ValueError("Incomplete response or unexpected proposal model")
            try:
                proposal = json.loads(choice["message"]["content"])
                save(step / "proposal.json", proposal)
                child_bytes = apply_proposal(code, proposal)
            except (ValueError, SyntaxError, TypeError) as exc:
                previous = {"validation_failure": str(exc), "returned": choice["message"]["content"][:800]}
                receipt.update(action="REJECT_PROPOSAL", reason=str(exc))
                save(step / "receipt.json", receipt)
                continue
            worker = step / "child_worker.py"
            worker.write_bytes(child_bytes)
            save(step / "patch.json", {k: proposal[k] for k in ("old", "new")})
            notes = {k: proposal[k] for k in ("hypothesis", "change", "impact", "falsification")}
            notes["observation"] = obs["category"]
            child = registry.derive(reg, parent, source / "implementation/B.json", worker, notes, source, obs["observation_id"])
            receipt.update(child=child, candidate_archived=True)
            target = step / "bundle"
            inputs = (cases, rubric) if manifest["r1"].get("fixture") else ()
            materialized = registry.materialize(reg, parent, child, target, *inputs)
            archive = step / "run"
            summary = run(materialized, archive)
            decision = review_run(archive, target, source, bundle)
            save(step / "decision.json", decision)
            remaining = failures(archive)
            action = next_action(decision, remaining)
            receipt.update(run_id=summary["run_id"], frozen_score=summary["status"], review=decision["next_step"],
                           action=action, accepted_as_development_parent=decision["eligible_development_parent"],
                           run_directory=str(archive))
            save(step / "receipt.json", receipt)
            if decision["eligible_development_parent"]:
                result["active_development_parent"] = child
            if action == "RETAIN_PARENT":
                previous = {"rejected_hypothesis": notes["hypothesis"], "decision": decision["next_step"],
                            "previous_edit": {k: proposal[k][:500] for k in ("old", "new")},
                            "measured_child_faults": [c["arms"]["B"]["faults"] for c in decision["cases"] if c["status"] == "PAIRED"]}
                continue
            if action != "CONTINUE_FROM_CHILD":
                result["stop_reason"] = action
                break
            parent, source, bundle, obs, before = child, archive, target, remaining[0], decision
            previous = {"prior_hypothesis": notes["hypothesis"], "decision": decision["next_step"]}
        else:
            result["stop_reason"] = "PROPOSAL_BUDGET_EXHAUSTED"
    except Exception as exc:
        result.update(stop_reason="STOP_ERROR_OR_UNKNOWN", error=f"{type(exc).__name__}: {exc}")
    save(out / "loop-result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New local directory; never overwrite or implicitly retry")
    parser.add_argument("--approved-proposals", type=int, default=0, help="Maximum authorized paid proposal calls for this run")
    parser.add_argument("--preflight", action="store_true", help="Validate the first request without sending it")
    parser.add_argument("--execute", action="store_true", help="Execute roles config; roles default to zero-call preflight")
    parser.add_argument("--approved-model-calls", type=int, default=0, help="Explicit role call authorization; quality pilot maximum 12")
    parser.add_argument("--resume-evaluated-baseline", type=Path, help="Reuse one complete hash-verified quality baseline; never rerun it")
    parser.add_argument("--quality-stage", choices=['baseline', 'full'], help="Override only the rag_quality pilot stage")
    parser.add_argument("--recover-baseline-from", type=Path, help="Archive a completed worker with independent review; zero model sends")
    parser.add_argument("--review", type=Path, help="Hash-bound independent AI_REVIEW JSON for baseline recovery")
    parser.add_argument("--reuse-completed-roles", type=Path, help="Read-only prior iteration with identical completed role requests")
    args = parser.parse_args()
    if args.recover_baseline_from:
        if not args.review or args.execute or args.preflight:
            parser.error('recovery requires --review and does not use --execute or --preflight')
        from rsi_quality_loop import recover_baseline
        result = recover_baseline(args.recover_baseline_from, args.out, args.review)
    else:
        result = execute(args.config, args.out.resolve(), args.approved_proposals, args.preflight,
                         args.approved_model_calls, args.execute, args.reuse_completed_roles, args.resume_evaluated_baseline, args.quality_stage)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("status") in {"PREFLIGHT", "BASELINE_COMPLETE", "PILOT_COMPLETE"} or result.get("stop_reason") in {"STOP_RECOVERED", "STOP_DEVELOPMENT_FACTS_SATISFIED"} else 1)
