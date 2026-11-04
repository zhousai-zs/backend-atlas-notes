# 第 47/208 课：cgroup v2与资源治理

> 所属阶段：第 4 阶段 · 隔离与资源治理  
> 前置课程：c-namespace、c-schedule  
> 关键词：cgroup v2、cpu.max、节流、memory.max、OOM、层次化资源分配、容器限流、throttling

## 0. 本课导读

微信公众号后台的某个接口服务，部署在 Kubernetes 里。监控上看宿主 CPU 利用率只有 30%，内存也远没打满，但业务侧 p99 延迟突然抬升，部分请求排队甚至超时。另一台机器上，某个容器被杀掉，日志里出现 OOM，可宿主 `free` 还显示有可用内存。还有一种更隐蔽的情况：容器里 `nproc` 显示 8，开发以为能吃满 8 核，实际上被限额卡在大约 1 核的 CPU 预算上。

这些现象往往不是“机器不够”，而是 **cgroup 在控制资源分配**。上一课讲的 namespace 改变的是进程看到的视图；本课的 cgroup 把进程组织成层次，并控制 CPU 带宽与内存上限，从而改变服务延迟和故障模式。本课从直觉到原理讲清三点：`cpu.max` 与节流、`memory.current`/`max` 和 OOM、层次化资源分配；最后在受控容器中限制 CPU 和内存，记录 throttling、p99 与 OOM 事件。

## 1. 学习目标

完成本课后，你应当能够：

1. 解释 cgroup v2 中 `cpu.max` 的格式与语义，并能从 `cpu.stat` 辨认 CPU 配额节流。
2. 说明 `memory.current`、`memory.max`、`memory.peak`、`memory.events` 的含义，并能区分进程/cgroup OOM 与系统级内存压力。
3. 理解层次化资源分配：子组份额受父组约束，容器限额不等于宿主空闲资源可随意占用。
4. 在受控环境中用 Docker、`systemd-run` 或直接操作 `/sys/fs/cgroup` 限制 CPU 和内存，记录 throttling、延迟分位与 OOM 相关事件。
5. 识别常见误区：容器可见 CPU 数不一定等于实际可用 CPU 预算；理解严格限额在隔离性与尾延迟之间的权衡。

对应节点的验收标准：

- 能辨认 CPU 配额节流；
- 能区分进程 OOM 与系统级内存压力。

## 2. 为什么后端要懂

**解释容器限流。** 公众号后台接口高峰时，某个推送服务或回调处理服务可能突然变慢。若该服务被设置了较低的 CPU 配额，cgroup 会在每个调度周期内强制“刹车”，表现为 `cpu.stat` 中 `nr_throttled` / `throttled_usec` 上升，应用侧看到排队、超时、p99 升高。宿主整体空闲，并不否定某个 cgroup 正在被节流。

**解释 OOM 与故障模式。** 内存上限会改变故障形态：没有 `memory.max` 时，某个泄漏进程可能拖垮整台宿主；有严格上限时，往往是该容器被 OOM kill，其他服务仍可运行。对微信公众号这类多服务共部署场景，隔离内存故障面非常关键，但也要能读懂是 cgroup 触发的 OOM，还是系统级内存压力导致的全局回收与杀进程。

**解释“宿主看似空闲但请求排队”。** CPU 利用率是全局或节点视角的平均指标；请求延迟取决于你的服务所在 cgroup 是否拿到足够的 CPU 时间片。配额用尽后，即使同机其他核空闲，你的进程也可能被强制等待下一个 period。这正是“机器很闲、接口却排队”的典型根因之一。

**几个真实场景。**

- K8s 给 Pod 配了 `limits.cpu: "1"`，容器内仍能看到 8 个 CPU；压测时 QPS 上不去，`cpu.stat` 显示大量 throttled。
- 容器 `memory.max=512M`，业务缓存膨胀后容器被杀，宿主 `free` 仍有数 GB 可用。
- 同一节点上 A 服务 `CPUQuota=200%`，B 服务无配额；A 高峰时宿主 CPU 仍有空闲，但 A 的 p99 已经恶化。
- 压测脚本只看平均 CPU，忽略 `throttled_usec` 与 p99，得出“资源够用”的错误结论。

## 3. 直觉：cgroup 是“预算本”，不是“房间门牌”

上一课把 namespace 比作楼层指南：改变你看到的编号、地图和设备清单。cgroup 更像财务预算：

- 不管你能看到多少间会议室（可见 CPU 数），财务只批给你每月固定额度（CPU 带宽配额）。
- 不管楼里还有多少空房间（宿主空闲内存），你的部门账户有硬顶（`memory.max`），超了就停你的卡或清退占用（OOM）。
- 大部门下面还有小组：父组预算约束所有子组之和，子组再怎么内部分配，也不能超过父组上限。

