# 第 73/208 课：可重入性与协作调度安全

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 4 阶段 · 并发与内存模型（内存可见性与状态归属） |
| 前置 | e-tls、c-dlopen |
| 难度 | 深入 |
| 领域 | E |
| 标签 | Reentrancy、E、C++ |
| 子分类 | 内存可见性与状态归属 |
| 节点 ID | e-reentrant |
| 英文名 | Reentrancy |

> 上一课（第 72 课 e-tls）讲了同线程协程共享 `thread_local`：协程挂起、恢复，看到的是同一条线程上的同一份实例。第 44 课（c-dlopen）讲过动态加载、懒绑定与可重入性：`dlopen` 加载的库、PLT 懒解析的符号，都依赖一套"进程级全局状态"。本课把两者合起来：**在协作调度下，同一条 OS 线程会在「隐藏 yield 点」形成重入**——前一次执行还没有结束，同一条线程上的另一个协程就再次进入了同一段代码。线程安全审查给不出的结论，必须靠逐个调度点的检查补上。

---

## 导读

先定义本课的主角——可重入（reentrant）：

> 一个函数是可重入的，指它可以在前一次调用尚未结束时被再次进入，并且两次执行互不干扰、结果正确。

"再次进入"这个词听起来很玄，但它有两个完全真实的来源。第一个是经典的一类：**信号处理**。主流程刚调用 `strtok()` 用到一块内部静态缓冲，还没返回，一个信号到达，处理函数又调用了 `strtok()`——同一块静态缓冲被两个"执行流"同时使用，第二次执行的中间状态会污染第一次。经典教科书用 `strtok`、`localtime` 这类返回静态缓冲的函数讲这个，man-pages 给它们标注 `MT-Unsafe race:...`。

第二个来源就是本课的语境：**协作调度**。libco 一类的协程框架 hook 了 `read`、`write`、`connect`、`poll` 等阻塞系统调用，把它们改造成挂起点：调用被 hook 的函数时，调度器把当前协程的上下文（寄存器 + 栈）保存起来，让同一条线程去跑别的协程。这意味着：

- 看起来普通的函数——打一行日志、查一次配置、做一次 DNS 解析、调用一个第三方 SDK——内部可能在调用链深处挂起；
- 挂起后，**同一条线程**上的另一个协程再次进入同一个函数（以及它用到的同一块静态缓冲、同一份 TLS、同一个加载器路径）；
- 这和信号处理的重入本质相同：前一次执行未结束时再次进入。只不过信号由内核插进来，协程 yield 点由框架和你的调用链插进来。

放到微信公众号后台，这类问题的报障形态非常具体：

- 热更新/配置重载检查里用一块全局 scratch buffer 拼路径或解析配置文本，拼到一半 `yield` 了（检查过程要 stat 一次文件、读一次远端配置），同线程另一个协程把这块 buffer 覆写成自己的路径，恢复后热更新用错了 appid 的配置，回滚错误或者生效错误；
- 首次 `dlopen` 或懒绑定路径中挂起：第一次调用的符号解析、加载器的互斥区都是不可随意让出的区域，在这里插入 yield 后果依赖具体实现；
- 跨挂起点持有 `std::mutex`：协程 A 在同一条线程上拿到普通互斥锁后挂起，协程 B 恢复后去 `lock()` 同一把锁——锁属于这条线程已经持有了，B 的 `lock()` 会把**整条线程**卡死，不只是卡死 B，scheduler 也永远回不来；
- 打日志、查配置、DNS 解析看起来是纯 CPU 调用，实际内部可能遇到被 hook 的阻塞调用，从而形成你不知道的隐藏 yield 点。

本课要建立四个核心认识：

1. **重入的几何**：只要存在"同一执行流在前次未结束时再次进入"，就有重入问题；线程数不是关键，调度点才是关键。
2. **隐藏 yield 点**：阻塞调用被 hook 成挂起调用后，你的调用链深处藏着 yield；审查的第一步是把"所有潜在挂起调用"列出来。
3. **跨挂起点存活的状态**：静态缓冲、函数内 static 初始化状态、TLS、全局 scratch buffer，这些状态跨过挂起点依然共享；栈上的局部状态不会。
4. **锁的语义要换轴**：`std::mutex` 按线程归属和阻塞，天然与协作调度冲突；协程锁必须"让出"而不是"阻塞"。

---

## 学习目标

### goals

- 能解释隐藏 yield 点。
- 能独立完成：给热更新检查加入可控 yield，复现共享临时缓冲覆盖并改为局部拥有。

### acceptance

- 能列出所有潜在挂起调用。
- 不会跨挂起点持有阻塞同线程的普通互斥锁。

---

## 为什么后端要懂

本节点给出的 why 很直接：**审查 hook、全局缓冲、动态加载和热更新路径。** 这四类路径正是后台服务里重入问题的高发区，而且它们的共同特点是：出问题的代码"看起来线程安全"，bug 却真实发生。逐个看：

1. **hook 路径**。libco 类协程框架 hook 了 `read`/`write`/`connect`/`poll`/`gethostbyname` 等阻塞系统调用。任何业务代码只要在调用链深处触及这些符号——打日志写到落盘、查配置读文件、DNS 解析、调用第三方 SDK 发请求——都会隐式产生一个调度点。审查时如果你只在业务层找 `co_await`/`yield` 关键字，会漏掉一整类。
2. **全局缓冲路径**。为了性能，"返回静态缓冲指针"的库函数（`strerror`、`inet_ntoa`、`localtime` 一类）和你自己写的 `static char buf[...]` 拼接代码都依赖"这一次调用独占缓冲"这个前提。同步调用成立，协作调度不成立：中间一挂起，另一条协程把缓冲覆盖。
3. **动态加载路径**。首次 `dlopen`、首次调用走 PLT 懒解析的符号、加载器的全局锁，这些状态是进程级的，语义上"同一时间只有一个线程在解析"。协作调度下若在解析区域挂起，行为依赖于具体版本的加载器实现，不能想当然。
4. **热更新路径**。配置重载/热更新检查是最典型的"检查很长、中途必然有 IO"的代码，以及最典型"用全局临时缓冲"的代码。它平时低频、出事影响面大（配置串号等于线上行为突发改变），值得重点审查。
5. **锁误用路径**。用"线程安全"的思路给协程代码加 `std::mutex`，等于留下了同线程自锁的雷：协程 A 持锁挂起，同线程协程 B 去 `lock()`，整条线程卡死，假死时间从"卡死 B"扩大为"卡死该 worker 上所有请求"。

一句话：**懂可重入，你才知道"线程安全"审查在协程服务里还差哪一步；不懂它，你会把"函数标了 MT-Safe"当成"它在任何调度点上都安全"。**

---

## 直觉 → 原理

先明确两个术语的边界，本课全部按这个口径使用：

| 术语 | 定义 | 典型保障手段 |
| --- | --- | --- |
| 线程安全（MT-Safe） | 多个**线程**并发调用该函数，抽象机器行为仍正确 | 每次调用独立状态、无共享可变状态、内部互斥 |
| 可重入（reentrant） | 同一执行流在前一次调用**未结束时**再次进入该函数，仍正确 | 只用局部状态，或对共享状态的读写不跨"再次进入点" |

关系是：可重入 ⇒ 线程安全（一般成立），而线程安全 ⇏ 可重入。反例随手可得：一个内部用锁的函数可以是 `MT-Safe`，但同线程"持锁挂起后再进"就按第 3 节卡死；一个"每线程一份内部缓冲"的函数同样 `MT-Safe`（跨线程互不干扰），但同一条线程上的两条协程在挂起窗口里共享**同一份**缓冲，照样互相覆盖。`MT-Safe` 的保证没有覆盖"同线程一次执行到一半被再次进入"这种场景——而协作调度带来的正是**同一条线程内**的再次进入，它既不是多线程竞争（TSan 按线程看是顺序的），也不是线程安全标注描述的场景。这就是本课 pitfalls 的核心：**线程安全标注不能直接推导任意协程调度下可重入。**

---

### 1. 隐藏 yield 点：哪里会"前次未结束而再次进入"

#### 信号处理与协作调度：同一种重入

先用一个对照把"重入"钉住。经典信号场景：

```text
主流程:   strtok(s, del) ──(修改内部静态游标)── IO/暂停 ─── 恢复 ──继续
信号到达:            截断！处理函数又调 strtok(s2, del)
                     → 主流程的静态游标已被改写，返回值指向 s2 或覆盖
```

协作调度场景只是把"内核插入信号"换成了"框架插入 yield"：

