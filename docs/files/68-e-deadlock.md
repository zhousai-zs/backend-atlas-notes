# 第 68/208 课：死锁、锁顺序与阻塞传播

## 1. 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 并发与内存模型（同步与正确性） |
| 前置课程 | `e-lock`（互斥锁与临界区基础） |
| 难度 | 进阶 |
| 领域 | E |
| 子分类 | 同步与正确性 |
| 标签 | Deadlocks、E、C++ |
| 节点 ID | `e-deadlock` |
| 英文名 | Deadlocks |

本课所处的位置很关键：上一课我们学了条件变量与状态谓词，也复用了前置互斥锁的知识。到目前为止，你已经能让一个临界区“正确”地保护共享状态。但从这一课开始，我们要面对一个更阴险的问题——**即使每一把锁单独看都用对了，把它们组合起来仍然可能让整个系统停摆**。这就是死锁（deadlock）以及它带来的阻塞传播（blocking propagation）。

---

## 2. 导读

### 2.1 承接上一课

在 `e-lock` 里，我们建立了几个基本认知：

- 互斥锁保护临界区，保证同一时刻只有一个线程修改共享状态；
- 锁必须成对获取与释放，通常用 RAII（`std::lock_guard`、`std::unique_lock`）保证异常安全；
- 条件变量必须和互斥锁配合，用状态谓词（predicate）判断“能不能继续”，避免虚假唤醒；
- 持锁时间应尽量短，避免在临界区里做慢操作。

上一课的条件变量还教给我们一个更深的道理：**线程的“等待”本身是有结构的**——它在等某个谓词变为真，而谓词为真依赖于别的线程推进。如果“别的线程推进”又反过来依赖这个正在等待的线程，等待就会形成闭环，系统就再也走不动了。

### 2.2 本课要讲什么

本课的核心结论可以先用一句话概括：

> **死锁由资源依赖环等条件导致；固定锁顺序与减少持锁跨模块调用，是两道最有效的预防闸门。**

围绕这句话，我们要展开三个技术要点：

1. **等待图和循环依赖**：如何把“谁在等谁”画成一张有向图，如何用死锁的四个必要条件判断风险，如何理解“图里出现环 = 死锁”。
2. **多锁获取与 `scoped_lock`**：当一次操作需要同时持有两把（或多把）锁时，如何用固定的全局锁序、按地址排序、以及 `std::lock` / `std::scoped_lock` 所提供的死锁避免算法来保证安全。经典例子是转账。
3. **持锁调用、回调和优先级反转**：为什么“持锁时调用外部模块、回调、日志、RPC”会制造隐式的锁依赖和阻塞传播；以及优先级反转、优先级继承这些实时系统里必须懂的概念，如何映射到后台服务。

### 2.3 后台场景：线上到底长什么样

把这些概念放到腾讯微信公众号后台这类服务里，死锁的典型表现是：

- **worker 线程全部卡住**：线程池里的线程一个个进入阻塞，监控上看到活跃线程数骤降、队列堆积、CPU 反而空闲。
- **请求链路阻塞**：一个请求在 A 模块持锁后调用 B 模块，B 模块的回调又要拿 A 模块的锁，形成跨模块的环形等待；上游请求超时，下游连接池被占满，故障沿调用链放大。
- **两个服务模块互相持锁回调**：模块 1 持有锁 L1 后同步调用模块 2，模块 2 在持锁 L2 的情况下回调模块 1，而模块 1 的回调入口需要 L1 或模块 2 需要 L1——环就闭合了。

死锁最可怕的地方在于它**不是崩溃**。进程还活着，端口还在监听，健康检查可能还返回 200，但业务已经不动了。所以“能看懂线程栈、能定位锁归属”不是加分项，而是后端工程师的基本生存技能。

---

## 3. 学习目标

### 3.1 Goals（学完你能做到）

- 能解释等待图和循环依赖：给定一组线程与锁的获取/等待关系，能画出等待图并指出环在哪里。
- 能独立完成：构造双锁死锁，用 gdb 线程栈识别等待环并修复。
- 能说清死锁的四个必要条件，并针对每个条件给出至少一种工程上的破坏手段。
- 能在需要同时获取多把锁时，正确使用固定锁序或 `std::scoped_lock`，并解释它们为什么能避免死锁。
- 能识别“持锁调用外部模块/回调/日志/RPC”带来的隐式锁依赖和阻塞传播风险。
- 能解释优先级反转的成因，以及优先级继承/优先级天花板的基本思路。

### 3.2 Acceptance（验收标准）

- **能用栈与锁归属证明死锁**：不是“我觉得它卡住了”，而是能从 `gdb` 的 `thread apply all bt` 里读出“线程 A 等锁 m2、线程 B 等锁 m1”，并结合 mutex 的 `__owner` 字段指出“m2 的持有者正是线程 B、m1 的持有者正是线程 A”。
- **修复后压力与停机测试通过**：修复不是“加个 sleep 看看”，而是在并发压力下长时间运行无卡死，并且服务能正常优雅停机（stop/shutdown），不会因为线程卡在锁上而无法退出。
- **能说明源码与边界**：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件（例如 `scoped_lock` 的 C++ 版本要求、锁的可重入性、`try_lock` 的语义等）。

