# 第 72/208 课：TLS与协程共享边界

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 并发与内存模型（内存可见性与状态归属） |
| 前置 | d-coroutine、e-race |
| 难度 | 进阶 |
| 领域 | E |
| 标签 | Thread Local Storage、E、C++ |
| 子分类 | 内存可见性与状态归属 |
| 节点 ID | e-tls |
| 英文名 | Thread Local Storage |

> 上一课「缓存一致性与伪共享」把 `thread_local` 聚合当作减少伪共享的性能手段；本课往前跨一步，讲 `thread_local` 的生命周期归属，以及它和协程、请求上下文之间的共享边界。第 61 课（d-coroutine）已经点过「同线程协程共享 TLS」这颗雷，本课把它讲透。

---

## 导读

上一课我们讨论伪共享时，`thread_local` 是以“性能工具”的身份出现的：每个线程维护自己的计数器，减少跨线程写同一条缓存行，最后再汇总。在那个语境里，我们关心的是“写得多不多、缓存行争不争”。

这一课要换一个视角。`thread_local` 不只是一个性能优化，它首先是一条**生命周期规则**：

> `thread_local` 变量为每个操作系统线程提供一份独立实例。实例跟着线程走，不跟着请求走，也不跟着协程走。

这条规则本身很简单，但它和协程放在一起就会产生一个非常隐蔽的边界问题。协程可以在同一条 OS 线程上交替执行：任务 A 写 TLS，挂起；任务 B 写 TLS，挂起；任务 A 恢复，读 TLS，读到的却是任务 B 写进去的内容。更糟的是，工作窃取调度器还可能把任务从一条 worker 线程迁移到另一条 worker 线程：任务 A 在线程 1 写的 TLS，恢复时跑在线程 2，看到的是线程 2 的另一份实例，可能是空的，也可能装着别人的请求上下文。

放到微信公众号后台的真实场景里，这类问题非常具体：

- 把当前请求的 `appid`、`openid`、`msgid`、`request_id`、`trace_id` 塞进 `thread_local`，协程在等微信 API、MySQL、Redis 时 yield。同线程另一条粉丝消息覆盖了上下文，恢复后日志、鉴权、写库都串到别人的账号。
- TLS 里复用 JSON / protobuf 序列化缓冲。协程 A 刚把回包写进缓冲，yield；协程 B 用同一个 TLS 缓冲构造自己的回包；协程 A 恢复后把缓冲发出去，回包内容错乱。
- 日志库、RPC stub、MySQL 会话用 TLS 缓存“当前上下文”，在协程环境里没有做协程级隔离。于是日志里的 `trace_id` 对不上，RPC 透传的调用链断了，事务写到了错误的会话上。
- 工作窃取调度把协程迁移到另一条 worker 线程，TLS 变成另一份实例。原来线程上的上下文没有跟过来，恢复后的代码读到空值或别人的值。

本课要建立三个核心认识：

1. **线程存储期**：`thread_local` 的实例数量由线程数决定，不由请求数或协程数决定。
2. **同线程共享 + 跨线程迁移丢失**：这是同一枚硬币的两面，都来自“按线程划分”这一条规则。
3. **让请求拥有状态**：把 `RequestContext` 放进协程帧、显式传参，或使用真正保证保存/恢复的 coroutine-local storage，而不是把请求身份寄托在 TLS 上。

先记住本课的结论：**线程安全不等于协程安全；没有数据竞争，也不等于没有逻辑串扰。**

---

## 学习目标

### goals

- 能解释线程存储期。
- 能独立完成：模拟两个协程交替写 TLS 缓冲，改成请求拥有状态并验证隔离。

### acceptance

- 能区分线程安全与协程安全。
- 恢复执行后上下文不会串请求。
- 能解释无数据竞争也可能存在逻辑串扰；TSan 不一定报告同线程协程问题。
- 明确区分线程局部、协程局部、请求局部三种生命周期，不把线程安全等同协程安全。

---

## 为什么后端要懂

`thread_local` 在单线程或“一个请求占一条线程”的模型里非常好用：函数签名不用层层传上下文，日志、鉴权、数据库会话都能从 TLS 里拿到“当前请求”。但后端服务一旦引入协程、异步 IO 或工作窃取调度，这个假设就悄悄失效了。

1. **请求身份可能串号**：`openid`、`msgid`、`request_id` 这类字段一旦放错生命周期，日志会把 A 粉丝的行为记到 B 粉丝头上，鉴权可能放行错误用户，写库可能更新错账号。它不会崩溃，也不会报错，只会产生“看起来正常但业务数据错了”的事故。
2. **可复用缓冲会被覆盖**：为了减少分配，很多库会在 TLS 里放序列化缓冲、临时字符串、错误信息缓冲。单线程同步调用时没问题；一旦在“写缓冲”和“使用缓冲”之间出现 `co_await`，同线程的另一条协程就可能把缓冲改掉。
3. **第三方库的隐式 TLS 依赖**：日志库、RPC stub、MySQL 客户端、链路追踪 SDK 可能自己用 TLS 缓存“当前上下文”。你无法通过阅读自己的业务代码发现它；问题往往在压测、切换调度器或迁移线程时才暴露。
4. **任务迁移让 TLS 失效**：协程不是线程。C++20 协程标准没有规定协程必须绑定在某条线程上，调度器完全可以把一个挂起的协程交给另一条 worker 恢复。TLS 不会自动跟着走。
5. **线程池复用会留下脏上下文**：线程处理完一个请求后，TLS 里的值不会自动清空。下一个请求复用同一条线程时，可能读到上一个请求的残留。如果没有显式的设置/清理，TLS 就变成了一个隐式的全局变量。
6. **排查工具容易给出假阴性**：这类 bug 往往没有数据竞争，ThreadSanitizer 可能完全不报告。你会看到“sanitizer 干净、日志却串了”的现象，最后只能靠生命周期分析定位。

本节点给出的 why 很直接：**防止请求上下文和可复用缓冲被协程切换覆盖。** 一句话：**懂 TLS 的归属边界，你才知道哪些状态可以放 TLS，哪些必须跟着请求或协程走；不懂它，你会把“线程安全”误当成“协程安全”。**

---

## 直觉 → 原理

### 1. 线程存储期：一份实例，按线程划分

#### 先把三种存储期放在一起看

C++ 里变量的生命周期和实例数量，首先由存储期（storage duration）决定。和本课最相关的是三种：

| 存储期 | 实例数量 | 典型声明 | 谁共享 | 销毁时机 |
| --- | --- | --- | --- | --- |
| 自动存储期 | 每次进入作用域一份 | 函数内局部变量 | 当前调用栈帧 | 离开作用域 |
| 静态存储期 | 通常整个程序一份（内部链接对象按翻译单元划分） | 全局变量、`static` 变量 | 所有线程、所有请求 | 程序结束 |
| 线程存储期 | 每个 OS 线程一份 | `thread_local` 变量 | 同一线程上的所有协程/调用 | 线程退出 |

这张表的关键不是背名字，而是看“实例数量”这一列：

- 全局变量是**进程级**的，所有线程看到同一份；内部链接的 `static` 对象按翻译单元划分，但仍是静态存储期；
- 普通局部变量是**栈帧级**的，每次函数调用一份；
- `thread_local` 是**线程级**的，每条 OS 线程一份。

所以 `thread_local` 的“独立”是按线程划分的，不是按请求划分，也不是按协程划分。这一点后面会反复用到。

#### `thread_local` 到底做了什么

写一个最小实验：两条线程分别读写同一个 `thread_local` 计数器，观察它们互不影响。

