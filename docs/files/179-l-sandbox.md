# 第 179/208 课：工具沙箱与权限隔离

> 序号 179/208 · id `l-sandbox` · 领域 L · 阶段 4 · 难度 深入 · 子类 权限与结果保障
> 前置：[`l-tools`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/170.html)、[`j-container`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/153.html)、[`j-security`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/156.html)、[`c-cgroup`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/47.html)
> 节点摘要：工具沙箱要限制实际执行环境能够接触的文件、网络、进程和计算资源，而不是仅靠提示词告诉模型不要越界。

## 导读

第 170 课把工具执行交给宿主，第 153 课和第 47 课分别讨论过容器边界与 cgroup。本课把这些约束落到“Agent 要跑一条命令”的现场：工作区外的密钥不能被读到，失控的 CPU、内存和进程要被终止，超时后不能留下孤儿进程。

教学假设：后台诊断执行器允许在隔离工作区里跑白名单命令。这是为讲解沙箱分层设计的场景，不代表腾讯或微信的内部隔离方案。本机有 `/usr/bin/bwrap`，**没有** Docker、Podman、gVisor/runsc，也没有可写的 cgroup 目录（创建子 cgroup 会 Permission denied），并且不能使用 sudo。实践用真实 bubblewrap，不把 Python 模拟写成“已经验证了容器引擎”。

## 学习目标

1. 说明宿主控制面与不可信执行面的分离，以及应用白名单与 OS 边界如何分工。
2. 独立完成实践：用 bwrap 运行命令，验证工作区外凭证、逃逸符号链接、资源超限和超时清理。
3. 用负例确认：超限有明确原因；超时后无孤儿进程且工作区被销毁；并如实写出本方案未覆盖的内核攻击与外部服务风险。

## 为什么后端要懂

节点给出的理由是「让后台诊断和代码执行工具的影响范围能够被验证，而非依赖模型自律」。只读提示词、删掉危险单词或“默认的 Docker 运行”，都不是已经验证的沙箱。不可信代码还可能通过脚本、依赖钩子和子进程走到你以为没挂上的路径。

## 核心知识点

### 1. 控制面与执行面分离

**直觉。** 决定“能不能跑”的是宿主；真正跑命令的是一个更小的命名空间。

**原理。** 实践先做应用层白名单：工具名必须在允许集合里，绝对路径必须落在沙箱内的 `/workspace`。`curl` 会在启动 bwrap 之前被 `DeniedByPolicy` 拒绝；直接 `cat /tmp/host-secret-179` 也会因路径在工作区外被拒绝。然后才进入 OS 层。

### 2. 最小挂载：只读根与临时工作区

**直觉。** 没有挂上的路径，对沙箱就是不存在的路径。

**原理。** bwrap 使用 `--ro-bind` 挂上 `/usr`、`/bin`、`/lib`，**不挂** `/etc`，`/tmp` 与 `/home` 用 tmpfs 盖住，只把本次工作区绑定到 `/workspace`，并加上 `--unshare-user --unshare-pid --unshare-net --unshare-uts --die-with-parent`。主机上的 `SUPERSECRET_TOKEN` 无论是直接读还是经由工作区内的符号链接，都得到 `No such file or directory`，令牌没有泄漏。指向 `/etc/shadow` 的符号链接同样失败。Python 3.13 在不挂 `/etc` 的情况下仍能启动，这次有意保持这一最小集合——每多一次 `ro-bind`，攻击面就大一圈。

### 3. 资源上限：这里用 rlimit，不是 cgroup OOM 137

**直觉。** 超限必须停，并且原因要写对。本机做不到的引擎，不能写进“已验证”。

**原理。** bwrap 没有 `--rlimit`，实践在沙箱内的 limiter 里调用 `resource.setrlimit`：

- 内存：`RLIMIT_AS=32MiB` 下分配 128MiB，触发 Python `MemoryError`，退出码为 **1**（未捕获异常的默认码）。这不是 cgroup OOM 的 137，本课也不把它写成 137。
- CPU：`RLIMIT_CPU` 软限制 1 秒、硬限制 2 秒，子进程捕获 `SIGXCPU` 后以退出码 **24** 结束，耗时约 1.006s。
- 进程数：在新的 user namespace 里把 `RLIMIT_NPROC` 设为 8，`fork` 到第 6 个之后得到 `Resource temporarily unavailable`（EAGAIN）。`NPROC` 在该命名空间里重新计数；若在宿主用户上直接降 nproc，会被这台机器上已经存在的大量进程干扰。

