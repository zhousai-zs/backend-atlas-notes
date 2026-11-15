# 第 153/208 课：容器镜像与运行边界

> 序号 153/208 · id `j-container` · 领域 J · 阶段 3 · 难度 进阶 · 子类 交付与安全
> 前置：[`j-ci`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/152.html) 和 [`c-cgroup`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/47.html)
> 节点摘要：容器以 namespaces 与 cgroups 管理进程视图和资源；共享宿主内核，不等同独立虚拟机。

## 导读

上一课把工具链锁在流水线里。交付时还要把编译环境和运行环境分开：运行镜像应只留下服务进程，不应带上编译密钥或编译器；进程要能在内存上限下失败，并在收到终止信号时收尾。

教学假设：内容后台有一个 C++ 教学服务，用多阶段布局生成最小运行根文件系统。以下镜像、密钥和限制均为教学设计，不代表腾讯或微信内部实现。本机没有 Docker / Podman 守护进程，也不能创建 cgroup；实验用目录化的构建/运行阶段和 POSIX `RLIMIT_AS` 完成等价检查，并如实记录与真实容器运行时的差异。

## 学习目标

1. 解释多阶段构建及适用边界
2. 独立完成实践：为 C++ 服务构建最小镜像，限制内存后观察 OOM 与退出码。

## 为什么后端要懂

节点给出的理由是「标准化服务交付与资源约束。」同一份源码如果把编译密钥和工具链带进运行环境，攻击面和泄露面都会变大；没有内存上限和终止处理，调度器也无法安全回收进程。

## 核心知识点

### 1. 多阶段构建

**原理。** Docker 文档 *Multi-stage builds* 写明：多阶段构建在一份 Dockerfile 里使用多条 `FROM`，每一条开始新的阶段，并可以只把需要的产物复制到下一阶段，把其余内容留在后面。示例把编译得到的二进制拷进最终阶段后指出：最终镜像里只有该二进制，构建所需工具不会被保存。

**直觉。** 编译需要编译器、头文件，甚至临时密钥；运行只需要程序和它的动态库。阶段之间用复制，而不是把整个构建目录打包。

**实践对照。** 本课提供符合该模式的 `Dockerfile`（`build` 阶段写入 `BUILD_SECRET`，`runtime` 阶段只复制二进制并 `USER app`）。因为没有守护进程，`driver.py` 把同样的划分落实到 `rootfs-build/` 与 `rootfs-runtime/`。扫描结果：密钥出现在构建阶段的 `secret/compile-secret`，运行阶段 0 处命中。二进制由 ziglang 0.17.1 的 `zig c++`（目标 `x86_64-linux-gnu`）编译，不是 `docker build` 的产物。`is_live_container_runtime=false`。

### 2. 非 root 运行

**原理。** 最终阶段以非特权用户启动，可降低容器被攻破后的默认权限。文档示例用 `scratch` 或精简基础镜像承载二进制；是否使用 `USER` 由镜像作者声明。

**直觉。** 进程以 uid 0 运行时，一次远程漏洞就更接近宿主权限。非 root 不是沙箱，只是缩小默认能力。

**实践对照。** 运行时 uid 为 1000，状态文件为 `shutdown_ok` 与 `uid=1000`。程序在 `getuid()==0` 时直接退出码 3。本环境本身不是 root，因此「拒绝 root」只验证了分支存在，没有在真实 uid 0 下启动。

### 3. CPU/内存限制

**原理。** 容器的硬限制通常由 cgroup 的 `memory.max`、`cpu.max` 实施；突破内存上限时，内核 OOM killer 常用 SIGKILL，退出码多为 137。本机 cgroup 控制器列表含 `memory`，但创建子 cgroup 被拒绝。

**直觉。** 没有上限的「最小镜像」仍然可以吃光宿主内存。限制必须由内核或监督者实际执行，而不是只写在清单里。

**实践对照。** `mkdir /sys/fs/cgroup/...` 返回 Permission denied。改用 `RLIMIT_AS=16MiB` 让进程尝试分配 64MiB：`malloc_failed at_mb=13`，退出码 2。这不是 cgroup OOM，也不是 137；`rlimit_is_cgroup_oom=false`。CPU 限制同样未能通过 cgroup 实施，本课未伪造 CPU 节流数据。

