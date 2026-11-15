# 第 154/208 课：Kubernetes 服务生命周期

> 序号 154/208 · id `j-kubernetes` · 领域 J · 阶段 4 · 难度 深入 · 子类 交付与安全
> 前置：[`j-container`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/153.html)
> 节点摘要：Kubernetes 通过声明式控制器协调 Pod、Service 和滚动更新；探针与资源配置影响可用性。

## 导读

上一课处理镜像内容和进程退出。多个副本上线时，还要决定何时接收流量、何时重启、滚动替换时如何处理在途请求。这些语义如果用错，一次依赖抖动就可能变成重启风暴。

教学假设：教学用两副本服务在本机用进程模拟 Pod 与 Service 端点。以下控制器、探针和滚动策略均为教学设计，不代表腾讯或微信内部实现，也没有连接 kube-apiserver。`is_live_cluster=false`。

## 学习目标

1. 解释readiness/liveness/startup及适用边界
2. 独立完成实践：部署两副本服务，滚动升级时持续请求并统计失败。

## 为什么后端要懂

节点给出的理由是「理解服务部署、扩缩容与终止语义。」副本数、探针和终止宽限共同决定用户是否还能打到健康实例，以及在途请求是被做完还是被切断。

## 核心知识点

### 1. readiness/liveness/startup

**原理。** Kubernetes *Pod Lifecycle* 文档区分三类探针：Startup 确认容器内应用是否已启动，成功之前不执行 liveness 或 readiness，失败则按重启策略处理该容器；Liveness 决定何时重启容器，例如陷入死锁但仍在运行的进程，超过容忍次数后 kubelet 重启该容器，且不等待 readiness 先成功；Readiness 决定容器何时可以接受流量，失败时 EndpointSlice 控制器会从匹配 Service 的端点中去掉该 Pod IP，从而不再把新请求打过去。

**直觉。** 还没准备好，应该摘掉流量，而不一定要杀死进程；已经死锁，才需要重启。把「依赖系统暂时不可用」写成 liveness，会把一次抖动放大成反复重启。

**实践对照。** readiness 被强制失败时：startup 已成功，liveness 仍为 200，进程还在，重启次数为 0，Service 侧没有就绪端点，`/work` 不被转发。对照地把缺失依赖挂到 liveness 上，教学控制器连续重启 3 次。这是本地进程模拟，不是 kubelet。

### 2. requests/limits

**原理。** 资源 requests 影响调度，limits 约束运行时用量。文档把它们作为 Pod spec 的一部分；没有真实 kubelet 就不能声称已执行这些限制。

**实践对照。** 结果里声明了 `requests.cpu=100m, memory=64Mi` 与相应 limits，但 `resource_limits_enforced_here=false`，与上一课无法写入 cgroup 的限制一致。

### 3. 优雅终止

**原理。** 文档说明删除 Pod 时，运行时向容器主进程发送 SIGTERM（或镜像配置的 STOPSIGNAL），并在宽限期结束后再发送 KILL。宽限期内应用可以停止接新请求、完成或拒绝在途工作。

**实践对照。** 滚动替换时，对即将退出的 v1 副本在在途请求期间发送 SIGTERM：该请求返回 200 且进程退出码 0。教学实现选择完成在途请求；也可以改为立即 503，两种都算「完成或拒绝」，但不能两者都不做就直接消失。

## 常见误区

1. **用依赖服务可用性作为 liveness 导致重启风暴。** 节点原句即此。本课在依赖文件缺失时把 liveness 判失败，连续重启 3 次。同样的条件放在 readiness 上只是摘除流量。
2. **滚动升级零失败是默认保证。** 本次 28 次请求中失败 0 次，并同时观察到 v1 与 v2，只说明这次教学滚动先 surge 再摘旧副本且宽限足够；换更短宽限或更高并发，失败计数完全可能上升。

## 设计权衡

节点原句是「自动编排降低操作成本，也增加控制面复杂性。」探针、端点和滚动策略把大量操作自动化，但也引入了「为什么这个 Pod 不接流量却没重启」这类需要对照官方语义才能回答的问题。本课用本地模拟理解语义，不能替代对真实集群控制面的排障。

## 动手实践

实践目录为 `practice154/`，需要 Python 3。本次为 CPython 3.13.5。

```bash
cd practice154
bash test.sh
cat results.json
```

脚本启动教学 Pod 进程，检查 readiness 失败与 liveness 风暴，然后做两副本滚动并持续发请求。成功最后一行为 `ALL_CHECKS_PASSED`。端口号随环境变化。

本次官方实践目录：readiness 失败时进程仍在、重启 0；liveness 依赖缺失重启 3 次；终止期间在途请求 HTTP 200；滚动统计 `ok=28, fail=0`，版本集合为 v1 与 v2。

两项验收是「解释 readiness 失败为何不一定重启」以及「终止期间完成或拒绝在途请求」。前者对应官方「从端点去掉 IP」与本课重启次数为 0；后者对应 SIGTERM 期间完成的 200 响应。

## 源码与文档阅读

阅读 `pod.py` 中三类探针与 SIGTERM 处理，对照 `results.json`。官方文档讨论的是 kubelet 与 EndpointSlice；本实验用本机进程和教学负载均衡代替，探针间隔、容忍次数和正式滚动参数都未按集群默认值复现。

2026-11-15 在本机核对节点 URL。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） |
| --- | ---: | --- | --- |
| [Kubernetes — Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)：Pod Lifecycle \| Kubernetes | 608435 | 2026-11-15 22:58:53 | （响应未提供） |

最终 URL 与节点所列相同，HTTP 200，锚点 `#container-probes` 指向同一文档。节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。

## 自测题

1. **readiness 失败为什么不一定重启？** 文档说明失败时从 Service 端点去掉 Pod IP，目的是停止派发新流量，而不是杀死容器。本课该进程仍在，liveness 仍为 200。
2. **为什么不要把依赖可用性做成 liveness？** 依赖抖动会让 kubelet 反复重启容器，形成风暴。本课在缺失依赖下连续重启 3 次。
3. **滚动时失败为 0，能否保证以后也是 0？** 不能。这只是本次宽限和并发下的计数。

## 推荐资源

- [Kubernetes — Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)：用于核对readiness/liveness/startup与requests/limits，将结论落实到本节点实验。英文，深入，免费。

## 本课小结

startup 负责初始化窗口，readiness 负责是否接流量，liveness 才决定是否重启；滚动替换时要在 SIGTERM 宽限里完成或拒绝在途请求，并把失败次数当作需要测量的量，而不是默认的零。下一课是第 155 课 j-release（灰度发布与回滚）。
