# 第 70/208 课：C++内存序与发布订阅

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 4 阶段 · 并发与内存模型（内存可见性与状态归属） |
| 前置 | e-atomic |
| 难度 | 深入 |
| 领域 | E |
| 标签 | Memory Ordering、E、C++ |
| 子分类 | 内存可见性与状态归属 |
| 节点 ID | e-order |
| 英文名 | Memory Ordering |

> 本课承接上一课「原子操作与复合不变量」。上一课解决的是“单个操作如何不可分割”，
> 本课解决的是“一个线程写下的东西，另一个线程凭什么一定能看到、并且按什么顺序看到”。
> 核心结论先放在这里：**原子性不等于可见性，更不等于顺序性；
> `release` / `acquire` 只有在“读者读到了写者发布的那个值”时才建立同步。**
> 因此内存序不是一个可以凭感觉加的参数，而是一份需要被证明的协议。

---

## 导读

上一课我们反复强调：`std::atomic` 只保证单个操作不可分割，多个原子字段拼不成事务。于是自然会冒出一个更深的问题：

- 我把 `data` 写好了，再把 `ready` 置为 `true`，读者看到 `true` 之后，真的能安全地读 `data` 吗？
- 如果两个线程都先写自己的标志、再读对方的标志，会不会同时看到对方“还没写”？
- `memory_order_relaxed` 到底“松”掉了什么？它还是原子的吗？
- 为什么有些代码在 x86 服务器上跑了三年都没事，换到 ARM 机器上就开始偶发读到旧值？

这些问题的答案都在 C++ 内存模型里。它的基本语言是三个关系：**sequenced-before（同线程内的先后）**、**synchronizes-with（跨线程的发布-获取配对）**、**happens-before（前两者加上传递闭包）**。`release` / `acquire` / `relaxed` / `seq_cst` 不是“刷新缓存的开关”，而是用来构造这些关系的工具。

放到微信公众号后台的真实场景里：

- **配置热更新**：配置线程构造好新的 `Config` 对象，然后发布一个指针；工作线程拿到指针后直接读字段。如果发布和读取没有正确的内存序，工作线程可能拿到一个“指针已经可见、对象内容还没可见”的配置。
- **标志位发布**：后台任务把一批素材处理完，写一个 `done = true`；前端线程轮询到 `done` 后去读结果。标志和数据之间的可见性必须由同步关系保证。
- **无锁队列的头尾指针**：生产者写数据槽位，再推进 `head`；消费者读 `head`，再读槽位。`head` 的发布必须让“槽位写入”对消费者可见。头尾指针各自原子，并不自动保证槽位内容可见。
- **计数器与统计**：阅读数、曝光数只需要原子性，不需要顺序，这时 `relaxed` 才是合适的工具，而不是无脑 `seq_cst`。

本课的目标不是让你背下六种内存序的名字，而是让你能在一段发布订阅代码旁边，逐条写出：**这条读从哪里取值、它和哪条写构成 synchronizes-with、由此推出了哪条 happens-before、所以哪次非原子访问不再构成数据竞争。**

---

## 学习目标

### goals

- 能解释 sequenced-before 和 happens-before 的区别与联系，并说明它们各自在什么范围内生效。
- 能解释 `release` / `acquire` 的配对条件，特别是“读值匹配”这一必要条件。
- 能解释 `relaxed` 只提供原子性、不建立跨线程顺序，并说明它适合什么、不适合什么。
- 能解释 `seq_cst` 的全局单一顺序含义，以及它额外阻止的 StoreLoad 重排。
- 能独立完成：写单次消息发布实验，标注每条 happens-before 边并使用模型化推理。

### acceptance

- 能证明非原子 payload 读取有同步：写出 `sb`、`sw`、`hb` 三类边，并据此说明 payload 的读写不构成数据竞争。
- 能解释 `relaxed` 只提供原子性：能指出它不产生 `synchronizes-with`，因此不能用来发布非原子数据。

---

## 为什么后端要懂

内存序是“并发代码从能跑到正确”的分水岭。后端工程师至少会在下面这些地方直接碰到它：

1. **配置与路由表热更新**：发布一个指针或版本号，让所有工作线程无锁读取。写错内存序，表现是“偶发读到半初始化配置”，而且极难复现。
2. **无锁队列与环形缓冲**：头尾索引的推进顺序决定了槽位内容是否可见。`relaxed` 和 `acquire/release` 用错，队列会偶发丢数据或读到脏槽。
3. **一次性初始化与状态机**：`initialized`、`running`、`stopping` 这类标志的发布，必须和它保护的状态一起建立 happens-before。
4. **性能敏感路径**：在热点路径上，`seq_cst` 可能引入额外的内存栅栏；`relaxed` 或 `acquire/release` 能省掉不必要的约束。但省约束的前提是你能证明协议正确，而不是“压测没崩”。
5. **跨架构可移植性**：x86 是较强的内存模型（TSO），很多弱序 bug 在 x86 上被掩盖；ARM、POWER 等弱内存模型平台会把它们暴露出来。云上混合架构部署时，这不是理论问题。
6. **排查线上“玄学”问题**：读到旧配置、看到半初始化对象、标志为真但数据为空，最后往往都能归结为“缺少一条 synchronizes-with 边”。

一句话：**懂内存序，你才知道自己的发布协议为什么正确；不懂内存序，你只是碰巧在某个 CPU 上正确。**

---

## 直觉 → 原理