一句话对照：

| 机制 | 主要改变什么 | 典型体感 |
| --- | --- | --- |
| namespace | 资源视图与命名空间 | 容器里 PID、网卡、挂载与宿主不同 |
| cgroup | 资源计量与限额 | 容器再怎么“看起来有 8 核”，也可能只用到 1 核预算 |
| 调度器（上一前置课） | 谁先跑、跑多久策略 | CFS/权重影响相对份额；cgroup 限额则是硬预算 |

**命名空间让你“看见不同的世界”；cgroup 让你“在同一个世界里只能花自己的配额”。**

## 4. 核心模型：统一层次、控制器与 `/sys/fs/cgroup`

### 4.1 cgroup v2 的统一层次

cgroup v1 按控制器拆多棵树，配置分散、语义容易冲突。cgroup v2 采用**统一层次（unified hierarchy）**：所有控制器挂在同一棵树上，默认挂载点通常是：

```bash
mount | grep cgroup2
ls /sys/fs/cgroup
```

常见输出会看到根 cgroup 下的接口文件，以及 `system.slice`、`user.slice`、`docker` 或 `kubepods` 等子目录。每个目录代表一个 cgroup；目录中的进程由 `cgroup.procs`（线程组）或 `cgroup.threads` 管理。

查看当前 shell 属于哪个 cgroup：

```bash
cat /proc/self/cgroup
# cgroup v2 典型输出类似：
# 0::/user.slice/user-1000.slice/session-3.scope
```

### 4.2 控制器与启用方式

根或中间节点通过 `cgroup.controllers` 列出可用控制器，通过 `cgroup.subtree_control` 向子树启用：

```bash
cat /sys/fs/cgroup/cgroup.controllers
# 例如：cpuset cpu io memory hugetlb pids rdma misc

# 向子树启用 cpu 与 memory（需有权限，且通常由 systemd/容器运行时管理）
# echo '+cpu +memory' > /sys/fs/cgroup/cgroup.subtree_control
```

本课聚焦节点要求的三点相关接口：

- **cpu**：`cpu.max`、`cpu.weight`、`cpu.stat`
- **memory**：`memory.current`、`memory.max`、`memory.peak`、`memory.events`、`memory.stat`
- **层次**：父组限制约束子组可用上限

生产环境通常不要手工乱改系统根树；优先用 Docker、`systemd-run` 或在自己创建的临时 cgroup 子树中实验。

### 4.3 进程如何进入 cgroup

把线程组 PID 写入目标 cgroup 的 `cgroup.procs`：

```bash
echo <pid> > /sys/fs/cgroup/mydemo/cgroup.procs
```

容器运行时会在创建容器时建立对应 cgroup，并把容器内进程放进去。你在容器外看到的路径可能类似：

```bash
# Docker 常见路径（发行版与版本不同，路径可能略有差异）
find /sys/fs/cgroup -name '*cgroup-demo*' 2>/dev/null
# 或通过容器主进程：
pid=$(docker inspect -f '{{.State.Pid}}' cgroup-demo)
cat /proc/$pid/cgroup
```

## 5. cpu.max 与节流

### 5.1 `cpu.max` 的格式与语义

cgroup v2 中，CPU 带宽硬限制写在 `cpu.max`：

```text
$MAX $PERIOD
```

- `$MAX`：在一个长度为 `$PERIOD` 微秒的周期内，该 cgroup 最多可获得的 CPU 时间（微秒）。
- `$PERIOD`：配额周期，默认通常为 `100000`（100ms）。
- 特殊值：`$MAX` 为字符串 `max` 表示**不限制**（仅受权重等相对份额影响，没有带宽硬顶）。

示例：

```bash
# 最多使用约 1 个 CPU（每个 100ms 周期最多跑 100ms）
echo '100000 100000' > cpu.max

# 最多使用约 0.5 个 CPU
echo '50000 100000' > cpu.max

# 最多使用约 2 个 CPU
echo '200000 100000' > cpu.max

# 不设硬顶
echo 'max 100000' > cpu.max

# 读取
cat cpu.max
```

对应 Docker：

```bash
docker run --cpus=1.5 ...
# 运行时通常映射为约 150000 100000 的 cpu.max（具体以实现为准）
```

对应 systemd：

```bash
systemd-run -p CPUQuota=150% --unit=cg-cpu-demo stress-ng --cpu 4 --timeout 60s
# CPUQuota=150% 表示约 1.5 核的带宽配额
```

### 5.2 节流（throttling）是什么

