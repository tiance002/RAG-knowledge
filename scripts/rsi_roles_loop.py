"""Role orchestration over the existing registry/executor/reviewer, never a new executor."""
from pathlib import Path

import rsi_candidates as registry
import rsi_roles as roles
from run_rsi_offline import ROOT, run, save, validate_source_roots
from rsi_decision import review_run


def executed_change(summary, child, proposed_bytes, archive):
    """Controller facts distinguish the executed frozen case from unrun extra suggestions."""
    actual_path = archive / "implementation/B_worker.py"
    proposed_hash = registry.sha(proposed_bytes)
    actual_hash = registry.sha(actual_path.read_bytes())
    return {"run_id": summary["run_id"], "child_id": child,
            "proposed_worker_sha256": proposed_hash, "executed_worker_sha256": actual_hash,
            "worker_hashes_equal": proposed_hash == actual_hash, "archived_worker": str(actual_path),
            "paired_count": summary["paired"], "frozen_case_count": summary["total"],
            "frozen_score_status": summary["status"], "execution_scope": "ONLY_FROZEN_CASES_EXECUTED",
            "proposed_patch_execution": "EXECUTED" if proposed_hash == actual_hash else "HASH_MISMATCH",
            "additional_suggested_falsification_cases": "NOT_RUN"}


