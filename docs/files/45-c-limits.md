# 第 45/208 课：资源限制与/proc诊断

> 课程信息
>
> | 项目 | 内容 |
> | --- | --- |
> | 主题 | 资源限制与/proc诊断（Resource Limits） |
> | 领域 / 阶段 | C / 阶段 3 |
> | 子类 | 隔离与资源治理 |
> | 难度 | 进阶 |
> | 前置 | c-fd、c-vm |
> | 标签 | Resource Limits、C、C++ |

上一课谈动态装载与重入边界；本课回到进程与系统的**硬天花板**——fd、线程、地址空间等资源限制，以及用 `/proc` 读出当前占用时必须对齐内核语义的问题。

进程不是无限容器。长连接网关把 `accept` 打满、日志线程把线程数顶穿、某个泄漏把 RSS 推到 OOM——表面上是业务故障，根因往往是：**软/硬限制、系统级配额、以及你对 `/proc` 字段的误读**。本课目标是把这套诊断链路练成肌肉记忆。

---

## 学习目标

完成本课后，你应该能够：

1. 解释 **rlimit（进程级）** 与 **系统级限制**（如 `fs.file-max`、`pid_max`、cgroup）的层次关系，以及它们如何共同约束后端进程。
2. 用 `/proc/<pid>/fd`、`/proc/<pid>/status`、`/proc/<pid>/smaps`（或 `smaps_rollup`）读出 fd 占用、内存与映射概况，并知道字段解释必须对照内核文档，不能凭感觉。
3. 区分 **soft limit** 与 **hard limit**：谁能改、改到哪里、对 `ulimit`/`prlimit`/`setrlimit` 的实际影响。
4. 独立完成：降低 `nofile` 限制触发接入失败，验证**预留 fd** 与**降载策略**；能区分 `EMFILE` 与 `ENFILE`；能用**时间序列证据**判断是否在泄漏。

验收标准：

- 能区分 `EMFILE` 与 `ENFILE`。
- 能用时间序列证据判断泄漏。

---

## 为什么后端要懂

### 场景一：长连接网关「突然接不进来」

微信公众号后台常见长连接/网关接入层：客户端心跳保活，单机持有大量 socket。某天峰值一到，新连接开始失败，日志里只有：

```text
accept: Too many open files
```

你会立刻想到「把 `ulimit -n` 调大」。有时这能救急，有时只是把爆点推迟几小时——因为真正问题是连接泄漏、或系统级 `file-max` 已满、或 cgroup 另有配额。不懂 rlimit 与系统限制的层次，排障会停在「调大 ulimit」这一层。

### 场景二：ENOMEM 不一定是「没内存」

后台进程 `malloc`/`mmap` 失败返回 `ENOMEM`，或被 OOM killer 杀掉。可能原因包括：

- 物理内存 / swap 真不够；
- 进程 `RLIMIT_AS`（虚拟地址空间）或 `RLIMIT_DATA` 触顶；
- cgroup memory 上限；
- 过度碎片或映射过多导致地址空间耗尽（尤其 32 位或受限地址空间）。

`/proc/<pid>/status` 里的 `VmSize`/`VmRSS`/`VmPeak`，以及 `smaps_rollup`，是区分「RSS 真涨」还是「虚地址打满」的入口。字段含义必须以内核文档为准。

### 场景三：资源泄漏被「调高限制」掩盖

运维把 `nofile` 从 65535 调到 100 万后，告警暂时消失。两周后机器 fd 表膨胀、延迟抖动、最终仍崩。**调高限制不能修复资源泄漏**——它只改变爆炸半径与爆炸时间。本课会强调用时间序列（fd 数、RSS、映射数随时间单调爬升）证明泄漏，而不是用「再调大一点」代替根因分析。

---

## 0. 一张图建立直觉