当 cgroup 在当前 period 内已经用尽 `$MAX` 微秒的 CPU 时间，即使宿主还有空闲核，该 cgroup 内的任务也会被**强制等待**到下一个 period。这就是 CPU 配额节流。

节流不是“进程 bug”，而是限额生效的正常机制。对业务的体感是：

- 吞吐上不去，CPU 利用率曲线被“削平”；
- 延迟抖动变大，p99/p999 升高；
- 线程看起来 runnable，却拿不到 CPU。

### 5.3 从 `cpu.stat` 辨认节流

```bash
cat /sys/fs/cgroup/<path>/cpu.stat
```

与配额相关的关键字段（名称以内核文档为准，常见包括）：

| 字段 | 含义 |
| --- | --- |
| `usage_usec` | 该 cgroup 累计使用的 CPU 时间（微秒） |
| `user_usec` / `system_usec` | 用户态 / 内核态累计时间 |
| `nr_periods` | 经历的配额周期数 |
| `nr_throttled` | 发生过节流的周期数 |
| `throttled_usec` | 因节流被强制等待的累计微秒数 |

**辨认方法（对应验收标准）：**

1. 给工作负载设置明确的 `cpu.max`（例如 `50000 100000`）。
2. 用 `stress-ng` 或业务压测打满该配额。
3. 观察 `nr_throttled` 与 `throttled_usec` 持续增长。
4. 同时看应用侧延迟直方图或 `hey`/`wrk` 的 p99。

```bash
# 示例：循环读取节流计数
while true; do
  date '+%F %T'
  grep -E 'nr_throttled|throttled_usec|nr_periods' /sys/fs/cgroup/<path>/cpu.stat
  sleep 1
done
```

若 `nr_throttled` 接近 `nr_periods`，说明几乎每个周期都被掐过一次，配额明显不足。

### 5.4 `cpu.weight` 与 `cpu.max` 的分工

- `cpu.weight`（默认 100）：在**没有打到硬顶**、或多个 cgroup 竞争同一父组剩余带宽时，按相对权重分配。
- `cpu.max`：硬顶。到了顶就节流，与“宿主是否空闲”无关。

公众号后台常见误配：只设了较低的 limit（对应 `cpu.max`），压测时把限流当成“代码性能问题”；或只看 weight/请求份额，忽略 limit 已经把突发削平。

### 5.5 与可见 CPU 数的关系

容器内常见接口：

```bash
nproc
getconf _NPROCESSORS_ONLN
ls /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null
cat /proc/cpuinfo | grep -c processor
```

这些反映的是**可见/可调度的 CPU 集合**，不等于 `cpu.max` 给出的**时间预算**。例如：可见 8 核，但 `cpu.max=100000 100000`，长期可持续算力大约只有 1 核。线程池若按 `nproc` 开 8 个重计算线程，只会加剧上下文切换与排队，更容易在配额内互相争抢。

## 6. memory.current / max 和 OOM

### 6.1 关键接口

```bash
cat memory.current   # 当前该 cgroup 计入的内存用量（字节）
cat memory.max       # 硬上限；max 表示不限制
cat memory.peak      # 历史峰值（若内核支持并已启用）
cat memory.high      # 软限：超过后积极回收，通常不直接 OOM kill
cat memory.events    # 事件计数：low, high, max, oom, oom_kill 等
cat memory.stat      # 详细统计：anon, file, kernel 等
```

设置硬上限示例：

```bash
echo $((512 * 1024 * 1024)) > memory.max   # 512 MiB
echo max > memory.max                      # 取消硬上限
```

Docker：

```bash
docker run --memory=512m --memory-swap=512m ...
# 将容器 memory 硬限设为 512MiB；swap 相关行为依赖运行时与宿主配置
```

systemd：

```bash
systemd-run -p MemoryMax=512M -p MemorySwapMax=0 --unit=cg-mem-demo \
  stress-ng --vm 1 --vm-bytes 800M --timeout 30s
```

### 6.2 `memory.max` 触发的 cgroup OOM

当 cgroup 用量触及 `memory.max`，且回收无法把用量压下去时，内核会在该 cgroup 内触发 OOM killer，选出受害者进程杀掉。这是**cgroup / 容器级 OOM**，不一定等于整机内存耗尽。

观察：

```bash
cat memory.events
# 关注：
# oom <次数>        # 进入过 OOM 路径
# oom_kill <次数>   # 实际杀掉进程的次数

dmesg -T | tail -n 50
# 或 journalctl -k -n 50
# 常见关键字：Memory cgroup out of memory、Killed process、oom-kill
```

容器场景：

```bash
docker inspect -f '{{.State.OOMKilled}}' <container>
docker events --filter container=<container>
```

