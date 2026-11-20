# 最终请求总预算修复与ECS十题复测

## 结果

- 同一10题：超限4→0，空答4→0，非空完整回答6→10；无跳过、重试或替换题。
- 以相同10题为分母，保守严格正确4/10→6/10（40%→60%，+20个百分点）。本轮6正确、3部分正确、1错误。
- 若只看各轮已生成回答，严格正确率分别是4/6与6/10；分母不同，不把这两个比例直接当作同题总体回归结论。qst0126主流程正确，但本轮按完整Gold条件保守标PARTIAL。
- 普通RAG mode=chat；Hybrid、查询改写和rerank沿用原请求，联网关闭，原始历史逐份完全相同。不是Agent工具循环评测。模型路由配置未改变，本轮不作路由开关效果结论。

## 实现

- 新增grounded_request_budget.py，用最终序列化messages/tools字符数计量，合并提示词、用户偏好、工作簿验证材料、当前问题、历史及带来源标签的证据；上限仍32000，未扩大。
- 优先删除较早的完整对话轮次，尽量保留最近指代上下文。仍不足时先移除完整邻块/历史补充，再移除过大的最后历史轮次；最终必要时按现有排名移除低优先级完整证据。首位来源、当前问题和系统规则、原文验证材料被保护。正文不被句中截断或改写。
- 单一prepared request同时用于最终引用SSE和模型发送；流式与非流式共用同一预算。路由特征仍隔离显示偏好。最终guard仍保留，极端情况下固定指令/当前问题/受保护原文自身就超限时明确拒绝，不静默丢掉关键内容。
- 本轮实际只裁历史：5题各省略1～3个较早轮次；10题均未删任何已检索正文/引用，未裁正文。注意这不等于Gold所需资料全部被召回。

## 本地与部署核验

- 本地153 passed、0 failed、0 skipped，命令exit0；359条现有依赖弃用警告保留。覆盖精确JSON边界、Unicode/转义、历史轮次、条件/单位、原文验证保护、SDK请求、流式/非流式和最终SSE引用一致性。
- 早期失败原始日志保留。测试适配包括不完整的工作簿替身、旧Agent评分/重试假设、额外最终引用事件契约和新流式替身回调调用方式；未因此改变正式Agent流程。
- 本地与ECS均对上轮10个实际请求/证据离线重建通过，全部已检索正文保持不变。
- ECS仅发布3个后端文件，先保存旧文件及SHA清单，API/队列空闲时重启API，健康HTTP200。未重解析或重建任何知识库；files/chunks/builds/KB完整SQL记录哈希部署前后和复测后完全一致。
- 独立ECS控制器replay-exit=0、systemd Result=success/ExecMainStatus=0。10题HTTP200、curl exit0，逐项done均COMPLETED，原始stderr为空；错误、空输出及截断均0。

## 逐题结果

字符数为最终应用层序列化消息；固定/历史/证据含标签三项之和已核对。实际模型SDK额外framing不在此字符定义内，也不声称字符数等价于token数。

| 题号 | 判定 | 耗时秒 | 最终字符 | 省略旧历史轮次 | 省略证据块 |
|---|---|---:|---:|---:|---:|
| isolated-session6-v4-s1-t08 | CORRECT | 18.008 | 19415 | 0 | 0 |
| isolated-session7-v2-s1-t03 | CORRECT | 14.006 | 20855 | 0 | 0 |
| isolated-session7-v2-s1-t06 | WRONG | 14.006 | 16145 | 0 | 0 |
| isolated-session7-v2-s1-t09 | CORRECT | 23.009 | 26121 | 0 | 0 |
| isolated-session7-v2-s1-t10 | PARTIAL | 12.005 | 16560 | 0 | 0 |
| qst_0126 | PARTIAL | 23.008 | 28811 | 1 | 0 |
| qst_0687 | CORRECT | 21.008 | 31959 | 2 | 0 |
| qst_0644 | CORRECT | 13.005 | 30587 | 1 | 0 |
| qst_0705 | PARTIAL | 30.011 | 30018 | 3 | 0 |
| qst_0354 | CORRECT | 17.006 | 31502 | 1 | 0 |

本轮10题平均请求耗时18.507秒。两轮都成功生成的同6题，平均28.010→15.673秒，单次观测下降44.05%。生成文本长度、候选和采样会变化；不能把这44%的下降全部归因于预算算法，也不是稳定性能承诺。此前4题生成前失败的耗时不作为回答速度基线。

## 剩余质量问题

- isolated-session7-v2-s1-t06（WRONG）：正确$59,415.21已进入最终引用，但模型归属到另一项目并回答$653,828；Gold是$59,515.21。
- isolated-session7-v2-s1-t10（PARTIAL）：$0.00正确，但把对应土壤液化研究项目错写为Support Services for Peer Exchanges。
- qst_0126（PARTIAL）：订阅→验证→建/关联组织→API Key→首次调用主流程正确；按完整Gold的保守口径，遗漏关联已有组织的管理员权限、适用区域限制和延迟时支持重同步兜底条件。
- qst_0705（PARTIAL）：配置CLI及部分观测方法正确；最快回滚却优先推荐pool层，未说明最高优先级节点紧急关闭及其覆盖关系。最高优先级关键段落不在最终候选，预算没有裁掉任何已检索正文。

- 本轮能证明预算未裁掉已检索正文及引用/发送列表一致；不能证明每项生成结论引用都正确。已有跨项目归属错误，完整引用精确率不作100%结论。
- 这是原失败题10题小样本，不是完整300题正确率，也未重测上次单独审查的15题。

## 证据、资源与回滚

- 冻结Gold SHA256：141c46825db096a236c34bf3458dc1e0f5608550f7e1140b57a0e54feecdca0a，未修改。复用既有源文件快照100个引用，仅补充下载缺少的2份原始TXT（4个引用）到D盘。122个原始单元格/摘录检查均匹配；Gold提供的源文件SHA均匹配。部分旧Gold条目本身没有SHA，已区分记录实际文件SHA，未伪称Gold包含该字段。
- ECS最低MemAvailable 2048.28 MiB，保留线800 MiB；测试过程云盘空闲8.802→8.797 GiB。正式API保持active/health200，解析和索引worker保持原先inactive。
- 复用现有venv、Redis/Chroma、原索引和冻结请求，未新建容器、数据库副本或索引副本。保留代码回滚、原始请求/SSE/headers/逐题结果、失败诊断日志和来源核对。仅清理本轮候选覆盖目录及传输压缩包，清理实测见cleanup-receipt.json。
- ECS证据目录：`/opt/amrag/evaluation/total-budget-20261116`；本地：`D:\RAG knowledge\var\total-budget-20261116`。
- 主要文件：replay-results.json、replay/000至009、raw-evidence-manifest.json、audited-summary.json、answer-accuracy-ten.json/.csv、original-evidence-checks.json、local-validation.json、final-runtime-receipt.json。

确认API与队列空闲后，可使用以下代码回滚命令；它只恢复两个旧文件并删除本轮新增模块，随后重启API，不改变知识库数据：

```bash
cd /opt/amrag/backend
UNIFIED_CHUNKER_RUN_DIR=/opt/amrag/evaluation/total-budget-20261116 \
PYTHONPATH=/opt/amrag/backend \
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python \
/opt/amrag/evaluation/total-budget-20261116/ecs_total_budget_control.py restore
```
