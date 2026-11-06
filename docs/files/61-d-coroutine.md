# 第 61/208 课：有栈协程与用户态调度

- **阶段**：第 3 阶段 · 传输与Socket
- **前置课程**：d-reactor、b-lifetime
- **关键词**：Stackful Coroutines、有栈协程、独立栈、上下文切换、协作调度、yield、用户态调度、IO hook、libco、协程栈、TLS 共享、coctx_swap、poll
- **难度**：进阶
- **所属领域**：D · IO模型与事件（子类：调度与资源复用）
- **标签**：Stackful Coroutines、D、C++

---

## 导读

上一课连接池解决的是**下游连接复用**：握手贵、下游并发要有舱壁。再往前一课，线程池解决的是**执行资源复用**：别让 CPU/阻塞任务堵死 EventLoop。

本课补上第三块拼图——**调用形态复用**：能不能用「看起来像同步」的写法，去组织非阻塞 IO？

节点 summary 给出命题：

> 有栈协程保存执行栈与寄存器上下文，由用户态调度器决定暂停和恢复；阻塞调用是否被 hook 取决于实现。

拆开是三件事：

1. **独立栈和上下文切换**：每个协程有一份自己的栈；切换时保存/恢复寄存器与栈指针（`coctx_swap` / `setjmp` 类思路）。
2. **协作调度与 yield**：协程主动让出；调度器维护 run queue；对比抢占式线程。
3. **IO hook 和事件循环**：把 `read`/`write`/`connect`/`poll` 等换成「未就绪则 yield + 注册事件」；**没被 hook 的阻塞调用会卡住同线程上所有协程**。

公众号后台的典型痛点是：接入层要调下游 RPC、读 Redis、写会话——若用回调/状态机，业务被拆得支离破碎；若直接阻塞在工作线程，线程数又要跟着「在飞 IO」涨。有栈协程（libco 类系统）给出第三种路径：

```text
粉丝消息进接入层
  → 起一个协程跑「同步风格」业务：
       user = redis_get(openid);      // 看起来像阻塞
       profile = rpc_user(user);      // 看起来像阻塞
       tpl = assemble_template(...);
       send_downstream(tpl);
  → 实际：hook 把上述调用变成「未就绪则 yield」
  → 同一 OS 线程上可挂成千上万个等待中的协程
  → 线程不被堵死，写法仍接近同步
```

本课承接 d-reactor（事件循环）和 b-lifetime（对象生命周期 / 栈上对象存活期），重点建立边界意识：

- 切换便宜 ≠ 没有栈内存成本、没有调度成本；
- 同步写法 ≠ 可以随便调任何阻塞 API；
- 同线程协程共享 TLS（`thread_local`），这既是便利也是坑。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释独立栈和上下文切换**
   - 说清「每协程一份栈」与「保存/恢复寄存器 + 栈指针」的关系。
   - 能对照 `coctx_swap` / `setjmp`/`longjmp` 类思路，画出切换时发生了什么。
   - 能说明为什么有栈协程可以在普通深层调用链中间暂停（对比无栈协程必须在协程函数内 `co_await`）。

2. **能独立完成：阅读 libco 切换与 poll 路径，实现仅用于教学的协作调度示例**
   - 读通：`coctx_swap.S` → `co_resume` / `co_yield` → hook 后的 `poll` 等待路径。
   - 实现：一个教学用协作调度器（run queue + yield + 简易「假 IO 就绪」唤醒）。
   - 验收：能说明 TLS 仍由同线程协程共享；对「未 hook 阻塞调用」有明确验证。

3. **满足本课验收标准（acceptance）**
   - 能说明线程局部变量仍由同线程协程共享。
   - 未 hook 阻塞调用影响有明确验证。

---

## 为什么后端要懂

节点里的理由是：

> 理解 libco 类系统为何能够以同步写法组织非阻塞 IO。

这句话直接决定你能不能正确使用（以及 debug）微信后台一类大量依赖协程框架的代码。

### 1. 同步写法 ≠ 阻塞线程

新人看到：

```cpp
int n = read(fd, buf, sizeof(buf));
auto resp = redis_get(key);
```