```cpp
// tls_per_thread.cpp
#include <cstdio>
#include <thread>

thread_local int t_counter = 0;

void worker(const char* name, int base, int iters) {
    t_counter = base;
    for (int i = 0; i < iters; ++i) {
        ++t_counter;
    }
    std::printf("%s: t_counter=%d, &t_counter=%p\n",
                name, t_counter, static_cast<void*>(&t_counter));
}

int main() {
    std::thread a(worker, "A", 100, 5);
    std::thread b(worker, "B", 200, 7);
    a.join();
    b.join();

    std::printf("main: t_counter=%d, &t_counter=%p\n",
                t_counter, static_cast<void*>(&t_counter));
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 tls_per_thread.cpp -o tls_per_thread
./tls_per_thread
```

预期输出类似：

```text
A: t_counter=105, &t_counter=0x...
B: t_counter=207, &t_counter=0x...
main: t_counter=0, &t_counter=0x...
```

A 和 B 各自从自己的基数开始累加，互不覆盖；主线程的 `t_counter` 仍然是 0。这就是“每线程一份独立实例”。

有两点边界要说明：

- **地址不同只对同时存活的线程有意义。** 两条线程同时存活时，它们的 TLS 实例必须是不同对象，所以地址不同。但线程退出后，地址空间可能被后续线程复用，不要把 TLS 地址当成永久身份。
- **`thread_local` 不保证不同线程的 TLS 块在内存里按缓存行隔离。** 上一课用 TLS 做统计聚合，是为了减少共享写；但 TLS 块本身的布局由实现决定，不要假设它天然没有伪共享。

#### 初始化与销毁：实例和线程绑定

`thread_local` 变量的初始化、销毁也和线程绑定。下面这个程序用块作用域的 `thread_local` 观察构造与析构：

```cpp
// tls_lifetime.cpp
#include <iostream>
#include <string>
#include <thread>
#include <utility>

struct Tracker {
    std::string name;

    explicit Tracker(std::string n) : name(std::move(n)) {
        std::cout << "ctor " << name << " on " << std::this_thread::get_id() << "\n";
    }

    ~Tracker() {
        std::cout << "dtor " << name << " on " << std::this_thread::get_id() << "\n";
    }

    void touch() const {}
};

void worker(const char* name) {
    thread_local Tracker t(name);
    t.touch();
}

int main() {
    std::thread a(worker, "A");
    std::thread b(worker, "B");
    a.join();
    b.join();

    worker("main");
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 tls_lifetime.cpp -o tls_lifetime
./tls_lifetime
```

你会看到每条线程各自构造、各自析构一份 `Tracker`。关键规则：

- **块作用域的 `thread_local` 在控制流第一次经过声明时初始化**，之后同一线程再次经过不会重新初始化。
- **非平凡的 `thread_local` 对象在线程退出时销毁**；主线程的实例在程序结束时销毁。
- **命名空间作用域的 `thread_local` 动态初始化**，会在该线程第一次 odr-use 它之前完成；具体时机由实现和优化影响，不要依赖“线程一启动就构造”。
- **销毁顺序不要拿来做业务依赖。** 如果 TLS 对象之间互相引用，或者析构时还要用另一个 TLS 对象，很容易踩到“对象已经销毁”的边界。

另外，`thread_local` 是 C++11 引入的存储期说明符，只能用于变量：命名空间作用域的变量、块作用域的变量、类的静态数据成员。它可以和 `static`、`extern` 组合来调整链接性；不能用在函数声明或函数参数上。

#### 关键边界：按线程划分，不按请求或协程

现在把最重要的一句话写下来：

> `thread_local` 的实例数量等于 OS 线程数量，不等于请求数量，也不等于协程数量。

这意味着：

- 同一条线程上跑 100 个协程，它们看到的是**同一份** TLS 实例；
- 同一条线程上处理 100 个请求，它们也看到**同一份** TLS 实例；
- 换一条线程，看到的是**另一份** TLS 实例，哪怕还是同一个请求、同一个协程。

后面的串扰和迁移问题，都是这一条规则的直接推论。

---

### 2. 线程迁移和请求上下文：同线程共享，跨线程丢失

#### 用可挂起任务复现串扰

先不引入完整协程框架，用 C++20 的最小协程骨架做一个实验：同一条 OS 线程上创建两个任务，每个任务模拟处理一条公众号消息。任务先把请求上下文写进 TLS，然后挂起（模拟等微信 API），恢复后再读 TLS。

```cpp
// tls_coroutine_bug.cpp
#include <coroutine>
#include <cstdio>
#include <exception>
#include <string>
#include <utility>

struct RequestContext {
    std::string request_id;
    std::string openid;
};

thread_local RequestContext t_ctx;

struct Task {
    struct promise_type {
        Task get_return_object() {
            return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() { std::terminate(); }
    };

    std::coroutine_handle<promise_type> handle{};

    Task() = default;
    explicit Task(std::coroutine_handle<promise_type> h) : handle(h) {}
    Task(Task&& other) noexcept : handle(other.handle) { other.handle = {}; }
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            if (handle) handle.destroy();
            handle = other.handle;
            other.handle = {};
        }
        return *this;
    }
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    ~Task() { if (handle) handle.destroy(); }

    void resume() const { handle.resume(); }
    bool done() const { return handle.done(); }
};

Task handle_message(std::string id) {
    t_ctx.request_id = id;
    t_ctx.openid = "openid-" + id;

    std::printf("[%s] step1 写入 TLS: request_id=%s openid=%s\n",
                id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());

    co_await std::suspend_always{};  // yield：等微信 API

    std::printf("[%s] step2 恢复后读 TLS: request_id=%s openid=%s\n",
                id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());

    co_await std::suspend_always{};  // yield：等 MySQL

    std::printf("[%s] step3 写库前读 TLS: request_id=%s openid=%s\n",
                id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());
}

int main() {
    Task a = handle_message("req-A");
    Task b = handle_message("req-B");

    // 同一线程上交替恢复：A -> B -> A -> B -> A -> B
    a.resume();  // A 写 TLS
    b.resume();  // B 覆盖 TLS
    a.resume();  // A 读到的其实是 B 的上下文
    b.resume();
    a.resume();
    b.resume();

    std::printf("main sees TLS request_id=%s\n", t_ctx.request_id.c_str());
    return 0;
}
```

```bash
g++ -std=c++20 -pthread -O2 tls_coroutine_bug.cpp -o tls_coroutine_bug
./tls_coroutine_bug
```

预期输出（同线程、顺序确定）：

```text
[req-A] step1 写入 TLS: request_id=req-A openid=openid-req-A
[req-B] step1 写入 TLS: request_id=req-B openid=openid-req-B
[req-A] step2 恢复后读 TLS: request_id=req-B openid=openid-req-B
[req-B] step2 恢复后读 TLS: request_id=req-B openid=openid-req-B
[req-A] step3 写库前读 TLS: request_id=req-B openid=openid-req-B
[req-B] step3 写库前读 TLS: request_id=req-B openid=openid-req-B
main sees TLS request_id=req-B
```

把时间线摊开：

```text
t1: A 写 TLS = A
t2: A yield
t3: B 写 TLS = B          // 同线程，同一份 TLS，A 的内容被覆盖
t4: B yield
t5: A resume，读 TLS      // 读到 B
t6: B resume，读 TLS      // 读到 B
t7: A 写库，用的是 B 的 openid
```

注意这里**没有数据竞争**：

- 所有访问都在同一条 OS 线程上；
- 线程内的操作有 `sequenced-before` 关系；
- 没有两个线程同时访问同一个内存位置。

它是**逻辑串扰**：A 的请求状态被 B 覆盖了。程序的行为完全符合 C++ 语言规则，但业务语义错了。这正是 TLS 最危险的地方：它不会以“崩溃”或“sanitizer 报错”的形式提醒你。

#### 没有 C++20 也能复现：协作式状态机