---

## 4. 为什么后端要懂

很多同学觉得死锁是操作系统课本里的东西，离业务很远。实际上，在后端服务里，死锁是**线上事故的常见根因之一**，而且它的排查成本很高。

### 4.1 排查线上线程卡住

当服务“假死”时，最常见的现象是：

- 请求延迟飙升，QPS 掉到接近 0；
- 线程池活跃线程数不变但都没进展，队列持续增长；
- CPU 占用不高（因为线程都在睡眠等锁，而不是在忙）；
- 重启后短时间恢复，过一段时间又复发。

如果你不会看线程栈，你只能猜：是下游慢？是 GC？是网络？而一旦你会看栈，死锁的现场其实非常“整齐”：多个线程停在 `pthread_mutex_lock` / `__lll_lock_wait` 上，且互相等待。这时候你能在几分钟内锁定问题，而不是靠重启碰运气。

### 4.2 排查请求链路阻塞

后台系统往往是多模块、多层次的：接入层、逻辑层、存储访问层、缓存层。锁可能分布在不同模块里。一个请求可能依次获取多把锁，或者持锁调用另一个模块。只要跨模块的调用关系里存在反向的锁获取路径，就可能出现**阻塞传播**：一个点的锁等待，沿着调用链向上游扩散，最终拖垮整个请求链路和连接池。

理解阻塞传播，你才能回答这些问题：

- 为什么一个“只读”接口会被一个“写”接口拖死？
- 为什么连接池耗尽后，连健康检查都失败？
- 为什么加了超时，问题从“卡死”变成了“疯狂重试 + CPU 打满”（活锁）？

### 4.3 设计阶段就能防

更重要的是，死锁是**可以在设计阶段预防**的。统一锁序、减少持锁跨模块调用、用消息化/异步化替代同步回调，这些设计决策的成本远低于线上止血。本课的目的，就是让你在做模块设计时，脑子里自动浮现“这里会不会形成等待环”。

---

## 5. 直觉 → 原理

### 5.1 要点一：等待图和循环依赖

#### 5.1.1 死锁的四个必要条件

经典理论给出死锁发生的四个必要条件（Coffman 条件）：

1. **互斥（Mutual Exclusion）**：资源同一时刻只能被一个线程占用。互斥锁天然满足。
2. **持有并等待（Hold and Wait）**：线程持有至少一个资源，同时又在等待其他资源。
3. **不可剥夺（No Preemption）**：资源不能被强行夺走，只能由持有者主动释放。
4. **循环等待（Circular Wait）**：存在一个线程的环形等待链：T1 等 T2 持有的资源，T2 等 T3 持有的资源，……，Tn 等 T1 持有的资源。

四个条件**同时满足**才可能死锁。工程上的预防思路，就是破坏其中至少一个：

- 破坏“持有并等待”：一次性申请所有资源（`std::scoped_lock` 的多锁版本就是一次性尝试），或者申请不到就释放已持有的。
- 破坏“不可剥夺”：使用带超时的 `try_lock_for`，超时后释放并回退（但要注意活锁，见第 6 节）。
- 破坏“循环等待”：**固定全局锁序**，让所有线程按同一顺序获取锁。这是最常用、最有效的办法。

> 注意：这里的“必要”是必要条件，不是充分条件。满足四个条件不一定马上死锁，但只要有环，就有死锁风险。

#### 5.1.2 等待图（Wait-for Graph）怎么画

等待图是一个有向图：

- **节点**：线程（也可以扩展到进程、事务）。
- **有向边**：如果线程 A 正在等待线程 B 持有的资源，就画一条 `A → B` 的边。

规则很简单：

- 图中**没有环** → 当前不存在死锁。
- 图中**出现环** → 存在死锁（对于单实例资源，环即死锁；对于多实例资源，环是必要条件，需要进一步做归约判断，但工程上绝大多数互斥锁都是单实例，环就等于死锁）。

举例，两个线程两把锁：

```
线程 T1: lock(m1); lock(m2); ...
线程 T2: lock(m2); lock(m1); ...
```

可能出现的时序：

```
T1: lock(m1) 成功
T2: lock(m2) 成功
T1: lock(m2) 阻塞  —— 等 T2 释放 m2
T2: lock(m1) 阻塞  —— 等 T1 释放 m1
```

等待图：

```
   T1 ──────→ T2
    ↑          │
    └──────────┘
```

这是一个 2 个节点的环，死锁成立。

把等待图落到 gdb 里，就是：每个线程的栈顶停在 `pthread_mutex_lock` / `__lll_lock_wait`，然后我们要找出“它等的这把锁现在被谁持有”。下一节会给出具体命令。

#### 5.1.3 一个最小的双锁死锁程序

