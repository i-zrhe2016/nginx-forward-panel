# 面板迁移文档

本文档用于把当前这套 `xray-routing-panel` 从旧机器迁移到新机器。

## 迁移目标

- 保留现有端口规则
- 保留面板配置
- 可选保留历史流量统计
- 迁移后自动恢复 Nginx 转发

## 需要迁移的内容

最关键的是这两个目录：

- `data/panel.db`
- `logs/stream-access.log`

如果你想把配置也完全一致，建议一并保留：

- `docker-compose.yml`
- `nginx.conf`
- 你手动改过的环境变量

不要迁移生成文件：

- `/etc/nginx/streams-enabled/ports.conf`

这个文件会在启动时自动重建。

## 迁移方式

### 方案 A：规则 + 统计一起迁

适合需要保留累计连接数、总流量、今日流量的场景。

### 方案 B：只迁规则

只复制 `panel.db` 即可，历史统计可重新开始。

## 迁移步骤

### 1. 停掉旧面板

```bash
docker compose down
```

建议先停服务，再做备份，避免数据库和日志不同步。

### 2. 备份旧数据

```bash
tar -czf xray-routing-panel-backup.tar.gz data logs docker-compose.yml nginx.conf
```

如果你没有改过 `docker-compose.yml` 或 `nginx.conf`，也可以只备份 `data` 和 `logs`。

### 3. 在新机器准备环境

- 拉取同一份项目代码
- 安装 Docker 和 Docker Compose
- 确认监听端口未被占用
- 确认 `PANEL_PORT`、`DEFAULT_UPSTREAM_HOST`、`DEFAULT_UPSTREAM_PORT` 等配置和旧机一致

### 4. 恢复数据

```bash
tar -xzf xray-routing-panel-backup.tar.gz
```

如果只迁规则，把旧机器的 `panel.db` 复制到新机器的 `./data/panel.db`。

### 5. 启动新面板

```bash
docker compose up -d --build
```

### 6. 验证

```bash
docker compose ps
docker compose logs -f
curl http://127.0.0.1:18080/healthz
```

也可以检查生成的转发配置：

```bash
docker exec -it xray-routing-panel cat /etc/nginx/streams-enabled/ports.conf
```

## 迁移后检查项

- 面板能正常打开
- 端口列表和旧机器一致
- 端口启停正常
- 统计数据是否符合预期
- 目标端口在新机器上没有冲突

## 回滚方法

如果新机器验证失败：

```bash
docker compose down
```

然后把旧机器的数据目录恢复回来，重新启动旧环境即可。

## 常见问题

- 如果只复制了 `panel.db`，但没复制 `stream-access.log`，历史统计会重置。
- 如果复制了 `panel.db` 和 `stream-access.log`，建议一定先停旧服务，再打包。
- `ports.conf` 不需要手工迁移，启动后会自动生成。
- 如果新机器上端口已被占用，先改监听端口再启动。