如果你手边的编译器不支持 C++20 协程，用协作式状态机也能得到同样的结论。状态机就是协程调度的最小模型：每个任务保存自己的执行位置，调度器一次只恢复一个任务，任务在 `yield` 点返回。

```cpp
// tls_state_machine_bug.cpp
#include <cstdio>
#include <string>

struct RequestContext {
    std::string request_id;
    std::string openid;
};

thread_local RequestContext t_ctx;

struct Task {
    std::string id;
    int state = 0;

    bool finished() const { return state >= 3; }

    void resume() {
        switch (state) {
        case 0:
            t_ctx.request_id = id;
            t_ctx.openid = "openid-" + id;
            std::printf("[%s] step1 写入 TLS: request_id=%s openid=%s\n",
                        id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());
            state = 1;
            break;
        case 1:
            std::printf("[%s] step2 恢复后读 TLS: request_id=%s openid=%s\n",
                        id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());
            state = 2;
            break;
        case 2:
            std::printf("[%s] step3 写库前读 TLS: request_id=%s openid=%s\n",
                        id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());
            state = 3;
            break;
        }
    }
};

int main() {
    Task a{"req-A"};
    Task b{"req-B"};

    while (!a.finished() || !b.finished()) {
        if (!a.finished()) a.resume();
        if (!b.finished()) b.resume();
    }

    std::printf("main sees TLS request_id=%s\n", t_ctx.request_id.c_str());
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 tls_state_machine_bug.cpp -o tls_state_machine_bug
./tls_state_machine_bug
```

输出和 C++20 版本一致：A 在 step2、step3 读到 `req-B`。这个版本说明，问题不在“协程语法”，而在“任务切换时 TLS 不跟着切换”。无论你用 C++20 协程、状态机、回调还是事件循环，只要多个任务共享同一条线程的 TLS，就会遇到同一条边界。

#### 把任务迁移到另一条线程

现在看硬币的另一面。下面这个程序让同一个协程先在线程 A（主线程）写 TLS，然后挂起；再由线程 B 恢复并读取 TLS。

```cpp
// tls_thread_migration.cpp
#include <coroutine>
#include <cstdio>
#include <exception>
#include <string>
#include <thread>
#include <utility>

thread_local std::string t_request_id = "(unset)";

struct Task {
    struct promise_type {
        Task get_return_object() {
            return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() { std::terminate(); }
    };

    std::coroutine_handle<promise_type> handle{};

    explicit Task(std::coroutine_handle<promise_type> h) : handle(h) {}
    Task(Task&& other) noexcept : handle(other.handle) { other.handle = {}; }
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            if (handle) handle.destroy();
            handle = other.handle;
            other.handle = {};
        }
        return *this;
    }
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    ~Task() { if (handle) handle.destroy(); }

    void resume() const { handle.resume(); }
};

Task handle_request(std::string id) {
    t_request_id = id;
    std::printf("[线程 A] 写入 TLS: %s &t_request_id=%p\n",
                t_request_id.c_str(), static_cast<void*>(&t_request_id));

    co_await std::suspend_always{};  // 挂起，调度器可以把任务交给别的线程

    std::printf("[线程 B] 恢复后读 TLS: %s &t_request_id=%p\n",
                t_request_id.c_str(), static_cast<void*>(&t_request_id));
}

int main() {
    Task task = handle_request("req-42");
    task.resume();  // 线程 A（main）

    std::thread worker([&task] {
        t_request_id = "req-OTHER";  // 线程 B 自己的 TLS 实例
        task.resume();               // 线程 B 恢复同一个协程
    });
    worker.join();

    return 0;
}
```

```bash
g++ -std=c++20 -pthread -O2 tls_thread_migration.cpp -o tls_thread_migration
./tls_thread_migration
```

预期输出：

```text
[线程 A] 写入 TLS: req-42 &t_request_id=0x...
[线程 B] 恢复后读 TLS: req-OTHER &t_request_id=0x...
```

线程 A 写的 `req-42` 没有跟过来。线程 B 读到的是它自己的 TLS 实例，而那个实例里恰好被写成了 `req-OTHER`。如果把 `t_request_id = "req-OTHER";` 删掉，线程 B 会读到 `(unset)`。这就是“空的，或别人的”两种情况。

为什么？因为协程帧和 TLS 是两套存储：

```text
协程帧（coroutine frame）：
  局部变量、按值传入的参数、promise 对象
  跟着协程走，挂起/恢复/迁移都还在

TLS（thread_local）：
  每个 OS 线程一份
  跟着线程走，不跟着协程走
```

C++20 协程标准没有要求协程绑定线程。`co_await` 只是把执行权交还给调度器；调度器完全可以在另一条线程上调用 `resume()`。TLS 不会因为“同一个协程恢复了”而自动搬运。

#### 同线程共享 + 跨线程丢失：同一枚硬币的两面

把两个实验放在一起看：

```text
同线程：
  A、B 两个协程共享同一份 TLS
  => A 写的内容会被 B 覆盖
  => 上下文串扰

跨线程：
  协程从线程 1 迁移到线程 2
  => 线程 2 有另一份 TLS
  => 上下文丢失或错位
```

这不是两个独立的问题，而是同一条规则的两个方向：

> TLS 的键是线程，不是协程，也不是请求。

因此：

- 只要多个协程共享一条线程，它们就共享 TLS；
- 只要协程换一条线程，它就换一份 TLS；
- 你无法通过加锁、原子操作或内存序来改变“它到底是哪一份实例”。

#### 为什么这不是同步问题

一个常见的错误反应是：“那我给 TLS 加把锁不就行了？”

锁能解决的是**并发访问同一份数据**时的互斥，不能让 TLS 变成“请求私有”。即使你把所有 TLS 访问都用同一把互斥锁保护：

- 同线程的两个协程仍然共享同一份 TLS，A 的内容仍然会被 B 覆盖，只是覆盖动作变成了串行执行；
- 跨线程迁移后，协程仍然看到新线程的 TLS，锁保护的是新线程的那一份，不是它原来写的那一份。

所以这类问题不是 `data race`，而是**状态归属（state ownership）**问题。数据竞争问的是“两个线程有没有同时乱写同一块内存”；状态归属问的是“这块内存到底应该属于谁”。TLS 回答的是“属于线程”，而业务需要的是“属于请求”或“属于协程”。

---

### 3. 协程局部状态与显式传参：让请求拥有状态

既然 TLS 的归属不对，正确方向就是让状态跟着真正需要它的东西走。常见有三条路：

1. 显式传参：把 `RequestContext&` 或 `RequestContext*` 一层层传下去。
2. 放进协程帧：按值传进协程，或者作为协程内的局部对象。
3. 使用框架保证的 coroutine-local storage：由运行时在挂起/恢复时保存和恢复上下文。

#### 方案 A：显式传参

最直白的方式是改函数签名：

```cpp
void handle_message(const RequestContext& ctx) {
    log_info(ctx.request_id, "start");
    call_wechat_api(ctx.openid);
    write_db(ctx.request_id, ctx.openid);
}
```

优点：

- 依赖是显式的，读函数签名就知道它需要请求上下文；
- 测试时可以直接构造一个 `RequestContext` 传进去；
- 不依赖线程模型，迁移到哪条线程都能工作；
- 生命周期由调用方负责，容易推理。

代价：

- 参数要一层层传，调用链深时很啰嗦；
- 如果跨挂起点持有引用，必须保证被引用对象活得比协程久；
- 第三方库的接口未必接受你的上下文参数，可能需要适配层。

显式传参不是“土办法”，它是并发代码里最可靠的状态归属方式。很多框架的 coroutine-local storage 本质上也是在替你做显式传递，只是把传递藏进了运行时。

#### 方案 B：把上下文放进协程帧

