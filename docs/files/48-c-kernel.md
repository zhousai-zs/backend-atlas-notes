# 第 48/208 课：内核路径与运行时边界

> 所属阶段：第 5 阶段 · 隔离与资源治理  
> 前置课程：c-cgroup、c-elf  
> 关键词：Kernel Internals、内核版本与配置、系统调用路径、ftrace、tracepoint、spinlock、mutex、RCU、进程上下文、软中断、运行时边界

## 0. 本课导读

微信公众号后台的长连接 Gateway，用 `epoll` 管理海量客户端与上游连接；接口服务里又有大量同步 `read`/`write` 去读回调 body、读本地 Unix socket、读管道。某天监控显示 Gateway 的 p99 突然抬升，业务线程池看起来并不饱和，宿主 CPU、内存也“还行”。值班同学开始争论：是用户态 event loop 处理慢了，还是内核没有及时把 socket 就绪事件递上来？另一条线：某个批量读配置的任务卡住，`strace` 里停在 `read`，但到底卡在 VFS、页缓存，还是块设备？

这些问题的共同点是：**可观察现象在用户态，真正的路径边界往往在内核。** 只背“read → vfs_read → …”“epoll_wait → do_epoll_wait → …”的调用链名字不够；要把**当前内核版本与配置**固定下来，从系统调用入口沿源码走进子系统，再用 ftrace / tracepoint 把实际跑过的路径钉死。上一课的 cgroup 告诉你资源预算如何改延迟与故障模式；本课则教你在预算与 ELF/进程边界之上，如何验证网络、内存或调度瓶颈到底卡在哪一段内核路径。

本课从直觉到原理讲清三点：内核版本与配置；系统调用到子系统路径；锁、RCU 与上下文约束。最后选择 `read` 或 `epoll_wait`，固定内核 tag，追踪实现并对照 trace 证据。

## 1. 学习目标

完成本课后，你应当能够：

1. 解释本机（或目标环境）的内核版本字符串含义，并能定位、阅读关键 `CONFIG_*` 配置项（如 ftrace、epoll、相关调试选项）。
2. 从 `read` 或 `epoll_wait` 的系统调用入口出发，沿**当前内核版本**的源码追踪到相关子系统（VFS / eventpoll 等），说清主要函数与数据结构，而不是背一份过时调用链。
3. 理解 spinlock、mutex、RCU 的基本适用场景，以及 process context 与 softirq 等上下文对“能不能睡、能不能拿某把锁”的约束。
4. 用 ftrace / tracepoint（或 `perf trace`）对照一次真实调用，把源码路径与运行时证据连起来。
5. 在报告中标明版本与配置，并能指出哪些结论是 Linux 实现细节，而非 POSIX 保证。

对应节点的验收标准：

- 报告标明版本与配置；
- 能指出哪些结论是 Linux 实现而非 POSIX 保证。

## 2. 为什么后端要懂

**能够验证网络、内存或调度瓶颈的具体原因。** 公众号后台不是“调参玄学场”：长连接推送延迟、回调接口超时、容器内偶发卡顿，都需要你能把现象拆到可证伪的一层。

**验证网络路径。** Gateway 的 p99 升高时，可能是用户态业务慢，也可能是 `epoll_wait` 唤醒路径、socket 接收队列、软中断处理在拖后腿。会沿 `epoll_wait` 看 eventpoll 与唤醒关系，再用 tracepoint 看真实进入/退出，才能避免“只改业务线程数”的无效循环。

**验证内存与 I/O 路径。** 同步 `read` 卡住时，可能在等页缓存填充、在等管道对端、也可能在文件系统或块层。把版本固定后从 `sys_read`/`vfs_read` 往下看，对照 `sys_enter_read` / 相关事件，才能判断是“内核路径问题”还是“对端没数据”。

**验证调度与上下文干扰。** 同一核上 softirq 过重、锁竞争、RCU 宽限期拖长，都会表现为“用户态代码看起来没跑多久，延迟却上去了”。不懂上下文约束，就容易在中断/软中断语境里误用会睡眠的接口，或把 Linux 特有行为当成可移植保证。

**几个真实场景。**