```text
                    ┌─────────────────────────────┐
                    │  系统 / 命名空间 / cgroup     │
                    │  fs.file-max, pid_max, mem… │
                    └──────────────┬──────────────┘
                                   │ 再收紧
                    ┌──────────────▼──────────────┐
                    │  进程 rlimit (soft / hard)   │
                    │  RLIMIT_NOFILE, NPROC, AS…  │
                    └──────────────┬──────────────┘
                                   │ 实际占用
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        /proc/pid/fd         /proc/pid/status      /proc/pid/smaps*
        打开了哪些 fd         汇总计数与信号等       映射与内存细节
              │                    │                    │
              └────────────────────┴────────────────────┘
                                   │
                         打满 → EMFILE / ENFILE / ENOMEM
                         泄漏 → 时间序列单调上升（不是瞬时尖刺）
```

记住三句话：

1. **系统限制是天花板，rlimit 是进程自己的安全带**；
2. **soft 是当前生效上限，hard 是 soft 能被抬到的天花板（无特权通常不能超 hard）**；
3. **`/proc` 给现状，内核文档给语义；泄漏靠时间序列，不靠单次快照。**

---

## 1. rlimit 与系统级限制

### 1.1 什么是 rlimit

Linux 为每个进程维护一组资源限制，核心 API：

```c
#include <sys/resource.h>

int getrlimit(int resource, struct rlimit *rlim);
int setrlimit(int resource, const struct rlimit *rlim);
/* Linux 还有更灵活的 prlimit(2) */

struct rlimit {
    rlim_t rlim_cur;  /* soft limit：当前生效上限 */
    rlim_t rlim_max;  /* hard limit：soft 的上限 */
};
```

与后台最相关的几项：

| resource | 含义 | 典型触顶现象 |
| --- | --- | --- |
| `RLIMIT_NOFILE` | 进程可打开的最大 fd 数（含 0/1/2） | `accept`/`socket`/`open` → `EMFILE` |
| `RLIMIT_NPROC` | 该用户可创建的进程/线程数（实现相关） | `pthread_create`/`fork` 失败 |
| `RLIMIT_AS` | 进程虚拟地址空间上限 | `mmap`/`malloc` → `ENOMEM` |
| `RLIMIT_RSS` | 历史遗留，现代 Linux 上常不强制 | 不要依赖它做硬隔离 |
| `RLIMIT_CORE` | core dump 大小 | 崩溃无 core / core 被截断 |
| `RLIMIT_CPU` | CPU 时间 | `SIGXCPU` |

> 注意：`RLIMIT_NOFILE` 的语义是「最大文件描述符编号相关的上限」，实践中常理解为进程可持有的 fd 数量上限；精确边界以 `getrlimit(2)` man 页与当前内核为准。

### 1.2 系统级限制（进程 rlimit 之上）

即使进程 soft/hard 很大，仍可能被系统配额卡住：

```bash
# 系统可分配的文件句柄总数（近似）
sysctl fs.file-max
cat /proc/sys/fs/file-max

# 当前系统已用 / 最大（file-nr：allocated, unused, max）
cat /proc/sys/fs/file-nr

# 进程数上限
sysctl kernel.pid_max

# 若在容器 / cgroup v2 中，还要看
# /sys/fs/cgroup/.../memory.max, pids.max 等
```

网关机上常见组合拳：

1. 进程 `nofile` soft=65535，但 `fs.file-max` 更小或已接近耗尽 → 新打开失败更像 **`ENFILE`**（系统级）；
2. 进程 soft 触顶 → **`EMFILE`**（进程级）；
3. 容器 `pids.max` 触顶 → 建线程失败，错误码因运行时而异。

### 1.3 命令行查看与临时修改

```bash
# 当前 shell 的 soft 限制
ulimit -n          # nofile soft
ulimit -Hn         # nofile hard
ulimit -a          # 一览

# 只对当前 shell 临时降低 soft（演示用）
ulimit -n 32

# 更精确：对指定 pid 读写（需要权限）
prlimit --pid $$ --nofile
prlimit --pid $$ --nofile=64:64   # soft:hard

# 进程自身视角
cat /proc/self/limits
```

`/proc/self/limits` 示例（字段以本机为准）：

```text
Limit                     Soft Limit           Hard Limit           Units
Max open files            1024                 1048576              files
Max processes             127728               127728               processes
Max address space         unlimited            unlimited            bytes
...
```

短 C 示例：查询并故意压低 `nofile` soft：

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <errno.h>
#include <string.h>
#include <sys/resource.h>

