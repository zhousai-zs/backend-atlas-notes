# 第 78/208 课：结构化并发、取消与停机

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 4 阶段 · 任务生命周期与诊断 |
| 前置 | e-condition、b-smart |
| 难度 | 深入 |
| 领域 | E |
| 标签 | Cancellation、E、C++ |
| 子分类 | 任务生命周期与诊断 |
| 节点 ID | e-cancel |
| 英文名 | Cancellation |

> 承接说明：前置节点 e-condition（条件变量与状态谓词，第 67 课）讲了条件变量必须配谓词、虚假唤醒，以及“等某个状态变化”时为什么不能只用一次 `wait`；前置节点 b-smart（智能指针与共享所有权，第 22 课）讲了 `shared_ptr` / `unique_ptr` 与对象所有权。本课把“谁拥有这段并发工作、工作结束前共享对象能不能销毁”接到结构化并发上：任务的拥有者应管理其启动、取消、等待和资源释放；取消通常是合作式请求而非强制终止。

---

## 导读

先看本节点的 summary，后面所有内容都围绕它展开：

> 任务的拥有者应管理其启动、取消、等待和资源释放，取消通常是合作式请求而非强制终止。

这句话里有两个关键词容易被轻轻放过：**拥有者**和**合作式**。很多人写并发代码时的默认动作是“起一个线程去干活”，然后把线程句柄一丢了之；工作有没有结束、结束前它引用的对象还在不在、服务停机时它会不会踩到已经释放的连接，全凭运气。结构化并发要做的第一件事，就是把“线程”从一种随手可丢的副作用，变成一种有明确归属、有明确结束点的资源。

第二件事更微妙：即使你承认“我得等它结束”，也不代表你能随时让它结束。操作系统层面当然可以把一个线程硬杀掉，但 C++ 层面没有一种安全的“强杀线程”手段——被强杀的线程可能正持有锁、正在改一条链表、正处在某个析构函数的中间。取消因此通常被设计成**合作式请求**：由拥有者发出“请你停下来”的信号，工作线程在它自己选定的安全点检查这个信号，然后主动退出、走完清理路径。

本节点给出的 why 是：**避免请求已结束而后台任务继续访问失效对象。** 这句话在微信公众号后台的开发里不是抽象风险，而是几类非常具体的故障：

1. **请求超时后仍写仓库。** 客户端设了 3 秒超时，网关到点已经给客户端返回了超时响应，处理线程却还在慢慢写数据库。更糟的是，请求上下文对象可能已经随着这次请求的收尾被析构，而处理线程还拿着指向它的指针去读 `article_id`、`user_id`，于是“超时”升级成了 use-after-free。
2. **客户端断开后仍打下游服务。** 用户关了页面、HTTP 连接已经断开，后台的 RPC 还在飞。对下游来说这是一次真实流量，会占连接、占线程、占配额；对本服务来说，结果已经没人接收，却还要为它付出完整成本。
3. **停机时仍在跑的工作线程访问已销毁的连接对象。** 典型的错误顺序是：先 `destroy` 连接池 / 下游客户端，再 `join` 工作线程。线程醒来发现 stop 还没被请求，继续拿着已经销毁的连接发请求——这正是节点 why 的反面教材。
4. **扇出多个下游 RPC 需要一起取消。** 一次“读文章”请求同时打用户资料、正文、推荐三个下游。其中推荐超时，或者客户端主动取消，另外两个如果继续跑，就是白白消耗资源；更严重的是，它们回来以后还要往一个已经结束的请求上下文里写结果。

本课沿着 3 个节点要点推进：

- **jthread 与 stop_token**：C++20 如何用“析构时 request_stop + join”把线程变成有归属的资源，以及 stop_token / stop_source / stop_callback 这套合作式取消的基本三角。
- **取消传播和截止时间**：父任务的取消如何传给子任务，截止时间如何与取消配合，`condition_variable_any` 如何把“等一个状态”变成“可被取消地等”。
- **join 顺序与资源释放**：停机的四步顺序，为什么必须先停止接入、再取消、再等待，最后才释放共享资源；以及“停机有界”到底难在哪里。

一个边界要在开头说清楚：本课讲的是 C++20 标准库提供的协作式取消工具，不是操作系统级的线程终结术。`std::stop_token` 只是“请求”的载体，它不会替你打断 `read`、`recv`、`pthread_cond_wait` 或者已经开始的 `sleep_for`。后面会反复回到这一点。

---

## 学习目标

### goals

- 能解释 `jthread` 与 `stop_token`。
- 能独立完成：实现可取消任务组，停机先停止接入再取消并等待任务。

### acceptance

- 取消不会被误报为业务成功。
- 所有任务有归属且停机有界。

把这两条验收翻译成可检查的产物，才算真的完成：

- **“取消不会被误报为业务成功”**：不是口头说“我知道取消不等于成功”，而是代码里有一个 `Result` 枚举，`Cancelled` 和 `Ok` 是两个不同的值；统计时 `Ok` 只统计 `Result::Ok`；程序结束时能打印出“取消 N 个、成功 M 个、业务失败 K 个”，并且断言 `N + M + K` 等于处理总数。更进一步，日志里能看到某个请求以 `Cancelled` 收尾，而它没有出现在成功计数里。
- **“所有任务有归属且停机有界”**：每个工作线程都由 `TaskGroup` 启动并持有；代码里没有 `detach`；停机时 `join_all` 带超时参数，正常路径下在超时前返回，异常路径下打印“hard timeout: 还有 N 个任务没退出”的日志。这里的“有界”不是数学证明，而是工程上的可观察事实：停机流程不会无声无息地永远卡住，卡住时有人留下证据。

### 先修自检

- e-condition：能写出“`wait(lock, pred)` + 谓词”的循环，能解释为什么 `wait` 返回不代表条件成立、必须重新检查谓词；能说出 `notify_all` 之后等待者还要重新抢锁。
- b-smart：能解释 `unique_ptr` 独占所有权、`shared_ptr` 共享所有权；能说出一个对象在最后一个 `shared_ptr` 析构时才会被销毁，因此“谁还可能访问它”决定了它能不能提前释放。

如果这两条里有任何一条说不清，先回去补：本课会同时用到“条件变量配谓词”和“共享对象的所有权边界”。

---

## 为什么后端要懂

本节点 why 的原话是：**避免请求已结束而后台任务继续访问失效对象。** 下面把四个公众号后台场景各展开一层，看清“失效对象”到底是怎么失效的。

### 1. 请求超时后仍写仓库：生命周期比请求长

假设接入层给每个请求分配一个 `RequestContext`，里面有 `article_id`、`user_id`、trace 信息，还有一个指向数据库会话的句柄。请求处理线程从队列里取出请求，开始执行“校验 → 写库 → 更新缓存”。

客户端 3 秒超时，网关返回 504，接入层把这次请求标记为结束，`RequestContext` 被回收。但处理线程没有收到任何“别写了”的信号，它还在 `INSERT`。等它写到最后一步要读 `ctx->user_id` 时，`ctx` 已经没了。这个故障的麻烦之处在于：

- 它不是每次必现，取决于数据库当时有多慢、请求上下文什么时候被回收；
- 它可能表现为崩溃，也可能表现为“写入了错误用户的数据”，后者更可怕；
- 超时响应已经发给客户端，业务方看到的是“失败”，数据库里却可能留下半条记录。

如果处理线程在关键步骤之间检查一次取消信号，并且上下文由拥有者保证“任务退出后才释放”，这类问题就会从“随机的 UAF”变成“确定的取消路径”。

### 2. 客户端断开后仍打下游服务：取消要能传播

读文章接口通常是一次扇出：用户资料服务、正文服务、推荐服务。客户端断开后，入口连接关闭，但三个下游 RPC 可能都还在飞。如果没有取消传播，它们会继续占用下游连接和本服务的等待线程；等结果回来，发现请求上下文已经结束，只能丢弃。

这里的核心不是“节省一点 CPU”，而是**资源的归属**：这次请求启动的所有子工作，应该由这次请求的拥有者统一管理。拥有者决定不干了，子工作应该收到同一个停止信号，而不是各自为政地跑完。

### 3. 停机时先销毁连接再 join：顺序错误的经典事故

服务收到 SIGTERM，停机函数开始执行。一个常见的错误写法是：

```cpp
// 错误示意：不要照抄
downstream_client.reset();   // 先释放下游客户端 / 连接池
workers.join_all();          // 再等工作线程结束
```

工作线程此时可能正在使用 `downstream_client`。它醒来后看到 `stop_requested()` 还是 false（因为还没人请求停止），于是继续发请求，访问已经析构的对象。正确顺序恰好相反：先让接入层不再接新请求，再请求停止，再等待任务退出，最后才释放共享资源。这个顺序就是本课要点三的主线。

### 4. 扇出 RPC 一起取消：一次请求是一个取消域

还是读文章接口。用户资料和正文都已经返回，推荐服务超时。此时正确的行为是：

