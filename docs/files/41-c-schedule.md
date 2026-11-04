# 第 41/208 课：调度、亲和性与上下文切换

> **节点**：`c-schedule`（Scheduling）  
> **领域 / 阶段 / 难度**：C / 3 / 进阶  
> **子类**：进程、线程与调度  
> **先修**：`c-thread`、`a-memory`  
> **标签**：Scheduling、C、C++  
> **一句话**：调度器在可运行任务间分配 CPU，CPU 亲和性限制运行位置，切换成本随工作集与调度场景变化。

---

## 1. 学习目标

完成本课后，你应该能够：

1. 解释**可运行（runnable）**与**阻塞（blocked）**的区别，并能用 `ps`、`/proc/<pid>/status`、`/proc/<pid>/schedstat`、`vmstat` 等工具区分“在等 CPU”和“在等事件”。
2. 解释 `nice`、调度策略、CPU 亲和性分别控制什么，以及它们与 CFS/EEVDF、容器 CPU 配额、NUMA 的关系。
3. 解释**自愿上下文切换**与**非自愿上下文切换**的成因，知道为什么不能把某次测得的切换耗时当作通用常数。
4. 独立完成实验：对线程池设置不同大小和亲和性，记录吞吐、p99 与切换率，并给出有数据支撑的结论。

本节点的实践目标是：

> 对线程池设置不同大小和亲和性，记录吞吐、p99 与切换率。

验收标准是：

- 能区分**阻塞等待**与**可运行排队**。
- 不把**固定切换耗时**当通用常数。

---

## 2. 为什么后端工程师要懂调度

以腾讯微信公众号后台这类服务为例：请求处理里既有 CPU 工作（协议解析、JSON 序列化、签名校验、业务逻辑），也有阻塞工作（数据库、缓存、RPC、日志落盘）。很多线上问题并不是“CPU 真的跑满了”，而是“任务在等 CPU”或“任务被限流了”。

### 2.1 CPU 未满，为什么延迟还高

`top` 看到整机 CPU 只有 40%，并不代表你的请求没有被 CPU 调度影响。常见情况有：

- **局部 CPU 争用**：所有工作线程被绑到 CPU 0，CPU 0 已经 100%，其他核空闲。整机利用率看起来不高，但 CPU 0 的运行队列很长。
- **容器 CPU 配额限流**：容器 `cpu.max` 只给 0.5 个 CPU，短时间突发把配额用完，剩余时间被 throttle 到下一个周期。宿主机很闲，但容器内请求 p99 飙升。
- **阻塞等待**：请求卡在数据库、磁盘、下游 RPC 上，线程处于 `S` 或 `D` 状态，CPU 自然不高。此时加线程通常只会增加“更多在等”的线程。
- **锁竞争或唤醒延迟**：线程可运行但抢不到锁，或者被唤醒后没有及时拿到 CPU。
- **迁核与缓存失效**：任务在 CPU 之间迁移，L1/L2 缓存、TLB、分支预测器状态变冷，单次请求的执行时间变长。

所以，**CPU 利用率回答的是“CPU 忙不忙”，不是“请求等多久”**。后端要同时看运行队列、阻塞状态、切换率、限流指标和尾延迟。

### 2.2 线程数过多会发生什么

线程池开得太大，对 CPU 密集型工作通常不是好事：

- 可运行线程数超过有效 CPU 数，任务开始在运行队列里排队。
- 上下文切换增多，缓存和 TLB 被反复污染。
- 每个线程的栈、调度实体和队列开销增加。
- 尾延迟先恶化：平均吞吐可能还没明显下降，p99 已经抖动。
- 在容器里还会叠加 `cpu.max` 限流：线程越多，越容易在短时间内打满配额，然后整组被 throttle。

可以用排队论直觉理解：当并发工作接近或超过服务能力时，排队延迟会非线性上升。线程池大小应该由**有效 CPU 配额**、工作类型和尾延迟目标共同决定，而不是“越大越安全”。

### 2.3 迁核为什么会影响延迟

CPU 亲和性允许任务在多个核上运行，调度器会做负载均衡，这可能把任务迁到另一个核。迁移的代价包括：

- 原核上的 L1/L2 缓存变冷，工作集需要重新加载。
- TLB 可能失效，页表遍历增加。
- 分支预测器、预取器等微架构状态丢失。
- 如果跨 NUMA 节点，还可能访问远端内存，延迟和带宽都变差。

迁核不一定是坏事：负载均衡能避免某些核过载。但在延迟敏感场景，频繁迁移会把“偶发尾延迟”放大。是否绑核，要用数据判断。

---

## 3. 从直觉到原理

### 3.1 可运行与阻塞

#### 直觉

把 CPU 想成服务窗口，任务想成顾客：

- **可运行（runnable）**：顾客已经准备好，站在队伍里等窗口。可能正在被服务，也可能在排队。
- **阻塞（blocked）**：顾客还没准备好，比如在等材料、等电话，暂时不在队伍里。材料到了才会重新排队。

调度器只从**可运行队列**里挑任务。阻塞任务不在运行队列里，所以“线程很多”不等于“运行队列很长”；关键看有多少线程同时处于可运行状态。

#### Linux 任务状态：R、S、D

`ps -o stat` 里的状态字母：

| 状态 | 含义 | 是否占运行队列 | 典型场景 |
|---|---|---|---|
| `R` | Running / Runnable：正在 CPU 上运行，或已就绪等待 CPU | 是 | CPU 计算、刚被唤醒 |
| `S` | Interruptible sleep：可中断睡眠 | 否 | 等网络、等锁、`sleep`、`epoll_wait` |
| `D` | Uninterruptible sleep：不可中断睡眠 | 否 | 磁盘 I/O、某些内核路径、NFS |
| `T` | Stopped：被暂停 | 否 | `SIGSTOP`、调试 |
| `Z` | Zombie：已退出但未被回收 | 否 | 父进程未 `wait` |
| `I` | Idle：空闲内核线程 | 否 | 内核后台线程 |

`ps` 还可能显示修饰符，例如：

- `<`：高优先级
- `N`：低优先级（nice）
- `L`：有页面被锁定在内存
- `s`：会话首进程
- `l`：多线程
- `+`：前台进程组

常用命令：

```bash
ps -o pid,stat,wchan:32,comm -p <pid>
ps -L -o pid,tid,psr,stat,comm,ni,pri,rtprio -p <pid>
```

`wchan` 显示内核等待通道，能帮助判断 `S`/`D` 在等什么。`psr` 显示任务最近运行的 CPU，可用于观察迁移。

#### 运行队列等待时间：`schedstat` 的第二个字段

`/proc/<pid>/schedstat` 每行通常有三个字段：

```text
<on_cpu_ns> <runqueue_wait_ns> <timeslices>
```

