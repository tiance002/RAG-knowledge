# PDF parser migration — 2026-10-27

## Table fixes and causal ablation — owner authorized
- [complete] Fix UI false admission, first-data-row header inference and nonempty corrupted cell OCR. Preserve raw OCR/provenance and add regressions plus a table master switch. Commitca65fa0;21Windows/21LinuxPASS.
- [complete] Freeze same-v2 off413/on438chunks,134pagebaseOCRhashes equal,89captionsidentical,old12questions/Gold byte-identical;455uniqueembeddinginputs. InputSHA557d80a52c532ac2140c316a22501ef181ff652058deff63c95c57ec7f677882. Preflightzero-call exit0.
- [complete] Execute24fresh paired answers:exit0/1222.167s,455embeddinginputs/58rerank/24generation,122HTTPsuccess,UNKNOWN0. Table4core4/4both/Recall1both;original8core6/8both/Recall.8333both;all12core10/12both/pageRecall.9both. TablepromptTokens51401->37250(-27.5%),all172268->156730(-9.0%);parse114.607s->154.108s.

Parser audit:source16normalizedcharerrors345/742->4/742,exactcells11/20->17/20;source transcription preserves printedaves/300m/s. Linuxallsource/languagehashes match;strictrowcomparisonexit1 atonein/In capitalization;casefoldallrowsPASS, bothresultsretained. No product/source changes afterfrozenrunstart.

Finalevidence:docs/pdf-table-ablation-20261027.md;privatevar/pdf-table-ablation-20261027.Productionpostcheckallsource/PDF/model/countsunchanged6/11779/37271;isolatedcustom_models0. BundleSHA2da061d8708b575f43180d5d5d848094f650fa4a88660ac3a11a36cd9f4e8739. No service switch/push/tag; no furthermodelcalls planned. Next:deliver result; independentmissing/misalignedtableholdouts would be new evaluation work.

Scope: D:/RAG knowledge only; E:/RAG quention remains read-only and paused. Target repo lacks root AGENTS.md/progress.md/docs/adr; existing selected .planning and parser/eval docs are current records. Old journals/results are immutable.

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
Deliver v2 parse evidence and correction. Keep legacy default; v2 retrieval/answer quality NOT RUN. Do not tune or relabel the frozen v1 eight-question results.

## Errors
- Combined skill reads truncated; relevant sections read separately.
- Advisory Windows wildcard rg failed; corrected with rg --files, not a test execution.
- Initial plan patch rejected duplicate delete/add path atomically; corrected using literal file writes.
- First pytest2pass8setup errors due absent basetemp parent; fixed and subsequent11pass.
- First parse A/B r1 invalid baseline because GBK cannot print legacy emojis. Preserved r1; r2 UTF8 completed.
- Frozen-input preparation first failed GBK decode; explicit UTF8 fixed before output creation.

## Owner follow-up — 2026-10-27
- [complete] Discover PDFs:38386magic candidates and65archives,3unique documents/43physical copies, no extra original PDFs.
- [complete] Visual review found native table in existing report PDFpage45 and scanned colored table in physics PDFpage13. Prior sample-absence statement corrected.
- [complete] Fix limited ruled-borderless native admission, colored raster grids and local empty-cell OCR recovery. Windows17PASS/Linux17PASS; real ECS two-page paired parse PASS(0->10rows/4cols and0->5rows/2cols). No new provider requests.

Next action: deliver v2 parse evidence and correction; keep legacy default. Retrieval/answer quality for v2 remains NOT RUN, previous v1 scores are not reused as v2 proof.

## V2 retrieval and answer evaluation — owner authorized
- [complete] Freeze b681671 v2 parse output on same3ECS PDFs, models, prompt, retrieval config and captions; retain original8regression items and add4source-verified table probes before inference. Before277/after441 chunks;636 unique embedding inputs including12questions.
- [complete] Validate actual production source, model identity, endpoint hashes and unchanged prompt AST; PREPARED_ZERO_CALLS exit0. Use24fresh paired answers, no baseline answer reuse.
- [complete] Execute bounded evaluation, score regression/table strata separately, retain failures and report Recall@10 + correctness without overwriting prior metrics.24fresh answers/636embedding inputs/62rerank,137HTTPsuccess,exit0.

Outcome: original8 pageRecall.8333->.8333/core6/8->4/8; table4 pageRecall1->1/core4/4->4/4; all12 pageRecall.9->.9/core10/12->8/12. Ambiguous115 loose scoring yields9/12after, still no gain. Table promptTokens86324->39038. Production source/PDF/models/counts unchanged; isolatedcustom_models0. No production switch. Preserve b681671 unchanged despite false-positive UI screenshot41 and damaged scan-table16 found in full-corpus audit.

Next action: deliver report docs/pdf-v2-evaluation-20261027.md; no additional model calls planned. Fixing table admission/OCR and a table-on/off causal ablation are follow-up work, not silently included in this frozen run.