- 对推荐这次调用标记超时（业务上的 `Timeout`）；
- 对另外两个仍在飞的下游调用发出取消；
- 等它们都以 `Cancelled` 或各自的结果收尾；
- 汇总时不要把“被取消”当成“成功”，也不要把“取消”当成“业务失败”混进错误率。

这就是“取消域”的概念：一次请求拥有一个 `stop_source`，它启动的所有子任务共享由它派生出来的 `stop_token`。父取消，子全停；子停完，父才能安全释放请求上下文。

### 一句话总结方向

**不要问“线程怎么杀”，要问“这段工作归谁拥有、它什么时候必须结束、结束前哪些对象必须还活着”。** 这三个问题答清楚了，`jthread`、`stop_token`、`join_all` 才有落点。

---

## 要点一：jthread 与 stop_token

### 从“起一个线程”到“拥有一段工作”

先把问题摆正。`std::thread` 的语义是“创建一个执行线程”，它本身不表达“这段工作属于谁、什么时候必须结束”。于是常见的写法变成：

```cpp
std::thread t(do_work);
t.detach();   // 把线程句柄丢掉，从此无人等待、无人取消
```

`detach` 之后，线程和创建它的作用域彻底脱钩。创建者离开作用域、对象析构、服务开始停机，都不会等待它。如果 `do_work` 捕获了某个局部对象的引用或裸指针，创建者析构后它继续访问，就是 use-after-free；如果进程退出，它可能在写到一半时被强行终止，析构函数不保证执行，缓冲里的数据可能丢失。

结构化并发的基本主张是：**父作用域拥有它启动的子任务，离开作用域之前必须等待子任务结束。** 这不是某个库的语法糖，而是一种责任划分：

- 谁启动，谁负责等待；
- 谁拥有取消信号，谁负责在合适的时机发出；
- 谁持有共享资源，谁负责在所有使用者退出后再释放。

`std::jthread`（C++20）把其中两件事做进了析构函数：**析构时先 `request_stop()`，再 `join()`**。于是“线程对象的生命周期”与“线程的执行生命周期”被绑定在一起：对象还在，线程就还在；对象要销毁，先请求停止，再等线程退出。

### jthread 的两个关键动作

```cpp
{
    std::jthread worker([](std::stop_token st) {
        while (!st.stop_requested()) {
            do_one_unit_of_work();
        }
        cleanup();
    });
    // ...
}   // 离开作用域：worker.~jthread() 先 request_stop()，再 join()
```

这里有两个细节必须说准：

1. **析构函数调用的是 `request_stop()`，然后 `join()`。** 请求停止只是把共享停止状态置为“已请求”，它不会从外部打断线程。线程要自己检查 `stop_requested()`，或者在可被 stop_token 唤醒的等待点上返回，才能真正退出。
2. **如果可调用对象的第一个参数能接收 `std::stop_token`，`jthread` 会把属于自己的 token 传进去。** 这个 token 观察到的是这个 `jthread` 内部 `stop_source` 的停止状态。线程体不需要自己去构造 token，也不应该把这个 token 当成“全局取消开关”。

`jthread` 还提供：

- `request_stop()`：请求停止，返回 `bool`，表示这次调用是否“首次”把状态从“未请求”变成“已请求”；
- `get_stop_source()`：拿到内部的 `stop_source`；
- `get_stop_token()`：拿到对应的 `stop_token`；
- `joinable()` / `join()` / `detach()`：和 `thread` 类似，但正常用法下你不需要 `detach`。

注意：`jthread` 的析构 `join` 是**无界**的。如果线程卡在一个不会响应 stop 的阻塞调用里，析构就会一直等下去。这一点是后面“停机有界”要处理的核心矛盾。

### 反例：detach 把“生命周期”交给了运气

```cpp
struct Session {
    int fd;
    std::string user;
};

void handle(Session& s) {
    // 假设这里会阻塞很久，并且期间反复读 s.user
    slow_work(s);
}

void start_bad(Session& s) {
    std::thread([&s] {
        handle(s);
    }).detach();     // 错误：线程可能在 s 析构后继续访问 s
}
```

调用方如果这么用：

```cpp
void request_scope() {
    Session s{42, "alice"};
    start_bad(s);
}   // s 在这里析构；detach 出去的线程还拿着 &s
```

`request_scope` 返回后，`s` 的存储期结束，但后台线程仍可能解引用它。这个例子里没有任何机制能保证“线程先结束，`s` 再销毁”。要修，至少得让拥有者持有线程并在释放 `s` 之前 join；而 `jthread` 把这件事变成了默认行为。

> 这里不展开“detach 之后立刻销毁对象”的运行结果，因为那是未定义行为：可能崩溃，可能读到旧值，也可能“看起来正常”。教学代码不依赖 UB 来证明问题，只把它标成危险写法。

### stop_token / stop_source / stop_callback 三角

`jthread` 只是把 stop 机制包了一层。真正要理解的是这三个类型的关系：

```text
                +-----------------------------+
                |   shared stop state         |
                |   (atomic: requested flag)  |
                +-----------------------------+
                   ^          ^           ^
                   |          |           |
          request_stop()      |      stop_requested()
                   |          |           |
             stop_source   stop_token   stop_token
                   |          |
                   |          +---- stop_callback(token, cb)
                   |
             get_token()
```

用文字说清楚：

- **`std::stop_source` 是“控制端”。** 它拥有共享停止状态；调用 `request_stop()` 把状态置为“已请求”。`stop_source` 可以拷贝，多个 `stop_source` 副本共享同一份状态；任意一个副本请求停止，所有观察者都能看到。
- **`std::stop_token` 是“观察端”。** 它是只读句柄，不能请求停止，只能查询：`stop_requested()` 返回是否已经请求；`stop_possible()` 返回是否还有可能被请求。线程体通常周期性检查 `stop_requested()`，或者把 token 传给支持 stop 的等待函数。
- **`std::stop_callback` 是“注册回调”。** 构造时把一个 token 和一个可调用对象绑定；当停止被请求时，回调会**同步执行**。如果构造时停止已经被请求，回调会**立即执行**。它常用来打断那些自己不会检查 token 的等待：关闭 socket、`notify_all` 一个条件变量、往 `eventfd` 写一个字节等。

一个最小的回调例子：

```cpp
#include <cstdio>
#include <functional>
#include <stop_token>

int main() {
    std::stop_source src;
    std::stop_token st = src.get_token();

    std::stop_callback<std::function<void()>> cb(st, [] {
        std::printf("[callback] stop requested, wake up waiters\n");
    });

    std::printf("before request: stop_requested=%d\n", st.stop_requested());
    src.request_stop();
    std::printf("after request : stop_requested=%d\n", st.stop_requested());
    // cb 在 request_stop() 返回之前已经同步执行完
}
```

编译：

```bash
g++ -std=c++20 -pthread -O2 -o stop_callback_demo stop_callback_demo.cpp
```

运行后你会看到回调的打印发生在 `request_stop()` 这一行返回之前——因为它是同步执行的，不是在某个后台线程里异步跑。这一点很重要：**不要在 stop_callback 里做长时间阻塞的操作**，否则调用 `request_stop()` 的线程会被拖住。

如果把这段改成“先 `request_stop()`，再构造 `stop_callback`”，构造函数会立刻调用回调。这也是标准行为，不是意外。

### 合作式取消 vs pthread_cancel

为什么不能“直接杀线程”？可以把线程想象成一个人正在操作一台共享机器：

- 他可能正握着一把锁。硬杀之后锁没有释放，其他线程永久卡死。
- 他可能正改到一半一条链表：已经改了头指针，还没改尾指针。硬杀之后结构处于中间态，后续访问直接坏掉。
- 他可能在某个对象的构造函数或析构函数中间。C++ 不保证栈展开，析构函数不一定走完，资源泄漏。
- 他可能刚分配了一块内存，还没来得及把指针存到任何地方。硬杀之后这块内存永远找不回来。
- 他可能正在写日志缓冲区。硬杀之后半条日志、撕裂的输出。

POSIX 的 `pthread_cancel` 看起来像“强杀”，但它并不是无条件、任意时刻都能安全使用的：它有取消点、延迟取消/异步取消、清理处理函数等一整套约定；在 C++ 代码里，异步取消尤其危险，因为编译器生成的栈展开和析构逻辑并不为“任意指令处被终止”设计。C++ 标准库没有提供线程强杀接口，这不是遗漏，而是有意为之。

因此本课的所有取消都是**合作式**的：

> 拥有者发出“请你停下来”的请求；工作者在安全点检查请求，自己决定如何退出、如何清理。

这也解释了本节点 summary 的后半句：**取消通常是合作式请求而非强制终止。** 既然是请求，就有“请求了但对方还没看到”的窗口；既然是合作，就要求每个可能长时间阻塞的地方都设计退出路径。

### 可编译例子：jthread 周期检查 stop_requested

