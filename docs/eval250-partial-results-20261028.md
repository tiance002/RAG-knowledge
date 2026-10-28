# ECS 250-question evaluation: partial results, 2026-10-28

## Inputs and coverage

- Original frozen200: 100 Gold answer questions and 100 distinct retrieval qrel questions.
- Added50: 25 Word and 25 Excel questions, each with an answer and source location; these questions were generated from public Office source cells, not copied as official benchmark questions. Their references have not received a separate human semantic review.
- Word source: `superdoc-dev/docx-corpus`; Excel source: `msaltnet/excel-search-mcp` public workbook collection. The ECS downloaded all raw sources directly. For the Word source, advertised source identifiers did not match file bytes; actual SHA-256 and matching repeat-download bytes were recorded instead of claiming the source IDs were verified hashes.
- Original200 evaluation: `/opt/amrag/experiments/pdf-table-hardcases-20261027/runtime200-r4`.
- Office evaluation: `/opt/amrag/experiments/pdf-table-hardcases-20261027/office50-v1/evaluation`.
- Original answer continuation: `/opt/amrag/experiments/pdf-table-hardcases-20261027/answers200-continuation-r1`.

## Retrieval evidence

Original 100 independent qrel questions: all 200 paired retrieval results exist. Both retrieval-stage and reranked top-10 metrics are computed over frozen source-document units for text and PDF-page units for PDF questions. These are source-level judgments, not exact chunk-level recall.

Before reranking, matching the user's requested retrieval stage:

| Cohort | n | Table off | Table on |
| --- | ---: | ---: | ---: |
| Original qrel100 | 100 | 77.77% | 77.27% |
| PDF qrel | 41 | 67.72% | 66.50% |
| Text qrel | 59 | 84.75% | 84.75% |

After reranking:

| Cohort | n | Table off | Table on |
| --- | ---: | ---: | ---: |
| Original qrel100 | 100 | 79.17% | 80.83% |
| PDF qrel | 41 | 66.26% | 70.33% |
| Text qrel | 59 | 88.14% | 88.14% |

On the100 paired qrel questions, 97 scores were equal, 2 improved, 1 declined. These counts and the limited source judgments do not establish a general improvement in answer accuracy.

Office50: all100 paired retrieval results exist, including retrieval results for failed generations. Word: 24/25 source documents retrieved in each arm; Excel: 25/25 in each arm. The Office format uses one shared non-PDF parser and index in both arms; the measurement is document-level source recall, not row/cell-level recall. It is a small generated-question control, not an estimate of general Office comprehension.

## Answer status and limitations

- Original run: 40/200 responses before the generation process stopped on explicit HTTP429 `TPM limit reached`; 11/40 were empty. These40 responses came from the reused text-answer cohort, so they cannot measure PDF table-answer quality.
- Office run: 86/100 responses and 14 generation skips after repeated HTTP429; 9/86 responses were empty. Counts before/after were 47/39, so do not compare answer correctness on unmatched denominators.
- Semantic answer correctness: NOT YET SCORED. Empty responses must not be treated as correct.
- All166 shared image captions eventually completed; 13 previously failed GLM requests subsequently succeeded.
- A separate continuation preserves the original40 responses and failure journal. It starts the remaining160 answers with the same model, prompt, temperature and context, waits on explicit429, and skips after bounded attempts. Unknown-outcome failures are recorded without automatic replay. At launch, zero-call preflight exit0; launcher PID2640345 runs in an ECS-independent session. Check its `status.json` and `completion.json` for current progress.
- Old and new data remain isolated from the production index. No production parser switch was made.

### Audit update, 2026-10-28 11:59 Asia/Shanghai

- The original answer continuation completed after a deliberate between-request restart for the speed change. Across its saved original40, resumed149 and faster11 records: 198 answers returned, 2 timeout skips, and 39 of the198 answers were empty.
- The14 Office responses originally skipped due to HTTP429 were retried separately; all14 returned. Office total is100 returned answers, including9 empty responses.
- `audit_ecs_eval250.py` exit0 verified the frozen hashes, unique 300 `(qid, arm)` answer slots and the replacement of Office429 skips by their separately journaled retries. Totals: 298 returned, 48 empty, 2 skipped, 250 nonempty answers pending semantic review. The audit does not assign correctness scores.
- The 4096-token experiment is a separate two-answer diagnostic, not a rerun of the 250-question evaluation. No background semantic-scoring job is currently running.

## Evidence

The result counts were calculated directly from the ECS JSONL files and frozen registry on 2026-10-28. `runtime200-r4/completion.json` exit1, and `office50-v1/evaluation/completion.json` exit0 (the latter explicitly includes14 recorded skips). No full250-answer accuracy or acceptance is claimed.
