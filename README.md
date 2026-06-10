# nginx-forward-panel

`nginx-forward-panel` 当前更适合被理解成一套“AI routing 控制面 + Xray REALITY 数据面”的组合部署，而不只是一个 TCP 端口面板。

这套仓库里的核心链路是：

- `nginx-forward-panel` 用 Nginx `stream` 管理入口端口，把公网 TCP 入口稳定转发到本机 Xray
- `xray-reality` 负责实际代理连接和路由命中
- `xray-ai-domain-manager` 按小时分析 Xray 访问日志，识别 AI 域名并生成动态路由片段
- 命中的 AI 域名自动改走 `AI_UPSTREAM_HOST:AI_UPSTREAM_PORT`
- 面板数据库 `data/panel.db` 同时保存端口规则、流量统计和 AI 域名聚合结果

如果不启用 `xray` profile，它仍然可以单独作为轻量 TCP 转发面板使用；但仓库当前最有价值的部分，是这条基于真实访问日志做动态 AI 分流的链路。

## AI Routing 能做什么

- 按真实访问域名把 AI 流量和普通流量分开处理
- 优先使用本机 `codex` 分类未知域名，必要时回退到 OpenAI Responses API
- 把 AI 域名分类结果缓存到 `runtime/ai-domain-decisions.json`
- 把可直接注入 Xray 的动态规则写到 `runtime/dynamic-routing.json`
- 在共享 `panel.db` 中维护 `ai_domains`、`ai_domain_observations`，方便面板和脚本复用
- 输出按小时归档的文本和 JSON 报告，便于审计最近一小时命中了哪些 AI 域名
- 仍然保留面板原有的端口管理、订阅链接、流量统计、到期停用和备份能力

## 适合场景

- 你有一个固定入口端口，希望把 AI 相关目标站点自动切到专用上游
- 你想根据真实访问日志持续收敛 AI 域名名单，而不是手工维护一长串规则
- 你想优先依赖本机 Codex 做分类，只在需要时才使用 OpenAI API
- 你仍然需要一个简单的 Web 面板来管理入口端口、订阅链接和流量上限

## 适用边界

- AI routing 只有在 `docker compose --profile xray ...` 启动后才会生效
- 当前只处理 TCP 链路，不做 HTTP 反代，不做 UDP 分流
- 面板本身仍是“单一固定上游”的 `stream` 管理器；AI 分流发生在后面的 Xray 路由层
- 域名分类是按窗口批处理，默认每 `3600` 秒统计最近 `3600` 秒访问，不是逐请求实时判定
- 没有本机 `codex` 且没有 `OPENAI_API_KEY` 时，只有内建已知 AI 域名会被自动识别，其他未知域名不会自动标成 AI

## AI Routing 工作原理

默认链路可以概括成：

```text
client
  -> nginx stream listen port
  -> local xray-reality
  -> access.log
  -> xray-ai-domain-manager
  -> codex / openai classify
  -> dynamic-routing.json
  -> AI domains => ai_proxy
  -> other domains => default route
```

运行时主要步骤如下：

1. 面板根据 `ports` 表生成 Nginx `stream` 配置，把入口端口转到统一上游，默认是本机 `127.0.0.1:443`
2. `xray-reality` 处理真实代理流量，并把目标域名写入 `deploy/xray-reality/logs/access.log`
3. `xray-ai-domain-manager` 读取最近一小时日志，先走内建强制 AI 路由名单和已知 AI 域名名单
4. 对剩余未知域名，优先调用本机 `codex`；如果不可用且设置了 `OPENAI_API_KEY`，再回退到 OpenAI Responses API
5. AI 域名结果写入：
   - `deploy/xray-reality/runtime/ai-domain-decisions.json`
   - `deploy/xray-reality/runtime/dynamic-routing.json`
   - `deploy/xray-reality/reports/hourly-domains/latest.{txt,json}`
   - `data/panel.db` 中的 `ai_domains`、`ai_domain_observations`
6. 动态路由片段变化后，管理器会重新渲染 Xray 配置，并在需要时重启 `xray-reality`

## 快速开始

推荐直接使用仓库根目录的 `docker-compose.yml` 启动完整 AI routing 栈。

### 1. 启动前准备

- Linux 宿主机
- 已安装 Docker 和 Docker Compose
- 宿主机上确认这些端口没有冲突：
  - `443`，Xray 默认监听端口
  - `18080`，面板端口
  - `31098`，仓库当前默认入口端口和探针观察端口
- 如果你想让未知域名自动分类，至少满足下面一项：
  - 宿主机已安装 `codex` CLI，且 `codex login status` 可用
  - 设置可用的 `OPENAI_API_KEY`

### 2. 准备 Xray REALITY 参数

```bash
./deploy/xray-reality/scripts/generate-secrets.sh
cp deploy/xray-reality/.env.example deploy/xray-reality/.env
```

至少需要修改：

