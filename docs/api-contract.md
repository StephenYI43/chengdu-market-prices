# 价格查询、文件导入与流处理接口契约

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
| POST | /api/v1/imports/preview | multipart上传CSV/XLSX，完整校验、分类和前20条预览，不入库 |
| POST | /api/v1/imports | JSON传preview_id，幂等确认，返回持久化任务（202） |
| GET | /api/v1/imports/service | 持续消费者心跳、当前批次和事件游标 |
| GET | /api/v1/jobs | 最近任务，limit默认20，最多100 |
| GET | /api/v1/jobs/{job_id} | 任务状态、实际记录计数与失败原因 |
| GET | /api/v1/stream/events | SSE任务通知，支持after和Last-Event-ID续传 |
| GET | /api/v1/forecasts/catalog | 指定数据批次的训练状态、模型目录与回测概况 |
| GET | /api/v1/forecasts | 指定商品规格的预测、历史均价、三模型误差和真实回测明细 |
| GET | /api/v1/forecasts/export.csv | 带模型计算值标记的未来四周预测CSV |
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

未来分析接口固定按报价日期筛选。回放使用隔离数据空间，不能显示为当前实时报价。历史回放预测只能使用该历史截止期已可见的数据。

## 文件导入规则

preview接收file、source_url（默认原官网栏目）、可选sheet_name。限制20MB、展开后100,000条报价；Excel最多100MB解压内容。支持原始7列长表、以“区·市场”作列名的宽表和平台导出的明细CSV，编码支持UTF-8、GB18030及带BOM的UTF-16。Excel默认优先“长表”，多个候选表需指定名称，不重复读取长宽两个表。

必填字段为日期、商品名称、产品规格、计量单位、区、市场、价格。报价日期按源数据周二口径校验；商品规格与市场须已登记，价格为正数或“-”。公式不执行，空白不视为无报价。

counts分别为new、existing_duplicate、file_duplicate、conflict、invalid，总和等于source_rows。业务键按日期、原始商品名、仅规范标点空白的原始规格、单位、区、市场计算。相同键和价格跳过；同键不同价格或报价状态禁止覆盖。有任意冲突或无效行则整个确认被阻止；修正文件后重新预览。预览批次、配置或保存内容变化返回409。

确认用preview_id作为幂等任务ID。同一预览重复确认只返回同一任务。全重复直接no_op；有新增则queued → publishing → streaming → building → verifying → succeeded。实际发送、接收、插入和跳过数量分别返回，计算阶段不伪造百分比。失败为failed并保留错误信息；消费者重启会恢复未完成任务，Kafka与本地inbox按事件ID去重。

SSE事件为job.updated和snapshot.published，id是持久化递增序号，断线重连从Last-Event-ID之后继续；空闲15秒发送心跳。它通知处理进度，不代表官网每秒发布价格。业务日期保留源日期，处理时间另存UTC，界面以北京时间显示。

新增报价先写独立不可变快照，Spark重算所有历史汇总及质量表，独立核对后以父批次比较的方式切换current.json。旧批次保留。多文件快照meta.source_kind=manifest、source_files_count表示来源文件数；source_file_sha256在此时对应source-manifest.json清单的哈希，各条报价仍保留实际来源文件哈希和记录号。图表收到新批次通知后由用户点击加载，原视图持续固定旧run_id。

## 周度预测

三个预测接口均接受可选run_id固定数据批次。detail和export需要product_id，可传model_run_id防止模型版本变化；当前模型ID不一致返回409，未知商品404，批次没有匹配模型时detail返回503，catalog返回200、status=not_ready和训练服务状态。模型文件损坏返回503并由服务重训，不能回退为无标记的过期预测。

catalog的report包含训练截止期、12个验证截止期、12个测试截止期、25次拟合的时间审计、LightGBM版本、MLflow run ID、模型选择分布和当前37个规格的宏平均测试MAPE。每个商品status为ready或excluded，后者forecasts为空并附profile排除原因，不生成填补价格。

detail.forecasts按未来1—4周返回target_date、horizon、predicted、lower、upper与reference_samples；价格和参考带端点为四位小数字符串。lower/upper是验证期80%绝对误差分位产生的参考带，不保证未来覆盖率。evaluation返回三种方法的MAE、RMSE、MAPE、WAPE和次数，另附选用方法分跨度指标及逐次预测/实际值；比例指标用0到1的比率表示。history仅包含源表实际出现的均价。

CSV采用UTF-8 BOM，并固定data_kind=model_prediction_not_observed_quote，包含数据与模型批次、训练截止期、原规格、单位和目标日期。历史报价导出和预测导出使用不同接口。

持续预测服务每15秒检查数据与训练指纹变化；先对新批次完成全部回测与拟合，再写不可变模型目录及原子指针。新批次等待期间不借用旧批次模型。已有相同训练版本的旧数据批次模型仍可固定读取。接口只读预测JSON与哈希清单，不在HTTP请求中加载可执行模型或触发同步训练。