```cpp
// jthread_basic.cpp
#include <chrono>
#include <cstdio>
#include <stop_token>
#include <thread>

int main() {
    std::jthread worker([](std::stop_token st) {
        int tick = 0;
        while (!st.stop_requested()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            if (st.stop_requested()) {
                break;
            }
            ++tick;
            std::printf("[worker] tick %d\n", tick);
        }
        std::printf("[worker] exit loop, stop_requested=%d\n",
                    static_cast<int>(st.stop_requested()));
    });

    std::this_thread::sleep_for(std::chrono::milliseconds(350));
    std::printf("[main] request_stop\n");
    worker.request_stop();
    std::printf("[main] leave scope, jthread will join\n");
}
```

编译与运行：

```bash
g++ -std=c++20 -pthread -O2 -o jthread_basic jthread_basic.cpp
./jthread_basic
```

输出大致是：

```text
[worker] tick 1
[worker] tick 2
[worker] tick 3
[main] request_stop
[main] leave scope, jthread will join
[worker] exit loop, stop_requested=1
```

顺序不保证完全一样（取决于调度），但有两件事是确定的：

1. `worker` 在 `request_stop()` 之后最多再睡完当前这 100ms，就会看到 `stop_requested()` 并退出；
2. `main` 离开作用域时，`jthread` 析构会等 `worker` 真正退出后才继续。

注意这里的“最多 100ms”不是取消的延迟保证，而是因为线程体自己选择了“每 100ms 检查一次”。如果线程体在 `sleep_for(10s)`，`request_stop()` 不会把它从睡眠里叫醒，它要睡满 10 秒才会看到请求。这正是 pitfalls 说的：**stop 请求不会中断任意阻塞系统调用。**

### 一个判断：什么时候该用 jthread

- 线程的生命周期明确绑定在某个作用域或对象上：用 `jthread`，让析构负责停和等。
- 需要把一个 token 传给多个子任务：用 `stop_source` 显式管理，再 `get_token()` 分发。
- 线程需要 detach 出去长期存活、且没有明确的拥有者：先停下来想清楚谁负责停它、谁保证它引用的对象还活着；大多数业务代码并不需要这种模式。
- 需要“等到线程结束，但最多等 N 毫秒”：`jthread` 本身不提供带超时的 join，需要自己用完成计数 + 条件变量（本课实践会实现），或者接受“超时后仍然 join”的风险并记录日志。

---

## 要点二：取消传播和截止时间

### 取消不是自动传播的

`stop_token` 不会沿着调用栈自动传播。父任务有一个 token，不代表它启动的子任务自动拥有同一个 token；你必须显式地把 token 传下去，或者用桥接的方式把“父停止”转换成“子停止”。

这里有两种常见结构：

**结构 A：共享同一个 stop_source。** 一次请求创建一个 `stop_source`，所有子任务都从它 `get_token()`。父任务和子任务看到的是同一份停止状态。这适合“一次请求 = 一个取消域”的场景，简单直接。

**结构 B：每个任务组有自己的 stop_source，用 stop_callback 桥接父信号。** 任务组需要独立管理自己的生命周期（比如组内还有额外的“只取消这个组”的操作），同时又要响应父级取消。这时注册一个回调：父 token 被请求停止时，回调里调用组自己的 `source.request_stop()`。

```cpp
#include <functional>
#include <optional>
#include <stop_token>

class Group {
public:
    explicit Group(std::stop_token parent)
        : bridge_(std::in_place, parent, [this] {
              // parent 停止 -> 组停止；若 parent 已停止，构造时立即执行。
              source_.request_stop();
          }) {}

    std::stop_token token() const { return source_.get_token(); }
    void cancel() { source_.request_stop(); }

private:
    // 先声明 source_，保证 bridge_ 先析构、source_ 后析构。
    std::stop_source source_;
    std::optional<std::stop_callback<std::function<void()>>> bridge_;
};
```

这里用 `std::optional` 是因为 `std::stop_callback` 没有默认构造函数，不能先在成员列表里“占个空位”、以后再赋值。如果不需要“可选注册”，也可以把桥接对象放在构造函数外（下面用 `std::function` 存回调，需要 `<functional>`）：

```cpp
std::stop_source request_src;
std::stop_token request_token = request_src.get_token();

std::stop_source group_src;
std::stop_callback<std::function<void()>> bridge(request_token, [&group_src] {
    group_src.request_stop();
});   // 父停止时，组也停止

std::stop_token child_token = group_src.get_token();
```

如果 `request_token` 在构造 `bridge` 之前就已经处于“已请求”状态，回调会立即执行一次，`group_src` 直接进入停止状态。这正好符合“父已经取消，子不应该再开始”的语义。

### 扇出示例：一个请求，三个下游

```cpp
struct ReadArticleRequest {
    std::stop_source src;
    std::stop_token token() const { return src.get_token(); }
};

// Result 与 TaskGroup 的完整定义见要点三和实践节：Ok / Cancelled / Error。
Result call_user_profile(std::stop_token st);
Result call_article_body(std::stop_token st);
Result call_recommendation(std::stop_token st);

void handle_read_article(ReadArticleRequest& req) {
    TaskGroup group;

    // 父请求停止 -> 组停止。bridge 必须在 group 之前析构（它声明在 group 之后）。
    // 需要 <functional> 来存放回调类型。
    std::stop_callback<std::function<void()>> bridge(req.token(), [&group] {
        group.request_stop();
    });

    group.spawn([&](std::stop_token st) { call_user_profile(st); });
    group.spawn([&](std::stop_token st) { call_article_body(st); });
    group.spawn([&](std::stop_token st) { call_recommendation(st); });

    // 任一子任务发现请求已取消，或父请求被取消：
    // group 的 stop_source 被 request_stop，三个子任务共享停止状态
    group.join_all(std::chrono::milliseconds(500));
}
```

这个例子里，“推荐超时”应该怎么让另外两个也停？有两种设计：

- 由协调者统一判断：推荐先返回 `Timeout`，协调者调用 `group.request_stop()`，另外两个在下一个检查点退出。
- 由每个子任务自己响应父 token：父请求被取消时，所有 token 都变成已请求，不需要谁去通知谁。

关键是：**取消域要有明确的拥有者。** 如果三个下游各自拿一个互不相干的 token，那么“一起取消”就只能靠调用方记得挨个通知，迟早漏掉一个。

### 截止时间：这条请求还能活多久

截止时间（deadline）和取消是两个正交的概念：

- **截止时间**回答：“这条请求最多允许活到什么时候？”它通常来自上游的超时预算（比如入口给了 800ms，已经花了 200ms，还剩 600ms）。
- **取消**回答：“拥有者还要不要你继续？”它可能来自客户端断开、上游请求取消、服务停机、或者另一个子任务已经失败。

它们经常一起使用：截止时间到达时，协调者往往会对任务发出取消，让还在等待的子任务尽快收尾。但语义上必须分开：

- 超时是一个**业务结果**（`Timeout`），不是取消；
- 取消是**控制信号**（`Cancelled`），不是业务成功，也不一定是业务失败；
- 一个任务可能因为超时被取消，但最终结果应该记录为 `Timeout` 或 `Cancelled`，取决于你的结果模型，不能混成 `Ok`。

### 用 condition_variable_any 可取消地等待

第 67 课讲过：等待条件变量必须配谓词，`wait` 返回不代表条件成立。C++20 给 `std::condition_variable_any` 增加了接受 `std::stop_token` 的 `wait` / `wait_until` / `wait_for` 重载。它们的价值是：等待者不仅会被 `notify_all` 唤醒，也会在 stop 被请求时返回。

注意：**普通的 `std::condition_variable` 没有这些重载。** 如果你需要“等条件或等取消”，要用 `condition_variable_any`，或者自己用 `stop_callback` 去 `notify_all` 普通条件变量。

下面是一个“等下游响应，但可以被取消或超时唤醒”的完整小例子：

```cpp
// wait_deadline.cpp
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <stop_token>
#include <thread>

enum class WaitOutcome { Ready, Timeout, Cancelled };

WaitOutcome wait_downstream(std::stop_token st,
                            std::chrono::steady_clock::time_point deadline,
                            std::mutex& m,
                            std::condition_variable_any& cv,
                            bool& response_ready) {
    std::unique_lock lk(m);
    while (!response_ready) {
        if (st.stop_requested()) {
            return WaitOutcome::Cancelled;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            return WaitOutcome::Timeout;
        }
        // 返回 false 表示被 stop 或超时唤醒；返回 true 表示谓词成立。
        cv.wait_until(lk, st, deadline, [&] { return response_ready; });
    }
    return WaitOutcome::Ready;
}

int main() {
    std::mutex m;
    std::condition_variable_any cv;
    bool ready = false;
    std::stop_source src;

    std::jthread responder([&] {
        std::stop_token rst = src.get_token();   // 共享请求的停止状态
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        if (rst.stop_requested()) {
            std::printf("[responder] cancelled before publishing\n");
            return;
        }
        {
            std::lock_guard lk(m);
            ready = true;
        }
        cv.notify_all();
    });

    auto deadline = std::chrono::steady_clock::now()
                  + std::chrono::milliseconds(100);
    WaitOutcome outcome = wait_downstream(src.get_token(), deadline, m, cv, ready);

    const char* name = "?";
    switch (outcome) {
        case WaitOutcome::Ready:     name = "Ready"; break;
        case WaitOutcome::Timeout:   name = "Timeout"; break;
        case WaitOutcome::Cancelled: name = "Cancelled"; break;
    }
    std::printf("[main] wait outcome = %s\n", name);

    src.request_stop();   // 告诉 responder：请求已经不要结果了
    responder.join();     // 等它醒来并退出，再离开作用域
}
```

