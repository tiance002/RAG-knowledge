# Progress

## Table-fix ablation — completed
- Added enhanced PDF_TABLES_ENABLED master switch (legacy production mode unchanged), conservative >=3scanrows admission, compact-label scan header inference (unknown => numbered columns, no lost rows), and bounded all-cell padded/high-contrast300dpi local OCR. Original page OCR and crop outputs retained; no lexical guessing or cloud OCR.
- Real pages:catdog41table2->0;physics16C1/C2 firstrow retained and major corruptMicrowaves/Aerial/Interference cells recovered;physics13retains5rows/headers. Minor character ambiguities remain flaggedSCAN_TABLE_OCR_UNVERIFIED.
- Windows21PASS exit0(final24.98s),ECSisolated21PASS exit0(14.36s),including actual3PDFregressions. Full134page same-version off/on parsing completed exit0;pageOCRhashesexactlyequal andimagecaptionsidentical. Historical journals unchanged.
- AblationCOMPLETED:24freshanswers,455embeddinginputs/58rerank,122HTTPsuccess,UNKNOWN0,exit0,1222.167s. Original8core6/8both,pageRecall.8333both;table4core4/4both,Recall1both;all12core10/12both,pageRecall.9both. Emptylength2both(111/114off,113/114on), no retuning/replay.
- Source16OCRnormalizedediterrors345/742->4/742(46.50%->.54%),exactcells11/20->17/20. Linuxsource/languagehashesmatch;strictrealrowcheckexit1 due onein/In capitalization;casefoldallrowsPASS. Strictfailure retained rather than relabelled.
- TablepromptTokens51401->37250(-27.5%),all172268->156730(-9.0%). Parsing114.607s->154.108s(+39.501s),chunks413->438. BetterOCR/compactevidence, no measured accuracy/recall gain. Reportdocs/pdf-table-ablation-20261027.md.
- Productionafterexactsource/PDF/model/countmatch6/11779/37271;isolatedcustom_models0. RawbundleSHA2da061d8708b575f43180d5d5d848094f650fa4a88660ac3a11a36cd9f4e8739. No service switch/push/tag, no morepaidcalls planned.

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

## ECS dataset200 dispatch / status request
- Frozen200 unique cases and complete paired PDF/text preprocessing; real retrieval0/400, answer0/200.
- New executor zero-call preflight r1 failed AST template counting, fixed without provider calls. r2 actual exit0: snapshot vector coverage and real bulk BM25 equivalence PASS.
- Detached ECS launcher dispatched independent session PID2483491; all dependencies/data/output are server-local, no PC execution dependency or automatic restart.
- Provider blocked at first caption phase: two concurrent Qwen/Qwen2.5-VL-72B-Instruct requests both403 code30003 Model disabled. Caption0/166; embedding0/rerank0/generation0; completion exit1; state UNKNOWN_STOP. No replay/fallback, preserve runtime200-r1 journal.
- Need restore same VLM or explicitly select shared ingestion replacement before separately journaled continuation. Production unchanged. See docs/dataset200-ecs-runtime-status-20261027.md and compact var status evidence.

## Persistent GLM replacement — owner correction
- Owner explicitly requested project-wide persistent Qwen VLM -> GLM5.3Flash, not one-call override. Authenticated provider /models HTTP200 confirms exact zai-org/GLM-5.3-Flash.
- ECS migration final exit0: same VLMid now GLM, all6KBrefs; backend .env fallback persisted securely. Othermodels unchanged, counts6/11779/37271. No production restart/index/parser switch. Existing settings caches not restarted; DB-backed KBlookups updated.
- First migration attempt beforemutation failed actualtable name files; correctedfile_documents, backupretained.
- ECS model-policy.json asserts future benchmark VLM consistency; fresh runtime200-r2 reuses no-call prep; r1 failed journal retained. GLMpreflight exit0; launcher2487337 detached to independent session, no PCdependency.
- OCR verified actualsourceandECS languagefiles: Tesseract viaPyMuPDF get_textpage_ocr eng+chi_sim, wholepage150DPI/cells>=300DPI; GLM iscaptionmodel, notOCR.
- GLM runtime actual progress check: first2captionrequests HTTPsuccess, finish_reason=stop, nonempty950/348characters. Fourcaptionrequests dispatched at check,2completed; retrieval0/400/answer0/200. Actualps shows launcherPID2487337 PPID1 SID2487337 TTY?, child2487338 in same detachedSID: survives closed SSH. No claim fullablation complete.