先看一个能稳定复现死锁的 C++ 例子。它故意用“相反的锁序”，并用一个 barrier 让两个线程尽量同时进入。

```cpp
// deadlock_demo.cpp
// 编译: g++ -std=c++17 -g -pthread deadlock_demo.cpp -o deadlock_demo
#include <iostream>
#include <mutex>
#include <thread>
#include <chrono>

std::mutex m1;
std::mutex m2;

void worker_a() {
    std::cout << "[A] try lock m1\n";
    std::lock_guard<std::mutex> g1(m1);
    std::cout << "[A] got m1, sleep\n";
    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    std::cout << "[A] try lock m2\n";
    std::lock_guard<std::mutex> g2(m2);   // 等 m2
    std::cout << "[A] got m2\n";
}

void worker_b() {
    std::cout << "[B] try lock m2\n";
    std::lock_guard<std::mutex> g2(m2);
    std::cout << "[B] got m2, sleep\n";
    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    std::cout << "[B] try lock m1\n";
    std::lock_guard<std::mutex> g1(m1);   // 等 m1
    std::cout << "[B] got m1\n";
}

int main() {
    std::thread ta(worker_a);
    std::thread tb(worker_b);
    ta.join();
    tb.join();
    std::cout << "done\n";
    return 0;
}
```

这个程序在多数情况下会永久挂住：A 持有 m1 等 m2，B 持有 m2 等 m1。

#### 5.1.4 用 gdb 现场取证

假设程序已经卡住，先拿到 PID（例如 `pgrep deadlock_demo`），然后：

```bash
# 附加到运行中的进程
gdb -p <pid>

# 在 gdb 交互界面里
(gdb) info threads
(gdb) thread apply all bt
```

你会在栈里看到类似这样的帧：

```
Thread 2 (LWP 12346):
#0  __lll_lock_wait () at ../sysdeps/unix/sysv/linux/x86_64/lowlevellock.S
#1  __GI___pthread_mutex_lock (mutex=0x... ) at pthread_mutex_lock.c:...
#2  worker_b () at deadlock_demo.cpp:29        <-- 等 m1
...

Thread 3 (LWP 12347):
#0  __lll_lock_wait () at ../sysdeps/unix/sysv/linux/x86_64/lowlevellock.S
#1  __GI___pthread_mutex_lock (mutex=0x... ) at pthread_mutex_lock.c:...
#2  worker_a () at deadlock_demo.cpp:18        <-- 等 m2
...
```

关键信息是 `pthread_mutex_lock` 的 `mutex=` 参数地址，以及业务函数停在哪一行。`__lll_lock_wait` 是 glibc 拿锁失败后在 futex 上睡眠等待的函数，看到它基本就说明线程在等一把用户态互斥锁。

接下来要回答：“它等的这把锁，现在被谁持有？” 对于 glibc 的 `pthread_mutex_t`，内部有 `__data.__owner` 字段，保存持有者的 TID（对普通互斥锁而言）。`std::mutex` 在 libstdc++ 里把它放在成员 `_M_mutex` 中，可以这样打印（字段名依赖 glibc/libstdc++ 实现，不是标准接口）：

```gdb
# std::mutex（libstdc++）内部包着一个 pthread_mutex_t，成员名是 _M_mutex
(gdb) p m1._M_mutex.__data.__owner
$1 = 12347        # 这个 TID 对应 worker_a 所在的线程

(gdb) p m2._M_mutex.__data.__owner
$2 = 12346        # 这个 TID 对应 worker_b 所在的线程
```

如果 gdb 里拿不到具体变量符号（比如在别的栈帧或优化过），可以把栈里看到的地址强转成 `pthread_mutex_t*` 再打印：

```gdb
(gdb) p *(pthread_mutex_t*)0x5555557690a0
```

输出里会包含 `__data.__owner` 的数值。也可以在已知变量时：

```gdb
(gdb) p *(pthread_mutex_t*)&m1
```

把 TID 和 `info threads` 里的 LWP 对应起来，你就能完成闭环证据：

- 线程 A（TID 12347）持有 `m1`，栈停在等 `m2`；
- 线程 B（TID 12346）持有 `m2`，栈停在等 `m1`；
- 等待图里 A→B、B→A，形成环 → 死锁。

有时候你不想用 gdb 交互，也可以直接用：

```bash
# 非交互式抓所有线程栈
gdb -p <pid> -batch -ex "thread apply all bt" > /tmp/bt.txt

# 或者用 pstack（很多发行版有）
pstack <pid>

# 或者用 elfutils 的 eu-stack
eu-stack -p <pid>
```

`pstack` 和 `eu-stack` 本质上也是采样线程栈，适合快速抓现场；需要看 mutex 的 `__owner` 时还是用 gdb 更灵活。

#### 5.1.5 TSan 的 lock-order-inversion 检测

ThreadSanitizer 除了查数据竞争，也能检测**锁序反转**（lock-order-inversion）。这属于运行时（动态）的“潜在死锁”检测：它记录程序运行中观察到的加锁顺序，如果发现 A 线程是 `m1→m2`、B 线程是 `m2→m1`，就会报 lock-order-inversion。

