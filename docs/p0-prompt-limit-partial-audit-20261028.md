# Prompt/output limit study: partial independent audit

Frozen dataset and context: `prompt-limit-factorial-2x2-20261028-v2/manifest.json`, SHA-256 `54cebe309024b751357609467c6b0a097e1478005eddca74f3cc1c7a716c676c`. Each case uses saved before-arm context; ordinal 137 shares the live10 question but not its after-arm input. One observation per cell; stochastic results are not a general accuracy estimate.

Manual review of six returned outputs against the frozen Gold and actual saved contexts:

| ordinal | arm/cap | Core answer against Gold | Citation assessment | termination |
|---|---|---|---|---|
| 83 | baseline/2000 | Correct: resolution better than 5m | Names section 6.1.1; no clickable evidence ID | stop, nonempty |
| 83 | concise/2000 | Correct: resolution better than 5m | Quotes section 6.1.1; no clickable evidence ID | stop, nonempty |
| 83 | baseline/4096 | Correct: resolution better than 5m | Names section 6.1.1; no clickable evidence ID | stop, nonempty |
| 83 | concise/4096 | Correct: resolution better than 5m | Names section 6.1.1; no clickable evidence ID | stop, nonempty |
| 122 | baseline/2000 | Correct: income 51,971.46 and expenditure 54,488.17 万元 | Names cn-office-2024.pdf and table; no clickable evidence ID | stop, nonempty |
| 122 | concise/2000 | Correct: income 51,971.46 and expenditure 54,488.17 万元 | Names table, not file; no clickable evidence ID | stop, nonempty |

Source evidence: `runtime200-r4/contexts/83-before.txt` source `6375aca51735a5ba906eeede.md`, section 6.1.1 states “影像星下点的分辨率应优于 5m”; `runtime200-r4/contexts/122-before.txt` source `cn-office-2024.pdf`, 公开01表 labels 本年收入合计 51,971.46 and 本年支出合计 54,488.17, unit 万元. Raw provider responses remain in the private ECS directory. Correctness and citation usability are separately assessed; neither measures the application citation pipeline.

V2 122 baseline/4096 timed out: outcome UNKNOWN; no automatic replay. Nonattempted slots are NOT RUN, not scored. Original historical 63 baseline/2000 response was empty with `length` and 2000 completion tokens; its concise/2000 attempt timed out UNKNOWN and was excluded from v2. Live Langfuse instrumentation was absent from these runs; no live trace is claimed.

Continuation ordinal 122 concise/4096: correct on both values, cites document [4] and 公开01表 in the fixed context; this is a textual reference, not a verified clickable application citation. Finish stop, nonempty; usage prompt 14151, completion 830. The process stopped after this response and before another send.

Ordinal 137 (`cn-fiscal2024/v1:cn-audit-2024:answer:4`), fixed Gold: total-account housing expenditure 14,009.54 万元; fiscal-appropriation account 13,898.15 万元. Saved before context `238722d1b532af47001e6b43643b6eaf64a45ab84ebb407cd480a2c71edbd76b` contains cn-audit-2024.pdf 公开03表 住房改革支出 14,009.54 and fiscal description 住房保障支出 13,898.15. It is not the live10 after context.

- Concise/2000: `根据参考资料，审计署2024年度“住房保障支出”在总收支表与财政拨款收支表中的金额如下：\n\n1. **总收支表**：**13,898.15 万元**。根据文档[7]中“` The first value contradicts Gold; second is absent and citation incomplete. `length`, nonempty, prompt 12,677 / output 2,000 tokens. Core FAIL, citation FAIL.
- Baseline/4096: content empty. `length`, prompt 12,662 / output 4,096 tokens. Core FAIL, citation absent.
- Concise/4096: `根据参考资料，审计署2024年度“住房保障支出”在两个口径下的金额如下：\n\n1. **总收支表**：13,898.15 万元。根据文档[1]（审计署2024年度部门决算情况说明）“二、支出总计217,720.33 万元”部分第6项可知，住房保障支出（类）支出为13,898.15万元。\n2. **财政拨款收支表**：13,898.15 万元。根据文档[10]（公开04 表 部门：审计署）表格中“六、住房保障支出”栏次可知，金额同样为13,898.15万元。\n\n因此，按这两个口径的顺序，分别均为13,898.15万元。` First account wrong, second matches Gold; source citation for first account does not support claimed account. `stop`, nonempty, prompt 12,677 / output 3,450 tokens. Core FAIL; citation FAIL for first claim. Each is a single stochastic response; no general conclusion is inferred.

Baseline/2000 produced provider-observed `length` and 2,000 output tokens, but wrapper failure prevented raw response persistence. Answer content UNKNOWN; no correctness score and no retry.
