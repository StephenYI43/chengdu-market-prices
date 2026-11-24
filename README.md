# 成都市农贸市场价格分析与预测平台

基于用户取得的真实周度监测数据，逐步实现历史价格分析、跨市场比较、Kafka与Spark流式处理、周度预测和网页展示。

当前阶段：第一天的运行环境、项目骨架、数据契约和真实样例链路验证。历史分析与预测功能尚未实现。

首日验收已通过：738条真实记录经Kafka、Spark Structured Streaming写入Parquet，并由DuckDB核对。见 [完成报告](docs/day1-report.md) 与 [验收结果](docs/evidence/day1-verification.json)。

## 输入数据

原始Excel和长宽CSV保留在 `D:/bigdatasource`，不修改。`data/samples/latest_week.csv` 是按原值抽取的完整最新一期，只用于隔离验证，不是生成数据。全量统计与哈希见同目录 `manifest.json`。

## 环境

- Windows Docker Desktop与WSL2，Linux容器。
- Spark 4.3.0与Kafka 4.4.0，镜像按SHA-256锁定。
- Python 3.13由uv管理，JDK 21使用Spark官方镜像内版本。
- 后端和开发依赖锁定于 `uv.lock`，前端依赖锁定于 `frontend/pnpm-lock.yaml`。
- Kafka和API仅绑定本机127.0.0.1，不修改已有项目的容器或卷。

## 启动后端与Kafka

```powershell
docker compose up -d --build kafka api
```

接口文档：<http://127.0.0.1:18880/docs>。就绪检查：<http://127.0.0.1:18880/health/ready>。

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