- 公众号长连接 Gateway：连接数上来后 p99 抖，怀疑 `epoll`；需要确认是就绪列表处理慢，还是根本没进 `sys_enter_epoll_wait` 的预期模式。
- 回调接口服务：某次 `read` 大 body 偶发超长，要区分慢客户端、页缓存缺失与 cgroup 内存回收（可结合上一课）。
- 容器节点滚动升级内核后，旧笔记里的函数名对不上，排障文档失效——典型的版本漂移。
- 把 POSIX 文档里的 `read`/`epoll` 语义直接当成 Linux 保证，在边缘行为（如 `epoll` 与 `fork`、边缘触发易漏事件）上踩坑。

## 3. 直觉：先钉住“哪一版内核的哪条路”，再谈调用链

把内核想成一座会改装修的地铁站：

- **版本（uname）** 是站名与通车年份：5.15 和 6.1 可能改线、改站名。
- **配置（CONFIG_*）** 是今天哪些闸机、安检仪实际装上了：没开 `CONFIG_FTRACE`，你就没有那套观测闸机。
- **系统调用入口** 是检票口：用户态 `read`/`epoll_wait` 都从这里进站。
- **子系统路径** 是换乘：VFS、eventpoll、网络协议栈……
- **锁 / RCU / 上下文** 是运行图约束：有的通道禁止停车睡觉（不能 schedule），有的允许多人同时只读扫一眼（RCU）。

**只背调用链 = 背一张不知年份的旧地图。** 正确姿势是：先固定 tag/版本与配置，再从入口追到子系统，最后用 trace 证明“这次运行真的走了这条路”。

| 层次 | 你在回答什么 | 典型工具 |
| --- | --- | --- |
| 版本与配置 | 我面对的是哪套实现？有哪些能力？ | `uname -r`、`/boot/config-*`、`/proc/config.gz` |
| 系统调用→子系统 | 入口之后进了谁的代码？ | 对应 tag 的源码、`SYSCALL_DEFINE*` |
| 运行时证据 | 这次进程是否真的进了这些函数/事件？ | ftrace、tracepoint、`perf trace` |
| 同步与上下文 | 这条路上能不能睡？用什么锁？ | spinlock/mutex/RCU、`in_interrupt()` 等 |

## 4. 内核版本与配置

### 4.1 版本字符串在说什么

```bash
uname -r
# 例：5.15.0-105-generic  或  6.1.0-22-cloud-amd64
uname -a
cat /proc/version
```

常见构成大致是：`VERSION.PATCHLEVEL.SUBLEVEL[-EXTRA]`，发行版还会带上自己的后缀（`-generic`、`-cloud`、厂商定制号）。对排障而言你至少要记下：

1. **完整 `uname -r`**（写进报告）；
2. 若在容器内，确认看的是**宿主内核**（容器一般不自带另一套内核，除非特殊虚拟化）；
3. 若对照源码，尽量找到与之最接近的 **stable tag**（如 `v5.15.105`），而不是随手打开 `master`。

公众号后台若跑在多代内核的混合集群上，同一段“经验贴”可能对一部分机器成立、对另一部分不成立——所以版本是第一验收项。

### 4.2 配置从哪里读

内核编译时的 `CONFIG_*` 决定功能是否编入、编成内建还是模块。常见读取方式：

```bash
# 许多发行版提供与运行内核匹配的配置
ls /boot/config-$(uname -r)
grep -E 'CONFIG_FTRACE|CONFIG_EPOLL|CONFIG_FUNCTION_TRACER|CONFIG_BPF' /boot/config-$(uname -r)

# 若内核启用了 IKCONFIG
zcat /proc/config.gz 2>/dev/null | grep -E 'CONFIG_FTRACE|CONFIG_EPOLL|CONFIG_IKCONFIG'
# 或
cat /proc/config.gz >/dev/null 2>&1 && echo "IKCONFIG available"
```

与本课实践强相关的配置直觉：

- **观测**：`CONFIG_FTRACE`、`CONFIG_FUNCTION_TRACER`、各类 `CONFIG_TRACEPOINTS`、事件相关选项；没有它们，后面的“对照 trace 证据”会缺工具。
- **能力**：`epoll` 在现代通用内核中几乎总是开启；但你仍应养成“先看配置再下结论”的习惯，尤其是裁剪过的嵌入式/定制内核。
- **调试与安全**：某些发行版默认限制 `debugfs`、`kptr_restrict`、`perf_event_paranoid`，会影响你是否看得到符号与 trace。