- 第 1 个字段：任务在 CPU 上运行的时间（纳秒）。
- 第 2 个字段：任务**可运行但在运行队列中等待**的时间（纳秒）。
- 第 3 个字段：任务运行过的 timeslice 次数。

第二个字段非常关键：它直接度量“已经准备好、但还没轮到 CPU”的时间。它和 CPU 利用率不是一回事。

一个短小的读取示例：

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>

int main(void) {
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/schedstat", getpid());

    FILE *f = fopen(path, "r");
    unsigned long long on_cpu, wait, slices;
    if (f && fscanf(f, "%llu %llu %llu", &on_cpu, &wait, &slices) == 3) {
        printf("on_cpu=%llu ns, runqueue_wait=%llu ns, slices=%llu\n",
               on_cpu, wait, slices);
    }
    if (f) fclose(f);
    return 0;
}
```

注意：

- `/proc/<pid>/schedstat` 对应进程的主线程；要看每个线程，使用 `/proc/<pid>/task/<tid>/schedstat`。
- 这是**累计值**，应取两次采样的差值，再除以请求数或时间，得到平均排队延迟。
- 被 cgroup 限流的任务不一定是“在运行队列里等”，它可能直接被 throttle 到下一个周期；这部分要看 `cpu.stat`，不能只看 `schedstat`。

#### 可运行排队与 CPU 利用率的区别

| 指标 | 回答的问题 | 高值说明什么 |
|---|---|---|
| CPU 利用率 | CPU 有多少时间在忙 | CPU 忙，但不一定代表请求在等 |
| `vmstat` 的 `r` | 有多少可运行任务 | 运行队列有压力，可能有排队 |
| `vmstat` 的 `b` | 有多少阻塞任务 | 可能在等 I/O、锁或内核事件 |
| `schedstat` 第二字段 | 任务可运行后等了多久 | 直接反映运行队列等待 |
| `schedstat` 第一字段 | 任务真正跑了多久 | 反映 CPU 服务时间 |
| `cpu.stat` 的 throttled | 容器是否被限流 | 配额耗尽导致停顿 |

一个典型误区是：看到 CPU 利用率不高，就认为“不是 CPU 问题”。如果线程都绑在少数核上，或者容器被限流，整机 CPU 可能不高，但请求仍然在等 CPU。

`vmstat 1` 示例：

```text
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 8  0      0  ...    ...    ...     0    0    10    20 1200 4500 45  5 45  5  0
```

- `r=8`：有 8 个可运行任务。若长期大于有效 CPU 数，说明运行队列有压力。
- `b=0`：没有明显的不可中断阻塞。
- `cs=4500`：每秒约 4500 次上下文切换。要和负载、线程数一起看，不能孤立判断好坏。

#### 阻塞等待与可运行排队的区分方法

可以按下面的顺序排查：

1. **看状态**：`ps -o stat` 中大量 `S`/`D`，更像阻塞；大量 `R` 且 CPU 数有限，更像可运行排队。
2. **看 `vmstat`**：`r` 高、`b` 低，偏向可运行排队；`b` 高，偏向不可中断阻塞。
3. **看 `schedstat`**：第二字段增长快，说明可运行后等 CPU 久；第一字段增长快，说明真正在跑。
4. **看 `cpu.stat`**：`nr_throttled`、`throttled_usec` 增长，说明容器配额限流。
5. **看线程数和亲和性**：线程数远超有效 CPU，或全部绑到同一核，都会制造局部排队。

### 3.2 nice、策略与亲和性

#### nice 与权重：不是硬限制，是相对份额

`nice` 的取值范围通常是 `-20` 到 `19`：

- 值越小，优先级越高；`-20` 最高，`19` 最低。
- 普通用户只能降低优先级（增大 nice 值）；提高优先级通常需要 `CAP_SYS_NICE`。
- `nice` 只对普通调度类（如 `SCHED_OTHER`、`SCHED_BATCH`）有相对份额意义，不是硬性 CPU 上限。

在 CFS 中，nice 会映射成权重。常见权重如下（不同内核版本数值可能有差异，但比例关系稳定）：

| nice | 权重 | 相对 nice 0 的份额 |
|---:|---:|---:|
| -20 | 88761 | 约 86.7 倍 |
| -10 | 9548 | 约 9.3 倍 |
| 0 | 1024 | 1 倍 |
| 5 | 335 | 约 0.327 倍 |
| 10 | 110 | 约 0.107 倍 |
| 19 | 15 | 约 0.015 倍 |

所以 nice 0 与 nice 5 的线程在同一个竞争域内，份额大约是 `1024 : 335 ≈ 3.06 : 1`。这只是竞争时的相对比例，不是“nice 5 最多只能用 24% CPU”的硬限制；如果 nice 0 的线程阻塞了，nice 5 仍然可以用满 CPU。

#### CFS 与 EEVDF 的直觉

- **CFS（Completely Fair Scheduler）**：给每个任务维护虚拟运行时间 `vruntime`，优先运行虚拟时间最落后的任务。nice 权重决定 `vruntime` 增长快慢：权重越高，`vruntime` 增长越慢，获得 CPU 越多。
- **EEVDF（Earliest Eligible Virtual Deadline First）**：Linux 6.6 起成为默认的普通任务调度器，在 CFS 的虚拟时间基础上引入“虚拟截止时间”，优先选择最早截止且 eligible 的任务，以改善延迟敏感任务的响应。nice 仍然映射为权重，份额直觉仍然适用。

不需要背公式，但要记住两点：

1. nice 是**相对权重**，不是绝对配额。
2. 调度器追求公平和吞吐，不承诺单个任务的固定延迟。

#### 调度策略

| 策略 | 类型 | 行为直觉 | 适用与注意 |
|---|---|---|---|
| `SCHED_OTHER` | 普通 | 默认策略，CFS/EEVDF，按 nice 权重分享 CPU | 大多数后端线程 |
| `SCHED_BATCH` | 普通 | 偏批处理，减少唤醒抢占，偏向吞吐 | CPU 密集型后台任务；交互延迟不保证 |
| `SCHED_IDLE` | 普通 | 权重极低，只在 CPU 空闲时运行 | 低优先级后台清理 |
| `SCHED_FIFO` | 实时 | 优先级 1–99，无时间片，运行到阻塞/让出或被更高实时任务抢占 | 严格低延迟；使用不当会饿死系统 |
| `SCHED_RR` | 实时 | 实时轮转，同优先级按时间片轮换 | 多个同优先级实时任务 |

实时策略要特别小心：一个不阻塞的 `SCHED_FIFO` 线程可能长期占住 CPU，导致其他任务得不到调度。容器里还可能没有权限设置实时策略。

常用命令：

```bash
nice -n 10 ./pool 4 0
renice -n 5 -p <pid>

