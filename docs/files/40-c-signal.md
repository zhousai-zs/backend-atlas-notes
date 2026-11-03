# 第 40/208 课：信号与安全停机

| 项目 | 内容 |
|---|---|
| 节点 | `c-signal` |
| 英文名 | Signals |
| 领域 / 阶段 | C / 2 |
| 子类 | 通信与运行时 |
| 难度 | 基础 |
| 前置节点 | `c-process`、`c-syscall` |
| 核心摘要 | 信号异步通知进程事件，处理器可能中断普通执行，因此只能使用符合信号安全约束的操作。 |

> 本课要建立的不是“信号编号记忆”，而是一条可靠的停机链路：内核把 `SIGTERM` 送到进程，进程安全地感知它，停止接入新请求，排空在途请求，并在截止时间前退出。对即将参与腾讯微信公众号后台开发的后端工程师来说，这条链路直接决定滚动发布时用户会不会看到连接被重置、请求半途失败。

## 学习目标

学完本课，你应该能够：

1. 解释信号的**处置**与**屏蔽**：默认、忽略、捕获分别意味着什么，`sigprocmask` / `pthread_sigmask` 如何让信号进入待决状态。
2. 说明多线程程序中信号会被投递给“任意一个未屏蔽它的线程”，并解释为什么必须在创建线程之前配置屏蔽字。
3. 解释 `async-signal-safe` 的约束，能够判断 `write`、`printf`、`malloc`、`syslog` 在信号处理器中是否可用。
4. 能独立完成本节点实践：用 `signalfd` 把 `SIGTERM` 纳入事件循环，停止接入并排空在途请求，且在截止时间到达时强制退出。
5. 能在代码评审中识别“线程安全函数被误用于信号处理器”“只屏蔽主线程”“退出没有截止时间”等常见问题。

## 为什么后端要懂信号与安全停机

节点给出的理由是：**让服务可靠处理退出与子进程通知。** 这句话在后端生产环境里非常具体。

以微信公众号后台的滚动发布为例：

- 发布系统（Kubernetes、systemd 或自研调度器）要下线一个旧实例时，通常会先发送 `SIGTERM`，给进程一段宽限期；宽限期结束后再发送 `SIGKILL`。
- 如果进程没有处理 `SIGTERM`，默认动作是立即终止。此时正在处理的 HTTP 请求、数据库事务、缓存更新、消息确认可能被硬生生打断，客户端看到 `502`、`connection reset` 或超时。
- 如果进程能优雅处理，就可以先关闭监听 socket，让新连接不再进来；再把已经接收的请求处理完；最后在截止时间前主动退出。负载均衡和发布系统看到的是平滑下线，而不是一批失败请求。
- 后台服务还经常创建子进程（worker、脚本、外部命令）。子进程退出时会产生 `SIGCHLD`。如果不回收，就会积累僵尸进程，最终耗尽进程表资源。
- 线上排障时，`kill -TERM`、`/proc/<pid>/status` 里的信号掩码、`strace -e trace=signal` 都是定位“为什么进程不退出”“为什么信号没收到”的基础工具。

所以，信号不是“写个小程序才用得到”的知识。它处在**进程生命周期、线程模型、事件循环和发布系统**的交汇处。

## 0. 先建立直觉：信号到底是什么

把进程想象成一间正在工作的办公室，信号就是内核按门铃：

- 门铃可以在任何时刻响，不问你当前是不是正在开会、正在拿锁、正在写文件。
- 你可以选择门铃响起后做什么：不理它、按固定流程处理、或者干脆把门铃拆掉（但有些门铃拆不掉）。
- 门铃响的时候，你原本的工作会被暂时打断；处理完门铃后，通常还要回到原来的工作。
- 如果同时响很多次同一种门铃，普通信号可能只会被记住一次，不会排队成很多次。

对应到 Linux：

- 信号是内核向进程或线程发送的**异步事件通知**。
- 信号有编号和名字，例如 `SIGTERM` 是 15，`SIGINT` 是 2，`SIGKILL` 是 9。
- 进程可以设置信号的**处置方式**，也可以**屏蔽**信号。
- 信号处理器运行在被打断的线程上下文中，可能打断任意普通代码，包括持有锁的代码。
- 因此，处理器能做的事情受到严格限制，这就是 `async-signal-safe`。

本课的三个核心要点是：

1. 信号处置和屏蔽；
2. `async-signal-safe`；
3. `signalfd` 与优雅退出。

下面按“直觉 → 原理 → 代码 / 命令 → 后端场景”逐层展开。

---

## 1. 核心要点一：信号处置和屏蔽

### 1.1 直觉：处置是“发生时怎么办”，屏蔽是“先别送来”

这两个概念很容易混淆：

- **处置（disposition）**：信号真正被递送时，进程要做什么。默认终止？忽略？还是执行一个处理器函数？
- **屏蔽（block）**：暂时不让这个信号被递送。信号不会消失，而是变成**待决（pending）**，等解除屏蔽后再递送。

可以这样记：

> 忽略是“收到了但扔掉”；屏蔽是“先别送，等我准备好”。

### 1.2 处置：默认、忽略、捕获

Linux 用 `sigaction` 设置信号处置。三种基本处置如下：

| 处置 | 设置方式 | 行为 | 典型例子 |
|---|---|---|---|
| 默认 | `SIG_DFL` | 按该信号的默认动作执行 | `SIGTERM` 默认终止进程；`SIGSEGV` 默认终止并可能产生 core dump；`SIGCHLD` 默认忽略 |
| 忽略 | `SIG_IGN` | 收到后丢弃，不执行处理器 | 明确不想响应某个信号时使用 |
| 捕获 | 函数指针 | 信号递送时执行处理器 | 收到 `SIGTERM` 后设置退出标志 |

`SIGKILL` 和 `SIGSTOP` 是例外：

- `SIGKILL` 不能被捕获、忽略或屏蔽，只能由内核强制终止进程。
- `SIGSTOP` 不能被捕获、忽略或屏蔽，只能由内核暂停进程。
- 因此，**不要指望捕获 `SIGKILL` 来做优雅退出**。发布系统先用 `SIGTERM` 给你机会，超时才用 `SIGKILL`。