### 1. sequenced-before 与 happens-before：两个不同范围的“先后”

#### sequenced-before：线程内的先后

`sequenced-before`（简称 `sb`）描述的是**同一个线程内部**的求值顺序。它来自语言规则，例如：

- 一条语句里的完整表达式按顺序求值；
- 前一条语句的求值先于后一条语句；
- 函数调用内部的求值先于调用返回后的求值；
- 初始化、operator 的求值规则等。

它只在一个线程内成立，不告诉你在另一个线程看来谁先谁后。看一段普通代码：

```cpp
int x = 0;
x = 42;        // (1)
int y = x;     // (2)
```

在同一个线程内，(1) sequenced-before (2)，所以 `y` 一定是 42。这是语言层面的顺序，不需要任何原子操作。

但请注意：**`sequenced-before` 是偏序，不是所有操作之间都有边。** 例如两个独立函数参数的求值顺序在 C++17 之前是未指定的（C++17 起函数参数求值顺序仍有未指定部分），不能想当然地认为“写在左边的先执行”。这也是为什么发布协议里不能依赖“看起来的代码顺序”。

#### happens-before：跨线程的可见性顺序

`happens-before`（简称 `hb`）是跨线程的偏序关系，定义可以概括为：

> 如果 A `sequenced-before` B，则 A `happens-before` B；
> 如果 A `synchronizes-with` B，则 A `happens-before` B；
> 如果存在 C，使得 A `happens-before` C 且 C `happens-before` B，则 A `happens-before` B（传递闭包）。

也就是说：

```text
sequenced-before（线程内）
        +
synchronizes-with（跨线程的发布-获取配对）
        ↓ 传递闭包
happens-before（跨线程的完整可见性顺序）
```

数据竞争的定义正是建立在 `hb` 上：

> 两个来自不同线程的冲突访问（至少一个不是原子操作，或者至少一个不是 `relaxed` 原子操作），如果它们之间没有 `happens-before` 关系，就构成数据竞争，程序行为未定义。

所以“我的非原子 `payload` 读取安全吗”这个问题，等价于“写 `payload` 和读 `payload` 之间是否存在一条 `happens-before` 链”。

#### 一个最小的发布-获取例子

```cpp
// message_publish.cpp
#include <atomic>
#include <cassert>
#include <iostream>
#include <thread>

int g_payload = 0;                                   // 非原子共享数据
std::atomic<bool> g_ready{false};                    // 发布标志

void publisher() {
    g_payload = 42;                                  // (1) 写 payload
    g_ready.store(true, std::memory_order_release);  // (2) 发布
}

void subscriber() {
    while (!g_ready.load(std::memory_order_acquire)) { // (3) 获取
        std::this_thread::yield();
    }
    int v = g_payload;                               // (4) 读 payload
    std::cout << "payload = " << v << "\n";
    assert(v == 42);
}

int main() {
    std::thread t1(publisher);
    std::thread t2(subscriber);
    t1.join();
    t2.join();
    std::cout << "ok\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 message_publish.cpp -o message_publish
./message_publish
```

把这段代码的边逐条标出来：

```text
sb: (1) -> (2)      // 同线程，payload 写先于 release store
sb: (3) -> (4)      // 同线程，acquire load 先于 payload 读
sw: (2) -> (3)      // (3) 读到了 (2) 写入的 true，release/acquire 配对
hb: (1) -> (4)      // 由 (1) sb (2) sw (3) sb (4) 传递得到
```

因为 `hb(1, 4)` 成立，非原子的 `g_payload` 写和读之间不构成数据竞争，`assert` 有意义。这就是“发布订阅”的全部骨架。

#### 为什么必须是“读到了写者写的值”

`synchronizes-with` 有一条容易被忽略的条件：**acquire 读必须读到 release 写所写入的那个值（或该 release store 主导的 release sequence 中的值）。** 仅仅“代码里有一个 release、有一个 acquire”不够。

看一个坏例子：

```cpp
// publish_initial_true.cpp
#include <atomic>
#include <cassert>
#include <thread>

int g_payload = 0;
std::atomic<bool> g_ready{true};   // 注意：初始值就是 true

void publisher() {
    g_payload = 42;
    g_ready.store(true, std::memory_order_release);  // (P)
}

void subscriber() {
    if (g_ready.load(std::memory_order_acquire)) {   // (C)
        assert(g_payload == 42);                     // 可能失败
    }
}
```

如果 `subscriber` 在 `publisher` 执行之前就运行，它在 `(C)` 读到的是**初始化时的 `true`**，不是 `(P)` 写入的值。此时没有 `sw(P, C)`，也就没有 `hb(payload 写, payload 读)`，`assert` 可能看到 0。这个例子说明：**同步关系是“按值匹配”的，不是“按变量存在”的。**

工程上的做法是：发布标志初始为 `false`，只有真正准备好之后才用 release 写成 `true`；读者只在读到 `true` 后才访问 payload。这样“读到 true”就必然对应某次真实的发布。

> `release sequence` 补充：如果 release store 之后，同一个原子对象上还有同线程的普通写或其他线程的 read-modify-write，这些操作可以构成一个序列，acquire 读到序列中的值也可能与最初的 release store 同步。日常发布协议通常只需要“release store 写 true，acquire load 读 true”这一种最简单的情形，但读 cppreference 时会看到这条扩展规则，不要把它和“任意读值都能同步”混为一谈。

---