## Status check 2026-10-27 22:54 Asia/Shanghai
- runtime200-r2 stopped at22:20:08: statusUNKNOWN_STOP phaseCAPTIONS, benchmark exit1.39/166caption responses completed (HTTPsuccess39); one further VLMrequest has noHTTPstatus and ended openai.APITimeoutError: Request timed out. Total40captionattempts. Embedding0/rerank0/retrieval0of400/answer0of200.
- Detached process no longer present because of controlled failure exit, not PC/SSH dependency. Completed39captions and the unresolved request journal remain onECS. No automatic replay/reset/model fallback. Fullqualitymetrics unavailable.

## 2026-10-27 GLM retry continuation
- Owner authorized reusing successful captions and retrying failed/unknown caption requests until success. Failed r2 journal retained. r3 stopped before any provider request because asset header did not reach the request guard; status RUNNING in its state was stale and completion exit1 is authoritative.
- r4 fix derives asset SHA from actual image request body; records each attempt and continues caption timeout / HTTP failure retries with bounded exponential backoff. Keeps other phases' existing failure-stop behavior; no general claim all-stage timeout resilience.
- Remote check_ecs_caption_retry.py simulated first ReadTimeout then success with real ledger: exit0 PASS, zero real provider calls. Preflight r4 exit0 zero calls. ECS detached launcher PID2506824, no local computer dependency.
- r2 had39 HTTP-success responses but one finish_reason=length;38 complete descriptions reused, one incomplete result retained but regenerated. r4 actual check:45 complete captions across old+new,7 new HTTPsuccess,2 in flight, retrieval0/400 and answers0/200; no real recurrent timeout observed since restart.
- Current 200-case corpus:14 PDFs and reused text sources (MD/TXT; SciFact/MIRACL JSONL -> text); no native DOCX/XLSX. 82 newly authored questions are PDF-specific because owner asked for PDF table ablation; this is a coverage gap for general document format evaluation, not public dataset unavailability. Keep frozen200 stable; any office evaluation needs separately versioned corpus and qrels.

## Partial results 2026-10-28
- runtime200-r4 completed400 paired retrievals and all166 image captions,13 caption retries later succeeded; generation stopped at40/200 due explicit HTTP429 TPM limit,11 of40 answers empty. Completion exit1; originaljournal preserved.
- 100 independent qrel questions source-level recall@10 off79.17% / on80.83%; PDF41 off66.26% / on70.33%; text59 both88.14%;97same/2improved/1declined. No answer-correctness conclusion.
- office50 generated source-cell QA,25 DOCX+25 XLSX;100 paired retrieval outputs, document recall Word24/25 each, Excel25/25 each; generation86/100 with14 skipped on429,9 empty, uneven arm denominators.
- Office retrievals use same non-PDF parser/index in both arms; doc-level not cell-level recall. 50QA machine-generated from source, not official benchmark. Word sourceID/hash mismatches preserved; real content SHA and repeated-download equality recorded.
- After owner-authorized continuation/skip, separate answers200-continuation-r1 reuses40 and plans160; zero-call preflight exit0 and detached ECS PID2640345. Explicit429 waits60s, skips after3, unknown outcomes retained without replay. First continuation answer completed. Answer semantic scoring pending.
- See docs/eval250-partial-results-20261028.md. No formal service switch.
- Metric correction: previous79.17%/80.83% figures are after reranking. Original100 before-rerank retrieval-stage source recall@10 was77.77%/77.27%; PDF41 67.72%/66.50%, text59 84.75% both. Do not label post-rerank score retrieval-stage or equate source-level judgments with chunk-id qrels.