```text
协程 A:   build_path(appid) ──(写入共享 scratch)── yield ─── 读 scratch ──发送
协程 B:                              ↑ yield 时调度器切进来
                                     又调 build_path(appid-B)，scratch 被覆盖
```

两个场景的共同点：**第一次执行在"写过共享状态、还没消费"的窗口里被打断，第二次执行进入同一段代码改写了共享状态。** 差别只是打断机制——信号是异步抢占，协作调度是同步让出，但重入的几何完全一样。

#### 成为挂起点的两种方式

在协程框架里，一个调用成为潜在挂起点的方式有两种：

1. **显式可控 yield**：你在业务代码里手工写的 `co_await` / `co_yield_to_sched()`。这类点看得见、可枚举。
2. **隐藏 yield 点**：你调用的函数（自己的或库的）内部，最终走到了被框架 hook 的阻塞调用。libco 的做法是把 `read`/`write`/`connect`/`poll` 等系统调用的入口替换成自己的包装：包装函数先尝试非阻塞，没就绪时把当前协程上下文存起来并切到调度器。于是「写日志 → write」「DNS 解析 → connect/gethostbyname」「第三方 SDK → read」全都成了潜在的挂起点，藏在你业务源码里根本看不到的位置。

审查口径也从这里来：**"所有潜在挂起调用" = 所有显式 yield + 所有终于走到被 hook 阻塞符号的调用链。**

#### 手写一个协作调度器，让重入显形

下面是一个教学用的协作调度器（ucontext 实现，轮询调度，单线程使用）。它把"保存上下文、切出去、再回来"做成两三行代码，没有任何魔法，目的就是让你看见切换发生在函数调用栈的**中间**：

```cpp
// coop_sched.h —— 教学用协作式调度器（ucontext，轮询调度，仅单线程使用）
#pragma once
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <ucontext.h>

constexpr std::size_t kStackSize = 256 * 1024;
constexpr int kMaxFibers = 8;

using TaskFn = void (*)(int);   // 任务是普通函数：挂起/恢复由 yield 控制

struct Fiber {
    ucontext_t ctx{};
    alignas(16) char stack[kStackSize];
    bool started = false;
    bool finished = false;
};

inline Fiber* g_fibers[kMaxFibers] = {};
inline TaskFn g_tasks[kMaxFibers] = {};
inline const char* g_names[kMaxFibers] = {};
inline int g_count = 0;
inline int g_cur = -1;              // 当前在跑的协程下标
inline ucontext_t g_sched_ctx{};    // 调度器自己的上下文（跑在主线程栈上）

// 让出：把"从当前调用点开始"的上下文保存进协程，跳回调度器
inline void co_yield_to_sched() {
    Fiber* f = g_fibers[g_cur];
    swapcontext(&f->ctx, &g_sched_ctx);
}

inline void fiber_trampoline(int idx) {
    g_tasks[idx](idx);
    g_fibers[idx]->finished = true;
    swapcontext(&g_fibers[idx]->ctx, &g_sched_ctx);
}

inline void co_add(const char* name, TaskFn fn) {
    int i = g_count++;
    g_fibers[i] = new Fiber();
    g_tasks[i] = fn;
    g_names[i] = name;

    getcontext(&g_fibers[i]->ctx);
    g_fibers[i]->ctx.uc_stack.ss_sp = g_fibers[i]->stack;
    g_fibers[i]->ctx.uc_stack.ss_size = kStackSize;
    g_fibers[i]->ctx.uc_link = nullptr;
    makecontext(&g_fibers[i]->ctx,
                reinterpret_cast<void (*)(void)>(fiber_trampoline), 1, i);
}

inline const char* co_current_name() {
    return g_cur >= 0 ? g_names[g_cur] : "main";
}

// 轮询调度：每轮把每个未完成协程跑到下一个 yield 点
inline void co_sched_loop() {
    for (;;) {
        bool any_alive = false;
        for (int i = 0; i < g_count; ++i) {
            Fiber* f = g_fibers[i];
            if (!f || f->finished) continue;
            any_alive = true;
            g_cur = i;
            // 保存调度器上下文，跳进协程；协程让出时从保存点回来
            swapcontext(&g_sched_ctx, &f->ctx);
        }
        if (!any_alive) break;
    }
}

inline void co_clear() {
    for (int i = 0; i < kMaxFibers; ++i) {
        delete g_fibers[i];
        g_fibers[i] = nullptr;
        g_tasks[i] = nullptr;
        g_names[i] = nullptr;
    }
    g_count = 0;
    g_cur = -1;
}
```

注意 `co_yield_to_sched()` 的位置：它 `inline` 定义在头文件里，**可以被任何调用深度的代码吸收进自己的调用链**——在一个函数调用链中间某个位置被调用时，swapcontext 保存的是"从那个位置开始"的整套上下文。这就是"隐藏 yield 点"的实现本质：真实框架的 `read()` 包装里不过是写了一行同样的逻辑，藏在了你看不见的地方。

用它演示"调用链深处的隐藏 yield"：

```cpp
// hidden_yield.cpp
#include "coop_sched.h"

// 模拟被 hook 的阻塞调用：真实框架里 read()/connect() 就长这样
static void hooked_read(const char* tag) {
    std::printf("[%s]   hooked_read(%s): 进入系统调用包装\n", co_current_name(), tag);
    co_yield_to_sched();                       // ← 隐藏 yield 点
    std::printf("[%s]   hooked_read(%s): 恢复\n", co_current_name(), tag);
}

static void load_config() { hooked_read("config/app.json"); }
static void lookup_perm() { hooked_read("perm/check"); }

static void task_handle_msg(int self) {
    (void)self;
    std::printf("[%s] 处理消息开始\n", co_current_name());
    load_config();       // 调用链深处挂起：handle_msg → load_config → hooked_read
    lookup_perm();
    std::printf("[%s] 处理消息结束\n", co_current_name());
}

static void task_report(int self) {
    (void)self;
    std::printf("[%s] 生成报表开始\n", co_current_name());
    load_config();
    std::printf("[%s] 生成报表结束\n", co_current_name());
}

int main() {
    co_add("A协程", task_handle_msg);
    co_add("B协程", task_report);
    co_sched_loop();
    return 0;
}
```

编译运行（`coop_sched.h` 与源码放同目录）：

```bash
g++ -std=c++17 -pthread -O2 hidden_yield.cpp -o hidden_yield
./hidden_yield
```

预期输出（A 在 `load_config` 内部挂起，调度器切到 B；B 随即在 `load_config` 同一位置挂起；再轮到 A 恢复）：

```text
[A协程] 处理消息开始
[A协程]   hooked_read(config/app.json): 进入系统调用包装
[B协程] 生成报表开始
[B协程]   hooked_read(config/app.json): 进入系统调用包装
[A协程]   hooked_read(config/app.json): 恢复
[A协程]   hooked_read(perm/check): 进入系统调用包装
[B协程]   hooked_read(config/app.json): 恢复
[B协程] 生成报表结束
[A协程]   hooked_read(perm/check): 恢复
[A协程] 处理消息结束
```

看这个输出序列：`A 的 load_config` 没返回，`B 的 load_config` 已经跑到了同一行。**同一个函数、同一份代码，在同一条线程上前次未结束而再次进入——这就是协作调度下的重入。** 没有第二个线程，没有信号，但 bug 的几何完全一样。

#### 怎么把潜在挂起调用枚举出来

审查时你要回答的问题是："这批代码里，哪些调用可能在内部挂起？"。实用工具与顺序：

```bash
# 1. 在二进制层面看"实际会翻哪些阻塞符号"：U = undefined 未定义符号（导入）
nm -D ./hidden_yield_demo_app | grep -E " U (read|write|connect|poll|epoll_wait|getaddrinfo|gethostbyname)"

# 2. 动态跟踪真实调用序列（间接证实走到哪）：
strace -f -e trace=connect,read,write,poll ./app 2>&1 | head -40
ltrace -e "read+write+connect+poll" ./app 2>&1 | head -20

# 3. 静态 grep 自己代码的显式 yield（与可控挂起点清单对照）
grep -rn "co_yield\|co_await" src/

# 4. 对每个第三方依赖，问"它内部会不会调到上面这些符号"——读它源码或文档，
#    结合 man 页面的 attributes 标注（见第 4 节）
```

`nm -D` 给你"可能会走到的阻塞符号候选"，`strace`/`ltrace` 给你"运行时真实走过的样本"，两者对照，再结合对调用链的阅读，就能建立**潜在挂起调用清单**。这是本课第一个验收目标的实操方式。

---

### 2. 静态缓冲与初始化状态：跨挂起点还共享的状态