## 常见误区

1. **把容器当作足够隔离任意恶意代码的沙箱。** 节点原句即此。容器与宿主共享内核，namespaces 和用户隔离也取决于运行时配置。本课甚至没有启动真正的容器运行时，更不能把目录化根文件系统当成安全沙箱。
2. **看见 Dockerfile 就认为已经构建过镜像。** 本课的 Dockerfile 是对照官方模式的声明；实际检查是对两个根目录的扫描和本地进程测试。

## 设计权衡

节点原句是「最小镜像降低攻击面，也减少现场调试工具。」运行阶段没有编译器或密钥，泄露面更小；一旦需要在现场用调试器，就要另备带符号的调试镜像或临时进入构建阶段，而不是把工具默认装进生产镜像。

## 动手实践

实践目录为 `practice153/`，需要 Python 3 和可调用的 C++ 编译器。脚本使用 `tools/zig-c++`。本次为 CPython 3.13.5 与 ziglang 0.17.1。

```bash
cd practice153
bash test.sh
cat results.json
cat reports/secret-scan.txt
cat reports/memory-rlimit.txt
cat reports/sigterm.txt
```

脚本物化两个阶段、扫描密钥、探测 cgroup、用 `RLIMIT_AS` 限制内存，并发送 SIGTERM。成功最后一行为 `ALL_CHECKS_PASSED`。`rootfs-runtime` 中的二进制可在复跑时重建，不必当作发布产物依赖。

本次官方实践目录：`docker_daemon=false`，`cgroup_writable=false`；运行镜像不含编译密钥；SIGTERM 后退出码 0 且写入 `shutdown_ok`；内存限制退出码 2 而非 137。

两项验收是「运行镜像不包含编译密钥」以及「进程正确处理 SIGTERM」。前者对应运行阶段扫描为空；后者对应退出码 0 与 `shutdown_ok`。内存限制的退出码按本机实际机制记录，不把 `RLIMIT_AS` 说成内核 OOM。

## 源码与文档阅读

阅读 `Dockerfile`、`service.cpp` 和 `reports/secret-scan.txt`。Docker 文档说明最终镜像可以只保留二进制；本实验没有执行 `docker build`，也没有使用 `scratch` 基础镜像，差异应记在适用边界里。

2026-11-15 在本机核对节点 URL。用普通库抓取时只得到 6876 字节的不完整文档；随后用浏览器 User-Agent 取得完整 HTML。表中 Date 为 CloudFront 响应头（缓存），另记抓取时刻。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） | 备注 |
| --- | ---: | --- | --- | --- |
| [Docker — Multi-stage builds](https://docs.docker.com/build/building/multi-stage/)：Multi-stage builds \| Docker Docs | 566751 | 响应头 2026-11-15 09:11:18（CDN Date）；完整页抓取约 22:59 | 2026-11-14 01:08:38 | HTTP 200，节点 URL 无路径跳转 |
| 同页源码 [docker/docs multi-stage.md](https://raw.githubusercontent.com/docker/docs/main/content/manuals/build/building/multi-stage.md) | 7227 | 2026-11-15 22:59 左右 | （未作为正文依据的缓存头） | 用于核对与 HTML 一致的多阶段定义 |

节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。

## 自测题

1. **为什么编译密钥不能出现在运行阶段？** 运行阶段会随服务一起分发和启动，密钥一旦进入该文件系统，就会扩大泄露范围。本课扫描要求运行阶段命中数为 0。
2. **退出码 2 是 OOM killer 吗？** 不是。本次是 `RLIMIT_AS` 下 `malloc` 失败。cgroup OOM 常见为 137，但本机无法写入 `memory.max`。
3. **非 root 是否等于与宿主隔离？** 不等于。容器共享内核；非 root 只降低默认权限。

## 推荐资源

- [Docker — Multi-stage builds](https://docs.docker.com/build/building/multi-stage/)：用于核对多阶段构建与非 root 运行，将结论落实到本节点实验。英文，进阶，免费。

## 本课小结

用多阶段划分把编译密钥和工具留在构建侧，运行侧只保留非 root 进程；内存与 CPU 限制必须由本机真实可执行的机制来验证，不能把清单上的数字说成已经生效。下一课是第 154 课 j-kubernetes（Kubernetes 服务生命周期）。
