# 报告数据缺失：2026-10-23续接修正

## 2026-10-24：接口分段恢复

无AI诊断#3确认45秒总预算、12秒单次预算已生效，但财务聚合和板块仍超时，资金流有连接/解析错误。此前一个外层超时会丢弃聚合函数已完成的数据，而且首个板块来源可能耗尽全部等待预算。

本轮复用管理器现有受限线程、按来源/操作隔离超时请求的保护。财务六组查询按剩余预算分配等待时间（主要指标权重4，机构汇总权重2，其余各1），组内候选也保留备用时间；完成的结果同步返回并保留，后面的超时不会抹掉前面的财报。新增AkShare东财主要指标候选，校验股票代码、报告期和已提供的公告日期，排除未来/未知日期、其他股票；使用元和百分比原单位，不把同比百分比当收入金额。新浪逐年财务指标只请求近两年，不再回溯1900年至今，也不调用无股票参数的默认股票接口。

板块查询在各来源间分配等待时间并单独隔离仍在运行的超时来源，使后续来源能执行；不使用无日期证明的旧缓存。资金流接口报错且没有数据时返回failed，普通空结果仍按原能力边界处理；仅拿到板块资金流而没有个股资金流时为partial。未用其他金额或净占比代替主力净流入金额。

诊断#4确认两只样本成长和财报字段恢复，但板块的Efinance/Akshare各只分到约2秒（其余四个来源实际没有板块实现）。后续排除继承的空能力以保留有效预算；同源新浪行业接口改为优先HTTPS、GBK解码及5秒传输超时，保留原速率限制，东财继续作后备。校验响应结构、非空名称和有限涨跌幅，空榜单继续向后查找。新浪原SDK的同一行业接口使用HTTP，不添加新的数据供应商或账号；板块口径为新浪行业，报告不宣称其分类等同于东财。

诊断#5确认两只板块均ok、五粮液机构变化可用，但茅台首选财报在1.6秒候选上限超时（五粮液同接口约0.5秒成功）。组内改为首选最多使用该组70%剩余预算，保留30%给其他候选；总预算不变，避免平均切得过短。免费接口波动仍可能导致缺失，不将一次成功视作长期可用保证。

诊断现在显示具体接口名称、结果、等待耗时和错误类别，并能识别嵌套financial_report/stock_flow的已填充字段；仍不输出原始异常、接口URL、密钥或字段值。报告正文结构未变，测试替代截图验证数据契约。未增加配置、密钥或数据源账号，不改变模型、PushPlus、定时、AI额度或交易方式；回滚本轮PR即可恢复原取数路径。中文专题排障文档为此fork维护入口，无独立英文同版文档。

验收：先运行离线财务契约、超时隔离、部分结果保留及诊断输出测试，再在修复分支手动运行“数据源诊断（不调用AI）”。只核对两只固定公开样本，不触发模型或通知。接口恢复以新诊断实际字段为准，不保证所有来源成功。

本补丁基于GitHub `09159e87`，保留另一台电脑已上传的实现。不要使用此前基于`7a21cd76`的主板筛选旧包覆盖本版本。原自选股任务、17:17候选任务、每天最多3只候选、模型中转、PushPlus及额度记录均保留。

## 诊断证据及边界

用户提供的10月22日诊断取自`98e93cfd`，早于`09159e87`：5225条沪深行情中5218条交易日期一致，7条成交额未知，旧校验返回`invalid turnover`。最新聚合函数已将未知成交额排除出交易样本，不再把这7条当作零或直接认定停牌；仍校验日期、重复代码、覆盖数和数值范围。不修改该保守聚合逻辑。

新代码返回`sample_stats`，但报告原来只消费顶层完整市场字段，因此备用样本即使成功也没有显示。本次将样本保留到`stats_metadata`，单独输出“已覆盖交易样本、样本涨跌家数、样本成交额、未知成交额行数及零成交额行数”，明确不是完整市场统计。完整市场字段仍为缺失，涨跌停不猜测，信号评分仍使用原partial契约。模型提示词也携带相同样本和范围警示，不把样本用于完整市场结论。

旧快照测试仍期待顶层`up_count/total_amount`，与最新返回契约不一致；本次同步修正。测试中实际执行聚合模块和经理方法，经理与报告用AST加载避开本机缺少的可选SDK，未伪造生产层成功。