会本能地以为「这会卡住线程」。在 libco 风格系统里，若这些符号被 hook，语义变成：

1. 把 fd 设成非阻塞；
2. 试一次真实系统调用；
3. 若 `EAGAIN` / 未连接完成 → 注册到 epoll/poll，当前协程 `yield`；
4. 事件就绪后由调度器 `resume`，继续从调用点往下走。

对业务作者，调用栈看起来连续；对调度器，这段「等待」期间 CPU 去跑别的协程。这就是「同步写法组织非阻塞 IO」的本质。

### 2. 不知道 hook 边界就会埋雷

未 hook 的 `gethostbyname`、某些 SSL 同步握手、本地大文件 `read`、持锁后的慢路径……只要其中一个真阻塞，**同线程上所有协程一起停**。表象是「偶发全站卡死几百毫秒」，日志里却看不到锁竞争。

### 3. 和线程池、连接池的分工

| 手段 | 解决什么 | 不解决什么 |
|------|----------|------------|
| 线程池 | 执行资源复用；保护 EventLoop | 写法仍可能是回调/投递；线程数有上限 |
| 连接池 | 下游连接复用；限制下游并发 | 不改变「等 IO 时线程是否空转」 |
| 有栈协程 | 同步写法 + 用户态调度大量等待中任务 | 不消灭 hook 边界、栈内存、重入问题 |

公众号接入层常见组合：**少量线程 × 每线程大量协程 × 下游连接池**。三者缺一，容量模型都会偏。

---

## 直觉 → 原理

### 0. 先建立直觉：给每个「逻辑任务」一本自己的草稿本

线程切换贵：内核要保存大量状态、可能换地址空间、要过调度器。协程把「逻辑任务」缩小成用户态对象：

- 每人一本草稿本（**独立栈**）；
- 写到一半可以夹书签放下（**保存寄存器 + SP**）；
- 调度员说「你继续」就把草稿本递回来（**restore + 跳转**）。

关键差别：

- **协作**：只有你主动 `yield`（或进入被 hook 的阻塞点）才会换人；没人能抢占你的时间片。
- **同线程**：多个协程共享同一个 OS 线程，因此共享同一个 `thread_local` 命名空间。

```text
OS 线程 T
├── 协程 A 栈 [.... 卡在 redis_get ...]
├── 协程 B 栈 [.... 正在拼模板消息 ...]
├── 协程 C 栈 [.... 等 epoll 可读 ...]
└── 调度器 run queue / 等待队列 / 事件循环
```

---

### 1. 独立栈和上下文切换

#### 1.1 每协程一份栈

创建有栈协程时，运行时通常：

1. `mmap` / `malloc` 一块连续内存当栈（常见 128KB～1MB 量级，可配置）；
2. 把栈顶设好，构造一个「假返回地址 / 入口函数」；
3. 第一次 `resume` 时跳到协程入口。

栈上放的就是普通 C/C++ 局部变量、返回地址、被调用者保存寄存器——所以你可以在 `foo() → bar() → baz()` 任意深度调用 `co_yield` / 被 hook 的 `read`，回来后局部变量还在。这是有栈相对无栈（C++20 协程帧）的最大工程优势：**旧代码、同步库、深层调用链更容易「协程化」**。

代价也很直接：

- N 个协程 ≈ N × 栈大小的虚拟/物理内存压力（写时复制与未触碰页可缓解，但不能当零）；
- 栈溢出不会像线程那样有很大默认栈，**默认栈过小会默默打坏堆**；
- 调试时 `gdb` 看到的「当前栈」是正在跑的那个协程的栈。

#### 1.2 上下文里有什么

一次切换至少要处理：

| 项目 | 作用 |
|------|------|
| 通用寄存器 | 计算中间值、参数、返回值 |
| 栈指针 SP | 指向该协程自己的栈 |
| 指令指针 / 返回地址 | 从哪里继续执行 |
|（可选）浮点/向量寄存器 | 视 ABI 与实现 |