挂起不改变状态的归属，只创造"有人趁机改写"的机会。那么哪些状态在挂起后**仍然被两条协程共享**？按层次列出：

| 状态 | 共享范围 | 对象示例 |
| --- | --- | --- |
| 全局/命名空间级 `static` 变量 | 进程所有线程、所有协程 | `static char g_scratch[N];` |
| 函数内 `static` 局部变量 | 同上（一个实例） | 一次性初始化 flag、全局缓存、token 表 |
| 静态/全局数组实现的"可复用缓冲" | 同上 | path 拼接、error text、序列化缓冲 |
| `thread_local` | 同一条 OS 线程上的所有协程（第 72 课） | TLS 里的请求上下文、TLS 缓冲 |
| 栈上局部变量 | 单个调用栈 | 你的协程帧、ucontext 栈、`char buf[64];` |

前四类跨挂起点共享，最后一类不共享——协程挂起时整套帧（ucontext 栈或 C++20 协程帧）原样保存，恢复时原样回来。**"改为局部拥有"的修复思路，本质就是把状态从上四类挪到最后一类。**

#### 实验：共享 scratch buffer 在挂起窗口被覆盖

```cpp
// static_buf.cpp
#include "coop_sched.h"

// 命名空间级 static 缓冲：进程内只有这一份
static char g_scratch[128];

static const char* compose_key(const char* appid) {
    std::snprintf(g_scratch, sizeof(g_scratch), "key:%s", appid);
    return g_scratch;   // 返回指向共享缓冲的指针——危险返回形态
}

static void task_bug(int self) {
    const char* appid = (self == 0) ? "wx-app-a" : "wx-app-b";
    const char* key = compose_key(appid);
    std::printf("[%s] 拿到 key 指针: %s (%p)\n", co_current_name(), key,
                static_cast<const void*>(key));
    co_yield_to_sched();   // 挂起窗口：另一个协程会改写 g_scratch
    std::printf("[%s] 恢复后使用 key: %s (%p) -> %s\n", co_current_name(), key,
                static_cast<const void*>(key),
                std::strstr(key, appid) ? "正常" : "已被覆盖!");
}

// ---------- 修复：调用方/协程拥有的局部缓冲 ----------
static void compose_key_local(const char* appid, char* buf, std::size_t n) {
    std::snprintf(buf, n, "key:%s", appid);
}

static void task_fixed(int self) {
    const char* appid = (self == 0) ? "wx-app-a" : "wx-app-b";
    char key[64];          // 协程栈上：跟着这次调用走，跨挂起点独占
    compose_key_local(appid, key, sizeof(key));
    std::printf("[%s] 拿到局部 key: %s (%p)\n", co_current_name(), key,
                static_cast<const void*>(key));
    co_yield_to_sched();
    std::printf("[%s] 恢复后使用 key: %s (%p) -> %s\n", co_current_name(), key,
                static_cast<const void*>(key),
                std::strstr(key, appid) ? "正常" : "已被覆盖!");
}

int main() {
    std::printf("======== bug 版：共享 static 缓冲 ========\n");
    co_add("A", task_bug);
    co_add("B", task_bug);
    co_sched_loop();
    co_clear();

    std::printf("======== fixed 版：调用方持有局部缓冲 ========\n");
    co_add("A", task_fixed);
    co_add("B", task_fixed);
    co_sched_loop();
    co_clear();
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 static_buf.cpp -o static_buf
./static_buf
```

预期输出：

```text
======== bug 版：共享 static 缓冲 ========
[A] 拿到 key 指针: key:wx-app-a (0x5562a6b0f520)
[B] 拿到 key 指针: key:wx-app-b (0x5562a6b0f520)
[A] 恢复后使用 key: key:wx-app-b (0x5562a6b0f520) -> 已被覆盖!
[B] 恢复后使用 key: key:wx-app-b (0x5562a6b0f520) -> 正常
======== fixed 版：调用方持有局部缓冲 ========
[A] 拿到局部 key: key:wx-app-a (0x5562df580600)
[B] 拿到局部 key: key:wx-app-b (0x5562df5c09f0)
[A] 恢复后使用 key: key:wx-app-a (0x5562df580600) -> 正确
[B] 恢复后使用 key: key:wx-app-b (0x5562df5c09f0) -> 正确
```

三个细节值得读：

1. **A 和 B 拿到的是同一个地址**（`0x5562a6b0f520`）。静态缓冲只有一份，这是"覆盖"的结构前提。
2. **B 显示"正常"**。谁最后写、谁读到自己的值："最后一次写入者获胜"。所以这类 bug 只惩罚**中间受害者**——很像线上"偶发串号，一半协程永远没事"的现象，重放不出来。
3. **修复后地址不同**。局部缓冲跟各自协程栈走，覆盖在物理上不可能发生。

#### 库函数里的同类问题：strerror / inet_ntoa / localtime

同一个结构在 libc 里大量存在，man-pages 对它们有明确标注（第 4 节详细讲读法）：

- `strerror`：`MT-Unsafe race:strerror` —— 错误文本写入内部静态缓冲并返回其指针；
- `inet_ntoa`：`MT-Unsafe race:inet_ntoa` —— 把网络地址点分十进制文本写入内部静态缓冲并返回其指针；
- `localtime`：`MT-Unsafe env race:tmbuff` —— 返回指向内部静态 `struct tm` 的指针，且受环境变量（`TZ`）影响。

libc 还提供 `_r` 变体作为官方答案：`strerror_r(code, buf, n)`、`inet_ntop(af, src, dst, size)`，即由**调用方提供缓冲**——和上面 `compose_key_local` 是同一个形状。自己的代码应直接按这个形状写，不要指望"库替你保证"。

#### 另一半：函数内 static 的一次性初始化状态

静态缓冲是"每次调用都会用到的共享状态"，还有一类是"只有一次初始化窗口的共享状态"：

```cpp
// init_state.cpp 片段
static int deep_status(const char* name) {
    static bool s_inited = false;    // 同一实例，同线程全部协程共享
    static int   s_tick  = 0;
    if (!s_inited) {
        s_inited = true;             // ← 初始化**进行到一半**的窗口在此打开
        // ……昂贵初始化：首次 dlopen、懒绑定、加载词典等……
        //   若中间挂起，其它协程会认为"已初始化"而直接使用未完成的状态
    }
    return ++s_tick;
}
```

风险窗口是"标志先行、资源未就绪"。这一点下文第 5 节连同预热一起处理；本段你只需要建立区分意识：**一次性初始化路径**和**每次调用路径**是两类不同的暴露面，修复手段不同。

---

### 3. 线程锁与协程锁语义：`std::mutex` 为什么会卡死整条线程

`std::mutex`（以及 `pthread_mutex_t`）的语义是按**线程**定义的：锁记录持有者线程，`lock()` 在被倚占时阻塞**调用所在线程**。协作调度把同一时刻并发的两条协程叠放进同一条线程，这两条语义都会出问题：

- **同线程重入**：协程 A 持锁挂起后，同线程协程 B 再去 `lock()` 这把锁。对 mutex 来说，这是"同一条线程对非递归互斥锁重复上锁"——未定义行为，典型表现是 futex 等待，**整条线程卡死**。
- **阻塞 vs 让出**：即使不在同一条线程（多 worker 也存在），`lock()` 阻塞的也是整个线程而不是单个协程。线程卡住时，调度器无法运行其它协程，worker 上的所有请求都停摆。正确的原则是：协程要等，就让出；不要阻塞它所在的线程。

实验演示（整条线程卡死是可观察的）：

```cpp
// mutex_deadlock.cpp
#include <mutex>
#include <cstdio>
#include "coop_sched.h"

std::mutex g_mu;   // 普通（非递归）互斥锁

static void task_hold(int self) {
    (void)self;
    std::printf("[%s] lock() 拿到锁\n", co_current_name());
    g_mu.lock();
    std::printf("[%s] 锁已拿到，接下来挂起（缺陷：跨挂起点持锁）\n", co_current_name());
    co_yield_to_sched();
    std::printf("[%s] 恢复（到不了这里）\n", co_current_name());
}

static void task_take(int self) {
    (void)self;
    std::printf("[%s] 尝试 lock()：锁已被本线程持有，同线程非递归锁重复上锁是未定义行为\n",
                co_current_name());
    g_mu.lock();   // 实测表现：整条线程卡死，再也回不到调度器
    std::printf("[%s] 意外拿到锁（理想情况下到不了这里）\n", co_current_name());
    g_mu.unlock();
}

int main() {
    co_add("A协程", task_hold);
    co_add("B协程", task_take);
    co_sched_loop();
    std::printf("调度结束（到不了这里）\n");
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 mutex_deadlock.cpp -o mutex_deadlock
timeout 3 stdbuf -o0 ./mutex_deadlock; echo "exit=$?  (124=超时：线程卡死)"
```

