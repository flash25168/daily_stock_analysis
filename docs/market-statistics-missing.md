# 大盘统计缺失修正 / Missing market statistics

接口失败、超时或返回全零空快照时，涨跌家数、涨跌停及成交额不再作为零事实传给模型；提示词与固定报告显示“数据不可用”。部分字段缺失逐项标记；有效市场快照中真正为零的涨跌停保留。结构化报告已有 breadth 字段中的缺失项为 null，消费者应允许 null。信号评分继续使用原有 partial 机制，不把缺失字段参与已观测数据判断。

Provider failures/timeouts and empty all-zero snapshots are marked unavailable in both model input and deterministic reports. Missing structured breadth fields are null. Genuine zero limit counts in a valid snapshot remain valid. Existing provider order, retries, timeouts, schedules, model and notification settings remain unchanged.

检查过原有备用链：东方财富失败会尝试其他来源；日志显示个股日线降级成功，但大盘统计备用源也超时。此补丁不保证上游恢复，不添加付费数据源，不修复未配置新闻服务或财务资料缺失。

验证：8 项离线回归（含完整提示词路径）和语法编译通过。测试直接执行生产方法 AST，绕过本机缺少的可选 newspaper/模型依赖；未完成全项目集成、GitHub 在线执行或真实数据/模型/推送验证。未调用付费模型。回滚：恢复上传前对应文件版本。

示例 / Example：旧输入“上涨 0、下跌 0、成交额 0”，新输入“上涨家数: 数据不可用；下跌家数: 数据不可用；两市成交额: 数据不可用”。此为文本语义修复，附文本前后对照代替界面截图。