libco 在汇编里做 `coctx_swap`：把「当前上下文」存到一块 `coctx_t`，再把「目标上下文」装进 CPU。逻辑上等价于：

```text
swap(from, to):
  save CPU regs → from
  save SP        → from
  load  SP        ← to
  load  CPU regs  ← to
  jump / ret 到 to 上次停下的地方
```

教学上也可以用 `setjmp`/`longjmp` 近似（**注意**：不能正确处理 C++ 析构、部分寄存器与信号掩码；仅帮助建立直觉）：

```cpp
#include <csetjmp>
#include <cstdio>

std::jmp_buf main_buf, co_buf;
alignas(16) char co_stack[64 * 1024];

void coroutine_entry() {
    std::printf("co: first run\n");
    if (setjmp(co_buf) == 0) {
        longjmp(main_buf, 1);  // yield
    }
    std::printf("co: resumed\n");
    longjmp(main_buf, 2);      // 结束
}

int main() {
    // 省略：把 co_stack 顶端设成「调用 coroutine_entry」的陷阱帧
    // 真实系统用汇编完成；这里只强调「保存点 + 换栈」的形状
    (void)co_stack;
}
```

生产代码请直接读 libco 的 `coctx_swap.S`，不要用 `longjmp` 代替。

#### 1.3 和线程切换比成本

| | OS 线程切换 | 有栈协程切换 |
|--|-------------|--------------|
| 谁做 | 内核调度器 | 用户态 `coctx_swap` |
| 涉及 | 内核态、可能 TLB/缓存更多扰动 | 通常只动用户寄存器 + SP |
| 触发 | 时间片、阻塞、唤醒 | 主动 yield / hook 点 |
| TLS | 每线程一份 | **同线程协程共享** |
| 抢占 | 可抢占 | 默认协作，不抢占 |

切换「便宜」指的是 CPU 路径短，**不是**「可以无限创建」。栈内存与缓存局部性仍是账单。

#### 1.4 短伪代码：教学用上下文

```cpp
struct Ctx {
    void* regs[8];   // 示意：真实 x86_64 要按 ABI 保存更多
    void* sp;
    void* ip;
};

extern "C" void coctx_swap(Ctx* from, Ctx* to);  // 汇编实现

struct Coroutine {
    Ctx ctx{};
    char* stack = nullptr;
    size_t stack_size = 0;
    enum { READY, RUNNING, WAITING, DEAD } state = READY;
};

void yield_to(Coroutine* self, Coroutine* other) {
    self->state = READY;          // 或 WAITING，取决于原因
    other->state = RUNNING;
    coctx_swap(&self->ctx, &other->ctx);
}
```

---

### 2. 协作调度与 yield

#### 2.1 主动让出

协作调度的契约：

> 正在运行的协程会一直跑，直到它调用 `yield`、结束、或进入「可挂起的等待」（通常由 hook 触发 yield）。

没有时钟中断帮你抢回来。于是：

- **好处**：临界区简单——同线程协程之间许多路径可不加锁（仍要注意可重入与共享堆结构）；
- **坏处**：一个协程里写了 `while (true) { heavy_cpu(); }` 且从不 yield，同线程其它协程全部饿死。

公众号业务里，CPU 很重的签名校验、大 JSON 解析，往往仍应丢到**工作线程池**，而不是占着协程线程空转；协程擅长的是「大量等待 IO」。

#### 2.2 调度器与 run queue

最小调度器长这样：

```text
while (true) {
  while (!run_queue.empty()) {
    co = run_queue.pop_front();
    resume(co);          // coctx_swap 到 co
    // resume 返回意味着：co 又 yield 了，或结束了
    if (co.dead) destroy(co);
  }
  wait_for_io_or_timer(); // epoll_wait / poll
  // 把就绪 fd 对应的协程推回 run_queue
}
```

libco 的路径可以简化记忆为：

1. `co_create`：分配栈与控制块；
2. `co_resume`：若是主协程/调度协程切到目标；
3. `co_yield`：切回调度侧；
4. hook 的 `poll`：把当前协程挂到「等事件」结构，然后 `yield`；事件循环发现就绪后再 `resume`。

