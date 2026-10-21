# GitHub Actions 自动选股测试 / Factor-only screening test

## 中文说明

当前版本 5 已按用户选择接入收费 AI，并保留旧任务；详见 [候选 AI 分析配置](screening-ai-actions.md)。
本页后续 v1–v4 内容记录此前无 AI 的筛选与推送行为，当前收费、额度及手动开关以新文档为准。

### 版本 4：工作日自动选股及 PushPlus（历史行为）

沿用 `01-screening-test.yml` 文件，显示名称更新为「每日自动选股（不调用AI）」。
默认分支上的工作流设为 UTC 周一到周五 09:17，即北京时间 17:17 触发；GitHub 调度可能延迟，
不是精确定时或送达保证。A 股节假日由交易日历跳过；日历不可靠时失败关闭，不按自然日放行。
定时模式仅在当天交易日收盘后处理当日已完成行情；延迟到次日凌晨不再推送前一日名单。

每策略默认核验前 100 只初筛候选，仍不等于全市场逐股日线扫描，不修改任何策略阈值。
原 `00-daily-analysis.yml` 继续按原设置运行（包括既有 AI 分析与推送），本更新不会禁用它。
本选股工作流不传模型 Secret，不请求主程序或深度分析服务，不产生模型 API 调用。

推送在单独一步执行，只复用已有 `PUSHPLUS_TOKEN` 及可选 `PUSHPLUS_TOPIC`，并沿用原工作流的
`environment: STOCK_LIST` 以保持环境级 Secret 的作用域。不新增必填 Secret 或 `.env` 配置项。
手动运行默认上限 100，勾选 `send_notification` 才发送；未勾选则只保存结果。
定时运行会自动尝试发送，但必须计算成功且全部样本日期核验通过，并在发送前再次核对时点。
未通过时保留 artifact 和失败原因，不将未知或过期名单发到微信。

发送脚本直接复用 DSA 现有 `PushplusSender`，公共发送地址由 HTTP 改为 HTTPS，配置和载荷格式不变。
发送紧凑候选摘要，不发送调试日志、配置、密钥或原始缓存。少于 10 只不补足；有效零候选会说明是核验范围内无候选。
若初筛没有任何可核验日线，不能自动宣称日期核验通过，因此会保留结果而不发送。