`make menuconfig` 是编译前的配置界面；生产排障通常**不是**去改宿主 menuconfig，而是**读取正在运行的配置**，确认假设成立。

### 4.3 为何“版本 + 配置”必须写进报告

同一大版本号下，发行版可能 backport 补丁；函数名、静态 inline、trace 事件字段都可能变。验收要求“报告标明版本与配置”，不是形式主义，而是让结论可复现：别人换一台 6.x 机器，不能默认你的 5.15 笔记仍然逐行成立。

## 5. 系统调用到子系统路径

### 5.1 总览：从用户态落到内核

用户态调用 C 库的 `read` / `epoll_wait`，经 syscall 指令（或 vdso/syscall wrapper）进入内核。内核侧用 `SYSCALL_DEFINE*` 宏生成入口，再进入子系统。

粗粒度地图（**具体函数名以你固定的 tag 为准**）：

```text
用户态 read(fd, buf, count)
  → 系统调用入口（如 ksys_read / sys_read 一代命名）
  → vfs_read()
  → file->f_op->read_iter / read 等
  → 具体文件类型：socket、pipe、常规文件、eventfd……

用户态 epoll_wait(epfd, events, maxevents, timeout)
  → 系统调用入口
  → do_epoll_wait() 一类实现（eventpoll 子系统）
  → 等待就绪列表 / 阻塞在 wait queue
  → 被文件就绪、超时或信号唤醒后拷贝事件回用户态
```

节点强调：**从具体系统调用与数据结构入手，连接可观察现象和实现，避免只背调用链。**

### 5.2 以 `read` 为例：入口与 VFS

在常见 Linux 源码树中（路径随版本微调）：

- 声明与宏：`include/linux/syscalls.h`、`fs/read_write.c` 一带；
- `read` 参数是 `(fd, buf, count)`，入口多为 `SYSCALL_DEFINE3(read, ...)`（不要死记成错误的 DEFINE 参数个数）；
- 核心会走到 `vfs_read()`，再根据 `struct file` 的 `f_op` 分派。

你可以在固定 tag 的源码中这样搜：

```bash
# 假设源码在 ~/linux，且已 checkout 到与 uname 接近的 tag
cd ~/linux
git describe --tags
rg -n 'SYSCALL_DEFINE3\(read' fs/ include/
rg -n '^vfs_read\(|vfs_read\(' fs/read_write.c
```

对公众号后台更有意义的分派直觉：

| `fd` 背后是什么 | `read` 往往进入谁 | 卡顿时你可能看到 |
| --- | --- | --- |
| TCP/Unix socket | socket 的 `read_iter` 等 | 对端没数据、接收队列、协议处理 |
| 管道 / eventfd | pipe、eventfd 实现 | 对端未写、缓冲空 |
| 常规文件 | 文件系统 + page cache | 缺页、磁盘、锁 |

**POSIX 保证的是语义轮廓**（例如成功返回字节数、错误码类别）；**Linux 保证的是这棵具体调用树与扩展行为**。例如某些 `flags`、重启语义与具体文件系统行为，不能从 POSIX 一行字推出来。

### 5.3 以 `epoll_wait` 为例：eventpoll 子系统

`epoll` 是 Linux 特有的 I/O 事件多路复用机制（对比：POSIX 有 `poll`/`select`，但没有规定 `epoll`）。长连接 Gateway 几乎必然依赖它。

常见追踪线索（名称随版本变化，见误区）：

```bash
cd ~/linux
rg -n 'SYSCALL_DEFINE.*\(epoll_wait' fs/eventpoll.c
rg -n 'do_epoll_wait|ep_poll\(' fs/eventpoll.c
```

直觉步骤：

1. 校验 `epfd` 对应的 eventpoll 文件；
2. 在超时时间内等待“就绪列表”非空，或超时/信号返回；
3. 把就绪事件拷贝到用户态数组；
4. 边缘触发（`EPOLLET`）下，若用户态没一次读到 EAGAIN，可能长期看不到新事件——这是**使用契约**问题，常被误报成“内核丢事件”。

