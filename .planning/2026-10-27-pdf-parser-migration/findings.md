# Findings

## Follow-up correction
Real table samples existed in the original three sources:reportPDF45(printed43) has a four-column semi-bordered survey table;physicsPDF13 has a two-column colored raster table. V1 omitted structure, not sources. Broad read-only scan38386magic candidates/65archives found only3uniquePDFs. V2 restores native10rows and scanned5rows including headers; page/cell coordinates retained. Local and Linux17testsPASS, real ECS paired parsePASS,0modelcalls. Old v1 Recall/QA scores remain historical only.
- Upstream PDF parser reads sorted text/image blocks; lacks page classification/OCR.
- Old OCR uses PyMuPDF/Tesseract eng+chi_sim, 150 dpi, full=True; traineddata exists locally. New project must not import old business code.
- Target DocumentChunk lacks metadata column. Provenance must survive preview/confirmation and reach BM25/Chroma/citations. Prefer source-hashed sidecars without production schema change.
- Upstream pins PyMuPDF1.26.7. Old venv1.26.4 lacks pandas/sqlmodel/celery; creating target venv.
- User confirmed root/ECS PDFs; old evidence identifies 120.55.115.162 and key path, requiring fresh verification for new deployment.

- Fresh ECS verified /opt/amrag Python3.12/PyMuPDF1.26.7. Production DB readonly6KB/11779files/37271chunks. Three unique PDF hashes134pages. Source-hashed local copies verified.
- Old PDF method AST matches upstream legacy method. ECS adds count_tokens and proper /rerank transport, VLM caption backfill and text-only-model image handling. Real comparison uses staged actual ECS retrieval code, not upstream rerank.
- Production prod-mmlongbench-doc lacks native45page report although3questions target it. Both experiment arms intentionally include same3source hashes; this is a fresh paired baseline, not historical production score.
- Pre-existing VLM caption text is frozen identical per source/page in both arms, no new VLM requests. Cases/Gold matched official HF rows, source/page qrels obtained before inference;2cases lack page qrels and excluded only from recall, retained in answer scoring.
- R2 raw parsing: before68/21image-only chunks and188text chunks; after158/62/260chunks,39751OCR chars and40native table rows. Scan-grid opt-in tested separately on real raster fixture, not claimed validated on borderless/merged scanned tables.
- Isolated remote experiment /opt/amrag/experiments/pdf-parser-20261027-r1; input SHA7ae9e0fa..., same Xing4.0-29B/BGE-M3/BGE-reranker-v2-m3. Max1024embedding inputs,64rerank,16generation; stop on provider failure, no automatic replay.