### 6.3 区分：进程/cgroup OOM vs 系统级内存压力

这是本课验收标准之一，务必分清：

| 维度 | cgroup / 进程侧 OOM | 系统级内存压力 |
| --- | --- | --- |
| 触发条件 | 该 cgroup 触及 `memory.max`（或等价 limit） | 整机可用内存紧张，全局 reclaim / 全局 OOM |
| 典型证据 | 该 cgroup 的 `memory.events` 中 `oom`/`oom_kill` 增加；容器 `OOMKilled=true`；dmesg 含 Memory cgroup out of memory | 宿主 `free`/`MemAvailable` 很低；多个无关服务同时变慢或被杀；`/proc/pressure/memory` 持续高压力；dmesg 为系统级 OOM，不一定带 cgroup 限额语义 |
| 宿主空闲内存 | 往往**仍然充足** | 通常**不充足** |
| 故障面 | 主要限于该容器/服务 | 可能影响同机所有负载 |
| 处理方向 | 调高 limit、修泄漏、降缓存、拆分热点 | 扩容节点、降密度、查全局泄漏、排查缓存与抖动 |

**后端排障口诀：** 先看受害服务的 cgroup `memory.current` 是否贴着 `memory.max`，再看宿主是否真的空闲。贴着 max 且宿主空闲 → 优先当容器限额问题；宿主也不空闲 → 同时考虑系统级压力。

### 6.4 `memory.high` 与故障模式选择

- `memory.high`：超过后内核对该 cgroup 更积极地回收，并可能对分配方施加节流，**通常不直接杀进程**。
- `memory.max`：硬顶，回收失败则 OOM kill。

对公众号后台这类延迟敏感服务：过低的 `memory.max` 会把偶发缓存膨胀直接变成进程死亡与请求失败；只用很高的 `memory.high`、不设合理 max，则隔离性变弱，极端情况仍可能逼近系统级压力。工程上常组合：合理 max 防拖垮宿主，监控 `memory.current`/`peak` 与 `memory.events`，对接近 high/max 提前告警。

### 6.5 短例：制造并观察 cgroup OOM

在受控环境：

```bash
docker run --rm --name mem-oom-demo --memory=128m --memory-swap=128m \
  polinux/stress --vm 1 --vm-bytes 200M --vm-hang 0 --timeout 20s
```

然后检查：

```bash
docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}' mem-oom-demo 2>/dev/null || true
# 若容器已退出，可在运行过程中另开终端观察：
# cat $(找到对应 cgroup)/memory.events
dmesg -T | grep -i -E 'oom|killed process' | tail
```

预期：进程因 cgroup 内存上限被杀；宿主可能仍有空闲内存。这就是“限额改变故障模式”。

## 7. 层次化资源分配

### 7.1 父约束子

cgroup 是树。子 cgroup 的有效上限不能突破祖先的限制：

- 父组 `cpu.max = 100000 100000`（约 1 核），两个子组即使各自写成 `100000 100000`，合起来也不能稳定吃到 2 核；竞争发生在父组预算之内。
- 父组 `memory.max = 1G`，子组设 `2G` 没有意义——有效硬顶仍受父组约束（具体生效规则以 v2 文档为准：子树用量计入父组，父组 max 会限制整棵子树）。

直觉：部门总预算 100 万，两个小组各报 100 万，并不等于公司会掏 200 万。

### 7.2 为什么后端要关心层次

在 Docker / Kubernetes 中，层次大致是：

```text
/sys/fs/cgroup
  └─ ... kubepods / docker ...
        ├─ burstable / besteffort / guaranteed（或 runtime 自己的层级）
        │    └─ pod...
        │         └─ container...
```

于是会出现：

- 节点总体仍有空闲，但某 Pod 的父级或同级竞争导致你的容器拿不到预期 CPU；
- 只调容器 limit，不理解节点可分配资源与 eviction，仍然会在高峰被挤；
- `requests` 影响调度与相对保障，`limits` 更贴近硬顶（CPU limit → 配额/节流，memory limit → 触及后 OOM）。

对微信公众号后台多服务同节点部署：推送、回调、素材、统计若共用节点，层次化配额决定了“谁挤占谁”以及“谁先被 OOM”。

### 7.3 权重、配额与请求的配合（概念层）

- **requests**：调度与相对保障，偏预留/期望。
- **limits**：硬顶。CPU limit 过紧 → 节流与尾延迟差；memory limit 过紧 → 更易 cgroup OOM；完全不设 limit → 隔离差，可能拖垮邻居或宿主。

### 7.4 用临时子树感受层次（需权限，仅限受控环境）

