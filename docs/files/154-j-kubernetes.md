# 第 154/208 课：Kubernetes 服务生命周期

> 序号 154/208 · id `j-kubernetes` · 领域 J · 阶段 4 · 难度 深入 · 子类 交付与安全
> 前置：[`j-container`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/153.html) 和 [`c-cgroup`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/47.html)
> 节点摘要：Kubernetes 以声明式对象管理副本、探针、滚动升级与资源限制。

## 导读

容器只解决「一个进程怎么打包」。服务上线还要回答：两个副本如何轮流更新？进程还没准备好时要不要杀它？依赖抖动时会不会重启风暴？在途请求怎么收尾？

教学假设：把一个教学 HTTP 服务以两副本 Deployment 发布。以下名称、镜像和限额都是教学设计，不代表腾讯或微信内部集群。

**本机事实：** 没有 `kubectl`，也没有可用集群（kind / k3s / minikube 均未安装）。因此**不再**用 Python 进程模拟 Pod 来充当 kube-apiserver 验收。实践包的主产物是 `manifests/` 与 `scripts/rollout-e2e.sh`。本机验收清单结构和教学进程自身的就绪/终止契约，并明确列出未在真实集群上验证的项。`is_live_cluster=false`，`python_pod_simulator_used_as_kube_acceptance=false`。

## 学习目标

1. 解释 readiness / liveness / startup 及适用边界
2. 独立完成实践：部署两副本服务，滚动升级时持续请求并统计失败。

## 为什么后端要懂

自动编排降低操作成本，也把失败模式从「机器宕了」变成「探针写错导致重启风暴」或「滚动时摘流过晚导致在途失败」。后端需要读懂自己写进清单的字段，而不是只把镜像交给平台。

## 核心知识点

### 1. readiness / liveness / startup

**直觉。** 还没准备好，应该摘掉流量，而不一定要杀死进程；已经死锁，才需要重启；启动很慢时，先不要用 liveness 去「催」。把「依赖系统暂时不可用」写成 liveness，会把一次抖动放大成反复重启。

**原理。** 2026-11-15 抓取 Kubernetes *Pod Lifecycle*（`https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/`，Date 2026-11-15 22:58:53 CST，608435 字节，未提供 Last-Modified）。文档区分三类探针：Startup 确认容器内应用是否已启动，成功之前不执行 liveness 或 readiness，失败则按重启策略处理该容器；Liveness 决定何时重启容器，例如陷入死锁但仍在运行的进程；Readiness 决定何时把 Pod 加入 Service 的 endpoints，失败时摘流但**不一定**重启。这正是节点验收「解释 readiness 失败为何不一定重启」要落实的官方区分。

本课主清单把三条探针指到**不同路径**：

```yaml
startupProbe:
  httpGet: { path: /startupz, port: http }
  failureThreshold: 12
  periodSeconds: 2
readinessProbe:
  httpGet: { path: /readyz, port: http }
  periodSeconds: 2
  failureThreshold: 3
livenessProbe:
  httpGet: { path: /healthz, port: http }
  periodSeconds: 5
  failureThreshold: 3
```

`app/server.py` 的实现约定：

- `/startupz`、`/readyz`：启动延迟内返回 503；收到 SIGTERM 后 `/readyz` 变为 503（`reason=draining`），以便 kubelet 把它从 endpoints 摘掉。
- `/healthz`：**故意不检查依赖、也不因 draining 失败**。它只表示「这个进程还活着」。
- `/work` 在 draining 或未就绪时拒绝。

本机应用级实验（明确标记 `scope=local_process_only_not_kubelet`）：进程在 SIGTERM 后仍短暂存活，`/readyz` 为 503，随后以退出码 0 结束。这验证的是**应用程序契约**，不是「kubelet 看到 readiness 失败不会重启」。后者必须在真实集群里用 `kubectl get pod -o yaml` 看 `restartCount` 与 endpoints，本机未做。

反例见 `manifests/bad-liveness-on-dependency.yaml`：liveness 指向 `/dependency-health`，且缺少 readiness、资源和 `terminationGracePeriodSeconds`。静态检查要求这份反例可被识别，主清单不得使用同一路径。

### 2. requests / limits

**直觉。** YAML 里写了 `limits.memory: 128Mi`，并不等于本机已经用 cgroup 卡住了进程。限额由 kubelet 落到容器运行时之后才生效。

**原理。** `resources.requests` 影响调度与超卖，`limits` 才是硬顶（具体是否转化为 cgroup `memory.max` / CPU quota 取决于版本与运行时）。本课主清单声明：