PushPlus 的 `code=200` 只表示异步受理，报告记为 `accepted`，不等同于微信已送达。
见 [官方 API 说明](https://www.pushplus.plus/doc/guide/api.html) 及
[HTTPS 支持说明](https://www.pushplus.plus/doc/help/https.html)。
同一个结果文件已有受理/提交中/响应未知记录时不自动再次提交；不会因超时自动重试。
GitHub 的 Re-run 或新的手动运行会重新生成结果，因此仍可能重复发送，同一天请不要反复点运行。
失败时先检查微信与 Actions 的通知状态，不要立即重跑；额度和账号限制依 PushPlus 实际服务为准。

安装普通文件后，再在线更新工作流内容；不必新建第二个工作流，避免重复定时。
首次手动选 100 并勾选推送，确认手机收到「A股选股候选（非AI）」再观察下一交易日的自动任务。
本地仅进行模拟 HTTP 验证，不读取用户密钥，也没有实际向微信发送测试消息。

回滚自动运行：从本工作流移除 `schedule` 或禁用本工作流；原每日分析和原 PushPlus 配置无需改动。
只想恢复手动测试，可恢复 v3 工作流和脚本；HTTPS sender 改动可单独保留。

新增离线验证命令：`python -m unittest discover -s tests -p test_screening_pushplus.py -v`。
覆盖假日/日历异常在抓行情前跳过、日期或旧报告阻止推送、Token 缺失、HTTPS 原生 sender 请求、
异步受理状态、超时后不自动重试及敏感信息不入产物。

### 版本 3：识别东方财富来源日期

直接在线核对原 `em_datacenter` 请求后，确认接口会在每行附带 `MAX_TRADE_DATE`。
此前版本只检查通用日期字段，遗漏了这个已有字段，因而将快照日期标记为未知。
本次只修正读字段逻辑，**不修改请求字段、筛选参数、行情来源优先级或策略**。

来源为 `em_datacenter` 时，读取同次运行快照原行的 `MAX_TRADE_DATE`，并记录
`snapshot_trade_date_field: "MAX_TRADE_DATE"`。不把同响应可能出现的其他用途 `TRADE_DATE`
当作当前快照声明日期，也不将 `MAX_TRADE_DATE` 规则套用到其他数据源。
该字段缺失或格式非法时仍保持 `unknown`；偏离日线或预期交易日时为 `fail`。
日历、日线、字段缺失及跨交易日边界检查继续保留。

该日期是**数据源声明的快照日期**，不是独立确认的每只股票最后成交日。
`pass` 只说明来源声明日期、实际使用的日线日期和交易日历一致，不证明每个价格新鲜、
停牌状态已排除、全部 A 股已覆盖或具备投资价值。
在线字段观测与离线回归测试均已完成，仍需在用户的 GitHub Actions 中运行确认实际缓存的字段。

### 版本 2：行情日期核验

本次更新仅替换脚本、离线测试和说明，不修改工作流、策略阈值、模型或通知配置。
脚本通过请求级 `daily_history_fetcher` 回调记录**实际送入原筛选管线**的日线日期和末收盘价，
没有另抓一套行情来代替原证据。JSON 的 `date_audit` 覆盖本次实际成功获取日线的样本，以及抓取失败的未知记录。
每次脚本运行使用独立缓存子目录，两个策略之间复用同一次快照；写缓存失败时不会误读上轮文件作为本轮证据。
Markdown 和 CSV 展示最终候选的日线交易日、快照交易日和核验状态。

- `daily_trade_date`：该次实际日线中有有效收盘价的最新交易日期；缺失保持空值。
- `daily_received_at`：日线返回到脚本的时间，不能当作行情发生时间。
- `snapshot_trade_date`：仅消费当前快照缓存中明确的完整交易日期字段（如 `TRADE_DATE`），不从价格、抓取时间或日线日期推测。
- `snapshot_fetched_at`：同一快照写入缓存的原始抓取时间；复用缓存时保留原值。
- `snapshot_original_source`：即使本策略通过 `last_good_cache` 读取，也记录该快照原始数据源。
- `expected_daily_date`：复用 DSA 交易日历计算的最近已完成交易日，按北京时间处理凌晨、休市和周末。

每股 `date_audit_status`：

- `pass`：日历可靠、当前不在盘中/午休/收盘竞价、快照和日线均有明确日期，且均等于预期已完成交易日。
- `unknown`：任一日期缺失、日历不可用或本次不是已收盘时段测试。保留候选供诊断，**不视为核验通过**。
- `fail`：日期非法、日线/快照相互不一致或偏离预期已完成交易日。Top 5 中相应股票移除，不自动补位。

版本 2 未识别该查询自动附带的 `MAX_TRADE_DATE`；版本 3 已按上节修正。
任何来源仍不能提供有效日期时继续保持未知，不伪造日期，不更换现有来源，不引入额外实时报价请求。

全局 `ready_for_automation` 只有在两套策略均运行成功、所有已记录的样本日期核验通过且未跨已完成交易日边界时为 `true`。
它仅说明本次有界样本具备日期条件，不表示全市场穷尽核验、无新闻风险或已具备交易资格。
运行 `status=ok` 只表示计算完成，不能替代日期核验；可能同时出现 `status=ok` 和 `ready_for_automation=false`。
空名单且无可核验样本仍不标记日期通过。原定时分析任务不会被这个诊断字段自动禁用或启用。

离线测试增加午夜/周末/盘中、未来日期、快照与日线不一致、未知日期不冒充通过、日线回调证据及缓存抓取时间隔离场景。

以下说明包含 v1–v3 的测试设计；当前行为以版本 4 为准。未勾选手动推送时只筛选候选，不调用 AI、不搜索新闻、不修改 `STOCK_LIST`。
原有 `00-daily-analysis.yml`、模型设置及 PushPlus 设置保持不变；原工作流的既有定时任务仍会按原设置运行。
v1–v3 只手动触发；v4 增加上述定时任务。GitHub Actions 本身仍可能占用账户运行分钟数。

### 运行

1. 将本补丁中的文件按原路径放进自己的仓库默认分支。
2. 进入 Actions，选择「每日自动选股（不调用AI）」并点击 Run workflow。
3. 当前默认 `daily_limit=100`。该值为每策略日线核验上限，支持 30 / 100 / 300；勾选推送才发送手动结果。
4. 查看运行 Summary，并下载 `screening-test-运行编号` artifact。

产物：`screening_report.md` 为阅读版，`screening_result.json` 为结构化诊断，
`candidates.csv` 为策略内名单，`candidate_codes.txt` 为去重代码列表。
CSV 中代码应按文本导入，以保留前导零。两个策略各最多 5 只，合并去重后最多 10 只；
先按各自策略名次交替合并，不将不同策略的分数直接比较。重合股票保留两种信号。

### 策略与覆盖范围

直接复用 `src/services/screening/pipeline.py` 和该目录的内置策略，不新增另一套算法。
选股引擎策略与根目录 `strategies/*.yaml` 的 AI 自然语言指导是两种用途，阈值并不完全相同。
本测试使用源码中的引擎版本：放量突破 1.2、缩量回踩 1.1，未自行修改原阈值。

- 放量突破：快照要求非 ST、成交额至少 1 亿、换手至少 3%、量比至少 2、涨幅 2%–9.9%；
  日线还需满足 MA20、信号分、MACD、突破幅度、整理天数等原策略硬条件。
- 缩量回踩：快照要求非 ST、成交额至少 8000 万、换手至少 1%；
  日线还需满足多头均线、MA20、信号分、20 日量比、回踩距离、波动、回撤与 ATR 条件。

引擎先获取全市场快照，对所有返回股票执行快照硬筛和预评分，再对每策略排名前 N 只补日线。
因此「全市场快照初筛」不等于「全部股票均完成日线策略核验」；N 以外可能有漏选。
数据源返回成功也不保证覆盖全部 A 股或交易日期正确，应核对快照条数、来源及时间。
默认 N=30 用于验证链路；不能把未选中理解为全市场无机会。
每策略输出 Top 5 后再移除陈旧、样本不足或严重质量标记的股票，不从后排自动补齐。

### 模型和费用边界

工作流不传入任何模型 Secret；v4 仅向单独的推送步骤传现有 PushPlus 配置。选股脚本使用显式 `Config()`，不从环境构建模型配置；
`use_llm=False`、`deep_analysis=False`、`context=None`，后分析仅允许本地 `scorecard`。
不调用主程序、不请求 DSA 深度分析接口或外部后分析服务，不触发模型生成；通知由 v4 的独立步骤负责。
不新增环境变量配置项；`--daily-limit` 和 `--output-dir` 是本脚本 CLI 参数。
行情仍走引擎原有公开数据源与备用链路，受接口可用性和限制影响。

### 验收及故障

成功但零候选是有效结果，不放宽条件或填补示例股票。
全市场快照失败、全部已尝试日线失败或单策略异常会产生 `incomplete`，并返回非零退出码，
已完成的另一策略结果仍保留；工作流尽量上传现有产物。
产物不包含原始完整日志、缓存和配置，只保留允许的字段；异常只输出类型，来源链接与常见密钥格式会隐藏。
依赖安装或模块导入失败发生在产物生成之前时，需查看对应 Actions 步骤错误。
任务被强制取消或超时时不保证能上传完整结果；先将 N 降至 30 复测。

报告是未经新闻、公告、财务、筹码核验的技术候选；盘中或休市运行只作测试。
v4 仅接入候选名单推送，未把名单交给模型分析；模型接入需另行决定。

### 验证和回滚

离线检查：`python -m unittest discover -s tests -p test_screen_candidates_cli.py -v`。
语法检查：`python -m py_compile scripts/screen_candidates.py tests/test_screen_candidates_cli.py`。
测试使用固定模拟行情，覆盖真实策略加载、过滤和评分，并将模型、新闻和远程后分析入口设为一旦调用即失败。
离线测试通过不能证明 GitHub 网络环境下真实行情源可用。

回滚只需禁用或删除新增 `01-screening-test.yml` 工作流；原分析工作流和现有 Secrets 无需更改。

## English

Version 5 adds explicitly authorized paid analysis while preserving the old daily task.
See [screening-ai-actions.md](screening-ai-actions.md) for the current behavior; the
v1–v4 descriptions below document the earlier factor-only workflow.

### Version 4: scheduled factor screening and PushPlus (historical behavior)

The existing workflow file is renamed in the UI to 每日自动选股（不调用AI） and triggers
at 09:17 UTC / 17:17 Beijing time on weekdays. GitHub scheduling can be delayed. The
exchange calendar skips CN holidays and fails closed when unavailable; scheduled work
requires today's completed session and will not send yesterday's report after midnight.
The daily-history cap is 100 per strategy, not an exhaustive full-market history scan.
The original daily AI workflow and its schedule remain unchanged.

Notification is a separate step with only the existing PushPlus token/topic configuration,
using the same environment scope as the original workflow. No model secret is exposed.
Manual runs default to no notification unless the checkbox is selected; scheduled runs
attempt it only after successful date validation, rechecked immediately before sending.
The sender reuses DSA's native PushplusSender, with its transport switched to HTTPS.
API acceptance is recorded as accepted, never as confirmed WeChat delivery. No actual
message was sent during local mocked validation. One existing result file is not retried
after acceptance or an uncertain attempt; new GitHub runs can still send duplicates.
Disable this workflow or remove its schedule to roll back automatic selection without
changing the original analysis workflow. The earlier manual-only design is described below.

### Version 3: Eastmoney source-declared date

A direct observation of the original Eastmoney xuangu request confirmed a per-row
`MAX_TRADE_DATE` field. Version 2 missed this existing field; version 3 consumes it
only when the original cache provider is `em_datacenter`, and records its field name.
It does not change the request, filters, provider priority or strategy. Other `TRADE_DATE`
fields are not substituted for the Eastmoney snapshot declaration; missing/invalid fields
remain unknown. All calendar/daily-date/boundary checks continue to apply.
This is the provider-declared snapshot date, not independent proof of each security's last
trade or quote accuracy. Live field observation and offline tests do not replace a run
in the user's Actions environment.

### Version 2: date evidence

The update records dates and last-close values from the very histories returned through
the request-scoped daily-history callback into the original screening pipeline. No
additional quote request, strategy change or workflow update is required. The snapshot
date is read only from an explicit full trade-date field in the same run's snapshot
cache; its acquisition timestamp and original provider are reported separately.
Missing snapshot dates remain unknown (version 3 now recognizes Eastmoney's existing field).
Never infer a trade date from cache age, the report time, matching prices or daily bars.

Date checks return `pass`, `unknown` or `fail`. Pass requires reliable exchange-calendar
context, a closed-session test and both explicit dates matching the latest completed
session. Unknown records remain diagnostic candidates, not verified picks; failed Top 5
records are removed without backfilling. Full per-sample evidence and rejection reasons
are stored in JSON; candidate dates/status are also shown in Markdown and CSV.
`status=ok` means computation completed; `ready_for_automation` is a separate date-only
condition over the bounded, recorded sample and cannot certify exhaustive coverage or
investment quality. A run crossing the completed-session boundary cannot pass.
Midnight/weekend/intraday, future/mismatched/missing dates, cache timestamps and callback
evidence are tested offline. Existing analysis schedules and secrets are unaffected.

The original manual-only **自动选股测试（不调用AI）** workflow (v1–v3) reuses DSA's bundled screening engine
and its existing `volume_breakout` / `shrink_pullback` strategies. It does not execute the
root-directory natural-language AI strategy instructions, whose thresholds differ.
No model/notification secrets are passed. Explicit configuration disables LLM ranking,
remote DSA/external post-analysis and candidate research; only the local scorecard runs.
The existing daily workflow, watchlist and PushPlus configuration remain unchanged,
including any existing paid analysis schedule. Actions runtime minutes may still apply.

Run from Actions with the default daily-history cap of 30 per strategy. The engine filters
the returned full-market snapshot, then verifies only each strategy's bounded top pool
with daily histories. This is not an exhaustive daily-history scan or guaranteed complete
market coverage. Each strategy returns at most five picks; interleaving and deduplication
produce at most ten codes without comparing scores across strategies. No filler picks.
Stale or materially incomplete Top 5 entries are removed without backfilling.

Download the Markdown report, JSON diagnostics, CSV and code list from the run artifact.
Keep codes as text in spreadsheet software. Snapshot/daily source outages produce an
incomplete result and nonzero exit status; a genuine zero-pick result is valid. Partial
completed results are retained. Raw configuration, caches and logs are not uploaded.
Public-source availability and trading dates require verification in the user's Actions
environment. Intraday/holiday runs are tests, not verified trading signals.

Run the unittest and py_compile commands above for offline validation. Fixture tests cover
the real strategy/filter/scoring pipeline and forbid model/research/remote analysis calls.
They do not establish live source availability. Disable the new workflow to roll back.