对照微信公众号长连接场景：连接 socket 被加入 epoll 后，数据到达 → 文件层标记就绪 → 唤醒阻塞在 `epoll_wait` 的线程 → 用户态再 `read`。p99 异常时，要会问：是唤醒慢、是用户态处理慢导致事件堆积，还是根本没走到你以为的那条路径？

### 5.4 用 trace 把“源码路径”钉成“这次真的走了”

源码是静态地图；trace 是行车记录仪。

```bash
# 权限与 debugfs（路径因发行版而异）
sudo mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
ls /sys/kernel/debug/tracing

# 查看与 syscall 相关的事件（名称以本机为准）
sudo cat /sys/kernel/debug/tracing/available_events | grep -E 'sys_enter_read|sys_exit_read|sys_enter_epoll_wait|sys_exit_epoll_wait'

# 使能事件（示例）
echo 0 | sudo tee /sys/kernel/debug/tracing/tracing_on
echo > /sys/kernel/debug/tracing/trace
echo 1 | sudo tee /sys/kernel/debug/tracing/events/syscalls/sys_enter_read/enable
echo 1 | sudo tee /sys/kernel/debug/tracing/events/syscalls/sys_exit_read/enable
# 若追踪 epoll：
# echo 1 | sudo tee /sys/kernel/debug/tracing/events/syscalls/sys_enter_epoll_wait/enable
# echo 1 | sudo tee /sys/kernel/debug/tracing/events/syscalls/sys_exit_epoll_wait/enable
echo 1 | sudo tee /sys/kernel/debug/tracing/tracing_on

# 另开终端制造一次调用后读取
# cat /sys/kernel/debug/tracing/trace | head

# 结束后关闭
echo 0 | sudo tee /sys/kernel/debug/tracing/tracing_on
echo 0 | sudo tee /sys/kernel/debug/tracing/events/syscalls/sys_enter_read/enable
echo 0 | sudo tee /sys/kernel/debug/tracing/events/syscalls/sys_exit_read/enable
```

更省事的用户态封装：

```bash
# 跟踪某进程的 syscall（示例）
perf trace -e raw_syscalls:sys_enter,raw_syscalls:sys_exit -p <pid> -- sleep 2
# 或聚焦名字（工具版本不同，选项略异）
strace -p <pid> -e read,epoll_wait -c
```

**注意：** `set_ftrace_filter` 过滤的是**函数 tracer 的函数名**，不是 `syscalls/sys_enter_*` 这种 trace event。事件应写到 `events/.../enable`，不要把 event 路径误喂给 `set_ftrace_filter`。

## 6. 锁、RCU 与上下文约束

### 6.1 三种同步手段的直觉

| 机制 | 直觉 | 典型约束 |
| --- | --- | --- |
| spinlock | 短临界区，持锁自旋等待 | 持锁期间通常不能睡眠；中断/软中断路径常用 |
| mutex | 可以睡的锁，适合较长临界区 | 进程上下文；不能在原子上下文里盲目 `mutex_lock` |
| RCU | 读多写少：读者极轻量，写者更新后等宽限期 | 读者在 RCU read-side critical section 有规则；用于保护大量只读查找结构 |

内核路径里常见模式：查找结构用 RCU 读；短更新用 spinlock；可能阻塞的资源获取用 mutex。**背名字无意义，要问：这段代码会不会睡？当前能不能睡？**

### 6.2 上下文：能不能睡，决定你能用什么 API

```text
硬中断 → 软中断（softirq）/ tasklet 等 → 进程上下文（syscall、内核线程）
```

粗规则（便于建立边界感，细节以文档与源码注释为准）：

- **进程上下文**（系统调用路径上）：在允许的点可以调度/睡眠；`read` 等对端无数据时阻塞，属于正常现象。
- **原子上下文 / 中断、软中断**：不能睡眠；只能用不会睡的同步原语与分配标志（如 `GFP_ATOMIC` 一类约束）。
- 辅助判断：`in_interrupt()` 等（具体 API 随版本演进，以你的 tag 文档为准）帮助区分“此刻是否在中断相关上下文”。

对后端排障的直接含义：

