# 历史价格查询接口契约

## 当前已实现

| 方法 | 地址 | 行为 |
|---|---|---|
| GET | /health/live | API进程存活状态 |
| GET | /health/ready | 访问Kafka元数据，未就绪返回503 |
| GET | /api/v1/metadata | 已核实源文件概况和真实样例来源 |
| POST | /api/v1/contracts/validate-quote | 校验并返回报价消息，不入库、不发送Kafka |
| GET | /api/v1/overview | 当前或指定已验收快照的规模、日期、类别、区和默认商品 |
| GET | /api/v1/products | 商品规格目录，支持q和category筛选 |
| GET | /api/v1/markets | 市场目录，支持district筛选 |
| GET | /api/v1/prices | 实际报价明细、筛选、排序和分页 |
| GET | /api/v1/prices/export.csv | 当前筛选下全部实际明细，UTF-8 BOM CSV |
| GET | /api/v1/trends | 指定商品规格的周趋势，可选区或市场 |
| GET | /api/v1/comparisons | 指定日期、商品规格和范围的市场报价与并列排名 |
| GET | /docs | 交互式接口文档 |
| GET | /openapi.json | 当前真实实现的OpenAPI描述 |

数据事件JSON Schema见 `contracts/quote-event.schema.json`。货币价格通过JSON字符串保存，避免二进制浮点误差；缺失价格为null。

当前完整OpenAPI见 `contracts/openapi.json`，历史首日契约保留在 `contracts/openapi.day1.json`。`/metadata` 仍提供首日样例来源；分析网页使用 `/overview` 读取实际可用仓库。

## 数据批次与读取方式

查询接口支持 `run_id`，省略时读取 `runtime/warehouse/current.json` 所指的已验收批次。每个响应均包含 `meta`，说明实际读取的批次、截止日期、源文件、哈希和 `data_mode=historical_snapshot`。

前端先读取overview，再将该run_id传给其余查询。同一请求使用一个只读数据库连接；批次目录不覆盖，因此当前指针变化不会令同一张图或表跨批次拼接。未验收、数据库元数据不匹配或默认仓库不可用时返回503；指定批次不存在返回404。

## 查询参数

| 参数 | 使用位置 | 规则 |
|---|---|---|
| product_id | prices、trends、comparisons、export | 必填，按独立规格选择；未知值404 |
| date_from / date_to | prices、trends、export | ISO日期，包含边界；省略则不限制历史范围；开始晚于结束返回422 |
| quote_date | comparisons | ISO日期，省略则取整个快照的最新报价日期 |
| district | markets及价格查询 | 原始监测区分组名称；未知值404 |
| market_id | prices、trends、comparisons、export | 市场ID；与district不相符返回422 |
| quote_status | prices、export | quoted或missing；不传则全部 |
| review_only | prices、export | true时仅返回已存在质量提示的记录 |
| sort | prices、export | date_desc（默认）、date_asc、price_asc、price_desc |
| page / page_size | prices | 页码从1开始，默认20条，最多100条；排序附稳定的次级排序，价格空值排最后 |
| q / category | products | 名称或规格的文字包含搜索；分类精确匹配，查无匹配返回空列表 |

SQL值使用绑定参数，排序使用固定枚举。响应和导出只包含实际报价记录，不为缺期生成明细。

## 比较与缺失语义

comparisons按所选范围的市场维表列出所有应观察市场。`quoted` 表示有价格，`missing` 表示源文件为“-”，`not_observed` 表示该规格在该期没有记录。后两者价格均为null，排名也为null。

summary同时返回观察、有价格、显式无报价、缺记录和应观察市场数。少于两个有效市场时 `ranking_available=false`。排名按价格升序，价格相同使用并列名次。

trends仅返回实际出现记录的报价期。客户端用实际报价日历补出图中的缺期位置，以null形成断点，不补价格。普通周变化只在相隔7天时计算；同市场周变化先配对两期均有价格的市场。日期窗口筛选在前期上下文计算之后进行，避免窗口首点丢失真实的前期信息。

prices返回 `total`、`page`、`page_size` 和 `items`，保留 `price_raw`、`product_spec_raw`、原文件记录序号和来源记录ID。CSV导出与明细筛选一致，不受当前页限制；空结果仅含表头。

## 状态码

- 200：成功，包括正常空结果。
- 404：商品、市场、区或指定批次不存在。
- 422：参数类型、分页、排序、日期区间或区市场组合不合法。
- 503：已验收仓库不可用；页面展示具体提示和重新读取入口，不返回伪造价格。

## 后续接口草案 尚未实现

| 方法 | 地址 | 主要参数或返回 |
|---|---|---|
| GET | /api/v1/alerts | 异常变化与缺失状态，保留原始记录 |
| GET | /api/v1/forecasts | 商品、预测截止期、预测期、模型版本及回测指标 |
| GET | /api/v1/stream/events | SSE处理通知，包含业务日期和处理时间 |
| POST | /api/v1/imports | 新一期文件导入任务，返回任务ID |
| GET | /api/v1/jobs/{job_id} | 任务状态、计数、耗时和失败原因 |

未来分析接口固定按报价日期筛选。回放使用隔离数据空间，不能显示为当前实时报价。历史回放预测只能使用该历史截止期已可见的数据。