#### 2.3 与抢占式线程对比（公众号场景）

```text
场景：1 万个「等 Redis」的在飞请求

线程模型：
  要么 1 万线程（栈 + 调度爆炸）
  要么 少量线程 + 异步回调（写法痛苦）

有栈协程：
  例如 8 个 OS 线程 × 每线程上千协程
  业务仍写成 redis_get()；hook 负责挂起
```

注意：**协程不增加 CPU 并行度**。同一线程上的协程是交错执行，不是并行。CPU 密集仍靠多线程/多进程。

#### 2.4 教学示例：纯协作调度（无真实 IO）

下面是一个可编译的最小示例（`ucontext` 在部分环境已过时，这里用「逻辑伪调度」+ 明确注释；练习包里会给可运行版本）：

```cpp
// 逻辑示意：run queue + 主动 yield
#include <deque>
#include <functional>
#include <iostream>
#include <string>

struct Sched;

struct Task {
    std::string name;
    std::function<void(Sched&, Task&)> body;
    int step = 0;
    bool done = false;
};

struct Sched {
    std::deque<Task*> rq;

    void spawn(Task* t) { rq.push_back(t); }

    void yield(Task* self) {
        rq.push_back(self);
        // 真实有栈协程此处 coctx_swap 回调度器；
        // 教学版用「步进状态机」模拟多次 resume。
    }

    void run() {
        while (!rq.empty()) {
            Task* t = rq.front();
            rq.pop_front();
            if (t->done) continue;
            t->body(*this, *t);
        }
    }
};

int main() {
    Sched S;
    Task a{"A", [](Sched& S, Task& self) {
        if (self.step == 0) {
            std::cout << "A: before yield\n";
            self.step = 1;
            S.yield(&self);
            return;
        }
        std::cout << "A: after resume\n";
        self.done = true;
    }};
    Task b{"B", [](Sched& S, Task& self) {
        (void)S;
        std::cout << "B: run once\n";
        self.done = true;
    }};
    S.spawn(&a);
    S.spawn(&b);
    S.run();
}
```

输出顺序会是 `A before` → `B` → `A after`：这就是「yield 把执行权交还调度器，再轮到别人」。有栈版本只是把 `step` 状态机换成真实栈与 `coctx_swap`。

---

### 3. IO hook 和事件循环

#### 3.1 hook 在干什么

「同步写法」能成立，靠的是把阻塞点改成挂起点。libco 的 `co_hook_sys_call.cpp` 一类实现对常见 libc 符号做拦截（具体列表以仓库版本为准），典型包括：

- `read` / `write` / `recv` / `send`
- `connect` / `accept`
- `poll` / `epoll_wait`（以及内部等待原语）
- 有时还有 `sleep` / `usleep` / `gethostbyname` 等

伪代码：

```cpp
ssize_t read(int fd, void* buf, size_t n) {
    if (!co_is_enable_sys_hook()) {
        return raw_read(fd, buf, n);   // 真阻塞
    }
    set_nonblock(fd);
    for (;;) {
        ssize_t r = raw_read(fd, buf, n);
        if (r >= 0) return r;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return r;

        // 未就绪：登记到事件循环，当前协程 yield
        wait_on_fd(fd, READABLE, timeout);
        // resume 后继续 for 循环再读
    }
}
```

业务侧仍然写 `read(...)`；链接期/加载期走到 hook 版本。

#### 3.2 和 Reactor 的关系

有栈协程并没有扔掉 Reactor，而是把 Reactor **藏在 hook 后面**：

```text
业务协程:  redis_get()
    → hook read/write
    → 未就绪: 注册 epoll + co_yield
调度器线程:
    → epoll_wait
    → 就绪: co_resume(等待该 fd 的协程)
```

所以前置课 d-reactor 仍然重要：超时、边缘触发、惊群、wakeup（eventfd）等坑，在协程框架里一样存在，只是调用栈更「同步」。

#### 3.3 未 hook 的阻塞调用：同线程全灭

这是本课验收点之一。验证思路：

