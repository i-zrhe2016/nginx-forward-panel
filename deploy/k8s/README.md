# Kubernetes 部署方案

这套项目在 Kubernetes 里最稳妥的落地方式，不是做成普通的多副本无状态服务，而是部署成一套运行在**单个边缘节点**上的 `hostNetwork` 工作负载。

原因很直接：

- `xray-routing-panel` 会根据数据库里的端口规则，动态让 Nginx `stream` 监听宿主机 TCP 端口
- 这些端口不是预先固定好的 `Service/NodePort` 清单，属于运行时动态变化
- 面板默认把入口端口转发到本机 `127.0.0.1:443`
- `xray-ai-domain-manager` 会动态改写 `runtime/config.json`，并在配置变化后重启 Xray

这几件事都决定了它更像一套“节点级 TCP 入口控制面”，而不是传统的 `Service + Ingress` 应用。

## 推荐拓扑

推荐部署形态：

- 一个 `StatefulSet`
- 同一个 Pod 内放三个容器：
  - `xray-routing-panel`
  - `xray-reality`
  - `xray-ai-domain-manager`
- Pod 使用：
  - `hostNetwork: true`
  - `shareProcessNamespace: true`
  - 固定 `nodeSelector` 到专用节点
- 一个独立 `CronJob` 做 SQLite 备份

这样设计有几个好处：

- `panel -> 127.0.0.1:443` 这条链路天然成立
- `ai-domain-manager` 可以在同 Pod 内通过 `XRAY_RESTART_COMMAND` 杀掉 Xray 进程，让 kubelet 自动拉起，无需 Docker socket
- `runtime/`、`logs/`、`reports/` 可以通过共享 PVC 在三个容器之间直接复用

## 不建议的部署方式

不建议一开始就做成下面这些形态：

- 多副本 Deployment
- 跨节点拆分 `panel` 和 `xray`
- 依赖 `NodePort/LoadBalancer` 暴露动态 TCP 端口
- 继续使用 `docker.sock` 在 K8s 里重启 Xray

这些方式要么和动态端口模型冲突，要么会把重启链路复杂化。

## 方案边界

这份方案默认前提：

- 集群是 `k3s` / `RKE2` / 单机 `kubeadm` 这类边缘型集群
- 你接受这套服务绑定到一个固定节点
- 你使用本机磁盘型 `StorageClass`，例如 `local-path`
- AI 域名分类在 K8s 里默认优先走 `OPENAI_API_KEY`

关于 Codex：

- 当前 Docker Compose 版本会把宿主机 `~/.codex` 和 Node 运行时挂进 `ai-domain-manager`
- 在 Kubernetes 里，这个模式不适合作为默认方案
- 所以示例清单里把 `CODEX_CLASSIFIER_ENABLED` 默认设为 `0`
- 如果你确实要在 K8s 里继续用 Codex，需要自定义 `ai-manager` 镜像和运行环境

## 资源清单

本目录提供这些文件：

- `namespace.yaml`
- `configmap-app.yaml`
- `secret-runtime.example.yaml`
- `pvc.yaml`
- `service.yaml`
- `statefulset.yaml`
- `cronjob-db-backup.yaml`
- `kustomization.yaml`

## 镜像构建

先把两个业务镜像打包并推到你的镜像仓库：

```bash
docker build -t ghcr.io/your-org/xray-routing-panel:latest .
docker build -t ghcr.io/your-org/xray-routing-panel-ai-manager:latest -f deploy/xray-reality/Dockerfile.ai-manager deploy/xray-reality

docker push ghcr.io/your-org/xray-routing-panel:latest
docker push ghcr.io/your-org/xray-routing-panel-ai-manager:latest
```

说明：

- 主面板镜像已经自带 `app/`、`scripts/`、`nginx.conf`
- `ai-manager` 镜像现在也会把 `deploy/xray-reality/` 脚本打进镜像，不再依赖本机 bind mount

## 节点准备

给目标节点打标签：

```bash
kubectl label node <your-node> xray-routing-panel/edge=true
```

如果这台节点还承载其他关键业务，建议额外配一组 taint / toleration，把入口流量和其他工作负载隔离开。

## 配置准备

1. 复制 `secret-runtime.example.yaml`
2. 填写：
   - `XRAY_PUBLIC_HOST`
   - `XRAY_CLIENT_UUID`
   - `XRAY_REALITY_PRIVATE_KEY`
   - `XRAY_REALITY_PUBLIC_KEY`
   - `XRAY_REALITY_SHORT_ID`
   - `XRAY_SERVER_NAME`
   - `XRAY_DEST`
   - `AI_UPSTREAM_HOST`
   - `AI_UPSTREAM_PORT`
   - `OPENAI_API_KEY`
   - `ai-proxy-outbound.json` 里的 Reality 出站参数
3. 修改 `configmap-app.yaml` 里的：
   - `PANEL_PUBLIC_URL`
   - `SEED_LISTEN_PORT`
   - 探针参数
   - AI 分类参数
4. 按你的集群实际情况调整 `pvc.yaml` 里的 `storageClassName`