编译：

```bash
g++ -std=c++20 -pthread -O2 -o wait_deadline wait_deadline.cpp
```

这段代码有几个教学点：

1. **等待必须放在循环里。** `wait_until` 返回后，可能只是 stop 被请求、可能只是超时、也可能只是虚假唤醒；回到循环顶部重新检查 `stop_requested`、deadline 和谓词，才是完整逻辑。
2. **先检查 stop，再检查 deadline。** 两者同时成立时，谁优先取决于业务约定。这里选择“取消优先”，因为拥有者已经不要结果了，报 `Cancelled` 比报 `Timeout` 更准确。
3. **`wait_until` 的 stop_token 重载返回 `bool`。** 它返回 `false` 时，表示“谓词没有成立就返回了”，可能因为 stop，也可能因为超时；不要把它直接当成“已取消”。
4. **`condition_variable_any` 比 `condition_variable` 更重。** 它能配任意可锁类型、能接 stop_token，但内部通常要做更多工作。不要因为“万能”就在所有地方替换普通条件变量。

### 超时之后做什么

截止时间到达后，协调者通常做三件事：

1. 对还在跑的子任务 `request_stop()`；
2. 给它们一个短暂的收尾窗口（比如再等几十毫秒），让它们走到安全点；
3. 记录结果：这次请求是 `Timeout`，被停掉的子任务是 `Cancelled`，两者分别计数。

这里不要犯两个错误：

- 把超时当成“成功返回一个空结果”。用户看到的是 200，实际是下游没返回，这是误报。
- 把取消当成“业务错误”混进错误率。取消是控制流，不是下游业务失败；混在一起会让告警和容量分析失真。

---

## 要点三：join 顺序与资源释放

### 正确停机顺序：四步

先把结论写成一张表：

| 步骤 | 动作 | 目的 | 做错会怎样 |
| --- | --- | --- | --- |
| 1 | 停止接入：`accepting = false`，关闭 listen，拒绝新请求 | 不再扩大工作集 | 边停边接，永远停不完 |
| 2 | 请求取消：`request_stop()` | 让已启动的任务知道该收尾 | 任务继续跑，甚至继续访问即将释放的资源 |
| 3 | 带超时地等待：`join_all(deadline)` | 确认所有任务退出，停机有界 | 直接释放资源，出现 UAF；或无限等待，停不下来 |
| 4 | 释放共享资源：连接、缓冲、下游 channel、请求上下文 | 最后清账 | 先释放再 join，正是节点 why 的反面 |

这个顺序不是风格问题，而是所有权问题：**共享资源的最后一个使用者是工作线程，所以工作线程必须先退出，资源才能销毁。** 第 1 步先停止接入，是为了让“工作集”不再增长；否则你一边等旧任务，一边又接新任务，join 的集合永远在扩大。

### 反例：先释放共享对象再 join

```cpp
// 错误停机顺序示意：不要照抄
void shutdown_bad(TaskGroup& group,
                  std::unique_ptr<DownstreamConn>& conn) {
    conn.reset();          // 第 1 步就释放了下游连接
    group.request_stop();  // 太晚：可能已有线程正在用 conn
    group.join_all(std::chrono::milliseconds(500));
}
```

如果某个工作线程此刻正卡在 `conn->send()` 里，`conn.reset()` 之后它手里的裸引用/引用就悬空了。即使它很快会响应 stop，也已经晚了：`request_stop()` 只是把标志位置位，不会把时间倒流回“还没释放”的那一刻。

更隐蔽的版本是“连接对象没被销毁，但内部的 fd 被关了”：

```cpp
conn->close_fd();        // 对象还在，fd 已失效
group.request_stop();    // 工作线程可能正拿着这个 fd 做 send/recv
group.join_all(...);
```

对象没析构不代表资源还能用。判断标准不是“指针有没有失效”，而是“工作线程还可能访问的每一个资源，是否都活到了它退出之后”。

### 停机有界：为什么 join 不是“一定能停”

`jthread` 的析构函数会 `join()`，而 `join()` 没有超时参数。它只保证“等到线程函数返回”，不保证“在某个时间内返回”。如果线程卡在下面这些地方，析构就会一直等：

- `recv(fd, ...)` / `read(fd, ...)`：阻塞在系统调用里，`request_stop()` 不会让它返回；
- `pthread_cond_wait`：没有 stop_token 参与，stop 请求不会自动唤醒；
- `std::mutex::lock()`：等锁的线程不会因为 stop 请求而放弃等待；
- 长时间 `sleep_for` / `sleep_until`：标准库的睡眠不接收 stop_token（有些实现提供可中断的 sleep，但不能依赖）；
- 下游 SDK 内部的阻塞调用：你甚至看不到它阻塞在哪。

所以“析构时会 join”不等于“析构一定能在有界时间内完成”。工程上的“停机有界”需要主动设计：

1. 为每个可能长时间阻塞的点提供退出路径：socket 设置超时、用 `poll` / `epoll` 并监听停止事件、用 `condition_variable_any` 的 stop_token 重载、周期检查 `stop_requested()`。
2. 在等待侧设置预算：`join_all(500ms)`，超时就打日志，指出还有哪些任务没退出。
3. 超时后的策略要提前定：是继续等（可能永远等）、是进程级重启（由外部监督者接管）、还是接受“硬杀”的代价。最后一种不是免费的：锁不会释放、堆可能泄漏、不变量可能被撕破、析构函数不保证执行。

教学代码里常见的做法是“超时后打印日志，然后再等一小段，最后仍然 join”。这样程序能正常结束，也把“硬超时”的位置标了出来。不要在示例里随手 `std::terminate()` 或 `_exit()` 假装问题解决了——那只是把烂摊子留给操作系统。

停机时可以用 Linux 的线程视角核对“工作线程是否真的退出了”：

```bash
# 把 <pid> 换成服务进程号；-T 会列出该进程下的每个 LWP（轻量级进程，也就是线程）
ps -T -p <pid>

# 每个数字是一个线程 id
ls /proc/<pid>/task

# 关注线程数是否在 request_stop 之后下降，而不是只看进程还活着
```

如果 `ps -T` 里除了主线程，停机窗口之后还挂着若干工作线程，说明 join 还没完成，或者有线程卡在了不响应 stop 的阻塞点上。这比只看进程是否存活更能定位“停机有界”有没有做到。

### 结果分类：Cancelled / Ok / Error

本课实践用这个枚举：

```cpp
enum class Result { Ok, Cancelled, Error };
```

三者的语义必须分清：

| 值 | 含义 | 是否计入成功 | 是否计入业务错误 | 典型来源 |
| --- | --- | --- | --- | --- |
| `Ok` | 业务处理完成，结果是成功 | 是 | 否 | 下游正常返回、写库成功 |
| `Cancelled` | 被拥有者取消，没有完成业务 | 否 | 否（单独统计） | 停机、父请求取消、超时后主动停 |
| `Error` | 业务处理失败 | 否 | 是 | 下游报错、校验失败、写库失败 |

关键纪律：**只有 `Result::Ok` 才能让成功计数加一。** 不要写这种代码：

```cpp
// 错误：取消被当成了成功
if (result != Result::Error) {
    ++success;
}
```

也不要把取消混进业务错误率：取消是控制流，不是下游故障。否则一次正常停机就会让错误率飙升，告警系统被误伤。

### 一个正确的停机骨架

```cpp
void shutdown(Service& svc, TaskGroup& group) {
    // 1. 停止接入：不再产生新任务
    svc.accepting.store(false, std::memory_order_relaxed);
    svc.close_listener();
    svc.cv.notify_all();   // 唤醒等在“有新请求”上的工作线程

    // 2. 请求取消：已启动的任务收到停止信号
    group.request_stop();

    // 3. 带超时地等待
    const bool all_done = group.join_all(std::chrono::milliseconds(500));
    if (!all_done) {
        std::printf("[shutdown] hard timeout: some tasks did not exit\n");
    }

    // 4. 所有任务退出后，才释放共享资源
    svc.release_shared_resources();
}
```

注意第 1 步里的 `notify_all`：如果工作线程正等在“队列非空”的条件变量上，单纯把 `accepting` 置 false 不会叫醒它们。要么用 `condition_variable_any` + stop_token 让第 2 步的 `request_stop()` 唤醒它们，要么在第 1 步显式 notify。两者选一个，不能都指望对方。

### “先停止接入”到底停什么

“停止接入”不只是关掉 listen socket。它至少包含：