```text
线程 T 上跑协程 A、B、C

A 调用未被 hook 的 sleep(5) 或阻塞 read(慢设备)
→ A 不 yield，OS 线程 T 进入内核睡眠
→ 调度器跑不了，B/C 即使「逻辑上就绪」也无法 resume
→ 表现为：整条 worker 卡 5 秒
```

对比：

```text
A 调用被 hook 的 sleep / poll
→ A yield，T 继续跑 B/C，并在事件循环里等超时
→ 只有 A 挂起，其它协程正常
```

可用教学手段验证：

1. 在同线程起两个协程；
2. 一个里 `::sleep(2)`（绕过 hook）或直接 `syscall(SYS_read, ...)`；
3. 另一个里只做打印/计时；
4. 观察第二个协程的打印是否也被推迟约 2 秒。

若使用 libco，注意确认 hook 是否启用（`co_enable_hook_sys` 一类开关，以源码为准）。

#### 3.4 TLS 共享：同线程协程的「隐形全局变量」

`thread_local` 绑定的是 **OS 线程**，不是协程：

```cpp
thread_local int tls_req_id = 0;

// 协程 1
tls_req_id = 1001;
co_yield();                 // 切走
// 协程 2
tls_req_id = 2002;          // 改的是同一块 TLS
co_yield();
// 协程 1 resume
assert(tls_req_id == 2002); // 往往成立——已被别人改掉！
```

因此：

- 不要把「当前请求 ID / 当前连接指针」只放在 `thread_local` 里就指望协程安全；
- 应放在协程局部栈、协程控制块、或显式 `CoLocal` 一类机制（若框架提供）；
- 日志库、RPC stub 若用 TLS 缓存「当前上下文」，在协程环境必须确认是否做了协程级隔离。

这是 acceptance 的另一句：**能说明线程局部变量仍由同线程协程共享**。

#### 3.5 公众号后台例子串起来

```text
协程 handle_mp_msg(Msg m):
  co_enable_hook_sys();
  auto session = redis_get("sess:" + m.openid);   // hook → 可能 yield
  auto user    = rpc_call(user_svc, m.openid);    // hook → 可能 yield
  auto media   = conn_pool.borrow(...);           // 上一课：连接池
  auto body    = build_template(user, media);
  rpc_call(send_svc, body);                       // 再 yield
  // 全程同步观感；同线程其它粉丝消息的协程交错推进
```

若 `rpc_call` 内部某段走到未 hook 的阻塞 DNS，整个 worker 抖动——这就是 hook 边界要进 code review 清单的原因。

---

## 常见误区

### 1. 「切换便宜 ⇒ 可以无限造协程」（节点 pitfalls）

节点 pitfalls：

> 协程切换低成本不意味着没有栈内存或调度成本。

切换路径短，但：

- 每协程有独立栈，触碰后占物理页；
- run queue、等待队列、fd→协程映射有 CPU 与缓存成本；
- 过多协程会让尾延迟变差（调度扫描、缓存打散）。

实践上要有**每线程协程数上限**和栈大小策略，并监控：活跃协程数、等待协程数、栈高水位。

### 2. 把协程当线程用：乱加锁 / 乱用 TLS

同线程协程之间「看起来并行、实际交错」。你在协程 A 持有一把非可重入互斥锁时 `yield`，协程 B 再去抢同一把锁 → 死锁（因为解锁的人被挂起了，而同线程又跑不回去）。

TLS 误用见上一节。

### 3. 以为所有阻塞都被 hook 了

第三方 SDK、Java 风格「同步客户端」的 C 包装、自写 `while(read)` 对阻塞 fd、`fstream` 大文件、某些 SSL 配置……都可能漏网。**默认假设：没审计过的调用都可能真阻塞。**

### 4. 栈太小 / 栈太大

- 太小：深层 JSON、递归、大缓冲局部数组 → 静默破坏；
- 太大：协程一多 RSS 起飞。

需要按业务画像设默认栈，并对可疑路径做栈高水位采样（许多框架提供）。

### 5. 在持有跨协程共享结构锁时调用可能 yield 的 API

