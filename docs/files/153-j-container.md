# 第 153/208 课：容器镜像与运行边界

> 序号 153/208 · id `j-container` · 领域 J · 阶段 3 · 难度 进阶 · 子类 交付与安全
> 前置：[`j-ci`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/152.html) 和 [`c-cgroup`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/47.html)
> 节点摘要：容器以 namespaces 与 cgroups 管理进程视图和资源；共享宿主内核，不等同独立虚拟机。

## 导读

上一课把工具链锁在流水线里。交付时还要把**编译环境**和**运行环境**拆开：运行镜像应只留下服务进程，不应带上编译器或编译密钥；进程要能在内存上限下失败，并在收到终止信号时收尾。

教学假设：内容后台有一个 C++ 教学服务，要用多阶段 Dockerfile 生成最小运行镜像。以下镜像名、密钥和限额都是教学设计，不代表腾讯或微信内部实现。

**本机事实（2026-11-15，Asia/Shanghai）：** 没有 `docker` / `podman` 二进制，也没有守护进程套接字；`/sys/fs/cgroup` 可见 `memory` 控制器，但当前用户创建子 cgroup 被拒绝（Permission denied）。因此本课**没有**把目录里的 rootfs 布局称作「已构建的镜像」，也**没有**观察到 cgroup OOM 退出码 137。实践包提供完整的 `Dockerfile`、`scripts/docker-e2e.sh` 和 `scripts/diagnose.sh`；在有引擎的机器上应跑端到端脚本。本机只验收 Dockerfile 静态策略，以及同一份 `service.cpp` 在宿主机上的 SIGTERM 与 `RLIMIT_AS` 行为，并在结果里列出未实测项。

## 学习目标

1. 解释多阶段构建及适用边界
2. 独立完成实践：为 C++ 服务构建最小镜像，限制内存后观察 OOM 与退出码。

## 为什么后端要懂

节点给出的理由是「标准化服务交付与资源约束。」同一份源码如果把编译密钥和工具链带进运行环境，攻击面和泄露面都会变大；没有内存上限和终止处理，调度器也无法安全回收进程。容器共享宿主内核，所以「最小镜像 + 非 root + cgroup 限额」是交付约定，不是任意恶意代码的沙箱。

## 核心知识点

### 1. 多阶段构建

**直觉。** 编译需要编译器、头文件，有时还需要临时密钥；运行只需要程序和它的动态库。阶段之间用「复制产物」，而不是把整个构建目录打包进最终镜像。

**原理。** 2026-11-15 抓取节点给出的 Docker 文档 *Multi-stage builds*（`https://docs.docker.com/build/building/multi-stage/`，响应 Date 为 2026-11-15 09:11:18 CST，Last-Modified 2026-11-14 01:08:38 CST；同日另一次抓取曾得到过仅 6876 字节的薄页面，用浏览器 UA 再抓为 566751 字节，以完整页面为准）。文档写明：多阶段构建在一份 Dockerfile 里使用多条 `FROM`，每一条开始新的阶段，并可以只把需要的产物复制到下一阶段；示例把编译得到的二进制拷进最终阶段后指出，最终镜像里只有该二进制，构建所需工具不会被保存。

本课 `Dockerfile` 按这一模式写了两个命名阶段：

```dockerfile
FROM debian:bookworm AS build
WORKDIR /src
ARG BUILD_SECRET=atlas-compile-key-1492
ENV BUILD_SECRET=${BUILD_SECRET}
COPY service.cpp ./
RUN echo "$BUILD_SECRET" > /tmp/compile-secret \
    && c++ -std=c++17 -O2 service.cpp -o /bin/service \
    && rm -f /tmp/compile-secret

FROM debian:bookworm-slim AS runtime
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app
COPY --from=build /bin/service /usr/local/bin/service
USER app
ENTRYPOINT ["/usr/local/bin/service"]
```

逐段说明：