预期输出：

```text
[A] lock() 拿到锁
[A] 锁已拿到，接下来挂起（缺陷：跨挂起点持锁）
[B] 尝试 lock()：锁已被本线程持有，同线程非递归锁重复上锁是未定义行为
exit=124  (124=超时：线程卡死)
```

（`stdbuf -o0` 是为了把 stdout 不受超时影响地刷出来；否则缓冲区内容会随进程被 kill 丢失。）

**规则：不跨挂起点持有阻塞同线程的普通互斥锁。** 展开为操作口径：

1. 在每一个 `yield`/`co_await` 点之前，确认当前没有持有 `std::mutex`/`pthread_mutex_t`/自旋锁（它们阻塞/自旋的对象是线程）；
2. 建临界区时，先判断其中会不会挂起；会挂起，临界区里就不能有（移走挂起点或改用协程锁）；
3. 或换用**协程锁**：让等待者 yield，而不是把线程投入 `futex wait`。

教学版协程锁长这样（配合上面的调度器）：

```cpp
// co_mutex.cpp
#include <cstdio>
#include <cstring>
#include <string>
#include "coop_sched.h"

// 协程锁：占用时，尝试者**让出**（yield），不阻塞线程
struct CoMutex {
    bool held = false;

    void lock() {
        while (held) {
            std::printf("[%s] 锁被占用：让出调度而不是阻塞线程\n", co_current_name());
            co_yield_to_sched();
        }
        held = true;
        std::printf("[%s] 拿到协程锁\n", co_current_name());
    }
    void unlock() {
        std::printf("[%s] 释放协程锁\n", co_current_name());
        held = false;
    }
};

CoMutex g_cm;
static char g_scratch[64];

// 用协程锁保护"写共享缓冲 + 跨过一次挂起 + 拷贝出来"的完整窗口
static std::string compose_token(const char* appid) {
    g_cm.lock();
    std::snprintf(g_scratch, sizeof(g_scratch), "token(%s)", appid);
    co_yield_to_sched();                 // 期间其它协程只会让出等待，不卡线程
    std::string token(g_scratch);        // 解锁前**拷贝**出来（变成自己的状态）
    g_cm.unlock();
    return token;
}

static void task_use(int self) {
    const char* appid = (self == 0) ? "wx-app-a" : "wx-app-b";
    std::string tok = compose_token(appid);
    bool ok = tok.find(appid) != std::string::npos;
    std::printf("[%s] 使用 token: %s -> %s\n", co_current_name(), tok.c_str(),
                ok ? "正常" : "被覆盖");
}

int main() {
    co_add("A协程", task_use);
    co_add("B协程", task_use);
    co_sched_loop();
    co_clear();
    return 0;
}
```

预期输出：A 先锁住并挂起；B 尝试时打印"锁被占用：让出调度"并 yield；A 恢复后完成拷贝与释放；B 在下一轮拿到锁，输出正常。注意这段演示里"持锁跨挂起"对**协程锁**是安全的——因为等待者让出而不是阻塞线程；换成 `std::mutex`，同样的使用方式就是上面的卡死案例。两种锁在使用方代码上几乎没有差别，差异只在 `lock()` 内部行为——**这正是本课把它们严格区分的原因**：普通互斥锁的价值在抢占调度，协程锁的价值在协作调度。

还要修正两个边缘直觉：

- 把 `std::mutex` 换成 spinlock 也救不了：自旋同样占用这条线程的 CPU，等待者转起来时调度器不能切走，协程 A 也不会被调度回来，死锁只是从"futex 等待"变成"忙等"。
- `std::recursive_mutex` 可以避免"同线程重复上锁未定义"吗？对，它允许同线程重复加锁，但它仍然阻塞**其它线程的等待者**，且把可变共享窗口继续跨挂起延长——它消除的是 UB 表现，不是协作调度的矛盾。

---

### 4. 版本与调度点检查：attributes 标注怎么读

Linux man-pages 有一份专门讲函数并发属性的文档：`attributes(7)`（见推荐资源）。它把"安全"拆成几个轴，每个标注都是**在某类并发模式下的保证声明**：

| 标注 | 含义 | 面向的并发模式 |
| --- | --- | --- |
| `MT-Safe` | 多线程并发调用保证抽象机器行为正确 | 多线程 |
| `MT-Unsafe` | 多线程并发不保证正确 | 多线程 |
| `race:<id>` | 与其它调用该 `<id>` 缓冲/对象的函数或同一缓冲的调用竞争 | 多线程（对象级竞争） |
| `env` | 受环境变量影响（如 `TZ` 触发懒初始化） | 进程全局状态 |
| `init` | 内部含一次性初始化 | 初始化路径 |
| `lock` | 内部使用锁 | 锁的可见性 |
| `AS-Safe`（async-signal-safe） | 可在信号处理函数内安全调用 | 异步抢占：信号 |
| `AC-Safe`（async-cancel-safe） | 可在异步取消点安全调用 | 线程取消 |

读法示例（真实标注，可直接验证）：

```bash
man 3 attributes            # 词汇表：各轴的含义与组合方式
man 3 strerror | grep -B1 "MT-Unsafe"      # → MT-Unsafe race:strerror
man 3 inet_ntoa | grep -B1 "MT-Unsafe"     # → MT-Unsafe race:inet_ntoa
man 3 localtime | grep -B1 "MT-Unsafe"     # → MT-Unsafe env race:tmbuff
```

`race:strerror` 的意思是：多个调用共享一个叫 `strerror` 的内部缓冲对象，并发调用会互相覆盖——这正是第 2 节 `g_scratch` 的库版本。`race:tmbuff` + `env` 表示共享的 `struct tm` 静态区和环境触发的懒初始化。

#### 为什么这些标注不能直接推导"任意协程调度下可重入"

三个原因，对应三点审查要求：

1. **轴不对齐**。`MT-Safe`/`race:` 描述的是**多线程**并发；协作调度的重入是**同一条线程**上的再次进入。一个 `MT-Safe` 函数完全可能内部用锁（部分标注含 `lock`）——多线程下正确，同线程"持锁 + 挂起后 yield 再进"就按第 3 节卡死。
2. **窗口假设不对齐**。线程互斥下，两次并发调用不会在同一线程的同一调用**中间**交错；协作调度恰恰允许在任意挂起点交错。`MT-Safe` 只保证"完整调用之间"的正确，不保证"一次调用执行到一半被再次进入"——信号处理函数为什么不安全就写到了这一层（所以有 `AS-Safe` 轴）。而隐藏 yield 点引入的，正是"调用执行到一半"的效果。
3. **按具体版本与调度点检查**。标注会随版本改写（glibc 版本维护者会把一个函数从 `MT-Unsafe` 补丁成 `MT-Safe`，反之也会发现并纠正错误标注；加载器、hook 的行为也取决于版本）。所以正确动作是把"排查+标注+版本"三件事像下面这样记录成检查表，而不是背结论：

```text
函数              man 标注                      我们调度点下的结论
strerror          MT-Unsafe race:strerror       共享缓冲 → 协程重入不安全，用 strerror_r
inet_ntoa         MT-Unsafe race:inet_ntoa      同上 → 用 inet_ntop + 调用方缓冲
localtime         MT-Unsafe env race:tmbuff     同上 → 用 localtime_r + 调用方缓冲
std::to_string    （按具体实现复核）            无静态缓冲依赖的典型，仍建议在锁定的实现上复核
你的 compose_key  自审                          本课实验：跨挂起点改局部拥有
```

#### 动态加载与懒绑定：首次解析路径在哪里

第 44 课（c-dlopen）给过结论：ELF 程序对外部函数的调用默认走 PLT 懒解析——第一次调用某个符号时，控制流先进加载器（`_dl_runtime_resolve` 类逻辑），查表、绑定、回写 GOT，然后才真正调用目标函数。这段路径有两个特征：

- 它发生在**你的业务代码之外**、且发生在"你调用那个函数的第一次"，源码层面不可见；
- `dlopen` 本身要拿加载器全局锁并可能执行 mmap/unmap、重定位。

用 `LD_DEBUG` 可以直接观察懒绑定的时机——绑定发生在**第一次调用之后**：

