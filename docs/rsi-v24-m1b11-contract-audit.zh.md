# RSI v2.4 M1-B1.1 契约审计

Response Schema、Profile 和 Statistical Protocol 各自独立版本化。

| 层 | 审计结论 |
| --- | --- |
| 既有响应 | 保留 `rsi-v2.2`。Generalist 的变动为各角色各自响应 Schema 中的 `angle` 枚举值，已有角色的响应字段、版本和校验入口继续沿用 |
| Adaptive Diagnosis | 单独使用 `rsi-diagnosis/v2.4-adaptive-v1`，因为新增必需的结构化 `task_signals`。通道按 Profile 验证。旧响应从旧入口兼容读取，Adaptive 通道拒绝把旧版本强行解释为结构化信号 |
| 其他 Adaptive 响应 | Proposal、Curator、Meta 继续使用原响应 Schema。Task Assignment 由可信调度器生成，属于运行输入和回执 |
| Profile | 新 `M1_ADAPTIVE_V1` 绑定 6 个角色的相应 Prompt、响应 Schema、模型、解码、实现、Dispatcher、Catalog 与降级配置。历史 Profile 哈希留原值 |
| Statistical Protocol | 继续 `fixed-holm3/v1`，角色身份映射到既有三个槽位。弃权和缺项留槽，全部静态合格实例进入原回放 |

信号只能使用已验证 Diagnosis 中的公开证据引用。局部结构校验包括闭合字段、已知任务类别、支持/冲突标记和非空引用。此后由纯 Dispatcher 再核对可公开的 Observation 计数是否支持任务假设。confidence 留作元数据。

审查覆盖 `contracts.py`、`role_profiles.py`、`openai_roles.py`、`collaboration.py` 与 `launch.py`。已有两臂配对框架继续固定原有 A/B。历史回执保持原格式与字节。无响应、模型或身份迁移写入。