int main(void) {
    struct rlimit rl;
    if (getrlimit(RLIMIT_NOFILE, &rl) != 0) {
        perror("getrlimit");
        return 1;
    }
    printf("nofile soft=%llu hard=%llu\n",
           (unsigned long long)rl.rlim_cur,
           (unsigned long long)rl.rlim_max);

    rl.rlim_cur = 16;                 /* 只降 soft */
    if (rl.rlim_cur > rl.rlim_max)
        rl.rlim_cur = rl.rlim_max;
    if (setrlimit(RLIMIT_NOFILE, &rl) != 0) {
        perror("setrlimit");
        return 1;
    }
    getrlimit(RLIMIT_NOFILE, &rl);
    printf("after: soft=%llu hard=%llu\n",
           (unsigned long long)rl.rlim_cur,
           (unsigned long long)rl.rlim_max);
    return 0;
}
```

无特权进程：**可以把 soft 在 `[0, hard]` 内调整**；可以把 hard **降低**；一般**不能**把 hard 抬高超过原 hard（需要 `CAP_SYS_RESOURCE` 等能力）。systemd 服务常用 `LimitNOFILE=` 在启动时设定。

### 1.4 和微信后台长连接的对应关系

假设接入进程：

- 监听 fd、健康检查、日志、配置热加载、内部 RPC 各占若干 fd；
- 每个活跃长连接至少 1 个 socket fd（再加 timerfd/eventfd 等则更多）；
- epoll 本身占 1 个 fd。

容量估算不能只看「`ulimit -n` 很大」：

```text
可用连接 ≈ soft_nofile - 预留(listen/log/内部) - 安全余量
每连接成本 ≈ socket 结构 + sk_buff 缓冲 + 用户态会话对象 + ...
```

高上限提升容量，也必须预算**每连接真实内存和内核成本**——后面「设计权衡」会展开。

---

## 2. /proc/pid/fd、status 和 smaps

`/proc` 是诊断窗口，不是故事书。字段名看起来像「人话」，语义却随内核版本演进；**解释必须对应内核文档 / man 页**（本课资源：`getrlimit(2)`，并延伸阅读 `proc(5)`）。

### 2.1 `/proc/<pid>/fd`：打开了什么

```bash
pidof your_gateway
ls /proc/$PID/fd | wc -l          # 当前 fd 数量
ls -l /proc/$PID/fd | head
# 常见输出：socket:[inode]、pipe:[inode]、anon_inode:[eventfd]、文件路径
```

要点：

- 目录下的**数字名**是 fd 号；软链目标揭示类型。
- `ls | wc -l` 统计的是**当前仍打开的 fd 个数**，不是历史峰值。
- 若怀疑泄漏：对同一 pid **周期性采样**（见实践节），看是否单调上升。
- `ls -l` 对已删除但仍打开的文件会显示 ` (deleted)`——日志轮转后仍占 fd 的经典坑。

配合：

```bash
# 更友好的汇总（若安装了 lsof）
lsof -p $PID | awk '{print $5,$9}' | sort | uniq -c | sort -nr | head

# 只关心 socket
ls -l /proc/$PID/fd | grep socket | wc -l
```

### 2.2 `/proc/<pid>/status`：汇总快照

```bash
grep -E '^(Name|Pid|FDSize|Threads|VmPeak|VmSize|VmRSS|VmData|VmStk|VmExe|VmLib|voluntary|nonvoluntary)' /proc/$PID/status
```

常见字段直觉（**以你机器内核的 `proc(5)` 为准**）：

| 字段 | 用途直觉 |
| --- | --- |
| `FDSize` | 内核为该进程 fd 表分配的槽位数（≥ 实际打开数，会按需扩张） |
| `Threads` | 线程数（含主线程） |
| `VmSize` / `VmPeak` | 虚拟地址空间当前 / 峰值 |
| `VmRSS` | 常驻物理内存近似 |
| `VmData` / `VmStk` / `VmLib` | 数据、栈、共享库等粗分 |

注意：

- `FDSize` **不是**「当前打开 fd 数」。当前打开数请数 `/proc/pid/fd` 或读更合适的接口。
- `VmRSS` 是诊断入口，精细归因要靠 `smaps` / `smaps_rollup` / cgroup 指标。
- 单次 `status` 只能说明「此刻」；判断泄漏必须**时间序列**。

### 2.3 `/proc/<pid>/smaps` 与 `smaps_rollup`

```bash
# 全量映射（大进程上可能很大，生产慎用频繁 cat）
head -40 /proc/$PID/smaps