- `AS build` / `AS runtime` 只是阶段名，供后面的 `COPY --from=` 引用。
- 密钥用 `ARG` 在构建时传入。教学文件给了一个默认值，便于本地复现；真实流水线应在 `--build-arg` 或 BuildKit secret 里提供，并避免把生产密钥写进仓库。
- `RUN` 把密钥写到构建阶段的 `/tmp/compile-secret`，编译结束后删除该文件。即便删除，它仍可能存在于**构建阶段的某一层**；关键是 **runtime 阶段不得 `COPY` 它**。
- 第二条 `FROM` 丢弃整个 build 阶段的文件系统，只通过 `COPY --from=build /bin/service` 拿走二进制。源码、`c++`、密钥文件都不应再出现。
- 静态检查（`static_check.py`）会拒绝：运行阶段提到 `BUILD_SECRET`、复制 `compile-secret` 或 `service.cpp`、缺少 `USER`、或 `USER root`。这些检查**不能**证明镜像已被构建，只能证明这份 Dockerfile 符合策略。本机结果：`stage_count=2`，`proves_image_was_built=false`。

**适用边界。** 多阶段解决的是「最终镜像里有什么」，不解决「构建机是否被污染」或「运行时进程是否被 cgroup 限制」。没有容器引擎时，不要用宿主机目录扫描代替 `docker export`。

**真实路径（本机未执行，见 `scripts/docker-e2e.sh` / `diagnose.sh`）。**

```bash
docker build --build-arg BUILD_SECRET=atlas-compile-key-1492 -t atlas-153-service:local .
cid=$(docker create --user 1000:1000 atlas-153-service:local)
docker export "$cid" | grep -F atlas-compile-key-1492 && echo LEAK || echo clean
docker rm "$cid"
```

### 2. 非 root 运行

**直觉。** 进程以 uid 0 运行时，一次远程漏洞更接近宿主权限。`USER app` 缩小的是容器**默认**身份，不是沙箱。

**原理。** 最终阶段用 `useradd --uid 1000 ... app` 再 `USER app`，使镜像默认入口不再是 root。文档示例可以用 `scratch` 或精简基础镜像承载静态二进制；是否声明 `USER` 由镜像作者决定。本课显式写出，并由静态检查强制。

**实践对照。** `service.cpp` 在 `getuid()==0` 时直接以退出码 3 拒绝启动。本机编译出的宿主机二进制以 uid 1000 运行（教学环境本身不是 root），SIGTERM 后状态文件为 `shutdown_ok` 与 `uid=1000`。这只验证了「进程代码在非 root 下能跑、并拒绝 root 分支存在」；**没有**验证容器运行时是否真的丢掉了特权、是否启用了 user namespace。这些列在 `not_verified_on_this_machine`。

### 3. CPU/内存限制与 SIGTERM

**直觉。** 没有上限的「最小镜像」仍然可以吃光宿主内存。限制必须由内核或运行时实际执行，而不是只写在注释里。终止则是另一条契约：调度器发 SIGTERM，进程应停掉新工作并退出 0。

**原理。** 容器的硬限制通常由 cgroup 的 `memory.max`、`cpu.max` 实施。突破内存上限时，内核 OOM killer 常用 SIGKILL，shell 里常见退出码 137（128+9）。这与「malloc 失败后进程自己 `return 2`」不是同一条路径。本机 cgroup 控制器列表含 `memory`，但 `mkdir /sys/fs/cgroup/...` 返回 Permission denied，因此不能在本机制造 137。

**实践对照（本机已验证的宿主机近似，不是容器 OOM）。** `host_checks.py` 用 POSIX `RLIMIT_AS=16MiB` 让同一份二进制尝试分配 64MiB：`malloc_failed at_mb=13`，退出码 2。结果明确记录 `is_cgroup_oom=false`、`observed_exit_is_137=false`。SIGTERM 路径：进程先写 `ready`，收到信号后写 `shutdown_ok` 并以 0 退出。节点验收里「进程正确处理 SIGTERM」这一条，在**进程契约**层面本机是成立的；「在容器里由 `docker stop` 触发」尚未做。

有引擎时应跑：

```bash
docker run --rm --user 1000:1000 --memory 16m --memory-swap 16m \
  atlas-153-service:local --eat-mb 64
echo exit=$?    # 可能是 137，也可能因引擎忽略 --memory 而仍成功；成功则应判失败并记录「限额未生效」
docker run -d --name atlas-153-term --user 1000:1000 atlas-153-service:local
docker stop -t 3 atlas-153-term
docker inspect -f '{{.State.ExitCode}}' atlas-153-term
```