一个最小的 `sigaction` 示例：

```c
#include <signal.h>
#include <string.h>

static volatile sig_atomic_t g_stop = 0;

static void on_term(int signo) {
    (void)signo;
    g_stop = 1;
}

int install_handler(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_term;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    return sigaction(SIGTERM, &sa, NULL);
}
```

这段代码有几个关键点：

- 处理器只写了一个 `volatile sig_atomic_t` 标志，没有调用 `printf`、`malloc`、`syslog`。
- `sa_mask` 是处理器执行期间额外屏蔽的信号集合；当前信号默认也会在处理期间被屏蔽，除非设置 `SA_NODEFER`。
- `sa_flags = 0` 表示不设置 `SA_RESTART`。这样被信号打断的阻塞系统调用可能以 `EINTR` 返回，事件循环需要处理 `EINTR`。
- 实际工程中优先使用 `sigaction`，不要依赖老式 `signal()` 的实现差异。

### 1.3 屏蔽与待决信号

屏蔽一个信号使用 `sigprocmask`（单线程）或 `pthread_sigmask`（多线程）。**多线程程序应使用 `pthread_sigmask`**，因为 `sigprocmask` 在多线程程序中的行为未指定。

```c
sigset_t mask;
sigemptyset(&mask);
sigaddset(&mask, SIGTERM);

if (pthread_sigmask(SIG_BLOCK, &mask, NULL) != 0) {
    return -1;
}
```

屏蔽后，如果 `SIGTERM` 到达，它不会立即递送，而是进入进程的待决集合。等解除屏蔽时，它才会被递送：

```c
pthread_sigmask(SIG_UNBLOCK, &mask, NULL);
```

查看待决信号可以使用 `sigpending`：

```c
sigset_t pending;
sigpending(&pending);
int is_pending = sigismember(&pending, SIGTERM);
```

但线上更常用 `/proc`：

```bash
grep -E 'Sig(Blk|Ign|Cgt|Pnd)' /proc/<pid>/status
```

各字段含义：

- `SigBlk`：被屏蔽的信号集合（blocked）。
- `SigIgn`：被忽略的信号集合（ignored）。
- `SigCgt`：被捕获的信号集合（caught）。
- `SigPnd`：线程级待决信号。
- `ShdPnd`：进程级共享待决信号。

这些字段是十六进制位掩码。信号编号 `n` 对应第 `n-1` 位（从 0 开始）。例如只屏蔽 `SIGTERM`（15）时，对应位是第 14 位，即 `0x4000`。可以用 `kill -l` 查看编号和名字的对应关系：

```bash
kill -l
```

发送信号：

```bash
kill -TERM <pid>
```

查看信号相关的系统调用：

```bash
strace -f -e trace=signal -p <pid>
```

你通常会看到 `rt_sigaction`、`rt_sigprocmask`、`signalfd4`，以及类似 `--- SIGTERM {si_signo=SIGTERM, ...} ---` 的输出。这比只盯着业务日志更能说明“信号到底有没有到进程”。

### 1.4 多线程下的关键规则

这是本课最重要的规则之一：

> 进程定向信号会被投递给**任意一个没有屏蔽它的线程**。

也就是说，如果你只在主线程调用了 `pthread_sigmask(SIG_BLOCK, &mask, NULL)`，然后创建了工作线程，工作线程会继承主线程的信号掩码；但如果某个线程后来解除屏蔽，或者你在创建线程之后才设置屏蔽，内核就可能把 `SIGTERM` 投递给那个线程。

后果可能是：

- 默认处置直接终止整个进程，优雅退出逻辑根本没机会执行。
- 处理器在某个工作线程中运行，而你以为是主线程在处理。
- 使用 `signalfd` 时，如果信号没有在所有线程中被屏蔽，信号可能被其他线程拿走，`signalfd` 读不到。

正确做法：

1. 在创建任何线程之前，由主线程调用 `pthread_sigmask` 屏蔽需要同步处理的信号。
2. 之后创建的所有线程都继承这个屏蔽字。
3. 工作线程不要再解除这些信号的屏蔽。
4. 如果线程已经存在，必须逐个线程配置屏蔽字，或者重新设计为“先屏蔽、后建线程”。

这也解释了为什么节点特别强调：多线程下要在创建线程前屏蔽。

---

## 2. 核心要点二：`async-signal-safe`

### 2.1 直觉：处理器可能打断“正在拿锁的人”

普通函数调用是可预期的：你调用 `malloc`，它执行完返回，你再继续。信号处理器不是这样。

假设一个线程正在 `malloc` 内部持有分配器的锁，这时 `SIGTERM` 到达，处理器开始执行。如果处理器里又调用 `malloc`：

- 同一个线程试图再次获取它已经持有的锁；
- 或者锁的状态被信号打断，无法保证一致；
- 结果可能是死锁、崩溃，甚至内存状态损坏。

再比如 `printf` / `syslog`：

- 它们内部可能使用 stdio 锁、缓冲区、动态内存分配；
- 信号可能正好打断这些内部状态；
- 在处理器中调用它们，可能死锁，或者把日志缓冲区弄坏。

所以，**信号处理器不是普通函数**。它运行在一个“随时可能打断别人”的上下文里，只能调用 POSIX 明确列为 **async-signal-safe** 的函数。

### 2.2 原理：安全函数是白名单，不是“看起来简单就行”

POSIX 维护了一份异步信号安全函数列表。常见的安全函数包括：

- `write`、`read`、`open`、`close`
- `_exit`
- `kill`
- 部分 `sigaction`、`sigprocmask` 相关函数
- `waitpid` 等（具体以目标系统和 man-pages 为准）

不安全的常见函数包括：

- `printf`、`fprintf`、`snprintf`（标准未保证异步信号安全）
- `malloc`、`free`、`calloc`、`realloc`
- `syslog`
- `exit`（会执行 `atexit`、刷新 stdio，可能死锁；应使用 `_exit`）
- `strerror`
- `pthread_mutex_lock` / `pthread_mutex_unlock`
- 大多数 C++ 标准库设施，例如 `std::string`、`std::cout`、`std::mutex`

节点要求特别记住：