- 不再从 listen queue 里 `accept` 新连接；
- 不再从内部请求队列里取新请求（或者允许把已经在队列里的请求处理完，取决于停机策略）；
- 不再向上游注册新的定时器 / 重试任务；
- 不再产生新的后台子任务。

如果只关了 listen，但内部还有一个“重试调度器”每隔 100ms 往队列里塞任务，那么工作集仍然在增长，join 依然可能等不到头。停机顺序的第一步要覆盖所有“新工作的入口”，而不只是最外层那个 socket。

---

## 常见误区和设计权衡

### 误区一：以为 request_stop 能打断任意阻塞调用（本节点 pitfalls 原文）

本节点的 pitfalls 是：**stop 请求不会中断任意阻塞系统调用。** 这句话需要展开到“具体哪些不会”：

- `recv(fd, buf, len, 0)`：线程阻塞在内核里等数据。`request_stop()` 只是改了用户态共享停止状态的一个原子标志，内核里的 `recv` 根本不知道这件事。除非对端发来数据、连接被关闭、socket 超时，或者线程被信号唤醒，否则它不返回。
- `read(fd, ...)`：同上。管道、终端、socket 的阻塞读都不会因为 stop 标志变化而返回。
- `pthread_cond_wait(cv, mutex)`：它等的是“有人 notify + 谓词可能变化”。stop 请求不会自动替你 notify；`pthread_cond_wait` 本身也不认识 stop_token。除非有人在 `stop_callback` 里 `pthread_cond_broadcast` / `notify_all`，否则它会一直睡。
- `std::mutex::lock()`：等锁的线程不会因为 stop 请求而放弃。它没有“可取消的锁”这种语义；要退出，只能等拿到锁之后自己检查。
- `std::this_thread::sleep_for(10s)`：标准库的睡眠不接收 stop_token，不会被请求唤醒。你只能把长睡眠切成小段，每段之间检查 stop。
- `poll` / `epoll_wait`：阻塞在事件循环里，stop 请求不会让它返回。要么设置超时，要么用 eventfd / pipe 往等待集合里塞一个“停止事件”。

为什么？因为 `stop_token` 不是内核级线程终止原语，也不是异步信号。它是一个**协作式标志**：

- 工作线程主动调用 `stop_requested()` 才能看到它；
- 等待函数主动接收 token，才会在 stop 时返回；
- `stop_callback` 注册的回调，才会在 `request_stop()` 时同步执行。

三者之外，没有任何魔法。把 `request_stop()` 想成“给线程发了一个它可以选择查看的通知”，而不是“按下了终止按钮”。

### 误区二：把 detach 当成“简化生命周期”

`detach` 并没有简化生命周期，它只是把“谁负责等待、谁负责取消、谁负责释放”这三个问题从代码里删掉了。问题不会消失，只会变成偶发的 UAF、退出时的数据丢失、测试环境复现不了的线上崩溃。如果确实需要一个长期后台线程，也应该有一个明确的服务对象持有它，在服务析构时请求停止并 join。

### 误区三：把 pthread_cancel 当快捷键

`pthread_cancel` 不是 C++ 标准的一部分，而且它的“取消点”语义与 C++ 对象的生命周期配合起来非常容易出错。异步取消可能在任意指令处终止线程，析构函数、栈展开、锁释放都没有保证。除非你在维护一个非常底层的、完全理解取消点与清理处理器的系统，否则不要用它来解决业务层的“停不下来”。

### 误区四：忘记区分取消与业务成功

这是 acceptance 第一条直接针对的问题。常见的错误映射包括：

- `if (!error) ++success;`：把 `Cancelled` 也算成成功；
- 返回一个空结果 + 200 状态码：调用方以为“查到了，只是没数据”；
- 把取消计入错误率：一次停机让监控红一片。

正确做法是把 `Cancelled` 作为独立的结果类别，单独计数、单独日志、单独告警策略。它可以不是错误，但它绝对不是成功。

### 误区五：先释放共享对象再 join

这是要点三的反例，也是节点 why 的直接来源。判断一段停机代码是否正确，可以问一个简单的问题：**“工作线程最后可能访问的每一个对象，是在 join 之后才销毁的吗？”** 如果答案里有一个“不是”，顺序就错了。

### 误区六：以为 request_stop 等于立刻终止

`request_stop()` 返回时，线程可能还在跑：它可能正睡在 `sleep_for` 里，可能正卡在 `recv` 里，可能刚检查完 `stop_requested()` 准备进入下一段工作。请求停止和线程真正退出之间有一个窗口，这个窗口的长度取决于工作线程的检查频率和阻塞点设计。停机逻辑必须等这个窗口结束（join），不能假设“request_stop 一调用，线程就没了”。

### 误区七：jthread 析构 join 无界，却当成“一定能停机”

`jthread` 的析构会 join，但 join 没有超时。如果线程不响应 stop，析构会挂死。把 `jthread` 当成“停机保险箱”是错误的；它只是把“必须 join”变成了默认行为，没有解决“join 等不到怎么办”。后者需要超时等待、可中断阻塞点、进程级兜底等工程手段。

### tradeoffs：本节点的原话

本节点的 tradeoffs 是：**合作式取消可保持不变量，但所有阻塞点都需退出设计。**

展开来说：

- **好处**：工作线程在安全点退出，锁可以正常释放，C++ 对象可以正常析构，业务不变量（比如“链表头尾一致”“连接状态机合法”）不会被从中间撕破。取消路径和正常路径可以共用清理逻辑。
- **代价**：每一个可能长时间阻塞的地方，你都要回答“它怎么醒来”。常见的退出设计有三类：
  1. **周期检查**：把长循环切成小段，每段检查 `stop_requested()`；适合计算密集型任务。
  2. **stop_callback 打断等待**：注册回调，在停止时 `shutdown()` socket、`notify_all()` 条件变量、往 eventfd 写一个字节，让阻塞在 `poll` / `epoll_wait` / 条件变量上的线程返回。注意：`close()` 一个正被 `recv` 使用的 fd 在不同平台上的行为并不完全一致，Linux 上更稳妥的做法通常是用 `shutdown(fd, SHUT_RDWR)` 让阻塞的 `recv` 返回，或者统一走 eventfd 唤醒事件循环。
  3. **可取消的等待原语**：`condition_variable_any` 的 stop_token 重载、带超时的 `poll` / `epoll_wait`、下游 SDK 提供的 cancel API。

没有一种设计能“自动覆盖所有阻塞点”。这就是合作式取消的真实成本：安全是有代价的，代价是你必须知道代码在哪里会停、以及怎么让它醒。

---

## 动手实践：实现可取消任务组，停机先停止接入再取消并等待任务

### 实验目标

这个实验把本课三个要点落到一个最小服务上：

- 一个 `TaskGroup` 拥有 `stop_source`，用 `std::jthread` 启动工作线程，提供 `request_stop()` 和带超时的 `join_all()`，析构时自动停止并等待；
- 一个极简的“公众号后台接入服务”：`accepting` 标志控制是否接新请求，队列里放请求 id，若干工作线程从队列取请求并处理；
- 工作函数返回 `Result { Ok, Cancelled, Error }`，主程序分别统计，并用断言证明“取消没有被算成成功”；
- 停机按四步打印：停止接入 → 请求取消 → 带超时等待 → 释放共享资源。

### 结构图

```text
main
  │
  ├─ SharedState: accepting / queue / counters / downstream_name
  │
  ├─ TaskGroup (owns stop_source)
  │    ├─ jthread worker 0 ──┐
  │    ├─ jthread worker 1   ├─ queue_cv.wait(lock, group_token, pred)
  │    └─ jthread worker 2 ──┘
  │
  └─ shutdown:
       1. accepting = false; notify_all
       2. group.request_stop()
       3. group.join_all(200ms)
       4. release downstream_name and other shared resources
```

### 步骤 1：TaskGroup 的接口

先把接口定下来，不急着写实现：

```cpp
class TaskGroup {
public:
    ~TaskGroup();                                  // request_stop + join_all
    std::stop_token token() const;                 // 给子任务观察
    bool request_stop();                           // 请求组内所有任务停止
    template <class F> void spawn(F&& f);          // 启动一个 jthread
    bool join_all(std::chrono::milliseconds timeout); // 有界等待
private:
    std::stop_source source_;
    std::mutex mu_;
    std::condition_variable cv_;
    int running_ = 0;
    std::vector<std::jthread> threads_;   // 最后声明，最先析构（join）
};
```

几个设计决定：

- **用 `std::jthread` 持有线程。** 这样即使 `join_all` 因为异常路径没被调用，`TaskGroup` 析构时 `jthread` 的析构仍然会 join。但注意：我们让线程体执行的是“组 token”，而不是 jthread 自己的 token；取消统一由 `TaskGroup::request_stop()` 发出。
- **`running_` 是完成计数。** `std::jthread` 没有带超时的 join，`join_all` 只能先等“所有线程函数返回”，再逐个 `join()`。完成计数配合条件变量，提供了“最多等 N 毫秒”的能力。
- **`spawn` 里先加计数再启动线程。** 否则线程可能瞬间结束、先减计数，导致计数变成负数。