## 部署顺序

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/pvc.yaml
kubectl apply -f deploy/k8s/configmap-app.yaml
kubectl apply -f deploy/k8s/secret-runtime.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/statefulset.yaml
kubectl apply -f deploy/k8s/cronjob-db-backup.yaml
```

如果你使用 `kustomize`：

```bash
kubectl apply -k deploy/k8s
```

注意：

- `kustomization.yaml` 默认不包含示例 secret
- 你需要把 `secret-runtime.example.yaml` 复制成 `secret-runtime.yaml` 后再 apply

## 持久化映射

Docker Compose 到 Kubernetes 的主要映射关系如下：

| Compose 路径 | K8s 方案 |
| --- | --- |
| `./data` | `panel-data-pvc` |
| `./logs` | `panel-logs-pvc` |
| `./backups` | `panel-backups-pvc` |
| `./deploy/xray-reality/runtime` | `xray-workspace-pvc` 的 `runtime/` 子目录 |
| `./deploy/xray-reality/logs` | `xray-workspace-pvc` 的 `logs/` 子目录 |
| `./deploy/xray-reality/reports` | `xray-workspace-pvc` 的 `reports/` 子目录 |
| `deploy/xray-reality/.env` | Secret 挂载到 `/workspace/.env` |
| `deploy/xray-reality/ai-proxy-outbound.json` | Secret 挂载到 `/workspace/ai-proxy-outbound.json` |

## 初始启动流程

`StatefulSet` 里有两个 initContainer：

1. `init-workspace`
   - 创建 `runtime/`、`logs/`、`reports/`
2. `render-xray-config`
   - 调用 `render_config.py`
   - 生成：
     - `runtime/config.json`
     - `runtime/client-test.json`
     - `runtime/client-share.txt`

这样 Xray 主容器第一次启动时，就已经有完整配置，不需要等 `ai-domain-manager` 先跑一轮。

## 配置变更和重启逻辑

在这套 K8s 方案里，`ai-domain-manager` 使用：

```text
XRAY_RESTART_COMMAND=kill $(pidof xray)
```

因为 Pod 开启了 `shareProcessNamespace: true`：

- `ai-domain-manager` 可以看到同 Pod 的 `xray` 进程
- 当 `runtime/config.json` 变化时，管理器会执行这条命令
- `xray` 主进程退出后，kubelet 会自动拉起该容器

这比在 K8s 里挂 `docker.sock` 或额外调用 `kubectl delete pod` 更干净。

## 验证方法

### 1. 看 Pod 状态

```bash
kubectl -n xray-routing-panel get pods -o wide
```

### 2. 看面板健康检查

```bash
kubectl -n xray-routing-panel exec -it xray-routing-panel-0 -c xray-routing-panel -- \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18080/healthz').read().decode())"
```

### 3. 看 Xray 配置是否有效

```bash
kubectl -n xray-routing-panel exec -it xray-routing-panel-0 -c xray-reality -- \
  /usr/local/bin/xray run -test -config /xray-runtime/config.json
```

### 4. 触发一次 AI 域名管理器

```bash
kubectl -n xray-routing-panel exec -it xray-routing-panel-0 -c xray-ai-domain-manager -- \
  python /workspace/scripts/ai_domain_manager.py --once
```

### 5. 看最近一小时报告

```bash
kubectl -n xray-routing-panel exec -it xray-routing-panel-0 -c xray-ai-domain-manager -- \
  cat /workspace/reports/hourly-domains/latest.txt
```

## 对外暴露方式

这套方案依赖 `hostNetwork`，所以：

- 面板默认直接暴露在 `节点IP:18080`
- Xray 默认直接监听 `节点IP:443`
- 面板动态创建的 TCP 入口端口也直接监听在节点上

这意味着：

- 不需要为这些动态端口创建 K8s `Service`
- 也不适合通过 Ingress/Nginx Ingress 去管理这些动态 L4 端口

如果你需要统一接入层，建议在集群外再放一层固定的四层负载均衡器，把公网流量导到这个专用节点。

## 资源建议

起步建议：

- `xray-routing-panel`
  - `100m / 256Mi`
- `xray-reality`
  - `200m / 256Mi`
- `xray-ai-domain-manager`
  - `200m / 512Mi`

如果 AI 域名分类大量依赖 OpenAI API，或者观察窗口内域名很多，`ai-manager` 的 CPU / 内存可以适当上调。

## 运维注意点

### 1. `ulimit -n`

Kubernetes 不能像 Docker Compose 那样直接在 manifest 里设置 `nofile`。

这套项目如果连接数上来，建议节点运行时默认 `nofile` 至少满足：

- `65535`

如果你的 container runtime 默认值太低，需要在节点侧调整 `containerd` / `kubelet` 运行环境。

### 2. `sysctl`

项目当前会受这些内核参数影响：

- `net.ipv4.tcp_fastopen`
- `net.ipv4.tcp_slow_start_after_idle`
- `net.core.somaxconn`
- `net.ipv4.tcp_max_syn_backlog`

如果你沿用仓库当前的低延迟调优，建议节点侧至少保证：

- `net.ipv4.tcp_fastopen=3`
- `net.ipv4.tcp_slow_start_after_idle=0`

### 3. 磁盘空间

`xray-reality` 和 `ai-domain-manager` 都依赖本地持久化文件：

- `access.log`
- `runtime/config.json`
- `reports/hourly-domains/*`
- `panel.db`

节点磁盘满了会直接影响：

- Xray 容器重启
- 配置渲染
- SQLite 备份

所以 PVC 所在磁盘必须有稳定余量。

## 什么时候要重构，而不是直接上 K8s

如果你的目标是下面这些之一，这份方案就不是终点，而只是过渡：

- 多节点横向扩容
- 每个租户独立入口节点
- 通过 CRD / Operator 管理端口规则
- 动态端口也走 K8s Service / LoadBalancer

那就应该考虑把“端口规则存 SQLite -> 容器里写 Nginx stream 配置”这层，重构成 Kubernetes 原生控制器。