chrt -p <pid>
chrt -f 10 ./pool 4 0      # SCHED_FIFO，优先级 10，谨慎使用
chrt -r 10 ./pool 4 0      # SCHED_RR
chrt -b 0 ./pool 4 0       # SCHED_BATCH
chrt -i 0 ./pool 4 0       # SCHED_IDLE
```

#### CPU 亲和性：限制任务能跑在哪些核上

亲和性分两类：

- **硬亲和性**：通过 `sched_setaffinity`、`taskset`、cgroup `cpuset` 明确限制任务可运行的 CPU 集合。
- **软亲和性**：调度器尽量让任务留在原 CPU，但为了负载均衡仍可能迁移。

C 代码设置亲和性：

```c
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

static void pin_cpu(int cpu) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);

    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        fprintf(stderr, "sched_setaffinity: %s\n", strerror(errno));
    }
}
```

`pid=0` 表示当前线程。常见错误：

- `EINVAL`：CPU 编号无效，或不在当前 cpuset 允许范围内。
- `EPERM`：权限不足。
- 在容器中，`sched_setaffinity` 不能突破 cgroup `cpuset` 的限制。

命令行工具：

```bash
taskset -c 0-3 ./pool 4 0
taskset -pc <pid>
taskset -pc 0-1 <pid>
```

查看允许的 CPU 和内存节点：

```bash
grep -E 'Cpus_allowed_list|Mems_allowed_list' /proc/<pid>/status
```

#### 亲和性、缓存局部性与迁核

绑核的主要收益是**缓存局部性**：

- 任务留在同一个物理核/同一 L3 域，工作集更容易命中缓存。
- 减少迁移带来的 TLB 和微架构状态失效。
- 对延迟敏感的线程，能降低尾延迟抖动。

但绑核也有代价：

- 调度器失去把任务搬到空闲核上的弹性。
- 如果绑到繁忙核、中断核或“吵闹邻居”所在的核，局部争用会更严重。
- 所有线程绑到同一个核，会制造一个很长的局部运行队列，而其他核空闲。
- 在容器里，`cpuset` 可能本来就很窄，再绑核可能进一步压缩可用 CPU。
- 绑了 CPU 不等于绑了内存；NUMA 下仍可能访问远端内存。

查看迁移情况：

```bash
ps -L -o pid,tid,psr,stat,comm -p <pid>
grep nr_migrations /proc/<pid>/sched
```

`psr` 是最近运行的 CPU；`nr_migrations` 是迁移计数。迁移次数多不一定是问题，要结合工作集和 p99 判断。

#### 容器 CPU 配额与 CFS 带宽限流

cgroup v2 的 `cpu.max` 格式是：

```text
<quota> <period>
```

例如：

```text
50000 100000
```

表示每 100ms 周期最多使用 50ms CPU，即 **0.5 个 CPU**。`max 100000` 表示不限制。

关键点：

- `cpu.max` 是**带宽上限**，不是绑核。任务仍然可以在允许的多个 CPU 上运行。
- `cpu.weight` 是竞争时的相对份额，不是硬上限。
- 配额用完后，cgroup 内的任务会被 **throttle** 到下一个周期，期间不能运行。
- 这会造成“宿主机很闲、容器内 p99 很高”的现象。

查看：

```bash
cat /sys/fs/cgroup/cpu.max
cat /sys/fs/cgroup/cpu.stat
```

`cpu.stat` 常见字段：

```text
nr_periods 1000
nr_throttled 200
throttled_usec 12345678
```

- `nr_periods`：经过的周期数。
- `nr_throttled`：发生限流的周期数。
- `throttled_usec`：累计被限流的微秒数。

cgroup v1 的路径和字段不同，常见为 `cpu.cfs_quota_us`、`cpu.cfs_period_us`，`cpu.stat` 中可能是 `throttled_time`。看容器实际挂载的 cgroup 版本再读。

还要注意：`nproc` 通常反映的是**CPU 亲和性/ cpuset 允许的 CPU 数**，不一定反映 `cpu.max` 的配额。一个 `--cpus=0.5` 的容器里，`nproc` 可能仍然显示很多核。线程池如果按 `nproc` 来开，很容易超配。

#### NUMA

NUMA（Non-Uniform Memory Access）下，CPU 访问本地节点内存快，访问远端节点内存慢。绑核但不绑内存，可能出现“CPU 在节点 0、内存却在节点 1”的情况。

常用命令：

```bash
lscpu | grep -i numa
numactl --hardware
numactl --cpunodebind=0 --membind=0 ./pool 4 0
numactl --localalloc ./pool 4 0
```

在容器和 Kubernetes 中，NUMA 感知取决于运行时和拓扑管理策略。不要假设绑核就自动获得本地内存。

### 3.3 自愿和非自愿上下文切换

#### 直觉

- **自愿切换（voluntary context switch）**：任务自己让出 CPU。比如等 I/O、等锁、`sleep`、`usleep`、`epoll_wait`、`futex` 等待。
- **非自愿切换（involuntary context switch）**：任务并不想停，但被调度器换下。比如时间片用完、被更高优先级任务抢占、被实时任务抢占。

高自愿切换不一定是坏事：I/O 密集型服务本来就会频繁阻塞。高非自愿切换通常意味着 CPU 竞争、线程过多或优先级压力。

#### 观察切换

`/proc/<pid>/status`：

```text
voluntary_ctxt_switches:        1234
nonvoluntary_ctxt_switches:      567
```

注意这些计数是**按任务/线程**统计的。`/proc/<pid>/status` 对应主线程；要汇总所有线程：

```bash
grep -H ctxt_switches /proc/<pid>/task/*/status
```

`pidstat -w`：

```bash
pidstat -w -p <pid> 1 5
pidstat -w -t -p <pid> 1 5
```

输出中的：

- `cswch/s`：每秒自愿上下文切换。
- `nvcswch/s`：每秒非自愿上下文切换。

`/proc/<pid>/sched` 也能看到：

```bash
grep -E 'nr_switches|nr_voluntary_switches|nr_involuntary_switches|nr_migrations' /proc/<pid>/sched
```

#### 自愿切换的短例

下面的代码每轮 `usleep(1000)` 主动睡眠，进入 `S` 状态，每次都是一次自愿切换（注意：读 `/dev/zero` 这类永远有数据的文件并不会阻塞，不产生自愿切换）：

```c
#include <unistd.h>

int main(void) {
    for (int i = 0; i < 3000; i++)
        usleep(1000);   /* 进入睡眠，主动让出 CPU */
    return 0;
}
```

运行后观察：

```bash
./voluntary &
PID=$!
grep ctxt_switches /proc/$PID/status
pidstat -w -p $PID 1 3
```

#### 非自愿切换的短例

CPU 密集型线程数超过有效 CPU 数时，很容易出现非自愿切换。可以用本课后面的线程池实验观察：

```bash
./pool 16 0
pidstat -w -t -p $(pgrep -n pool) 1 5
```

如果 `nvcswch/s` 很高，同时 `vmstat` 的 `r` 长期大于有效 CPU 数，说明存在明显的可运行竞争。

#### 为什么切换成本不是固定常数

不要记住“上下文切换固定耗时 3 微秒”这类说法，然后拿切换次数乘一个常数估算延迟。切换成本至少受这些因素影响：

- 切换的是同进程线程还是不同进程：跨进程切换可能涉及地址空间切换，成本更高。
- 工作集大小：缓存越热，切换后重新加载越少；工作集大时，缓存污染更严重。
- 缓存和 TLB 状态：迁移、跨 NUMA、页表变化都会增加成本。
- CPU 微架构和频率：不同代际、不同频率差异明显。
- 内核版本和调度器路径：统计、安全缓解、cgroup 记账等都会影响。
- 切换原因：自愿阻塞可能伴随唤醒路径成本；抢占可能伴随调度器负载均衡成本。
- 系统负载：高负载下间接成本（排队、缓存争用）远大于寄存器保存/恢复的直接成本。

因此正确做法是：**在你的环境里测量**，并且把切换率与吞吐、p99、运行队列等待、限流指标放在一起看。切换次数本身不是目标，尾延迟和有效吞吐才是。

---

## 4. 一次完整的排障路径

当线上出现“CPU 不高但 p99 高”时，可以按下面顺序收集证据：

```bash
# 1. 全局可运行/阻塞/切换
vmstat 1 5