### 2. release / acquire 与 relaxed：分别约束了什么

#### release：把之前的写“压”在这一步之前

`memory_order_release` 用在 store（或读-改-写）上，语义是：

> 这个 release 操作**之前**的所有读和写（包括非原子访问），都不能被重排到这个 release 操作之后。

注意它只约束“之前的东西不能往后跑”，不约束“之后的东西不能往前跑”。所以单独一个 release store 并不构成同步，它只是“准备好了发布的内容”。

#### acquire：把之后的读“压”在这一步之后

`memory_order_acquire` 用在 load（或读-改-写）上，语义是：

> 这个 acquire 操作**之后**的所有读和写，都不能被重排到这个 acquire 操作之前。

它只约束“之后的东西不能往前跑”。单独一个 acquire load 也不构成同步。

#### 两者配对：synchronizes-with

当 acquire load 读到了 release store 写入的值时：

```text
release store 之前的所有写
        |
        |  (sb)
        v
   release store  ---- synchronizes-with ---->  acquire load
                                                      |
                                                      |  (sb)
                                                      v
                                             acquire load 之后的所有读
```

于是 release 之前写的非原子数据，在 acquire 之后读就是安全的。这就是 `message_publish.cpp` 的原理。

如果发布和获取跨越了多个线程，或者中间经过 RMW，还可以用 `memory_order_acq_rel`：它同时具有 acquire 和 release 语义，常用于 CAS 循环、引用计数、无锁队列的索引推进等“既读又写”的 RMW 操作。

#### relaxed：只有原子性，没有顺序

`memory_order_relaxed` 是最弱的序。它保证：

- 这个操作本身是原子的，不会出现“改到一半被看见”；
- 对同一个原子对象，所有线程看到的修改顺序是一致的（modification order），不会出现某个线程先看到新值、再看到旧值这种“倒流”。

但它**不建立 `synchronizes-with`**，也**不约束周围非原子访问的重排**。因此：

```cpp
std::atomic<int> counter{0};

void on_read() {
    counter.fetch_add(1, std::memory_order_relaxed);  // 原子，但无跨线程顺序
}
```

这个计数器最终值仍然正确，因为 `fetch_add` 是 RMW，原子性足够。但它不能用来说明“我在 `fetch_add` 之前写的某个普通变量，别的线程在 `fetch_add` 之后一定看得到”。

一个常见的错误是用 relaxed 做发布：

```cpp
// publish_relaxed_bug.cpp
#include <atomic>
#include <cassert>
#include <thread>

int g_payload = 0;
std::atomic<bool> g_ready{false};

void publisher() {
    g_payload = 42;                                    // (1)
    g_ready.store(true, std::memory_order_relaxed);    // (2)
}

void subscriber() {
    while (!g_ready.load(std::memory_order_relaxed)) { // (3)
        std::this_thread::yield();
    }
    assert(g_payload == 42);                           // (4) 可能是 0，甚至 UB
}
```

这里 `(2)` 和 `(3)` 都是 relaxed，即使 `(3)` 读到了 `true`，也不会产生 `sw(2,3)`，于是没有 `hb(1,4)`。`g_payload` 的写和读构成数据竞争，程序行为未定义。编译器甚至可以把 `(4)` 的读取提前到循环之前，直接读到一个旧值。

**一句话对比：**

| 内存序 | 原子性 | 建立 synchronizes-with | 约束周围访问重排 | 典型用途 |
| --- | --- | --- | --- | --- |
| `relaxed` | 是 | 否 | 否 | 纯计数、统计、只关心单个原子对象 |
| `release` | 是 | 只能作为“写”的一端 | 之前的访问不能后移 | 发布数据 |
| `acquire` | 是 | 只能作为“读”的一端 | 之后的访问不能前移 | 获取数据 |
| `acq_rel` | 是 | RMW 同时可作两端 | 两侧都约束 | CAS 循环、队列索引 |
| `seq_cst` | 是 | 是 | 全局单一顺序 + 阻止 StoreLoad | 默认、需要全局顺序时 |

这里要特别纠正一个流传很广的说法：**内存序不是“把缓存刷到内存”的命令。** CPU 缓存一致性协议本来就会让同一地址的写最终可见；内存序约束的是**不同地址之间的访问顺序**以及**跨线程的可见性关系**。把它理解成“刷新缓存”，就会得出“加了 fence 就万事大吉”的错误结论，也会解释不了为什么 `relaxed` 明明是原子的却不能发布数据。

---

### 3. seq_cst 与重排序：什么时候“弱序”不够

`memory_order_seq_cst` 是默认内存序，也是最强的。它有两层含义：

1. **每个 `seq_cst` 操作都带有 acquire/release 语义**：`seq_cst` store 是 release store，`seq_cst` load 是 acquire load，`seq_cst` RMW 是 acq_rel RMW。
2. **所有 `seq_cst` 操作存在一个全局单一总顺序**，所有线程都同意这个顺序，并且这个总顺序与 `happens-before` 一致。

第 2 条是 `seq_cst` 独有的。`acquire/release` 可以建立跨线程的可见性，但它们不保证所有线程对“两个不同原子变量的操作”看到同一个全局顺序。

#### StoreLoad 重排：acquire/release 管不到的地方

`release` 只阻止“之前的访问后移”，`acquire` 只阻止“之后的访问前移”。那么一个线程里的 **store 后面跟一个 load** 呢？