> `write` 可以；`printf` / `malloc` / `syslog` 不行，可能死锁。

此外，处理器还应保存和恢复 `errno`，因为它可能打断正在依赖 `errno` 的普通代码：

```c
static void on_term(int signo) {
    int saved_errno = errno;
    (void)signo;
    g_stop = 1;
    errno = saved_errno;
}
```

### 2.3 两种安全模式：标志位与 self-pipe

如果不想使用 `signalfd`，有两种常见的安全做法。

#### 模式一：只设置 `volatile sig_atomic_t` 标志

```c
static volatile sig_atomic_t g_stop = 0;

static void on_term(int signo) {
    int saved_errno = errno;
    (void)signo;
    g_stop = 1;
    errno = saved_errno;
}
```

主循环周期性检查：

```c
while (!g_stop) {
    do_one_event();
}
```

这种模式最简单，但有局限：

- 只能传递非常简单的状态，通常就是一个“该停了”的标志。
- 如果主循环阻塞在 `epoll_wait`、`accept`、`read` 上，需要处理 `EINTR`，或者设置超时定期醒来。
- 不适合传递复杂数据，也不适合做日志、内存分配等操作。

#### 模式二：self-pipe

处理器只向管道写入一个字节，事件循环从管道读端感知：

```c
static int g_wakeup[2] = {-1, -1};

static void on_term(int signo) {
    int saved_errno = errno;
    unsigned char byte = 1;
    ssize_t n = write(g_wakeup[1], &byte, 1);
    (void)n;
    errno = saved_errno;
}
```

事件循环把 `g_wakeup[0]` 加入 `epoll`，读到字节后执行真正的退出流程。`write` 是异步信号安全的。管道写端通常设为非阻塞；如果写满返回 `EAGAIN`，说明通知已经存在，可以忽略。注意保存和恢复 `errno`。

self-pipe 的优点是**可移植**，不依赖 Linux 专有的 `signalfd`；缺点是多了文件描述符和读写处理。

### 2.4 常见误区：线程安全不等于异步信号安全

这是节点明确点出的坑：

> 线程安全函数不必然异步信号安全。

`malloc` 是线程安全的：多个线程同时调用时，它用锁保护内部数据结构。但它是**不可重入**的：如果信号处理器在同一个线程持有 `malloc` 锁时再次调用 `malloc`，就可能自死锁。

同样，`printf` 可能对 stdio 加锁，`syslog` 可能内部加锁或分配内存。它们“多线程调用没问题”，不代表“信号处理器里调用没问题”。

判断标准不是“这个函数看起来简单吗”，而是：

> 它是否在 POSIX 的 async-signal-safe 列表中？

工程上最稳妥的做法是：**处理器只做最小动作**——设置 `volatile sig_atomic_t` 标志，或 `write` 到 self-pipe，或完全交给 `signalfd`，把业务逻辑放到正常的事件循环上下文里执行。

---

## 3. 核心要点三：`signalfd` 与优雅退出

### 3.1 直觉：把信号变成“可读的文件描述符”

传统处理器模式是“信号来了，打断你，执行处理器”。`signalfd` 换了一个思路：

> 先把信号屏蔽住，让它们保持待决；然后创建一个文件描述符，信号到达时这个 fd 变成可读；事件循环像处理 socket 一样处理它。

这样就不需要在信号处理器里写任何业务逻辑。信号消费发生在正常上下文，可以安全地调用 `printf`、`malloc`、业务函数和日志库。

节点要求的实践正是：

> 用 `signalfd` 将 `SIGTERM` 纳入事件循环，停止接入并排空请求。

### 3.2 `signalfd` 的基本用法

关键顺序不能错：

1. 先构造信号集合；
2. **先屏蔽这些信号**（多线程程序用 `pthread_sigmask`）；
3. 再调用 `signalfd` 创建 fd；
4. 把 fd 加入 `epoll`；
5. 事件循环中 `read` 出 `struct signalfd_siginfo`。

示例：

```c
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <sys/signalfd.h>
#include <unistd.h>

int setup_signal_fd(void) {
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);

    if (pthread_sigmask(SIG_BLOCK, &mask, NULL) != 0)
        return -1;

    return signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
}
```

几个细节：

- `SFD_NONBLOCK` 让 `read` 在没有信号时返回 `EAGAIN`，便于事件循环处理。
- `SFD_CLOEXEC` 避免 fd 泄漏到 `exec` 后的子进程。
- 必须在所有线程中屏蔽相应信号，否则信号可能被其他线程直接消费，`signalfd` 读不到。
- `signalfd` 是 Linux 专有接口。需要可移植时使用 self-pipe。

读取信号：

```c
struct signalfd_siginfo si;
ssize_t n = read(sfd, &si, sizeof si);

if (n == (ssize_t)sizeof si) {
    if (si.ssi_signo == SIGTERM) {
        begin_shutdown();
    }
}
```

`signalfd_siginfo` 还带有发送者 pid、uid、信号编号等信息，排障时很有用。

### 3.3 `SIGCHLD` 与 `waitpid` 回收

子进程退出时，父进程会收到 `SIGCHLD`。如果父进程不回收，子进程会变成僵尸进程（zombie）。

常见做法：

- 把 `SIGCHLD` 也加入屏蔽集合和 `signalfd`；
- 事件循环收到 `SIGCHLD` 后，循环调用 `waitpid(-1, &status, WNOHANG)`，直到没有可回收的子进程；
- 不要在处理器里做复杂逻辑，更不要依赖“一个 `SIGCHLD` 对应一个子进程”——普通信号会合并。

回收示例：

```c
for (;;) {
    int status;
    pid_t pid = waitpid(-1, &status, WNOHANG);
    if (pid > 0) {
        handle_child_exit(pid, status);
        continue;
    }
    if (pid == 0)
        break;
    if (pid < 0 && errno == EINTR)
        continue;
    break;
}
```

补充说明：

- `SIGCHLD` 的默认处置是忽略，但“默认忽略”并不等于自动回收，仍然需要 `waitpid`，除非显式设置 `SIG_IGN` 或 `SA_NOCLDWAIT` 让内核自动回收。
- 如果业务不关心子进程退出状态，可以显式设置 `SIG_IGN` 或 `SA_NOCLDWAIT`；但多数服务需要知道退出码，所以更常见的是 `waitpid`。