```bash
cat > lazy_demo.cpp <<'EOF'
#include <cstdio>
int main() {
    std::printf("第一次调用 printf 前\n");
    std::printf("第二次调用 printf\n");
    return 0;
}
EOF
g++ -std=c++17 -pthread -O2 lazy_demo.cpp -o lazy_demo
# 懒绑定：注意 puts（printf 编译优化为 puts）的绑定行出现在"第一次调用"之后
LD_DEBUG=bindings ./lazy_demo 2>&1 | grep -E "normal symbol \`puts\`|第一次调用"
```

输出按顺序分为两类行（行号每次运行不同，看的是顺序）：

```text
 106:     189489: binding file .../lazy_demo to /lib/x86_64-linux-gnu/libc.so.6 ... normal symbol `puts` ...
 116: 第一次调用 printf 前
```

绑定日志行（106）出现在第一次 `printf` 输出（116）之前、且紧随其后——**绑定就是那次调用触发的**。这时的 `puts` 还走懒解析。加 `LD_BIND_NOW=1` 后重跑，绑定行会移到启动期（本机实验中在第 89 行附近）、远在任何业务输出之前，即"启动时全部解析，不走首次调用路径"。

```bash
LD_DEBUG=bindings LD_BIND_NOW=1 ./lazy_demo 2>&1 | grep -E "normal symbol \`puts\`|第一次调用"
```

审查口径由此而来：**首次解析、首次初始化区是一个调度点敏感区**。在你尚未验证之前，默认"不要在可能含首次解析/首次初始化的调用链中间插显式挂起点"。稳妥的收束方式，见下一节。

---

### 5. 启动阶段预热：只覆盖一次性初始化路径

预热（warmup）的思路是：服务启动、还没有请求进来、调度还没运行起来时，把"后面的请求才会第一次触发的昂贵路径"**提前、显式地触发完一次**，让之后的调用走稳态路径。可用手法：

- 启动函数里调用一次昂贵的"first-call"接口，例如把配置库 `dlopen` 后 `dlsym` 出函数表手动激活一遍；
- 代码内预热：在 `main()`/启动钩子里直接调用一次会触发懒初始化的函数；
- 环境级预热：`LD_BIND_NOW=1` 让加载器在启动期完成全部符号绑定，PLT 懒解析路径整体消失；
- `RTLD_NOW` 代替 `RTLD_LAZY`：`dlopen` 时立刻解析全部重定位，而不是各条懒解析在调用点逐个发生。

一个完整、可编译的演示，兼述预热的**局限**：

```cpp
// warmup_limit.cpp
#include <cstdio>
#include <cstring>
#include <string>
#include "coop_sched.h"

static bool g_inited = false;

// 一次性初始化路径：预热**能**消掉它的首次风险窗口
static void one_time_init() {
    if (g_inited) return;
    std::printf("[init] 一次性初始化（模拟懒绑定/首次 dlopen 路径，昂贵）\n");
    g_inited = true;
}

// 每次调用都会用的共享缓冲：预热**不能**消掉它的覆盖风险
static char g_token[64];

static std::string lookup_token_scratch(const char* appid) {
    one_time_init();        // 第二次起直接返回——预热消掉了首次窗口
    std::snprintf(g_token, sizeof(g_token), "token(%s)", appid);
    co_yield_to_sched();    // 缺陷点：跨挂起点引用共享缓冲
    return std::string(g_token);   // 恢复后读到的可能已经是别人的 token
}

static void task_use(int self) {
    const char* appid = (self == 0) ? "wx-app-a" : "wx-app-b";
    std::string tok = lookup_token_scratch(appid);
    bool ok = tok.find(appid) != std::string::npos;
    std::printf("[%s] 拿到 token: %s（预期含 %s） -> %s\n",
                co_current_name(), tok.c_str(), appid, ok ? "正常" : "被覆盖");
}

int main() {
    // —— 启动预热：此时还没有其它协程，只有 main ——
    std::printf("[main] 启动预热：先触发一次 one_time_init\n");
    one_time_init();

    co_add("A", task_use);
    co_add("B", task_use);
    co_sched_loop();
    co_clear();
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 warmup_limit.cpp -o warmup_limit
stdbuf -o0 ./warmup_limit
```

预期输出：

```text
[main] 启动预热：先触发一次 one_time_init
[init] 一次性初始化（模拟懒绑定/首次 dlopen 路径，昂贵）
[A] 拿到 token: token(wx-app-b)（预期含 wx-app-a） -> 被覆盖
[B] 拿到 token: token(wx-app-b)（预期含 wx-app-b） -> 正常
```

预热照做了：`one_time_init` 只在 main 里执行了一次，调度期里各协程正文的 `one_time_init()` 都直接返回——**一次性初始化路径的首次风险窗口确实被消掉了**。但输出里返回的 token 仍然是 `token(wx-app-b)`，A 协程的 token 被覆写——**静态覆盖 bug 原样复现**。这就是本课 tradeoffs 的精确含义：**预热只覆盖特定首次路径**（一次性初始化、懒绑定、首次 `dlopen`），**每次调用都会用到的全局临时状态，预热完全覆盖不了**，必须逐点修：局部拥有或协程锁。

预热也不是"万能保险"还有一层原因：它只在你确定的那些首次路径上生效。第三方库里的首次路径（内部懒初始化、单例、DH/dns 缓存填充）需要你去发现；发现不了就不存在"预热到它"。所以把预热定位成**沿着热更新检查清单上的某一条路径做的定向补丁**，而不是"协程安全修复"。

---

## 场景示例

### 场景一：热更新检查里全局 scratch buffer 串 appid

配置重载定时任务对 200 个 appid 逐个做"读远端配置 → 校验 → 落盘"的检查。检查函数用全局 `static char g_path[...]` 拼配置路径，检查中段有 `yield`（等网络读回）。app-wx-a 的检查挂起后，app-wx-b 的检查把 `g_path` 覆写成自己的路径；A 恢复后按 `g_path` 里现存的路径校验、又把"app-wx-b 的配置"写到了"app-wx-a 的配置文件位置"。表现是热更新后部分号的行为突然变成另一个号的配置；冲突只在两条协程的挂起窗口前后错开特定距离时出现，难以回放。

### 场景二：首次 dlopen / 懒绑定挂起

服务在忙时才第一次使用某个功能（如签名库、某个第三方 SDK），首次调用走 `dlopen` + `dlsym` 或 PLT 懒解析。恰好有工程师给这段调用链加了跟踪点（插了 yield）。首次解析路径挂起后另一个协程再次调用同一符号，两个协程同时处于"解析到一半"的状态。表现依赖具体版本的加载器实现——这正是第 4 点强调"依据具体版本与调度点检查"的原因；修复方向是启动预热（`RTLD_NOW`/首调一次）或确保解析区不含挂起点。

### 场景三：跨挂起点持有 std::mutex 卡死 worker

给"检查唯一性"加了把 `std::mutex`：锁里做配置对比并发起一次远程 read。A 持锁挂起，同线程 B 去 `lock()`，普通互斥锁同线程重复上锁是未定义行为，worker 线程卡死，`healthz` 探活超时，这条 worker 上几十个连接无法推进。日志末行永远停在同一处，`pstack`/`gdb` 里可以看到 `lock()` 和挂起点的栈。换协程锁（让出）后，等待者只是让出，不再阻塞线程。

---

## 常见误区与设计权衡

### 误区 1：函数是线程安全的，所以在协程框架里怎么挂起都安全

这是本课 pitfalls 原话的展开：**线程安全标注不能直接推导任意协程调度下可重入。** 线程安全保障的是"多次**完整调用**并发时的正确"；协程调度允许"一次调用**中途**遭遇再次进入"。一个内部用互斥锁的 MT-Safe 函数，同线程持锁挂起会按第 3 节卡死；一个用静态缓冲的函数，跨挂起被覆盖。要按"轴、窗口、版本"三类差异分别检查，别背"MT-Safe 一定没问题"。

### 误区 2：单线程调度，就没有死锁

普通互斥锁的死锁需要"互相等待的环"，但同线程上简化成"同线程一个协程持锁挂起、另一个去 lock"——不必多线程，一次 futex 等待就能把整条线程变成不可推进的休眠。反过来看，单线程也有一个优势：协程锁（让出）方案先天不存在"两把锁交叉等待"的环——它在逻辑上就是顺序化的，只要 lock/unlock 配对。

### 误区 3：返回静态缓冲的函数只在信号处理里危险

`inet_ntoa(daddr)` 这类调用在日志、鉴权、转发代码里到处都是。信号场景文档里都写清楚了（要 `AS-Safe`）；协作调度的等价场景没有任何 man 页警告教你，因为 man 面向线程/信号，协程是框架语义。审查 hook 命中的调用链时要把"返回静态缓冲指针"主动列入检查项，不留到出问题再回溯。

### 误区 4：预热把一切初始化路径都销掉了