```bash
# 警告：仅在自己的实验机、可回滚环境操作；不要在生产节点乱写系统 cgroup。
ROOT=/sys/fs/cgroup/cgroup-lesson-demo
sudo mkdir -p "$ROOT/parent/child-a" "$ROOT/parent/child-b"

# 启用控制器（若尚未启用，且你有权限）
# echo '+cpu +memory' | sudo tee /sys/fs/cgroup/cgroup.subtree_control
# echo '+cpu +memory' | sudo tee "$ROOT/cgroup.subtree_control"
# echo '+cpu +memory' | sudo tee "$ROOT/parent/cgroup.subtree_control"

echo '100000 100000' | sudo tee "$ROOT/parent/cpu.max"
echo '80000 100000'  | sudo tee "$ROOT/parent/child-a/cpu.max"
echo '80000 100000'  | sudo tee "$ROOT/parent/child-b/cpu.max"

# 把压力进程分别放进 child-a / child-b，观察二者合计不会稳定超过父组约 1 核
```

读两边的 `cpu.stat`，对照父组 `cpu.stat`，能更直观理解“子组份额受父组约束”。

## 8. 容器、systemd 与观察命令速查

```bash
# Docker 限额：--cpus → cpu.max；--memory → memory.max
docker run -d --name cgroup-demo --cpus=0.5 --memory=256m --memory-swap=256m alpine sleep 3600
docker stats cgroup-demo
pid=$(docker inspect -f '{{.State.Pid}}' cgroup-demo)
cat /proc/$pid/cgroup
# 进入该路径后读：cpu.max、cpu.stat、memory.current/max/peak/events
# 路径因发行版而异（system.slice/docker-....scope 或 docker/<id>），以 /proc/<pid>/cgroup 为准

# systemd-run
systemd-run --scope -p CPUQuota=50% -p MemoryMax=256M --unit=lesson-cg-scope \
  stress-ng --cpu 2 --timeout 20s

# 压力与 p99：把压测分位与 throttled_usec、nr_throttled 放同一时间轴
stress-ng --cpu 4 --timeout 60s
stress-ng --vm 2 --vm-bytes 300M --timeout 30s
# hey -z 30s -c 20 http://127.0.0.1:8080/api
```

## 9. 常见误区

**误区一：容器可见 CPU 数等于实际可用 CPU 预算。**  
这是节点明确标出的坑。`nproc`、`/proc/cpuinfo`、Go 的 `runtime.NumCPU()`、Java 某些版本对可用处理器的判断，反映的往往是可见 CPU，而不是 `cpu.max` 预算。按可见核数配置线程池、并行度或压测期望，会系统性高估算力，并在限额下放大排队与尾延迟。

**误区二：宿主 CPU 空闲，所以我的服务不可能在排队。**  
错。节流看的是**你的 cgroup 配额是否用尽**，不是宿主平均值。宿主 30% 利用率与你的服务每个 period 都被 throttle 可以同时成立。

**误区三：容器 OOM 等于机器内存不够。**  
不一定。先查该容器 `memory.current` 是否顶着 `memory.max`，以及 `memory.events` 的 `oom`/`oom_kill`。宿主仍空闲却容器被杀，是典型的 cgroup 限额故障模式。

**误区四：namespace 已经隔离了，所以资源自然也被隔离。**  
错。上一课强调过：namespace 管视图，cgroup 管配额。只有 namespace、没有合理 cgroup 限额的容器，仍可能打满宿主。

**误区五：把 `cpu.weight` 当成硬顶。**  
weight 是相对份额；没有 `cpu.max`（或等价 limit）时，空闲资源可能被单容器吃光。隔离突发要靠硬顶，或上层准入与弹性策略。

**误区六：`memory.high` 与 `memory.max` 混为一谈。**  
high 偏回收与减速，max 是硬顶并可走向 OOM kill。监控和告警策略应分开设计。

**误区七：只盯 `docker stats` 的平均 CPU%，不看节流计数。**  
平均利用率可能“看起来健康”，但 `nr_throttled` 已经很高，p99 已恶化。限额场景必须把 throttling 指标纳入排障清单。

**误区八：子组 limit 写很大就能突破父组。**  
不能。层次化分配下，有效能力受祖先约束。K8s/节点层级的父组限制同样会卡住你。

## 10. 设计权衡

节点给出的权衡是：**严格限额提高隔离性，也可能放大突发负载尾延迟。**

**严格限额的收益。**

- 故障隔离：内存泄漏或 CPU 狂奔更可能只打到当事容器。
- 多租户/多服务密度：公众号后台多种服务共机时，邻居更可控。
- 容量可预期：按 limit 规划节点承载，避免“偶发打满整机”。