### 步骤 2：join_all 的“有界”到底界在哪

```cpp
bool join_all(std::chrono::milliseconds timeout) {
    std::unique_lock lk(mu_);
    bool done = cv_.wait_for(lk, timeout, [this] { return running_ == 0; });
    if (!done) {
        std::printf("[shutdown] hard-timeout: %d task(s) still running\n",
                    running_);
    }
    lk.unlock();

    for (auto& t : threads_) {
        if (t.joinable()) {
            t.join();
        }
    }
    threads_.clear();
    return done;
}
```

这段代码的“有界”只体现在**等待计数归零这一步**。如果超时了，它仍然会执行 `join()`，而 `join()` 本身没有超时——这是教学示例为了能正常退出而做的选择，也是真实系统里的“硬超时策略决策点”：

- 如果所有任务最终都会响应 stop，那么超时只是“等得比预期久”，join 会在稍后返回；
- 如果某个任务永远不会响应 stop，那么 join 会挂死。此时要么在外部用进程级监督者兜底，要么接受“硬杀”的代价，不能假装 join 能自己解决。

所以程序用 `stuck` 参数演示“超时日志出现，但任务最终还是退出”的正常教学路径；真正“永不退出”的线程不放进示例里跑，因为它只会让示例挂住，学不到更多东西。

### 步骤 3：工作循环用 condition_variable_any + stop_token

```cpp
std::unique_lock lk(s.queue_mu);
s.queue_cv.wait(lk, st, [&] {
    return !s.queue.empty() || !s.accepting.load() || st.stop_requested();
});

if (st.stop_requested()) {
    break;
}
```

这里把第 67 课的条件变量谓词和本课的 stop_token 接上了：

- 队列非空 → 有活干；
- `accepting == false` → 不再接新活，队列空了就退出；
- `st.stop_requested()` → 拥有者要求停止，立即退出。

`wait` 的 stop_token 重载保证：`request_stop()` 之后，即使没人 `notify`，等待者也会返回（然后重新检查谓词）。这就是“停机时工作线程不会永远睡在条件变量上”的关键。

### 步骤 4：停机序列

```cpp
// 1. 停止接入
state.accepting.store(false, std::memory_order_relaxed);
state.queue_cv.notify_all();

// 给已经排队的请求一个很短的“排空窗口”（可选，取决于策略）
std::this_thread::sleep_for(20ms);

// 2. 请求取消
group.request_stop();

// 3. 带超时等待
bool all_done = group.join_all(200ms);

// 4. 释放共享资源
state.downstream_name.clear();
```

第 1 步和第 2 步之间是否要留“排空窗口”，是一个策略问题：

- 想“优雅停机”，就让已接入的请求尽量处理完，再取消；
- 想“快速停机”，就立即取消，队列里没开始的请求记为 `Cancelled`。

本实验两者都展示：先停接入，短暂排空，然后取消；join 之后把队列里剩下的请求也记为 `Cancelled`，保证每个被接入的请求都有一个明确结果。

### 完整代码

```cpp
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <mutex>
#include <stop_token>
#include <string>
#include <thread>
#include <utility>
#include <vector>

using namespace std::chrono_literals;

enum class Result { Ok, Cancelled, Error };

const char* to_string(Result r) {
    switch (r) {
        case Result::Ok:        return "Ok";
        case Result::Cancelled: return "Cancelled";
        case Result::Error:     return "Error";
    }
    return "?";
}

struct Record {
    int id;
    Result result;
};

struct SharedState {
    std::atomic<bool> accepting{true};

    std::mutex queue_mu;
    std::condition_variable_any queue_cv;
    std::deque<int> queue;

    std::mutex log_mu;
    std::vector<Record> log;

    std::atomic<int> ok{0};
    std::atomic<int> cancelled{0};
    std::atomic<int> error{0};
    std::atomic<int> total{0};

    std::string downstream_name = "article-downstream-conn";

    void record(int id, Result r) {
        switch (r) {
            case Result::Ok:        ok.fetch_add(1, std::memory_order_relaxed); break;
            case Result::Cancelled: cancelled.fetch_add(1, std::memory_order_relaxed); break;
            case Result::Error:     error.fetch_add(1, std::memory_order_relaxed); break;
        }
        total.fetch_add(1, std::memory_order_relaxed);
        std::lock_guard lk(log_mu);
        log.push_back(Record{id, r});
    }
};

class TaskGroup {
public:
    TaskGroup() = default;
    TaskGroup(const TaskGroup&) = delete;
    TaskGroup& operator=(const TaskGroup&) = delete;

    ~TaskGroup() {
        request_stop();
        join_all(500ms);
    }

    std::stop_token token() const { return source_.get_token(); }

    bool request_stop() { return source_.request_stop(); }

    template <class F>
    void spawn(F&& f) {
        {
            std::lock_guard lk(mu_);
            ++running_;
        }
        threads_.emplace_back(
            // jthread 会传入它自己的 token；这里忽略它，统一使用组 token，
            // 这样 TaskGroup::request_stop() 能一次通知组内所有任务。
            [this, fn = std::forward<F>(f)](std::stop_token) mutable {
                fn(source_.get_token());
                {
                    std::lock_guard lk(mu_);
                    --running_;
                }
                cv_.notify_all();
            });
    }

    bool join_all(std::chrono::milliseconds timeout) {
        std::unique_lock lk(mu_);
        bool done = cv_.wait_for(lk, timeout, [this] { return running_ == 0; });
        if (!done) {
            std::printf("[shutdown] hard-timeout: %d task(s) still running\n",
                        running_);
        }
        lk.unlock();

        for (auto& t : threads_) {
            if (t.joinable()) {
                t.join();
            }
        }
        threads_.clear();
        return done;
    }

private:
    std::stop_source source_;
    std::mutex mu_;
    std::condition_variable cv_;
    int running_ = 0;
    std::vector<std::jthread> threads_;
};

Result handle_request(int id, int worker_id, SharedState& s, std::stop_token st) {
    // 模拟访问共享下游连接。停机时必须保证：本函数返回之后，
    // 才允许 clear / destroy downstream_name。如果顺序反了，这里会读到空名字。
    if (s.downstream_name.empty()) {
        std::printf("[worker %d] request %d: downstream already released\n",
                    worker_id, id);
        return Result::Error;
    }
    const std::string& downstream = s.downstream_name;
    (void)downstream;   // 教学：持有引用期间对象必须仍然有效

    const int slices = 1 + (id % 4);
    for (int i = 0; i < slices; ++i) {
        if (st.stop_requested()) {
            return Result::Cancelled;
        }
        std::this_thread::sleep_for(5ms);
    }

    if (st.stop_requested()) {
        return Result::Cancelled;
    }

    if (id % 11 == 0) {
        std::printf("[worker %d] request %d: downstream business error\n",
                    worker_id, id);
        return Result::Error;
    }

    if (id % 7 == 0) {
        for (int i = 0; i < 30; ++i) {
            if (st.stop_requested()) {
                return Result::Cancelled;
            }
            std::this_thread::sleep_for(10ms);
        }
    }

    if (st.stop_requested()) {
        return Result::Cancelled;
    }
    return Result::Ok;
}

void worker_loop(int worker_id, SharedState& s, std::stop_token st) {
    while (true) {
        int id = -1;
        {
            std::unique_lock lk(s.queue_mu);
            s.queue_cv.wait(lk, st, [&] {
                return !s.queue.empty() || !s.accepting.load() || st.stop_requested();
            });

            if (st.stop_requested()) {
                break;
            }
            if (s.queue.empty()) {
                if (!s.accepting.load()) {
                    break;
                }
                continue;
            }
            id = s.queue.front();
            s.queue.pop_front();
        }

        Result r = handle_request(id, worker_id, s, st);
        s.record(id, r);
        std::printf("[worker %d] request %d -> %s\n", worker_id, id, to_string(r));
    }
    std::printf("[worker %d] exit\n", worker_id);
}

int main(int argc, char** argv) {
    const bool simulate_stuck =
        (argc > 1 && std::string(argv[1]) == "stuck");

    // 声明顺序即析构顺序的相反：后声明的 group 先析构。
    // ~TaskGroup 会 request_stop + join，保证工作线程退出后，
    // 才轮到 ~SharedState 释放 queue / downstream_name。
    // 如果把这两行对调，析构时就会先毁掉共享状态再 join，正是节点 why 的反面。
    SharedState state;
    TaskGroup group;

    std::printf("=== service start ===\n");
    for (int i = 0; i < 3; ++i) {
        group.spawn([&state, i](std::stop_token st) {
            worker_loop(i, state, st);
        });
    }

    if (simulate_stuck) {
        group.spawn([](std::stop_token st) {
            std::printf("[stuck] enter non-interruptible sleep (ignores stop)\n");
            std::this_thread::sleep_for(800ms);
            std::printf("[stuck] woke up, stop_requested=%d\n",
                        static_cast<int>(st.stop_requested()));
        });
    }

    for (int id = 1; id <= 20; ++id) {
        {
            std::lock_guard lk(state.queue_mu);
            state.queue.push_back(id);
        }
        state.queue_cv.notify_one();
        std::this_thread::sleep_for(2ms);
    }

    std::this_thread::sleep_for(120ms);

    std::printf("\n=== shutdown begin ===\n");

    std::printf("[shutdown 1/4] stop accepting new requests\n");
    state.accepting.store(false, std::memory_order_relaxed);
    state.queue_cv.notify_all();

    std::this_thread::sleep_for(20ms);

    std::printf("[shutdown 2/4] request_stop -> cancel in-flight tasks\n");
    group.request_stop();

    std::printf("[shutdown 3/4] join_all with deadline\n");
    const bool all_done = group.join_all(200ms);
    std::printf("[shutdown 3/4] result: %s\n",
                all_done ? "all tasks exited within deadline"
                         : "hard timeout, log emitted");

    {
        std::lock_guard lk(state.queue_mu);
        for (int id : state.queue) {
            state.record(id, Result::Cancelled);
            std::printf("[shutdown] queued request %d never started -> Cancelled\n",
                        id);
        }
        state.queue.clear();
    }

    std::printf("[shutdown 4/4] release shared resources: %s\n",
                state.downstream_name.c_str());
    state.downstream_name.clear();

    const int ok = state.ok.load();
    const int cancelled = state.cancelled.load();
    const int error = state.error.load();
    const int total = state.total.load();

    std::printf("\n=== result ===\n");
    std::printf("ok=%d cancelled=%d error=%d total=%d\n",
                ok, cancelled, error, total);

    assert(total == 20);
    assert(ok + cancelled + error == total);
    assert(cancelled > 0);

    int cancelled_in_log = 0;
    int ok_in_log = 0;
    for (const Record& r : state.log) {
        if (r.result == Result::Cancelled) ++cancelled_in_log;
        if (r.result == Result::Ok) ++ok_in_log;
    }
    assert(cancelled_in_log == cancelled);
    assert(ok_in_log == ok);
    std::printf("assert: cancelled tasks are not counted as Ok\n");

    return 0;
}
```

