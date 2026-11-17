# Task Plan: RSI v2.3 S2.1–S7

## Goal
Complete authorized trusted gates, data/evidence preparation, conditional real-search/validation, isolated shadow and full handoff.

## Current Status (2026-11-16 final closeout)

| Stage | Status | Evidence |
|---|---|---|
| S0 workspace / baseline | COMPLETE | remote head `d47e9a3`; 50 original dirty files verified; six protected files and budget hash match |
| S1 CI raw audit | COMPLETE | Push Run `43894292539` + PR Run `43894305537`; 8 Jobs × 518/518 |
| S2 S7 final report | COMPLETE | `var/rsi-v23-qa300-selected-20261116/S7-takeover-final-report.zh.md` |
| S2.1 admission / recovery / baseline | COMPLETE | prior evidence retained |
| S3 profiles / source exposure / readiness | BLOCKED_DATA | `COVERAGE_EVERY_ATOM_CONTROL_REQUIRED`; no gate or denominator change |
| S4 conditional frozen search | NOT_STARTED | blocked by S3 |
| S5 independent final test / paired QA | NOT_STARTED | blocked by S3 |
| S6 isolated shadow | SYNTHETIC_CONTROLS_COMPLETE; REAL_SHADOW_NOT_STARTED | 15 synthetic scenarios only; no production shadow claim |
| S7 delivery | RESTRICTED_NEGATIVE_RESULT_DELIVERED; AWAITING_FINAL_REVIEW | no autonomous gain claimed |

## Git / CI Baseline

- Branch: `codex/rsi-v22-review-20261115`
- Head: `d47e9a37505a0a726e320120377d62bd4a1e8ddb`
- Commits: `d3e7651` fixed-history fixtures + literal Gold compiler; `d13f6e4` CI frozen-worker dependencies; `d47e9a3` worker template LF attribute.
- Push Run: `43894292539` — 4/4 Jobs success, each 518 collected / 518 passed / 0 failed / 0 skipped / 0 errors.
- PR Run: `43894305537` — 4/4 Jobs success, each 518 collected / 518 passed / 0 failed / 0 skipped / 0 errors.
- Local full R2 was stopped at 55% after two 45s cold-import timeouts; it is not a completed full pass.
- Local clean environment with the same CI selection: 518 passed, exit 0.
- PR #1: draft / open / unmerged / undeployed; S8 not started.
- ECS services, indexes, production database, services and traffic: unchanged / paused.

## Next Step

Await final user review. Do not start S4, do not merge or deploy, do not enable S8, and do not relax any fact/statistical gate. Preserve `BLOCKED_DATA` and all UNKNOWN/negative evidence.

## Historical Phase Record

### Phase 1: S2.1 admission, recovery and baseline
- **Status:** complete (historical)
- Evidence of the original 50 dirty files, six protected files, budget and disk snapshot retained.

### Phase 2: S3 profiles, source exposure and readiness
- **Status:** BLOCKED_DATA (current DEV20)
- Prior `529b3bd` branch had its own 437/437 CI result; it is not transferable to the current fixed-history suite.

### Phase 3: S4/S5 conditional frozen experiments
- **Status:** NOT_STARTED in the current epoch.
- A prior epoch only reached a conditional handoff; no current S4 search or candidate terminal test was run.

### Phase 4: S6 isolated shadow and full regression
- **Status:** synthetic controls complete; real production shadow NOT_STARTED.
- Do not present the 15 synthetic control scenarios as a real shadow run.

### Phase 5: S7 evidence and draft PR delivery
- **Status:** restricted negative result delivered; awaiting final review.
- The upload/CI engineering fixes do not count as autonomous RSI gain.

### Phase 6/7: Real S3 completion and frozen continuation
- **Status:** closed as BLOCKED_DATA for this epoch.

## Decisions Made

| Decision | Rationale |
|---|---|
| Accept `BLOCKED_DATA`; do not start S4/S5 | The unchanged six-class negative-control gate cannot PASS on the selected DEV20. |
| Do not narrow the DEV denominator or relax fact/stat gates | Preserves evaluator validity; no fabricated negative controls. |
| Upload fixed-history engineering and CI fixes as a draft PR | Makes evidence reproducible without merging or deploying. |
| Keep PR #1 draft; do not enable S8 | Production services, indexes and traffic remain untouched. |
| Do not claim autonomous optimization success | All CI and fixture work is engineering, not RSI-generated gain. |

## Errors Encountered

| Error | Resolution |
|---|---|
| CI 4/4 failed on `d3e7651`: `ModuleNotFoundError: sqlmodel` | Installed the minimal frozen-worker runtime dependencies in `.github/workflows/rsi-v22.yml`. |
| Windows CI failed on `d13f6e4`: worker template CRLF vs LF byte mismatch | Marked `scripts/rsi_fixed_history_worker_v1.py` as `-text` in `.gitattributes`. |
| Local full R2: two 45s frozen-worker cold-import timeouts, stopped at 55% | Retained as an environmental limitation; targeted and clean-environment runs passed; full R2 is still not claimed green. |
| `prepare_collection` exit 1: `COVERAGE_EVERY_ATOM_CONTROL_REQUIRED` | Accepted BLOCKED_DATA; no gate or data change. |