财务诊断中：600519估值用了5610ms，整个基本面聚合只有8000ms，财报查询余量约2390ms后超时，后续资金流等耗尽预算。000858估值可用，但财报查询仍失败，资金流出现连接/解析问题，板块超时。0ms timeout可能与前一请求仍运行的隔离保护有关，现有诊断不足以确认。新闻未在此诊断范围内。

两个现有超时参数此前没有在Actions步骤映射：只在GitHub添加变量并不会自动传入应用。本次同时接通原分析、候选AI和无AI诊断三处。不改变全局默认8秒，也不添加付费数据源。

## 安装（网页操作）

1. 解压本次`DSA-data-missing-fix-09159e87.zip`，不要上传ZIP本身。
2. 打开`flash25168/daily_stock_analysis`的Code，分支下拉框创建`fix-report-missing-data`。
3. **Add file → Upload files**，拖入解压后根目录的`.github`、`src`、`tests`、`docs`文件夹和`.env.example`，不能再多套一层目录。只覆盖清单里的文件，其他文件不删除。
4. 提交到此新分支，打开PR，在Files changed核对模型、PushPlus密钥引用没有变化，两套定时和AI名额没有变化，再等待CI并合并。若远端已超过`09159e87`，先比较新改动，不要直接整文件覆盖。
5. 仓库 **Settings → Secrets and variables → Actions → Variables → New repository variable**，添加下面两项（普通数值，不是密钥）：

| Name | Value | 含义 |
|---|---|---|
| FUNDAMENTAL_STAGE_TIMEOUT_SECONDS | 45 | 每只股票基本面聚合的总等待预算 |
| FUNDAMENTAL_FETCH_TIMEOUT_SECONDS | 12 | 每个查询阶段的等待预算；仍受总预算限制 |

这是用于下一轮诊断的起始配置，不是保证恢复的参数。更长预算会延长原任务等待时间，不会直接调用更多AI；数据接口阻断、解析错误或权限不足仍需要另行处理。原超时线程隔离和重试保护不变。

## 先验证，不调用AI

1. 合并后打开 **Actions → 数据源诊断（不调用AI）→ Run workflow**，选择默认分支，运行一次。
2. 查看Summary：`fundamental_settings`应显示`stage_budget_seconds=45`、`fetch_timeout_seconds=12`。若仍为8，说明文件未更新、分支不对或Repository variable名称/位置不对。
3. 快照`validation`应重新核对；`pass`仅表示样本校验通过，不保证完整市场统计。新的交易日必须与当天交易阶段的预期日期一致。
4. 对比600519/000858的growth、earnings、capital_flow和boards的status、error_categories。若仍失败，请复制新的诊断JSON；不要先重跑付费分析。
5. 生产报告展示须在下一次原定任务完成后核对：完整市场未知项继续显示“数据不可用”，其下新增明确标注的样本区。样本成功不代表财报和新闻已恢复，也不能将Actions绿色视为所有数据完整。

## 可复制的展示验收样例

以下仅是用于验证格式的虚构计数，不是实际行情：

> 上涨家数：数据不可用
>
> 可用样本统计（非完整市场统计）
>
> 已覆盖交易样本：5218/5225条
>
> 样本上涨/下跌/平盘：2000/3000/218
>
> 成交额未知：7条

旧展示没有这个样本区。未执行付费模型来生成界面截图，本例及离线文本断言作为替代展示证据。

## 验证与回滚

离线执行：`python -m unittest discover -s tests -p test_market_snapshot_stats.py -v`、`test_market_stats_missing.py`、`test_report_data_diagnostic.py`，并编译修改过的Python文件。实际行情抓取、真实报告投递和模型没有在本地运行；完整依赖导入及全仓CI须在GitHub核验。

回滚代码可在合并PR页面选择Revert。仅想恢复原等待时长，可将这两个变量设回8或删除；报告样本区可保留。没有新增或修改密钥。不保证财报、机构、资金流、板块或新闻全部恢复。

## 后续字段修复与验收

### 独立资金流来源