编译：

```bash
g++ -std=c++17 -g -pthread -fsanitize=thread deadlock_demo.cpp -o deadlock_demo_tsan
./deadlock_demo_tsan
```

可能看到类似这样的报告（不同版本措辞略有差异）：

```
WARNING: ThreadSanitizer: lock-order-inversion (potential deadlock)
  Cycle in lock order graph: M0 (0x...) => M1 (0x...) => M0
  ...
  Mutex M0 acquired here while holding mutex M1 in thread T2
  Mutex M0 previously acquired by the same thread here
```

注意两个边界：

- TSan 报的是“**潜在**死锁”，它基于观测到的锁序，可能对只在某些时序下才死锁的代码给出警告，也可能因为运行时没走到某条路径而漏报。所以它是辅助，不是证明。
- TSan 需要编译期插桩，运行开销和内存开销都很大，一般只在测试环境开；生产环境一般用采样栈 + 锁归属来取证。

### 5.2 要点二：多锁获取与 `scoped_lock`

#### 5.2.1 固定全局锁序

最直接地破坏“循环等待”的方法：给所有锁定义一个**全局一致的获取顺序**，任何线程都必须按这个顺序加锁，禁止逆序。

常见做法：

- 用锁的“层级编号”：L1 < L2 < L3，只允许低层→高层或相反的固定方向；
- 用锁对象的内存地址排序：先按 `std::less<const void*>` 比较地址，再依次加锁；
- 用业务语义定义顺序：先账户锁、再订单锁、再日志锁。

转账是最经典的例子：账户 A 转账户 B，如果线程 1 做 A→B 时先锁 A 再锁 B，线程 2 做 B→A 时先锁 B 再锁 A，就形成环。固定锁序后，两个方向都按“先小后大”的顺序加锁，环就不存在。

```cpp
// transfer_fixed_order.cpp
// 编译: g++ -std=c++17 -g -pthread transfer_fixed_order.cpp -o transfer_fixed_order
#include <functional>
#include <mutex>
#include <iostream>
#include <thread>
#include <vector>

struct Account {
    std::mutex mtx;
    long long balance;
    explicit Account(long long b) : balance(b) {}
};

// 固定锁序：按 Account 对象地址排序，先锁地址小的
void transfer(Account& from, Account& to, long long amount) {
    if (&from == &to) return;

    Account* first  = &from;
    Account* second = &to;
    if (std::less<Account*>{}(second, first)) {
        std::swap(first, second);
    }

    std::lock_guard<std::mutex> lk1(first->mtx);
    std::lock_guard<std::mutex> lk2(second->mtx);

    if (from.balance >= amount) {
        from.balance -= amount;
        to.balance   += amount;
    }
}

int main() {
    Account a(1000), b(1000);

    std::thread t1([&] {
        for (int i = 0; i < 100000; ++i) transfer(a, b, 1);
    });
    std::thread t2([&] {
        for (int i = 0; i < 100000; ++i) transfer(b, a, 1);
    });

    t1.join();
    t2.join();

    std::cout << "a=" << a.balance << " b=" << b.balance
              << " sum=" << (a.balance + b.balance) << "\n";
    return 0;
}
```

这段代码里，`transfer(a,b)` 和 `transfer(b,a)` 都会先锁地址较小的那个账户，所以两个线程的加锁顺序一致，不会形成环。`std::less<Account*>` 在这里保证了一个全序（对指针用 `std::less` 比直接用 `<` 更稳妥，标准保证 `std::less` 对指针给出全序）。

#### 5.2.2 `std::lock` 与 `std::scoped_lock`

固定锁序需要人工纪律，一旦某个调用点忘了排序，坑就埋下了。C++ 提供了算法级的死锁避免手段：

- `std::lock(l1, l2, ...)`：尝试同时锁住多个 `Lockable`，内部使用“尝试加锁 + 失败则释放已持有的再重试”的算法来避免死锁，保证要么全部锁住，要么一个都不锁（在正常返回时全部锁住）。
- `std::scoped_lock`（C++17）：RAII 包装，构造时对多个锁调用 `std::lock`，析构时逆序解锁。单锁时等价于 `lock_guard`。

转账用 `scoped_lock` 可以写成：

```cpp
// transfer_scoped_lock.cpp
// 编译: g++ -std=c++17 -g -pthread transfer_scoped_lock.cpp -o transfer_scoped_lock
#include <mutex>
#include <iostream>
#include <thread>

struct Account {
    std::mutex mtx;
    long long balance;
    explicit Account(long long b) : balance(b) {}
};

void transfer(Account& from, Account& to, long long amount) {
    if (&from == &to) return;

    // std::scoped_lock 内部用死锁避免算法锁住两把锁，无需关心顺序
    std::scoped_lock lock(from.mtx, to.mtx);

    if (from.balance >= amount) {
        from.balance -= amount;
        to.balance   += amount;
    }
}

int main() {
    Account a(1000), b(1000);

    std::thread t1([&] {
        for (int i = 0; i < 100000; ++i) transfer(a, b, 1);
    });
    std::thread t2([&] {
        for (int i = 0; i < 100000; ++i) transfer(b, a, 1);
    });

    t1.join();
    t2.join();

    std::cout << "a=" << a.balance << " b=" << b.balance
              << " sum=" << (a.balance + b.balance) << "\n";
    return 0;
}
```