等价于在中断点持锁。规则：**持锁区间禁止 hook 点 / 禁止 yield**；或改用协程感知的锁，并想清楚唤醒关系。

### 6. 忽略生命周期（呼应 b-lifetime）

协程栈上的对象，在 yield 期间仍然「活着」。若把栈上指针注册到 epoll 回调、或丢到别的线程，resume 前对象已析构 → UAF。有栈协程不免除生命周期纪律，只是把帧从「编译器协程帧」换成了「用户提供的栈」。

---

## 设计权衡

节点 tradeoffs：

> 有栈协程兼容同步调用形态，但重入、栈大小和 hook 边界需谨慎。

### 兼容同步形态（收益）

- 旧同步业务、同步客户端库更容易迁入；
- 调用栈可读，排障时「从 yield 点往上看」更自然；
- 减少手写状态机。

### 重入与锁（成本）

- 协作调度 ≠ 无并发问题；同线程重入、跨线程共享仍在；
- 条件变量/互斥锁语义要按「可能 yield」重新审查。

### 栈大小（成本）

- 全局默认很难两全；可考虑大栈协程只给少数重路径，轻路径用小栈。

### hook 边界（成本）

- 收益完全建立在 hook 覆盖率与正确性上；
- 升级 libc、换 SSL、新系统调用，都可能引入回归。

### 和其他模型怎么选

| 模型 | 更合适 |
|------|--------|
| 纯 Reactor + 回调 | 极致可控的 IO 路径、少业务逻辑 |
| 线程池 | CPU 密集、或必须调用无法 hook 的阻塞库 |
| 有栈协程（libco 类） | 大量 IO 等待 + 希望同步写法 |
| 无栈协程（C++20） | 新代码、需要语言级状态机、接受改造调用链 |

公众号后台常见答案不是单选，而是：**接入与下游 IO 用协程；重 CPU 用线程池；下游连接用连接池。**

---

## 动手实践

节点 practice：

> 阅读 libco 切换与 poll 路径，实现仅用于教学的协作调度示例。

sourceStudy：

> 从 Tencent/libco 的 `coctx_swap.S`、`co_routine.cpp` 和 `co_hook_sys_call.cpp` 串联切换与等待。

### 步骤 1：克隆并定位三个文件

```bash
git clone --depth 1 https://github.com/Tencent/libco.git /tmp/libco
cd /tmp/libco
ls coctx_swap.S co_routine.cpp co_hook_sys_call.cpp
```

阅读顺序建议：

1. `coctx_swap.S`：寄存器与栈如何交换；
2. `co_routine.cpp`：`co_create` / `co_resume` / `co_yield`、与 poll 相关的等待结构；
3. `co_hook_sys_call.cpp`：哪些符号被替换，未就绪时如何挂起。

记录你仓库的 commit，避免「文档说一套、版本另一套」。

### 步骤 2：画一张串联图

要求自己能画出：

```text
业务 read()
  → hook_read
  → EAGAIN
  → 把当前协程挂到 fd 等待表
  → co_yield（coctx_swap 回调度）
  → 事件循环 poll/epoll 就绪
  → co_resume（再 swap 回去）
  → hook_read 重试 raw_read
```

### 步骤 3：实现教学用协作调度示例

目标不在「重新实现 libco」，而在验证两个 acceptance：

1. **TLS 共享**：同线程两协程读写同一 `thread_local`，证明会互相可见；
2. **未 hook 阻塞影响**：一协程 `sleep`/阻塞，另一协程推进被推迟。

配套练习可放在 `/workspace/atlas/practice61/`（见文末）。最小自检：

```bash
cd /workspace/atlas/practice61
./run.sh
```

### 步骤 4：验收清单

- [ ] 能口述独立栈 + `coctx_swap` 保存了什么；
- [ ] 能说明协作调度与抢占式线程的差异；
- [ ] 能说明 hook 如何把阻塞点变成挂起点；
- [ ] **能说明 TLS 仍由同线程协程共享**（有实验或推理链）；
- [ ] **未 hook 阻塞调用影响有明确验证**（有实验现象）；
- [ ] sourceStudy 笔记覆盖上述三个源文件的关键函数名。