**严格限额的代价。**

- CPU 硬顶直接制造节流，短时突发被削平，排队变长，p99/p999 变差。
- 内存硬顶把“可回收缓存偏高”变成“直接 OOM”，可用性抖动可能更刺耳。
- 可见资源与预算不一致，导致框架默认并发度失配。

**工程上常见折中。**

- CPU：对延迟敏感的入口服务慎设过紧 limit；用请求保障 + 监控 throttling；突发明显时考虑提高 limit、拆分热点、或用 HPA/垂直扩容，而不是盲目加线程。
- 内存：max 保护宿主；同时监控 `memory.current`/`peak`，对缓存型服务区分“工作集”与“可回收页”；避免把 limit 设得刚好贴着平均用量。
- 层次：节点级预留系统与运维组件；应用层再分子组，防止某一业务线吃光整机。
- 观测：把 `throttled_usec`、`oom_kill`、容器 `OOMKilled`、应用 p99 放在同一张排障图里。

没有免费午餐：要隔离，就要接受硬顶带来的另一种失败模式；要吃突发，就要接受更弱的邻居隔离或更高的容量成本。

## 11. 动手实践：在受控容器限制 CPU 和内存，记录 throttling、p99 与 OOM

本实践必须在**受控开发环境**中进行（个人开发机、测试虚拟机或实验集群），不要在生产节点上随意改 cgroup 或对线上容器施压。需要：Linux（cgroup v2）、Docker 或 Podman、可选 `stress-ng`/`hey`、能读 `/sys/fs/cgroup` 与 `dmesg` 的权限。

### 11.1 步骤一：确认 cgroup v2

```bash
mount | grep -E 'cgroup2|cgroup'
stat -fc %T /sys/fs/cgroup
# 期望看到 cgroup2fs
cat /proc/self/cgroup
```

验收点：确认当前系统以 cgroup v2 统一层次为主（若仍是混合/v1，本课接口名可能不同，应换到 v2 实验环境）。

### 11.2 步骤二：启动带 CPU / 内存限制的容器

```bash
docker rm -f cg-cpu-demo cg-mem-demo 2>/dev/null || true

docker run -d --name cg-cpu-demo \
  --cpus=0.5 \
  --memory=512m \
  alpine sleep 3600

docker run -d --name cg-mem-demo \
  --memory=128m \
  --memory-swap=128m \
  alpine sleep 3600

docker inspect -f 'cpuNano={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}}' cg-cpu-demo
docker inspect -f 'mem={{.HostConfig.Memory}}' cg-mem-demo
```

### 11.3 步骤三：定位 cgroup 路径并记录基线

```bash
cpu_pid=$(docker inspect -f '{{.State.Pid}}' cg-cpu-demo)
mem_pid=$(docker inspect -f '{{.State.Pid}}' cg-mem-demo)
echo "cpu_pid=$cpu_pid mem_pid=$mem_pid"
cat /proc/$cpu_pid/cgroup
cat /proc/$mem_pid/cgroup
```

把输出中的路径记为 `CPU_CG`、`MEM_CG`（形如 `/sys/fs/cgroup/system.slice/docker-....scope`，以实际为准），然后：

```bash
# 将下面路径替换为真实 CPU_CG
cat "$CPU_CG/cpu.max"
cat "$CPU_CG/cpu.stat"
cat "$MEM_CG/memory.max"
cat "$MEM_CG/memory.current"
cat "$MEM_CG/memory.events"
```

验收点：能读到 `cpu.max` 大致对应 0.5 核（例如 `50000 100000`），`memory.max` 对应 128MiB 量级。

### 11.4 步骤四：打 CPU 负载并记录 throttling

在容器内制造 CPU 压力（Alpine 可先装 `stress-ng`，或改用已有工具镜像）：

```bash
docker exec -d cg-cpu-demo sh -c 'while true; do :; done'
# 更好的方式（若镜像可用）：
# docker run --rm --cpus=0.5 --name cg-cpu-stress polaris/stress-ng \
#   stress-ng --cpu 4 --timeout 45s
```

另开终端，每秒采样：

```bash
for i in $(seq 1 20); do
  echo "==== sample $i ===="
  grep -E 'nr_periods|nr_throttled|throttled_usec|usage_usec' "$CPU_CG/cpu.stat"
  sleep 1
done
```

记录：`nr_throttled` 是否上升、`throttled_usec` 增量、以及你对“宿主是否空闲”的观察：

```bash
# 宿主视角
uptime
nproc
# 容器内可见 CPU（对比误区）
docker exec cg-cpu-demo nproc 2>/dev/null || docker exec cg-cpu-demo getconf _NPROCESSORS_ONLN
```