### 编译与运行

```bash
g++ -std=c++20 -pthread -O2 -o lesson78 lesson78.cpp
./lesson78
./lesson78 stuck     # 额外启动一个不响应 stop 的阻塞任务，观察超时日志
```

编译命令没有定义 `NDEBUG`，所以代码里的 `assert` 是生效的；`-O2` 不会关掉断言。跑 `./lesson78 stuck` 时，可另开一个终端用 `ps -T -p $(pgrep -n lesson78)` 观察：`request_stop` 之后普通工作线程应消失，那个故意不响应 stop 的线程会一直在，直到 800ms 睡眠结束。

### 输出解读：把 acceptance 变成可检查事实

正常运行时，结果区会打印类似：

```text
=== result ===
ok=... cancelled=... error=... total=20
assert: cancelled tasks are not counted as Ok
```

不要纠结具体数字，它们取决于调度和停机时机。要看的是四个结构性事实：

1. `total == 20`：20 个接入的请求，每个都有且只有一个结果；
2. `ok + cancelled + error == total`：没有请求被漏账，也没有被重复计数；
3. `cancelled > 0`：慢请求或尚未开始的队列项会被取消；
4. 日志计数与汇总计数一致：`Cancelled` 只出现在取消计数里，绝不会进入 `Ok`。

对应到 acceptance：

- **“取消不会被误报为业务成功”**：`Result` 三值互斥；`record` 的 `switch` 只有 `Result::Ok` 累加 `ok`；结束前的两个断言把“日志里的取消数”和“汇总的取消数”对齐。谁把 `!= Error` 当成成功，断言就会失败。
- **“所有任务有归属且停机有界”**：所有线程由 `TaskGroup::spawn` 创建并存入 `threads_`，代码中没有 `detach`；`join_all(200ms)` 给出等待预算，超时会打印 `hard-timeout`。正常路径下，线程在 `request_stop()` 后的一个检查周期内退出，join 在预算内完成。

`stuck` 模式下会多出一行：

```text
[shutdown] hard-timeout: 1 task(s) still running
```

它证明“等待预算被用完时留下了证据”。随后程序仍会等那个 800ms 的睡眠结束，再释放共享资源——这是示例为了正常退出而做的选择，也是真实系统里“硬超时策略”的占位点：日志之后，是继续等、进程级兜底，还是接受硬杀的代价，需要提前决定。

### 反例对照：先释放共享状态再 join 为什么是 UAF

```cpp
// 错误顺序：不要这样写
TaskGroup group;
SharedState state;
group.spawn([&state](std::stop_token st) {
    use_downstream(state.downstream_name, st);   // 可能阻塞很久
});

state.downstream_name.clear();   // 线程可能正在读它
state.accepting = false;
group.request_stop();            // 太晚：清理已经发生
group.join_all(200ms);
```

问题不在于 `clear()` 本身，而在于它发生在工作线程退出之前。工作线程可能正拿着 `downstream_name` 的引用做下游调用；`clear()` 之后字符串内容变了，内部缓冲可能释放，继续读就是 UAF 或读到空串。如果共享对象是连接，后果就是访问已销毁的 socket / 连接状态。

正确方向只有一个：**先让所有使用者退出，再销毁被使用的东西。** 这正是“停止接入 → 取消 → join → 释放”的全部理由。

### 如果任务真的永不退出

教学代码里，`stuck` 任务睡 800ms 后会自己醒来。现实中可能遇到下游 SDK 的 `recv` 没有超时、第三方库取消 API 不完整、任务死锁等情况。此时 `join_all` 的超时日志只能告诉你“还有任务没退出”，不能替它们退出。可选策略：

1. **修阻塞点**：加 socket 超时、改成 `poll` / `epoll` + eventfd、使用可取消等待；
2. **进程级兜底**：停机超时后由外部监督者（容器、systemd、K8s）发送 SIGKILL，接受整个进程重启的代价；
3. **隔离**：把不可控的第三方调用放到独立进程，超时后放弃那个进程，而不是杀线程。

第三种本质上是“用进程边界换线程安全”，因为进程可以被强杀而线程不行。这也从反面说明：合作式取消是 C++ 里的默认答案，而“停机有界”最终是一个系统级问题，不只是 `join` 的返回值问题。

---

## sourceStudy：读 cppreference 的 jthread 页面

本节点的 sourceStudy 要求：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。资源只有一条：

> https://en.cppreference.com/w/cpp/thread/jthread.html

阅读时不要只扫一眼类声明，建议分三遍：

### 第一遍：确认“jthread 比 thread 多了什么”

在页面里找到 `std::jthread` 的成员函数列表，重点核对：

- 构造函数：当可调用对象的第一个参数可以接收 `std::stop_token` 时，jthread 会把自己的停止 token 传进去；
- 析构函数：等价于先 `request_stop()`，再 `join()`；
- `get_stop_source()`：返回内部的 `stop_source`；
- `get_stop_token()`：返回对应的 `stop_token`；
- `request_stop()`：请求停止，返回 `bool`，表示这次调用是否首次完成“未请求 → 已请求”的转换；
- `join()` / `joinable()` / `detach()`：与 `std::thread` 类似。

把这些和本课要点一对齐：**jthread 的“自动停 + 自动等”只发生在析构函数里，而且“停”是请求，“等”是无界的。** 这两句话决定了它不能替代完整的停机设计。

### 第二遍：顺着 stop_token / stop_source / stop_callback 的链接走

页面会把这三个类型分开讲。核对它们的角色：

- `stop_source` 拥有共享停止状态，`request_stop()` 是控制端；
- `stop_token` 是只读观察端，`stop_requested()` / `stop_possible()` 是查询；
- `stop_callback` 把回调注册到某个 token 上；停止被请求时同步执行；构造时若已经请求，则立即执行。

再对照本课要点一：jthread 的析构为什么能“通知到”线程体？因为线程体拿到的是 jthread 内部 `stop_source` 的 token；但这只是“通知”，线程体不检查、不退出，join 就仍然会等。

### 第三遍：划出版本与边界

读页面时把下面这些边界记在笔记里，不要把它们和“强制终止”混为一谈：

- **版本**：`std::jthread`、`std::stop_token`、`std::stop_source`、`std::stop_callback` 都是 C++20 起可用。C++20 之前没有标准库级别的这套协作式取消工具。
- **析构会阻塞**：jthread 析构的 join 会阻塞到线程函数返回；如果线程卡在不响应 stop 的阻塞调用里，析构就会一直等。
- **stop 只是请求**：`request_stop()` 不会中断 `recv` / `read` / `pthread_cond_wait` / `mutex::lock` / `sleep_for`；它只改共享停止状态，并同步执行已注册的 `stop_callback`。
- **token 可查询但不可请求**：`stop_token` 没有 `request_stop()`；要请求停止，必须持有 `stop_source` 或 jthread 对象。
- **回调是同步的**：`stop_callback` 在触发它的线程上执行；不要在回调里做长时间阻塞操作。