第 5 节的实验证明了这点：预热消掉了**一次性初始化**（`one_time_init`、懒绑定、首次 `dlopen`）的窗口，但**每次调用都用的共享临时缓冲/静态缓冲**照旧被覆盖。把预热定位为针对特定首次路径的清单化补丁并逐项标注，而不是"修复协程安全"。

### 误区 5：scratch buffer 加把锁就安全

`std::mutex` 在协作调度里是天然的同线程死锁陷阱；加锁的目的若是"独占写窗口"，正确方向要么是把锁换成**协程锁**（等待者让出），要么重新审视"这个共享状态是否真的需要跨挂起存活"。多数情况下后者才是正解（见权衡）。

### 误区 6：没有全局变量就是安全的

可重入隐患不止业务代码里的 static。库函数内部的静态缓冲（`strerror` 一类）、TLS（同线程共享，第 72 课）、加载器内部状态（首次解析）、框架 hook 自身的实现细节，都是"你看不见的全局"。审查时以"这份状态跨不跨挂起点存活"为准，不以"我的代码没写 static"为准。

### 设计权衡

**权衡 1：移除全局临时状态（推荐） vs 加锁保护 vs 定点预热。** 本课 tradeoffs 明确给出方向：**移除全局临时状态通常更稳妥，预热只覆盖特定首次路径。** 三种方案的本质差异在"风险面"：

| 方案 | 消除的窗口 | 残留风险 | 维护成本 |
| --- | --- | --- | --- |
| 移除全局临时状态（局部拥有） | 全部 | 上调用栈确认"局部"不被别处间接共享 | 低（一次性改） |
| 协程锁 | 并发写入窗口 | 锁的持有跨长挂起时序问题仍在 | 中（需审查临界区） |
| 普通互斥锁 | 多线程竞态 | 同线程重入 → 卡死线程 | 高（默认视为错误） |
| 预热 | 仅一次性初始化路径 | 每调用路径完全未保护 | 低，但覆盖面窄 |

判断顺序建议：先试图移除（改返回缓冲为入参缓冲/返回 `std::string`）；做不到再考虑协程锁并逐个审查临界区；预热只用于明确发现的一次性初始化路径。

**权衡 2：给库"标注协程安全"可行吗？** 即你给某个函数标 `@coroutine-safe`——问题是协程安全不是函数内属性，它同时取决于**调度点**（该函数被谁调用、是否中途挂起）和**版本**。库侧能诚实给出的只有"执行期间我是否使用跨调用可变的共享状态"这一事实；把"哪些调用模式安全"留给调用方审查。

**权衡 3：显式可控 yield vs 全部隐藏。** 热更新/检查这类长路径**必须**挂起（检查期间要等 IO），但挂起点应当**显式、白名单化**：把允许挂起的位置写成命名好的函数（如 `async_read_config()`），并有单元测试枚举挂起点。可控 yield 的价值是审查时能拿清单；不可控隐藏 yield 的代价是永远算不准调用链深处在何时踩雷。

---

## 动手实践

### 任务：给热更新检查加入可控 yield，复现共享临时缓冲覆盖并改为局部拥有

本实践对应 `practice` 原文：

- 给热更新检查加入可控 yield；
- 复现共享临时缓冲覆盖；
- 改为局部拥有并验证不再覆盖。

验收对应：

- 能列出所有潜在挂起调用；
- 不会跨挂起点持有阻塞同线程的普通互斥锁。

#### 步骤 0：准备环境

把第 1 节的 `coop_sched.h` 保存到工作目录，确认编译器：

```bash
g++ --version
ls   # 确认 coop_sched.h 就在同目录
```

#### 步骤 1：编写实验程序

一个程序三段：阶段 1 用共享 scratch buffer 复现覆盖；阶段 2 改为调用方/协程拥有局部缓冲验证隔离；阶段 3 挂起点审计（把"列出所有潜在挂起调用 + 不跨挂起点持普通互斥锁"的验收落成可运行产物）。

```cpp
// hot_reload_lab.cpp —— 第 73 课动手实践
#include <cstdio>
#include <cstring>
#include <cstddef>
#include <mutex>
#include "coop_sched.h"

// ===== 共享临时状态：本实践要复现并消除的对象 =====
static char g_scratch[128];        // 命名空间级 scratch buffer：路径拼接共用

// ---------- 版本 1：跨挂起点使用共享 scratch buffer（缺陷版） ----------
static const char* build_config_path_scratch(const char* appid) {
    std::snprintf(g_scratch, sizeof(g_scratch), "config/%s/app.json", appid);
    return g_scratch;   // 返回共享缓冲的指针
}

static void reload_check_scratch(int self) {
    const char* appid = (self == 0) ? "app-wx-a" : "app-wx-b";

    const char* path = build_config_path_scratch(appid);
    std::printf("[%s] 开始热更新检查: path=%s\n", co_current_name(), path);

    co_yield_to_sched();   // ← 可控 yield：模拟检查中段"hook 的 stat/read / 远端校验"

    bool ok = std::strstr(path, appid) != nullptr;
    std::printf("[%s] 恢复后使用 path=%s（预期含 %s）: %s\n",
                co_current_name(), path, appid, ok ? "正常" : "已被覆盖!");
}

// ---------- 版本 2：调用方/协程拥有局部缓冲（修复版） ----------
static void build_config_path_local(const char* appid, char* buf, std::size_t n) {
    std::snprintf(buf, n, "config/%s/app.json", appid);
}

static void reload_check_local(int self) {
    const char* appid = (self == 0) ? "app-wx-a" : "app-wx-b";

    char path[128];        // 协程栈上：跟这次检查走，跨挂起点独占
    build_config_path_local(appid, path, sizeof(path));
    std::printf("[%s] 开始热更新检查: path=%s\n", co_current_name(), path);

    co_yield_to_sched();   // 同样的挂起窗口，不再有人能改写 path

    bool ok = std::strstr(path, appid) != nullptr;
    std::printf("[%s] 恢复后使用 path=%s（预期含 %s）: %s\n",
                co_current_name(), path, appid, ok ? "正常" : "已被覆盖!");
}

// ---------- 阶段 3：挂起点审计（验收） ----------
struct SuspendPoint {
    const char* where;   // 哪个调用可能挂起
    const char* via;     // 为什么会挂起
};

static const SuspendPoint g_suspend_table[] = {
    {"reload_check::co_yield_to_sched", "显式可控 yield"},
    {"远端配置模块 read_check()",       "内部走被 hook 的阻塞读 → 隐藏 yield 点"},
    {"配置文件 stat / open / read",     "若命中 hook 的 read 系则是隐藏 yield 点"},
    {"日志落盘 write",                  "若走被 hook 的 write 则是隐藏 yield 点"},
};

static const char* g_cross_suspend_state[] = {
    "g_scratch（缺陷版在用；修复版已不再跨挂起点引用）",
};

static void reload_audit(int self) {
    (void)self;
    std::printf("[%s] 潜在挂起调用清单：\n", co_current_name());
    for (const auto& sp : g_suspend_table) {
        std::printf("[%s]   - where=%s | via=%s\n", co_current_name(), sp.where, sp.via);
    }
    std::printf("[%s] 跨挂起点共享状态清单：\n", co_current_name());
    for (const char* s : g_cross_suspend_state) {
        std::printf("[%s]   - %s\n", co_current_name(), s);
    }
    std::printf("[%s] 结论：本检查路径未跨挂起点持有"
                "std::mutex/pthread_mutex_t 等阻塞同线程的锁（第 3 节规则）\n",
                co_current_name());
}

static void run_stage(const char* banner, void (*task)(int)) {
    std::printf("======== %s ========\n", banner);
    co_add("A", task);
    co_add("B", task);
    co_sched_loop();
    co_clear();
}

int main() {
    run_stage("阶段 1：共享 scratch buffer（复现覆盖）", reload_check_scratch);
    run_stage("阶段 2：调用方拥有局部缓冲（修复）",     reload_check_local);
    run_stage("阶段 3：挂起点审计（验收）",             reload_audit);
    return 0;
}
```

（`#include <mutex>` 放在这里的目的是提醒：如果哪一步你真往检查路径里放锁，必须重新过第 3 节的规则；本程序本身不持任何锁。）

#### 步骤 2：编译与运行

```bash
g++ -std=c++17 -pthread -O2 hot_reload_lab.cpp -o hot_reload_lab
stdbuf -o0 ./hot_reload_lab
```

预期输出：

