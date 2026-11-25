# 第四天：真实文件增量导入与持续流处理

## 实现范围

2026-11-25，在已有历史分析网页上增加“数据管理”：CSV与XLSX上传、长宽表识别、全量预览校验、明确确认、幂等任务和实际状态通知。新增数据经Kafka与常驻Spark Structured Streaming处理，复用第二天清洗统计，独立核对后发布新的不可变仓库批次。

真实报价日期保留源值。现有文件最新一期仍为2026-11-24，通知时间与任务心跳不冒充报价更新时间。当前入口为文件导入，自动官网采集、手工逐条编辑与预测模型尚未实现。

## 使用方法

```powershell
powershell -NoProfile -File scripts/start-day4.ps1
```

打开 <http://127.0.0.1:18880/#data-management>。可上传用户原始Excel、长表CSV、宽表CSV或系统明细导出CSV。单次20MB、最多100,000条展开后的市场报价；Excel默认选择“长表”。预览显示读取、新增、历史重复、文件内重复、冲突和无效数量。确认前不会创建入库任务。

全部重复记录会产生“无需新增”的检查任务；有价格冲突或无效行时整批阻止，原历史不覆盖。新商品规格和市场需先维护config映射后重新预览。当前数据管理用于可信本机课程演示环境，无账户及多用户权限体系。

## 数据流与一致性

上传原文件及SHA-256 → 保存预览和候选记录 → 确认任务 → Kafka单分区报价消息与结束标记 → Spark每2秒微批 → SQLite inbox按任务与事件ID去重 → 合并原始历史和实际新增 → Spark重算各层 → DuckDB与Python Decimal独立核对 → 发布新批次 → SSE通知。

SQLite保存任务、收件箱、事件游标与消费者租约；Spark checkpoint在runtime/stream/checkpoint。重复确认、消息重放和同一文件再次导入不会重复增加业务报价。重启会恢复发送或计算中的任务；未完成目录留作排查，新尝试使用新目录。

旧DuckDB批次只读且不覆盖。发布时比较预期父批次，避免过期计算覆盖已更新数据。前端图表固定run_id，收到新批次通知后点击“加载新数据”才切换。多文件来源使用source-manifest.json，每条报价继续保留原始字符串、实际来源文件SHA与记录号。

## 隔离验收复现

正式数据只读。验收在runtime/day4-acceptance和独立Kafka主题price.imports.acceptance.v1、API端口18882运行，从真实历史留出最新一期738条及更早的1条，原价格、日期和缺失符号均不改写。

```powershell
$env:PYTHONPATH='D:/bigdata/src'
.venv/Scripts/python.exe scripts/seed_day4_acceptance.py --prepare
docker compose --profile tools run --rm worker /opt/spark/bin/spark-submit --master 'local[2]' --driver-memory 2g scripts/seed_day4_acceptance.py
docker compose --profile acceptance up -d --wait --wait-timeout 240 api-acceptance stream-acceptance
.venv/Scripts/python.exe scripts/verify_day4.py
docker compose --profile acceptance stop api-acceptance stream-acceptance
```

首次初始化创建74,987条真实记录的基线；上传739条原值记录后应还原75,726条完整业务账本。脚本在计算阶段实际重启隔离消费者，核对恢复、幂等、历史字段保留、全量账本、SSE游标续传和正式仓库SHA不变。已存在验收快照时种子脚本会拒绝重建，防止覆盖证据。

## 实测结果

- 原始长表CSV、宽表CSV、Excel均读取75,726条，全部匹配正式历史账本，无冲突和无效记录，三个原文件的字节哈希保持不变。Excel自动选择“长表”。
- 隔离基线74,987条，真实导入739条，恢复后75,726条：75,564个有效价格、162个源“-”。原有业务字段和来源信息逐条保留，正式current指针与数据库SHA均未变化。
- 实际在building阶段重启隔离消费者，恢复后完成；重复确认只创建一个任务，再次导入为no_op；冲突价格被阻止，旧批次仍可查询。
- 完整快照通过10项独立核对，包含全部报价字段、业务键和来源ID唯一性、所有商品与区的Decimal汇总、空报价和周历。任务链路核对包括SSE Last-Event-ID续传及正式数据保护。
- 浏览器实际上传738条真实样例、预览并确认后显示“无需新增”，再次确认按钮禁用。1280px与390px布局均无页面横向溢出；临时视口已还原。
- 真实发布事件到达时，原图表仍显示74,987条和104期；点击“加载新数据”后显示75,726条和105期，截止日期从2026-11-17变为2026-11-24。未捕获浏览器控制台错误。
- 58项后端测试通过；历史查询回归20项检查通过，覆盖全部44个规格、4,207个周点和最新738条明细。前端TypeScript检查与生产打包、Ruff检查和编译检查通过。

保存的证据：`docs/evidence/day4-verification.json`、`docs/evidence/day4-source-file-previews.json`、`docs/evidence/day4-browser-verification.json`；完整运行目录与日志位于`artifacts/day4-acceptance`。隔离服务验证后停止，正式Kafka、API及持续消费者保持运行。

当前为单机课程实现：Kafka主题单分区、Spark local[2]，每次确认按增量记录合并历史并重算汇总，便于保证历史口径一致；没有宣称已部署分布式集群或运行海量数据。2秒是消费检查间隔，整批统计验收耗时另计。镜像沿用`day1`标签供已有脚本复用，当前代码与API版本已更新至第四天。