页面上没有展开的内容——例如“停机顺序应该怎么排”“超时后应该硬杀还是进程级兜底”“为什么取消不能算业务成功”——以本课讲解和实践为准，不要把它们说成页面结论。页面的价值是核对标准语义，不是替你设计业务停机策略。

---

## 自测题

先别看答案。能对着要点一的三角图、要点二的等待循环、要点三的四步停机顺序讲清楚，才算过关。

### 题 1：jthread 的析构到底做了什么？

**问题**：下面这段代码里，`worker` 的线程体从不检查 `stop_token`，只做一次 2 秒的 `sleep_for`。主线程在 100ms 时调用 `worker.request_stop()`，然后立刻离开作用域。请回答：

1. `request_stop()` 返回时，线程体是否已经结束？
2. 离开作用域时，程序会等多久？
3. 如果把 `std::jthread` 换成 `std::thread` 并 `detach()`，行为有什么不同？风险是什么？

**答案**：

1. 没有。`request_stop()` 只是把共享停止状态置为“已请求”，并同步执行已注册的 `stop_callback`；它不会打断 `sleep_for`。线程体没有检查 token，所以它会继续睡完 2 秒。
2. 离开作用域时，`jthread` 的析构函数会先再次 `request_stop()`（返回 `false`，因为已经请求过），然后 `join()`。由于线程体不响应 stop，`join()` 会一直等到 2 秒的睡眠结束、线程函数返回。也就是说，析构会阻塞接近剩余的睡眠时间（按 2 秒减去已经过的 100ms 估算）；如果线程是永久阻塞，析构就永久阻塞。
3. 换成 `std::thread` + `detach()`，主线程离开作用域时不会等，程序可能继续往下走甚至退出；后台线程在进程退出时可能被强行终止，析构函数不保证执行，捕获的引用/指针可能已经悬空。`detach` 把“等待和生命周期”的责任删掉了，不会让问题消失。

### 题 2：为什么 request_stop 唤醒不了 recv / read / pthread_cond_wait？

**问题**：一个工作线程阻塞在 `recv(fd, buf, len, 0)` 上，另一个线程调用 `src.request_stop()`。请解释为什么 `recv` 不会返回，并给出至少两种让这个线程“可取消”的工程做法。

**答案**：

`stop_token` 是用户态的协作式标志，不是内核级线程终止原语，也不是异步信号。`request_stop()` 做两件事：把共享停止状态置为已请求；同步执行已注册的 `stop_callback`。它不会进入内核去修改正在阻塞的 `recv` 的等待条件。`recv` 只有在数据到达、连接关闭/出错、socket 超时、或被信号打断等情况下才会返回；停止标志的变化不在其中。同理，`read`、`pthread_cond_wait`、`mutex::lock`、`sleep_for` 也不会自动因 stop 而退出。

可取消的做法包括：

- 给 socket 设置接收超时（`SO_RCVTIMEO`），让 `recv` 周期性返回，返回后检查 `stop_requested()`；
- 用 `poll` / `epoll_wait` 并设置超时，或者把 eventfd / pipe 加入等待集合，在 `stop_callback` 里往 eventfd 写一个字节唤醒事件循环；
- 需要让阻塞的 `recv` 返回时，调用 `shutdown(fd, SHUT_RDWR)`（注意跨平台行为差异，Linux 上通常比直接 `close` 更可控）；
- 对条件变量等待使用 `condition_variable_any` 的 stop_token 重载，或注册 `stop_callback` 去 `notify_all`；
- 把长睡眠切成小段，每段之间检查 stop。

核心是：**每个阻塞点都要有自己的退出设计**，不能指望 `request_stop()` 一刀切。

### 题 3：停机顺序与“取消不是成功”

**问题**：某服务的停机函数这样写：先 `conn.reset()` 释放下游连接，再 `group.request_stop()`，最后 `group.join_all()`。请指出至少两个问题；并说明为什么即使所有任务都“正常结束”，也不能把被取消的请求计入成功。

**答案**：

问题一：**释放顺序错误**。`conn.reset()` 发生在工作线程退出之前，工作线程可能正持有连接对象的引用/裸指针，继续访问就是 use-after-free。正确顺序是停止接入 → 请求取消 → 带超时 join → 最后释放共享资源。

问题二：**停机可能无界**。`join_all()` 如果没有超时预算，遇到不响应 stop 的阻塞调用会一直等；如果 `join_all()` 有超时但超时后仍然无条件 `join()`，那只是把“无界”推迟到了 join 内部，需要明确的硬超时策略和日志。

问题三（如果统计写成 `if (result != Error) ++success`）：**取消被误报为成功**。取消是“拥有者不要结果了”，业务并没有完成；把它计入成功会污染成功率、掩盖真实的失败，还可能让上游以为请求已经处理。正确做法是 `Ok / Cancelled / Error` 三值分开统计：只有 `Ok` 是成功，`Cancelled` 单独计数，`Error` 才是业务失败。

即使任务“正常结束”，也要看它是以什么结果结束的：`Cancelled` 代表没有完成业务，不是成功。验收要求“取消不会被误报为业务成功”，说的就是这条语义纪律。

---

## 推荐资源

- **结构化并发、取消与停机：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/thread/jthread.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：深入
  - 是否免费：是
  - 推荐理由：按节点建议，重点阅读 `jthread` 与 `stop_token`、取消传播和截止时间，用于核对本节点实验的语义和边界。阅读时抓住：构造函数如何把 `stop_token` 传给可调用对象、析构等价于 `request_stop()` + `join()`、`request_stop()` / `get_stop_token()` / `get_stop_source()` 的关系，以及 `stop_token` / `stop_source` / `stop_callback` 三者的角色。记下版本边界：C++20 起可用；析构的 join 会阻塞到线程函数返回；stop 只是请求，不会中断任意阻塞系统调用。页面没有展开的停机顺序、超时策略和业务结果分类，以本课讲解与实践为准。

---

## 本课小结

1. **拥有者负责整条生命周期。** 任务的拥有者应管理其启动、取消、等待和资源释放；起线程不是“丢出去就不管”的副作用。
2. **结构化并发的底线是 join。** 父作用域在离开前等待子任务结束；`detach` 把等待、取消、生命周期三个问题一起删掉，留下的是 UAF 和退出时的数据丢失。
3. **jthread = 自动 request_stop + join，三角分工明确。** C++20 的 `std::jthread` 析构先请求停止再等待；可调用对象第一个参数能接收 `stop_token` 时，jthread 会传入自己的 token。`stop_source` 是控制端，`stop_token` 是只读观察端，`stop_callback` 在停止被请求时同步执行（构造时已停止则立即执行）。
4. **取消是合作式请求，不是强制终止。** 硬杀线程会留下未释放的锁、半截数据结构、未走完的析构；C++ 选择让工作线程在安全点自己退出。
5. **取消不会自动传播。** 父 token 需要通过共享 `stop_source` 或 `stop_callback` 桥接到子任务组；一次请求是一个取消域，拥有者要明确。
6. **截止时间和取消正交。** deadline 回答“还能活多久”，取消回答“还要不要你继续”；超时通常触发取消，但结果要分别记为 `Timeout` / `Cancelled`，不能混成成功。
7. **可取消等待要用对工具。** `condition_variable_any` 的 `wait` / `wait_until` / `wait_for` 可以接 `stop_token`；普通 `condition_variable` 不接收。等待必须放在循环里重新检查谓词、stop 和 deadline。
8. **停机顺序是四步。** 停止接入 → `request_stop()` → 带超时 join → 释放共享资源；反序（先释放再 join）正是节点 why 的反面。
9. **停机有界需要工程手段。** jthread 析构的 join 无界；要在等待侧加预算、在阻塞点加退出路径、在超时后打日志并决定硬超时策略。硬杀不是免费的。
10. **pitfalls 与 tradeoffs 是一体两面。** stop 请求不会中断 `recv` / `read` / `pthread_cond_wait` / `mutex::lock` / `sleep_for`；合作式取消能保持不变量，代价是所有阻塞点都要设计退出路径：周期检查、`stop_callback` 唤醒、可取消等待，缺一不可。
11. **结果分类与验收。** `Ok` / `Cancelled` / `Error` 三值互斥，只有 `Ok` 计成功；取消单独统计，既不算成功，也不混进业务错误率。验收靠断言落地：枚举与统计分支证明取消不被误报为成功，`TaskGroup` 持有全部 jthread、没有 detach、join 带超时并打日志证明有归属且停机有界。

本课的主线可以压缩成一句话：**先让拥有者接管线程，再让取消变成可传播的请求，最后用正确的停机顺序把“资源释放”排在“所有使用者退出”之后。** 下一步可以继续沿着任务生命周期往下走：超时预算如何在多层调用间分配、取消如何与重试策略配合、以及服务优雅停机时如何把“排空窗口”做成可观测的指标。