如果不想层层传参，可以让协程按值持有上下文。按值传入协程的参数会被复制到协程帧里，跟着协程一起挂起、恢复、迁移。

```cpp
// request_owned.cpp
#include <coroutine>
#include <cstdio>
#include <exception>
#include <string>
#include <thread>
#include <utility>

struct RequestContext {
    std::string request_id;
    std::string openid;
};

struct Task {
    struct promise_type {
        Task get_return_object() {
            return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() { std::terminate(); }
    };

    std::coroutine_handle<promise_type> handle{};

    explicit Task(std::coroutine_handle<promise_type> h) : handle(h) {}
    Task(Task&& other) noexcept : handle(other.handle) { other.handle = {}; }
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            if (handle) handle.destroy();
            handle = other.handle;
            other.handle = {};
        }
        return *this;
    }
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    ~Task() { if (handle) handle.destroy(); }

    void resume() const { handle.resume(); }
};

void write_audit_log(const RequestContext& ctx) {
    std::printf("[%s] audit openid=%s\n",
                ctx.request_id.c_str(), ctx.openid.c_str());
}

Task handle_message(RequestContext ctx) {  // 按值：上下文进入协程帧
    std::printf("[%s] step1 请求自带上下文: openid=%s\n",
                ctx.request_id.c_str(), ctx.openid.c_str());

    co_await std::suspend_always{};  // yield：等微信 API

    std::printf("[%s] step2 恢复后读请求上下文: openid=%s\n",
                ctx.request_id.c_str(), ctx.openid.c_str());

    co_await std::suspend_always{};  // yield：等 MySQL

    write_audit_log(ctx);
}

int main() {
    RequestContext ca{"req-A", "openid-A"};
    RequestContext cb{"req-B", "openid-B"};

    Task a = handle_message(ca);
    Task b = handle_message(cb);

    a.resume();
    b.resume();
    a.resume();
    b.resume();
    a.resume();
    b.resume();

    return 0;
}
```

```bash
g++ -std=c++20 -pthread -O2 request_owned.cpp -o request_owned
./request_owned
```

预期输出：

```text
[req-A] step1 请求自带上下文: openid=openid-A
[req-B] step1 请求自带上下文: openid=openid-B
[req-A] step2 恢复后读请求上下文: openid=openid-A
[req-B] step2 恢复后读请求上下文: openid=openid-B
[req-A] audit openid=openid-A
[req-B] audit openid=openid-B
```

A 和 B 互不影响。原因是 `ctx` 不是 TLS，而是协程帧里的一份副本。协程挂起时，帧还在；协程迁移时，帧跟着走；协程销毁时，副本一起销毁。

这里有两个必须注意的边界：

- **按值传参会复制。** `RequestContext` 如果很大（例如带大缓冲、长字符串），复制成本要算清楚；可以在请求对象里用 `shared_ptr` 或稳定的请求存储，但不要为了省一次复制又把状态塞回 TLS。
- **按引用传参要保证生命周期。** 如果写成 `Task handle_message(RequestContext& ctx)`，协程帧里存的是引用；协程挂起期间如果调用方销毁了 `ctx`，恢复后就是悬空引用。引用适合“调用方保证活到协程结束”的场景，不适合跨请求边界的异步任务。

#### 方案 C：框架提供的 coroutine-local storage

很多协程框架会提供类似 `current_context()` 的 API，让你不用层层传参。它可能有两种实现：

1. **帧内存储**：框架把上下文存在协程帧或任务对象里，`current_context()` 从当前协程的帧里取。这种实现和方案 B 本质相同，只是 API 更顺手。
2. **TLS 交换**：框架仍然用 TLS 做“当前上下文”的快速访问，但在协程挂起/恢复时做保存和恢复。

第二种实现需要非常小心。一个正确的 TLS 交换逻辑，至少要在每次恢复和挂起时成对执行：

```text
on_resume(task):
    task.saved_tls = t_ctx          // 保存当前线程的 TLS
    t_ctx = task.current_ctx        // 安装本协程的上下文

on_suspend(task):
    task.current_ctx = t_ctx        // 把本协程的上下文存回任务
    t_ctx = task.saved_tls          // 恢复线程原来的 TLS
```

这段伪代码看起来简单，但工程上要满足一串条件：

- **每个挂起点都要走 `on_suspend`，每个恢复点都要走 `on_resume`。** 漏掉任何一条路径，TLS 就会残留或错位。
- **迁移线程时也要正确保存/恢复。** 不能假设恢复一定发生在原线程；保存的是“协程的上下文”，不是“原线程的 TLS 地址”。
- **嵌套协程要处理栈式恢复。** 父协程恢复子协程时，`saved_tls` 不能互相覆盖；通常需要按协程栈保存。
- **协程结束时必须清理。** 否则同线程的下一个协程可能读到上一个协程的残留上下文。
- **不参与框架的库无法自动修复。** 如果日志库、RPC stub 直接读裸 TLS，而框架只在业务协程边界做交换，那么库在挂起后读到的仍可能是错的。要么让库也接入框架的上下文 API，要么不要在这些库里缓存请求状态。

所以，“框架提供 coroutine-local storage”不是一句可以盲信的话。你必须确认它在 **yield/resume** 和 **线程迁移** 两种情况下都保存并恢复了状态，而且覆盖了所有挂起路径。

#### 三种生命周期对照

把三个概念放在一张表里：

| 生命周期 | 绑定对象 | 同线程多协程 | 跨线程迁移 | 销毁时机 | 典型用途 | 主要风险 |
| --- | --- | --- | --- | --- | --- | --- |
| 线程局部 | OS 线程 | 共享同一份 | 变成另一份，状态不跟随 | 线程退出 | 每线程缓存、统计、临时缓冲 | 请求串扰、迁移丢失、线程池残留 |
| 协程局部 | 协程帧/任务对象 | 各自独立 | 跟随协程帧 | 协程销毁 | 单个协程的请求上下文、状态机 | 实现必须覆盖挂起/恢复/迁移 |
| 请求局部 | 请求对象/请求作用域 | 可被多个协程共享 | 由请求对象显式携带 | 请求结束 | `request_id`、`trace_id`、鉴权信息、审计上下文 | 生命周期管理、跨协程传递成本 |

再强调一次：

- **线程局部**回答“哪条线程”，适合每线程资源，不适合请求身份。
- **协程局部**回答“哪个协程”，适合单个协程的执行上下文。
- **请求局部**回答“哪个请求”，适合跨多个协程、跨多个阶段的业务上下文。

一个请求可能包含多个协程（例如并行调用微信 API、MySQL、Redis），一个协程也可能处理多个请求（例如批处理）。所以“请求局部”和“协程局部”也不能随便画等号。

#### 线程安全 ≠ 协程安全

这两个词经常被混用，但它们回答的问题不同：

- **线程安全（thread-safe）**：多个线程并发访问同一份数据时，不会产生数据竞争、不会破坏不变量。手段有互斥锁、原子操作、每线程独立数据等。
- **协程安全（coroutine-safe）**：多个协程在同一线程上交替执行时，不会破坏彼此的**逻辑状态**。它关心的是挂起点前后的状态归属，而不是跨线程的可见性。

`thread_local` 很容易制造一种错觉：

```cpp
thread_local RequestContext t_ctx;  // 每个线程一份，看起来“没有共享”

void handle() {
    t_ctx.request_id = current_id;  // 线程安全？
    co_await something();           // 挂起
    use(t_ctx.request_id);          // 协程安全？
}
```

- 从**线程**角度看，`t_ctx` 每线程一份，多个线程同时写各自的实例，确实没有数据竞争。
- 从**协程**角度看，同线程的两个 `handle()` 共享同一份 `t_ctx`，在 `co_await` 处交替执行，A 的 `request_id` 会被 B 覆盖。