## 常见误区

- 把容器当作足够隔离任意恶意代码的沙箱。节点指出容器共享宿主内核。
- 把「目录里仿了一个 rootfs」写成已经 `docker build` 成功。本课明确 `directory_layout_is_not_an_image=true`。
- 把 `RLIMIT_AS` 的退出码 2 说成 cgroup OOM 137。
- 认为最终阶段 `rm` 了密钥，构建层历史里就一定没有。要用 `docker history` / `docker export` 检查运行镜像。

## 设计权衡

最小镜像降低攻击面，也减少现场调试工具（往往没有 shell 和调试器）。多阶段增加 Dockerfile 复杂度，但换来运行环境与工具链分离。把密钥放进 `ARG` 图方便，却容易进层缓存；更严的做法是 BuildKit secret 挂载（本课未在本机实跑，不据此下实验结论）。

## 动手实践

```bash
cd practice153
bash test.sh
# 若本机有 Docker/Podman：脚本会自动跑 scripts/docker-e2e.sh
# 若没有：阅读 results.json 中的 verified_on_this_machine 与 not_verified_on_this_machine
# 并按 scripts/diagnose.sh 在你自己的引擎上补齐端到端项
```

本机一次运行（2026-11-15 23:22 左右，Asia/Shanghai）：`docker_e2e_verified=false`，跳过原因 `NO_CONTAINER_ENGINE`；Dockerfile 静态检查通过；宿主机 SIGTERM 退出 0；`RLIMIT_AS` 退出 2，stderr 为 `malloc_failed at_mb=13`。二进制内不含编译密钥字符串。

## 源码与文档阅读

1. `Dockerfile`：两阶段、`COPY --from=build`、`USER app`。
2. `static_check.py`：只解析文本，不构建镜像。
3. `service.cpp`：`SIGTERM`、`--eat-mb`、拒绝 uid 0。
4. `scripts/docker-e2e.sh`：有引擎时的 export 扫密钥、`docker stop`、`--memory`。
5. `results.json` / `run.log`：必须同时出现已验证项和未验证项（无引擎时）。

## 自测题

**1. 为什么 `COPY --from=build /bin/service` 能丢掉编译器和 `/tmp/compile-secret`？若有人在 runtime 阶段再 `COPY service.cpp` 会怎样？**

答案：第二条 `FROM` 从空的新基础镜像开始，只有被 `COPY --from` 点名的路径进入最终镜像。再复制源码会把构建输入带进运行环境，静态检查应失败；有引擎时应能在 `docker export` 里看到该文件。

**2. 本机实验退出码是 2，为什么不能写成「已验证容器 OOM」？**

答案：2 来自程序在 `malloc` 失败后主动返回，限制手段是 POSIX `RLIMIT_AS`。cgroup 的 `memory.max` 触发的是内核 OOM killer，常见 137。本机不能创建子 cgroup，也没有容器引擎执行 `--memory`。

**3. 同事说「我用 Python 扫了一个 rootfs 目录，密钥不在，所以镜像是安全的」。如何回应？**

答案：那只是目录布局，不是镜像层。没有 `docker build` 与 `docker export`，不能验收「运行镜像不包含编译密钥」。应使用本课的 Dockerfile 和 `docker-e2e.sh`。

## 推荐资源

| 资源 | 字节 | 抓取时间 (CST) | Last-Modified (CST) | 说明 |
| --- | --- | --- | --- | --- |
| [Docker — Multi-stage builds](https://docs.docker.com/build/building/multi-stage/) | 566751（完整页） | 2026-11-15 09:11:18（文档 Date）/ 当日另有完整抓取 | 2026-11-14 01:08:38 | 核对应使用多条 `FROM` 与 `COPY --from`。英文，进阶，免费。首次精简抓取仅 6876 字节，不以薄页为准。 |

## 本课小结

多阶段构建把编译密钥和工具链留在被丢弃的阶段，运行镜像只保留二进制并以非 root 启动；内存上限必须由运行时真正执行，SIGTERM 必须由进程自己收尾。本机验证了 Dockerfile 策略和进程契约，没有验证 Docker 构建、镜像 export、cgroup OOM 137。有引擎时不要跳过 `scripts/docker-e2e.sh`。