验收点（对应“能辨认 CPU 配额节流”）：能指出至少一次 `nr_throttled`/`throttled_usec` 增长，并说明这与 `cpu.max` 有关，而不是宿主没有空闲核。

### 11.5 步骤五：粗测延迟分位（p99）

若你有本地 HTTP 演示服务，可把它放进同样的 `--cpus` 限制下，用压测工具对比“无 CPU 限制”与“有 CPU 限制”的 p99。若暂时没有业务服务，可用“完成固定计算的耗时分布”代替：

```bash
# 示例：在受限容器内反复跑一段计算，记录耗时（示意）
docker run --rm --cpus=0.5 alpine sh -c '
  for i in $(seq 1 30); do
    start=$(date +%s%N)
    # 空转一小段
    j=0; while [ $j -lt 200000 ]; do j=$((j+1)); done
    end=$(date +%s%N)
    echo $(( (end - start)/1000000 ))
  done
' | sort -n | awk '
  { a[NR]=$1 }
  END {
    n=NR
    print "count", n
    print "p50_ms", a[int(n*0.50)]
    print "p90_ms", a[int(n*0.90)]
    print "p99_ms", a[int(n*0.99)]
  }'
```

再与 `--cpus=2` 或不设 `--cpus` 的对照组比较。预期：更紧的 CPU 配额下，尾部耗时更大、更不稳定。把数字与步骤四的 `throttled_usec` 对照记录。

### 11.6 步骤六：制造并记录 OOM 事件

```bash
# 观察基线
cat "$MEM_CG/memory.events"
cat "$MEM_CG/memory.current"

# 在 128m 限制的容器里尝试分配更大内存
docker exec cg-mem-demo sh -c '
  # BusyBox/Alpine 未必有 stress；用 dd 尝试吃内存（可能很快被杀）
  dd if=/dev/zero of=/dev/shm/hog bs=1M count=300 2>/dev/null || true
'

# 更可控：单独跑一次性容器
docker run --rm --name cg-oom-once \
  --memory=128m --memory-swap=128m \
  polinux/stress --vm 1 --vm-bytes 300M --timeout 20s || true
```

检查：

```bash
docker inspect -f 'OOMKilled={{.State.OOMKilled}} Exit={{.State.ExitCode}}' cg-oom-once 2>/dev/null || true
dmesg -T | grep -i -E 'oom|killed process|memory cgroup' | tail -n 20
free -h
```

验收点（对应“能区分进程 OOM 与系统级内存压力”）：

- 若容器 `OOMKilled=true` 或 dmesg 出现 Memory cgroup out of memory，且 `free -h` 仍显示宿主有可用内存 → **cgroup/进程侧 OOM**；
- 若宿主 `MemAvailable` 也很低、多个无关进程异常、压力是全局性的 → 才倾向 **系统级内存压力**。

把 `memory.events` 中 `oom`/`oom_kill` 的前后差值记入实验笔记。

### 11.7 步骤七：可选——systemd-run 对照

```bash
systemd-run --scope -p CPUQuota=50% -p MemoryMax=128M \
  --unit=lesson47-demo \
  stress-ng --cpu 2 --vm 1 --vm-bytes 200M --timeout 20s || true

# 查找对应 scope 的 cgroup 并阅读 cpu.stat / memory.events
systemctl show lesson47-demo.scope -p ControlGroup
```

### 11.8 步骤八：清理

```bash
docker rm -f cg-cpu-demo cg-mem-demo cg-oom-once 2>/dev/null || true
docker rm -f cg-cpu-stress 2>/dev/null || true
sudo rmdir /sys/fs/cgroup/cgroup-lesson-demo/parent/child-a \
           /sys/fs/cgroup/cgroup-lesson-demo/parent/child-b \
           /sys/fs/cgroup/cgroup-lesson-demo/parent \
           /sys/fs/cgroup/cgroup-lesson-demo 2>/dev/null || true
```

### 11.9 实践验收清单

- [ ] 我能确认系统使用 cgroup v2，并找到容器对应的 cgroup 目录。
- [ ] 我能读懂 `cpu.max` 的 `$MAX $PERIOD` 格式，并知道 `max` 表示不限制。
- [ ] 我能在负载下观察 `cpu.stat` 的 `nr_throttled` / `throttled_usec` 增长，从而辨认 CPU 配额节流。
- [ ] 我记录了至少一组与节流对照的延迟分位（p99）或耗时分位。
- [ ] 我能读 `memory.current` / `memory.max` / `memory.events`，并解释 OOM 相关字段。
- [ ] 我能说明一次容器 OOM 时，为何宿主仍可能空闲——从而区分 cgroup OOM 与系统级内存压力。
- [ ] 我能说出“可见 CPU 数 ≠ CPU 预算”，并举出 `nproc` 与 `cpu.max` 不一致的例子。
- [ ] 我理解严格限额提高隔离性，也可能放大突发尾延迟。