所以：**用了 TLS 的函数可以是线程安全的，但仍然不是协程安全的。** 这句话是本课验收的核心之一。

---

## 场景示例

### 场景一：日志里的 `trace_id` 串了

很多日志库支持“当前线程上下文”：业务代码不传 `trace_id`，日志库从 TLS 里取。单线程同步模型下，这很方便：

```cpp
thread_local std::string t_trace_id;

void on_message(const std::string& trace_id) {
    t_trace_id = trace_id;
    log_info("receive message");
    call_wechat_api();      // 内部可能 co_await
    log_info("api done");
}
```

一旦 `call_wechat_api()` 内部挂起，同线程另一条消息就会覆盖 `t_trace_id`。恢复后的 `log_info("api done")` 会把 A 的完成日志记到 B 的链路下。日志本身没有数据竞争，但链路追踪已经错了。

正确做法：把 `trace_id` 放进请求对象或协程帧，日志接口显式接收 `const RequestContext&`；或者确认日志库的上下文实现是 coroutine-local 的，而不是裸 TLS。

### 场景二：TLS 里的序列化缓冲被覆盖

为了减少分配，序列化库常常在 TLS 里复用缓冲：

```cpp
thread_local std::string t_json_buf;

std::string build_reply(const std::string& id) {
    t_json_buf.clear();
    t_json_buf += "{\"id\":\"";
    t_json_buf += id;
    t_json_buf += "\"}";
    co_await send_downstream();   // 挂起
    return t_json_buf;            // 可能已经是别人的内容
}
```

A 构造完缓冲后挂起，B 用同一个 TLS 缓冲构造自己的回包，A 恢复后返回的是 B 的内容。这个问题的本质和请求上下文串扰一样：**TLS 缓冲在 yield 点不是可重入的。** 解决方式：

- 缓冲跟随请求/协程，不放在 TLS；
- 或者在挂起前把需要的内容复制出来，保证挂起期间不再依赖 TLS 缓冲；
- 或者保证“写缓冲到使用缓冲”之间没有挂起点，但这在异步代码里很难长期维持。

### 场景三：工作窃取把协程迁到另一条 worker

工作窃取调度器会让空闲 worker 从其他 worker 的队列里偷任务。一个协程在 worker 1 上运行，遇到 IO 挂起；恢复时可能被 worker 2 捡到。

```text
worker 1: 协程写 TLS(request_id=A) -> 挂起
worker 2: 协程恢复 -> 读 TLS -> 空值或 worker 2 上另一个请求的值
```

这不是调度器“有 bug”，而是 TLS 的定义如此。调度器只负责搬运协程帧，不负责搬运 TLS。要修复，必须让请求状态进入协程帧/请求对象，或者使用真正支持迁移的 coroutine-local storage。

---

## 常见误区与设计权衡

### 误区 1：线程局部等于请求局部

这是本节点明确点出的陷阱。`thread_local` 的实例数量由线程数决定。一个线程可能依次处理很多请求，也可能同时交错执行很多协程。TLS 里的 `openid` 不会因为“换了一个请求”而自动切换，除非你显式重写；而显式重写也挡不住同线程协程在挂起点之后互相覆盖。

判断方法：问自己“这个值应该在哪个范围内唯一？”如果答案是“每个请求唯一”，那它就不该放在线程局部。

### 误区 2：线程局部等于协程局部

同线程的多个协程共享 TLS。协程切换不是线程切换，TLS 不会跟着 `co_await` 切换。很多人看到“协程是用户态调度、没有线程切换开销”，就误以为“协程有自己的局部存储”。标准 C++20 协程只保证协程帧里的局部变量和按值参数跟随协程；TLS 不在协程帧里。

把这两条合起来就是本节点最核心的 pitfall：**线程局部不等于请求局部，也不等于协程局部。**

### 误区 3：没有数据竞争就没有逻辑串扰

数据竞争是语言层的未定义行为，需要“不同线程 + 冲突访问 + 无 happens-before”。同线程协程交替访问 TLS 完全可能满足所有访问都有顺序，因此**不是数据竞争**。但业务状态仍然会串。把“没有 data race”当成“逻辑正确”，是本课最核心的误区。

### 误区 4：TSan 会报告同线程协程问题

ThreadSanitizer 检测的是数据竞争：它跟踪不同线程对同一内存位置的冲突访问，以及它们之间有没有同步关系。同线程协程的切换在 TSan 看来仍然是同一条线程上的顺序执行，所有访问都有 `sequenced-before`，所以它通常不会报告。

即使协程迁移到另一条线程，如果两条线程访问的是各自的 TLS 实例（不同内存位置），也不构成同一位置的数据竞争。TSan 可能保持沉默，但上下文已经丢了。**TSan 干净，不等于请求隔离正确。**

### 误区 5：函数里用了 TLS，所以线程安全，所以协程安全

“线程安全”和“协程安全”是两个维度。TLS 可以让每个线程拥有独立实例，从而避免跨线程数据竞争；但它不能阻止同线程协程共享实例。一个函数可以同时是“线程安全”和“协程不安全”的。不要把这两个结论连乘。

### 误区 6：TLS 可复用缓冲在 yield 点是可重入的

“可重入”意味着函数在执行到一半、被再次进入时仍然正确。TLS 缓冲在同步代码里可能看起来可重入，因为一次调用会完整跑完；但在协程里，`co_await` 就是一个可以被其他任务插入执行的挂起点。A 写到一半挂起，B 进来改写同一份缓冲，A 恢复后继续用，缓冲区已经不是 A 的了。

只要“写缓冲”和“读缓冲”之间可能挂起，就必须把缓冲的所有权提升到请求/协程级别，或者在挂起前完成复制。

### 误区 7：加锁或原子操作能修复 TLS 的归属问题

锁和原子操作解决的是并发访问的互斥与可见性，不改变“这份 TLS 属于哪条线程”。同线程协程仍然共享同一份实例；跨线程迁移仍然换一份实例。用锁保护一个归属错误的对象，只是让错误串行发生。

### 误区 8：线程池复用不会留下脏上下文

线程池的线程是长期存活的。请求结束时如果只清理了业务对象，没有清理 TLS，下一个请求可能读到上一个请求的 `request_id`、`openid` 或错误信息。即使你每次请求开始都重写 TLS，也要注意“重写之前”的代码路径、异常路径和日志路径是否可能读到旧值。

### 设计权衡

| 方案 | 生命周期 | 同线程多协程隔离 | 跨线程迁移 | 代价 / 风险 |
| --- | --- | --- | --- | --- |
| TLS 存请求上下文 | 每线程 | 不隔离，会串扰 | 丢失或读到别人的 | 参数传递少，但依赖隐藏、限制迁移 |
| 显式传 `RequestContext&` | 调用方/请求对象 | 隔离 | 跟随引用，需保证生命周期 | 参数多、调用链长；引用可能悬空 |
| 按值传入协程帧 | 协程 | 隔离 | 跟随协程帧 | 复制成本；大对象不适合按值 |
| 框架 coroutine-local storage | 协程 | 取决于实现 | 取决于保存/恢复 | API 方便，但必须审计挂起/恢复/迁移/嵌套 |
| 请求对象持有上下文 | 请求 | 多协程共享同一请求状态 | 显式携带 | 需要设计请求作用域和传递方式 |
| TLS + RAII 守卫 | 每线程 | 仍不隔离 | 仍会丢失 | 只能保证清理，不能保证协程隔离 |

一句话总结 tradeoff：**TLS 减少参数传递，但隐藏依赖并限制任务迁移。** 它适合“每线程资源”（线程本地缓存、统计、临时缓冲），不适合“每请求身份”。当状态需要跟着请求或协程走时，就应该让请求或协程拥有它。

---

## 动手实践