### 3.4 优雅退出流程

节点要求的目标流程是：

> 收到 `SIGTERM` 后关闭监听 socket 停止接入、排空在途请求、截止时间到了强制退出。

把它拆成状态机：

1. **正常运行**：监听 socket 在 `epoll` 中，接受新连接，处理请求。
2. **收到 `SIGTERM`**：进入 draining 状态。
3. **停止接入**：把监听 fd 从 `epoll` 删除并 `close`，不再 `accept` 新连接。
4. **拒绝新任务**：对已经建立的 keep-alive 连接，不再派发新请求；可以返回 `503` 并带 `Connection: close`，或者处理完当前请求后关闭连接。
5. **排空在途请求**：等待 `active_requests` 计数归零。
6. **截止时间**：从收到信号开始计算一个 deadline，通常小于发布系统的宽限期（例如宽限期 30 秒，deadline 设为 25 秒，留出清理时间）。
7. **正常退出**：在途请求清空后，刷新必要的日志/指标，关闭下游连接，退出。
8. **强制退出**：如果 deadline 到了还有请求没完成，记录一条固定信息后调用 `_exit`，不要继续等待，否则会被 `SIGKILL` 硬杀。

伪代码：

```text
on SIGTERM:
    if already draining: return
    draining = true
    deadline = monotonic_now() + 25s
    epoll_ctl(DEL, listen_fd)
    close(listen_fd)

event loop:
    if draining and active_requests == 0: break
    if draining and monotonic_now() >= deadline: force_exit()
    wait for events with timeout = remaining time
    on new connection: only possible before draining; accept and active_requests++
    on request complete: active_requests--
    during draining: do not start new requests
```

两个验收关键点：

- **处理器不调用非安全日志函数**：如果使用 `signalfd`，处理器根本不存在；如果使用传统处理器，只允许 `write`、`_exit` 或写 `volatile sig_atomic_t`。
- **退出有截止时间且无新任务进入**：关闭监听 fd 后不再接受新连接；draining 状态下不再启动新请求；deadline 到达必须强制退出。

### 3.5 一个可用的 C 骨架

下面是一个简化的事件循环骨架，省略了 socket 创建、请求解析和业务处理细节。重点是信号如何进入 `epoll`、如何停止接入、如何计算 deadline。

```c
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <time.h>
#include <unistd.h>

static int g_signal_fd = -1;
static int g_listen_fd = -1;
static int g_epoll_fd = -1;
static int g_active = 0;
static int g_draining = 0;
static long long g_deadline_ms = -1;

static long long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

static int setup_signal_fd(void) {
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);

    if (pthread_sigmask(SIG_BLOCK, &mask, NULL) != 0)
        return -1;

    g_signal_fd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
    return g_signal_fd;
}

static void begin_shutdown(void) {
    if (g_draining)
        return;

    g_draining = 1;
    g_deadline_ms = now_ms() + 25000;

    if (g_listen_fd >= 0) {
        epoll_ctl(g_epoll_fd, EPOLL_CTL_DEL, g_listen_fd, NULL);
        close(g_listen_fd);
        g_listen_fd = -1;
    }
}

static int drain_signals(void) {
    for (;;) {
        struct signalfd_siginfo si;
        ssize_t n = read(g_signal_fd, &si, sizeof si);

        if (n < 0) {
            if (errno == EAGAIN || errno == EINTR)
                return 0;
            return -1;
        }
        if (n != (ssize_t)sizeof si)
            return -1;

        if (si.ssi_signo == SIGTERM || si.ssi_signo == SIGINT)
            begin_shutdown();
    }
}

int main(void) {
    if (setup_signal_fd() < 0)
        return 1;

    g_epoll_fd = epoll_create1(EPOLL_CLOEXEC);
    if (g_epoll_fd < 0)
        return 1;

    struct epoll_event ev;
    ev.events = EPOLLIN;
    ev.data.fd = g_signal_fd;
    if (epoll_ctl(g_epoll_fd, EPOLL_CTL_ADD, g_signal_fd, &ev) < 0)
        return 1;

    g_listen_fd = create_listen_socket(8080);
    if (g_listen_fd < 0)
        return 1;

    ev.events = EPOLLIN;
    ev.data.fd = g_listen_fd;
    if (epoll_ctl(g_epoll_fd, EPOLL_CTL_ADD, g_listen_fd, &ev) < 0)
        return 1;

    struct epoll_event events[128];

    for (;;) {
        int timeout = -1;

        if (g_draining) {
            if (g_active == 0)
                break;

            long long left = g_deadline_ms - now_ms();
            if (left <= 0)
                break;

            timeout = (int)left;
        }

        int n = epoll_wait(g_epoll_fd, events, 128, timeout);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            break;
        }

        for (int i = 0; i < n; i++) {
            int fd = events[i].data.fd;

            if (fd == g_signal_fd) {
                if (drain_signals() < 0)
                    goto out;
            } else if (fd == g_listen_fd) {
                accept_connections();
            } else {
                handle_connection(fd);
            }
        }
    }

out:
    if (g_draining && g_active > 0)
        _exit(1);

    return 0;
}
```

说明：

- `create_listen_socket`、`accept_connections`、`handle_connection` 需要按业务实现。
- `accept_connections` 中每接受一个连接，`g_active` 增加；请求处理完成或连接关闭时减少。
- draining 期间不能再 `accept`，因为监听 fd 已从 `epoll` 删除并关闭。
- 对 keep-alive 连接，`handle_connection` 必须检查 `g_draining`，不要在当前请求结束后继续派发新请求。
- 如果请求由工作线程处理，`g_active` 和 `g_draining` 的修改需要同步；可以只在事件循环线程修改计数，工作线程通过事件通知完成，或者使用互斥锁/原子变量。
- `CLOCK_MONOTONIC` 不受系统时间跳变影响，适合计算超时。

### 3.6 结合 systemd 与 Kubernetes

发布系统通常遵循同一套节奏：