## 12. 自测题

### 题一：某容器 `cpu.max` 为 `100000 100000`，宿主 8 核且整体利用率只有 25%。容器内压测时 p99 升高，`cpu.stat` 中 `nr_throttled` 持续增加。这可能吗？为什么？

**参考答案：** 可能。`cpu.max` 的语义是每个 period（这里 100000 微秒）内该 cgroup 最多使用 100000 微秒 CPU 时间，约等于 1 核带宽硬顶。节流取决于**该 cgroup 是否用尽自己的配额**，不取决于宿主平均利用率。宿主整体空闲时，该容器仍会在配额用尽后被强制等待下一个 period，于是出现“宿主很闲、请求却排队、p99 升高”，同时 `nr_throttled`/`throttled_usec` 上升。这正是 CPU 配额节流的典型辨认方式。

### 题二：容器内 `nproc` 输出 8，K8s `limits.cpu` 为 `"1"`。把线程池设为 8 是否合理？可能带来什么问题？

**参考答案：** 不合理，至少不能默认合理。可见 CPU 数反映的是 CPU 集合可见性，不等于实际可用 CPU 预算；`limits.cpu: "1"` 通常对应约 1 核的 `cpu.max` 配额。按 8 去设重计算线程池，会在大约 1 核的预算里塞进过多可运行线程，加剧争抢、上下文切换与排队，更容易触发节流并放大尾延迟。更稳妥的做法是按有效配额（或实测可持续 CPU）配置并发，并把 throttling 与 p99 纳入观测。

### 题三：服务进程被杀，dmesg 出现 OOM 相关日志，但 `free -h` 显示宿主仍有数 GB 可用。如何判断这是 cgroup/进程侧 OOM 还是系统级内存压力？还要看哪些信号？

**参考答案：** 优先查该服务所在 cgroup 的 `memory.current` 是否接近或达到 `memory.max`，看 `memory.events` 中 `oom`/`oom_kill` 是否增加，容器场景查 `OOMKilled`，并读 dmesg 是否包含 Memory cgroup out of memory 一类描述。若限额触顶且宿主内存仍充足，应判定为 **cgroup/进程侧 OOM**（限额改变了故障模式）。系统级内存压力则通常伴随宿主 `MemAvailable` 很低、`/proc/pressure/memory` 持续压力、多个无关负载同时受害，且不一定有“单容器 max 触顶”的证据。二者处理方向不同：前者调 limit/修泄漏/降缓存；后者还要考虑节点扩容、降密度与全局回收问题。

## 13. 推荐资源

- **cgroup v2与资源治理：参考文档 / 原始资料**  
  https://docs.kernel.org/admin-guide/cgroup-v2.html  
  来源：docs.kernel.org；语言：英文；类型：文档；难度：深入；免费。  
  说明：重点阅读 `cpu.max` 与节流，以及 `memory.current`/`max` 和 OOM 相关章节，用于核对本节点实验的语义和边界。建议对照本课实践，在受控环境中边读边验证 `cpu.stat`、`memory.events` 字段。

本地可继续查阅：

```bash
man 7 cgroups
man 1 systemd-run
man 5 systemd.resource-control
# 内核文档（若本地有 kernel-doc 包）
# /usr/share/doc/... 或线上 admin-guide/cgroup-v2.html
```

## 14. 本课小结

- cgroup 把进程组织成层次并控制资源分配；CPU 带宽与内存上限会改变服务延迟和故障模式。
- `cpu.max` 格式为 `$MAX $PERIOD`；`max` 表示不限制。配额用尽后即使宿主空闲也会节流，辨认看 `cpu.stat` 的 `nr_throttled`/`throttled_usec`。
- `memory.current` 是当前用量，`memory.max` 是硬顶；触顶且回收失败会走 cgroup OOM。要区分它与系统级内存压力：前者常伴随宿主仍空闲，后者是整机压力。
- 层次化分配下，子组受父组约束；容器/K8s 中的 limits 必须放在整棵树的语境里理解。
- 常见误区：容器可见 CPU 数不一定等于实际可用 CPU 预算。
- 设计权衡：严格限额提高隔离性，也可能放大突发负载尾延迟。
- 实践上：在受控容器限制 CPU 和内存，同时记录 throttling、p99 与 OOM 事件，才能把原理连到公众号后台真实的限流与排障现场。