### 任务：同一线程复现串扰，改成请求拥有状态，再对比跨线程迁移

本实践对应 `practice`：

- 在同一操作系统线程运行两个可挂起任务，使它们依次写入 `thread_local` 请求上下文并在恢复后读取，复现上下文串扰；
- 再改为显式传递 request context 或使用框架保证的 coroutine-local storage，验证恢复后不串请求；
- 对比跨线程迁移后的行为（同一任务在线程 A 写入 TLS、在线程 B 恢复读取）。

验收对应：

- 能区分线程安全与协程安全；
- 恢复执行后上下文不会串请求；
- 能解释无数据竞争也可能存在逻辑串扰、TSan 不一定报告同线程协程问题；
- 明确区分线程局部、协程局部、请求局部三种生命周期，不把线程安全等同协程安全。

#### 步骤 0：检查环境

先确认编译器支持 C++20 协程，并记录版本：

```bash
g++ --version
echo 'int main(){}' | g++ -std=c++20 -x c++ - -o /tmp/cxx20_probe && echo "c++20 ok"
```

如果 `g++` 不支持 C++20 协程，可以用本课第 2 节的 C++17 状态机版本复现串扰；再按第 3 节的思路，把 `RequestContext` 放进 `Task` 对象而不是 TLS，完成隔离验证。跨线程迁移可以用 `std::thread` + 状态机模拟。记录你实际使用的编译器和标准版本。

检查 ThreadSanitizer 是否可用：

```bash
g++ -std=c++20 -pthread -O1 -g -fsanitize=thread \
    tls_coroutine_lab.cpp -o tls_coroutine_lab_tsan
```

如果链接失败或运行环境不支持 TSan，记录“未使用 TSan”，不要强行解读结果。

#### 步骤 1：编写实验程序

把前面的协程骨架和四种模式合并到一个文件里。这个程序包含：

- `bug`：两个协程在同一线程交替写 TLS，复现串扰；
- `fixed`：请求上下文按值进入协程帧，验证隔离；
- `migrate`：同一协程在线程 A 写 TLS，在线程 B 恢复；
- `fixed-migrate`：请求上下文按值进入协程帧，跨线程恢复仍然正确。

```cpp
// tls_coroutine_lab.cpp
#include <coroutine>
#include <cstdio>
#include <exception>
#include <string>
#include <thread>
#include <utility>

struct RequestContext {
    std::string request_id;
    std::string openid;
};

thread_local RequestContext t_ctx;

struct Task {
    struct promise_type {
        Task get_return_object() {
            return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() { std::terminate(); }
    };

    std::coroutine_handle<promise_type> handle{};

    Task() = default;
    explicit Task(std::coroutine_handle<promise_type> h) : handle(h) {}
    Task(Task&& other) noexcept : handle(other.handle) { other.handle = {}; }
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            if (handle) handle.destroy();
            handle = other.handle;
            other.handle = {};
        }
        return *this;
    }
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    ~Task() { if (handle) handle.destroy(); }

    void resume() const { handle.resume(); }
    bool done() const { return handle.done(); }
};

Task bug_task(std::string id) {
    t_ctx.request_id = id;
    t_ctx.openid = "openid-" + id;
    std::printf("[%s] 写入 TLS: request_id=%s openid=%s &t_ctx=%p\n",
                id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str(),
                static_cast<void*>(&t_ctx));

    co_await std::suspend_always{};  // 等微信 API

    std::printf("[%s] 恢复后读 TLS: request_id=%s openid=%s\n",
                id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());

    co_await std::suspend_always{};  // 等 MySQL

    std::printf("[%s] 写库前读 TLS: request_id=%s openid=%s\n",
                id.c_str(), t_ctx.request_id.c_str(), t_ctx.openid.c_str());
}

Task fixed_task(RequestContext ctx) {
    std::printf("[%s] 请求自带上下文: request_id=%s openid=%s &ctx=%p\n",
                ctx.request_id.c_str(), ctx.request_id.c_str(), ctx.openid.c_str(),
                static_cast<void*>(&ctx));

    co_await std::suspend_always{};

    std::printf("[%s] 恢复后读请求上下文: request_id=%s openid=%s\n",
                ctx.request_id.c_str(), ctx.request_id.c_str(), ctx.openid.c_str());

    co_await std::suspend_always{};

    std::printf("[%s] 写库前读请求上下文: request_id=%s openid=%s\n",
                ctx.request_id.c_str(), ctx.request_id.c_str(), ctx.openid.c_str());
}

Task migrate_task(std::string id) {
    t_ctx.request_id = id;
    std::printf("[线程 A] 写入 TLS: %s &t_ctx=%p\n",
                t_ctx.request_id.c_str(), static_cast<void*>(&t_ctx));

    co_await std::suspend_always{};

    std::printf("[线程 B] 恢复后读 TLS: %s &t_ctx=%p\n",
                t_ctx.request_id.c_str(), static_cast<void*>(&t_ctx));
}

Task fixed_migrate_task(RequestContext ctx) {
    std::printf("[线程 A] 请求自带上下文: %s &ctx=%p\n",
                ctx.request_id.c_str(), static_cast<void*>(&ctx));

    co_await std::suspend_always{};

    std::printf("[线程 B] 恢复后读请求上下文: %s &ctx=%p\n",
                ctx.request_id.c_str(), static_cast<void*>(&ctx));
}

int main(int argc, char** argv) {
    const std::string mode = (argc > 1) ? argv[1] : "bug";

    if (mode == "bug") {
        Task a = bug_task("req-A");
        Task b = bug_task("req-B");
        a.resume();
        b.resume();
        a.resume();
        b.resume();
        a.resume();
        b.resume();
        return 0;
    }

    if (mode == "fixed") {
        RequestContext ca{"req-A", "openid-A"};
        RequestContext cb{"req-B", "openid-B"};
        Task a = fixed_task(ca);
        Task b = fixed_task(cb);
        a.resume();
        b.resume();
        a.resume();
        b.resume();
        a.resume();
        b.resume();
        return 0;
    }

    if (mode == "migrate") {
        Task task = migrate_task("req-42");
        task.resume();  // 线程 A（main）

        std::thread worker([&task] {
            t_ctx.request_id = "req-OTHER";  // 线程 B 自己的 TLS 实例
            task.resume();                   // 线程 B 恢复同一个协程
        });
        worker.join();
        return 0;
    }

    if (mode == "fixed-migrate") {
        RequestContext ctx{"req-42", "openid-42"};
        Task task = fixed_migrate_task(ctx);
        task.resume();  // 线程 A（main）

        std::thread worker([&task] {
            t_ctx.request_id = "req-OTHER";  // 线程 B 的 TLS，不影响协程帧
            task.resume();                   // 线程 B 恢复同一个协程
        });
        worker.join();
        return 0;
    }

    std::fprintf(stderr, "usage: %s [bug|fixed|migrate|fixed-migrate]\n", argv[0]);
    return 1;
}
```

```bash
g++ -std=c++20 -pthread -O2 tls_coroutine_lab.cpp -o tls_coroutine_lab
```

#### 步骤 2：复现同线程串扰

```bash
./tls_coroutine_lab bug
```

预期输出：

```text
[req-A] 写入 TLS: request_id=req-A openid=openid-req-A &t_ctx=0x...
[req-B] 写入 TLS: request_id=req-B openid=openid-req-B &t_ctx=0x...
[req-A] 恢复后读 TLS: request_id=req-B openid=openid-req-B
[req-B] 恢复后读 TLS: request_id=req-B openid=openid-req-B
[req-A] 写库前读 TLS: request_id=req-B openid=openid-req-B
[req-B] 写库前读 TLS: request_id=req-B openid=openid-req-B
```

记录以下事实：