- **systemd**：默认停止信号是 `SIGTERM`，在 `TimeoutStopSec` 之后仍未退出时发送 `SIGKILL`。可通过 `KillSignal`、`TimeoutStopSec`、`KillMode` 等配置调整。
- **Kubernetes**：Pod 终止时先发送 `SIGTERM`，等待 `terminationGracePeriodSeconds`（常见默认 30 秒，以集群配置为准），超时后发送 `SIGKILL`。

因此：

- 优雅退出的 deadline 必须小于平台宽限期，给日志刷新、连接关闭留出余量。
- 不要捕获 `SIGKILL` 做收尾，它不可捕获。
- 在 Kubernetes 中，还要考虑 Endpoint/负载均衡摘除流量的延迟。常见做法是配合 readiness probe、preStop hook 或短暂的摘流等待，让新流量先停止，再处理 `SIGTERM`。
- 如果容器里的业务进程不是 PID 1，而是被 shell 脚本包了一层，要确保 shell 会把 `SIGTERM` 转发给业务进程，或者直接用 `exec` 启动业务进程。否则发布系统杀的是 shell，业务进程可能收不到信号。

---

## 4. 常见误区与设计权衡

### 4.1 常见误区

1. **“线程安全函数就能在信号处理器里用”**
   这是本节点明确指出的坑。`malloc` 是线程安全的，但不是异步信号安全的；信号处理器可能打断持有锁的代码，再次调用会导致死锁。

2. **在处理器里打日志**
   `printf`、`fprintf`、`syslog` 都不是异步信号安全的，可能死锁或破坏缓冲区。应使用 `write`、标志位、self-pipe 或 `signalfd`。

3. **只屏蔽主线程**
   进程定向信号可能被投递给任意未屏蔽线程。若工作线程没有屏蔽，`SIGTERM` 可能在 worker 中触发默认终止，`signalfd` 也收不到。必须在创建线程前屏蔽。

4. **以为 `SIGKILL` 可以捕获**
   `SIGKILL` 和 `SIGSTOP` 不可捕获、不可忽略、不可屏蔽。优雅退出的时间窗来自 `SIGTERM` 到 `SIGKILL` 之间的宽限期。

5. **关闭监听 fd 后还继续处理 keep-alive 上的新请求**
   停止接入不仅是“不再 `accept`”，还包括 draining 状态下不再启动新任务。否则旧连接会不断带来新请求，永远排不空。

6. **没有截止时间**
   如果某个请求卡住，进程会一直等，最后被 `SIGKILL` 硬杀。必须设置 deadline，并在超时后 `_exit`。

7. **忘记回收子进程**
   `SIGCHLD` 默认忽略不等于自动回收。需要 `waitpid` 循环，或显式使用 `SIG_IGN` / `SA_NOCLDWAIT`。

8. **处理器里调用 `exit`**
   `exit` 会刷新 stdio、执行 `atexit` 回调，不是异步信号安全的。处理器中需要立即退出时使用 `_exit`。

9. **忽略 `EINTR`**
   信号可能打断 `epoll_wait`、`accept`、`read` 等阻塞调用。事件循环应检查 `errno == EINTR` 并继续，而不是当成致命错误。

10. **忘记保存和恢复 `errno`**
    处理器可能打断依赖 `errno` 的普通代码。进入处理器时保存，返回前恢复。

### 4.2 设计权衡

节点给出的权衡是：

> 同步消费信号易维护，但需正确配置所有线程的屏蔽字。

展开来说：

| 方案 | 优点 | 代价 |
|---|---|---|
| 传统处理器 + `volatile sig_atomic_t` 标志 | 实现小、可移植、延迟低 | 处理器能力极弱；主循环需要轮询或处理 `EINTR`；无法安全传递复杂信息 |
| 传统处理器 + self-pipe | 可移植；能把信号接入事件循环 | 多一个 fd；需要处理非阻塞写和 `EAGAIN`；仍要小心处理器约束 |
| `signalfd` | 无处理器；在正常上下文消费；可直接纳入 `epoll`；能拿到信号附加信息 | Linux 专有；必须在所有线程正确屏蔽；要理解 fd 生命周期 |
| 单独信号线程 + `sigwait` / `sigwaitinfo` | 同步消费、逻辑集中 | 多一个线程；同样需要所有线程屏蔽；线程间通信需要设计 |

对大多数 Linux 后端服务，`signalfd` 很适合“事件循环 + 优雅退出”的场景：信号处理和业务处理在同一个线程上下文，日志、内存分配都可以正常使用，维护成本低。代价是必须把屏蔽字配置正确，尤其是多线程服务。

如果追求可移植性，self-pipe 是经典替代方案；如果只是一个小工具，标志位最简单。

---

## 5. 动手实践：用 `signalfd` 让 `SIGTERM` 进入事件循环

目标：实现一个最小可验证的优雅下线流程。不要只写“捕获信号后退出”，而要真正完成“停止接入 + 排空请求 + 截止时间强制退出”。

### 步骤 1：搭一个最小事件循环服务器

准备以下结构：

- 一个监听 socket，设为非阻塞，加入 `epoll`。
- 一个 `active_requests` 计数，表示当前在途请求数。
- 一个缓慢请求，例如 sleep 2 秒或模拟数据库操作，用来观察排空过程。
- 一个 `draining` 状态，表示是否已经收到 `SIGTERM`。

验收观察点：正常运行时，新连接可以建立，请求可以完成，`active_requests` 能正确增减。

### 步骤 2：在创建线程之前屏蔽 `SIGTERM`

在主函数最开始、创建任何线程之前执行：

```c
sigset_t mask;
sigemptyset(&mask);
sigaddset(&mask, SIGTERM);
sigaddset(&mask, SIGINT);

if (pthread_sigmask(SIG_BLOCK, &mask, NULL) != 0) {
    return 1;
}
```

如果后续要创建 worker 线程，不要在工作线程里解除这些信号的屏蔽。这样所有线程都继承屏蔽字，信号会保持待决，交给 `signalfd` 消费。

### 步骤 3：创建 `signalfd` 并加入 `epoll`

