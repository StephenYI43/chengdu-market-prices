# Windows运行包

这个运行包解决“源码已上传，但另一台电脑缺少镜像、前端产物、仓库与模型”的问题。Dockerfile和Compose是构建、启动说明，实际镜像与挂载数据需要另行传递。本项目使用真实数据预置一个可运行副本，主机只需Docker Desktop和WSL2，无需主机Python环境或Node工具链。

## 接收与启动

1. 安装并打开[Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，确认Linux容器引擎已就绪。
2. 将整个ZIP解压到本机一个目录。
3. 双击`start.cmd`，等待镜像导入、数据初始化和4个服务就绪。
4. 打开`http://127.0.0.1:18880/`。停止运行使用`stop.cmd`，排查问题使用`check.cmd`。

建议16GB主机内存、至少20GB可用磁盘空间。Docker可分配约8GB内存作为起点，具体按本机资源和实际任务调整。Docker安装和WSL首次配置需要按官方提示完成；应用镜像、Python依赖、前端及Spark连接器均已带入运行包。

包内附`先看这里.txt`，包括端口冲突的处理方式、重复文件导入演示和持久化目录说明。

## 内容与持久化

| 内容 | 用途 |
|---|---|
| `images.tar.gz` | 实际运行的应用、ML与Kafka三张Linux amd64镜像，共享层只保存一次 |
| `initial-state.zip` | 当前已验收快照、Parquet分层、对应预测产物、SQLite备份与实验文件 |
| `connectors/` | Spark Kafka连接器及全部配套JAR，启动时不走Maven解析下载 |
| `samples/latest_week.csv` | 738条真实最新一期报价，用于导入演示 |
| `source-data.zip` | 用户取得的真实长表、宽表和Excel，原值保留 |
| `source-code.zip` | 打包时的源码副本及Git版本信息 |
| `bundle.json` | 数据/模型批次、镜像ID、文件SHA256和体积 |

镜像内包含后端依赖、编译后的前端及训练环境。`.venv`不作为Windows目录跨电脑复制。镜像存档使用[Docker image save](https://docs.docker.com/reference/cli/docker/image/save/)，启动时由[Docker image load](https://docs.docker.com/reference/cli/docker/image/load/)恢复。

第一次启动才解压初始状态。后续导入、模型和复核状态保存在解压目录的`runtime/`；再次启动不会用初始状态覆盖它。Kafka使用运行包自己的命名卷。不会复制原电脑的Kafka日志、Spark检查点、工作进程租约或心跳，避免新机器沿用不匹配的消费游标。

应用网页绑定本机127.0.0.1；Kafka不映射到主机。Spark、预测与Kafka使用内部网络，API另接本地网页网络，以便Docker Desktop在Windows上正确发布网页端口。Compose不含build，镜像拉取策略为never，Spark使用包内JAR显式启动。

## 从当前已运行项目制作

```powershell
.venv/Scripts/python.exe scripts/export_handoff_images.py --output-dir artifacts/handoff-build
.venv/Scripts/python.exe scripts/package_windows.py --image-archive artifacts/handoff-build/images.tar.gz --images-json artifacts/handoff-build/images.json --output '交付材料/chengdu-market-windows-20261125'
```

输出目录须为新目录。镜像导出按当前运行容器的镜像ID绑定；已有存档如不匹配则拒绝复用。仓库快照为不可变文件，SQLite使用在线backup API复制，打包前后核对正式仓库与指针SHA。

## 隔离验证

```powershell
.venv/Scripts/python.exe scripts/verify_windows_bundle.py --archive '交付材料/chengdu-market-windows-20261125.zip' --test-root 'artifacts/handoff-acceptance/同学 电脑' --port 18885 --project chengdu-handoff-acceptance
```

验证从实际ZIP解压到包含中文与空格的新路径，使用Windows自带PowerShell启动器，强制执行镜像导入，并使用独立容器、网络和数据卷。检查真实数据与模型、内置前端、真实CSV上传幂等、真实738条报价经Kafka与Spark落Parquet并由DuckDB逐字段核对，以及重复启动、停止与重启。完成后移除测试容器和网络，保留测试日志及数据；正式服务保持运行。

这是同一台Windows电脑上使用同一个Docker引擎的隔离重部署验证，不代表已经在接收方的硬件上实测。最终检查证据见`docs/evidence/windows-handoff-verification.json`。