```yaml
resources:
  requests: { cpu: 100m, memory: 64Mi }
  limits:   { cpu: 200m, memory: 128Mi }
```

静态检查要求两套字段都在。`resource_limits` **本机未强制执行**，结果里写明 `kubelet_enforced_requests_and_limits` 未验证。

### 3. 优雅终止与滚动升级

**直觉。** 滚动升级不是「先杀掉再启动」，而是「先有新副本 ready，再摘旧副本；摘之前要让 endpoints 消失，并给在途请求收尾时间」。

**原理。** 本课 `replicas: 2`，`rollingUpdate.maxUnavailable: 0`、`maxSurge: 1`，表示更新期间希望保持可用副本数，额外再允许一个新 Pod。`terminationGracePeriodSeconds: 15` 与 `lifecycle.preStop`（`sleep 2`）给摘流和在途请求留窗口。节点验收「终止期间完成或拒绝在途请求」依赖 kubelet 的终止顺序：先执行 preStop，再 SIGTERM，到期 SIGKILL。本机只验证了应用在 SIGTERM 后拒绝新 `/work` 并退出 0，**没有**统计真实滚动中的失败率。

有集群时的路径（`scripts/rollout-e2e.sh` / `diagnose.sh`）：

```bash
kubectl apply -f manifests/service.yaml -f manifests/deployment.yaml
kubectl rollout status deployment/atlas-kv --timeout=180s
# 另开一个循环：while true; do curl -sS --max-time 1 http://<svc>/work; done
kubectl apply -f manifests/deployment-v2.yaml
kubectl rollout status deployment/atlas-kv
kubectl rollout history deployment/atlas-kv
```

把连续请求的成功/失败次数记下来，才算完成节点实践「滚动升级时持续请求并统计失败」。本机因 `NO_KUBECTL` 将此项列为未验证。

## 常见误区

- 用依赖服务可用性作为 liveness，导致重启风暴。
- 把 Python 模拟控制器的「重启计数」写成 Kubernetes 行为。
- 以为清单里写了 limits 就已经在本机生效。
- readiness 与 liveness 共用同一条「依赖是否可达」的路径。

## 设计权衡

`maxUnavailable: 0` 更稳，但需要更多瞬时容量（`maxSurge`）。更长的 `terminationGracePeriodSeconds` 减少在途失败，却拖慢滚动。严格的 probes 提高安全，也让启动慢或依赖抖动的服务更难变 Ready。

## 动手实践

```bash
cd practice154
bash test.sh
# 阅读 results.json 的 verified_on_this_machine / not_verified_on_this_machine
# 有集群时再跑 scripts/rollout-e2e.sh，不要用旧版 pod.py 充当验收
```

本机一次运行：清单静态检查通过（两副本，路径分别为 `/startupz` `/readyz` `/healthz`，grace=15）；应用在 SIGTERM 后 `/readyz=503`、退出 0；`cluster_e2e_verified=false`，原因 `NO_KUBECTL`。

## 源码与文档阅读

1. `manifests/deployment.yaml` 与 `deployment-v2.yaml`：对照滚动字段和探针。
2. `manifests/bad-liveness-on-dependency.yaml`：反例。
3. `app/server.py`：三条探针和 draining。
4. `static_check.py`、`app_contract.py`、`scripts/rollout-e2e.sh`。

## 自测题

**1. 为什么 readiness 失败不一定重启？liveness 失败呢？**

答案：Readiness 只影响 Service endpoints，用来摘流；Liveness 失败由 kubelet 按重启策略重启容器。这是官方 Pod Lifecycle 的分工，不是应用自己「决定重启」。

**2. 把 liveness 指到依赖的健康检查，最可能看到什么？**

答案：依赖抖动时本进程被反复重启，形成风暴。主清单禁止 liveness 使用 `/readyz` 或 `/dependency-health`。

**3. 本机 `app_contract` 通过了，能否声称「滚动升级零失败已验证」？**

答案：不能。那只验证了单进程的摘流契约。滚动失败率、endpoints 变化和 kubelet 重启计数必须在真实集群上统计。

## 推荐资源

| 资源 | 字节 | 抓取时间 (CST) | Last-Modified | 说明 |
| --- | --- | --- | --- | --- |
| [Kubernetes — Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) | 608435 | 2026-11-15 22:58:53 | （响应未提供） | 核对三类探针与终止顺序。英文，深入，免费。 |

## 本课小结

三类探针解决的问题不同：启动、摘流、重启。两副本滚动依赖就绪后再替换，以及足够的终止宽限。本机验证了清单与进程契约，没有验证 kube-apiserver、endpoints 或 kubelet 限额；有集群时必须跑 `rollout-e2e.sh`。