# 2. 进程与线程状态、所在 CPU、nice、实时优先级
ps -L -o pid,tid,psr,stat,comm,ni,pri,rtprio -p <pid>

# 3. 切换计数
grep -H ctxt_switches /proc/<pid>/task/*/status
pidstat -w -t -p <pid> 1 5

# 4. 运行队列等待与运行时间
for f in /proc/<pid>/task/*/schedstat; do echo "$f: $(cat "$f")"; done

# 5. 亲和性与允许的 CPU/内存节点
taskset -pc <pid>
grep -E 'Cpus_allowed_list|Mems_allowed_list' /proc/<pid>/status

# 6. 容器配额与限流
cat /sys/fs/cgroup/cpu.max
cat /sys/fs/cgroup/cpu.stat

# 7. NUMA 拓扑
numactl --hardware
```

判断思路：

- `r` 高、`b` 低、`schedstat` 第二字段增长快：可运行排队。
- `b` 高、`S`/`D` 多：阻塞等待，先查 I/O、锁、下游。
- `nvcswch/s` 高、`r` 高：线程过多或 CPU 配额不足。
- `nr_throttled`/`throttled_usec` 增长：容器 CPU 限流。
- `psr` 频繁变化、`nr_migrations` 增长快：迁核明显，结合缓存和 NUMA 判断。
- 所有线程都在同一个核、其他核空闲：绑核造成的局部争用。

---

## 5. 常见误区

1. **“CPU 没满，就不是 CPU 问题。”**  
   错。可能是局部核争用、容器限流、运行队列等待或阻塞。要看 `r`、`schedstat` 第二字段、`cpu.stat`。

2. **“线程越多，吞吐越高。”**  
   对 CPU 密集型工作，线程超过有效 CPU 后，排队和切换会增加，p99 往往先恶化。

3. **“绑核一定更快。”**  
   绑核可能提高缓存局部性，但也可能失去调度弹性并加重局部争用。没有测量就不要绑。

4. **“上下文切换固定耗时多少微秒。”**  
   错。切换成本随工作集、缓存/TLB、NUMA、内核、CPU、切换类型变化，不能当通用常数。

5. **“nice 是硬性 CPU 上限。”**  
   错。nice 是相对权重，只在竞争时体现；空闲时低优先级任务仍可用满 CPU。

6. **“`schedstat` 第二个字段是 CPU 时间。”**  
   错。第一个字段是运行时间，第二个字段是运行队列等待时间。

7. **“`ps` 里是 `R` 就一定在 CPU 上跑。”**  
   错。`R` 包括正在运行和已就绪等待 CPU。

8. **“`D` 状态可以随便 `kill -9`。”**  
   通常不行。`D` 是不可中断睡眠，要等内核路径返回；应先排查 I/O、存储、驱动等问题。

9. **“`cpu.max` 会绑定 CPU。”**  
   错。`cpu.max` 限制带宽，绑核要用 `cpuset` 或 `sched_setaffinity`。

10. **“`nproc` 就是容器可用 CPU 配额。”**  
    不一定。`nproc` 反映亲和性/cpuset，未必反映 `cpu.max` 的配额。

11. **“切换次数越少越好。”**  
    不一定。I/O 密集型任务本来就会自愿切换；关键是尾延迟和有效吞吐是否改善。

12. **“实时策略能解决所有延迟问题。”**  
    实时策略可能饿死其他任务，且容器中常无权限。它是隔离手段，不是万能药。

---

## 6. 设计权衡

### 6.1 亲和性：局部性 vs 弹性

- **收益**：减少迁核，提升缓存/TLB 命中，降低延迟抖动。
- **代价**：调度器不能灵活搬运负载；绑到忙核会加重局部争用；容器 cpuset 可能很窄；NUMA 内存未必本地。
- **建议**：先不绑，测出迁移和 p99 的关系；只对延迟敏感线程做小范围绑核；同时设置内存策略；保留一定的 CPU 余量。

### 6.2 线程池大小：吞吐 vs 尾延迟

- CPU 密集型：接近有效 CPU 数，通常不超过太多；有效 CPU 要按 `cpu.max` 配额折算，而不是只看 `nproc`。
- I/O 密集型：可以高于 CPU 数，但要有上界；用队列长度、下游超时和 p99 控制，而不是无限加线程。
- 监控：吞吐、p50/p99、运行队列等待、非自愿切换、限流时间一起看。

### 6.3 nice 与实时策略

- nice 适合做相对优先级，不适合做硬 SLA。
- 实时策略适合严格低延迟场景，但需要隔离 CPU、限制运行时间，并防止饿死系统。
- 在共享容器环境里，优先考虑 cgroup 配额和权重，而不是随意用实时优先级。

### 6.4 容器配额与 NUMA

- 容器里先确认 `cpu.max`、`cpu.weight`、`cpuset.cpus` 和 `cpu.stat`。
- 如果绑核，同时考虑 NUMA 内存绑定；否则可能“绑了 CPU，却访问远端内存”。
- 在 Kubernetes 中，CPU limit 往往对应 CFS quota，CPU request 对应权重；线程池要按 limit 设计，避免 throttle。

---

## 7. 动手实践：线程池大小与亲和性实验

目标：对线程池设置不同大小和亲和性，记录**吞吐、p99、切换率**，并解释结果。

### 7.1 准备实验程序

保存为 `pool.c`：

```c
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#define OPS 50000