# 汇总版（较新内核提供）：一眼看 RSS/PSS 等
cat /proc/$PID/smaps_rollup
```

`smaps_rollup` 示例字段直觉：

```text
Rss:    ... kB
Pss:    ... kB
Private_Dirty: ... kB
Swap:   ... kB
```

- **Rss**：该进程页驻留总量近似；
- **Pss**：按共享比例分摊后的「公平份额」，多进程共享库场景更适合做容量账；
- **Private_Dirty**：私有脏页，接近「进程自己写脏的内存」。

定位 `ENOMEM` 时的分层问题：

1. RSS / cgroup memory 是否触顶？
2. `VmSize` 是否逼近 `RLIMIT_AS`？
3. 是否 mmap 过多小映射导致管理开销或地址空间碎片？

后台长连接场景：连接对象、接收缓冲、写出队列若按连接线性增长，你会在时间序列里看到 `Private_Dirty`/`Rss` 与连接数同向爬升；若 fd 数爬升而 Rss 几乎不动，更像「空连接/泄漏的 fd」而非大缓冲泄漏。

### 2.4 把三类 /proc 信息拼成一张诊断表

| 怀疑 | 先看 | 再验证 |
| --- | --- | --- |
| fd 打满 / 泄漏 | `ls /proc/pid/fd \| wc -l`、`/proc/self/limits` | `accept` errno；时间序列 |
| 线程打满 | `status` 的 `Threads`、`RLIMIT_NPROC` | `pthread_create` 失败路径 |
| 内存 / 地址空间 | `status` Vm*、`smaps_rollup`、cgroup | `ENOMEM` 调用栈；`RLIMIT_AS` |
| 系统句柄耗尽 | `/proc/sys/fs/file-nr` | 多进程同时失败 → `ENFILE` |

---

## 3. soft / hard 限制

### 3.1 定义

- **Soft limit（`rlim_cur`）**：内核当前对进程强制的上限。触顶就失败或发信号（视资源类型）。
- **Hard limit（`rlim_max`）**：soft 能被设置到的最大值。无特权用户不能超过 hard；可以降低 hard，一旦降低通常难以再自行抬回。

```text
0  ≤  soft  ≤  hard  ≤  （内核/特权允许的更大上限）
         ▲
    日常真正卡你的是 soft