- A 和 B 的 `&t_ctx` 相同，因为它们在同一条线程上；
- A 在恢复后读到了 B 的 `request_id` 和 `openid`；
- 如果这段代码去写库，就会把 A 的操作写到 B 的账号上。

在笔记里写出时间线：

```text
A 写 TLS = A
A yield
B 写 TLS = B
B yield
A resume -> 读到 B
B resume -> 读到 B
A 写库 -> 用的是 B 的 openid
```

再回答两个问题：

1. 这里有没有数据竞争？没有。所有访问都在同一条线程上，线程内访问有先后顺序。
2. 这里有没有逻辑错误？有。A 的请求上下文被 B 覆盖，业务状态串了。

#### 步骤 3：用 TSan 做对照

```bash
g++ -std=c++20 -pthread -O1 -g -fsanitize=thread \
    tls_coroutine_lab.cpp -o tls_coroutine_lab_tsan
./tls_coroutine_lab_tsan bug
```

在支持 TSan 的环境里，你通常会看到和 `bug` 模式一样的串扰输出，但**没有数据竞争报告**。原因：

- TSan 检测的是不同线程对同一内存位置的冲突访问；
- 同线程协程的切换在 TSan 看来只是同一条线程上的顺序执行；
- 所有访问都有 `sequenced-before`，不满足数据竞争的条件。

再跑一次迁移模式：

```bash
./tls_coroutine_lab_tsan migrate
```

即使上下文已经丢失，TSan 也通常不会报告，因为线程 A 和线程 B 访问的是各自的 TLS 实例，不是同一个内存位置。这正好说明：**TSan 干净，不等于请求隔离正确。**

如果 TSan 在你的环境里不可用，记录“未使用 TSan”，并用输出和生命周期分析完成验收。不要因为工具没报错就认为程序正确。

#### 步骤 4：改为请求拥有状态

```bash
./tls_coroutine_lab fixed
```

预期输出：

```text
[req-A] 请求自带上下文: request_id=req-A openid=openid-A &ctx=0x...
[req-B] 请求自带上下文: request_id=req-B openid=openid-B &ctx=0x...
[req-A] 恢复后读请求上下文: request_id=req-A openid=openid-A
[req-B] 恢复后读请求上下文: request_id=req-B openid=openid-B
[req-A] 写库前读请求上下文: request_id=req-A openid=openid-A
[req-B] 写库前读请求上下文: request_id=req-B openid=openid-B
```

验证：

- A 的每一步都读到 `req-A` / `openid-A`；
- B 的每一步都读到 `req-B` / `openid-B`；
- 两条协程交错恢复，但互不覆盖。

原因：`ctx` 是按值传入协程的参数，复制在协程帧里。协程挂起时帧保留，恢复时读的是自己帧里的副本，不是线程 TLS。这个方案不依赖调度器是否迁移线程。

#### 步骤 5：对比跨线程迁移

先看错误版本：

```bash
./tls_coroutine_lab migrate
```

预期输出：

```text
[线程 A] 写入 TLS: req-42 &t_ctx=0x...
[线程 B] 恢复后读 TLS: req-OTHER &t_ctx=0x...
```

线程 A 写的 `req-42` 没有跟过来。线程 B 读到的是自己的 TLS 实例。把 `t_ctx.request_id = "req-OTHER";` 删掉再运行，线程 B 会读到 `(unset)`。这说明 TLS 在迁移后要么是空的，要么是别人的。

再看请求拥有状态的版本：

```bash
./tls_coroutine_lab fixed-migrate
```

预期输出：

```text
[线程 A] 请求自带上下文: req-42 &ctx=0x...
[线程 B] 恢复后读请求上下文: req-42 &ctx=0x...
```

即使恢复发生在线程 B，协程仍然读到 `req-42`。因为上下文在协程帧里，帧跟着协程走。这里的 `&ctx` 是协程帧中的对象地址，不是线程 TLS 的地址；它不应该被误读成“TLS 被迁移了”。

#### 步骤 6：填写实验记录

用下面的模板记录你的实际观察。不要编造数据，写你看到的输出和结论。

```markdown
# TLS 与协程共享边界实验记录（模板）

## 1. 环境
- 编译器与版本：______
- 标准版本：C++20 / C++17 状态机回退：______
- 操作系统与架构：______
- 是否使用 TSan：是 / 否，原因：______

## 2. 同线程串扰（bug）
- A 写入的 request_id：______
- B 写入的 request_id：______
- A 恢复后读到的 request_id：______
- 是否观察到串扰：是 / 否
- 是否有数据竞争：______
- TSan 是否报告：______

## 3. 请求拥有状态（fixed）
- A 恢复后读到的 request_id：______
- B 恢复后读到的 request_id：______
- 是否串请求：是 / 否

## 4. 跨线程迁移
- 线程 A 写入的 request_id：______
- 线程 B 读到的 TLS request_id：______
- fixed-migrate 中线程 B 读到的 request_id：______
- TLS 是否跟随协程迁移：______

## 5. 结论
- 线程局部、协程局部、请求局部三者的区别：______
- 为什么 TSan 不一定报告同线程协程问题：______
- 你会在项目里如何避免同类问题：______
```

#### acceptance 自检

- [ ] 能区分线程安全与协程安全：TLS 可以做到每线程独立、避免跨线程数据竞争，但同线程多个协程仍共享同一份实例，所以线程安全不等于协程安全。
- [ ] 恢复执行后上下文不会串请求：在 `fixed` 和 `fixed-migrate` 模式中，A、B 各自读到自己的 `request_id`，跨线程恢复也保持 `req-42`。
- [ ] 能解释无数据竞争也可能存在逻辑串扰：同线程协程的所有访问都有 `sequenced-before`，不构成数据竞争，但 TLS 实例被覆盖，业务状态仍然会串。
- [ ] 能解释 TSan 不一定报告同线程协程问题：TSan 找的是跨线程、同内存位置的冲突访问；同线程协程切换和各自的 TLS 实例都不满足这个条件。
- [ ] 明确区分线程局部、协程局部、请求局部三种生命周期：线程局部跟线程走，协程局部跟协程帧走，请求局部跟请求对象走。
- [ ] 不把线程安全等同协程安全：能举出“TLS 每线程一份，但同线程协程互相覆盖”的反例。

#### sourceStudy

围绕本节点给出的参考文档 `thread_local` 关键字页面（https://en.cppreference.com/w/cpp/keyword/thread_local.html），建议按以下路径核对实现与边界：

1. **定义**：确认 `thread_local` 指定的是**线程存储期（thread storage duration）**。每个线程拥有自己的实例，实例的生命周期与线程绑定；它不是请求级，也不是协程级。
2. **允许出现的位置**：核对页面上的限制——`thread_local` 只能用于变量：命名空间作用域的变量、块作用域的变量、类的静态数据成员；不能用于函数声明或函数参数。它可以和 `static`、`extern` 组合来调整链接性。
3. **初始化**：块作用域的 `thread_local` 在控制流第一次经过声明时初始化，同一线程后续经过不会重新初始化；命名空间作用域的 `thread_local` 动态初始化会在该线程第一次 odr-use 之前完成。用本课的 `tls_lifetime.cpp` 对照构造/析构输出，确认“每线程一份”的实际表现。
4. **销毁**：非平凡的 `thread_local` 对象在线程退出时销毁；主线程的实例在程序结束时销毁。检查析构顺序和异常路径：析构函数不应抛出异常；不要在析构时依赖另一个可能已经销毁的 TLS 对象；线程退出时的清理路径要和业务清理逻辑分开考虑。
5. **与 `static` 的对比**：`static` 是进程级一份（或按链接性/翻译单元划分），`thread_local` 是每线程一份。把本课第 1 节的三张表对上：自动存储期是栈帧级，静态存储期是进程级，线程存储期是线程级。
6. **协程边界**：C++20 协程标准没有“协程存储期”这个概念，也没有要求协程绑定线程。`thread_local` 不会在 `co_await` 时自动保存/恢复，也不会在协程迁移线程时自动搬运。核对这一点时，把本课的 `tls_coroutine_bug.cpp` 和 `tls_thread_migration.cpp` 作为反例。
7. **错误处理路径**：核对动态初始化抛出异常时的行为。块作用域的动态初始化如果抛出异常，变量视为未初始化，异常继续传播；命名空间作用域的情况要按标准条款和实现核对，不要假设异常可以安全地穿过线程启动/退出路径。实际工程中，TLS 对象的构造函数应尽量简单，避免在初始化阶段做可能失败的重操作。
8. **版本与平台记录**：`thread_local` 是 C++11 引入的；C++20 才引入标准协程。记录你使用的编译器版本、标准库版本、目标架构，以及是否在弱内存模型平台或不同调度器上验证过。若只在单线程/单 worker 环境跑过，要明确标注“未验证线程迁移”。