- `XRAY_PUBLIC_HOST`
- `XRAY_CLIENT_UUID`
- `XRAY_REALITY_PRIVATE_KEY`
- `XRAY_REALITY_PUBLIC_KEY`
- `XRAY_REALITY_SHORT_ID`
- `XRAY_SERVER_NAME`
- `XRAY_DEST`

如果你希望通过面板暴露的入口端口接入，例如公网访问 `31098` 再转到本机 Xray `443`：

- 保持 `XRAY_LISTEN_PORT=443`
- 额外设置 `XRAY_PUBLIC_PORT=31098`

### 3. 渲染配置并启动完整栈

```bash
python3 deploy/xray-reality/scripts/render_config.py
docker compose --profile xray up -d --build
```

这条命令会启动：

- `nginx-forward-panel`
- `nginx-forward-panel-db-backup`
- `xray-reality`
- `xray-ai-domain-manager`

默认配置下：

- 面板地址：`http://服务器IP:18080`
- 探针监控页：`http://服务器IP:18080/probe-dashboard`
- 健康检查：`http://服务器IP:18080/healthz`
- Xray 监听：`0.0.0.0:443`
- 面板默认把入口端口转发到 `127.0.0.1:443`
- 数据库持久化到 `./data`
- 面板日志持久化到 `./logs`
- Xray 日志持久化到 `./deploy/xray-reality/logs`

仓库当前 `docker-compose.yml` 还带了一个示例值：

- `PANEL_PUBLIC_URL=http://64.186.224.96:18080`

正式部署前建议改成你自己的公网地址，否则页面里的面板地址和订阅链接会继续指向这个示例 URL。

### 4. 验证 AI routing 是否已经生效

默认按一小时窗口执行。想立刻跑一轮分类，可以手动执行：

```bash
docker compose --profile xray run --rm xray-ai-domain-manager python /workspace/scripts/ai_domain_manager.py --once
```

常用检查方式：

```bash
docker compose --profile xray logs -f xray-ai-domain-manager
cat deploy/xray-reality/reports/hourly-domains/latest.txt
sed -n '1,220p' deploy/xray-reality/reports/hourly-domains/latest.json
```

检查共享数据库中的 AI 聚合结果：

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('./data/panel.db')
for row in conn.execute('select domain, classification, total_hits from ai_domains order by domain'):
    print(row)
PY
```

如果 `latest.txt` 里出现类似下面的内容，说明动态规则已经生成：

```text
route_status: applied
```

### 5. 停止

```bash
docker compose --profile xray down
```

### 6. 如果只想使用面板

```bash
docker compose up -d --build
```

这种模式下只会启动：

- `nginx-forward-panel`
- `nginx-forward-panel-db-backup`

AI routing 相关的 `xray-reality` 和 `xray-ai-domain-manager` 不会启动。

## AI Routing 组件与产物

AI routing 相关内容主要集中在：

- `deploy/xray-reality/.env`：REALITY 参数和 AI 管理器运行参数
- `deploy/xray-reality/runtime/config.json`：渲染后的 Xray 服务端配置
- `deploy/xray-reality/runtime/client-share.txt`：客户端导入链接
- `deploy/xray-reality/runtime/client-test.json`：本地验证用客户端配置
- `deploy/xray-reality/runtime/ai-domain-decisions.json`：域名分类缓存
- `deploy/xray-reality/runtime/dynamic-routing.json`：动态路由片段
- `deploy/xray-reality/reports/hourly-domains/`：最近一小时和历史归档报告
- `deploy/xray-reality/logs/access.log`：AI 管理器的主要输入日志
- `data/panel.db`：共享数据库，保存端口规则、流量和 AI 域名聚合数据

更完整的 REALITY 和 AI 域名管理说明见：

- [deploy/xray-reality/README.md](deploy/xray-reality/README.md)

## 页面使用

面板首页可以直接完成这些操作：

- 新增监听端口
- 默认固定转发到 `127.0.0.1:443`
- 查看每个端口专属的 `V2Ray / Clash` 订阅链接
- 默认订阅路径是 `/<token>/<listen_port>`，当前返回 Clash 配置；显式路径还有 `/<token>/<listen_port>/clash` 和 `/<token>/<listen_port>/v2ray`
- 设置到期时间
- 设置流量上限，例如 `10G`、`500MB`、`1048576`
- 编辑备注
- 手动启用或停用端口
- 删除端口
- 在启用探针时跳转到独立监控页查看后端连通性

状态说明：

- `运行中`：端口启用，且未过期、未达到流量上限
- `已停用`：手动停用
- `已过期`：到达过期时间后自动停用
- `已达流量上限`：累计收发流量超过上限后自动停用

流量重置说明：

- 对 `已达流量上限` 的端口，页面会显示“重置流量并启用”
- 该操作会清零该端口的累计流量和按天流量统计
- 累计连接次数不会被重置

## 探针监控

当 `PROBE_ENABLED=1` 时，服务会在后台定时对后端目标做 TCP 连通性探测，并提供独立监控页：

- 页面地址：`/probe-dashboard`
- 可选时间窗口：`1h`、`24h`、`7d`
- 默认行为：如果设置了 `PROBE_TEST_LISTEN_PORT`，监控页固定展示该监听端口；否则自动选第一条已启用端口
- 记录内容：最近状态、成功率、最近 12 次检测结果、最近 7 天内的历史时间线

这个页面只用来观察“固定上游是否可达”，不替代业务级可用性监控。

## 环境变量

下表是程序默认值。使用仓库自带的 `docker-compose.yml` 时，部分值会被覆盖，最常见的是：

- `PROBE_ENABLED=1`
- `PROBE_INTERVAL=180`
- `PROBE_TEST_LISTEN_PORT=31098`

常用环境变量如下。

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `PANEL_HOST` | `0.0.0.0` | Flask 面板监听地址 |
| `PANEL_PORT` | `18080` | Flask 面板监听端口 |
| `PANEL_PUBLIC_URL` | 空 | 面板对外展示地址；设置后页面中的“面板地址”和生成的订阅链接会固定使用这个公开 URL |
| `PANEL_USERNAME` | 空 | Basic Auth 用户名，留空表示不开启认证 |
| `PANEL_PASSWORD` | 空 | Basic Auth 密码 |
| `DEFAULT_UPSTREAM_HOST` | `127.0.0.1` | 统一转发目标主机，默认是本机回环地址 |
| `DEFAULT_UPSTREAM_PORT` | `443` | 统一转发目标端口 |
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
| `AI_UPSTREAM_HOST` | `nat.qq.pw` | 命中 AI 域名后转发到的专用上游主机 |
| `AI_UPSTREAM_PORT` | `31098` | 命中 AI 域名后转发到的专用上游端口 |
| `AI_DOMAIN_INTERVAL_SECONDS` | `3600` | AI 域名管理器执行周期 |
| `AI_DOMAIN_LOOKBACK_SECONDS` | `3600` | 每轮分析最近多少秒的访问日志 |
| `AI_DOMAIN_BATCH_SIZE` | `50` | 单批送给分类器的最大域名数 |
| `CODEX_CLASSIFIER_ENABLED` | `1` | 是否优先启用本机 `codex` 做未知域名分类 |
| `CODEX_TIMEOUT_SECONDS` | `180` | 调用本机 `codex` 的超时时间 |
| `OPENAI_API_KEY` | 空 | 本机 `codex` 不可用时的 OpenAI 回退凭据 |
| `OPENAI_MODEL` | `gpt-5.5` | OpenAI 回退分类时使用的模型 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1/responses` | OpenAI Responses API 地址 |
| `PANEL_ROUTE_LISTEN_PORT` | `0` | 可选；指定某个面板监听端口作为模板里 `__PANEL_*__` 占位符的优先来源 |

