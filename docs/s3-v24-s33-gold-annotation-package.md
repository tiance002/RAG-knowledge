# S3.3 Gold候选标注包：12题，未评分、未独立复核、未准入

本包只推进已有S3.2草案8道DSM与4道九寨沟的具体标注准备；没有新增题、替换冻结DEV20、重写原Gold或读取FINAL/混合Gold。12题、2个既有已暴露来源组、0个已准入独立家族、0个正负控制实跑、0个模型调用、0个新增真实回放。最终统一 `DRAFT_UNGRADED_NOT_ADMITTED`，真实基线仍 `BLOCKED_DATA`。

“Gold候选”指可交给标注者逐条核对的临时命题。不是Gold签发，也不宣称答案正确率。每题component为本次AI拆分；必要性、组件边界、语义蕴含、独立复核签名均未通过。`UNKNOWN`是明确缺口；`NOT_RUN`是没有执行，不是失败评分。原问题文字与S3.2逐字一致。

## 证据链、快照与成员边界

`HISTORICAL_VAR`为既有RSI本地var档案根目录别名，`DEV_SNAPSHOT`为已绑定的qa500-ecs-sources-20261116快照根目录；完整机器路径仅在既有受限DEV-only回执中恢复，不在此复刊。源文件相对定位见CSV `source_artifact_ref`。两份新增文件为UTF-8（CSV带BOM，RFC4180引号转义；一题一行）。

只读复用 DEV-only audit `HISTORICAL_VAR/rsi-qa500-selected-dev-20261116/audit-r2/audit.restricted.json`，重算 SHA256 `56c6343f1628a3cba0e325b392f899fb40176ee71d7e6d0f8f426427c4ab787c`。仅用其中两份明确DEV来源，逐份源字节哈希重核一致；没有打开混合Gold。

只读复用 nonsealed native receipt/capture `HISTORICAL_VAR/rsi-v23-qa300-selected-20261116/gold-native-capture/{receipt.json,capture.restricted.json}`；capture SHA256 `2545dee8a4a2248ef64cf756a9a0c1cc4712d4ad184e8677bf93b7dfc4404528`。这是既有解析行窗口，不证明当前active index，也不是完整原生成员池。另核对DEV parsed capture（SHA256 `f666610241dd576c2ed6b0ee3aa7d192dc3b5d76f8c882ed93bc2943360f2a34`）中无这两源的直接file_id/源SHA匹配，不猜测跨快照映射。

DSM可见原生窗口只有original_index=10/11/12，原文区间[4918,5473)、[5423,5940)、[5890,6469)。它们与本次目标必要节段不重合。仅有同文档/其他节段不能绑定目标atom；8题目标原生成员、实际seed及控制均UNKNOWN。节段跨越不自动形成跨成员关系；表1压缩排版的逐格数值和裁切公式保持UNKNOWN。

九寨沟capture总共2个重叠原生行：index0 `[0,519)` 和index1 `[469,579)`。本包四题完整目标证据均只由index0包含；index1与JZG-03末尾只重叠3个字符，其余主要为末尾受损段，不能支持完整必要事实，也不能当作第二个必要支持成员。故本包所称“单chunk”仅指已知必要支持几何：若index0为seed，则不能删除唯一支持seed来构造nonseed负控制；新题实际seed分配和更广成员池均UNKNOWN，未声称未来任何配置都不可能。保持原生行，不复制、不拆块。

引用判据：回答每个必要组件须以相同源SHA和精确span支持；原生引用还须绑定chunk_id/content_sha256和真实成员/seed。当前只验证来源定位及可见成员包含；没有给模型生成回答打分，没有把引用可定位当作回答正确。

[逐题机器可读审计](evidence/s3-v24-s33-question-audit.csv)使用一题一行，共12行；JSON单元保留组件、谓词与span。[来源家族映射](evidence/s3-v24-s33-source-family-map.json)和[可行性矩阵](evidence/s3-v24-s33-dev20-r2-feasibility.json)独立报告家族与资格；它们不能由本包关键词或题数推导通过。