def execute_roles_loop(cfg, out, approved, preflight, approved_calls, reuse_completed_roles=None):
    from run_rsi_loop import failures, evidence, apply_proposal, next_action
    b = cfg["budget"]
    if not (type(b["max_proposals"]) is int and 1 <= b["max_proposals"] <= 2
            and type(b["max_cases"]) is int and 1 <= b["max_cases"] <= 3
            and type(b["max_model_calls"]) is int and 1 <= b["max_model_calls"] <= 10
            and type(b["max_output_tokens"]) is int and 1 <= b["max_output_tokens"] <= 8192
            and type(b["max_input_bytes"]) is int and 1 <= b["max_input_bytes"] <= 65536
            and b["external_rag_calls"] == 0):
        raise ValueError("Unsupported roles development budget")
    reg, source, bundle = [(ROOT / cfg[k]).resolve() for k in ("registry", "failure_run", "frozen_bundle")]
    out = out.resolve()
    if any(out.is_relative_to(p) or p.is_relative_to(out) for p in (reg, source, bundle)):
        raise ValueError("Output overlaps protected inputs")
    ledger = (ROOT / cfg["experience_ledger"]).resolve()
    if any(ledger.is_relative_to(p) for p in (reg, source, bundle)):
        raise ValueError("Experience ledger overlaps protected inputs")
    parent = cfg["parent"]
    obs, = [o for o in failures(source) if o["observation_id"] == cfg["observation_id"]]
    before = review_run(source, bundle)
    manifest = registry.read_json(bundle / "manifest.json")
    if not (before["scope"] == "registered_offline" and before["child"] == parent and not before["exceptions"]
            and 0 < before["planned"] <= b["max_cases"] and manifest["budget"]["external_calls"] == 0
            and cfg["approved_development_cases_sha256"] == manifest["cases"]["sha256"]):
        raise ValueError("Failure does not bind to approved parent and frozen fixture")
    code = registry.get(reg, registry.candidate(reg, parent)["worker"])
    business_root = (ROOT / cfg["business_source_root"]).resolve() if cfg.get("business_source_root") else None
    source_roots = validate_source_roots(manifest, business_root, bundle=bundle, parent_worker=code)
    prior = roles.eligible_experience(ledger, reg)
    info = evidence(parent, code, source, obs, before, b, None)
    info["prior_experience_candidates"] = prior
    requests = {r: roles.request_for(r, info, cfg) for r in roles.ROLES}
    reuse = reuse_completed_roles.resolve() if reuse_completed_roles is not None else None
    reusable_diagnosis = roles.completed_source(reuse / "diagnosis", requests["diagnosis"]) if reuse else None
    if preflight:
        return {"status": "PREFLIGHT", "model_calls": 0, "rag_external_calls": 0,
                "cases": before["planned"], "roles": cfg["roles"], "budget": b,
                "first_request_bytes": len(registry.canonical(requests["diagnosis"])),
                "prior_experience_candidates": len(prior), "cost": "UNKNOWN",
                "reusable_diagnosis": reusable_diagnosis is not None, "source_roots": source_roots}
    if not (1 <= approved <= b["max_proposals"] and 5 * approved - bool(reusable_diagnosis) <= approved_calls <= b["max_model_calls"]):
        raise ValueError("Authorize iterations with --approved-proposals and 5 calls per iteration with --approved-model-calls (max10)")
    # Check credentials before creating a run or consuming any call. Values never enter artifacts.
    roles.runtime_key(ROOT)
    out.mkdir(parents=True, exist_ok=False)
    save(out / "config.json", cfg)
    state = {"scope": cfg["scope"], "initial_parent": parent, "active_development_parent": parent,
             "config_sha256": registry.sha(registry.canonical(cfg)), "model_calls": 0,
             "proposer_calls": 0, "role_calls": [], "reused_role_calls": 0, "rag_external_calls": 0,
             "formal_promotion": "NOT_RUN", "cost": "UNKNOWN", "attempts": [], "source_roots": source_roots}
    previous = None
    try:
        for i in range(1, approved + 1):
            step = out / f"iteration-{i:02}"
            step.mkdir()
            code = registry.get(reg, registry.candidate(reg, parent)["worker"])
            info = evidence(parent, code, source, obs, before, b, previous)
            info["prior_experience_candidates"] = roles.eligible_experience(ledger, reg)
            save(step / "evidence.json", info)
            def call(role, data):
                return roles.call(role, data, cfg, step, state, approved_calls, ROOT, save,
                                  reuse if i == 1 else None,
                                  reusable_diagnosis["body_sha256"] if i == 1 and role == "diagnosis" and reusable_diagnosis else None)
            diagnosis = call("diagnosis", info)
            analysis = call("analysis", {"evidence": info, "diagnosis": diagnosis})
            proposal = call("implementation", {"evidence": info, "diagnosis": diagnosis, "analysis": analysis})
            state["proposer_calls"] += 1
            save(step / "proposal.json", proposal)
            receipt = {"parent": parent, "source_run_id": before["run_id"], "observation": obs["observation_id"],
                       "candidate_archived": False, "accepted_as_development_parent": False}
            state["attempts"].append(receipt)
            try:
                child_bytes = apply_proposal(code, proposal)
            except (ValueError, TypeError, SyntaxError) as exc:
                receipt.update(action="REJECT_PROPOSAL", reason=str(exc))
                save(step / "receipt.json", receipt)
                previous = {"validation_failure": str(exc)}
                continue
            worker = step / "child_worker.py"
            worker.write_bytes(child_bytes)
            save(step / "patch.json", {k: proposal[k] for k in ("old", "new")})
            notes = {k: proposal[k] for k in ("hypothesis", "change", "impact", "falsification")}
            notes["observation"] = obs["category"]
            child = registry.derive(reg, parent, source / "implementation/B.json", worker, notes, source, obs["observation_id"])
            receipt.update(child=child, candidate_archived=True)
            target = step / "bundle"
            inputs = tuple(bundle / manifest[k]["path"] for k in ("cases", "evaluation")) if manifest["r1"].get("fixture") else ()
            materialized = registry.materialize(reg, parent, child, target, *inputs)
            archive = step / "run"
            summary = run(materialized, archive, business_root=business_root) if business_root else run(materialized, archive)
            decision = review_run(archive, target, source, bundle)
            decision_path = step / "decision.json"
            save(decision_path, decision)
            remaining = failures(archive)
            action = next_action(decision, remaining)
            receipt.update(run_id=summary["run_id"], frozen_score=summary["status"], review=decision["next_step"],
                           action=action, accepted_as_development_parent=decision["eligible_development_parent"],
                           run_directory=str(archive))
            save(step / "receipt.json", receipt)
            # Commit deterministic outcome before advisory roles. A later UNKNOWN cannot undo it.
            if decision["eligible_development_parent"]:
                state["active_development_parent"] = child
            execution = executed_change(summary, child, child_bytes, archive)
            record = {"status": "UNKNOWN", "registry": str(reg), "source_run": before["run_id"],
                      "run_id": summary["run_id"], "candidate": child, "parent": parent,
                      "decision_path": str(decision_path), "decision_sha256": registry.sha(registry.canonical(decision)),
                      "formal_promotion": "NOT_RUN"}
            try:
                evaluation = call("evaluation", {"deterministic_review": decision, "frozen_score": summary["status"],
                                                  "proposal": proposal, "role": "ADVISORY_ONLY", "executed_change": execution})
                suggestion = call("experience", {"binding": record, "deterministic_review": decision,
                                                  "proposal": proposal, "advisory_evaluation": evaluation,
                                                  "executed_change": execution})
                record.update(status="CANDIDATE", suggestion=suggestion)
            finally:
                roles.append_experience(ledger, record)
                save(step / "experience.json", record)
            if action == "RETAIN_PARENT":
                previous = {"rejected_hypothesis": notes["hypothesis"], "decision": decision["next_step"]}
                continue
            if action != "CONTINUE_FROM_CHILD":
                state["stop_reason"] = action
                break
            parent, source, bundle, obs, before = child, archive, target, remaining[0], decision
            manifest = registry.read_json(bundle / "manifest.json")
            previous = {"prior_hypothesis": notes["hypothesis"], "decision": decision["next_step"]}
        else:
            state["stop_reason"] = "PROPOSAL_BUDGET_EXHAUSTED"
    except Exception as exc:
        state.update(stop_reason="STOP_ERROR_OR_UNKNOWN", error=f"{type(exc).__name__}: {exc}")
    save(out / "loop-result.json", state)
    return state
