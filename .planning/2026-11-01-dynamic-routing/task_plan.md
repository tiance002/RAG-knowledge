# Dynamic Routing V1 implementation completion — 2026-11-02

Owner root; reuse plan and checkout. HEAD 8fca08ad669b3ba8b2fb6fe0da50cb9c2c54b48d. Existing uncommitted edits preserved.

## Goal
Finish two-stage opt-in routing, configuration, focused tests and truthful experiment/report deliverables. No real model requests, deployment, commit or push in this implementation run.

## Phases
1. complete — inspect current router/integration and latest 36-case review (selected sample, not total180 accuracy).
2. complete — improve measured rule signals from failure categories; audit normal/Agent provider call safety; update role configuration for latest Qwen experiment without changing production defaults.
3. complete — focused unit/integration and offline replay; verify UNKNOWN, no-evidence, provider errors, single escalation and OFF equivalence.
4. complete — final evidence review, update implementation report and experiment plan, explicit NOT_RUN limitations.

## Ownership
root: model_router.py, test_model_router.py, config/router-v1.env.example, report, planning.
route_runtime: grounded_generation.py, agent_workflow.py, corresponding two tests.
route_experiment: summarize_routing_experiment.py, accounting tests, experiment plan.

## Next Step
2026-11-02 follow-on complete: opt-in AFFINITY_V2 implemented and offline reviewed. Await owner review of docs/dynamic-router-affinity-v2-report-20261102.md. Future real validation needs a concrete bounded input/call plan; no production switch or push.

## Paired affinity candidate follow-on
1. COMPLETE — historical36/current164 joins, actual answer/model/message identities checked against read-only SQLite; 178 frozen contexts and citation hashes verified.
2. COMPLETE — opt-in generic task interactions and target-heading interference; V1/default OFF and failure safeguards preserved. Owner removed reverse GLM/Qwen preference optimization.
3. COMPLETE — historic partition diagnostic (already reviewed, NOT blind holdout), 132 tests/54 subtests exit0, report. Known rescue routing 1/11 ->11/11, persistent current misses4/4 captured; 31 selected GLM arms unavailable. Zero new model calls.
Root owns changes; read-only subagent examines failure features. No corpus/index/environment copies.

## Constraints
Never inject Gold/labels into online route. No source/Gold changes, no strict semantic validator, no implicit UNKNOWN replay. Full three-arm quality/savings remain NOT_RUN. Existing budget/auth architecture differs from E project; document actual boundaries.

## Follow-on: authorized 12-question three-arm experiment (2026-11-02)

1. IN PROGRESS — recover actual historic delivered messages, freeze 12 representative qids and documented evidence.
2. IN PROGRESS — durable single-writer ECS runner, capped attempts, guard historical UNKNOWN; use actual router and provider request settings. Zero-call preflight and mocks.
3. PENDING — review manifest, safety and launch limited independent ECS service; monitor to completion or legitimate terminal state.
4. PENDING — review each answer and citations against frozen source; report paired quality and real token/latency/cost UNKNOWN.

User's current instruction authorizes experiment; no renewed approval needed for up to 48 proposed calls. No production service switching. Fixed model/prompt/config, no historical UNKNOWN replay. Do not fabricate missing evidence, missing arms or metrics. Reuse frozen data and ECS dependencies, no new full index/database copy.

Follow-on completed: 12 frozen question provider replay and DYNAMIC-only continuation; no Expensive selection observed. Results and independent source-based review saved to private report and structured JSON; future higher-risk routing test would need separate frozen experiment, not claimed by this one.

## Dynamic 138 continuation, owner requested
1. IN PROGRESS freeze 138 new initial qids excluding first12 (10 initial +2 followups); disclose two initial not selected by predetermined rule, preserve corrected questions/gold history.
2. IN PROGRESS validate durable dynamic-only runner, 138 calls max, UNKNOWN no replay, no extra fixed arms; no production switch.
3. PENDING autonomous ECS startup, independent execution, persistent progress and results.
4. PENDING answer/citation review, compare historical Cheap only where exact inputs/provider parameters match; report denominators and cost UNKNOWN.