`std::scoped_lock` 的死锁避免算法大致思想（不同实现细节不同，但语义一致）：先尝试锁第一把，成功后尝试锁后续锁；如果某次 `try_lock` 失败，就释放已经持有的锁，然后重试。这样不会出现“持有一把、永远等另一把”的环。它牺牲的是“可能重试”的代价，换来的是调用点不必手工排序。

编译与运行：

```bash
g++ -std=c++17 -g -pthread transfer_scoped_lock.cpp -o transfer_scoped_lock
./transfer_scoped_lock
# 预期输出类似: a=1000 b=1000 sum=2000
```

注意边界：

- `std::scoped_lock` 需要 **C++17**。C++11/14 里用 `std::lock` + `std::unique_lock` 的 `std::defer_lock` 组合。
- 它避免的是**这些锁之间**的锁序死锁；如果锁的获取还涉及别的资源、别的模块，算法管不到。
- 不要在同一线程里对**同一把不可重入锁**重复加锁（`std::mutex` 不可重入），那是另一类死锁（自死锁），`scoped_lock` 也救不了。
- 传参顺序仍然影响性能和公平性，只是不再影响正确性。

#### 5.2.3 一次性申请 vs 固定锁序 vs 算法避免

三种手段对比：

| 手段 | 破坏的条件 | 优点 | 代价 |
| --- | --- | --- | --- |
| 固定全局锁序 | 循环等待 | 无重试、可预测、易审计 | 需要人工治理，跨模块难统一 |
| `std::lock` / `scoped_lock` | 持有并等待 + 循环等待 | 调用点简单，库保证 | 可能重试，锁多时开销上升 |
| 超时 `try_lock_for` + 回退 | 不可剥夺（弱化） | 不会永久卡死 | 易变活锁，业务需可回滚 |

在实际系统里，通常是“固定锁序为主，`scoped_lock` 处理局部多锁，超时只作为兜底”。

### 5.3 要点三：持锁调用、回调和优先级反转

#### 5.3.1 持锁调用外部模块 → 隐式锁依赖

死锁不一定来自“显式地按相反顺序拿两把锁”。更隐蔽的情况是：**你在持锁时调用了外部模块，而外部模块内部会拿别的锁**。于是你的锁与外部模块的锁之间形成了一条你没写出来的依赖边。

反例：

```cpp
// bad_hold_lock_call.cpp
// 编译: g++ -std=c++17 -g -pthread bad_hold_lock_call.cpp -o bad_hold_lock_call
#include <chrono>
#include <mutex>
#include <iostream>
#include <string>
#include <thread>

std::mutex g_user_mtx;

// 外部模块（比如审计/日志/回调注册中心）内部有自己的锁
std::mutex g_audit_mtx;

void audit_record(const std::string& msg) {
    std::lock_guard<std::mutex> lk(g_audit_mtx);   // 外部模块的锁
    std::cout << "[audit] " << msg << "\n";
}

void update_user_profile() {
    std::lock_guard<std::mutex> lk(g_user_mtx);
    // 持锁调用外部模块：隐式建立了 g_user_mtx -> g_audit_mtx 的边
    audit_record("profile updated");
}

void export_audit_snapshot() {
    std::lock_guard<std::mutex> lk(g_audit_mtx);
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    // 外部模块在持锁时又回调了业务层，业务层需要 g_user_mtx
    std::lock_guard<std::mutex> lk2(g_user_mtx);
    std::cout << "[export] snapshot done\n";
}

int main() {
    std::thread t1(update_user_profile);
    std::thread t2(export_audit_snapshot);
    t1.join();
    t2.join();
    return 0;
}
```

这里线程 1 的路径是 `g_user_mtx → g_audit_mtx`，线程 2 的路径是 `g_audit_mtx → g_user_mtx`，环闭合。问题不在于哪一把锁用错了，而在于**持锁跨越了模块边界**，调用点看不到对方内部的锁。

预防原则：

- **锁内不做 I/O、RPC、日志落盘、回调**；把需要这些操作的数据先拷贝出来，释放锁后再做。
- 如果必须调用外部模块，就要把“外部模块可能拿哪些锁”纳入全局锁序治理；做不到就改成异步/消息化。
- 明确模块的加锁契约：一个模块对外接口是否“可在持锁时调用”，必须写进文档。

#### 5.3.2 回调造成的重入与阻塞传播

回调是另一个高发点。同步回调意味着“调用者等待被调用者返回”，如果回调里又去拿调用者持有的锁，就重入死锁：

