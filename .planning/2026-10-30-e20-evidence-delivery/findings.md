# Findings
- HEAD 722f000; branch codex/pdf-parser-migration; E1.2 eight tracked service edits already dirty and must be preserved.
- chat._chat_stream_normal uses original_content for expanded rows. grounded_generation builds messages from citations without general input token clipping.
- rerank.expand_context knows exact included siblings/prev/next but returns only combined text + seed ID. Parent + neighbors can duplicate members.
- Agent uses separate retrieval tool assembly; do not change its default expansion result or final answer path.
- Actual DocumentChunk schema has no source_metadata column; do not invent per-member table/cell positions. Read actual text/page/heading, preserve source metadata only where verified for that member.
- Target root has no progress.md or docs/adr; existing E1.1/E1.2 reports used.
- Langfuse official best-practices and instrumentation docs fetched 2026-10-30. Keep installed5.0.4 per user scope.

Research: six frozen Office questions,107 actual prior SSE candidates,72 bounded real source chunks; historical candidate replay not live retrieval. No parent relationships in171Office/42430PDF chunks. Excel031 seed -1 suppressed after neighbor grouping into -0, so initial-seed recovery denominator0; post-group delivery is the real loss. B can add66k chars, no quality inference.
Early worker synthetic tests appended default local events log; fixed test isolation, original log not deleted/truncated.

Final caution: Badds mean15945.67context bodychars,17478.5fullrequestchars; Excel adds66067requestchars. Gold-freeadded members not automatically irrelevant. No model tokenizer cached; trueTokenUNKNOWN. Finaltests17pass, aggregateearlier59pass overlaps. Agentusesseparatebuilderdefaultunchanged.