---

## 自测题

**题 1.** 有栈协程在 `foo→bar→baz` 的 `baz` 里 yield，为什么回来后 `foo`/`bar` 的局部变量还在？这和 C++20 无栈协程的「暂停点限制」差在哪里？

**题 2.** 同 OS 线程上协程 A、B 都使用 `thread_local UserContext* ctx`。A 设置后 yield，B 修改后再 yield 回 A。A 读到的是什么？若要做「每请求上下文」，更合理的存放位置是？

**题 3.** 某 worker 线程上数千协程偶发卡住 3 秒，线程并未退出，CPU 也不高。如何用本课知识做最短排查？如何用实验证明「未 hook 阻塞」假说？

### 自测题参考答案

**题 1.** 有栈协程为每个协程分配独立栈，yield 时保存的是该栈的 SP 与寄存器；调用链上的栈帧都还在这份栈里，故局部变量仍在。无栈协程通常只保存编译器生成的协程帧，暂停点必须落在协程函数内的 `co_await` 等位置，普通中间函数不能「随便暂停」除非整条链都协程化。

**题 2.** A 读到的通常是 B 写入的值（同一线程同一块 TLS）。每请求上下文应放在协程栈上的对象、协程控制块字段，或框架提供的协程局部存储；不要只靠裸 `thread_local`。

**题 3.** 先看该线程是否卡在某个系统调用（`pstack`/`gdb`/`perf`）；若全线程协程停止推进且栈在未审计的阻塞调用，高度怀疑 hook 漏网。验证：同线程两协程，一个绕过 hook 阻塞，观察另一个是否一并停顿；对比换成 hook 版 `sleep`/`poll` 后是否恢复交错执行。

---

## 推荐资源

1. [有栈协程与用户态调度：参考文档 / 原始资料](https://github.com/Tencent/libco)  
   - 来源：github.com｜类型：源码｜难度：进阶｜免费  
   - 重点阅读**独立栈和上下文切换**与**协作调度与 yield**，用于核对本节点实验的语义和边界。

2. [libco · 协程上下文与调度](https://github.com/Tencent/libco/blob/master/co_routine.cpp)  
   - 来源：Tencent/libco｜类型：源码｜难度：进阶｜免费  
   - 从 `co_resume`、`co_yield` 与 poll 关联路径理解有栈协程；以仓库版本为准。

建议同时打开同仓库的 `coctx_swap.S` 与 `co_hook_sys_call.cpp`，三件套对照阅读。

---

## 本课小结

- **有栈协程**用独立栈 + 用户态上下文切换，让你可以在深层同步调用链中暂停/恢复。
- **协作调度**靠主动 yield；调度器维护 run queue，并与事件循环衔接。
- **IO hook** 是「同步写法组织非阻塞 IO」的秘密：未就绪则挂起，而不是堵死线程。
- **坑**：切换便宜仍有栈与调度成本；TLS 同线程共享；未 hook 阻塞会拖垮同线程所有协程；重入与栈大小要显式设计。
- 与前两课分工：线程池管执行资源，连接池管下游连接，协程管等待中的并发形态。

---

## 配套练习建议

1. **读源码三联**：`coctx_swap.S` → `co_routine.cpp` → `co_hook_sys_call.cpp`，写一页笔记只回答：一次 `read` 未就绪时，谁调用了 swap？谁在就绪后 resume？
2. **跑 practice61**：验证 TLS 共享与未 hook 阻塞影响（见 `/workspace/atlas/practice61`）。
3. **对比无栈**：回顾第 31 课 `b-coroutine`，列一张「有栈 vs 无栈」表：暂停点、栈内存、旧代码兼容、工具链成熟度。
4. **线上意识**：给自己的服务列「可能漏 hook 的 API」黑名单，并想清楚发现卡顿时的第一组命令（`gdb -p`、`cat /proc/pid/stack`、框架协程指标）。

下一课将继续在「调度与资源复用」脉络上推进；先确保你能独立讲清：为什么 libco 类系统能「看起来同步、实际不堵线程」，以及它在什么条件下会失效。