```cpp
// callback_reentry.cpp
// 编译: g++ -std=c++17 -g -pthread callback_reentry.cpp -o callback_reentry
#include <mutex>
#include <functional>
#include <iostream>
#include <thread>

class Registry {
public:
    void on_change(std::function<void()> cb) {
        std::lock_guard<std::mutex> lk(mtx_);
        callback_ = std::move(cb);
    }

    void notify() {
        std::lock_guard<std::mutex> lk(mtx_);
        // 持锁执行回调：若回调又调用 Registry 的方法，就会自死锁
        if (callback_) callback_();
    }

private:
    std::mutex mtx_;
    std::function<void()> callback_;
};

int main() {
    Registry reg;
    reg.on_change([&] {
        // 回调里再次进入 Registry，需要同一把不可重入锁
        reg.notify();   // 死锁（自死锁）
    });
    reg.notify();
    return 0;
}
```

正确的做法是**把回调拷出来，释放锁后再调**：

```cpp
void notify() {
    std::function<void()> cb;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        cb = callback_;
    }
    if (cb) cb();   // 锁外执行
}
```

注意：改完后不再死锁，但上面演示用的回调本身又调用 `notify()`，会变成无限递归。真实代码里回调不应该反过来触发同一个通知；这里只是为了制造重入现场。

这也解释了“阻塞传播”：持锁执行慢回调，会让所有需要这把锁的线程一起排队；如果回调里再发 RPC，RPC 的超时又把锁的持有时间拉长，等待队列进一步膨胀，最终从单点慢变成全局阻塞。

#### 5.3.3 优先级反转与优先级继承

“优先级反转”最早在实时系统里被广泛讨论：高优先级任务 H 需要一把锁，而这把锁被低优先级任务 L 持有；同时中优先级任务 M 抢占了 L 的 CPU，导致 L 迟迟不能释放锁，于是 H 被 M 间接“压住”。现象是：**高优先级任务被低优先级任务阻塞，而中优先级任务反而先跑**。

映射到后台服务：

- 如果把“处理用户请求的线程”看作高优先级、把“后台清理/日志线程”看作低优先级，那么当清理线程持有一把热路径也要用的锁，且清理线程被调度延迟或做慢操作时，用户请求就会被拖住。
- 线程池里不同任务、不同优先级队列共享同一把锁时，也会出现类似问题：低优先级任务持锁时间长，高优先级请求排队。

解决思路：

- **优先级继承（Priority Inheritance）**：当高优先级任务等待低优先级任务持有的锁时，临时把持有者的优先级提升到等待者的优先级，使其尽快执行并释放锁，释放后恢复。
- **优先级天花板（Priority Ceiling）**：给每把锁设定一个“天花板优先级”，任何持有该锁的任务临时提升到天花板优先级，从根本上避免中优先级插队。
- **工程上更常见的替代**：缩短持锁时间、锁分段、读写分离、把慢操作移出临界区、用无锁队列/消息传递解耦。

要强调一点：`std::mutex` 本身**不提供**优先级继承。优先级继承通常需要实时 OS 的 mutex 属性（如 pthread 的 `PTHREAD_PRIO_INHERIT`，在支持的平台上）。所以后台工程师更现实的做法是“别在临界区里做慢事”，而不是指望锁帮你解决调度问题。

#### 5.3.4 小结：三条边最容易出事

把上面三类问题抽象成等待图的边：

1. **显式多锁**：`T1 → T2` 因为锁 A/B 顺序相反；
2. **隐式模块锁**：持锁调用外部模块，形成你代码里看不见的边；
3. **回调重入**：同步回调回到调用者，形成自环或跨线程环。

排查时，永远问三个问题：**谁持有？谁在等？等的这把锁还会引出谁？**

---

## 6. 常见误区与设计权衡

### 6.1 常见误区（Pitfalls）

#### 误区一：加超时重试就能“解决”死锁

很多人第一反应是“把 `lock` 换成 `try_lock_for(100ms)`，超时就重试”。这确实能让线程不永久卡死，但**不一定消除死锁根因**，还可能把死锁变成**活锁（livelock）**：

- 两个线程都超时、都释放、都重试，又同时拿到各自的第一把锁，再次撞上，如此循环；
- CPU 被大量重试占满，业务仍然没有进展；
- 日志里全是超时，但没有任何一个请求成功。

活锁的特征是“线程在动，但系统没前进”。它比死锁更难查，因为栈上看到的是活跃的重试循环，而不是静止的等待。

正确姿势：超时只能作为**兜底与可观测手段**（记录超时、暴露指标、触发告警），根因仍然要用锁序/减少持锁调用来消除。

#### 误区二：以为“锁加得少就不会死锁”

死锁与锁的数量不是线性关系，**两把锁就足够**。而且锁越少、粒度越粗，持锁时间往往越长，阻塞传播越严重。关键不是“少”，而是“有序”和“短”。

#### 误区三：以为 `recursive_mutex` 能解决回调重入