1. 你在用户态看到的 `read` 阻塞，常常是**故意睡眠等待**，不是死锁；要用 trace 区分“在睡等数据”和“锁/资源异常”。
2. 网络收包大量走 softirq，若 softirq 过重，会表现为延迟升高——问题可能不在你的业务函数行数，而在同核的中断后半部。
3. 在内核模块或错误示例里把会睡的锁拿到软中断里，属于经典事故；用户态工程师也要懂，才能读懂 oops/lockdep 线索。

### 6.3 和 `read` / `epoll_wait` 的关系

- `read` 在无数据且阻塞式 fd 上，会进入可中断/可重启的睡眠等待；这是进程上下文路径。
- `epoll_wait` 在超时前等待事件，同样是可睡眠的等待；唤醒可能来自文件就绪回调（回调本身的上下文要更小心）。
- eventpoll 内部用锁保护就绪列表等结构；版本不同，锁类型与 RCU 用法会调整——再次印证“固定 tag 再读”。

## 7. 常见误区

**不同内核版本的函数名与实现路径会变化。** 这是本节点明确指出的陷阱。

具体表现：

1. 博客里的 `sys_epoll_wait` / `ep_poll` / 某静态函数，在你的 6.x 树里可能改名、inline 掉或拆分。
2. 发行版 backport 让“看起来是 5.15”的树带有更新行为。
3. 把某次 `ftrace` 函数图当成永恒真理，升级内核后脚本全红。

对策：

- 报告写明 `uname -r` 与关键 `CONFIG_*`；
- 源码研究固定 tag（`git checkout vX.Y.Z`）；
- 结论区分：**POSIX 可移植语义** vs **Linux 实现细节**；
- 用本机 trace 事件名（以 `available_events` 为准），不要抄过时路径。

其他连带误区：

- 把 `strace` 看到的 libc 包装语义直接当成内核保证；
- 认为容器里的“内核版本”可以和宿主不同（通常不能）；
- 边缘触发 epoll 用错导致“以为内核丢事件”。

## 8. 设计权衡

**深入源码耗时，应由可证伪的运行时问题驱动。**

权衡双方：

- **深入的收益**：能验证网络/内存/调度瓶颈的具体原因，避免错误加机器、错误加线程。
- **深入的成本**：读内核、搭 trace、对齐版本，非常耗时；无目标的“通读 subsystem”容易沉没。

实践策略（呼应 sourceStudy）：

1. 先有可观测问题（p99、阻塞栈、超时）；
2. 固定内核 tag 与配置；
3. 只从相关系统调用入口追到必要子系统；
4. 用 tracepoint 验证实际路径；
5. 写清哪些是 Linux 特有结论。

对公众号后台：优先追 `epoll_wait`/`read`/`write`/`sendto`/`recvfrom` 等热路径，而不是先去通读整个网络栈。

## 9. 动手实践

目标：选择 `read` 或 `epoll_wait`，沿**当前内核版本**追踪实现并对照 trace 证据。  
验收：报告标明版本与配置；能指出哪些结论是 Linux 实现而非 POSIX 保证。

### 9.1 步骤一：固定版本与配置（必做）

```bash
uname -r | tee lesson48-version.txt
uname -a | tee -a lesson48-version.txt
cat /proc/version | tee -a lesson48-version.txt

CONF=/boot/config-$(uname -r)
if [ -f "$CONF" ]; then
  grep -E 'CONFIG_FTRACE|CONFIG_FUNCTION_TRACER|CONFIG_EPOLL|CONFIG_IKCONFIG|CONFIG_TRACEPOINTS' "$CONF" \
    | tee lesson48-config-snippet.txt
else
  zcat /proc/config.gz 2>/dev/null \
    | grep -E 'CONFIG_FTRACE|CONFIG_FUNCTION_TRACER|CONFIG_EPOLL|CONFIG_IKCONFIG' \
    | tee lesson48-config-snippet.txt || echo "未找到 config，记录发行版文档中的获取方式" | tee lesson48-config-snippet.txt
fi
```

验收点：你有一份写明版本的文件，并记录了与 ftrace/epoll 相关的配置有无。

### 9.2 步骤二：准备对照用的源码 tag