## User-requested speed increase and retry of 429 answers
- Original slow continuation stopped only at an audited between-request checkpoint after149 visited (147 completed, 2 unknown-timeout skips); ledger and exit-15 retained, no in-flight request interrupted.
- Fresh answers200-fast-r2 zero-call preflight exit0 planned11 remaining, same frozen contexts/model/prompt and temperature. Fixed inter-request delay reduced15s ->2s; explicit429 uses60s backoff and max3 attempts, unknown outcomes remain skipped.
- Office retry429-r1 zero-call preflight exit0 confirmed14 failed generations each had only RateLimitError. On ECS, sequencer PID2667255 runs fast-r2, then Office14 sequentially to avoid TPM contention, remains independent of PC. Office generation context was not preserved on skipped attempts; retry reconstructs retrieval and rerank from the same frozen index/config and records new context hashes, preserving original results and judgments.
- Observed first fast response HTTPsuccess:42.66s provider latency, then next request after2.00s. Faster pacing does not remove provider inference time or guarantee avoidance of429. No model/Prompt/production switch.

## Empty-answer audit
- Offline ECS script inspect_ecs_empty_answers.py exited0: all48/48 empty responses recorded finish_reason=length and completion_tokens=2000. Prior model-answer name XingChenAGI/Xing4.0-29B. Usage did not include reasoning_tokens details, so the reason content stayed empty (hidden reasoning versus other provider/model behavior) is not established. API success does not mean a usable answer.
- Empty outputs were not retried: previous continuation covered unanswered or explicit429 requests, and the runner accepted HTTP-success empties. Source outputs are preserved and should remain incorrect/no usable answer in original A/B accuracy; any later same-model retry or increased max_tokens must be reported separately with both arms receiving identical settings.

## 4096 answer output limit
- User requested4096. Actual old evaluation and non-streaming LLMService.generate set max_tokens=2000; production streaming omits max_tokens and Agent ChatOpenAI default None, so provider defaults apply and 2000 is not the project-wide old default.
- Local candidate adds configurable LLM_ANSWER_MAX_TOKENS=4096 to stream/non-stream/ChatOpenAI default, keeps explicit override and rewrite200/VLM1000. Stage ECS candidate from live source and safe exact patch; production unchanged per no-switch instruction.
- Local pytest exit1 during collection because local venv lacks langchain_openai; ECS staged actual pytest exit0 2passed; logs/evidence in docs/answer-output-limit-4096-20261028.md.
- 4096 diagnostic on one historical empty question, both arms, frozen same model/prompt/contexts; preflight exit0 zero calls; detached launcher2683412,2 actual calls underway. Old2000 results unmodified; no mixed-parameter accuracy claim.
- 4096 real paired diagnostic completed exit0: previously empty same question returned964/955 answer characters, finish_reason stop, completion tokens3603/2624; provider returned reasoning_content8824/5039 characters. Supports output-budget explanation for this case, not an established cause or fix for all48. Full new 4096 evaluation not run.
- git diff --check for modified candidate files exit0 after preserving newline style.

## 250-result status audit 2026-10-28
- User asked whether results are out. ECS audit_ecs_eval250.py exited0 after validating frozen hashes, unique300 answer slots, old/new lineage and Office429 retries:298 responses,48 empty,2 timeout skips,250 nonempty awaiting semantic review. No background semantic-scoring job exists; correctness remains NOT SCORED.
- Recall source-level original qrel100 remains pre-rerank77.77%/77.27%; post-rerank79.17%/80.83%. Word/Excel source-doc recall24/25 and25/25 both arms; Office generated QA and doc-level only. 4096 was only a2-answer diagnostic, not a250-question rerun.
- 12 regression answers inspected; two records skipped at q111; other nonempty need complete semantic scoring and source/contradiction review before reporting accuracy.
