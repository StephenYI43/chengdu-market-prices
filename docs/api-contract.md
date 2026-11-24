# 第一天接口契约

## 当前已实现

| 方法 | 地址 | 行为 |
|---|---|---|
| GET | /health/live | API进程存活状态 |
| GET | /health/ready | 访问Kafka元数据，未就绪返回503 |
| GET | /api/v1/metadata | 已核实源文件概况和真实样例来源 |
| POST | /api/v1/contracts/validate-quote | 校验并返回报价消息，不入库、不发送Kafka |
| GET | /docs | 交互式接口文档 |
| GET | /openapi.json | 当前真实实现的OpenAPI描述 |

数据事件JSON Schema见 `contracts/quote-event.schema.json`。货币价格通过JSON字符串保存，避免二进制浮点误差；缺失价格为null。

## 后续接口草案 尚未实现

| 方法 | 地址 | 主要参数或返回 |
|---|---|---|
| GET | /api/v1/products | 商品、规格、单位、历史覆盖范围 |
| GET | /api/v1/markets | 区、市场编码和名称 |
| GET | /api/v1/prices | product_id、date_from、date_to、district、market_id、分页 |
| GET | /api/v1/trends | 同商品规格的周趋势、报价市场数 |
| GET | /api/v1/comparisons | 同周同商品规格的市场价格比较 |
| GET | /api/v1/alerts | 异常变化与缺失状态，保留原始记录 |
| GET | /api/v1/forecasts | 商品、预测截止期、预测期、模型版本及回测指标 |
| GET | /api/v1/stream/events | SSE处理通知，包含业务日期和处理时间 |
| POST | /api/v1/imports | 新一期文件导入任务，返回任务ID |
| GET | /api/v1/jobs/{job_id} | 任务状态、计数、耗时和失败原因 |

未来分析接口固定按报价日期筛选。回放使用隔离数据空间，不能显示为当前实时报价。历史回放预测只能使用该历史截止期已可见的数据。