代码里还有一些偏内部用途的路径变量，例如：

- `NGINX_CONFIG_PATH`
- `STREAMS_DIR`
- `STREAM_ACCESS_LOG`
- `NGINX_PID_PATH`
- `XRAY_CLIENT_CONFIG_PATH`

一般不需要改。

## 目录结构

```text
.
├── app/
│   ├── panel.py              # Flask 应用和 Nginx 管理逻辑
│   ├── templates/index.html  # 面板页面
│   ├── templates/probe_dashboard.html
│   └── static/style.css      # 页面样式
├── backups/                  # SQLite 备份输出目录
├── data/
│   └── panel.db              # SQLite 数据库
├── deploy/
│   └── xray-reality/
│       ├── README.md         # Xray REALITY 和 AI 域名管理详细说明
│       ├── logs/             # Xray 访问日志，AI 管理器从这里读入域名
│       ├── reports/          # 按小时输出 AI 域名报告
│       ├── runtime/          # 渲染配置、分类缓存、动态路由片段
│       └── scripts/          # REALITY 渲染和 AI 域名管理脚本
├── logs/
│   ├── error.log             # Nginx 错误日志
│   └── stream-access.log     # Nginx stream 访问日志
├── PANEL_MIGRATION.md        # 迁移文档
├── docker-compose.yml
├── Dockerfile
├── nginx.conf
├── scripts/
│   ├── backup_db.py          # SQLite 备份脚本
│   └── start-backup-cron.sh  # 备份定时任务启动脚本
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

如果启用了 `xray` profile 中的 AI 域名管理服务，还会新增：

- `ai_domains`：当前 AI 域名聚合状态
- `ai_domain_observations`：按统计窗口保存的 AI 域名命中历史

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

## 数据备份与迁移

- 根目录 `docker-compose.yml` 默认会启动 `nginx-forward-panel-db-backup`，按 `DB_BACKUP_CRON_SCHEDULE` 定时备份 `panel.db`
- 默认备份目录是 `./backups`
- 手动备份可执行：

```bash
python3 ./scripts/backup_db.py --db-path ./data/panel.db --backup-dir ./backups
```

- 迁移到新机器时，核心数据通常是：
  - `data/panel.db`
  - `logs/stream-access.log`
- 完整迁移步骤见：[PANEL_MIGRATION.md](PANEL_MIGRATION.md)

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
