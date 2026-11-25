# 成都市农贸市场价格分析与预测平台

基于用户取得的真实周度监测数据，逐步实现历史价格分析、跨市场比较、Kafka与Spark流式处理、周度预测和网页展示。

当前阶段：第六天已加入可追溯的预警与复核、原始记录依据、复核备注与操作历史、筛选导出及运行概况；历史分析、持续处理和周度预测保持可用。

## 打开价格观察网页

已运行时访问 **http://127.0.0.1:18880/** 。历史分析支持商品规格、日期、区与市场筛选，周趋势、同日比价、分页、缺失及复核筛选和CSV导出。左侧“数据管理”可上传文件，预览分类后确认导入，并查看实际任务状态。

```powershell
powershell -NoProfile -File scripts/start-day6.ps1
```

已构建过当前源码，可加 `-SkipBuild`。构建脚本在本机用Node/pnpm打包前端，再将静态文件放入API镜像，同一地址提供网页和接口。需要第一天的开发依赖、Node 24、pnpm 11.13.0和Docker Desktop；仓库数据由第二天流程生成。新电脑可先运行全量清洗，再启动网页。

左侧“周度预测”显示37个可预测规格的未来4周结果、误差参考带、独立时段回测和预测CSV；7个未入选规格保留具体原因。预测结果标注为模型计算值，不写回真实报价表。

左侧“预警与复核”默认查看最新报价期，支持历史筛选、规则依据、来源记录和可撤销的复核状态。“运行概况”显示Kafka、持续消费者、预测模型与数据业务日期。

第六天使用与验收见 [完成报告](docs/day6-report.md)，预测阶段见 [第五天报告](docs/day5-report.md)，增量导入见 [第四天报告](docs/day4-report.md)。当前接口定义见 [接口契约](docs/api-contract.md) 和 `contracts/openapi.json`；`contracts/openapi.day1.json` 保留首日历史版本。

首日验收已通过：738条真实记录经Kafka、Spark Structured Streaming写入Parquet，并由DuckDB核对。见 [完成报告](docs/day1-report.md) 与 [验收结果](docs/evidence/day1-verification.json)。

第二天的流程、输出和检查结果见 [完成报告](docs/day2-report.md)、[清洗口径](docs/cleaning-rules.md) 与 [仓库结构](docs/warehouse-schema.md)。

## 全量清洗与周度汇总

```powershell
powershell -NoProfile -File scripts/run-day2.ps1
```

默认读取 `D:/bigdatasource` 下的长表与宽表，可用 `-SourceDir` 指定其他目录。流程会构建工作镜像、运行Spark，并使用Python和DuckDB独立复核。已经构建过同一份源码时可以加 `-SkipBuild`。

每次写入新的 `runtime/warehouse/runs/<run_id>/`，只有验收通过后才切换 `runtime/warehouse/current.json`。其中 `warehouse.duckdb` 可直接查询；`artifacts/day2/<run_id>/` 包含预测候选、最新一期汇总、规格映射和质量提示的CSV，均可用WPS打开。CSV只包含实际数据或实际数据的统计，不生成预测值。

## 输入数据

原始Excel和长宽CSV保留在 `D:/bigdatasource`，不修改。`data/samples/latest_week.csv` 是按原值抽取的完整最新一期，只用于隔离验证，不是生成数据。全量统计与哈希见同目录 `manifest.json`。

## 环境

- Windows Docker Desktop与WSL2，Linux容器。
- Spark 4.3.0与Kafka 4.4.0，镜像按SHA-256锁定。
- Python 3.13由uv管理，JDK 21使用Spark官方镜像内版本。
- 后端和开发依赖锁定于 `uv.lock`，前端依赖锁定于 `frontend/pnpm-lock.yaml`。
- Kafka和API仅绑定本机127.0.0.1，不修改已有项目的容器或卷。

## 构建应用与启动服务

```powershell
powershell -NoProfile -File scripts/start-day6.ps1
```

接口文档：<http://127.0.0.1:18880/docs>。就绪检查：<http://127.0.0.1:18880/health/ready>。

直接执行 `docker compose build api` 前需先在 `frontend` 下运行 `pnpm build`。推荐使用上述一键脚本，它会在依赖输入变化时安装依赖、编译前端、构建镜像，并等待服务存活后验证数据入口。

## 验证查询接口

```powershell
$env:PYTHONPATH='D:/bigdata/src'
.venv/Scripts/python.exe scripts/verify_day3.py
```

HTTP验收与独立打开的已验收数据库比较全部商品趋势和最新一期明细。结果保存至 `artifacts/day3/api-verification.json`。

## 重复真实样例验证

```powershell
powershell -NoProfile -File scripts/verify-day1.ps1
```

每次创建独立的 `price.day1.<run_id>` 测试主题，发布一个完整真实报价期，Spark Structured Streaming使用availableNow消费。验证结果保存在 `artifacts/day1/<run_id>/verification.json`，包括逐个事件ID、数量、无报价语义和传输控制合计的核对。

完成后Kafka、API继续运行，临时Spark验证容器自动退出。停止本项目使用 `docker compose stop`，不删除卷。

## 本地开发

```powershell
uv sync --frozen
uv run pytest
uv run ruff check .
cd frontend
pnpm install --frozen-lockfile
pnpm build
```

项目已准备本机作用域的uv。若uv不在PATH中，可使用 `.tools/bin/uv.exe`，实际位置以 `docs/day1-report.md` 为准。

数据字段与分析口径见 `docs/data-dictionary.md`，接口实现状态与草案见 `docs/api-contract.md`。第一天实测结果记录于 `docs/day1-report.md`。