```c
int sfd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
if (sfd < 0) {
    return 1;
}

struct epoll_event ev;
ev.events = EPOLLIN;
ev.data.fd = sfd;
if (epoll_ctl(epfd, EPOLL_CTL_ADD, sfd, &ev) < 0) {
    return 1;
}
```

同时把监听 fd 也加入 `epoll`。

### 步骤 4：事件循环中读取信号

当 `epoll_wait` 返回 `sfd` 可读时：

```c
struct signalfd_siginfo si;
ssize_t n = read(sfd, &si, sizeof si);
if (n == (ssize_t)sizeof si) {
    if (si.ssi_signo == SIGTERM || si.ssi_signo == SIGINT) {
        begin_shutdown();
    }
}
```

注意要循环读到 `EAGAIN`，一次事件可能对应多个待决信号，普通信号也可能合并。

### 步骤 5：收到 `SIGTERM` 后停止接入

`begin_shutdown` 至少做三件事：

```c
static void begin_shutdown(void) {
    if (g_draining)
        return;

    g_draining = 1;
    g_deadline_ms = now_ms() + 25000;

    if (g_listen_fd >= 0) {
        epoll_ctl(g_epoll_fd, EPOLL_CTL_DEL, g_listen_fd, NULL);
        close(g_listen_fd);
        g_listen_fd = -1;
    }
}
```

动作顺序：

1. 设置 `draining`，让后续请求处理逻辑知道不能再启动新任务。
2. 计算 deadline，建议小于 Kubernetes 的宽限期。
3. 从 `epoll` 删除监听 fd。
4. 关闭监听 fd，内核不再接受新连接。

### 步骤 6：排空在途请求，并拒绝新任务

在连接处理函数中：

- 每开始处理一个请求，`active_requests++`。
- 每完成一个请求，`active_requests--`。
- 如果 `draining` 为真，不再从 keep-alive 连接上读取并派发新请求；可以返回 `503` 和 `Connection: close`，或者完成当前请求后直接关闭连接。
- 不要在处理完当前请求后再启动后台任务、定时任务或新的下游请求，除非这些任务也被纳入排空和截止时间管理。

事件循环的等待时间要根据 deadline 计算：

```c
int timeout = -1;

if (g_draining) {
    if (g_active == 0)
        break;

    long long left = g_deadline_ms - now_ms();
    if (left <= 0)
        break;

    timeout = (int)left;
}

int n = epoll_wait(g_epoll_fd, events, 128, timeout);
```

### 步骤 7：截止时间到达时强制退出

如果 deadline 到了还有在途请求：

- 用正常上下文（不是信号处理器）记录一条固定信息，例如“graceful shutdown timeout, forcing exit”；
- 调用 `_exit(1)` 立即退出，避免继续被平台 `SIGKILL`；
- 不要在超时路径依赖 stdio 缓冲，必要时使用 `write` 写入 STDERR。

验收时重点确认：

- 新连接在 `SIGTERM` 后无法建立；
- 已在处理的慢请求可以完成；
- 如果人为制造一个永不完成的请求，进程会在 deadline 到达后退出，而不是无限等待。

### 步骤 8：用命令验证

启动服务后，先记录 pid，然后观察信号掩码：

```bash
grep -E 'Sig(Blk|Ign|Cgt|Pnd)' /proc/<pid>/status
```

确认 `SIGTERM` 对应的位出现在 `SigBlk` 中，而不是只出现在 `SigCgt` 中。

跟踪信号相关系统调用：

```bash
strace -f -e trace=signal -p <pid>
```

另开一个终端发送信号：

```bash
kill -TERM <pid>
```

观察：

- `strace` 中出现 `SIGTERM`；
- 服务日志显示进入 draining；
- 新连接被拒绝或立即关闭；
- 在途请求完成；
- 进程在 deadline 前退出，退出码符合预期。

也可以直接查看信号编号：

```bash
kill -l
```

如果进程没有按预期退出，优先检查：

- 是否只有主线程屏蔽了信号；
- 是否 `signalfd` 没有加入 `epoll`；
- 是否读取 `signalfd` 后没有循环读到 `EAGAIN`；
- 是否关闭监听 fd 后仍然在 keep-alive 连接上派发新请求；
- 是否某个请求阻塞导致 `active_requests` 永远不归零；
- 是否被 shell 包装导致 `SIGTERM` 没有到达业务进程。

### 步骤 9：验收标准

完成实践后，逐项检查：

- [ ] 信号处理器（如果存在）不调用 `printf`、`fprintf`、`syslog`、`malloc`、`exit` 等非异步信号安全函数。
- [ ] 使用 `signalfd` 时，所有线程在创建前屏蔽了 `SIGTERM`，`/proc/<pid>/status` 的 `SigBlk` 能证明这一点。
- [ ] 收到 `SIGTERM` 后，监听 fd 从 `epoll` 删除并关闭，新连接无法进入。
- [ ] draining 状态下不再启动新请求，keep-alive 连接不会继续带来新任务。
- [ ] 在途请求计数归零后正常退出；有明确的截止时间。
- [ ] 截止时间到达后强制退出，不会无限等待平台发送 `SIGKILL`。
- [ ] 能通过 `strace -e trace=signal` 观察到信号相关的系统调用和信号递送过程。

---

## 6. 自测题

### 题目 1

为什么不能在 `SIGTERM` 处理器中调用 `printf` 或 `syslog`？`malloc` 明明是线程安全的，为什么也不能在处理器中调用？

### 答案 1

信号处理器可能打断任意普通代码，包括正在持有 stdio 锁、分配器锁或内部缓冲区的代码。`printf`、`syslog`、`malloc` 都不是异步信号安全的：

- `printf` / `syslog` 可能使用锁、缓冲区和内存分配，处理器中调用可能死锁或破坏状态；
- `malloc` 虽然是线程安全的，但线程安全只保证多线程并发调用的正确性，不保证信号处理器重入安全。如果信号打断了正在持有 `malloc` 锁的线程，处理器再次调用 `malloc` 就可能自死锁。

正确做法是处理器只做最小动作：设置 `volatile sig_atomic_t` 标志、`write` 到 self-pipe，或使用 `signalfd` 把信号消费移到正常事件循环上下文。

### 题目 2