```text
线程 A:  store x = 1   (release)
         load  y       (acquire)

线程 B:  store y = 1   (release)
         load  x       (acquire)
```

release 不阻止后面的 load 前移，acquire 不阻止前面的 store 后移，于是两个线程都可能先完成自己的 store、再去读对方，结果**双方都读到 0**。这在 `acquire/release` 下是允许的，在 `seq_cst` 下不允许。这就是著名的 StoreLoad 重排，也是 Dekker 互斥算法的核心。

一个可运行的验证程序：

```cpp
// seqcst_store_load.cpp
#include <atomic>
#include <iostream>
#include <thread>

#ifdef USE_SEQ_CST
constexpr std::memory_order kStore = std::memory_order_seq_cst;
constexpr std::memory_order kLoad  = std::memory_order_seq_cst;
#else
constexpr std::memory_order kStore = std::memory_order_release;
constexpr std::memory_order kLoad  = std::memory_order_acquire;
#endif

std::atomic<int> x{0}, y{0};
int r1 = 0, r2 = 0;

void thread_a() {
    x.store(1, kStore);   // A
    r1 = y.load(kLoad);   // B
}

void thread_b() {
    y.store(1, kStore);   // C
    r2 = x.load(kLoad);   // D
}

int main() {
    constexpr int kIters = 20000;
    for (int i = 0; i < kIters; ++i) {
        x.store(0, std::memory_order_relaxed);
        y.store(0, std::memory_order_relaxed);
        r1 = 0;
        r2 = 0;

        std::thread a(thread_a);
        std::thread b(thread_b);
        a.join();
        b.join();

        if (r1 == 0 && r2 == 0) {
            std::cout << "observed both zero at iteration " << i << "\n";
            return 1;
        }
    }
    std::cout << "never both zero in " << kIters << " iterations\n";
    return 0;
}
```

```bash
# 弱序版本：理论上允许 r1 == 0 && r2 == 0
g++ -std=c++17 -pthread -O2 seqcst_store_load.cpp -o weak
./weak

# 顺序一致版本：不允许双方都读到 0
g++ -std=c++17 -pthread -O2 -DUSE_SEQ_CST seqcst_store_load.cpp -o strong
./strong
```

需要提醒：**这个实验是“发现反例”的，不是“证明正确”的。** 弱序版本在某些机器上可能跑很多次都不出现双 0（尤其 x86 的 store-load 重排窗口很小），但这不代表弱序版本正确；而只要出现一次双 0，就足以证明它不是 `seq_cst`。反过来，`seq_cst` 版本不出现双 0，也不能只靠压测证明，必须靠模型推理。

#### 什么时候必须 seq_cst

- 你需要“所有线程对多个原子变量的操作顺序达成一致”；
- 你在写类似 Dekker、Peterson 的互斥/握手协议；
- 你有一个“先写自己的标志，再读对方标志”的对称协议；
- 你不确定弱序是否够用，且该路径不是性能瓶颈。

#### 什么时候可以更弱

- 纯计数、统计、只关心单个原子对象：`relaxed`；
- 单向发布数据：release store + acquire load；
- 队列索引推进：生产者 release 写 head，消费者 acquire 读 head；消费者 release 写 tail，生产者 acquire 读 tail；
- 需要 RMW 且两侧都要约束：`acq_rel`。

选择的原则不是“越弱越快”，而是**先写出协议需要的同步边，再选能提供这些边的最弱内存序**。

---

## 场景示例

### 场景一：配置热更新（release/acquire 发布指针）

后台配置经常是“读多写少”。用一把读写锁当然可以，但热路径上更常见的是发布一个不可变配置对象的指针，让读者无锁获取。

```cpp
// config_publish.cpp
#include <atomic>
#include <iostream>
#include <thread>

struct Config {
    int timeout_ms;
    int max_retry;
};

std::atomic<Config*> g_current{nullptr};

void publish(Config* c) {
    g_current.store(c, std::memory_order_release);   // 发布：之前的初始化对读者可见
}

Config* snapshot() {
    return g_current.load(std::memory_order_acquire); // 获取：之后的读取不会被前移
}

int main() {
    Config c{3000, 5};          // 先构造完整对象
    publish(&c);                // 再发布

    std::thread reader([&] {
        Config* p = snapshot();
        if (p) {
            std::cout << "timeout = " << p->timeout_ms
                      << ", retry = " << p->max_retry << "\n";
        }
    });
    reader.join();
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 config_publish.cpp -o config_publish
./config_publish
```

这里的 `hb` 链是：

```text
构造 Config 的写  sb  publish() 的 release store
release store  sw  snapshot() 的 acquire load（读到了该指针）
acquire load  sb  读取 p->timeout_ms / p->max_retry
=> 构造的写 hb 字段读取
```

注意生命周期问题：本课只讨论“发布可见性”，不讨论“旧配置何时可以释放”。如果配置会不断替换，`delete` 旧对象的时机需要引用计数、RCU 或延迟回收来保证，否则读者可能拿到已释放的指针。**内存序只保证可见性，不保证对象活得够久。**

### 场景二：标志位发布（一次性任务完成）

这是最典型的“单次消息发布”：一个线程写结果，另一个线程等完成标志。

