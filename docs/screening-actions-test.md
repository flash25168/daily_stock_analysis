# GitHub Actions 自动选股测试 / Factor-only screening test

## 中文说明

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

当前已验证可访问的 `em_datacenter` 查询未显式请求交易日期，因此其快照日期通常仍是 `unknown`。
这属于证据不足，不能声称已经完成快照与日线的同日核验；需要下一步寻找有明确日期证据的快照源。
本更新不伪造日期，不更换现有来源，也不引入额外的实时报价请求。

全局 `ready_for_automation` 只有在两套策略均运行成功、所有已记录的样本日期核验通过且未跨已完成交易日边界时为 `true`。
它仅说明本次有界样本具备日期条件，不表示全市场穷尽核验、无新闻风险或已具备交易资格。
运行 `status=ok` 只表示计算完成，不能替代日期核验；可能同时出现 `status=ok` 和 `ready_for_automation=false`。
空名单且无可核验样本仍不标记日期通过。原定时分析任务不会被这个诊断字段自动禁用或启用。

离线测试增加午夜/周末/盘中、未来日期、快照与日线不一致、未知日期不冒充通过、日线回调证据及缓存抓取时间隔离场景。

这是第一阶段测试入口，只筛选候选，不调用 AI、不搜索新闻、不推送消息、不修改 `STOCK_LIST`。
原有 `00-daily-analysis.yml`、模型设置及 PushPlus 设置保持不变；原工作流的既有定时任务仍会按原设置运行。
本测试只手动触发，没有新增定时任务。GitHub Actions 本身仍可能占用账户运行分钟数。

### 运行

1. 将本补丁中的文件按原路径放进自己的仓库默认分支。
2. 进入 Actions，选择「自动选股测试（不调用AI）」并点击 Run workflow。
3. 首次保持 `daily_limit=30`。该值为每策略日线核验上限，支持 30 / 100 / 300。
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

工作流不传入任何模型或通知 Secret。脚本使用显式 `Config()`，不从环境构建模型配置；
`use_llm=False`、`deep_analysis=False`、`context=None`，后分析仅允许本地 `scorecard`。
不调用主程序、不请求 DSA 深度分析接口或外部后分析服务，不触发模型生成或通知。
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
第一轮测试通过后，再单独讨论每日自动运行和将候选交给现有模型与微信推送链路。

### 验证和回滚

离线检查：`python -m unittest discover -s tests -p test_screen_candidates_cli.py -v`。
语法检查：`python -m py_compile scripts/screen_candidates.py tests/test_screen_candidates_cli.py`。
测试使用固定模拟行情，覆盖真实策略加载、过滤和评分，并将模型、新闻和远程后分析入口设为一旦调用即失败。
离线测试通过不能证明 GitHub 网络环境下真实行情源可用。

回滚只需禁用或删除新增 `01-screening-test.yml` 工作流；原分析工作流和现有 Secrets 无需更改。

## English

### Version 2: date evidence

The update records dates and last-close values from the very histories returned through
the request-scoped daily-history callback into the original screening pipeline. No
additional quote request, strategy change or workflow update is required. The snapshot
date is read only from an explicit full trade-date field in the same run's snapshot
cache; its acquisition timestamp and original provider are reported separately.
Missing snapshot dates (normally the current `em_datacenter` query) remain unknown.
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

The manual-only **自动选股测试（不调用AI）** workflow reuses DSA's bundled screening engine
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