```

### 3.2 谁在改它们

| 路径 | 典型用途 |
| --- | --- |
| `ulimit`（shell 内建） | 交互调试、临时实验 |
| `setrlimit` / `prlimit` | 程序自愈、启动后主动收紧 |
| systemd `LimitNOFILE=` 等 | 服务启动时统一设定 |
| PAM /login 配置 | 用户登录会话默认值 |
| 容器运行时 | 把宿主机默认映射进容器 |

生产建议：**在服务管理器里显式声明**，不要依赖「某个运维登录后手动 ulimit」。接入进程启动日志应打印 `nofile soft/hard`，避免「配置了但没生效」。

### 3.3 只降 soft 做演练：为什么安全且有用

故障演练时，把 soft 降到很小（例如 32），hard 保持不变：

- 容易复现 `EMFILE`；
- 演练结束后把 soft 调回（仍 ≤ hard）即可；
- 避免一上来改系统 `file-max` 影响整机。

```bash
# 演示：仅当前 shell
ulimit -Hn   # 记下 hard
ulimit -Sn 32
cat /proc/self/limits | grep 'open files'
```

### 3.4 soft 触顶 vs hard 触顶

对 `RLIMIT_NOFILE`：进程打开到 soft 上限就会失败（`EMFILE`）。你不能「不知不觉用到 hard」——必须先成功把 soft 抬高到更大值（且 ≤ hard）。因此监控应报 **相对 soft 的使用率**，而不是只看 hard。

---

## 4. 常见误区与设计权衡

### 4.1 误区：调高限制就能修复资源泄漏

**调高限制不能修复资源泄漏。**

泄漏的本质是「申请与释放不匹配」。把 `nofile` 从 10 万调到 100 万，只会让：

- 暴露时间更晚；
- 故障爆炸时占用更多内核内存与表项；
- 更难在测试环境复现。

正确顺序：

1. 用时间序列证明泄漏（fd / RSS / 映射数）；
2. 找到泄漏路径（未 `close`、未从连接表移除、异常分支跳过清理）；
3. 修复后，再评估合理上限与余量。

### 4.2 误区：`EMFILE` 和 `ENFILE` 混为一谈

| errno | 含义（直觉） | 典型对策方向 |
| --- | --- | --- |
| `EMFILE` | **本进程** fd 太多（触 soft `RLIMIT_NOFILE`） | 查本进程泄漏/预留/降载；必要时合理提高本进程 nofile |
| `ENFILE` | **系统**打开文件总数达上限 | 查 `file-nr`/`file-max`、整机其他进程、是否恶意耗尽 |

网关日志必须把 `errno` 打出来；只写「Too many open files」而不区分，会带偏整次排障。

### 4.3 误区：把 `FDSize` 当成打开数

`status` 里的 `FDSize` 是内核 fd 表容量相关字段，可能大于当前打开数。容量规划与告警请用「实际打开数」或应用自己的连接计数。

### 4.4 设计权衡：高上限 vs 每连接真实成本

**高上限提升容量，也要预算每连接真实内存和内核成本。**

- 把 `nofile` 调到 100 万，并不等于机器能扛 100 万长连接；
- 每条连接还有：socket 结构、TCP 缓冲、用户态会话、加解密上下文、定时器等；
- 内核 fd 表、epoll 兴趣集、定时轮也会随规模变贵；
- 更合理的做法是：**容量 = min(fd 余量, 内存预算, CPU, 带宽, 下游限额)**，并在接近软阈值时主动降载。

预留 fd 策略（接入层常用）：

```text
启动时：打开并持有若干「应急 fd」（或至少算清预留配额）
接近 soft 时：
  1) 停止 accept / 对监听 fd 做限流（降载）
  2) 保证日志、管理端口、健康检查仍可用（预留）
  3) 拒绝新连接优于拖死整个进程
```

伪代码：

```c
enum { RESERVE = 16 };  /* 日志、admin、health、内部 RPC 等 */

int can_accept_more(void) {
    struct rlimit rl;
    getrlimit(RLIMIT_NOFILE, &rl);
    int used = count_open_fds();   /* 读 /proc/self/fd 或自维护计数 */
    return used + RESERVE < (int)rl.rlim_cur;
}

int accept_loop(int lfd) {
    if (!can_accept_more()) {
        /* 降载：暂停 accept、对外返回过载、打点告警 */
        return -1;
    }
    int cfd = accept(lfd, NULL, NULL);
    if (cfd < 0) {
        if (errno == EMFILE || errno == ENFILE) {
            /* 区分打点；EMFILE 可尝试关闭预留应急 fd 再救一次日志 */
        }
        return -1;
    }
    return cfd;
}
```

「预留 fd」的一种经典手法：启动时 `open("/dev/null", …)` 若干个，真正 `EMFILE` 时先 `close` 掉预留，腾出额度写完日志/指标再优雅退出或降载——这是**救观测性**，不是鼓励泄漏。

---

## 5. 动手实践

目标：降低 `nofile` 限制触发接入失败，验证预留 fd 与降载策略；验收能区分 `EMFILE`/`ENFILE`，并能用时间序列判断泄漏。

> 建议在个人开发机或容器内完成，不要在共用生产机上把系统 `file-max` 改到极端值。

### 步骤 A：准备一个「伪接入」小服务

将下列代码保存为 `tiny_accept.c`（演示用，非生产）：

```c
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <unistd.h>

#define RESERVE_N 8
static int reserve_fds[RESERVE_N];

static void reserve_open(void) {
    for (int i = 0; i < RESERVE_N; i++) {
        reserve_fds[i] = open("/dev/null", O_RDONLY);
        if (reserve_fds[i] < 0) perror("reserve open");
    }
}

