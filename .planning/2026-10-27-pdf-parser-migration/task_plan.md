# PDF parser migration — 2026-10-27

## Goal and scope
Implement per-page routing/audit, local OCR with coordinates, native tables, then separate scanned-table handling. Compare the same ECS PDFs before/after with frozen models/retrieval configuration. Old project is read-only reference; no old business writes or overwritten indexes.

## Phases
- [complete] 1. Inspect ingestion contracts, baseline, ECS/corpus and dependencies.
- [complete] 2. Implement diagnostics, OCR, provenance propagation and native tables.
- [complete] 3. Separate opt-in raster-grid scan-table route; final14 tests PASS on Windows and Linux.
- [complete] 4. Parse identical ECS PDFs locally:134pages, before277/after413chunks; new OCR39751chars. Initial40chart rows rejected after generic admission repair; valid native/scan tables covered by actual PDF fixtures.
- [complete] 5. Final real paired inference COMPLETED. SourceRecall1.0->1.0, pageRecall.8333->.8333(n6); core correctness8/8->7/8(n8,agent semantic review). Quality gate FAIL, no owner acceptance or live activation.

## Constraints
Baseline b5ceb463a433193a908e1d814b221c855f8717d0; branch codex/pdf-parser-migration. Target has no AGENTS.md, progress.md, docs/adr or automated tests at baseline. Raw data and secrets stay ignored. No production replacement needed for isolated A/B; no model calls before corpus/privacy/config checks. Do not resend unknown requests automatically.

## Next Step
Deliver candidate and evidence. Next implementation requires a separate decision on OCR noise/context interaction and real table samples; do not tune this frozen eight-question set.

## Errors
- Combined skill reads truncated; relevant sections read separately.
- Advisory Windows wildcard rg failed; corrected with rg --files, not a test execution.
- Initial plan patch rejected duplicate delete/add path atomically; corrected using literal file writes.
- First pytest2pass8setup errors due absent basetemp parent; fixed and subsequent11pass.
- First parse A/B r1 invalid baseline because GBK cannot print legacy emojis. Preserved r1; r2 UTF8 completed.
- Frozen-input preparation first failed GBK decode; explicit UTF8 fixed before output creation.