```bash
# 示例：用内核官方树或发行版源码包；以下为示意
# git clone --depth 1 --branch v5.15 https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git
# 将 v5.15 换成与 uname 最接近、你能获取的 tag

cd /path/to/linux
git describe --tags | tee ~/lesson48-kernel-tag.txt
```

若暂时无法完整克隆，可用 [https://elixir.bootlin.com/linux](https://elixir.bootlin.com/linux) 选择接近的版本浏览，但仍须在报告中写明你对照的版本号。

### 9.3 步骤三：选择一条调用并静态追踪

**方案 A：`read`（适合接口读 body、读文件）。**

```bash
cd /path/to/linux
rg -n 'SYSCALL_DEFINE3\(read' 
rg -n 'vfs_read' fs/read_write.c | head
# 继续向下阅读 f_op 分派；针对你关心的 fd 类型（socket/文件）再往下跳
```

**方案 B：`epoll_wait`（适合长连接 Gateway）。**

```bash
cd /path/to/linux
rg -n 'epoll_wait' fs/eventpoll.c | head -40
# 阅读从 SYSCALL_DEFINE* 到等待/就绪列表的主路径
# 记下你跟踪到的函数名（以该 tag 为准）
```

在笔记中画一张**仅含你亲见函数**的路径图，禁止从旧博客整页粘贴。

### 9.4 步骤四：写一个最小用户态触发器

```c
/* save as lesson48_touch.c — 以 read 为例；epoll 版可自行改写 */
#define _GNU_SOURCE
#include <unistd.h>
#include <stdio.h>
#include <string.h>

int main(void) {
  char buf[64];
  ssize_t n = read(0, buf, sizeof(buf)); /* 从 stdin 读，便于控制 */
  if (n < 0) {
    perror("read");
    return 1;
  }
  fprintf(stderr, "read returned %zd\n", n);
  return 0;
}
```

```bash
cc -O0 -g -o lesson48_touch lesson48_touch.c
# 触发：printf 'hello' | ./lesson48_touch
```

`epoll_wait` 版可用一小段创建 `epoll`、加入 timerfd/eventfd、再 `epoll_wait` 的程序（此处从略）；关键是**可重复、可单独 attach**。

### 9.5 步骤五：对照 trace 证据

```bash
# 以 read 为例；需要 root 或相应能力
sudo bash -c '
  TR=/sys/kernel/debug/tracing
  echo 0 > $TR/tracing_on
  echo > $TR/trace
  echo 1 > $TR/events/syscalls/sys_enter_read/enable
  echo 1 > $TR/events/syscalls/sys_exit_read/enable
  echo 1 > $TR/tracing_on
'
printf 'hello' | ./lesson48_touch
sudo bash -c '
  TR=/sys/kernel/debug/tracing
  echo 0 > $TR/tracing_on
  grep -E "sys_(enter|exit)_read" $TR/trace | tail -n 20 | tee lesson48-trace.txt
  echo 0 > $TR/events/syscalls/sys_enter_read/enable
  echo 0 > $TR/events/syscalls/sys_exit_read/enable
'
```

若权限或事件名不同，改用：

```bash
strace -o lesson48-strace.txt -e read ./lesson48_touch <<<'hello'
# 或
perf trace -e syscalls:sys_enter_read,syscalls:sys_exit_read -- ./lesson48_touch <<<'hello'
```

把 trace 片段与步骤三的函数路径写在同一份报告里：**源码说会进 A，trace 显示本次确实发生了 enter/exit（或等价证据）。**

### 9.6 步骤六：区分 Linux 实现与 POSIX 保证（验收）

在报告中增加一小节，至少列出两条对比，例如：

| 结论 | 类别 |
| --- | --- |
| `read` 从 fd 读至多 `count` 字节，返回值语义 | POSIX 层面有规定的可移植语义（细节仍看标准） |
| 经 `vfs_read` → 某 `f_op->read_iter` 的具体函数名与锁 | **Linux 实现**，随版本变 |
| 存在 `epoll_wait` / `EPOLLET` 行为 | **Linux 特有**（非 POSIX 规定的 epoll） |
| 某次 ftrace 里出现的内部符号 | **Linux 实现 + 本机构建** |

验收点：读者能看出你不是把内核函数名误标成“标准保证”。

### 9.7 实践验收清单

- [ ] 报告写明 `uname -r`（及 `uname -a`/`/proc/version`）与关键配置片段。
- [ ] 固定并对齐了源码 tag（或明确写了 elixir 上的版本号）。
- [ ] 选择了 `read` 或 `epoll_wait`，并从系统调用入口追踪到子系统关键函数（名称来自该 tag）。
- [ ] 有 ftrace/tracepoint/`perf`/`strace` 之一的对照证据。
- [ ] 明确列出至少两条“Linux 实现而非 POSIX 保证”的结论。
- [ ] 理解函数名/路径会随版本变化，下次换内核要重做对齐。

## 10. 自测题

### 题一：为什么排障报告里只写“内核 5.x”不够？还需要哪些信息？与本课哪条验收对应？

**参考答案：** 大版本号过粗，发行版后缀、stable 小版本、以及关键 `CONFIG_*`（如是否具备 ftrace/tracepoint）都会改变你能看到的路径与工具。同一“5.x”下函数名与 backport 行为也可能不同。本课验收要求**报告标明版本与配置**，通常至少包含完整 `uname -r`（更好加上 `/proc/version`）以及与结论相关的配置片段，并固定对照的源码 tag。

### 题二：把网上一篇旧文里的 `epoll` 调用链原样背到生产环境有什么风险？应如何做？

**参考答案：** 不同内核版本的函数名与实现路径会变化，旧文可能已过时；且未对照本机配置与 trace 时，无法证明当前故障真走了那条路。正确做法是：固定与运行环境接近的内核 tag，从系统调用入口自己追到 eventpoll 子系统，再用 `sys_enter_epoll_wait` 等 trace 证据核对，并区分 Linux 实现细节与 POSIX 可移植语义。

### 题三：长连接 Gateway 的线程阻塞在 `epoll_wait`，能否据此断定“内核网络栈故障”？锁与上下文知识如何帮助你避免误判？

**参考答案：** 不能直接断定。阻塞在 `epoll_wait` 常常只是**进程上下文里正常的可睡眠等待**——没有就绪事件时睡眠是设计行为。需要结合：是否有数据包到达、softirq/接收路径是否异常、用户态是否因 `EPOLLET` 漏读、以及 cgroup CPU 节流等（上一课）综合判断。锁与上下文知识帮助你理解：唤醒与就绪列表维护有同步约束；若问题在软中断过重或锁竞争，表现也可能是延迟升高，但不能把“停在 epoll_wait”本身等同于内核故障。应用 trace 看 enter/exit 与超时模式，才能把猜想变成证据。

## 11. 推荐资源

- **内核路径与运行时边界：参考文档 / 原始资料**  
  https://docs.kernel.org/core-api/index.html  
  来源：docs.kernel.org；语言：英文；类型：文档；难度：专家；免费。  
  说明：重点阅读与内核核心 API、同步（锁/RCU）及可与系统调用到子系统路径互证的章节，用于核对本节点实验的语义和边界。建议固定内核版本后，对照本课的 `read`/`epoll_wait` 追踪与 trace 实践阅读。

本地可继续查阅：

```bash
man 2 read
man 7 epoll
man 2 epoll_wait
# 内核文档树中的 core-api、locking、RCU 相关章节
# 以及 /sys/kernel/debug/tracing/README（若存在）
```

## 12. 本课小结

- 内核源码研究应从**具体系统调用与数据结构**入手，连接可观察现象和实现，避免只背调用链。
- 先固定**内核版本与配置**，再谈路径；报告必须写明版本与配置。
- 从 `read`/`epoll_wait` 入口进入 VFS 或 eventpoll 等子系统，用 ftrace/tracepoint 对照实际路径。
- 理解 spinlock、mutex、RCU 以及 process/softirq 等上下文约束，才能解释“为什么能睡/不能睡、瓶颈可能在哪”。
- 常见误区：不同内核版本函数名与实现路径会变。
- 设计权衡：深入源码耗时，应由可证伪的运行时问题驱动；固定 tag，从入口追踪，并用 trace 验证。
- 验收时还能指出：哪些结论只是 Linux 实现，而非 POSIX 保证——这对公众号后台的跨环境排障与文档沉淀至关重要。