static void reserve_release_one(void) {
    for (int i = 0; i < RESERVE_N; i++) {
        if (reserve_fds[i] >= 0) {
            close(reserve_fds[i]);
            reserve_fds[i] = -1;
            return;
        }
    }
}

static int fd_usage(void) {
    int n = 0;
    for (int fd = 0; fd < 4096; fd++) {
        if (fcntl(fd, F_GETFD) != -1) n++;
    }
    return n;
}

int main(void) {
    struct rlimit rl;
    getrlimit(RLIMIT_NOFILE, &rl);
    rl.rlim_cur = 32;                 /* 故意压低 soft */
    if (rl.rlim_cur > rl.rlim_max) rl.rlim_cur = rl.rlim_max;
    if (setrlimit(RLIMIT_NOFILE, &rl) != 0) {
        perror("setrlimit");
        return 1;
    }

    reserve_open();

    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    int on = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof on);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(18080);
    if (bind(lfd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        perror("bind");
        return 1;
    }
    listen(lfd, 128);
    printf("listen 127.0.0.1:18080 soft_nofile=%llu reserve=%d used=%d\n",
           (unsigned long long)rl.rlim_cur, RESERVE_N, fd_usage());

    for (;;) {
        int used = fd_usage();
        if (used + 4 >= (int)rl.rlim_cur) {  /* 降载阈值：预留余量 */
            printf("DEGRADE used=%d soft=%llu — pause accept, sleep 1s\n",
                   used, (unsigned long long)rl.rlim_cur);
            sleep(1);
            continue;
        }
        int cfd = accept(lfd, NULL, NULL);
        if (cfd < 0) {
            if (errno == EMFILE) {
                fprintf(stderr, "EMFILE: process fd limit (used=%d)\n", fd_usage());
                reserve_release_one(); /* 腾额度写完关键日志 */
                fprintf(stderr, "after reserve release used=%d\n", fd_usage());
            } else if (errno == ENFILE) {
                fprintf(stderr, "ENFILE: system-wide file table full\n");
            } else {
                perror("accept");
            }
            sleep(1);
            continue;
        }
        printf("accepted fd=%d used=%d\n", cfd, fd_usage());
        /* 故意不 close，模拟泄漏；对照实验可改为 close(cfd) */
    }
}
```

编译运行：

```bash
gcc -O2 -o tiny_accept tiny_accept.c
./tiny_accept
```

### 步骤 B：降低 nofile，触发接入失败

另开终端：

```bash
# 观察服务进程 limits 与 fd
pid=$(pidof tiny_accept)
cat /proc/$pid/limits | grep 'open files'
ls /proc/$pid/fd | wc -l

# 用短连接打满（泄漏版会很快触顶）
for i in $(seq 1 40); do
  nc -d 127.0.0.1 18080 >/dev/null &
done
```

预期观察：

1. 服务在阈值前打印 `DEGRADE`（降载，主动暂停 accept）；
2. 若仍触顶，日志出现 **`EMFILE`**（进程级），而不是笼统的「打满了」；
3. 释放一个预留 fd 后，短暂能再写日志 / 再 accept 一次——验证预留策略。

### 步骤 C：对照 `ENFILE`（可选、谨慎）

`ENFILE` 需要系统级压力，演示成本高。认知上掌握：

```bash
cat /proc/sys/fs/file-nr
# 三个数：已分配句柄数、空闲、上限(file-max)
```

若多个无关进程同时出现「Too many open files」，且本进程 `ls /proc/pid/fd | wc -l` **远低于** soft limit，应转向系统级 / 其他进程，怀疑 `ENFILE` 或别的进程占满。

在日志与打点中**分开计数** `EMFILE` vs `ENFILE`，是本课硬验收点。

### 步骤 D：用时间序列证据判断泄漏

不要只看一次快照。对 pid 采样 1–5 分钟：

```bash
pid=$(pidof tiny_accept)
rm -f /tmp/fd_series.txt
for i in $(seq 1 30); do
  ts=$(date +%H:%M:%S)
  n=$(ls /proc/$pid/fd 2>/dev/null | wc -l)
  rss=$(awk '/VmRSS/{print $2}' /proc/$pid/status)
  echo "$ts fd=$n rss_kB=$rss" | tee -a /tmp/fd_series.txt
  sleep 2