`std::recursive_mutex` 只允许**同一线程**重入，能缓解自死锁，但它掩盖了设计问题，并且会隐藏“锁的临界区边界不清”。跨线程的回调环它完全无能为力。不要把 `recursive_mutex` 当成万能药。

#### 误区四：在持锁时打印日志 / 写监控

日志库、监控上报、序列化往往内部有锁或 I/O。持锁打日志是典型的“隐式锁依赖 + 长临界区”，在高并发下极易放大成阻塞传播。日志应在锁外打，或使用异步日志。

#### 误区五：把“没复现”当成“没问题”

死锁是时序相关的，测试环境没复现不代表没有。要用 TSan 的 lock-order-inversion 检查、代码审计锁序、以及压力测试来降低概率。

### 6.2 设计权衡（Tradeoffs）

#### 统一锁序 vs 治理成本

统一锁序非常有效，但代价是**治理成本**：

- 需要维护一份“锁层级表”，新代码必须遵守；
- 跨团队、跨模块时，锁的归属和顺序难以强制；
- 代码评审要专门检查锁序，工具支持有限；
- 历史代码改造量大，容易“新代码守序、老代码乱序”。

所以实践中常配合：模块内固定锁序、模块间尽量不共享锁、用 `scoped_lock` 处理局部多锁、用静态检查/规范做兜底。

#### 消息化设计 vs 实时性/复杂度

消息化（把同步调用改成异步消息、事件、队列）能显著减少跨模块的锁依赖：模块之间不再“互相持锁调用”，而是通过消息传递数据，锁的边界收缩到模块内部。但代价是：

- 系统复杂度上升（消息丢失、重复、顺序、幂等都要处理）；
- 端到端延迟增加，调试链路变长；
- 一致性模型从“同步返回”变成“最终一致”，业务需要适配。

因此不是所有路径都值得消息化。通常对**热路径、跨模块回调、慢操作**优先消息化，对**模块内短临界区**保留锁。

#### 粗粒度锁 vs 细粒度锁

- 粗粒度：实现简单、锁序少，但持锁时间长、并发度低、阻塞传播明显；
- 细粒度：并发度高，但锁多、锁序复杂、死锁风险上升。

折中：按数据分片（sharding）降低锁竞争，同时保持每片内部锁序简单；读多写少用读写锁或无锁结构。

---

## 7. 动手实践

### 7.1 实验目标

构造双锁死锁，用 gdb 线程栈识别等待环并修复。

### 7.2 步骤

#### 步骤 1：构造并复现死锁

使用 5.1.3 的 `deadlock_demo.cpp`：

```bash
g++ -std=c++17 -g -pthread deadlock_demo.cpp -o deadlock_demo
./deadlock_demo &
PID=$!
sleep 2
```

观察：程序不退出，`ps -o pid,stat,cmd -p $PID` 显示进程存在，但 CPU 基本不动。

#### 步骤 2：抓线程栈，定位等待点

```bash
gdb -p $PID -batch -ex "info threads" -ex "thread apply all bt" > /tmp/deadlock_bt.txt
```

在 `/tmp/deadlock_bt.txt` 里找：

- 哪些线程停在 `__lll_lock_wait` / `pthread_mutex_lock`；
- 对应的业务帧是 `worker_a` 还是 `worker_b`；
- `pthread_mutex_lock` 的 `mutex=` 地址。

#### 步骤 3：确认锁归属，画出等待环

在 gdb 里打印两把锁的 owner：

```bash
gdb -p $PID
(gdb) p m1._M_mutex.__data.__owner
(gdb) p m2._M_mutex.__data.__owner
(gdb) info threads
```

对照 TID，写出：

```
T_A 持有 m1，等待 m2
T_B 持有 m2，等待 m1
等待图: T_A → T_B → T_A，存在环 → 死锁
```

这就是验收标准里的“**能用栈与锁归属证明死锁**”。

如果符号拿不到，用地址：

```gdb
(gdb) p *(pthread_mutex_t*)&m1
(gdb) p *(pthread_mutex_t*)&m2
```

#### 步骤 4：用 TSan 复现“潜在锁序反转”

```bash
g++ -std=c++17 -g -pthread -fsanitize=thread deadlock_demo.cpp -o deadlock_demo_tsan
./deadlock_demo_tsan
```

观察 `lock-order-inversion` 报告，理解它和真实死锁的关系（潜在 vs 实际）。

#### 步骤 5：修复

至少做两种修复并对比：

**修复 A：固定锁序**（改 `worker_b`，让它也先锁 `m1` 再锁 `m2`）：

```cpp
void worker_b() {
    std::lock_guard<std::mutex> g1(m1);
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    std::lock_guard<std::mutex> g2(m2);
    // ...
}
```

**修复 B：`std::scoped_lock`**：

```cpp
void worker_a() {
    std::scoped_lock lk(m1, m2);
    // ...
}
void worker_b() {
    std::scoped_lock lk(m1, m2);   // 顺序无关，算法保证
    // ...
}
```

#### 步骤 6：压力与停机测试