把“定义、初始化、销毁、与 static 的对比、协程边界、异常路径、版本”这几项写进实验笔记，比只记住“每个线程一份”更有用。

---

## 自测题

### 题 1

同一条 OS 线程上有两个协程，它们都先写 `thread_local std::string t_request_id`，然后 `co_await` 挂起，恢复后再读 `t_request_id`。A 先写 `"A"`，B 后写 `"B"`，A 恢复后读到 `"B"`。请回答：

1. 这里有没有数据竞争？
2. ThreadSanitizer 会不会报告？
3. 这是哪一种生命周期的错误？
4. 应该怎样修复？

**答案：**

1. **没有数据竞争。** 两个协程运行在同一条 OS 线程上，所有访问都在同一线程内，存在 `sequenced-before` 关系；没有两个线程同时访问同一个内存位置，因此不满足数据竞争的定义。
2. **通常不会报告。** TSan 检测的是跨线程的冲突访问；同线程协程的交替执行在 TSan 看来仍是同一条线程上的顺序执行。TSan 可能对这段代码完全沉默。
3. **把线程局部误当成了请求局部/协程局部。** `t_request_id` 是 `thread_local`，同线程的两个协程共享同一份实例。A 写完后挂起，B 覆盖了它；A 恢复后读到的自然是 B 的值。这是状态归属错误，不是可见性或原子性问题。
4. **让请求或协程拥有状态。** 把 `RequestContext` 按值传入协程，存进协程帧；或者显式传 `RequestContext&`；或者使用真正在挂起/恢复时保存和恢复上下文的 coroutine-local storage。不要用裸 TLS 保存请求身份。

### 题 2

请分别说明线程局部、协程局部、请求局部三种生命周期，并各举一个适合的场景。然后解释：为什么“函数里用了 TLS，所以它是线程安全的”不能推出“它是协程安全的”？

**答案：**

- **线程局部**：实例绑定 OS 线程，每条线程一份，线程退出时销毁。适合每线程资源，例如每线程统计计数器、线程本地缓存、临时缓冲。不适合保存请求身份，因为同线程的多个请求/协程会共享它。
- **协程局部**：实例绑定协程帧或任务对象，跟着协程挂起、恢复、迁移，协程销毁时销毁。适合单个协程的执行上下文，例如一个协程处理一条消息时需要的请求状态、状态机数据。
- **请求局部**：实例绑定请求对象或请求作用域，可能跨多个协程、多个阶段，请求结束时销毁。适合 `request_id`、`trace_id`、`openid`、鉴权信息、审计上下文等业务身份数据。

“TLS 所以线程安全”只说明：多个线程各自访问自己的 TLS 实例，不会对同一内存位置产生数据竞争。它不能推出协程安全，因为同一条线程上的多个协程共享同一份 TLS 实例；协程在 `co_await` 处交替执行，A 的状态会被 B 覆盖。线程安全回答“跨线程会不会乱写同一块内存”，协程安全回答“同线程交替执行时逻辑状态会不会串”。两者维度不同，不能连乘。

### 题 3

一个工作窃取调度器把协程从 worker A 迁移到 worker B。协程在 worker A 上写了 `thread_local std::string t_ctx = "req-42"`，然后在 worker B 上恢复。请回答：

1. 协程在 worker B 上读到什么？
2. 如果把 `RequestContext` 按值传进协程，结果会怎样？
3. 如果框架声称提供 coroutine-local storage，你必须确认它做到哪些事情？
4. 这个迁移场景会不会被 TSan 报告为数据竞争？

**答案：**

1. 读到 worker B 自己的 TLS 实例：可能是空值 `(unset)`，也可能是 worker B 上另一个请求留下的值。TLS 不会跟着协程迁移。
2. 按值传入的 `RequestContext` 会被复制到协程帧里，协程帧跟着协程走，所以 worker B 恢复后仍然读到 `req-42`。前提是传的是值，不是指向调用方栈对象的引用；引用需要额外保证生命周期。
3. 必须确认框架在**挂起时保存**当前上下文、在**恢复时安装**该协程的上下文；在**线程迁移**时也能正确保存/恢复，而不是假设恢复发生在原线程；处理**嵌套协程**的栈式恢复；在协程结束时**清理**，避免同线程下一个协程读到残留；并且覆盖所有挂起/恢复路径，包括异常路径。只“在创建时设置一次 TLS”是不够的。
4. 通常不会。worker A 和 worker B 访问的是各自的 TLS 实例，不是同一个内存位置；没有同一位置的冲突访问，就不构成 TSan 所检测的数据竞争。上下文丢失是逻辑错误，不是数据竞争。

---

## 推荐资源

- **TLS与协程共享边界：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/keyword/thread_local.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：重点阅读线程存储期与线程迁移和请求上下文，用于核对本节点实验的语义和边界。

---

## 本课小结

1. **`thread_local` 是线程存储期。** 每个 OS 线程一份独立实例，实例跟着线程走，不跟着请求走，也不跟着协程走。
2. **同线程协程共享 TLS。** `co_await` 不会切换 TLS；A 写、B 写、A 恢复，A 读到的就是 B 的值。这不是数据竞争，而是逻辑串扰。
3. **跨线程迁移会换一份 TLS。** 协程从 worker A 迁到 worker B，看到的是 worker B 的实例，可能是空的，也可能是别人的。协程帧会跟着走，TLS 不会。
4. **同线程共享和跨线程丢失是同一枚硬币的两面。** 它们都来自“TLS 的键是线程”这一条规则；加锁、原子操作、内存序都不能改变状态归属。
5. **没有数据竞争也可能有逻辑串扰。** 同线程协程的访问有 `sequenced-before`，TSan 通常不会报告。TSan 干净不等于请求隔离正确。
6. **线程安全不等于协程安全。** TLS 可以让每线程数据免于跨线程竞争，但同线程多个协程仍然共享同一份实例；函数可以是线程安全但协程不安全的。
7. **正确方向是让请求拥有状态。** 显式传 `RequestContext&`、按值放进协程帧、或使用请求对象持有上下文，都比裸 TLS 更可靠。
8. **框架的 coroutine-local storage 必须被审计。** 确认它在挂起/恢复、线程迁移、嵌套协程、异常路径和协程结束时都正确保存/恢复/清理；否则只是把 TLS 的 bug 藏得更深。
9. **三种生命周期要分清。** 线程局部跟线程走，协程局部跟协程帧走，请求局部跟请求对象走。选择哪一种，取决于状态应该在哪个范围内唯一。
10. **TLS 的 tradeoff 是“少传参数，换隐藏依赖和迁移限制”。** 它适合每线程缓存和统计，不适合请求身份；当状态需要跟着请求或协程走时，就让它跟着请求或协程走。