东方财富历史和收盘快照仍共享上游，不能视作真正独立容灾。沪深个股现在先查询新浪资金页使用的单证券历史接口，再回退到现有东财候选。一次最多10条记录，网络超时4秒，不增加付费数据服务、密钥、AI调用或通知次数。仅可靠交易日历判定的盘后、盘前及非交易日可用；最新记录必须是预期最近交易日。按交易日历核对5/10个交易日，窗口缺一日或金额缺失就显示N/A，不从更早记录凑数。0和负数保留，百分比不冒充金额。

新浪官方页面脚本[历史成交分布](https://n.sinaimg.cn/finance/cnstock/pc/zjlx.z.js?ver=1.1)把`r0_net`/`r1_net`标为超大单/大单净额，原始人民币元在展示时除以10000转万元；本项目将二者相加作为该来源的主力净额，5/10日全段沿用同一来源。报告显示“新浪财经；超大单＋大单净额”，不将新浪与东财分类金额直接比较或混算。参考[新浪资金流说明](https://finance.sina.com.cn/360desktop/stock/data/20150710/140922650279.shtml)与[资金页面](https://vip.stock.finance.sina.com.cn/moneyflow/)。行情收盘价仍由原行情链路提供，不用资金接口的trade字段覆盖。

该单证券接口依靠请求参数`daima=市场+股票代码`限定股票，正常响应不回显证券身份，诊断保留`identity_validation=request_scoped`，不声称独立核实了回显代码；如果响应提供symbol/daima/code且不匹配则拒绝。新浪自身故障或日历不可用时仍可能取不到，已有东财回退继续生效，绝不补零。新浪取到个股数据后不再等待东财行业排名，避免外部超时让已恢复字段一起丢失；行业强弱仍由原有boards阶段获取。

小白复核：仓库Actions → 数据源诊断（不调用AI）→ Run workflow，使用main（修复未合并时选修复分支）。查看Summary两只样本的`capital_flow.status`、`stock_flow.provider`字段覆盖，以及下方“个股主力资金流”的交易日期、来源、当日与5/10日金额；N/A表示未知。这个流程不会发微信。微信展示须等下一份新报告，并核对来源文字。回滚本次PR即可恢复东财优先，不需改密钥或变量。

English: CN capital flow now prefers Sina's independent single-security history (four-second request, at most ten rows), with existing Eastmoney fallbacks retained. The amount is r0_net + r1_net in CNY; the renderer states the source and definition. All windows use the same provider and must contain the exact calendar sessions; missing values are not zero. Identity is request-scoped because the endpoint does not normally echo a symbol; mismatching optional identities are rejected. Recovered stock data returns without waiting for optional Eastmoney sector rankings. No model, notification, schedule, secret or paid-service configuration changes. No paired English topic document exists; this paragraph covers the new behavior.

- 财务指标接口不提供经营现金流总额时，按当前财报的股票代码和报告期查询东财现金流量表；校验证券、报告期和公告日，保持人民币元口径。每股现金流、TTM和不同报告期的数值不替代该字段。
- 资金历史请求只取10个交易日，检查响应代码、证券代码和15列结构；当日、5日、10日金额分别显示，窗口不足或非有限数值保持未知。日期单独展示，不能把历史记录当今日记录。免费接口存在间歇断连，成功一次不保证下一次成功；不伪造资金流，也不以板块涨跌幅替代。
- 机构数变化（家）、持股比例、持股比例增幅（来源原口径，%）分开，不再将机构数变化映射成持股变化。定期披露不是当日机构成交；没有可比较的两期完整股东名单，前十大股东合计变化保持未知，不将首位股东变化当成全部变化。
- 已确定财报期间时，机构和前十大股东查询使用同一已披露期间，并优先于可选预告、快报和分红；不再先消耗预算查询尚未披露的季度。只有名单数量/说明而没有持股变化指标时，机构块标记为partial。资金连接失败在原5秒总网络预算内最多重试一次，JSON/身份/权限错误不触发该重试；发生恢复时诊断保留fetch_retries。
- 历史资金接口失败时，备用查询东财`ulist.np/get`中的单一证券。f62/f164/f174分别为当日、5日和10日主力净流入金额，f124为行情更新时间戳；校验证券代码和市场、可靠交易日历所预期交易日、北京时间15点后更新且不在未来，才接受该收盘快照。未取到的窗口仍为N/A，注明`window_source=provider_snapshot`，不使用净占比代替金额。来源定义见[AkShare资金字段](https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_fund_em.py)、[Efinance批量行情](https://github.com/Micro-sheep/efinance/blob/main/efinance/common/getter.py)与[时间戳定义](https://github.com/Micro-sheep/efinance/blob/main/efinance/common/config.py)。
- 单股和汇总报告均固定追加机构与资金流摘要，不依赖AI是否在正文主动引用这些字段；港台三大法人原有展示不变。
- GPT-6系列在首次请求前省略temperature，原有路由别名解析、错误恢复与其他模型规则继续生效。依据官方部署指南以及当前中转服务default-only报错；没有更换模型或中转配置。
- 六项旧格式测试改为当前手机纵向文本契约，并恢复涨跌颜色标记；保留行情金额、单位、英文/中文和红涨/绿涨的实际检查。

微信验收：长截图已确认两股正文及财务/板块区块存在，但原图在会话中缩小，不能可靠核对每个数字。请分别放大两股的“财务摘要”“机构持股”“个股主力资金流”，检查财报日期、现金流正负号、亿元换算、资金流交易日与N/A。旧消息不会随代码合并自动更新，须核对后续新报告。没有为展示验收新增付费模型调用。

以下为**离线格式测试数据**，并非行情或投资结论，作为无法直接截取手机页面的替代展示证据：

> ### 🏦 机构持股（定期披露）
> - 报告期: 2026-06-30
> - 机构数量变化（家）: -2
> - 持股比例: 3.50%
> - 持股比例增幅（来源原口径）: N/A
>
> ### 💰 个股主力资金流
> - 交易日期: 2026-10-23
> - 当日主力净流入: 1.18 亿元
> - 近5个交易日净流入: -2.00 亿元
> - 近10个交易日净流入: N/A

无AI复核仍使用 Actions → 数据源诊断（不调用AI），选择修复分支运行。Summary中检查`financial_report.operating_cash_flow`、`stock_flow.main_net_inflow`及机构独立字段。诊断JSON只列字段名，不输出密钥或原始异常；摘要另外追加`report_fields.md`，重用正式报告的字段渲染器，展示这两只公开样本的财务、机构和资金流金额/日期。它不初始化通知客户端、不调用模型，也不发送微信，不能代替手机端最终验收。日常中转、PushPlus、股票名单、筛选预算及人工下单规则不变。回滚使用PR的Revert；所有未披露、接口失败或不可比较的数据继续保持未知。

### English field-recovery notes

Cash flow is supplemented from the exact issuer and disclosed statement period in CNY, with identity/period/notice checks. Capital history is bounded to ten sessions and validates response identity and row shape; dates and units are displayed explicitly. Institutional count changes are distinct from provider-defined holding-ratio growth. A first shareholder's change is no longer misrepresented as an aggregate top-ten change. Deterministic report blocks expose the available fields independently of AI prose. GPT-6 requests omit temperature before the first call. Six stale layout assertions now verify mobile text while preserving color/amount/unit contracts. Free endpoints remain intermittent; no complete-data or phone-pixel acceptance claim is made. The Chinese-only operating instructions have this English behavior summary; no existing paired English document exists.

Holdings queries now prioritize the issuer's known disclosed statement period before optional forecasts/dividends. A count/note-only institutional payload is partial. A connection failure may retry once within the original five-second network budget; parse/identity/permission failures are not retried by this transport repair.

When history fails, a single-security Eastmoney closing snapshot can supply the same CNY net-flow fields. Identity, market, the calendar's expected session and a non-future quote timestamp after 15:00 Beijing time are mandatory. Provider-computed 5/10-session amounts retain their source marker; missing windows and ratios never become invented amounts.

## Earlier change: English summary

The supplied diagnostic predates `09159e87`. Its seven unknown-turnover rows are already excluded by the latest sample aggregator. This patch displays the validated sample separately in reports and prompts, preserving missing canonical market fields and partial signal scoring. It updates stale contract tests and forwards existing fundamental timeout Repository variables to the native report, screened AI and no-AI diagnostic steps. Defaults remain eight seconds; test with 45 seconds total and 12 seconds per fetch before any paid rerun. Provider outages, parsing errors, pending request quarantine, credentials and untested news coverage remain separate causes. No secrets, schedules, three-candidate limits or notification configuration are changed. Full SDK imports, hosted CI and actual data delivery still require acceptance. Revert the eventual PR or remove timeout overrides to roll back.