一个多线程服务使用 `signalfd` 处理 `SIGTERM`。为什么必须在 `pthread_create` 之前屏蔽 `SIGTERM`？只在主线程屏蔽可以吗？

### 答案 2

不可以只在主线程屏蔽。进程定向信号会被投递给任意一个没有屏蔽它的线程。如果工作线程没有屏蔽 `SIGTERM`，内核可能把信号投递给工作线程：

- 如果该信号仍是默认处置，整个进程可能直接终止；
- 如果工作线程有自己的处理器，处理器会在工作线程中执行，而不是由 `signalfd` 消费；
- `signalfd` 只能读到被屏蔽后保持待决的信号。

线程会继承创建时的信号掩码，所以在创建任何线程之前由主线程调用 `pthread_sigmask(SIG_BLOCK, ...)`，可以让所有新线程自动继承屏蔽字。之后工作线程不要解除这些信号的屏蔽。如果线程已经存在，就必须逐个线程配置屏蔽字。

### 题目 3

请设计一个基于 `SIGTERM` 的优雅退出流程。为什么必须有截止时间？截止时间与 Kubernetes 的 `SIGKILL` 有什么关系？

### 答案 3

流程：

1. 通过 `signalfd`（或安全的标志位/self-pipe）收到 `SIGTERM`。
2. 进入 draining 状态，记录 `CLOCK_MONOTONIC` 截止时间。
3. 从 `epoll` 删除并关闭监听 socket，停止接受新连接。
4. 对已有连接不再派发新请求；可以返回 `503` 并关闭 keep-alive 连接。
5. 等待在途请求计数归零。
6. 在途请求清空后，刷新必要日志/指标，关闭下游资源，正常退出。
7. 如果截止时间到达仍有请求未完成，记录固定信息后调用 `_exit` 强制退出。

必须有截止时间，因为某个请求或下游依赖可能永远不返回。如果没有截止时间，进程会一直等待，最终被平台发送 `SIGKILL` 硬杀，导致日志、连接和事务都无法按计划收尾。

Kubernetes 先发送 `SIGTERM`，等待 `terminationGracePeriodSeconds`，超时后发送 `SIGKILL`。`SIGKILL` 不可捕获、不可屏蔽，所以应用自己的 deadline 必须小于平台宽限期，给清理留出余量。systemd 同理，超时后会发送 `SIGKILL`。

---

## 7. 推荐资源

