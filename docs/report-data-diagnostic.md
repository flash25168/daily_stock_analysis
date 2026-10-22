# 数据诊断（无模型） / Read-only data diagnostics

新增手动工作流 `.github/workflows/02-data-diagnostic.yml`，不含定时、模型密钥、搜索密钥或通知密钥。只调用已有公开数据源，生成诊断JSON和Markdown。

快照输出：日历阶段、预期日期、来源总数、沪深行数、重复代码、缺少列、成交额无效数、交易行日期缺失/不匹配数；按生产 aggregate_snapshot 执行同样校验，并仅输出有限白名单原因。原始HTTP异常仅输出异常类型，不输出内容。诊断可以成功完成但发现数据不可用，工作流绿色不是数据校验通过。

财务输出：两只原自选股的各模块状态、非空字段名、错误与来源数量；不输出原始响应。使用默认配置与原默认阶段预算。此任务没有注入仓库Secrets/Variables，因此诊断的是无额外认证的默认公开数据链路；若原流程使用了自定义财务开关、预算或付费数据源，必须另外对照，不可将差异归因于接口。不会补齐数据，只为定位原因。网页新闻不在本次诊断范围。

Validation: three offline output/redaction tests and syntax compilation passed. No live endpoint or full workflow validation was run locally; full dependencies install in Actions. No model or notification initialized. Financial blocks are summarized, not a proof of independently verified financial-statement completeness.

回滚：删除此诊断工作流即可停用；新增脚本和文档不会改动原分析任务、选股任务、调度、模型或推送设置。