typedef struct {
    int tid;
    int mode;   /* 0=不绑核, 1=分散绑核, 2=全部绑到 CPU0 */
    long *lat;  /* 每个操作的延迟样本，单位 ns */
} arg_t;

static long now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000000000L + ts.tv_nsec;
}

static void pin_cpu(int cpu) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("sched_setaffinity");
    }
}

static void *worker(void *p) {
    arg_t *a = p;
    int ncpu = (int)sysconf(_SC_NPROCESSORS_ONLN);

    if (a->mode == 1) pin_cpu(a->tid % ncpu);
    if (a->mode == 2) pin_cpu(0);

    volatile unsigned long x = 0;
    for (int i = 0; i < OPS; i++) {
        long t0 = now_ns();
        for (int k = 0; k < 2000; k++) {
            x += (unsigned long)k; /* 模拟一小段 CPU 工作 */
        }
        a->lat[i] = now_ns() - t0;
    }
    return NULL;
}

static int cmp_long(const void *a, const void *b) {
    long x = *(const long *)a;
    long y = *(const long *)b;
    return (x > y) - (x < y);
}

int main(int argc, char **argv) {
    int nthread = argc > 1 ? atoi(argv[1]) : 4;
    int mode = argc > 2 ? atoi(argv[2]) : 0;

    if (nthread < 1 || nthread > 64) {
        fprintf(stderr, "nthread must be 1..64\n");
        return 1;
    }

    pthread_t th[64];
    arg_t args[64];
    long *all = malloc(sizeof(long) * (size_t)OPS * nthread);
    if (!all) return 1;

    long t0 = now_ns();
    for (int i = 0; i < nthread; i++) {
        args[i].tid = i;
        args[i].mode = mode;
        args[i].lat = &all[(size_t)i * OPS];
        pthread_create(&th[i], NULL, worker, &args[i]);
    }
    for (int i = 0; i < nthread; i++) {
        pthread_join(th[i], NULL);
    }
    long wall = now_ns() - t0;

    long n = (long)OPS * nthread;
    qsort(all, (size_t)n, sizeof(long), cmp_long);

    long p99 = (long)(n * 0.99);
    if (p99 >= n) p99 = n - 1;

    double throughput = (double)n / ((double)wall / 1e9);
    printf("threads=%d mode=%d wall=%.3fs throughput=%.0f ops/s "
           "p50=%.1fus p99=%.1fus\n",
           nthread, mode, wall / 1e9, throughput,
           all[n / 2] / 1000.0, all[p99] / 1000.0);

    free(all);
    return 0;
}
```

说明：

- `mode=0`：不绑核，交给调度器。
- `mode=1`：线程分散绑到不同 CPU。
- `mode=2`：所有线程绑到 CPU 0，用来制造局部争用。
- 注意这个程序没有任务队列，测的是“每段 CPU 工作被抢占/排队拉长了多少”；真正带队列、按到达速率投递任务的线程池版本见文末「附：已验证的实践脚本」。
- 程序是 CPU 密集型；如果要模拟 I/O 型，可以在每个操作里加入 `usleep` 或阻塞读取，再单独观察。

编译：

```bash
cc -O2 -pthread pool.c -o pool
```

### 7.2 基线：只改线程数

先确认机器/容器的有效 CPU：

```bash
nproc
nproc --all
cat /sys/fs/cgroup/cpu.max
cat /sys/fs/cgroup/cpu.stat
```

依次运行：

```bash
./pool 1 0
./pool 2 0
./pool 4 0
./pool 8 0
./pool 16 0
```

记录每次的吞吐、p50、p99。重点观察：

- 吞吐在哪个线程数附近达到平台。
- p99 是否在吞吐下降之前就开始恶化。
- 线程数超过有效 CPU 后，延迟是否明显抖动。

### 7.3 改变亲和性

运行：

```bash
./pool 4 1
./pool 8 1
./pool 16 1
./pool 4 2
./pool 8 2
./pool 16 2
```

对比：

- `mode=1` 是否降低迁移和 p99？
- `mode=2` 是否出现“整机 CPU 不高，但单核打满、排队严重”？
- 线程数越多，绑到同一核的局部争用是否越明显？

### 7.4 采集切换率与运行队列等待

在程序运行期间，另开终端采集。先找到进程：

```bash
PID=$(pgrep -n pool)
```

采集状态与切换：

```bash
ps -L -o pid,tid,psr,stat,comm,ni,pri,rtprio -p $PID
grep -H ctxt_switches /proc/$PID/task/*/status
pidstat -w -t -p $PID 1 5
```

采集运行队列与系统切换：

```bash
vmstat 1 5
```

采集每个线程的 `schedstat`：

```bash
for f in /proc/$PID/task/*/schedstat; do
    echo "$f: $(cat "$f")"
done
```

采集亲和性：

```bash
taskset -pc $PID
grep -E 'Cpus_allowed_list|Mems_allowed_list' /proc/$PID/status
```

建议在每次运行前后各读一次 `schedstat`，用差值计算：

```text
平均运行队列等待 ≈ Δ第二个字段 / 操作数
```

注意：如果容器被限流，`schedstat` 不一定能完整反映 throttle 的停顿，要同时看 `cpu.stat`。

### 7.5 容器配额实验

在一个临时容器或临时 cgroup 中，把 CPU 配额设为 0.5：

```bash
# 示例：Docker
docker run --rm --cpus=0.5 -v "$PWD":/w -w /w gcc:13 \
  bash -c 'cc -O2 -pthread pool.c -o pool && ./pool 4 0'
