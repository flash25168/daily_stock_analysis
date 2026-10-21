# 候选 AI 分析 / Screened-candidate AI analysis

## 当前行为（版本 5）

用户明确选择两套都保留。`00-daily-analysis.yml` 完全不改：原固定自选股与大盘分析仍独立运行，
原模型及 PushPlus 配置均保留。新增链路每天最多 3 只的限制**不约束原任务或账户总费用**。
股票重合时可能重复分析收费；数据时点、上下文或候选池不同也可能导致结论不同。

新增流程仍使用 `01-screening-test.yml`，显示为「每日自动选股与AI分析（最多3只）」。
原北京时间工作日 17:17 的筛选与日期核验保持不变。定时运行启用 AI；手动运行新增 `run_ai`，默认不勾选。
`send_notification` 独立控制手动推送：只勾 AI 不勾推送则生成报告文件，不发送消息。
原交易日历、节假日跳过、日期不一致阻止推送、每策略日线核验上限 100 均保留。

## 每日额度

- 只从本轮通过日期核验的去重候选中，按原有两策略交替名次取最多 3 只；不跨策略比较分数，不回退到旧自选股。
- 限制的是每天一批、最多 3 只股票，**不是最多 3 次 HTTP 请求**。原模型的参数恢复、重试或备用模型可能增加计费请求和 token。
- 按北京时间自然日保留一个批次；凌晨手动测试会占用当天批次，当天 17:17 不再启动新 AI 分析。
- 计划阶段用 GitHub Artifacts API 检查 `screening-ai-budget-YYYY-MM-DD` 是否存在；网络、权限或响应异常时停止，不假设额度空闲。
- 同一 concurrency group 串行执行运行；模型初始化前先持久化 `ai_plan.json` 为独立额度 artifact（30 天保留）。上传失败不调用模型。
- 调用前核对 artifact ID、名称、所属运行、未过期及同名记录唯一性；已有额度记录时跳过本批 AI，继续按设置推送因子候选摘要。
- 崩溃、部分失败或推送失败不自动释放额度或重复调用；少于 3 只也占用唯一一批。占用额度不代表失败一定已经收费。
- 不要删除当天额度 artifact。手动删除、改名或修改工作流会破坏限制；这不是模型账户层面的货币预算。

工作流增加 `actions: read`，用 GitHub 自动提供的 `github.token` 查询元数据，不需要用户申请新 Token。
额度 artifact 仅含股票代码、日期、上限和运行 ID，不包含模型密钥或账户资料。
接口参考：[GitHub Artifacts API](https://docs.github.com/en/rest/actions/artifacts)、
[upload-artifact](https://github.com/actions/upload-artifact)。

## 复用原模型

模型步骤复用原 `00-daily-analysis.yml` 的模型、渠道、基础地址及可选搜索/行情配置映射，
不更换已配置模型，不要求重新填写密钥。映射只进入模型步骤，不进入因子筛选或推送步骤。
高级 `LITELLM_CONFIG_YAML` 仅在 runner 的 `work/` 写入临时配置，不打印或上传。

入口使用原 `StockAnalysisPipeline.run(stock_codes=...)`，显式传入最多 3 只股票。
本新增链路关闭 Agent、多轮工具编排、大盘生成、每日大盘上下文生成、自动回测、上下文快照及原管线通知，
再单独用已有 PushPlus 发送完成的 DSA 报告，避免额外大盘分析和重复推送。
原任务的这些设置没有修改。新分析会重新抓单股数据，因此内容可与筛选时点不同；筛选日期一致不代表
已独立认证分析阶段所有外部材料。新闻是否可用仍取决于已有服务配置。

## 结果与故障

只有本批全部股票分析成功才自动发送完整报告；部分报告保留为文件，运行标记失败，不自动再次收费重跑。
PushPlus 的 `accepted` 仅表示受理，仍需确认手机实际收到。原任务也会继续发自己的报告。
因子筛选成功不等于 AI 成功，Summary 分别展示 AI 计划、完成状态和推送状态。

新增产物为 `ai_plan.json`、`ai_result.json`、`ai_report.md`，不上传原始模型日志、提示词或临时配置。
离线测试使用模拟模型/HTTP，没有真实费用；原管线的真实依赖、渠道认证、GitHub 额度 artifact 读写与微信送达
仍需首次手动验证，不能用离线测试宣称端到端分析已跑通。

首次手动选择上限 100，同时勾 AI 与推送，仅运行一次。失败后先查看结果，勿直接重跑或删除额度记录。
停用新增收费分析时可恢复 v4 工作流，继续无 AI 候选推送；原固定自选股任务不受影响。

## 本地验证

```text
python -m unittest discover -s tests -p test_screen_candidates_cli.py -v
python -m unittest discover -s tests -p test_screening_pushplus.py -v
python -m unittest discover -s tests -p test_screening_ai.py -v
python -m py_compile scripts/screening_ai.py scripts/screen_candidates.py
```

AI 测试覆盖最多 3 只、空候选、旧额度、元数据查询失败、日期失败、未核验额度前不初始化模型、非独占额度、
异常后不重复付费、部分结果不冒充成功、原管线参数与配置继承、报告发送保密和工作流顺序。
测试通过不等于实际费用被金额封顶；账户费用仍取决于渠道价格、计费 token、重试和原任务。

## English

Version 5 explicitly enables paid candidate analysis while leaving the original daily
watchlist/market-review workflow unchanged. The new 3-stock limit applies only to the
new workflow, not the account or original task. Overlapping stocks may be billed twice.

At 17:17 Beijing time on eligible weekdays, screening and date validation run first,
then at most three candidates are selected in the existing interleaved rank order.
Manual AI defaults off; notification is a separate checkbox. No stale/default watchlist
fallback is allowed. The model and provider mappings are inherited from the original
workflow; no model name is replaced and no new user secret is required.

One batch is conservatively reserved per Beijing calendar day through a persistent
`screening-ai-budget-YYYY-MM-DD` artifact before model initialization. API read errors,
failed uploads, mismatched/nonexclusive reservations, expired plans and prior records
block model work. An aborted/partial run consumes the batch without automatic release.
Do not delete reservation artifacts. This operational guard is not a provider-side
monetary limit, and native request retries may exceed three HTTP calls. A midnight
manual test consumes that day's batch, so the later scheduled run may only send the
factor shortlist. The original workflow is independent and still incurs its own costs.

Analysis calls the original StockAnalysisPipeline with explicit codes, one worker,
Agent/market-context generation/backtest/snapshots and built-in notifications disabled.
Only a fully completed batch is automatically pushed through the existing PushPlus
sender. Partial reports remain artifacts and are not automatically rerun. Provider
acceptance is not delivery confirmation. Raw prompts, logs and temporary model YAML
are excluded from uploaded artifacts.

All local model and notification tests are mocked. Live imports/configuration,
GitHub reservation read/write and real model/report delivery require the user's first
manual run. Restore the v4 workflow to stop the new paid path without altering the
original task. Ordinary files can be installed before replacing the workflow.