```text
======== 阶段 1：共享 scratch buffer（复现覆盖） ========
[A] 开始热更新检查: path=config/app-wx-a/app.json
[B] 开始热更新检查: path=config/app-wx-b/app.json
[A] 恢复后使用 path=config/app-wx-b/app.json（预期含 app-wx-a）: 已被覆盖!
[B] 恢复后使用 path=config/app-wx-b/app.json（预期含 app-wx-b）: 正常
======== 阶段 2：调用方拥有局部缓冲（修复） ========
[A] 开始热更新检查: path=config/app-wx-a/app.json
[B] 开始热更新检查: path=config/app-wx-b/app.json
[A] 恢复后使用 path=config/app-wx-a/app.json（预期含 app-wx-a）: 正常
[B] 恢复后使用 path=config/app-wx-b/app.json（预期含 app-wx-b）: 正常
======== 阶段 3：挂起点审计（验收） ========
[A] 潜在挂起调用清单：
[A]   - where=reload_check::co_yield_to_sched | via=显式可控 yield
[A]   - where=远端配置模块 read_check() | via=内部走被 hook 的阻塞读 → 隐藏 yield 点
[A]   - where=配置文件 stat / open / read | via=若命中 hook 的 read 系则是隐藏 yield 点
[A]   - where=日志落盘 write | via=若走被 hook 的 write 则是隐藏 yield 点
[A] 跨挂起点共享状态清单：
[A]   - g_scratch（缺陷版在用；修复版已不再跨挂起点引用）
[A] 结论：本检查路径未跨挂起点持有 std::mutex/pthread_mutex_t 等阻塞同线程的锁（第 3 节规则）
```

（阶段 3 的审计任务不挂起，A 和 B 会先后打印同样的内容，读一份即可。）

读证据：阶段 1 中 A、B 的 `path` 指针指向**同一块** `g_scratch`（你可以在两行输出对上地址，下述输出的 `%s` 内容已说明被覆盖）；A 恢复后读到 B 的路径——共享临时缓冲覆盖复现。阶段 2 两个协程各自的局部缓冲地址不同，恢复后各自内容完好——局部拥有验证通过。

#### 步骤 3：潜在挂起调用扫描（验收目标 1）

用工具 + 代码双重核对"潜在挂起调用清单"：

```bash
# 显式挂起点
grep -n "co_yield_to_sched" hot_reload_lab.cpp

# 锁：确认没有跨挂起点持有的普通互斥锁
grep -n "mutex\|lock()\|unlock()" hot_reload_lab.cpp
```

第一条命令命中 `reload_check_scratch`/`reload_check_local` 里各一处 `co_yield_to_sched`（两版检查各一次可控 yield）；第二条命令命中的只有 `#include <mutex>` 和审计文本里的字样，**没有任何 `lock()`/`unlock()` 调用**——检查路径中未持有普通互斥锁，更谈不上跨挂起点持有。把这两条 `grep` 的结果合并进你阶段 3 打印的清单，就是一份完整的"潜在挂起调用 + 跨挂起状态"审查记录。

如果要看更接近生产版的调用链，第 1 节的 `nm -D`/`strace`/`ltrace` 组合同样适用：对真实的 `read/stat/write` 调用逐一判定"这个符号是否被框架 hook"。

#### 步骤 4：验收对照

| 验收项 | 本练习如何覆盖 |
| --- | --- |
| 能列出所有潜在挂起调用 | 阶段 3 输出清单 + `grep co_yield_to_sched` + 第 1 节工具对真实二进制的核对 |
| 不会跨挂起点持有阻塞同线程的普通互斥锁 | `grep mutex/lock` 无命中；审计结论输出；第 3 节规则口径逐点核对过 |
| （practice 主体）加可控 yield → 复现覆盖 → 改局部拥有 | 阶段 1 复现 `已被覆盖!`；阶段 2 同样挂起窗口下全部 `正常` |
| 隐藏 yield 点的认知 | 阶段 3 清单区分了"显式可控 yield"与"被 hook 的隐藏 yield 点"两类 |

---

## sourceStudy：源码审查任务

对应本节点 `sourceStudy`：**审查 libco hook 边界及实际第三方函数实现，列出可能挂起的位置和跨挂起保留的共享状态。**

不要求你重写 libco，而是像安全审计一样把"边界"摸清楚。注意：不同版本/分支的 libco hook 范围不同，以下步骤都要求**以你实际锁定的版本源码为准**，不要凭记忆写结论。

### 审查步骤

1. **确定 hook 清单**。在 libco 源码中找到 hook 的实现文件（hook 集中在 `co_hook_sys_call.c` 一类文件里，具体名称以你拉到的版本为准）：
   ```bash
   # 在库源码目录里找 hook 相关文件与符号替换列表
   grep -rn "hook" . --include="*.c" -l
   grep -rn "read\|write\|connect\|poll\|gethostbyname" co_hook_sys_call.c
   ```
   得到一张**被 hook 的符号清单**。通常包括 `read`、`write`、`connect`、`poll` 以及 DNS 相关调用，但要按你的版本确认，并注意 hook 是否需要显式开启（如 `co_enable_hook_sys` 一类开关）。
2. **对齐你的依赖**。对你的可执行文件/动态库跑：
   ```bash
   nm -D ./你的服务二进制 | grep " U " | grep -E "read|write|connect|poll|getaddrinfo|gethostbyname|dlopen|dlsym"
   ```
   目的：确定业务依赖（日志库、配置库、第三方 SDK、DB 客户端）实际会走到哪些**被 hook** 的符号。`strace`/`ltrace` 提供运行时证据。
3. **审查每个被命中的函数**：读它的实现（第三方库源码或反汇编），回答三个问题：
   - 它是否调用被 hook 的符号（→ 内部可能有隐藏 yield 点）；
   - man 页的 attributes 标注是什么（`MT-Unsafe race:`/`env`/`init`/`lock`），对照你锁定的 glibc/其它库版本；
   - 它返回的缓冲/写入的目标状态属于哪个层级（进程全局 static / TLS / 调用方缓冲）。
4. **不要只看返回值载体**。跨挂起保留的共享状态还包括：静态返回缓冲、全局 token/dns 缓存、内部 mutex、加载器首次解析路径、函数内 static 的一次性初始化标志。
5. **产出审查表格**并入库（code review 记录或评审文档），形如：

| 函数/调用点 | 是否可挂起（凭据） | 共享状态 | 层级 | 处置 |
| --- | --- | --- | --- | --- |
| `config_client::read_check()` | 调用被 hook 的 `poll/read` | 连接缓冲 | 协程内局部 | 保留 |
| `strerror(errno)` | 本身不挂起，风险在**拿着返回指针跨挂起点使用** | 内部静态缓冲 race:strerror | 进程全局 | 改 `strerror_r` + 局部缓冲 |
| `inet_ntoa(peer)` | 同上 | race:inet_ntoa | 进程全局 | 改 `inet_ntop` + 局部缓冲 |
| 首次 `token_lib::sign()` | 含首次 dlopen/dlsym/懒绑定 | 加载器解析状态 | 进程全局 | 启动预热 + 禁止解析区内挂起 |
| 热更新检查 `reload_check()` | 内含可控 yield | `g_scratch` 路径缓冲 | 进程全局 | 改局部拥有（本课实践） |

### 检查清单（做成评审用 checklist）

- [ ] 列出了本版本 libco 实际 hook 的符号清单（以源码为准，不凭记忆）
- [ ] 服务二进制里被 hook 的符号与业务依赖调用对得上证据（`nm -D` + `strace`）
- [ ] 对每个命中函数标注：挂起可能性 / man attributes / 共享状态层级 / 版本号
- [ ] 所有"返回静态缓冲指针"的调用点都已替换为调用方缓冲或 `_r`/`_ntop` 变体
- [ ] 每个挂起点之前，确认当前没有持有 `std::mutex`/`pthread_mutex_t`/自旋锁
- [ ] 等待场景使用协程锁（让出），并审查没有跨挂起的长临界区
- [ ] 一次性初始化路径（首次 dlopen/懒绑定/单例）已定向预热并在文档中注明"仅覆盖此路径"
- [ ] 每次调用路径的共享临时缓冲（error 串、path、token、时间字符串）已全部改为局部拥有

---

## 自测题

### 题 1

以下是公众号后台某函数的调用链：

```text
check_config():
    strcpy 到全局 static 缓冲 g_buf
    third_party_client.query(appid)     // 第三方 SDK
    使用 g_buf 做校验
```

`third_party_client.query()` 内部会调用被 libco 类框架 hook 的 `read`/`connect`。请回答：

1. 这个调用链里有哪些潜在挂起调用？各属于显式还是隐藏 yield 点？
2. 这段代码在什么条件下会发生重入？
3. 线程安全审查（例如查 `third_party_client` 函数的 man 页标注）能否发现这里的风险？
4. 怎样才能消除这里的重入风险？

