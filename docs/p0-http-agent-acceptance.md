# HTTP/SSE 与 Agent 重试验收记录（2026-10-28）

## 输出截断复核

既有 Trace `b77c3d8744d2801961360dca8a9a1aaa`：请求 max_tokens=4096，prompt_tokens=12858、completion_tokens=4096、finish_reason=length，最终 content 为空。模型为 XingChenAGI/Xing4.0-29B。证据直接支持输出预算耗尽；不证明输入超过模型上下文上限。提供商没有返回 completion_tokens_details，因此不能证明输出预算具体如何分配，也无法确认是否被内部推理消耗。

该实验 runner 的系统 Prompt 要求基于资料、资料不足说明、中文回答，没有字数要求；正式普通问答 Prompt 要求准确、完整、有条理、引用和中文，也没有字数限制。Prompt 可以添加简洁或字数要求，但不是 Token 硬约束；本次没有改变正式 Prompt 或模型参数。

## 正式 HTTP/SSE：PASS（单题全文检索范围）

两次真实请求运行于独立数据库及代码快照，首次本地 Trace、第二次启用 Cloud；两次 runner 退出码均为 0。第二次 HTTP 200，80 个 answer_chunk、1 个 done，无 error；真实生成请求 1 次，Embedding 和 Rerank 均 0 次，全文检索，未开启改写。流 finish_reason=stop、truncated=false。回答的收入 190395.32 万元、支出 189274.27 万元均与冻结 Gold 一致，所引用的第 8 条来源为目标 PDF 第 10 页，包含两个数值。18 个返回来源的文件、页码和片段能从隔离 DB 回读；这是定位可回读性，不代表所有 18 条均支持每个回答断言。

Cloud Trace `89c59ab47d8370662cbe7a810534705e` 实际回读包含 qa.request、retrieval.search、qa.citations、llm.generate_stream、llm.stream_result。证据 `var/p0-observability/http-cloud-20261028/`。该验收是一题一次全文检索，不代表混合检索、所有格式或全部答案质量已验收。

## 真实模型 Agent：重试路径已观察；完整验收待补

两个冻结 HotpotQA 题分别得到 0.85、0.9 的相关性评分，均未触发低分重试；各自工作流退出 0。第三次使用显式标记的空知识库负例，第一次实际查询后自然得分 0.0，随后出现 retry_count=1 的实际新查询，且查询哈希与第一次不同；整个负例记录 7 次查询和 retry_count 0/1/2/3，runner 退出 0。没有修改质量阈值或伪造评分。

此负例的所有检索都产生 retrieval.failed，原因包括原实现将双路无候选视作异常；4 次 Embedding HTTP 上限在第一次评分后第二个查询时耗尽，后续轮次受此测试约束影响。所以它验证了“低分后模型确实再次调用检索”，不证明正常非空知识库中的低质量证据可以被成功补救，也不证明真实业务配置无限制重试效果。

Cloud 负例记录实际首次查询 → score=0.0 → retry_count=1 新查询；Cloud 可回读 agent.actual_query、agent.quality、retrieval.search 和 llm.response。它在测试设定的 8 次 chat HTTP 上限停止，runner 退出码 1，不能算完成的工作流。该次本地 13 条 actual_query 事件、Cloud 当前回读到 11 条，差异尚未消除，不能声称完整导出。Trace `93746d54dbfed14ebda0b7c9316ba0e6`。直接访问旧版 Trace API 返回 HTTP 410，改用 observations 接口回读；保留失败记录。

四次 Agent 的隔离 DB 使用后均清理模型密钥并 VACUUM，核验非空密钥 0。Cloud 凭据通过 stdin 提供，没有写入隔离文件。证据 `var/reports/p0-agent-accept-20261028/`；其中原始服务器日志可能包含服务信息，服务器保留原文件，本地保留脱敏日志摘要。

## 命令与测试口径

`python -m py_compile scripts/run_p0_agent_acceptance.py` 与 `python -m py_compile scripts/run_p0_http_acceptance.py` 均退出 0；ECS 隔离 venv 的 Agent runner 使用 case 参数 0、4、empty-kb 三次退出 0，empty-kb --cloud 退出 1（受调用次数上限）；HTTP runner 普通及 --cloud 两次退出 0。两类 runner 均限制供应商调用，使用隔离数据和当前代码快照。原始事件、运行 manifest、结果及本地证据目录可复核。

未完成项：Agent 通过正式 HTTP/SSE 路由的带真实模型重试、非空知识库中低质量结果的成功补救、Cloud 本地与云端全部事件数量一致。所有这些标记 NOT RUN 或待核查，不能以一个负例替代验收。未重跑 250 题，未切换正式服务。消费金额因价格未确认记 UNKNOWN。

后续每次测试的结果审查要求已写入项目根 AGENTS.md：核对原始日志、分母、版本、失败、跳过、空结果、截断及证据，并明确未知。此流程降低误判风险，不承诺绝对不会误判。