```cpp
// result_publish.cpp
#include <atomic>
#include <cassert>
#include <iostream>
#include <thread>
#include <vector>

struct Result {
    int code;
    long long value;
};

Result g_result{};                                  // 非原子 payload
std::atomic<bool> g_done{false};                    // 发布标志

void worker() {
    g_result.code = 200;                            // (1)
    g_result.value = 123456789;                     // (2)
    g_done.store(true, std::memory_order_release);  // (3) 一次性发布
}

void waiter() {
    while (!g_done.load(std::memory_order_acquire)) { // (4)
        std::this_thread::yield();
    }
    assert(g_result.code == 200);                   // (5)
    assert(g_result.value == 123456789);            // (6)
    std::cout << "result = " << g_result.value << "\n";
}

int main() {
    std::thread t1(worker);
    std::thread t2(waiter);
    t1.join();
    t2.join();
    std::cout << "ok\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 result_publish.cpp -o result_publish
./result_publish
```

边标注：

```text
sb: (1) -> (2) -> (3)
sb: (4) -> (5) -> (6)
sw: (3) -> (4)        // (4) 读到 true
hb: (1) -> (5), (2) -> (6)
```

关键点：`(3)` 必须用 release，`(4)` 必须用 acquire。如果 `(4)` 只是 relaxed，即使它读到了 true，也不会把 `(1)(2)` 带到读者线程。

### 场景三：无锁环形队列的头尾指针

单生产者单消费者（SPSC）队列是 `acquire/release` 最经典的应用。生产者写槽位，再推进 `head`；消费者读 `head`，再读槽位。反向地，消费者推进 `tail`，让生产者知道槽位可以复用。

```cpp
// spsc_ring.cpp
#include <atomic>
#include <cstddef>
#include <iostream>
#include <thread>

template <typename T, std::size_t N>
class SpscRing {
    static_assert(N >= 2 && (N & (N - 1)) == 0, "N must be a power of two >= 2");

public:
    bool push(const T& v) {
        const std::size_t head = head_.load(std::memory_order_relaxed);
        const std::size_t next = (head + 1) & (N - 1);
        if (next == tail_.load(std::memory_order_acquire)) {
            return false;  // 队列满
        }
        buf_[head] = v;
        head_.store(next, std::memory_order_release);  // 发布槽位写入
        return true;
    }

    bool pop(T& out) {
        const std::size_t tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) {
            return false;  // 队列空
        }
        out = buf_[tail];
        tail_.store((tail + 1) & (N - 1), std::memory_order_release);  // 发布槽位释放
        return true;
    }

private:
    T buf_[N]{};
    alignas(64) std::atomic<std::size_t> head_{0};  // 只有生产者写
    alignas(64) std::atomic<std::size_t> tail_{0};  // 只有消费者写
};

int main() {
    constexpr int kItems = 1000000;
    SpscRing<int, 1024> q;
    std::atomic<long long> sum{0};

    std::thread producer([&] {
        for (int i = 0; i < kItems; ++i) {
            while (!q.push(i)) std::this_thread::yield();
        }
    });

    std::thread consumer([&] {
        int v = 0;
        for (int i = 0; i < kItems; ++i) {
            while (!q.pop(v)) std::this_thread::yield();
            sum.fetch_add(v, std::memory_order_relaxed);
        }
    });

    producer.join();
    consumer.join();

    std::cout << "sum = " << sum.load() << "\n";
    std::cout << "expected = " << (1LL * (kItems - 1) * kItems / 2) << "\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 spsc_ring.cpp -o spsc_ring
./spsc_ring
```

同步边：

```text
生产者: buf_[head] = v  sb  head_.store(next, release)
消费者: head_.load(acquire) 读到 next  -> sw
        => buf_[head] = v hb out = buf_[tail]

消费者: out = buf_[tail]  sb  tail_.store(..., release)
生产者: tail_.load(acquire) 读到新 tail -> sw
        => 消费者读完槽位 hb 生产者复用槽位
```

注意这里 `head_` 和 `tail_` 的“本地读”用的是 `relaxed`，因为读写各自由单一所有者负责，不需要跨线程顺序；真正需要发布的是“推进索引”这一步，所以用 release；对端读索引用 acquire，从而看到索引背后的槽位内容。**这就是“按需选最弱序”的典型例子。**

### 场景四：什么时候 relaxed 就够了

如果状态只是一个独立计数器，不承载任何其他数据的发布，`relaxed` 完全够用：

```cpp
// relaxed_counter.cpp
#include <atomic>
#include <iostream>
#include <thread>
#include <vector>

std::atomic<long long> g_reads{0};

void worker() {
    for (int i = 0; i < 100000; ++i) {
        g_reads.fetch_add(1, std::memory_order_relaxed);
    }
}

int main() {
    constexpr int kThreads = 8;
    std::vector<std::thread> ts;
    for (int i = 0; i < kThreads; ++i) ts.emplace_back(worker);
    for (auto& t : ts) t.join();

    std::cout << "reads = " << g_reads.load(std::memory_order_relaxed) << "\n";
    std::cout << "expected = " << kThreads * 100000 << "\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 relaxed_counter.cpp -o relaxed_counter
./relaxed_counter
```

结果稳定为 800000。这里 relaxed 提供的是：

- `fetch_add` 的原子性（不会丢失更新）；
- 对同一原子对象的修改顺序一致性（不会看到计数倒退）。

它不提供的是：不能用 `g_reads` 的读写去给别的非原子数据“打包票”。**relaxed 只提供原子性，这句话是本课验收点之一。**

---

## 常见误区与设计权衡

### 误区 1：压力测试没失败，就认为弱内存序正确

