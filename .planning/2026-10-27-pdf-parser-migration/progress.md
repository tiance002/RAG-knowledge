# Progress

## Latest checkpoint
- Final-r2 COMPLETED:8fresh candidate answers,604embedding inputs,22rerank calls;8baseline answers reused exactly. SourceRecall@10=1.0both/pageRecall@10=.833333both(n6). Core correctness8/8before,7/8after;114empty/length2000,context contains Kaggle/Cat vs Dog. No claim that parser alone caused this generation failure.
- Production postcheck PASS: same6KB/11779files/37271chunks, identical backend source hashes and PDF source hashes. Isolated custom_models count0; model secrets not persisted there.
- Candidate remains opt-in; defaultslegacy because quality gate FAIL. Synced existing deployed count_tokens, standalone-image naming, proper /rerank API and text-only-model caption compatibility instead of overwriting these ECS hotfixes with upstream clone.
- Final delivery Linux14PASS plus7affected-module import smoke PASS. Non-PDF TXT/DOCX/Excel/CSV method AST unchanged from upstream. No more provider requests planned.
- First real A/B completed16generation/664embedding inputs/45rerank calls. Generic numeric-grid admission regression fixed; original answers/contexts/journal retained.
- Final raw parse134pages:277before/413after chunks;39751OCR characters alongside89retained images. Native report193text chunks; falsely admitted chart tables rejected. Real ECS table coverage limited; generated real PDF fixtures cover native and scanned grids.
- Final Windows14PASS and Linux14PASS including actual OCR->SQLite/BM25->provenance. Remote pytest8.3.5 installed only in isolated test-tools.
- First final reuse attempt stopped at zero calls because89image timestamps differed. Verified all inference fields identical; exact previous baseline chunks/answers retained in new final-r2. InputSHA513725e09d43ea654cab4059a4d41a77d72d7ea3b6ef953012eb3be7a289bdfa.
- Final-r2 running8fresh candidate answers and reusing8completed baseline answers. No production replacement, DB mutation or index rebuild.
- CRLF-aware diff-check PASS after removing added .env.example EOF blank. Local language files copied independently into backend/storage/tessdata.

## Earlier history
2026-10-27: Baseline clean at b5ceb463, new branch created. Skills read. Venv started. No models or remote writes.

- Isolated dependencies installed; real PyMuPDF1.26.7 import PASS. First pytest attempt had 2 PASS/8 setup errors because the basetemp parent was absent; fixed parent directory. Second attempt10PASS; third11PASS including actual local OCR, raster-grid table, native table, source manifests, missing-OCR failure and mixed-document protection.
- ECS read-only identity: /opt/amrag,6KB/11779files/37271chunks,3unique PDFs/134pages. Downloaded source-hashed PDFs and existing118-case evaluation bundle privately. No remote mutations or model calls.
- ECS differs from upstream: count_tokens hotfix and VLM-caption/chat changes. Eight PDF questions exist; SciFact/MIRACL are text-corpus controls, not PDF parsing metrics.
- First parse A/B r1 baseline invalid on Windows: legacy emoji logging raises GBK encoding inside image extraction handler and suppresses images. Preserve evidence; rerun with UTF-8, do not report zero baseline chunks as real parser quality.