## DRAFT-DSM-01
问题：根据该报批稿，不同纬度区域分别采用什么投影？哪些情况下允许采用其他坐标系或投影方式？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP;NEGATION_CONDITION;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-01-C1`：纬度带与投影对应：南纬88°至北纬88°用UTM、6°分带、坐标单位m；南北纬88°至90°用极地方位投影且切于极点。
- `DRAFT-DSM-01-C2`：坐标系默认为2000国家大地坐标系；其他坐标系或投影/经纬度坐标须确有必要且依法批准。

字面谓词草案：`{"ALL_verified_tokens": ["UTM", "6°", "m", "2000", "88°", "90°", "确有必要", "依法批准"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `4.2.1 数学基础`：`[3792,4036)`，行 `11–12`，span SHA256 `961f8008c93600a872e542ca1a4964119adaa4019325c4e3e16f006821bc2c4e`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-02
问题：比较南北纬88°之间与极地区域的存储单元规定，并说明其与投影分区的对应关系。

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `CROSS_MEMBER_RELATION;UNIT_SCOPE_OWNERSHIP`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-02-C1`：88°之间区域按1:50 000分幅存储，分幅规则依GB/T13989。
- `DRAFT-DSM-02-C2`：88°至90°两极区域各存为一个单元；这与前节相同纬度带的投影规定对应。

字面谓词草案：`{"ALL_verified_tokens": ["1:50 000", "GB/T13989", "88°", "90°", "各存储为一个单元"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `4.2.1 数学基础`：`[3792,4036)`，行 `11–12`，span SHA256 `961f8008c93600a872e542ca1a4964119adaa4019325c4e3e16f006821bc2c4e`。
- `4.2.2 存储单元`：`[4036,4135)`，行 `12–12`，span SHA256 `9b765469996d0bd12b8cafca127d48bc1a23cab27e2173e1c5e5272262a64486`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-03
问题：按文本，格网大小有哪些方向一致性、单位和推荐取值要求？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-03-C1`：产品为矩形格网；格网大小为整数、单位m；南北和东西方向大小一致。
- `DRAFT-DSM-03-C2`：全球产品宜用5m和10m，推荐语气不改成必须。

字面谓词草案：`{"ALL_verified_tokens": ["矩形格网", "整数", "m", "保持一致", "宜采用", "5m", "10m"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `4.2.3 表达形式`：`[4135,4224)`，行 `12–12`，span SHA256 `dcc8a546b895b55f3d138ee6472f5d4718e6c500f885f3c739866d6436527879`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-04
问题：缺少影像造成的高程无值区和海域分别如何赋值？为什么不能混用这两个数值？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-04-C1`：缺少影像资料等导致的高程无值区赋-9999。
- `DRAFT-DSM-04-C2`：海域赋-8888；两值与对象分别对应，不能交换，不增加外部因果。

字面谓词草案：`{"ALL_verified_tokens": ["高程无值区", "-9999", "海域", "-8888"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `4.2.5 高程赋值`：`[4589,4667)`，行 `12–12`，span SHA256 `35c4c69bdf17f689cf50683d55a8b823d1eb120259c0f0e299fc0697611ba2b9`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-05
问题：两幅相邻图幅地形类别不同且接边高差超限时，该如何确定接边误差标准并处理同名格网点？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `NEGATION_CONDITION;CROSS_MEMBER_RELATION;UNIT_SCOPE_OWNERSHIP`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-05-C1`：相邻图幅地形类别不同，接边误差按低精度执行；超限同名格网点据立体模型修改，保证合理接边和平滑自然过渡。
- `DRAFT-DSM-05-C2`：接边重叠带同名格网点高差不大于2倍高程中误差；同一点高程应一致；数值表1各格网/地形明细暂UNKNOWN。

字面谓词草案：`{"ALL_verified_tokens": ["不大于 2 倍", "低精度执行", "根据立体模型修改", "应一致"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `9.3 接边处理`：`[9556,9746)`，行 `17–17`，span SHA256 `2a47b54a9d57ccecbd4af54d98efb639dc16185de65c9701fbb87c07b80c4089`。
- `4.2.4 高程精度`：`[4224,4589)`，行 `12–12`，span SHA256 `9dc57529ece4d6e3723697c410c9ed92bc96767152693720873e39e6963709cf`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-06
问题：在DSM拼接前、拼接时和分幅裁切时，分别需要满足哪些条件或处理规则？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `NEGATION_CONDITION;CROSS_MEMBER_RELATION`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-06-C1`：拼接前接边较差应合规定；拼接同名点高程取中数；地物地形自然过渡且无明显痕迹。
- `DRAFT-DSM-06-C2`：分幅裁切依据§4.2格网大小及裁切范围；裁切外扩50个格网规则需保留；公式精确形式暂UNKNOWN。

字面谓词草案：`{"ALL_verified_tokens": ["接边较差符合规定要求", "中 数", "无明显拼接痕迹", "格网大小", "数据裁切范围", "50"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `9.4 数据拼接与裁切`：`[9746,9890)`，行 `17–17`，span SHA256 `c1e1e8463fa33d441eaf4ef37e4bab894307538fc8355bfa97e18d043393c470`。
- `4.2.3 表达形式`：`[4135,4224)`，行 `12–12`，span SHA256 `dcc8a546b895b55f3d138ee6472f5d4718e6c500f885f3c739866d6436527879`。
- `4.2.6 数据裁切范围`：`[4667,4876)`，行 `12–12`，span SHA256 `b3197d3fa1ef0688d36100f2783c38c01ef78d9e3deccac61ee70f8fdadb0c2e`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-07
问题：影像预处理和数字表面模型生产两个环节的质量控制检查重点有什么区别？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `CROSS_MEMBER_RELATION;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-07-C1`：预处理检查像素增减、阴影/云影细部与立体效果、同名点灰度及边缘变化、整体灰度色调纹理层次。
- `DRAFT-DSM-07-C2`：DSM生产检查接边镶嵌裁切、高程异常、参数、高程中误差、粗差与同名点高程、元数据及相关文件正确完整；分别归属两道工序。

字面谓词草案：`{"ALL_verified_tokens": ["影像像素", "阴影", "同名点影像灰度", "纹理", "接边", "粗差", "元数据"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `10.2.2 影像预处理`：`[10420,10569)`，行 `18–18`，span SHA256 `cacf1144bd9119c21700e28e3d28db608025d1b895094a4c9c4dd3387a6a66e0`。
- `10.2.4 数字表面模型生产`：`[10723,10902)`，行 `18–18`，span SHA256 `63de74097a997764aad5a531b3fc0554725a7c2e87b4a0c41b992790bfdb00bd`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-DSM-08
问题：按成果整理要求，数据文件和文档资料分别应包括哪些内容，目录及文件组织由什么规定？

来源：`DSM`；文件 `bee35f97-cba0-43ea-9ef9-ee8efdaf022b`；源 SHA256 `a00fca96fed4629eb7d38db5b18b25f50d5b533f8e420ccb2f72366d402fa654`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-DSM-08-C1`：数据文件包括DSM数据和DSM元数据。
- `DRAFT-DSM-08-C2`：文档资料包括成果清单、分幅结合表、技术设计书、技术总结、检查报告与验收报告、其他相关资料；目录和文件组织由技术设计书规定。

字面谓词草案：`{"ALL_verified_tokens": ["DSM 数据", "DSM 元数据", "成果清单", "分幅结合表", "技术设计书", "技术总结", "检查报告与验收报告", "其他相关资料"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `11.1 成果整理`：`[11532,11719)`，行 `19–19`，span SHA256 `b3af8f5167b4688f66608849ce38803565ed615c9bc9c4712e1e2b7c22242f53`。

原生成员：`UNKNOWN`。绑定状态 `UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`UNKNOWN`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `UNKNOWN_NO_TARGET_NATIVE_BINDING_OR_SEED`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_VERIFIED;NATIVE_TARGET_UNKNOWN`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-JZG-01
问题：按该文本，九寨沟1992年、1997年、2000年、2001年及2007年的认定或证书事件分别是什么？

来源：`JZG`；文件 `aeb51a7c-bf57-4d94-ae8a-12074230297d`；源 SHA256 `731d9e51e79964747dd60d663d734659a9ddc10e7b6b5f568dbc901f63b4c8ea`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-JZG-01-C1`：1992世界自然遗产；1997世界人与生物圈保护区；2000中国首批国家4A级旅游景区。
- `DRAFT-JZG-01-C2`：2001年2月绿色环球21证书；2007国家5A级旅游景区；保持日期-事件配对。

字面谓词草案：`{"ALL_verified_tokens": ["1992", "1997", "2000", "2001年2月", "2007", "绿色环球21"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `九寨沟风景名胜区于1992年`：`[123,226)`，行 `3–3`，span SHA256 `cd45fc048c23d386b7043b05a904f690231f67cca676c7b57a66a408d78dd74d`。

原生成员：`[{"chunk_id": "45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69", "original_index": 0, "source_start": 0, "source_end_exclusive": 519, "content_sha256": "ed1c3f71090596ab250eb4e9c80633583b51b536e6c4db6beba2851d513e03ae"}]`。绑定状态 `VERIFIED_SINGLE_SUPPORT_MEMBER_IN_CAPTURE`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `BLOCKED_FOR_KNOWN_SINGLE_SUPPORT_SEED_GEOMETRY; proposed_seed=UNKNOWN; broader_native_pool=UNKNOWN`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_AND_CAPTURE_MEMBER_VERIFIED;ANSWER_CITATION_NOT_SCORED`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-JZG-02
问题：树正沟的长度、湖泊数量及其占九寨沟全部湖泊的比例分别是什么？这些量分别描述什么范围？

来源：`JZG`；文件 `aeb51a7c-bf57-4d94-ae8a-12074230297d`；源 SHA256 `731d9e51e79964747dd60d663d734659a9ddc10e7b6b5f568dbc901f63b4c8ea`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-JZG-02-C1`：树正沟全长13.8km，共有湖泊40余个。
- `DRAFT-JZG-02-C2`：约占九寨沟景区全部湖泊40%；长度/局部数量/整体占比对象分别绑定，保留余与约。

字面谓词草案：`{"ALL_verified_tokens": ["13.8km", "40余个", "约占", "40%"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `树正沟是九寨沟风景秀丽的大门`：`[313,366)`，行 `3–3`，span SHA256 `69449ae421ecfec7f7bfbf74553261bed8753f1d5d450d0488ba3b24a0499285`。

原生成员：`[{"chunk_id": "45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69", "original_index": 0, "source_start": 0, "source_end_exclusive": 519, "content_sha256": "ed1c3f71090596ab250eb4e9c80633583b51b536e6c4db6beba2851d513e03ae"}]`。绑定状态 `VERIFIED_SINGLE_SUPPORT_MEMBER_IN_CAPTURE`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `BLOCKED_FOR_KNOWN_SINGLE_SUPPORT_SEED_GEOMETRY; proposed_seed=UNKNOWN; broader_native_pool=UNKNOWN`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_AND_CAPTURE_MEMBER_VERIFIED;ANSWER_CITATION_NOT_SCORED`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-JZG-03
问题：根据该文本对2017年地震后状况的记载，哪些迹象支持当时火花海及原有堰塞湖没有溃决风险？是否发现新的堰塞湖？

来源：`JZG`；文件 `aeb51a7c-bf57-4d94-ae8a-12074230297d`；源 SHA256 `731d9e51e79964747dd60d663d734659a9ddc10e7b6b5f568dbc901f63b4c8ea`。类别为候选标签 `NEGATION_CONDITION;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-JZG-03-C1`：震后主要江河水势平稳，火花海大量流失后9日排空；文本记述当时无溃决风险。
- `DRAFT-JZG-03-C2`：原有堰塞湖仅经初步排查无溃决风险；新堰塞湖没有出现；不能外推当前风险。

字面谓词草案：`{"ALL_verified_tokens": ["水势平稳", "9日", "排空", "没有溃决风险", "初步排查", "無潰決風險", "没有出现"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `2017年8月8日`：`[366,472)`，行 `3–3`，span SHA256 `3561e74388de6d7287263a9343590989cd5a90ced989202214a5c3c922ef7ce4`。

原生成员：`[{"chunk_id": "45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69", "original_index": 0, "source_start": 0, "source_end_exclusive": 519, "content_sha256": "ed1c3f71090596ab250eb4e9c80633583b51b536e6c4db6beba2851d513e03ae"}]`。绑定状态 `VERIFIED_SINGLE_SUPPORT_MEMBER_IN_CAPTURE`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `BLOCKED_FOR_KNOWN_SINGLE_SUPPORT_SEED_GEOMETRY; proposed_seed=UNKNOWN; broader_native_pool=UNKNOWN`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_AND_CAPTURE_MEMBER_VERIFIED;ANSWER_CITATION_NOT_SCORED`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。

## DRAFT-JZG-04
问题：文本中的三条主沟与六大景区分别有哪些？为什么回答时需要把这两组名称分开列出？

来源：`JZG`；文件 `aeb51a7c-bf57-4d94-ae8a-12074230297d`；源 SHA256 `731d9e51e79964747dd60d663d734659a9ddc10e7b6b5f568dbc901f63b4c8ea`。类别为候选标签 `UNIT_SCOPE_OWNERSHIP;CITATION_SUPPORT`，不计通过。

必要事实/组件（临时，不是正式Gold；每组件的源span哈希+literal_token_all_of+typed_relation_requirement谓词见CSV，关系判定器明确UNKNOWN）：
- `DRAFT-JZG-04-C1`：三主沟：日则沟、树正沟、则查洼沟。
- `DRAFT-JZG-04-C2`：六景区：树正、诺日朗、剑岩、长海、扎如、天海；两种分类分别列出，不能把主沟数当景区数。

字面谓词草案：`{"ALL_verified_tokens": ["日则沟", "树正沟", "则查洼沟", "树正", "诺日朗", "剑岩", "长海", "扎如", "天海"], "unmatched": "NONE"}`。这只是源字面约束；对象归属、条件、完整性与语义蕴含仍须独立复核，不能把出现关键词当作答案正确。

精确来源定位（UTF-8解码后的Unicode code points，零基半开区间；不是字节offset；不刊登原文正文）：
- `九寨沟分三条沟`：`[260,313)`，行 `3–3`，span SHA256 `18de7b6d85ff192e2a41d143cb03397ff8e06f51c16ec59adf98d8059e6759e0`。

原生成员：`[{"chunk_id": "45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69", "original_index": 0, "source_start": 0, "source_end_exclusive": 519, "content_sha256": "ed1c3f71090596ab250eb4e9c80633583b51b536e6c4db6beba2851d513e03ae"}]`。绑定状态 `VERIFIED_SINGLE_SUPPORT_MEMBER_IN_CAPTURE`。实际 seed=`UNKNOWN`；仅供复核的seed候选=`45bd0118-c1cd-4947-ac5c-ce2c2c3f2e69`，没有为新题签发seed。
控制：正控制 `NOT_RUN; candidate includes all required members, actual allocation UNKNOWN`；负控制 `BLOCKED_FOR_KNOWN_SINGLE_SUPPORT_SEED_GEOMETRY; proposed_seed=UNKNOWN; broader_native_pool=UNKNOWN`。每个必要组件须证明其合法nonseed删除改变判定；禁止删seed、复制/拆块凑控制。
判定器：independent bilingual-capable human verifies all components against locators; then deterministic literal predicates and every-atom legal paired controls; semantic entailment unresolved。执行状态 `NOT_RUN`；引用状态 `SOURCE_SPAN_AND_CAPTURE_MEMBER_VERIFIED;ANSWER_CITATION_NOT_SCORED`；必须以实际回答逐项核对引用覆盖，尚无回答或分数。
标注者 `Codex draft33 / AI provisional source audit 2026-11-18`；独立复核 `UNKNOWN_NOT_REVIEWED`；家族 `UNKNOWN`；最终状态 `DRAFT_UNGRADED_NOT_ADMITTED / BLOCKED_DATA`。
## 独立标注与验收入口

独立标注者须先逐题确认上列命题是否穷尽问题、每个组件是否真正必要，核对对象/单位/日期/范围/否定/条件；在原文精确span上签名，再核对实际native成员及不删seed的每atom正负控制。DSM-05表1、DSM-06公式尤其不能凭压缩文本抄为Gold。任何暂不能形式化的组件继续UNKNOWN，不能删除难项缩分母。第二位独立审阅者身份、签名、时间与冲突解决均 `UNKNOWN`；审阅决定单列 `review_decision=UNKNOWN_NO_INDEPENDENT_GOLD_REVIEW`，没有通过或拒绝Gold的签发记录；AI交叉检查不充当独立人工Gold复核。

本次只读核对脚本exit=0，逐份源SHA及capture SHA匹配，12题与原草案集合及文字一致；每个span非空且精确起止，native内容SHA匹配且源子串精确包含。CSV每组件保留独立literal_token_all_of与typed_relation_requirement；仅源token存在性已核对，语义关系判定器未实现/未复核，不将token出现代替事实归属。未运行新模型、ECS、真实回放、prepare、性能评估或Gold评分。历史控制失败保持原状态，不在本包重算为成功。

资源：复用2份DEV源、已有DEV-only audit与native/parsed receipts及本地Python；新建本CSV和本标注包两份小文档；容器/数据库/索引/语料副本=0，删除=0。保留原始资料及失败与UNKNOWN证据，磁盘前后由本轮总审计统一记录（并发变化不可全归于本包）。
