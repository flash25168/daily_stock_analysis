# 大盘备用快照 / Market snapshot fallback

原有大盘数据源全部失败后，CN 大盘复盘会复用现有选股 em_datacenter 接口，不配置新密钥。核对分页总数稳定、行数匹配、无重复代码、至少3000条沪深行、成交额有效、交易行 MAX_TRADE_DATE 与既有交易日历的已收盘日期相同。仅允许盘后、盘前或非交易时段；未知日历拒绝。未通过继续显示数据不可用，不混入旧缓存。

统计口径明确为：来源声明覆盖范围内沪深 A 股、成交额大于零的交易行；排除北交所，零成交额行不计入上涨/下跌/平盘。成交额 DEAL_AMOUNT 按元换算亿元。接口声明总数与最低样本量不是独立的上市股票全覆盖证明，来源日期也不证明每只股票最新成交；报告显示来源与口径。无法可靠推导的涨跌停数量仍缺失，不按涨幅猜测。此来源与东方财富其他接口同属一个服务商，并非独立行情校验源。

指数宽表改为每个指数一个文字块；数据质量非 ok 时，固定信号区显示“信号不完整”。保留评分算法与 partial 数据契约，分数不是上涨概率。模型自行生成的其他表格不在此次改动范围。

Existing providers remain first. The fallback reuses the screening endpoint and validates pagination, row count, codes, dates, and numeric fields before aggregating Shanghai/Shenzhen traded rows. Source-declared coverage and trade dates are not independent proof of complete universe coverage or live prices. Limit counts remain unavailable. Incomplete signals are explicitly labeled; index tables become mobile-friendly text blocks.

验证：14项离线测试通过（5项备用数据/管理器/分页测试，9项缺失统计/提示词/显示测试），变更Python文件编译通过。无付费模型调用，未执行GitHub在线任务、真实接口恢复或完整项目测试；本机缺少可选新闻SDK，显示测试使用生产方法AST加载。配置、两套定时、PushPlus和现有搜索密钥不变。

验收：下一次运行日志出现 provider=em_datacenter status=success 并核对日期、来源、口径；如果 unavailable，查看原因，不能将工作流绿色视为数据完整。对照报告应出现“信号不完整”和竖排指数。前后文本示例：旧为8列表格，现为“指数名称 / 最新与涨跌幅 / 开高低 / 振幅 / 成交额”的逐项列表；以此可复制文本证据替代界面截图。

回滚：恢复本次上传前文件版本；对新增文件可保留不用，恢复 base.py 后不再进入备用路径。没有新增环境变量，无需删除或重建密钥。
