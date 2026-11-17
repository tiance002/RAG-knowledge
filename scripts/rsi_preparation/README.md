# RSI Preparation：合成实验入口

这些脚本用于研究验证，不接入正式RAG或自动优化，不调用外部模型。详细结论见 `docs/rsi-preparation-report.md`，不是完整RSI实现。

|脚本|用途|依赖|
|---|---|---|
|gepa_synthetic.py|官方optimize、人工暂停、UNKNOWN及跨进程恢复|固定GEPA源码；正常import，不安装包|
|reflection_unknown_synthetic.py|官方反射异常包装器的重复尝试和守卫对照|同上|
|git_patch_synthetic.py|三代父相对Patch、临时Git Worktree恢复及清理|Git、固定DGM源码|
|aflow_synthetic.py|固定工具类AST执行、有限工作流搜索模拟|固定AFlow源码、已有numpy/pandas|
|docker_isolation_synthetic.py|Linux上修改/评测容器分离、限制、只读、Patch与Hash|Docker、Git及脚本中固定的既有镜像；无pull/build|
|test_evaluation_contract.py|现有纯评测函数六项合成契约检查|标准库；不加载RAG服务配置或数据|

官方版本：GEPA `1a435101551629de917a37110b29805268a98bfd`，DGM `a565fd2d1dca504ef5104a7cc0f3bdc4ab9b4fd2`，AFlow `3f457218fc716093fe53f6df8a5d5e6379d66346`。

执行命令见研究报告第8节。先核对源码版本和资源，不覆盖旧输出；每次新合成实验使用新的小型输出目录，恢复测试才使用`--resume-only`。`--guard-resume`启用示例结果/提议缓存与UNKNOWN门；这不是生产可用事务执行器。

Docker脚本仅挂载自己新建的 `/var/tmp/rag-rsi-preparation-*` 合成目录，输出路径限定在 `/var/log/rag-rsi-preparation/`。镜像不存在时停止，不下载；不能替换成含私密数据的镜像或扩大挂载。两个容器和临时目录由finally清理，保留小型证据。完整RAG模型运行、耗尽资源测试、真实外发与新Gold均不在脚本范围。

测试预期错误（乱序Patch退出1、无日志/已删除资源检查等）与脚本最终退出码分别记录；不能以脚本退出0宣称上游缺陷已修复或业务质量提升。