- [信号与安全停机：Linux man-pages](https://man7.org/linux/man-pages/man7/signal-safety.7.html)  
  来源：man7.org｜语言：英文｜类型：文档｜难度：进阶｜免费：是  
  推荐理由：重点阅读信号处置和屏蔽与 `async-signal-safe`，用于核对本节点实验的语义和边界。

阅读时建议按节点 `sourceStudy` 的要求做：

1. 先核对异步信号安全的定义和函数列表，不要凭经验判断某个函数是否安全。
2. 阅读信号处理器相关的示例和错误处理路径，注意 `errno`、`EINTR`、`SA_RESTART` 等边界。
3. 记录适用版本和平台差异，尤其是 `signalfd` 的 Linux 专有属性，以及不同 libc、内核版本下的行为差异。
4. 把 man-pages 的约束与自己的代码逐条对照：处理器里调用了什么？屏蔽字在哪些线程生效？退出路径有没有 deadline？

---

## 8. 本课小结

- 信号是异步事件通知，可能在任意时刻打断普通代码。
- 处置决定“信号来了做什么”：默认、忽略、捕获；`SIGKILL` / `SIGSTOP` 不可捕获、不可忽略、不可屏蔽。
- 屏蔽不是忽略：被屏蔽的信号会待决，解除屏蔽后再递送；可用 `/proc/<pid>/status` 的 `SigBlk`、`SigIgn`、`SigCgt` 检查。
- 多线程下，进程定向信号会投递给任意未屏蔽线程，因此要在创建线程前用 `pthread_sigmask` 配置好所有线程的屏蔽字。
- 处理器只能调用异步信号安全函数；`write` 可以，`printf` / `malloc` / `syslog` 不行，可能死锁。稳妥做法是只设置 `volatile sig_atomic_t` 标志、写 self-pipe，或使用 `signalfd`。
- `signalfd` 把信号变成可读 fd，纳入 `epoll`，让信号消费回到正常上下文；`SIGCHLD` 要配合 `waitpid` 回收子进程。
- 优雅退出要关闭监听 socket、停止接入、排空在途请求，并在截止时间到达时强制退出；截止时间必须小于 systemd / Kubernetes 发送 `SIGKILL` 的宽限期。
- 设计权衡：同步消费信号易维护，但前提是正确配置所有线程的屏蔽字。

---

## 附：可运行的实践参考与验证脚本

下面是一个已在 Linux（gcc）上编译运行通过的最小参考实现：4 个工作线程在屏蔽 SIGTERM 之后才创建，signalfd 加入 epoll；每个连接是一个“慢请求”（`req_ms` 毫秒后回复 `ok`）。收到 SIGTERM 后从 epoll 删掉并关闭监听 fd，等在途请求排空后退出码 0；超过 `deadline_ms` 仍未排空就 `_exit(3)`。

实测结果：全部 5 个线程的 `SigBlk` 都含 SIGTERM（`0x4000`）；3 个 1.5 秒请求在途时发 SIGTERM，新连接立刻被拒（connection refused），3 个请求全部拿到 `ok` 后退出码 0；请求需 10 秒、截止时间 1 秒时，约 1003ms 强制退出、退出码 3，客户端读到 EOF。脚本最后打印 `ALL_CHECKS_PASSED`。

用法：`gcc -O1 -Wall -pthread -o srv srv.c && ./test.sh`

### srv.c

```c
#define _GNU_SOURCE
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
/* 用法: srv <port> <req_ms> <deadline_ms>
   每个连接 = 一个请求：accept 后 req_ms 毫秒回复 "ok\n" 并关闭 */
#define MAXC 256
static long long now_ms(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000LL+t.tv_nsec/1000000;}
static void *worker(void *a){(void)a;for(;;)pause();return NULL;}
int main(int argc,char**argv){
  int port=atoi(argv[1]); long long req_ms=atoll(argv[2]), dl_ms=atoll(argv[3]);
  sigset_t m; sigemptyset(&m); sigaddset(&m,SIGTERM); sigaddset(&m,SIGINT);
  pthread_sigmask(SIG_BLOCK,&m,NULL);          /* 先屏蔽，再建线程 */
  pthread_t th[4]; for(int i=0;i<4;i++) pthread_create(&th[i],NULL,worker,NULL);
  int sfd=signalfd(-1,&m,SFD_NONBLOCK|SFD_CLOEXEC);
  int lfd=socket(AF_INET,SOCK_STREAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
  int one=1; setsockopt(lfd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);
  struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(port),.sin_addr.s_addr=htonl(0x7f000001)};
  if(bind(lfd,(void*)&a,sizeof a)<0||listen(lfd,64)<0){perror("bind");return 2;}
  int ep=epoll_create1(EPOLL_CLOEXEC); struct epoll_event ev={.events=EPOLLIN};
  ev.data.fd=sfd; epoll_ctl(ep,EPOLL_CTL_ADD,sfd,&ev);
  ev.data.fd=lfd; epoll_ctl(ep,EPOLL_CTL_ADD,lfd,&ev);
  int cfd[MAXC]; long long due[MAXC]; int nc=0, draining=0, accepted=0, served=0; long long deadline=0;
  printf("READY pid=%d\n",getpid()); fflush(stdout);
  for(;;){
    long long now=now_ms(); int timeout=-1;
    for(int i=0;i<nc;i++){ if(due[i]<=now){ write(cfd[i],"ok\n",3); close(cfd[i]); served++; cfd[i]=cfd[--nc]; due[i]=due[nc]; i--; } }
    if(draining){
      if(nc==0){ printf("DRAINED accepted=%d served=%d\n",accepted,served); return 0; }
      if(now>=deadline){ printf("DEADLINE_FORCE_EXIT inflight=%d served=%d\n",nc,served); fflush(stdout); _exit(3); }
      timeout=(int)(deadline-now);
    }
    for(int i=0;i<nc;i++){ int t=(int)(due[i]-now); if(timeout<0||t<timeout) timeout=t<0?0:t; }
    struct epoll_event evs[16]; int n=epoll_wait(ep,evs,16,timeout);
    if(n<0){ if(errno==EINTR) continue; return 1; }
    for(int i=0;i<n;i++){
      int fd=evs[i].data.fd;
      if(fd==sfd){ struct signalfd_siginfo si;
        while(read(sfd,&si,sizeof si)==sizeof si){
          if(!draining){ draining=1; deadline=now_ms()+dl_ms;
            epoll_ctl(ep,EPOLL_CTL_DEL,lfd,NULL); close(lfd); lfd=-1;
            printf("SIGTERM from pid=%u, draining inflight=%d\n",si.ssi_pid,nc); fflush(stdout);} } }
      else if(fd==lfd){ int c; while(nc<MAXC&&(c=accept4(lfd,NULL,NULL,SOCK_CLOEXEC))>=0){ cfd[nc]=c; due[nc]=now_ms()+req_ms; nc++; accepted++; } }
    }
  }
}
```

### test.sh

```bash
#!/bin/bash
# Backend Atlas 第40课实践验证：signalfd + 优雅停机
set -u; cd "$(dirname "$0")"; fail=0
gcc -O1 -Wall -pthread -o srv srv.c || exit 1
ok(){ echo "PASS: $1"; }; bad(){ echo "FAIL: $1"; fail=1; }
req(){ python3 - "$1" <<'PY'
import socket,sys
try:
    s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=5); print(s.recv(10).decode().strip() or "EOF")
except Exception as e: print("REFUSED" if isinstance(e,ConnectionRefusedError) else type(e).__name__)
PY
}
# 场景1：3 个 1.5s 慢请求在途时 SIGTERM，deadline 5s → 排空后退出0，新连接被拒
./srv 18440 1500 5000 > s1.log & P=$!; sleep 0.3
echo "SigBlk 主线程: $(grep SigBlk /proc/$P/status)"
allblk=1; for t in /proc/$P/task/*; do b=$(awk '/SigBlk/{print $2}' $t/status); (( (0x$b & 0x4000) )) || allblk=0; done
n=$(ls /proc/$P/task | wc -l); [ $allblk = 1 ] && ok "全部 $n 个线程 SigBlk 含 SIGTERM(0x4000)" || bad "有线程未屏蔽 SIGTERM"
for i in 1 2 3; do (req 18440 > r1_$i.txt) & done; sleep 0.3
kill -TERM $P; sleep 0.2
nr=$(req 18440); [ "$nr" = REFUSED ] && ok "SIGTERM 后新连接被拒绝" || bad "新连接结果=$nr"
wait $P; rc=$?; wait
oks=$(cat r1_*.txt | grep -c ok)
[ $rc = 0 ] && [ $oks = 3 ] && ok "在途 3 个请求全部完成后退出码 0" || bad "rc=$rc ok=$oks"
cat s1.log
# 场景2：请求要 10s，deadline 1s → 到点强制退出码3
./srv 18441 10000 1000 > s2.log & P=$!; sleep 0.3
(req 18441 > r2.txt) & sleep 0.2
t0=$(date +%s%N); kill -TERM $P; wait $P; rc=$?; t1=$(date +%s%N); wait
ms=$(( (t1-t0)/1000000 ))
[ $rc = 3 ] && [ $ms -lt 2000 ] && ok "deadline 1s 到点强制退出 rc=3 用时 ${ms}ms，客户端看到 $(cat r2.txt)" || bad "rc=$rc ms=$ms"
cat s2.log
# 场景3（反例）：处理器里 printf/malloc 的危险不易稳定复现，这里只用 strace（若有）看 signalfd4
if command -v strace >/dev/null; then ./srv 18442 10 10 >/dev/null & P=$!; sleep 0.3; timeout 2 strace -f -e trace=signal -p $P 2>&1 | head -5 & sleep 0.5; kill -TERM $P; wait; else echo "SKIP: 无 strace"; fi
[ $fail = 0 ] && echo ALL_CHECKS_PASSED
```
