# ECS 200-case benchmark: dispatch status

## Scope and frozen inputs

- 100 Gold answer questions and 100 distinct retrieval qrel questions, 200 unique questions total.
- 118 unique existing cases reused; 82 new cases. Original 12-case PDF regression retained.
- ECS-only raw PDF downloads and execution. No production service switch.
- 14 PDFs, 759 pages parsed in both arms; identical underlying per-page OCR hashes checked. Text corpus: 11,183 documents / 25,374 chunks.
- Dataset root: `/opt/amrag/experiments/pdf-table-hardcases-20261027/dataset200`.
- Runtime root: `/opt/amrag/experiments/pdf-table-hardcases-20261027/runtime200-r1`.
- Freeze SHA256: `c3a0a79e6dd53b28c8c2456d08d27d0357137b80633d08fec945839d15a205f3`.

## Actual commands and evidence

1. `/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-table-hardcases-20261027/run_ecs_dataset200.py --root /opt/amrag/experiments/pdf-table-hardcases-20261027 --preflight`
   - First attempt FAIL before inference: prompt AST check counted runtime prompt assignments. Fixed to identify the one f-string template. Original `preflight200-r1.log` retained.
   - Second attempt exit 0; `preflight200-r2.log`: `PREPARED_ZERO_CALLS`, 200 cases, vector snapshot coverage PASS, actual BM25 frequency/average/query score-and-order equivalence PASS.
2. `/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-table-hardcases-20261027/launch_ecs_dataset200.py --root /opt/amrag/experiments/pdf-table-hardcases-20261027`
   - Dispatch command exit 0, independent-session launcher PID 2483491. Python `Popen(start_new_session=True, stdin=DEVNULL, close_fds=True)` detaches from SSH; all scripts, dependencies, documents, cache, outputs and logs reside on ECS.
   - Benchmark process exit 1, persisted in `completion.json`. No automatic restart.
3. `status_ecs_dataset200.py --root /opt/amrag/experiments/pdf-table-hardcases-20261027`
   - `UNKNOWN_STOP`, phase `CAPTIONS`; 2 failed VLM requests, both HTTP 403.
   - Provider error: `openai.PermissionDeniedError: Error code: 403 - {'code': 30003, 'message': 'Model disabled.', 'data': None}`.
   - Model: `Qwen/Qwen2.5-VL-72B-Instruct`.

## Result: BLOCKED, no quality scores yet

- Shared new image captions completed: **0/166**.
- Retrieval completed: **0/400**.
- RAG answers completed: **0/200**.
- Embedding inputs / rerank requests / answer-generation requests: **0 / 0 / 0**.
- Journal and failure evidence retained on ECS; original frozen inputs remain unchanged. Do not reset or replay this runtime.
- No model fallback or substitution was dispatched. Restoring the existing VLM or explicitly choosing one shared replacement ingestion model is required before a separately journaled continuation can proceed. Keep answer model, embedding, reranker, Prompt and retrieval configuration fixed across arms.
- Production service has not been switched. No claim of answer or recall improvement is supported yet.

## Version / acceptance

Parser candidate remains `ca65fa0980d0609d6f2f5b2e5c59bfdc554f6abe`; runtime tooling currently uncommitted. Dataset completion and zero-call preflight are verified; the full ablation has **not passed** and has not been accepted or tagged.

## Owner correction: persistent GLM replacement

The owner clarified that the Qwen VLM must be replaced project-wide, persistently, with GLM-5.3-Flash. The provider's authenticated `/models` endpoint returned HTTP200 and exact ID `zai-org/GLM-5.3-Flash`.

- `migrate_ecs_vlm_glm53.py --root /opt/amrag/experiments/pdf-table-hardcases-20261027` final exit0: updated the existing VLM record in the ECS database, retaining its stable ID so all six KB references use GLM. Persisted the fallback `VLM_MODEL`, endpoint and existing key in the ECS-only backend `.env`; backups remain permission-restricted on ECS. No secrets exported. Existing in-memory settings caches were not restarted; DB-backed per-KB model lookups use the updated record, while environment fallback defaults take effect when processes next start.
- Initial migration attempt failed before mutation on a mistyped table name (`files`); corrected to actual `file_documents`, with backup and failure retained.
- Other model records unchanged; counts remain 6 KB / 11,779 files / 37,271 chunks. No parser service switch, production restart or re-index.
- Added durable `model-policy.json` on ECS. The runner requires its selected VLM to match this policy, preventing a stale Qwen configuration from silently returning. Both experimental arms use the same newly generated GLM caption cache; the original 89 frozen historical captions remain identical in both arms and retain their historical provenance.
- Fresh journal directory `runtime200-r2` reuses completed preprocessing only. `runtime200-r1` and its failed requests are retained unchanged.
- New `--preflight` exit0: `PREPARED_ZERO_CALLS`, actual bulk BM25 equivalence PASS, vector snapshot coverage PASS. Evidence: `preflight200-glm-r2.log`.
- Detached dispatch exit0, launcher PID2487337. All work remains ECS-local and independent of the PC/SSH session.
- Actual process inspection after disconnect/reconnect: launcher PPID1, SID2487337, no TTY; benchmark child2487338 shares that independent session. First2 GLM requests succeeded with nonempty captions (950/348characters), both `finish_reason=stop`. At that check4caption requests dispatched,2completed; retrieval0/400,answers0/200. Full ablation is still in progress.

### Actual OCR implementation

OCR remains local **Tesseract**, called through PyMuPDF `get_textpage_ocr(language='eng+chi_sim')`, using `eng.traineddata` and `chi_sim.traineddata` in the runtime tessdata directory. Full-page OCR uses150DPI; bounded scan-table cell re-OCR uses at least300DPI. GLM replaces image descriptions; it does not replace this OCR path.