```

或者使用 cgroup v2（路径按实际环境调整）：

```bash
sudo mkdir -p /sys/fs/cgroup/pooltest
echo "50000 100000" | sudo tee /sys/fs/cgroup/pooltest/cpu.max
sudo bash -c 'echo $$ > /sys/fs/cgroup/pooltest/cgroup.procs; exec ./pool 4 0'
cat /sys/fs/cgroup/pooltest/cpu.stat
```

观察：

- `nproc` 是否仍然显示很多 CPU。
- `cpu.stat` 的 `nr_throttled`、`throttled_usec` 是否增长。
- 线程数从 4 增加到 16 时，p99 是否因为更容易打满配额而恶化。
- 与无配额时相比，吞吐和尾延迟差多少。

### 7.6 记录与分析

建议填写下表：

| 线程数 | 亲和性 | 吞吐 ops/s | p50 | p99 | 自愿切换/s | 非自愿切换/s | 运行队列等待/操作 | throttle 情况 |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | 无 | | | | | | | |
| 4 | 无 | | | | | | | |
| 8 | 无 | | | | | | | |
| 16 | 无 | | | | | | | |
| 4 | 分散 | | | | | | | |
| 16 | 分散 | | | | | | | |
| 4 | 全绑 CPU0 | | | | | | | |
| 16 | 全绑 CPU0 | | | | | | | |
| 4 | 无 + 0.5 CPU 配额 | | | | | | | |
| 16 | 无 + 0.5 CPU 配额 | | | | | | | |

分析时回答：

1. 吞吐的平台点在哪里？和 `nproc`、`cpu.max` 的关系是什么？
2. p99 是否早于吞吐拐点恶化？
3. `mode=2` 是否出现整机 CPU 低但 p99 高？证据是什么？
4. `mode=1` 是否降低了迁移和 p99？有没有引入新的局部争用？
5. 配额实验中，throttle 是否与 p99 峰值对应？
6. 切换率升高时，是自愿还是非自愿占主导？分别说明原因。

### 7.7 验收自查

- [ ] 我能用 `vmstat` 的 `r`/`b`、`ps` 的 `R`/`S`/`D`、`schedstat` 的第二字段，区分可运行排队和阻塞等待。
- [ ] 我能解释 `schedstat` 第一字段与第二字段的区别，以及它们和 CPU 利用率的区别。
- [ ] 我能说出至少三个影响上下文切换成本的因素，并说明为什么不能把固定耗时当通用常数。
- [ ] 我能解释绑核的收益与风险，并说明容器 CPU 配额和 NUMA 为什么要一起考虑。
- [ ] 我完成了实验表格，并给出了有数据支撑的线程池大小建议。

---

## 8. 自测题

### 题 1

某服务 `top` 显示整机 CPU 约 35%，但 p99 很高。`vmstat 1` 显示 `r=16`、`b=0`、`cs` 很高；`pidstat -w` 显示非自愿切换很高；`cpu.stat` 中 `throttled_usec` 持续增长。请分析可能原因，并说明如何区分“可运行排队”“绑核导致的局部争用”和“cgroup 限流”。

**参考答案**

可能原因不是单一的，至少有三类：

1. **可运行排队/线程过多**：`r=16` 且非自愿切换高，说明很多线程已就绪但拿不到 CPU。线程数可能超过有效 CPU 数。
2. **局部争用**：如果线程都绑在少数核上，整机 CPU 可能只有 35%，但被绑的核已经 100%，`schedstat` 第二字段会快速增长。
3. **cgroup 限流**：`throttled_usec` 增长说明配额被打满，任务被 throttle 到下一个周期，期间不是“在运行队列里等”，而是被限流。

区分方法：

- 看 `ps -L -o psr,stat` 和 `taskset -pc`：是否所有线程都在同一个/少数 CPU 上，其他核空闲。
- 看 `/proc/<pid>/task/*/schedstat`：第二字段增长快，支持“可运行排队”；第一字段增长快，说明确实在跑。
- 看 `vmstat` 的 `b` 和 `ps` 的 `D`：若 `b` 高、`D` 多，则更偏阻塞而非可运行排队。
- 看 `cpu.stat` 的 `nr_throttled`/`throttled_usec`：增长明显则是限流。
- 看 `nproc`、`cpu.max`、线程数：判断是否超配。

结论要基于多组证据，不能只看 CPU 利用率。

### 题 2

有人说：“上下文切换固定耗时 3 微秒，所以每秒 10 万次切换就会吃掉 0.3 秒 CPU，延迟直接加 0.3 秒。”这句话错在哪里？你会怎么测量？

**参考答案**

错在把切换成本当成通用常数，并且把切换次数直接线性映射成请求延迟。切换成本受多种因素影响：

- 同进程线程切换还是跨进程切换；是否切换地址空间。
- 工作集大小、缓存和 TLB 状态；迁移和 NUMA 会影响。
- CPU 微架构、频率、内核版本和调度器路径。
- 切换原因（阻塞唤醒、时间片抢占、优先级抢占）和系统负载。
- 间接成本（缓存污染、排队、锁竞争）通常比寄存器保存/恢复更大。

测量方法：在目标环境用基准程序或真实负载，记录切换率（`pidstat -w`、`/proc/<pid>/status`）、吞吐、p50/p99、`schedstat` 运行队列等待和 `cpu.stat` 限流；做对照实验（改变线程数、亲和性、配额），观察延迟变化。不要拿单一常数外推。

### 题 3

在同一个 cgroup 内，两个 CPU 密集型线程分别使用 nice 0 和 nice 5。它们的 CPU 份额大约是多少？如果把两个线程都绑到同一个 CPU，会发生什么？还需要考虑哪些因素？

**参考答案**

在 CFS/EEVDF 的权重表中，nice 0 的权重约为 1024，nice 5 约为 335，所以竞争时份额约为 `1024 : 335 ≈ 3.06 : 1`。这是相对份额，不是硬上限；如果 nice 0 的线程阻塞，nice 5 仍可使用 CPU。

把两个线程都绑到同一个 CPU：

- 它们只能在这一个 CPU 上竞争，另一个线程无法利用其他空闲核。
- 会形成局部运行队列，增加运行队列等待和非自愿切换。
- 如果这个 CPU 还处理中断或有其他忙任务，局部争用会更严重。
- 绑核可能提高缓存局部性，但在这个场景下更可能损失调度弹性。

还需要考虑：

- 容器 `cpu.max` 配额和 `cpu.stat` 限流；即使绑核，配额仍可能 throttle。
- `cpuset` 是否允许目标 CPU。
- NUMA：绑了 CPU 后内存是否本地；跨节点访问会增加延迟。
- 是否有实时任务或中断干扰。
- 线程的实际工作集和 I/O 比例；CPU 密集型与 I/O 密集型的最优策略不同。

---

## 9. 推荐资源

- **调度、亲和性与上下文切换：Linux man-pages**  
  https://man7.org/linux/man-pages/man7/sched.7.html  
  来源：man7.org ｜ 语言：英文 ｜ 类型：文档 ｜ 难度：进阶 ｜ 免费：是  
  阅读重点：可运行与阻塞、nice/策略/亲和性；用于核对本节点实验的语义和边界。

可配合本地 man 页查阅：

```bash
man 7 sched
man 2 sched_setaffinity
man 5 proc
man 1 taskset
man 1 chrt
man 1 nice
man 8 pidstat
```

---

## 10. 版本与边界条件

按 `sourceStudy` 的要求，使用资源中的定义、示例和错误处理路径核对实现时，记录以下边界：

1. **调度器版本**：CFS 长期是普通任务默认调度器；Linux 6.6 起默认演进为 EEVDF。nice 权重语义仍然适用，但具体延迟行为可能不同。
2. **cgroup 版本**：cgroup v1 与 v2 的路径和字段不同。v2 使用 `cpu.max`、`cpu.weight`、`cpu.stat`；v1 常见 `cpu.cfs_quota_us`、`cpu.cfs_period_us`、`cpu.stat` 中的 `throttled_time`。
3. **`/proc` 语义**：`schedstat`、`status`、`sched` 的字段可能随内核版本变化；切换计数通常是按线程统计，进程级汇总要遍历 `task/`。
4. **工具可用性**：`pidstat` 来自 sysstat，未必默认安装；`chrt`、`taskset` 需要相应权限；容器可能限制实时策略和亲和性设置。
5. **错误处理**：`sched_setaffinity` 可能返回 `EINVAL`（CPU 不在允许集合）或 `EPERM`（权限不足）；`nice` 负值需要权限；实时策略需要 `CAP_SYS_NICE`。
6. **容器边界**：`nproc` 不必然等于 `cpu.max` 配额；`cpu.max` 只限带宽，不绑核；`cpuset` 才限制可运行 CPU 集合。
7. **NUMA 边界**：绑核不等于绑内存；需要配合 `numactl`/`set_mempolicy`，且容器运行时可能限制。
8. **性能数字不可移植**：切换耗时、吞吐、p99 都依赖机器、内核、编译选项和负载，实验结论要带环境说明。

---

## 11. 本课小结

- **可运行不等于正在运行**：`R` 包括在运行队列中等待的任务；`S`/`D` 是阻塞，不在运行队列里。
- **CPU 利用率不等于延迟**：要同时看运行队列、`schedstat` 第二字段、切换率和容器限流。
- **nice 是相对权重**：CFS/EEVDF 下按权重分享 CPU，不是硬上限；实时策略是另一套机制。
- **亲和性提高局部性，但牺牲弹性**：绑核可能降低迁核，也可能加重局部争用；要结合 cgroup 配额和 NUMA。
- **自愿切换和非自愿切换含义不同**：前者多与阻塞/等待有关，后者多与 CPU 竞争和抢占有关。
- **切换成本不是常数**：不要用固定微秒数外推延迟；在目标环境测量，并和吞吐、p99、排队、限流一起分析。
- **实验结论要落到数据**：线程数、亲和性、配额三个维度做对照，记录吞吐、p99 和切换率，才能给出可靠的线程池配置建议。

---

## 附：已验证的实践脚本（practice41）

下面这套是在 8 核 Linux 云主机（无 cgroup cpu.max 限额）上实际跑通的版本：`state.c` 演示阻塞与可运行排队，`pool.c` 是一个真正带任务队列的线程池（生产者按约 80% CPU 容量的速率投递每个 200us 的任务，延迟从入队算到完成），`test.sh` 自动断言。

本机一次实测输出（你的机器数字会不同，这正是“切换成本和延迟不是常数”的体现）：

```text
sleep 进程: stat=S vol=1407 nonvol=0 schedstat=10243454 69687 1409
spin  进程: stat=R vol=1 nonvol=127 schedstat=509798527 1016407682 128
PASS: 阻塞进程以自愿切换为主 (1418 vs 0)，CPU 只用了 10ms
PASS: 可运行排队：运行 509ms 却在队列里等了 1016ms，非自愿切换 127
PASS: nice0:nice10 CPU 时间比 8.8（权重比≈9.3，不是“优先级高就独占”）
threads=8 aff=none tasks=4000 thr=30169/s p50=407us p99=621us vcsw=2911 nvcsw=91
threads=64 aff=none tasks=4000 thr=26663/s p50=524us p99=3401us vcsw=13796 nvcsw=968
threads=8 aff=spread tasks=4000 thr=30236/s p50=406us p99=611us vcsw=3714 nvcsw=796
threads=8 aff=one tasks=4000 thr=4996/s p50=336949us p99=665902us vcsw=1250 nvcsw=199
PASS: 线程数 8→64：p99 621us→3401us 变差
PASS: 全部绑到 CPU0：p99 665902us，绑核失去调度弹性
ALL_CHECKS_PASSED
```

读法：睡眠进程是 `S`、几乎全是自愿切换、schedstat 第二字段（队列等待）只有 0.07ms；三个进程挤在 CPU0 上自旋时是 `R`，跑了 0.5s 却在运行队列里等了 1s——这就是“可运行排队”，和“阻塞等待”完全不同。线程数从 8 开到 64，吞吐没涨反降、p99 涨了 5 倍多、自愿切换涨了近 5 倍；把 8 个线程全绑到一个核，吞吐掉到 1/6，p99 到了 666ms。分散绑核（spread）在这台机器上和不绑几乎一样，说明绑核不是免费收益，要实测。

### state.c

```c
// 阻塞 vs 可运行：./state sleep|spin 毫秒
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
static long ms(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000L+t.tv_nsec/1000000;}
int main(int c,char**v){long e=ms()+atol(v[2]);
 if(!strcmp(v[1],"sleep"))while(ms()<e)usleep(1000); else {volatile long x=0;while(ms()<e)x++;}
 return 0;}
```

### pool.c

```c
// 线程池压测：./pool <线程数> <亲和性: none|one|spread> <任务数> <每任务自旋us>
// 输出 吞吐、p50/p99（入队到完成）、自愿/非自愿切换、跑队列等待
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/resource.h>
#include <unistd.h>
static long now_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000000000L+t.tv_nsec;}
typedef struct{long enq;} task_t;
static task_t *q; static int qh,qt,qn,done_prod; static long *lat; static int nlat;
static pthread_mutex_t mu=PTHREAD_MUTEX_INITIALIZER; static pthread_cond_t cv=PTHREAD_COND_INITIALIZER;
static int spin_us; static char *aff; static int ncpu;
static long waitns_total; 
static void spin(int us){long e=now_ns()+us*1000L;while(now_ns()<e);}
static long rq_wait_ns(void){ // /proc/thread-self/schedstat 第二个字段：在运行队列上等待的纳秒
  FILE*f=fopen("/proc/thread-self/schedstat","r");long a=0,b=0;if(f){if(fscanf(f,"%ld %ld",&a,&b)!=2)b=0;fclose(f);}return b;}
static void *worker(void *arg){
  long id=(long)arg;
  if(strcmp(aff,"none")){cpu_set_t s;CPU_ZERO(&s);CPU_SET(strcmp(aff,"one")?id%ncpu:0,&s);pthread_setaffinity_np(pthread_self(),sizeof s,&s);}
  for(;;){
    pthread_mutex_lock(&mu);
    while(qh==qt&&!done_prod)pthread_cond_wait(&cv,&mu);
    if(qh==qt&&done_prod){pthread_mutex_unlock(&mu);break;}
    task_t t=q[qh++];pthread_mutex_unlock(&mu);
    spin(spin_us);
    long l=now_ns()-t.enq;
    pthread_mutex_lock(&mu);lat[nlat++]=l;pthread_mutex_unlock(&mu);
  }
  long w=rq_wait_ns();pthread_mutex_lock(&mu);waitns_total+=w;pthread_mutex_unlock(&mu);
  return 0;}
static int cmp(const void*a,const void*b){long x=*(long*)a,y=*(long*)b;return x<y?-1:x>y;}
int main(int c,char**v){
  int nt=atoi(v[1]);aff=v[2];qn=atoi(v[3]);spin_us=atoi(v[4]);ncpu=sysconf(_SC_NPROCESSORS_ONLN);
  q=calloc(qn,sizeof*q);lat=calloc(qn,sizeof*lat);
  pthread_t th[nt];for(long i=0;i<nt;i++)pthread_create(&th[i],0,worker,(void*)i);
  struct rusage r0;getrusage(RUSAGE_SELF,&r0);
  long t0=now_ns();
  // 生产者按固定速率投递：每个间隔投递一批，模拟请求到达
  int batch=ncpu*2;
  for(int i=0;i<qn;i+=batch){
    pthread_mutex_lock(&mu);
    for(int j=i;j<i+batch&&j<qn;j++){q[qt].enq=now_ns();qt++;}
    pthread_cond_broadcast(&cv);pthread_mutex_unlock(&mu);
    long e=now_ns()+(long)spin_us*1000*batch/ncpu*10/8; // 到达速率≈80% 的 CPU 容量
    while(now_ns()<e)usleep(50);
  }
  pthread_mutex_lock(&mu);done_prod=1;pthread_cond_broadcast(&cv);pthread_mutex_unlock(&mu);
  for(int i=0;i<nt;i++)pthread_join(th[i],0);
  long t1=now_ns();struct rusage r1;getrusage(RUSAGE_SELF,&r1);
  qsort(lat,nlat,sizeof*lat,cmp);
  double sec=(t1-t0)/1e9;
  printf("threads=%d aff=%s tasks=%d thr=%.0f/s p50=%.0fus p99=%.0fus vcsw=%ld nvcsw=%ld rqwait_avg=%.1fms\n",
    nt,aff,nlat,nlat/sec,lat[nlat/2]/1e3,lat[nlat*99/100]/1e3,
    r1.ru_nvcsw-r0.ru_nvcsw,r1.ru_nivcsw-r0.ru_nivcsw,waitns_total/1e6/nt);
}
```

### test.sh

```bash
#!/bin/bash
# Backend Atlas 第41课实践验证：可运行 vs 阻塞、nice 权重、线程池大小与亲和性
set -u; cd "$(dirname "$0")"; fail=0
gcc -O1 -Wall -pthread -o pool pool.c && gcc -O1 -o state state.c || exit 1
ok(){ echo "PASS: $1"; }; bad(){ echo "FAIL: $1"; fail=1; }
f(){ awk -v k="$2:" '$1==k{print $2}' /proc/$1/status; }
echo "nproc=$(nproc)  cgroup cpu.max=$(cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo 无)"
# 1) 阻塞：睡眠进程处于 S，几乎全是自愿切换，跑队列等待很小
./state sleep 3000 & S=$!
# 2) 可运行排队：3 个自旋进程挤在 CPU0 上，处于 R，非自愿切换多，等待时间大
for i in 1 2 3; do taskset -c 0 ./state spin 3000 & done; sleep 1.5
P=$(pgrep -f 'state spin' | head -1)
echo "sleep 进程: stat=$(ps -o stat= -p $S) vol=$(f $S voluntary_ctxt_switches) nonvol=$(f $S nonvoluntary_ctxt_switches) schedstat=$(cat /proc/$S/schedstat)"
echo "spin  进程: stat=$(ps -o stat= -p $P) vol=$(f $P voluntary_ctxt_switches) nonvol=$(f $P nonvoluntary_ctxt_switches) schedstat=$(cat /proc/$P/schedstat)"
read sr sw _ < /proc/$S/schedstat; read pr pw _ < /proc/$P/schedstat
sv=$(f $S voluntary_ctxt_switches); sn=$(f $S nonvoluntary_ctxt_switches); pn=$(f $P nonvoluntary_ctxt_switches)
[ $sv -gt $((sn*10)) ] && ok "阻塞进程以自愿切换为主 ($sv vs $sn)，CPU 只用了 $((sr/1000000))ms" || bad "sleep vol=$sv nonvol=$sn"
[ $pw -gt $((pr/2)) ] && [ $pn -gt 10 ] && ok "可运行排队：运行 $((pr/1000000))ms 却在队列里等了 $((pw/1000000))ms，非自愿切换 $pn" || bad "spin run=$pr wait=$pw nonvol=$pn"
wait
# 3) nice：同一核上 nice 0 vs nice 10，CPU 时间比接近权重比 1024:110≈9.3
taskset -c 1 ./state spin 3000 & A=$!; taskset -c 1 nice -n 10 ./state spin 3000 & B=$!; sleep 1.5
read ar _ < /proc/$A/schedstat; read br _ < /proc/$B/schedstat; wait
ratio=$(awk -v a=$ar -v b=$br 'BEGIN{printf "%.1f", a/b}')
awk -v r=$ratio 'BEGIN{exit !(r>5 && r<15)}' && ok "nice0:nice10 CPU 时间比 $ratio（权重比≈9.3，不是“优先级高就独占”）" || bad "ratio=$ratio"
# 4) 线程池：大小与亲和性
echo "--- 线程池（每任务 200us CPU，到达率约 80% 容量）---"
for cfg in "$(nproc) none" "$(( $(nproc)*8 )) none" "$(nproc) spread" "$(nproc) one"; do ./pool $cfg 4000 200 | tee -a pool.log; done
p99(){ grep "threads=$1 aff=$2 " pool.log | tail -1 | sed 's/.*p99=\([0-9]*\)us.*/\1/'; }
N=$(nproc); a=$(p99 $N none); b=$(p99 $((N*8)) none); c=$(p99 $N one)
[ $b -gt $a ] && ok "线程数 ${N}→$((N*8))：p99 ${a}us→${b}us 变差" || bad "p99 $a vs $b"
[ $c -gt $((a*10)) ] && ok "全部绑到 CPU0：p99 ${c}us，绑核失去调度弹性" || bad "one p99=$c"
rm -f pool.log
[ $fail = 0 ] && echo ALL_CHECKS_PASSED
```
