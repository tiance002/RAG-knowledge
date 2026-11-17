# P0 stage call-order audit

- `RetrievalService.search`: cache hit returns immediately; otherwise vector/fulltext/hybrid executes, followed by before/after deduplication and cache write. Single-mode observations occur before enrichment and sorting.
- Hybrid: vector collection -> BM25 collection -> score normalization -> weighted fusion and top-k -> enrichment -> deduplication. Each emitted list follows the actual list order; normalized mappings are represented in insertion order.
- `batch_rerank`: batch input -> model call or original-score fallback -> combined output -> sorting -> threshold filter -> top-k. `expand_context` emits parent, sibling and neighbor identifiers after context assembly.
- `process_file_parsing`: parser configuration -> parsed count -> saved chunk count -> optional `process_submitted_chunks`. Parser and task spans nest when called synchronously.
- `process_submitted_chunks`: source hash when readable -> persisted chunk IDs -> BM25 write -> embedding -> vector write. These stages report success only after the corresponding call returns. The observed chunk-set fingerprint is not a persisted index version or proof of vector-index consistency.
- Event metadata excludes query and content; candidate snapshots contain hashes, IDs, scores and location metadata. Existing algorithms and external calls remain unchanged. Live database, vector service and model calls are not covered by the local mocked tests.

`ingest.file_lineage` and `retrieval.file_lineage` carry a source SHA-256 when the source is readable and an observational `index_version`: SHA-256 of the ordered persisted chunk IDs and their content SHA-256 values. This is a fingerprint of the database chunk set, not a persisted database schema version or proof that the vector index matches it. Retrieval computes it at most once per file per enrichment call; instrumentation errors are ignored. BM25/vector success events include the same fingerprint after their respective writes.
