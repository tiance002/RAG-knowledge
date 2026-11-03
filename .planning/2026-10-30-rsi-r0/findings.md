# Findings

## Owner-authorized resource cleanup
- Stopped containers consumed disk, not running-process RAM. 579 removed, 0 stopped remain, 3 running IDs unchanged.
- Chroma copies were not all byte-identical. Counts/collection identity grouped them into 14 x 69,814 records, 3 x 37,271 records, and one independent 1-record sample. Retained runtime200-r4, runtime200-r3, and the tiny ingest sample.
- Complete file hashes plus persisted delta reconstruction preserved every original file identity for 15 removed copies; 48,802 bytes of differences instead of full-size backups. Freed ~9.26 GiB. Current health endpoint OK; actual restore/load test not run.
- Original sources, frozen datasets/qrel, outputs, running services, images and volumes untouched. Recovery pointer files explicitly forbid writing directly to retained shared baselines.
- Baseline HEAD 5e36633; E2.1 scoped context selector remains experimental.
- Root progress.md/docs/adr absent in prior stage; no RSI platform assumed.
- Initial source search requested nonexistent intent.py; locate actual intent logic in agent_workflow.py.

- Primary papers GEPA v1/DGM v1 read; AFlow v1/v3 HTML disabled, v4 HTML and PDF available.
- GitHub unauthenticated API returned403 rate limit; switched to shallow blob-filtered, no-checkout official Git repositories. All3 clones exit0.
- ECS readonly audit exit0: 2CPU, total memory3803394048B, MemAvailable1952268288B, swap0, rootavailable9099952128B; three unrelated/legacy PostgreSQL/Redis containers observed, not proof target RAG uses PostgreSQL.

- Pinned official revisions: GEPA1a435101551629de917a37110b29805268a98bfd (pyproject0.1.4), DGMa565fd2d1dca504ef5104a7cc0f3bdc4ab9b4fd2, AFlow3f457218fc716093fe53f6df8a5d5e6379d66346. Source only, no imports or tests.
- GEPA current API supports typed adapter, per-instance/objective/hybrid/cartesian frontiers; generic RAG adapter builds its own pipeline and estimated-token fields, not a drop-in for target RAG.
- DGM parent patch chain is ordered deltas; default keep_all eligible archive, not only improved children. is_compiled_self_improve is metadata/nonempty-patch/coverage check, not literal compiler invocation.
- AFlow paper alpha.4/lambda.2 differs pinned code alpha.2/lambda.3 and scores*100. format_experience emits Absolutely prohibit for both success/failure; static observation, no runtime claim.
- Confirmed real prompt pilot failure: Word B core/faithfulness/support PASS, coverage3/4, uncited project identity present verbatim in actual model request; frozen messages identical. Mirror case missing required evidence, unsuitable for pure generation core-correctness optimization.