**答案：**

1. `third_party_client.query()` 内部命中被 hook 的 `read`/`connect` 时挂起——这是隐藏 yield 点（源码里看不到 yield 关键字）；如果 `check_config` 本体还写了显式 `co_await`/`yield`，那些是显式可控 yield 点。审查要求两类都列出来，形成潜在挂起调用清单。
2. 当第一次执行在"已写 `g_buf`、还没消费"的窗口里挂起、而同线程另一个协程重走 `check_config` 或任何写 `g_buf` 的路径时——就是前次执行未结束而再次进入，即重入。
3. 只能部分。线程安全标注（如 `MT-Safe`）讲的是**多线程**并发，不覆盖"同线程挂起后再次进入"，这正是本课 pitfalls；而且一个函数内部调不调被 hook 的符号，man 标注不管，要结合 hook 清单核对。
4. 把 `g_buf` 改为调用方/协程提供的局部缓冲（或改成返回 `std::string` 的值语义）；或者严格界定 `g_buf` 的写入-消费窗口内永不挂起；并对照被 hook 的符号清单核实隐藏 yield 的真实位置，而不是凭感觉判断。

### 题 2

同一条线程上，协程 A、B 都调用 `compose_key(appid)`，它内部向 `static char g_buf[64]` 写 `"key:%s"` 并**返回指向 `g_buf` 的指针**。A 写、挂起；B 进入、写、挂起；A 恢复后用返回的指针打印。请回答：

1. 打印结果是什么？为什么？
2. 为什么 B"看起来正常"不能用来排除 bug？
3. 给出三种修复方式，并指出各自把状态归到哪个共享层级。
4. 如果 `compose_key` 改成把结果写入**调用方指定的缓冲**，调用方挂起期间的安全性由谁保障？换成 `thread_local` 有同样的保障吗？

**答案：**

1. A 打印出 `key:B的 appid`。A 恢复时读到的 `g_buf` 已被 B 覆盖——共享静态状态"最后一次写入者获胜"，牺牲的是中间受害者。
2. B 是"最后写者"，恢复时读到的就是自己的值，自然显示正常。周期性观察会发现"一部分请求串号、另一部分永远没事"的怪现象；以"协程 B 正常"为证据会漏掉 bug。判断应针对结构（跨挂起共享可变状态），而非样本表现。
3. ① 改为调用方缓冲：`compose_key(appid, buf, n)`——状态归**当次调用的栈**（协程栈），跨挂起独占；② 返回 `std::string` 等拥有型值——归**返回值**（拷贝语义），同样独立；③ 库函数场景换 `_r`/`_ntop` 变体——同①。也可以改用协程锁，但那只是把"写窗口"变成临界区而未移除共享，属次选（见权衡）。
4. 由调用方缓冲的**归属**保障：缓冲在协程栈/调用方栈帧上，不跨挂起与他人共享。`thread_local` 不行：同一条线程上的所有协程共享同一份 TLS 实例（第 72 课），挂起窗口照样被同线程另一协程覆盖——放进 thread_local 只排除了进程级多线程竞争，并没有解决"同线程重入覆盖"。

### 题 3

有人在热更新检查里加了 `std::mutex`：

```text
reload_check():
    g_mu.lock();
    对比新旧配置（其中要 yield 等一次远程读）
    g_mu.unlock();
```

请回答：

1. 同一条线程上会发生什么？整个调度器还能推进吗？
2. 这是"线程死锁"还是"协作调度下专用锁语义失效"？
3. 用 `std::recursive_mutex` 能解决吗？用 spinlock 呢？
4. 正确的做法是什么，并给出"在哪些情况下加锁才是合适选择"。

**答案：**

1. A 持锁后挂起，同线程 B 执行 `g_mu.lock()`——同一条线程对非递归普通互斥锁重复上锁是未定义行为，典型表现是 futex 等待；等待者会阻塞线程，协作调度器也停留在这条线程上，**整条线程卡死**（本课实验中 `timeout 3` 表现为 exit 124），该 worker 上全部协程停止推进。
2. 属于"协作调度下普通锁语义失效"：`std::mutex` 的语义按线程/抢占调度建模（阻塞等待），而协作调度需要"等待者让出"模式；在协作环境里引入阻塞原语就破坏了模型，而不只是两个协程对资源的普通争用。
3. 不能。`recursive_mutex` 只能消除"同线程重复上锁即未定义"的表现，但等待者依旧会**阻塞线程**，而且把"可变共享窗口"继续沿着挂起点拉长，问题未消除。spinlock：等待者会打转烧 CPU，同样占用线程，调度器切不回持锁者，等于忙等死锁。
4. 正确做法：①首选移除"跨挂起持锁"需求——把临界内的共享缓冲改为局部拥有，锁整个消失；②确实需要锁就换**协程锁**（等待者 yield），并审计临界区内不发生意外的挂起；③锁只在"确实有共享可变状态且无法归属化"时使用，同时按第 3 节规则逐个挂起点核对。加锁从不替代"归属重构"，它只是把并发窗口显式化。

---

## 推荐资源

- **可重入性与协作调度安全：Linux man-pages**
  - 链接：https://man7.org/linux/man-pages/man7/attributes.7.html
  - 来源：man7.org
  - 语言：英文
  - 类型：文档
  - 难度：深入
  - 是否免费：是
  - 推荐理由：重点阅读隐藏 yield 点与静态缓冲与初始化状态，用于核对本节点实验的语义和边界。`attributes(7)` 是 POSIX 体系对函数并发属性（`MT-Safe`/`MT-Unsafe`、`race:`、`env`、`init`、`AS-Safe`、`AC-Safe`）的权威词汇表；先读这份，再拿 `man 3 strerror` 等具体条目回到本课第 4 节的读取方法上训练。

---

## 本课小结

1. **可重入 = 前一次执行未结束时再次进入。** 信号处理和协作调度都能造成它；讨论线程数没有意义，看的是"哪里存在再次进入点"。
2. **隐藏 yield 点是重入的暗渠。** 被 hook 的阻塞调用（`read`/`write`/`connect`/`poll`/DNS）在调用链深处形成调度点；潜在挂起调用 = 显式可控 yield + 命中被 hook 符号的调用链。
3. **静态缓冲与初始化状态跨挂起共享。** `static char buf[]`、返回静态缓冲指针的库函数（`strerror`、`inet_ntoa`、`localtime`）、函数内 `static` 的一次性初始化标志，都会在挂起窗口里被同线程另一条协程改写；栈上局部状态不会。
4. **暴露面的形状都一样**："写入共享状态 → 挂起 → 恢复后读"，中间受害者被惩罚，最后写入者看似正常。审查要识别结构，不要凭样本行为下结论。
5. **`std::mutex` 与协作调度不兼容。** 同线程持锁挂起 + 另一协程 `lock()` 是未定义行为，实测整条线程卡死；spinlock 同样占用线程。规则：**不跨挂起点持有阻塞同线程的普通互斥锁**；等待换协程锁（让出）。
6. **协程锁让等待者让出而不是阻塞线程。** 它使"持锁跨挂起"安全化，但临界区内的时序仍需审计；能移除共享状态就别靠锁。
7. **attributes(7) 标注要按轴读，不能跨轴套用。** `MT-Safe/race:/env/init/lock/AS-Safe/AC-Safe` 面向线程、信号、取消这些并发模式；"同线程任意挂起点重入"不在任何轴上，所以线程安全标注不能直接推导任意协程调度下可重入（pitfalls）。结论必须按"函数 × 挂起点 × 共享状态 × 版本"逐条检查。
8. **动态加载的首次路径是敏感区。** PLT 懒绑定、首次 `dlopen`/`dlsym` 发生在业务之外；`LD_DEBUG=bindings` 能实测"绑定发生在第几次"；未验证前不在这些区域插挂起点。
9. **预热是定向补丁，不是安全修复。** 它能消一次性初始化路径（懒绑定、首次 `dlopen`/`dlsym`、单例）的窗口（`LD_BIND_NOW`、`RTLD_NOW`、首调一次）；每次调用都用的全局临时缓冲完全不受其覆盖——最终答案仍是条款 6 指向的结构改造（tradeoffs）。
10. **优先局部拥有，其次协程锁，最后预热。** 移除全局临时状态通常最稳妥；预热只覆盖特定首次路径。把本课的产出固化成"潜在挂起调用清单 + 跨挂起状态清单 + 处置方案"，这是审查 hook、全局缓冲、动态加载、热更新四类路径的工作台。