这是本节点明确点出的陷阱。原因有三：

1. **x86 是强内存模型**：很多 acquire/release 甚至部分 seq_cst 的差异在 x86 上被硬件“顺手”保证了。代码在 x86 上跑一万次不失败，换到 ARM 就可能失败。
2. **编译器重排与硬件重排是两回事**：即使硬件不重排，编译器也可能基于 relaxed 语义把读提前、把写延后。`-O2` 下尤其要小心。
3. **竞态窗口可能极小**：依赖时序的 bug 可能只在特定负载、特定核数、特定调度下出现。没观察到反例，不等于没有反例。

正确的态度是：**用 happens-before 模型证明正确性；压测只用来发现反例，不用来证明正确性。**

### 误区 2：把内存序当成“刷新缓存”的命令

`memory_order_release` 不是“把 cache line 刷回内存”，`acquire` 也不是“让缓存失效”。它们定义的是**访问之间的顺序约束**。理解这一点，才能明白：

- 为什么 `relaxed` 是原子的，却不能发布数据；
- 为什么 `release` / `acquire` 必须配对，单独一个没用；
- 为什么“我加了 seq_cst 所以肯定对”也是错的——如果根本没有建立 `hb`，seq_cst 也救不了非原子的并发读写。

### 误区 3：以为写了 release/acquire 就一定有同步

同步需要“读值匹配”。如果 acquire 读到的不是 release 写写入的值（比如读到了初值、读到了另一个 relaxed 写、或者读到了被覆盖后的值），就没有 `synchronizes-with`。发布标志一定要保证“只有发布者会写、读者只在发布后读到目标值”。

### 误区 4：以为 relaxed 是“不安全”的，所以一律用 seq_cst

`relaxed` 在“只关心单个原子对象”的场景下是完全正确的，而且通常更便宜。无脑 `seq_cst` 会带来不必要的栅栏和优化限制。真正的安全性来自协议，而不是最强的参数。

### 误区 5：把“原子变量”当成“它周围数据的保护伞”

`std::atomic<bool> ready` 只保护 `ready` 自己。它不会自动保护 `payload`。只有当你用 `ready` 建立了一条 `hb` 边，`payload` 才被纳入保护范围。

### 误区 6：忽略了 StoreLoad 重排

`acquire/release` 不是万能的。对称的“先写自己、再读对方”协议需要 `seq_cst`，否则双方可能都读到 0。看到“两个线程互相等待对方标志”的代码，先检查是不是需要全局顺序。

### 设计权衡

| 方案 | 适合 | 代价 / 风险 |
| --- | --- | --- |
| `relaxed` | 纯计数、统计、单对象原子性 | 不能发布其他数据；误用会导致数据竞争 |
| `release` / `acquire` | 单向发布订阅、SPSC 队列 | 需要精确配对；读值不匹配则无同步 |
| `acq_rel` | RMW 需要两侧约束 | 比 relaxed 贵；协议仍需证明 |
| `seq_cst` | 需要全局顺序、对称握手、默认安全 | 可能引入额外栅栏；性能敏感路径需测量 |
| 互斥锁 / 条件变量 | 复杂状态、多字段事务 | 阻塞、上下文切换、死锁风险 |

一句话总结 tradeoff：**弱内存序可能提升性能，但应由可说明的协议与测量支持。** 如果说不清每条 `hb` 边从哪来，就不要为了省一点栅栏去用更弱的序；如果瓶颈确实在原子操作上，再用测量数据驱动优化。

---

## 动手实践

### 任务：写单次消息发布实验，标注每条 happens-before 边

本实践对应 `practice`：写单次消息发布实验，标注每条 happens-before 边并使用模型化推理。验收对应：

- 能证明非原子 payload 读取有同步；
- 能解释 relaxed 只提供原子性。

#### 步骤 1：实现正确的发布订阅

1. 新建 `message_publish.cpp`，内容使用本课第 1 节的完整示例。
2. 编译运行：
   ```bash
   g++ -std=c++17 -pthread -O2 message_publish.cpp -o message_publish
   for i in $(seq 1 20); do ./message_publish; done
   ```
3. 在纸上或笔记里画出三条边：
   ```text
   sb: (1) -> (2)
   sb: (3) -> (4)
   sw: (2) -> (3)   // 因为 (3) 读到了 (2) 写的 true
   hb: (1) -> (4)   // 传递闭包
   ```
4. 用一句话说明为什么 `g_payload` 的非原子读不构成数据竞争：因为 `hb(写 payload, 读 payload)` 成立，两个冲突访问之间有 happens-before。

#### 步骤 2：把发布改成 relaxed，观察“没失败”的假象

1. 复制一份 `message_publish_relaxed.cpp`，把 `(2)` 和 `(3)` 的 `memory_order_release` / `memory_order_acquire` 都改成 `memory_order_relaxed`。
2. 用同样的命令多跑几次，记录是否看到 `payload = 0` 或 assertion 失败。
3. 再打开优化和更多轮次：
   ```bash
   g++ -std=c++17 -pthread -O3 message_publish_relaxed.cpp -o message_publish_relaxed
   for i in $(seq 1 200); do ./message_publish_relaxed; done
   ```
