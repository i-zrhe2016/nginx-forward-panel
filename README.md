# nginx-forward-panel

`nginx-forward-panel` 是一个基于 `Flask + Nginx stream` 的轻量面板，用来在宿主机上动态管理 TCP 端口转发规则。

它适合这类场景：

- 给不同客户或不同用途临时开独立端口
- 将多个入口端口转发到固定上游
- 按到期时间自动停用端口
- 按累计流量上限自动停用端口
- 在网页里查看每个端口的累计连接数和流量

项目当前不是通用的 V2Ray 面板。根服务是一个 Nginx `stream` 转发管理器；历史上的 `dimt_v2/` 配置和测试资料属于本地敏感材料，不随公开仓库分发。

## 功能特性

- Web 面板增删改查端口转发规则
- 规则变更后自动生成 Nginx `stream` 配置并校验
- Nginx 配置校验通过后自动 reload
- 支持 Basic Auth 面板登录保护
- 支持端口备注
- 支持端口到期自动停用
- 支持流量上限自动停用
- 已达流量上限的端口支持重置流量并恢复启用
- 解析 `stream-access.log`，统计总连接数、总流量、今日流量、最后访问时间
- 首次启动可自动写入一个默认端口
- SQLite 数据库支持定时备份

## 工作原理

应用启动后会做几件事：

1. 初始化 SQLite 数据库
2. 根据数据库里的启用端口生成 `/etc/nginx/streams-enabled/ports.conf`
3. 执行 `nginx -t`
4. 启动 Nginx
5. 后台定时扫描访问日志并同步流量统计
6. 对已过期或已超流量上限的端口自动停用并 reload Nginx

每条端口规则最终会生成类似下面的 Nginx `stream` 配置：

```nginx
server {
    listen 31098 reuseport;
    proxy_connect_timeout 5s;
    proxy_timeout 600s;
    proxy_pass example.com:443;
}
```

## 快速启动

推荐直接使用 Docker Compose。

### 1. 启动

```bash
docker compose up -d --build
```

默认配置下：

- 面板地址：`http://服务器IP:18080`
- 容器使用 `host` 网络模式
- 数据库持久化到 `./data`
- 日志持久化到 `./logs`

### 2. 停止

```bash
docker compose down
```

### 3. 查看状态

```bash
docker compose ps
docker compose logs -f
```

## 页面使用

面板首页可以直接完成这些操作：

- 新增监听端口
- 设置转发目标主机和目标端口
- 设置到期时间
- 设置流量上限，例如 `10G`、`500MB`、`1048576`
- 编辑备注
- 手动启用或停用端口
- 删除端口

状态说明：

- `运行中`：端口启用，且未过期、未达到流量上限
- `已停用`：手动停用
- `已过期`：到达过期时间后自动停用
- `已达流量上限`：累计收发流量超过上限后自动停用

流量重置说明：

- 对 `已达流量上限` 的端口，页面会显示“重置流量并启用”
- 该操作会清零该端口的累计流量和按天流量统计
- 累计连接次数不会被重置

## 环境变量

