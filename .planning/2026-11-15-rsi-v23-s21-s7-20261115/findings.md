# Findings & Decisions

## Requirements
-

## Research Findings
-

## Technical Decisions
| Decision | Rationale |
|----------|-----------|

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| Selected DEV20 has 4 retrieval items whose source files are all single-chunk (forward-session14, terra-ecs-b8-006, terra-ecs-b6-019, terra-ecs-b11-005; 9 atoms). Their facts live in the only member, which is also the seed; the coverage adapter forbids removing seeds. No legal negative control can be constructed without fabricating a second member. | Keep the gap explicit; do not fake splits/copies. Coverage cannot PASS under the unchanged rule. |
| The only two `多来源支持`-tagged atoms are both in forward-session14 (an irreducible item). `reference-lake` maps only one native file in the capture; `higher-lake` maps two. | Cannot use them as a second independent multi-source family/examples for coverage; record as structural blocker. |
| The escaped-newline literal transform for qst_0131 contradicted the old builder test. | Updated only the new builder test to allow the exact reversible `newline-to-literal-backslash-n/v1` transform with recorded evidence; CRLF and invented joins still rejected. 6/6 passed. |
| Main-suite Gold-map seeds included >4000-char parents that expand to zero members and left second source files unseeded (qst_0150/qst_0131 unreachable). | Added `deliverable_seed_ids`: real native non-support child/neighbour seeds that expand to every support; protected 4000-char cap unchanged. Compiler r5 emitted four suites, gap_count 0. |
| Even the best (backward-seed) suite has 15 negative-control gaps; `prepare_collection` rejects with `COVERAGE_EVERY_ATOM_CONTROL_REQUIRED`. | Accepted BLOCKED_DATA; did not narrow the DEV denominator or change the coverage rule. |

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| Accept S3 BLOCKED_DATA on 2026-11-16; no S4/S5; S7 negative result. | The unchanged six-class gate cannot PASS with 9 structurally seed-bound atoms; user explicitly chose to accept the negative result without relaxing fact/stat gates. |
| Do not commit/push the local RSI changes. **SUPERSEDED 2026-11-16:** the user requested the upload; commits `d3e7651`, `d13f6e4`, `d47e9a3` were pushed and CI is green. | PR #1 stayed draft at b690fcd at the time; full post-change regression was stopped after 2 cold-import 45s timeouts, so no full green was claimed. |
| Final upload baseline is `d47e9a3`; PDF/draft PR remains unmerged. Push Run `43894292539` and PR Run `43894305537` each have 4/4 Jobs success with 518/518 tests. | The CI failures were engineering defects, not RSI gain; all historical red runs are retained. |
| Local pytest-html-reporter overwrote two baseline untracked reports (`output.json`, `pytest_html_report.html`) during test runs. | Restored both from existing archived copies; all 50 baseline files now hash-match. Avoid future repo-root pytest runs or accept that these generated reports change. |

## Resources
-

Entry PR branch/localhead dabaaa7 unchanged; no interruptedcommands running. Originaldirtyfiles/protected/budget/disks snapshotted. Workers own gates+baseline, learningrecovery, sourceprofile+ledger; root owns shadow, phase orchestration and integration. No paidactions before credible admission.

Continuation2998638: threefreshdelegates Solmedium forconnectivity andrealDEVcuration, Luna forfamilyaudit. LiveofficialmodelGET directtimeout butexplicitexistingloopbackproxysucceedsHTTP200IDsverified; no paidgeneration. DEVcurationbuilds20realcases6classgroundedatoms; citationv1 incompatible frozenworker capture found, addingexplicit numbered-delivery binding profile beforefreeze (oldmode unchanged). Familymetadatahashidentity alone insufficient, auditingupstream origins ratherthanblessingindependence. Entry reports backedup toavoidglobalpytestplugin loss. No ordinarymodel experiment started.
