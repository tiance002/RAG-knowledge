# DOCX 统一分块原库替换与10题复测结果

本轮仅替换原知识库的5个DOCX，保留原始文件、知识库ID和文件ID。没有修改其它文件索引、正式代码或全局预算；没有重启API。

## 执行与索引验收

- 本地5个真实DOCX解析/父子图检查通过；相关测试22 passed，exit 0，1条Pydantic弃用警告。ECS解析预检exit 0。
- 目标SQL由74块改为89块：12父块、76子块、1独立块；77个可检索块的向量与BM25记录均核对通过，无空白正文或跨文件/悬空父链接。
- 原16,680字符块已替换；父块最大3,425字符，独立可检索块最大4,956字符。legacy配置为目标尺寸，非硬上限。
- 其它118,209个块，以及其它文件和构建的完整SQL记录有序哈希均未变；知识库ID列表不变。
- 仅目标SQL/BM25/构建和旧向量备份，备份清单共401,168字节；D盘副本哈希已核对。SQL恢复函数在内存目标数据上执行两次通过，未在正式库执行回滚。

## 原10题重测

10题均保留原请求参数和原历史，逐份请求JSON比对一致；无重试、跳过或替换。HTTP200且curl exit0均为10，但SSE中4题报告生成预算错误，不能算成功。6题done为COMPLETED且非空，未发现本轮输出截断。

| 题号 | 结果/当前题判定 | 请求耗时秒 | 最终超限字符数 |
|---|---|---:|---:|
| isolated-session6-v4-s1-t08 | CORRECT | 24.010 | 未记录，已通过护栏 |
| isolated-session7-v2-s1-t03 | CORRECT | 29.010 | 未记录，已通过护栏 |
| isolated-session7-v2-s1-t06 | WRONG | 33.011 | 未记录，已通过护栏 |
| isolated-session7-v2-s1-t09 | CORRECT | 41.014 | 未记录，已通过护栏 |
| isolated-session7-v2-s1-t10 | PARTIAL | 13.006 | 未记录，已通过护栏 |
| qst_0126 | FINAL_REQUEST_OVER_CHAR_BUDGET | 6.003 | 34286 |
| qst_0687 | FINAL_REQUEST_OVER_CHAR_BUDGET | 5.003 | 33330 |
| qst_0644 | CORRECT | 28.010 | 未记录，已通过护栏 |
| qst_0705 | FINAL_REQUEST_OVER_CHAR_BUDGET | 7.004 | 34273 |
| qst_0354 | FINAL_REQUEST_OVER_CHAR_BUDGET | 5.003 | 33547 |

6道非空回答平均耗时28.010秒。此前10题均在生成前失败，不能据此声称成功回答耗时下降。

## 回答与剩余问题

- 六道已生成回答中，按当前题必要事实及直接关联项目归属判定：4正确、1部分正确、1错误，严格4/6=66.67%；全部10题严格成功4/10=40%。此为原失败题子集，不能外推完整300题或更新未重测的前15题正确率。
- isolated-session7-v2-s1-t06：正确的$59,415.21已经进入本次引用候选；回答混用其它项目金额，最终给$653,828，还把59,415.21+100算成59,415.31。原文支持$59,515.21。
- isolated-session7-v2-s1-t10：$0.00正确，但项目标题及归属混为Support Services for Peer Exchanges，标记PARTIAL，不算严格正确。
- qst_0126、qst_0687、qst_0705、qst_0354仍超32000字符。本轮证据正文21,432至23,213字符；加历史、标签、提示词和序列化开销后33,330至34,286字符。全局动态预算问题未改。
- qst_0644本轮生成且核心答案正确；其候选与标签数量发生变化。仅此一次通过不能证明全局输入预算已修复。
- 部分回答继续输出无关历史纠错说明。三道done包含source_mapping_status=UNKNOWN、SEED_ONLY，其余三道VERIFIED、DELIVERED；本轮未评完整引用精确率，不将非空、接口成功或自动校验未运行当作全部正确。

## 资源与收尾

- 最低MemAvailable 1428.51 MiB，高于800 MiB保留线；未触发资源护栏。
- 云盘空闲：备份前8.793 GiB，最终核对后8.778 GiB。当前保留必要回滚、逐题结果、原始SSE/请求和日志；没有新建语料副本、数据库副本、索引库或容器，没有删除原始资料。
- 独立控制服务Result=success、ExecMainStatus=0。API保持active且/health HTTP200；解析/索引worker恢复inactive，全局recovery hold保留。
- 复用既有Python环境、Chroma/Redis、源文件及冻结Gold。新建仅为小型操作脚本、目标回滚数据和本轮证据；源文件保持原位。

## 交付位置

- ECS：`/opt/amrag/evaluation/unified-docx-20261116/`
- D盘：`D:\RAG knowledge\var\unified-docx-20261116\`
- 回滚说明：`D:\RAG knowledge\docs\docx-reparse-rollback-20261116.md`
- 逐题结果与请求、响应：`replay-results.json`、`replay/000`至`009`；审计文件`audited-summary.json`、`answer-audit-six.json`、`raw-evidence-manifest.json`。
- 来源核对：5道DOCX题23个原始单元格核对；qst0644补充下载一份原始TXT至D盘，SHA与冻结证据一致，两个原文摘录均匹配。冻结Gold与旧轮结果没有修改。