4. 回答：即使 200 次都没失败，能否证明它正确？不能。请从“x86 TSO”“编译器重排”“竞态窗口”三个角度解释。
5. 用模型标注这个版本：
   ```text
   sb: (1) -> (2)
   sb: (3) -> (4)
   sw: 无              // relaxed 不建立 synchronizes-with
   hb: (1) -/-> (4)    // 没有跨线程边，传递闭包无法连接
   ```
   结论：`(1)` 和 `(4)` 是不同线程的冲突访问，且无 `hb`，构成数据竞争，行为未定义。

#### 步骤 3：验证“读值匹配”条件

1. 把 `g_ready` 的初始值改成 `true`，保留 release/acquire，构造本课 `publish_initial_true.cpp` 的情形。
2. 让 `subscriber` 先启动、`publisher` 延迟一会儿再写（可以用 `std::this_thread::sleep_for`），观察读者可能读到初始 `true`。
3. 标注这个执行：
   ```text
   (C) 读到的是初始化的 true，不是 (P) 写的 true
   => 不存在 sw(P, C)
   => 不存在 hb(payload 写, payload 读)
   ```
4. 修复方式：把标志初始化为 `false`，确保读者读到 `true` 时只能来自发布者的 release store。思考：如果确实需要一个“初始就有效”的配置，应该怎么设计协议？（提示：让初始状态也经过一次等价的发布，或使用版本号/序列号区分。）

#### 步骤 4：做一个 SPSC 队列的边标注

1. 编译运行 `spsc_ring.cpp`，确认 `sum` 与 `expected` 相等。
2. 标出生产者和消费者之间的两条 `sw` 边（`head` 的发布与获取、`tail` 的发布与获取）。
3. 把 `head_.store` 改成 relaxed，压力测试并观察是否出现数据错误。同样提醒：没出错不代表正确。
4. 解释为什么 `head_.load(relaxed)` 在生产者内部是安全的：因为只有生产者写 `head_`，它读自己写的值不需要跨线程同步；真正需要 acquire 的是消费者读 `head_`。
5. 回答：如果把消费者的 `head_.load(acquire)` 改成 relaxed，槽位内容的读取还安全吗？不安全，因为缺少 `sw` 边，`buf_[head]` 的写与读之间没有 `hb`。

#### 步骤 5：用 seq_cst 做 StoreLoad 对照

1. 编译运行 `seqcst_store_load.cpp` 的弱序版本和 `-DUSE_SEQ_CST` 版本。
2. 记录是否观察到 `both zero`。
3. 用模型解释：
   ```text
   弱序：A sb B，C sb D，但 A 与 D、C 与 B 之间无 hb
        => 允许 r1 == 0 && r2 == 0
   seq_cst：所有 seq_cst 操作有全局单一顺序
        => 若 A 在 C 前，则 D 必在 A 后，r2 读到 1；反之 r1 读到 1
        => 不可能双方都读到 0
   ```
4. 结论：这不是“压测出来的经验”，而是内存模型给出的约束。

#### acceptance 自检

- [ ] 能在 `message_publish.cpp` 上写出 `sb(1,2)`、`sb(3,4)`、`sw(2,3)`、`hb(1,4)` 四条边。
- [ ] 能说明 `g_payload` 的读写不构成数据竞争，理由是 `hb(写, 读)` 成立。
- [ ] 能解释 `relaxed` 版本为什么没有 `sw` 边，因此不能发布非原子 payload。
- [ ] 能说清 `relaxed` 仍然提供原子性和同一原子对象的修改顺序一致性，只是不提供跨线程顺序。
- [ ] 能说明“压力测试未失败不能证明弱内存序正确”，并给出至少两个原因。
- [ ] 能解释 `seq_cst` 额外提供的全局单一顺序，以及 StoreLoad 重排为什么需要它。

#### sourceStudy

以 cppreference 的内存序定义为准，对代码逐条标出读取来源、synchronizes-with 及 happens-before 关系。建议按以下路径阅读并对照本课代码：

1. **`std::memory_order` 枚举**：逐个核对 `relaxed`、`consume`、`acquire`、`release`、`acq_rel`、`seq_cst` 的定义与适用操作（本课不展开 `consume`，但要知道它存在且语义特殊）。
2. **sequenced-before 与 happens-before 的正式定义**：核对“同线程 sb”“跨线程 sw”“hb 是传递闭包”这三条，和本课第 1 节的标注一一对应。
3. **synchronizes-with 的条件**：重点确认“acquire 读必须读到 release 写写入的值或 release sequence 中的值”，并用 `publish_initial_true.cpp` 验证“读到初值不构成同步”。
4. **release/acquire 的排序保证**：核对 release 之前的访问不能后移、acquire 之后的访问不能前移，以及它们各自单独不构成同步。
5. **seq_cst 的总顺序**：核对“所有 seq_cst 操作存在单一总顺序且与 hb 一致”，并对照 `seqcst_store_load.cpp` 的 Dekker 例子。
6. **数据竞争与未定义行为**：核对“冲突访问 + 至少一个非原子 + 无 hb”这个定义，用它来判断本课每个 relaxed 反例为什么是 UB。
7. **版本与平台**：记录你使用的标准版本（C++17）、编译器版本、目标架构。若在 ARM 机器上能复现弱序反例，把结果写进实验笔记；若只在 x86 上跑过，明确标注“未在弱内存模型平台验证”。

对每一段自己写的发布订阅代码，都做同样的动作：标出每个共享读的**值来源**，找出对应的 `sw`，再推出 `hb`。这比背六种内存序的名字有用得多。

---

## 自测题

### 题 1