常用环境变量如下。

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `PANEL_HOST` | `0.0.0.0` | Flask 面板监听地址 |
| `PANEL_PORT` | `18080` | Flask 面板监听端口 |
| `PANEL_USERNAME` | 空 | Basic Auth 用户名，留空表示不开启认证 |
| `PANEL_PASSWORD` | 空 | Basic Auth 密码 |
| `DEFAULT_UPSTREAM_HOST` | `nat.qq.pw` | 新建端口时默认目标主机 |
| `DEFAULT_UPSTREAM_PORT` | `31098` | 新建端口时默认目标端口 |
| `SEED_LISTEN_PORT` | `31098` | 首次启动且数据库为空时自动创建的监听端口 |
| `PROXY_CONNECT_TIMEOUT` | `5s` | Nginx `proxy_connect_timeout` |
| `PROXY_TIMEOUT` | `600s` | Nginx `proxy_timeout` |
| `MAINTENANCE_INTERVAL` | `10` | 后台维护线程扫描日志和自动停用的间隔，单位秒 |
| `PROBE_ENABLED` | `0` | 是否启用后端连通性探针，默认关闭，设为 `1` 可恢复 |
| `PROBE_INTERVAL` | `60` | 探针执行间隔，单位秒 |
| `PROBE_TIMEOUT` | `3` | 单次 TCP 探针超时，单位秒 |
| `PROBE_TEST_LISTEN_PORT` | 空 | 探针监控页固定使用的测试端口；在当前 `docker-compose.yml` 中示例固定为 `31098`，留空时自动选第一条已启用端口 |
| `DATA_DIR` | `/data` | 数据目录 |
| `DB_PATH` | `/data/panel.db` | SQLite 数据库路径 |
| `DB_BACKUP_DIR` | `/backups` | SQLite 备份输出目录 |
| `DB_BACKUP_KEEP_DAYS` | `7` | 备份保留天数，超期自动清理 |
| `DB_BACKUP_PREFIX` | `nginx-forward-panel` | 备份文件名前缀，文件名形如 `nginx-forward-panel-20260531T030000Z.db` |
| `DB_BACKUP_CRON_SCHEDULE` | `0 3 * * *` | 备份定时表达式 |

代码里还有一些偏内部用途的路径变量，例如：

- `NGINX_CONFIG_PATH`
- `STREAMS_DIR`
- `STREAM_ACCESS_LOG`
- `NGINX_PID_PATH`

一般不需要改。

## 目录结构

```text
.
├── app/
│   ├── panel.py              # Flask 应用和 Nginx 管理逻辑
│   ├── templates/index.html  # 面板页面
│   └── static/style.css      # 页面样式
├── data/
│   └── panel.db              # SQLite 数据库
├── logs/
│   ├── error.log             # Nginx 错误日志
│   └── stream-access.log     # Nginx stream 访问日志
├── docker-compose.yml
├── Dockerfile
├── nginx.conf
├── scripts/
│   ├── backup_db.py        # SQLite 备份脚本
│   └── start-backup-cron.sh # 备份定时任务启动脚本
```

## 数据与统计

统计数据来自 Nginx `stream` 访问日志，日志格式为：

```text
$time_iso8601    $server_port    $bytes_sent    $bytes_received
```

程序会把日志增量同步到 SQLite 中的这些表：

- `ports`：端口配置
- `traffic_totals`：每个监听端口的累计统计
- `traffic_daily`：按天统计
- `app_state`：日志同步偏移量等运行状态

流量上限判断使用：

```text
累计出站 + 累计入站
```

## 健康检查

服务提供健康检查接口：

```text
GET /healthz
```

当 Nginx 运行正常时返回 `200`，否则返回 `500`。

## 部署注意事项

- `docker-compose.yml` 使用的是 `network_mode: host`，更适合 Linux 主机。
- 面板里创建的监听端口会直接在宿主机生效，避免和宿主机已有服务端口冲突。
- `nginx-forward-panel-db-backup` 服务会按定时表达式把 `panel.db` 备份到 `./backups`。
- 如果设置了 `PANEL_USERNAME` 或 `PANEL_PASSWORD`，面板会启用 Basic Auth。
- 删除端口时会同时删除该端口对应的累计统计数据。
- 程序依赖 `nginx-full` 的 `stream` 能力，镜像里已经安装好。
- 如果你手动修改 `/etc/nginx/streams-enabled/ports.conf`，下次面板操作会被重新生成覆盖。

## 常用命令

```bash
docker compose up -d --build
docker compose logs -f
docker compose restart
docker compose down
```

如果需要进入容器检查：

```bash
docker exec -it nginx-forward-panel bash
nginx -t
cat /etc/nginx/streams-enabled/ports.conf
```

查看数据库备份：

```bash
python3 ./scripts/backup_db.py --db-path ./data/panel.db --backup-dir ./backups
ls -lh ./backups
tail -f ./logs/nginx-forward-panel-db-backup.log
```

## 后续可扩展方向

- 增加 API 接口而不只依赖表单页面
- 为每个端口增加创建人或租户字段
- 支持导出统计报表
- 支持 UDP 转发开关
- 支持更细粒度的认证和操作审计
