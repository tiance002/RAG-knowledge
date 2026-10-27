# Progress

## Table-fix ablation — active
- Added enhanced PDF_TABLES_ENABLED master switch (legacy production mode unchanged), conservative >=3scanrows admission, compact-label scan header inference (unknown => numbered columns, no lost rows), and bounded all-cell padded/high-contrast300dpi local OCR. Original page OCR and crop outputs retained; no lexical guessing or cloud OCR.
- Real pages:catdog41table2->0;physics16C1/C2 firstrow retained and major corruptMicrowaves/Aerial/Interference cells recovered;physics13retains5rows/headers. Minor character ambiguities remain flaggedSCAN_TABLE_OCR_UNVERIFIED.
- Windows21PASS exit0(final24.98s),ECSisolated21PASS exit0(14.36s),including actual3PDFregressions. Full134page same-version off/on parsing inprogress. No paid model calls yet in this ablation; historical journals unchanged.

## V2 retrieval/answer run completed
- Frozen parser b681671, same3rawECS PDFs, original8plus4source-reviewedtableprobes.277legacy/441v2chunks;636uniqueembeddinginputs.24freshpairedanswers, no historical answer reuse.
- Zero-call preflight exit0; frozen modelIDs/endpoint hashes/production source/prompt AST verified. Runtime exit0,756.706seconds;137HTTPsuccess(51embeddingrequests,62rerank,24generation),UNKNOWN0,no replay.
- Original8:pageRecall@10 .8333both;core6/8before,4/8after. Table4:Recall1both,core4/4both. Combined10eligibleRecall .9both;core10/12->8/12.115after contradictory no-tiger/unknown count markedwrong; loose scorer9/12 still no gain. Candidate113/114empty-length;baseline115empty-length.
- Table rows reach correct retrieved top10, but baseline flattened native text and frozen VLMcaption already contain all4answers. Table4generationpromptTokens86324->39038(-54.8%); total276874->156216(-43.6%). Efficiency signal, no proven accuracy improvement or table-only causality.
- Full-corpus audit foundUI screenshotcatdog41 wrongly admitted2rows;physics16true table firstdata rowusedasheader and OCRdamaged. Frozen product code unchanged, failures retained; no assertion these caused particular answer errors.
- Production postcheck exact source/PDF/model/count equality(6/11779/37271);isolatedcustom_models0. Evidence bundleSHA c72f6d45180c9810a478a7061ec05d6abc8a84d9c2cb1d06b10848a22abe7d02. Report docs/pdf-v2-evaluation-20261027.md. No production switch/push/tag. Parser17tests NOT RUN this round; previouscommit evidence remains historical.

## Owner follow-up completed
- Discovered3uniquePDFs/43copies after38386magic checks plus65archives; no extra originals. Rendered report45 and physics13 to confirm genuine tables. Corrected prior claim of missing real samples.
- V2 narrow native ruled-borderless path and colored-grid/local empty-cell OCR path added. Real native0->10rows/4cols;scan0->5rows/2cols. OCR character uncertainty remains explicitly marked.
- Windows17passed(exit0),Linux17passed(exit0),ECS real-file paired comparisonexit0. Production source/PDF hashes and6KB/11779files/37271chunks unchanged.0newprovidercalls.
- Preserved failures:old skill-path miss corrected;temporary debug expression SyntaxError replaced by file script;first colored-grid test failed at equal-color intersections, then bridging fix passed. Historical v1 model results not relabelled as v2.

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