done
cat /tmp/fd_series.txt
```

判断标准：

- **泄漏**：在负载模式不变（或连接应已结束）时，`fd=` 或 `rss_kB=` **台阶式/单调爬升**，无法回到基线；
- **正常峰值**：打压测时上升，压测结束后回到基线附近；
- **单次尖刺**：短暂升高立刻回落，通常不是泄漏。

把「故意不 close」改成 `close(cfd)` 再跑一轮，时间序列应在客户端退出后回落——这就是对照实验。

### 步骤 E：验收清单

- [ ] 能说明本次失败是 `EMFILE` 还是 `ENFILE`，并指出证据（本进程 fd vs soft；或 `file-nr`）。
- [ ] 展示至少一份时间序列（时间戳 + fd 数，和/或 RSS），据此判断泄漏或非泄漏。
- [ ] 演示降载（阈值前暂停 accept）与预留 fd（触顶后释放应急 fd 恢复可观测性）。

---

## 6. 自测题

**题 1.** 接入进程 soft `nofile=1024`，`ls /proc/self/fd | wc -l` 已是 1024，此时 `accept` 最可能返回什么 errno？若 soft 仍富余但 `/proc/sys/fs/file-nr` 显示系统已用尽，又更可能是什么？

<details><summary>答案</summary>

前者是进程级上限触顶，典型为 **`EMFILE`**。后者是系统打开文件数耗尽，典型为 **`ENFILE`**。排障时两者对策完全不同：一个查本进程泄漏/预留/降载，一个查整机 `file-max` 与其他进程。

</details>

**题 2.** 无特权服务能否把 `RLIMIT_NOFILE` 的 hard 从 4096 提升到 1048576？soft 与 hard 的关系是什么？

<details><summary>答案</summary>

一般**不能**自行抬高 hard（需要特权/`CAP_SYS_RESOURCE` 或在具备权限的启动器里设置）。约束是 **`0 ≤ soft ≤ hard`**：日常强制的是 soft；hard 是 soft 可被设置到的天花板。可降低 soft 做演练；无特权降低 hard 后通常无法自行改回。

</details>

**题 3.** 有人把生产 `nofile` 调到 100 万后告警消失，宣称「泄漏已修复」。如何用本课方法证伪或证实？

<details><summary>答案</summary>

**调高限制不能修复资源泄漏。** 应对同一进程做时间序列采样（fd 数、`VmRSS`/`smaps_rollup`、连接表长度）。若在负载平稳甚至连接应释放后计数仍单调上升，则泄漏仍在，只是触顶被推迟。修复应落在释放路径与降载/预留策略；上限调整只是容量规划，不是根因治疗。

</details>

---

## 7. 推荐资源

| 资源 | 说明 |
| --- | --- |
| [资源限制与/proc诊断：Linux man-pages](https://man7.org/linux/man-pages/man2/getrlimit.2.html) | man7.org 文档（英文）。重点阅读 rlimit 与系统级限制，并结合同页与 `proc(5)` 理解 `/proc/pid/fd`、`status`、`smaps` 语义与边界；核对本课实验时对照适用内核版本。进阶 / 免费。 |

学习建议（节点 sourceStudy）：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件——尤其是 `RLIMIT_RSS` 是否生效、`smaps_rollup` 是否存在、容器 cgroup 是否另有配额。

---

## 8. 本课小结

1. **rlimit** 约束进程，**系统级限制 / cgroup** 再盖一层；网关容量取所有层的最小值。
2. **`/proc/pid/fd`、`status`、`smaps(_rollup)`** 提供现状；字段解释必须对齐内核文档；**泄漏靠时间序列**。
3. **soft / hard**：soft 当前生效，hard 是天花板；演练优先只降 soft。
4. **`EMFILE` ≠ `ENFILE`**；日志与指标必须分开。
5. **调高限制 ≠ 修复泄漏**；高上限要配套每连接内存/内核成本预算，以及预留 fd 与降载。

下一课将继续在「隔离与资源治理」脉络里，把这些上限与更完整的治理手段串起来。遇到接入失败时，先问：是进程天花板、系统天花板，还是泄漏把余量吃光了？