- 把循环次数拉高（例如每个线程 10 万次），反复运行多轮，确认不再卡死；
- 用 `timeout 60 ./fixed_demo` 包一层，确认能在超时前正常退出；
- 对服务化版本，验证能响应 SIGTERM 优雅停机（线程池 `shutdown`、`join` 不挂住）；
- 用 `gdb -p` 再抓一次栈，确认没有线程长期停在锁等待。

#### 步骤 7：源码核对（sourceStudy）

沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。具体检查清单：

- `std::scoped_lock` 的定义与构造函数语义：多锁版本是否调用 `std::lock`？单锁版本是否等价于 `lock_guard`？
- `std::lock` 的错误处理：如果某个锁的 `try_lock` 抛出异常，已持有的锁是否会被释放？（标准要求保证不会泄漏锁）
- 适用版本：`scoped_lock` 是 C++17；`std::lock` 是 C++11。确认项目的编译标准。
- 边界条件：锁对象生命周期、不可重入、`scoped_lock` 与 `unique_lock` 的配合、传入同一把锁两次的行为（未定义/错误用法）。
- 记录你所用编译器与标准库版本（`g++ --version`、libstdc++ 版本），因为不同实现对“潜在死锁检测”和 `__owner` 布局可能有差异。

### 7.3 Acceptance 对照

| 验收项 | 完成标志 |
| --- | --- |
| 能用栈与锁归属证明死锁 | 能从 `thread apply all bt` + `p m._M_mutex.__data.__owner` 写出等待环 |
| 修复后压力与停机测试通过 | 多轮高并发不卡死，`timeout` 内退出，SIGTERM 可优雅停机 |

---

## 8. 自测题（3 道，附答案）

### 题 1：等待图中出现环，是否一定死锁？

**答案**：对于**单实例资源**（例如每个线程独占的 `std::mutex`），等待图中出现环就意味着死锁已经发生。对于**多实例资源**（例如信号量有多个许可、资源有多份副本），环是死锁的**必要条件但不充分**，还需要结合资源分配情况做进一步判断。工程上绝大多数互斥锁是单实例，所以“环即死锁”是实用的判断。答题时要点出“单实例/多实例”的区别。

### 题 2：把 `std::lock_guard` 换成带超时的 `try_lock_for` 并重试，能不能解决死锁？

**答案**：不能从根本上解决。超时重试只是让线程不会永久阻塞，但**不消除资源依赖环**，可能把死锁变成**活锁**：多个线程反复超时、释放、重试，系统仍无进展，还额外消耗 CPU。正确做法是用固定锁序或 `std::scoped_lock` 消除环，超时仅作为兜底和可观测手段（记录指标、告警）。

### 题 3：为什么“持锁调用外部模块/回调/日志/RPC”容易导致死锁和阻塞传播？如何改？

**答案**：因为这些调用会在你不知情的情况下获取其他锁，形成隐式的锁依赖；如果外部模块在持锁时又回调业务代码，就会形成跨模块的环。同时这些操作通常很慢，会拉长临界区，让所有等待该锁的线程一起排队，阻塞沿调用链传播。改法：锁内只做内存操作，把需要 I/O/回调的数据拷贝出来，**释放锁后再调用**；跨模块场景优先改为异步/消息化，并把外部模块的加锁契约纳入锁序治理。

---

## 9. 推荐资源

- [std::scoped_lock — cppreference](https://en.cppreference.com/w/cpp/thread/scoped_lock.html)

阅读建议：重点看多锁构造函数的语义、它与 `std::lock` 的关系、单锁版本与 `lock_guard` 的等价性，以及异常安全说明。用这些定义去核对你实验里的行为和边界条件。

---

## 10. 本课小结

- 死锁由**资源依赖环**等条件导致；四个必要条件（互斥、持有并等待、不可剥夺、循环等待）同时满足才可能发生，破坏任意一个就能预防。
- **等待图**是排查死锁的核心工具：节点是线程，边是“等待”，有环就有死锁风险（单实例下环即死锁）。
- **固定全局锁序**是最常用的预防手段；`std::lock` / `std::scoped_lock` 提供了算法级的死锁避免，适合局部多锁场景（转账例子）。
- **持锁调用外部模块、回调、日志、RPC** 会制造隐式锁依赖和阻塞传播；原则是“锁内不做慢事，回调移到锁外”，跨模块优先消息化。
- **优先级反转**提醒我们：锁的等待还会被调度放大；`std::mutex` 不提供优先级继承，工程上靠缩短临界区和锁分段解决。
- 排查现场的标准动作：`gdb -p <pid>` → `info threads` → `thread apply all bt` → 在栈里找 `__lll_lock_wait` / `pthread_mutex_lock` → 打印 mutex 的 `__owner` 找到持有线程 → 画出等待环。
- 超时重试不是根因解药，可能变成活锁；验收要看“能证明死锁 + 修复后压力与停机通过”。
下一课是第 69 课「原子操作与复合不变量」：从锁转向原子操作，看单个原子变量能保证什么、保证不了什么。