下面代码中，`assert` 是否一定成立？如果把 `store` 改成 `memory_order_relaxed`，结论如何变化？请用 `sb` / `sw` / `hb` 说明。

```cpp
int data = 0;
std::atomic<bool> ready{false};

void producer() {
    data = 7;
    ready.store(true, std::memory_order_release);
}

void consumer() {
    if (ready.load(std::memory_order_acquire)) {
        assert(data == 7);
    }
}
```

**答案：** 在 release/acquire 版本中，`assert` 成立。推理：`data = 7` sequenced-before release store；consumer 的 acquire load 如果读到 `true`，它读到的正是 producer 的 release store 写入的值，因此 release store synchronizes-with acquire load；acquire load 又 sequenced-before `assert` 中的读取。于是 `hb(data 写, data 读)` 成立，非原子 `data` 的读写不构成数据竞争，读取必然看到 7。

如果把 store 改成 `relaxed`，则没有 `sw` 边：即使 load 读到 `true`，也不建立任何跨线程顺序。`data` 的写和读来自不同线程且无 `hb`，构成数据竞争，程序行为未定义，`assert` 可能失败（也可能“看起来正常”，但那只是碰巧）。

### 题 2

有人说：“只要代码里一个线程用了 `release`，另一个线程用了 `acquire`，就一定有同步。”这句话对吗？请举一个反例并说明 `synchronizes-with` 成立的条件。

**答案：** 不对。`synchronizes-with` 还要求 acquire 读必须读到 release 写所写入的值（或该 release store 主导的 release sequence 中的值）。反例：发布标志初始就是 `true`，读者在发布者执行之前用 acquire 读到初始 `true`，此时它与发布者的 release store 没有读值关系，因此没有 `sw`，也就没有 `hb`，读者访问 payload 可能读到旧值。修复方式是让标志初始为 `false`，或确保读者只会读到发布者写入的目标值（例如使用版本号/序列号，并校验读到的是发布后的版本）。

### 题 3

下面的 Dekker 式代码，如果两个 store 都用 `memory_order_release`、两个 load 都用 `memory_order_acquire`，是否可能出现 `r1 == 0 && r2 == 0`？为什么？`seq_cst` 能避免吗？

```cpp
std::atomic<int> x{0}, y{0};
int r1 = 0, r2 = 0;

void a() {
    x.store(1, MO_STORE);
    r1 = y.load(MO_LOAD);
}

void b() {
    y.store(1, MO_STORE);
    r2 = x.load(MO_LOAD);
}
```

**答案：** 使用 release/acquire 时，**可能**出现双方都读到 0。原因是 release 只阻止之前的访问后移，acquire 只阻止之后的访问前移，二者都不阻止“store 后面跟 load”这种 StoreLoad 重排。两个线程都可能先完成自己的 store，再去读对方的变量，于是都读到 0。

`seq_cst` 能避免。因为所有 `seq_cst` 操作存在一个所有线程都同意的全局单一总顺序，且与 `happens-before` 一致。若 `a` 的 store 在总顺序中先于 `b` 的 store，则 `b` 的 load 一定在 `b` 的 store 之后，也就一定在 `a` 的 store 之后，因此 `r2` 会读到 1；反之 `r1` 会读到 1。所以不可能双方都读到 0。这也是“需要全局顺序的对称协议必须用 `seq_cst`”的典型理由。

---

## 推荐资源

- **C++内存序与发布订阅：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/atomic/memory_order.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：深入
  - 是否免费：是
  - 推荐理由：重点阅读 sequenced-before 和 happens-before 与 release/acquire 与 relaxed，用于核对本节点实验的语义和边界。

- **std::memory_order**
  - 链接：https://en.cppreference.com/cpp/atomic/memory_order
  - 来源：cppreference
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：核对 release/acquire 同步关系成立的条件，避免把内存序当成刷新缓存命令。

---

## 本课小结

1. **sequenced-before 是线程内的先后，happens-before 是跨线程的可见性偏序。** `hb` 由 `sb`、`sw` 和传递闭包共同构成；判断数据竞争，看的就是冲突访问之间有没有 `hb`。
2. **`release` / `acquire` 必须配对，而且要求读值匹配。** acquire 读到 release 写入的值（或 release sequence 中的值）才产生 `synchronizes-with`；读到初值或无关的 relaxed 写，不产生同步。
3. **`relaxed` 只提供原子性和同一原子对象的修改顺序一致性。** 它不建立 `synchronizes-with`，不能用来发布非原子数据；但纯计数、统计场景用它完全正确。
4. **`seq_cst` 额外提供全局单一总顺序。** 它阻止 StoreLoad 重排，适合对称握手、Dekker 式协议，以及你不确定弱序是否够用的路径。
5. **内存序不是“刷新缓存”，而是构造同步边的工具。** 先写出协议需要的 `hb` 链，再选择能提供这些边的最弱内存序。
6. **压力测试未失败不能证明弱内存序正确。** x86 TSO、编译器重排、极小竞态窗口都会掩盖 bug；正确性必须靠模型化推理，压测只用于发现反例。
7. **弱内存序可能提升性能，但应由可说明的协议与测量支持。** 说不清每条边从哪来，就不要用更弱的序。
8. 下一课将在本课基础上继续深入并发与内存模型：把发布订阅、无锁结构与可见性协议放进更完整的工程语境里，讨论如何把这些模型化推理变成可检查、可维护的代码约束。