2026-11-15 23:53:00（UTC+8）抓取的 [Introduction to gVisor security](https://gvisor.dev/docs/architecture_guide/intro/)（Last-Modified 2026-11-15 21:47:20，UTC+8）说明 gVisor 用用户态内核隔离系统调用。本机没有 `runsc`，所以这条文档只用于对照，不作为本实践的引擎证据。[Docker Engine security](https://docs.docker.com/engine/security/) 同日可访问（本机记录的 Date 为 2026-11-15 13:25:07，Last-Modified 2026-11-14 01:08:49，UTC+8）；普通容器共享宿主内核，文档自己也划了这条边界。本实践没有启动 Docker。

### 4. 符号链接、路径穿越、子进程树与超时清理

**直觉。** 工作区里的 `../` 或符号链接，不能成为通向宿主密钥的桥；杀掉沙箱后，里面的子进程必须一起消失。

**原理。** 路径穿越在应用层被拒绝；符号链接逃逸由“目标不在挂载树里”阻断。超时用例跑 `sleep 30`，0.4s 后杀掉 bwrap：`--die-with-parent` 加上 pid 命名空间后，进程树上没有残留的 `sleep 30`，工作区目录被销毁。取消与超时走同一条清理路径。

### 5. 凭证不落工作区；未覆盖的风险必须写明

**直觉。** 沙箱不是万能边界，没测到的攻击不能写成已经防住。

**原理。** 模拟凭证放在工作区之外，也不挂进沙箱。本课明确未覆盖：

- 内核漏洞与共享内核逃逸（没有 gVisor，也没有独立 VM）；
- Docker / Podman 引擎（本机不存在，未测）；
- 可写 cgroup 的 OOM kill（mkdir 子 cgroup 被拒绝）；
- 未安装 seccomp 过滤器；
- 若以后打开网络，远端服务在沙箱之外；
- `ro-bind` 的 `/usr` 与 `/bin` 属于被信任的供应链。

## 常见误区

把 Docker 默认运行、只读提示词或删除危险单词当成安全沙箱。把长期凭证挂进可能执行任意代码的目录。节点把这两条列为陷阱。本课如果因为没有 Docker 就用模拟层“冒充引擎通过”，会再制造一种更糟的误区，所以选择真实 bwrap 并如实写未测边界。

## 设计权衡

更强隔离通常增加启动开销和兼容性成本，应按威胁模型选择，而不是把教学白名单执行器描述成通用恶意代码沙箱。本次 bwrap 方案验证的是用户命名空间里的挂载与 rlimit，不是对内核攻击的完整答案。

## 动手实践

实践包是 `practice179.tar.gz`，需要 `/usr/bin/bwrap` 以及带 `resource` 模块的 Python：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice179.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice179"
bash test.sh
```

本次机器环境为 CPython 3.13.5、Linux 6.12 用户命名空间可用、`bwrap` 在 `/usr/bin/bwrap`。独立复跑后：工作区读成功；未知工具与区外路径被策略拒绝；直接读密钥与逃逸符号链接都不泄漏令牌；内存超限为 MemoryError（退出码 1，不是 137）；CPU 超限退出码 24；进程超限为 EAGAIN；超时后工作区销毁且无孤儿进程。未验证项见上文列表，并写在 `results.json` 的 `uncovered` 字段。

## 源码与文档阅读

gVisor 与 Docker 安全文档均在 2026-11-15（UTC+8）抓取为 200，用于说明“共享内核的容器不是对所有恶意代码都足够的隔离”。本实践的引擎是 bubblewrap，不要把这两篇文档的存在写成“本课跑过了 gVisor 或 Docker”。第 153 课在没有容器引擎时也采取了同样的诚实原则。

## 自测题

**1. 为什么工作区里指向宿主密钥的符号链接仍然读不到内容？**

答：符号链接的目标路径没有被挂进沙箱，`/tmp` 又被 tmpfs 盖住。本次 `cat /workspace/escape` 得到 `No such file or directory`，日志中没有 `SUPERSECRET_TOKEN`。应用层允许这个相对工作区的路径，OS 层仍然阻断。

**2. 为什么这次内存超限的退出码是 1，不能写成 cgroup OOM 137？**

答：本机不能创建子 cgroup，限制来自 `RLIMIT_AS`，Python 抛出未捕获的 `MemoryError`，默认退出码是 1。137 是 SIGKILL，通常与 cgroup OOM 或 `kill -9` 相关，本次没有发生。

**3. 这个沙箱能当成“任意恶意代码都可以放心执行”的证据吗？**

答：不能。它没有 gVisor 或独立内核，没有 seccomp，也没有验证内核逃逸。教学白名单加上用户命名空间，只覆盖了本课列出的文件、rlimit 和超时清理。

## 推荐资源

- [Introduction to gVisor security](https://gvisor.dev/docs/architecture_guide/intro/)：对照更强的系统调用隔离；本机未运行 runsc。
- [Docker Engine security](https://docs.docker.com/engine/security/)：核对容器与共享内核的边界；本机未启动 Docker。

## 本课小结

沙箱要同时有应用白名单和 OS 挂载 / 命名空间 / rlimit；符号链接和区外路径必须实际读不到；超限原因要写准；超时必须带走子进程和工作区。本课用真实 bwrap 验证了这些点，并写明没有 Docker、gVisor 和可写 cgroup。下一课是第 180 课 l-human（审批与人工介入）。
