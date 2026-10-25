# 数据诊断（无模型） / Read-only data diagnostics

新增手动工作流 `.github/workflows/02-data-diagnostic.yml`，不含定时、模型密钥、搜索密钥或通知密钥。调用已有数据客户端，生成诊断JSON和Markdown；配置 TUSHARE_TOKEN 后会额外访问官方 Tushare 接口。

快照输出：日历阶段、预期日期、来源总数、沪深行数、重复代码、缺少列、成交额无效数、交易行日期缺失/不匹配数；按生产 aggregate_snapshot 执行同样校验，并仅输出有限白名单原因。原始HTTP异常仅输出异常类型，不输出内容。诊断可以成功完成但发现数据不可用，工作流绿色不是数据校验通过。

财务输出：两只原自选股的各模块状态、非空字段名、错误与来源数量；不输出原始响应。此任务转发 TUSHARE_TOKEN（Repository secret），并转发 Repository variables 中的`FUNDAMENTAL_STAGE_TIMEOUT_SECONDS`和`FUNDAMENTAL_FETCH_TIMEOUT_SECONDS`，未配置仍为8秒。原分析和候选AI步骤同步转发这两项，便于同预算核验。没有注入模型、搜索或通知密钥；除可选 Tushare Token 外不注入其他数据源密钥，因此其余认证/财务开关仍与生产可能不同，不可将差异全部归因于接口。不会补齐数据，只为定位原因。网页新闻不在本次诊断范围。

Validation: three offline output/redaction tests and syntax compilation passed. No live endpoint or full workflow validation was run locally; full dependencies install in Actions. No model or notification initialized. Financial blocks are summarized, not a proof of independently verified financial-statement completeness.

回滚：删除此诊断工作流即可停用；新增脚本和文档不会改动原分析任务、选股任务、调度、模型或推送设置。


增强诊断：列出最多30条成交额异常记录的白名单公开行情字段；缺少明确状态仍标记停牌未验证，不能推断为零成交。财务错误按阶段预算耗尽、超时、连接、权限、限流、解析、不支持、空结果等分类，未知保留unclassified，不输出原始错误。来源链仅允许简短来源名、状态、耗时和错误类别。新增5项离线测试通过；未在线验证。本次无需修改工作流。

## Tushare 接口权限检查 / Tushare access check

在 Repository secrets 添加 `TUSHARE_TOKEN` 后，手动运行 `02-data-diagnostic.yml`。诊断使用现有 `TushareFetcher` 和官方 HTTPS 地址，只输出接口状态、记录数和日期。先核验 `trade_cal`；成功取得最近开市日期后，再检查 `daily`、`index_daily`、`daily_basic`、`income`、`cashflow`、`fina_indicator` 和 `moneyflow`。日历失败时跳过依赖日期的检查，不猜测交易日期。请求间隔15秒，结果写入 `tushare_access.json` 和 `tushare_access.md`，随原诊断产物保存。

`ok` 仅表示该次接口返回非空数据，不保证完整市场覆盖或财报字段齐全；`permission_denied` 表示接口权限不足，`rate_limited` 表示频次限制，`invalid_token` 表示令牌相关错误，`network_failure` 表示连接类错误。工作流绿色表示诊断执行完成，仍需阅读每个接口状态。此流程不会自动购买权限、改变数据源优先级或调用AI。Token 已被原每日分析工作流引用，但每种报告是否使用相应接口仍需独立验证。

This manual workflow reuses the production client against official Tushare HTTPS endpoints. It injects only the optional Tushare credential, never AI or notification credentials. Access status does not prove data completeness; a failed calendar prevents date-dependent probes. Revert the workflow change to remove this check.
