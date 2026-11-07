# 第 71/208 课：缓存一致性与伪共享

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 并发与内存模型（内存可见性与状态归属） |
| 前置 | a-memory、e-atomic |
| 难度 | 进阶 |
| 领域 | E |
| 标签 | False Sharing、E、C++ |
| 子分类 | 内存可见性与状态归属 |
| 节点 ID | e-cache |
| 英文名 | False Sharing |

> 本课承接上一课「C++内存序与发布订阅」。上一课的内存模型讲的是**可见性与顺序**：
> 一个线程写下的值，另一个线程凭什么能看到、按什么顺序看到。
> 本课往下钻一层，讲**硬件缓存一致性**如何在同一缓存行上产生额外流量。
> 两者不是替代关系：内存模型定义语言层的契约，缓存一致性解释性能代价。
> 核心结论先放在这里：**没有数据竞争，也可能有伪共享；伪共享是性能问题，不是正确性问题。**

---

## 导读

上一课我们花了很大力气建立 `sequenced-before`、`synchronizes-with`、`happens-before` 这套模型，目的是回答“跨线程的写到底什么时候可见、按什么顺序可见”。那一课的关键词是**正确性**：如果没有正确的同步边，非原子访问就是数据竞争，程序行为未定义。

但并发程序还有另一个维度：**即使完全正确，也可能很慢。** 一个非常典型的例子是：每个线程都在更新自己的计数器，变量彼此独立，代码里没有任何数据竞争，可是多线程一跑，性能却随着线程数增加而急剧下降。原因往往不是锁，也不是原子操作本身，而是这些独立变量恰好落在了同一条缓存行上，硬件被迫在核心之间来回搬运这条线的所有权。这就是**伪共享（False Sharing）**。

放到微信公众号后台的真实场景里：

- **每线程 QPS / 错误计数统计**：每个 worker 线程分别统计自己处理的请求数和错误数。如果这些计数器在内存里紧挨着，线程 A 写自己的 QPS、线程 B 写自己的错误数，硬件却把它们当成同一条缓存行上的写，频繁让对方的缓存副本失效。
- **监控上报计数器数组**：按状态码、接口、渠道维护一组计数器。多个线程并发更新相邻下标的元素，数组元素之间只差 8 字节，极易共享缓存行。
- **线程池 worker 状态数组**：每个 worker 高频更新自己的 `running`、`processed` 等状态。相邻 worker 的状态变量挨在一起时，一个 worker 的写会不断打断另一个 worker 的缓存。
- **无锁队列的 head / tail**：生产者推进 `head`，消费者推进 `tail`。如果这两个索引在同一条缓存行上，生产者和消费者会持续争抢整条线，队列吞吐被硬件一致性流量拖住。

本课要解决的不是“如何让程序正确”，而是“**在已经正确的前提下，如何识别并降低缓存一致性带来的扩展性瓶颈**”。你会学到三个核心要点：缓存行所有权、缓存一致性与内存模型的区别、以及对齐/填充/线程局部聚合这三种工程手段。

---

## 学习目标

### goals

- 能解释缓存行所有权。
- 能独立完成：对比相邻计数器与隔离布局的多线程更新，记录 CPU 型号与线程分布。

### acceptance

- 能解释无数据竞争仍可有伪共享。
- 优化报告包含内存膨胀成本。

---

## 为什么后端要懂

缓存一致性和伪共享看起来像“底层硬件细节”，但它们会直接出现在后端服务的性能曲线里：

1. **识别原子计数器的扩展性瓶颈**：很多人以为把 `int` 换成 `std::atomic<int>` 就万事大吉。原子性解决了正确性，却没有解决缓存行争用。高并发下，多个线程更新同一缓存行上的不同原子变量，仍然可能产生大量一致性流量。
2. **定位“线程越多越慢”的异常现象**：加线程本来应该提高吞吐，结果 QPS 不升反降。如果代码里没有明显锁竞争，就要怀疑伪共享、共享计数器、以及错误的填充策略。
3. **设计可扩展的统计系统**：每线程本地聚合、分片计数、按 CPU 分片、定期汇总，都是后端统计的常见套路。理解伪共享，才知道为什么要这样设计，以及代价是什么。
4. **优化无锁数据结构**：无锁队列、环形缓冲、引用计数、序列锁里，头尾指针或热点字段的布局会直接影响性能。把 `head` 和 `tail` 放在不同缓存行，是很多高性能队列的基本操作。
5. **避免“拍脑袋优化”**：看到伪共享就到处加 `alignas(64)`，可能让工作集膨胀数倍，反而降低缓存命中率。你需要能测量、能计算内存成本、能说明收益边界。
6. **跨平台部署**：不同 CPU 的缓存行大小可能不同，`64` 不是宇宙常数。把某个平台的经验值写死到所有平台，是典型的隐性 bug。

一句话：**懂缓存一致性，你才知道“正确但慢”的并发代码慢在哪里；不懂它，你会在加线程、加原子、加填充之间反复试错。**

---

## 直觉 → 原理

### 1. 缓存行所有权：硬件是按“行”搬运的

#### 从一次写开始

现代 CPU 不会每次读写都直接访问内存。每个核心有自己的 L1/L2 缓存，缓存和内存之间按**缓存行（cache line）**为单位搬运数据。你可以先把缓存行理解成一个固定大小的块，常见是 64 字节，但这不是所有平台都相同。

当核心要读一个变量时，它会把包含这个变量的整条缓存行加载到自己的私有缓存里。当核心要写一个变量时，事情变得更严格：它必须先获得这条缓存行的**所有权（ownership）**，让自己的副本进入可写/独占状态，并让其他核心手里的有效副本**失效（invalidation）**。

用 MESI 类协议的直觉来描述：

- **M（Modified）**：这条线被当前核心修改过，其他核心没有有效副本。
- **E（Exclusive）**：这条线只被当前核心持有，且与内存一致。
- **S（Shared）**：这条线可能被多个核心共享，大家手里都是只读副本。
- **I（Invalid）**：当前核心手里的副本已失效，不能再用。

真实硬件可能是 MESI、MOESI 或各种变体，细节因平台而异。但对本课来说，抓住三个动作就够了：

```text
读：核心把缓存行装入自己的缓存；多个核心可以同时持有 Shared 副本。
写：核心必须先拿到独占权；其他核心的副本被失效。
竞争：另一个核心也要写同一条线时，所有权在核心之间来回转移。
```

#### 伪共享是怎么发生的

假设有两个独立变量 `a` 和 `b`，它们在内存里相邻，落在同一条缓存行上：

```text
缓存行 X：
+--------+--------+------------------------------+
|   a    |   b    |        其他相邻数据          |
+--------+--------+------------------------------+
```

线程 0 只写 `a`，线程 1 只写 `b`。从语言角度看，这是两个不同的对象，两个线程访问的是不同的内存位置，**没有数据竞争**。但从硬件角度看，它们共享同一条缓存行：

```text
t1: 线程 0 写 a  -> 需要独占缓存行 X -> 线程 1 手里的 X 失效
t2: 线程 1 写 b  -> 需要独占缓存行 X -> 线程 0 手里的 X 失效
t3: 线程 0 再写 a -> 再次让线程 1 失效
t4: 线程 1 再写 b -> 再次让线程 0 失效
...
```

缓存行的所有权在核心之间来回“ ping-pong ”，每次写都可能产生一致性流量和缓存未命中。程序的结果完全正确，但性能被这种**并非真正共享同一变量**的争用拖慢了。这就是“伪共享”这个名字的由来：**共享的是缓存行，不是逻辑上的数据。**

需要补充一个边界：如果多个线程只是**读**同一条缓存行，通常不会产生这种失效风暴；伪共享的主要代价来自**至少一方在写**。这也是为什么本课的例子几乎都是“每个线程高频更新自己的计数器”，而不是“每个线程只读同一份配置”。

#### 看一个布局对比

下面的程序不测性能，只把两个布局的地址和大小打印出来，让你直观看到“相邻”和“隔离”的区别：

```cpp
// layout_demo.cpp
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <new>

#if defined(__cpp_lib_hardware_interference_size)
constexpr std::size_t kLine = std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif

static_assert(kLine >= sizeof(std::atomic<long long>),
              "cache line hint is smaller than the counter type");

struct alignas(kLine) Adjacent {
    std::atomic<long long> a{0};
    std::atomic<long long> b{0};
};

struct alignas(kLine) Padded {
    std::atomic<long long> a{0};
    char pad[kLine - sizeof(std::atomic<long long>)];
    std::atomic<long long> b{0};
};

int main() {
    Adjacent x;
    Padded y;

    const auto xb = reinterpret_cast<std::uintptr_t>(&x);
    const auto yb = reinterpret_cast<std::uintptr_t>(&y);
    const auto xa = reinterpret_cast<std::uintptr_t>(&x.a);
    const auto xc = reinterpret_cast<std::uintptr_t>(&x.b);
    const auto ya = reinterpret_cast<std::uintptr_t>(&y.a);
    const auto yc = reinterpret_cast<std::uintptr_t>(&y.b);

    std::printf("cache-line hint = %zu bytes\n", kLine);
    std::printf("Adjacent: sizeof=%zu a_off=%zu b_off=%zu same_line=%s\n",
                sizeof(Adjacent),
                static_cast<std::size_t>(xa - xb),
                static_cast<std::size_t>(xc - xb),
                (xa / kLine == xc / kLine) ? "yes" : "no");
    std::printf("Padded:   sizeof=%zu a_off=%zu b_off=%zu same_line=%s\n",
                sizeof(Padded),
                static_cast<std::size_t>(ya - yb),
                static_cast<std::size_t>(yc - yb),
                (ya / kLine == yc / kLine) ? "yes" : "no");
    return 0;
}
```

```bash
g++ -O2 -pthread -std=c++17 layout_demo.cpp -o layout_demo
./layout_demo
```

你会看到类似这样的结构（具体数值取决于你的平台和 `kLine`）：

```text
cache-line hint = 64 bytes
Adjacent: sizeof=64 a_off=0 b_off=8 same_line=yes
Padded:   sizeof=128 a_off=0 b_off=64 same_line=no
```

这里有一个容易忽略的点：`Adjacent` 被 `alignas(kLine)` 对齐后，`sizeof` 也变成了 `kLine`，但 `a` 和 `b` 仍然在同一条线内。**对齐结构体本身不等于隔离成员；真正让 `b` 离开 `a` 所在行的是中间那段填充或成员级别的对齐。**

#### 为什么叫“伪”

- **真共享（true sharing）**：多个线程确实在访问同一个变量或同一份数据，比如都去 `fetch_add` 同一个全局计数器。这种共享是逻辑上必要的，代价来自真实的争用。
- **伪共享（false sharing）**：多个线程访问的是不同变量，逻辑上不需要共享，但硬件因为缓存行粒度把它们绑在了一起。代价来自“本可以不共享”的一致性流量。

伪共享的关键特征是：**你从代码里看不出共享，但从缓存行布局上看，它们确实共享了。**

---

### 2. 缓存一致性与内存模型：两个不同层次的问题

#### 缓存一致性管的是“同一地址”

缓存一致性（cache coherence）要解决的问题是：

> 对于**同一个内存位置**，所有核心最终要就该位置的写顺序达成一致；一个读不能无缘无故读到比它已经见过的写更旧的值。

MESI 类协议就是为此服务的。它保证同一地址的写不会在核心之间“各说各话”。但请注意它的范围：**一致性通常是按缓存行、按地址维护的，它不负责告诉你不同地址之间的访问顺序。**

#### 内存模型管的是“跨地址的顺序与可见性”

C++ 内存模型（memory model）要解决的问题更广：

- 一个线程对某个地址的写，什么时候、按什么顺序对另一个线程可见；
- 不同地址之间的访问能不能被重排；
- `std::atomic` 的 `relaxed`、`acquire`、`release`、`seq_cst` 各自提供什么保证；
- 什么情况下两个冲突访问构成数据竞争，程序什么时候是未定义行为。

上一课的 `release` / `acquire`、`happens-before`、`seq_cst` 都属于这个层次。它们约束的是**跨地址、跨线程的观察关系**，而不仅仅是“同一地址的写最终会一致”。

可以用一张表区分：

| 维度 | 缓存一致性 | C++ 内存模型 |
| --- | --- | --- |
| 关注对象 | 同一地址/同一缓存行的多核观察 | 跨地址、跨线程的可见性与顺序 |
| 典型机制 | MESI 类协议、失效、所有权转移 | `happens-before`、原子操作、内存序 |
| 保证什么 | 同一位置的写不会各说各话 | 哪些访问有顺序、哪些是数据竞争 |
| 不保证什么 | 不同地址之间的顺序 | 具体硬件缓存行大小与性能 |
| 出问题的表现 | 性能下降、一致性流量 | 读到旧值、数据竞争、未定义行为 |

#### 无数据竞争仍可有伪共享

这是本课最重要的结论之一。看下面这段代码：

```cpp
// relaxed_adjacent.cpp
#include <atomic>
#include <cstddef>
#include <cstdio>
#include <new>
#include <thread>

#if defined(__cpp_lib_hardware_interference_size)
constexpr std::size_t kLine = std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif

struct alignas(kLine) Counters {
    std::atomic<long long> a{0};
    std::atomic<long long> b{0};
};

Counters g;

void bump_a() {
    for (int i = 0; i < 1000000; ++i)
        g.a.fetch_add(1, std::memory_order_relaxed);
}

void bump_b() {
    for (int i = 0; i < 1000000; ++i)
        g.b.fetch_add(1, std::memory_order_relaxed);
}

int main() {
    std::thread t1(bump_a);
    std::thread t2(bump_b);
    t1.join();
    t2.join();

    std::printf("a=%lld b=%lld\n",
                g.a.load(std::memory_order_relaxed),
                g.b.load(std::memory_order_relaxed));
    return 0;
}
```

```bash
g++ -O2 -pthread -std=c++17 relaxed_adjacent.cpp -o relaxed_adjacent
./relaxed_adjacent
```

这段代码的正确性没有问题：

- `a` 和 `b` 是两个不同的原子对象，分别只被一个线程写；
- `relaxed` 足以保证各自的原子性；
- 最终结果一定是 `a=1000000 b=1000000`；
- 两个线程没有访问同一个对象，因此不存在数据竞争。

但是，`a` 和 `b` 在 `Counters` 里相邻，通常落在同一条缓存行上。线程 0 写 `a`、线程 1 写 `b`，硬件仍然会让这条线在两个核心之间来回转移。**程序是对的，但可能很慢。** 这就是“无数据竞争仍可有伪共享”。

反过来也要注意：**缓存一致性不等于数据竞争自由。** 即使硬件保证同一地址的写最终一致，如果你的非原子访问没有正确的 `happens-before`，程序仍然是未定义行为。上一课的发布订阅协议不能因为“缓存会保持一致”就省掉。一致性是硬件行为，数据竞争是语言层规则，两者不能互相替代。

#### 与上一课的联系

把两课放在一起看：

```text
上一课 e-order：
  关注“写什么时候可见、按什么顺序可见”
  工具是 release/acquire、seq_cst、happens-before
  解决的是正确性

本课 e-cache：
  关注“正确之后，为什么多核更新会变慢”
  工具是缓存行布局、对齐、填充、线程局部聚合
  解决的是性能与扩展性
```

一个常见的误区是：既然 `relaxed` 已经能保证独立计数器正确，为什么还会慢？因为 `relaxed` 只放松了**顺序约束**，并没有取消**缓存一致性流量**。你可以用 `relaxed` 避免不必要的栅栏，但无法用内存序参数告诉硬件“这两个变量不在同一条缓存行上”。布局问题只能用布局手段解决。

---

### 3. 对齐、填充与线程局部聚合

#### 手段一：对齐（alignment）

`alignas` 可以指定对象或类型的对齐要求。把热点计数器按缓存行对齐，可以让每个计数器从一条新缓存行开始：

```cpp
#include <atomic>
#include <cstddef>
#include <new>

#if defined(__cpp_lib_hardware_interference_size)
constexpr std::size_t kLine = std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif

struct alignas(kLine) PaddedCounter {
    std::atomic<long long> value{0};
};

PaddedCounter counters[8];
```

因为 `PaddedCounter` 的对齐是 `kLine`，编译器会把它的 `sizeof` 补到 `kLine` 的整数倍，数组里相邻元素的起始地址至少相差 `kLine`。这样每个计数器独占一条缓存行，线程之间就不会因为“别人的变量”而互相失效。

也可以对成员分别对齐：

```cpp
struct Stats {
    alignas(kLine) std::atomic<long long> qps{0};
    alignas(kLine) std::atomic<long long> errors{0};
};
```

这会让 `qps` 和 `errors` 各自从一条缓存行开始，`Stats` 的大小也相应增大。

#### 手段二：填充（padding）

填充是在对象之间显式插入无用字节，把下一个热点字段推到另一条缓存行：

```cpp
struct alignas(kLine) Padded {
    std::atomic<long long> a{0};
    char pad[kLine - sizeof(std::atomic<long long>)];
    std::atomic<long long> b{0};
};
```

`pad` 不参与业务逻辑，只负责“占位”。它和 `alignas` 经常一起使用：`alignas` 保证起始地址对齐，`pad` 保证下一个字段被推到下一条线。实际工程中不一定需要手写 `char pad[...]`，`alignas` 配合结构体自然补齐也能达到类似效果，但理解“填充是在用内存换隔离”很重要。

#### 手段三：`std::hardware_destructive_interference_size`

C++17 在 `<new>` 中提供了：

```cpp
std::hardware_destructive_interference_size
std::hardware_constructive_interference_size
```

前者是“为了避免破坏性干扰，建议的对象间最小偏移”，也就是常说的“缓存行大小提示”；后者是“为了利用建设性干扰，建议放在同一行的最大偏移”。本课主要用前者：

```cpp
#include <atomic>
#include <cstddef>
#include <new>

#if defined(__cpp_lib_hardware_interference_size)
constexpr std::size_t kLine =
    std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif

struct alignas(kLine) IsolatedCounter {
    std::atomic<long long> value{0};
};
```

它比手写 `alignas(64)` 更可移植，但**不是运行时真值**：它是编译期常量，可能与你实际运行的机器不同，也可能被实现选成一个保守值。后文“常见误区”会详细讲这一点。

#### 手段四：线程局部聚合

如果每个请求都去写全局计数器，即使加了填充，所有线程仍然在争同一条缓存行（这是真共享，不是伪共享）。更可扩展的做法是：**每个线程先在自己的局部计数器上累加，定期或不定期汇总到全局。**

```cpp
// thread_local_stats.cpp
#include <atomic>
#include <cstdio>
#include <thread>
#include <vector>

thread_local long long t_qps = 0;
thread_local long long t_errors = 0;

std::atomic<long long> g_qps{0};
std::atomic<long long> g_errors{0};

void on_request(bool ok) {
    ++t_qps;
    if (!ok) ++t_errors;
}

void flush() {
    g_qps.fetch_add(t_qps, std::memory_order_relaxed);
    g_errors.fetch_add(t_errors, std::memory_order_relaxed);
    t_qps = 0;
    t_errors = 0;
}

void worker(int iters) {
    for (int i = 0; i < iters; ++i) {
        on_request(i % 10 != 0);
        if ((i & 1023) == 1023) flush();
    }
    flush();
}

int main() {
    constexpr int kThreads = 8;
    constexpr int kIters = 1000000;

    std::vector<std::thread> ts;
    for (int i = 0; i < kThreads; ++i)
        ts.emplace_back(worker, kIters);
    for (auto& t : ts) t.join();

    std::printf("qps=%lld errors=%lld expected_qps=%lld\n",
                g_qps.load(std::memory_order_relaxed),
                g_errors.load(std::memory_order_relaxed),
                static_cast<long long>(kThreads) * kIters);
    return 0;
}
```

```bash
g++ -O2 -pthread -std=c++17 thread_local_stats.cpp -o thread_local_stats
./thread_local_stats
```

这段代码的关键点：

- `t_qps`、`t_errors` 是 `thread_local`，每个线程有自己的一份，普通 `++` 不构成数据竞争；
- 线程不是每个请求都去碰全局原子变量，而是每 1024 次请求才 `flush` 一次；
- 全局计数器仍然用 `relaxed`，因为这里只需要计数原子性，不需要发布其他数据；
- 最终 `qps` 和 `errors` 在 `join` 之后读取，`join` 本身提供了同步，因此读到的是完整结果。

代价也很明显：**运行过程中全局计数器是滞后的**。如果监控系统每秒抓取一次，看到的可能是上一批 flush 之前的值；线程崩溃时，未 flush 的局部计数会丢失。所以线程局部聚合适合“允许最终一致、允许少量延迟或丢失”的统计，不适合“必须实时精确”的账务数据。

另外，`g_qps` 和 `g_errors` 也是两个相邻的原子变量；如果 flush 频率很高，它们自己也可能发生伪共享。这里的例子假设 flush 相对稀疏，所以先把重点放在“减少共享写的频率”上；如果 flush 本身成为热点，就要再考虑填充、合并计数或继续分片。

#### 手段五：每线程分片后汇总

另一种做法是给每个线程一个独立的分片，分片按缓存行填充，线程只写自己的分片，读取时再求和：

```cpp
struct alignas(kLine) Shard {
    std::atomic<long long> value{0};
};

Shard g_shards[64];

void worker(int id) {
    for (int i = 0; i < 1000000; ++i)
        g_shards[id].value.fetch_add(1, std::memory_order_relaxed);
}

long long total() {
    long long sum = 0;
    for (auto& s : g_shards)
        sum += s.value.load(std::memory_order_relaxed);
    return sum;
}
```

这兼得了“没有伪共享”和“没有同一个全局原子变量的真共享”，代价是：

- 内存按 `kLine * 分片数` 增长；
- 读取总数要遍历所有分片，且读到的不是一个瞬间一致的快照；
- 分片数通常要跟线程数或 CPU 数匹配，线程迁移、线程池扩容都会影响效果。

工程上常见的组合是：**热路径写线程局部分片，监控线程周期性汇总，读取方接受最终一致。** 这就是很多高并发统计系统的核心结构。

---

## 场景示例

### 场景一：每线程 QPS / 错误计数统计

这是最贴近微信公众号后台的例子。每个 worker 处理请求，需要统计自己的 QPS 和错误数。错误写法是把所有 worker 的计数器放在一个数组里，而且不填充：

```cpp
struct WorkerStats {
    std::atomic<long long> qps{0};
    std::atomic<long long> errors{0};
};

WorkerStats g_stats[64];
```

`g_stats[0].qps`、`g_stats[0].errors`、`g_stats[1].qps` 在内存里紧挨着。worker 0 更新自己的 QPS，可能让 worker 1 的缓存副本失效；worker 1 更新错误数，又反过来影响 worker 0。**逻辑上每个 worker 只写自己的数据，硬件上却在共享缓存行。**

更可扩展的写法是线程局部聚合（见上一节的 `thread_local_stats.cpp`），或者给每个 worker 的结构体按缓存行对齐：

```cpp
struct alignas(kLine) WorkerStats {
    std::atomic<long long> qps{0};
    std::atomic<long long> errors{0};
};

WorkerStats g_stats[64];
```

注意：一个 worker 的 `qps` 和 `errors` 在同一个结构体里、同一条缓存行上，通常没问题，因为它们是**同一个线程写的**。伪共享关心的是“不同线程写同一条线”，不是“同一条线上有多个字段”。

### 场景二：监控上报计数器数组

监控系统常常按状态码、接口、渠道维护一个计数器数组：

```cpp
std::atomic<long long> g_status_counters[256];
```

多个线程并发更新相邻下标时，数组元素共享缓存行。优化方式有两种：

1. **只对热点下标做填充**：不是所有 256 个计数器都高频写，只给最热的几个加隔离，避免整体内存膨胀。
2. **按线程分片**：每个线程维护自己的计数数组，上报时汇总。内存是 `线程数 × 数组长度 × 每项大小`，需要算清楚成本。

不要一上来就把 256 个计数器全部填充成 256 条缓存行。如果每个计数器 8 字节、缓存行 64 字节，填充后内存从约 2 KiB 变成 16 KiB；如果计数器有几十万个，膨胀会非常可观，甚至影响缓存和 TLB。

### 场景三：线程池 worker 状态数组

线程池里每个 worker 可能高频更新自己的状态：

```cpp
struct alignas(kLine) WorkerState {
    std::atomic<int> state{0};
    std::atomic<long long> processed{0};
};

WorkerState g_workers[64];
```

每个 worker 只写自己的 `state` 和 `processed`。按缓存行对齐后，worker 之间不会互相失效。这里 `state` 和 `processed` 在同一行是合理的，因为它们由同一个 worker 更新；如果某个字段会被监控线程高频读、而 worker 高频写，那还要进一步考虑读写分离或快照。

### 场景四：无锁队列的 head / tail

上一课的 SPSC 队列里，我们已经在 `head_` 和 `tail_` 上写了 `alignas(64)`。现在可以解释原因了：

```cpp
class SpscRing {
    // ...
    alignas(kLine) std::atomic<std::size_t> head_{0};  // 生产者写
    alignas(kLine) std::atomic<std::size_t> tail_{0};  // 消费者写
};
```

生产者推进 `head`，消费者推进 `tail`。如果两者在同一条缓存行上，生产者的每次推进都会让消费者的缓存副本失效，消费者的每次推进也会让生产者的副本失效。队列的吞吐会被这种所有权来回转移拖慢。把两个索引分到不同缓存行，是高性能队列非常常见的基础优化。

同样要记住：这是性能优化，不是正确性修复。`head` 和 `tail` 之间的发布/获取关系仍然要由上一课的 `release` / `acquire` 保证；填充不能替代内存序。

---

## 常见误区与设计权衡

### 误区 1：没有数据竞争，就没有性能问题

这是本课最核心的误区。`relaxed_adjacent.cpp` 里两个线程分别写两个不同的原子变量，完全没有数据竞争，结果也永远正确，但它们仍然可能因为共享缓存行而变慢。**数据竞争回答“程序是否正确”，伪共享回答“程序是否高效”，两者是不同维度。**

### 误区 2：把 64 字节当成所有平台的缓存行大小

64 字节是很多 x86 平台的常见值，但不是普遍事实。某些 ARM、POWER 或 Apple Silicon 平台的缓存行可能更大，例如 128 字节。如果你在代码里无条件写死 `alignas(64)`，在 128 字节行的平台上，两个“被 64 字节隔开”的对象仍可能落在同一行，伪共享依然存在。

正确做法是：

- 用 `lscpu`、`getconf LEVEL1_DCACHE_LINESIZE`、`/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size` 查询目标机器；
- 用 `std::hardware_destructive_interference_size` 作为编译期提示；
- 在跨平台库中把缓存行大小做成可配置项或运行时探测项，而不是当成语言常量。

### 误区 3：把 `hardware_destructive_interference_size` 当成运行机器的真值

这个常量是**编译期常量**，不是运行时从当前 CPU 读出来的。它可能：

- 与运行机器的实际缓存行大小不同，尤其是在交叉编译、容器迁移、异构集群里；
- 随编译器版本、目标架构、编译选项变化；
- 被标准库实现成一个保守值，而不是精确值；
- 在 ABI 相关用法上触发编译器警告。GCC 12 及以后会通过 `-Winterference-size` 提示：把这个值写进稳定的二进制接口可能不稳定。

所以它是“提示”，不是“事实”。如果你的程序需要精确适配运行机器，应该在启动时读取系统信息，或者把布局参数暴露为配置。

### 误区 4：伪共享是正确性 bug

伪共享不会让计算结果出错，它只会让性能变差。真正会让结果出错的是数据竞争、缺少同步、错误的原子操作。把伪共享当成正确性问题，会导致过度设计；把数据竞争当成性能问题，则会导致线上事故。两者要分开诊断。

### 误区 5：加了填充就一定更快

填充减少争用，但会增大工作集，可能降低缓存利用率。原来 8 字节一个计数器，填充后变成 64 或 128 字节；如果计数器很多，内存和缓存压力都会上升。对于“读多写少”或“很少被并发写”的数据，填充可能完全没有收益，反而浪费缓存。**填充应该只用在真正高频、跨线程写同一缓存行的热点上，并且要用测量证明收益。**

### 误区 6：用了 `thread_local` 就没有任何共享

`thread_local` 消除了对同一个全局变量的争用，但：

- 汇总时仍然要写全局原子变量或汇总结构；
- 线程局部变量的内存布局由实现决定，不保证不同线程的 TLS 块之间按缓存行隔离；
- 线程退出、线程池复用时，局部计数可能需要 flush 或清理；
- 读取方看到的是最终一致或滞后数据，不是实时精确值。

`thread_local` 是减少共享频率的手段，不是“消灭共享”的魔法。

### 误区 7：只改内存序，不改布局

`relaxed`、`acquire`、`release` 约束的是顺序和可见性，不会改变缓存行布局。两个相邻变量该共享缓存行还是共享。**内存序解决正确性，布局解决伪共享，两者不能互相替代。**

### 设计权衡

| 方案 | 适合 | 代价 / 风险 |
| --- | --- | --- |
| 紧凑相邻布局 | 读多写少、顺序扫描、冷数据 | 高频跨线程写时可能伪共享 |
| `alignas` / 填充 | 少量高频热点计数器、队列头尾 | 内存膨胀、工作集变大、可能降低缓存利用率 |
| `thread_local` 聚合 | 每线程高频统计、允许最终一致 | 读数滞后、线程退出可能丢未 flush 数据 |
| 每线程分片 + 汇总 | 高并发计数、监控统计 | 汇总成本、内存随分片数增长、快照不一致 |
| 全局原子计数器 | 低/中并发、需要实时精确 | 高并发下真共享争用，扩展性差 |
| 互斥锁保护多个字段 | 复杂状态、需要一致快照 | 阻塞、上下文切换、死锁风险 |

一句话总结 tradeoff：**填充是用内存换隔离，线程局部聚合是用一致性时效换扩展性。** 选择哪一种，取决于你的读写比例、并发强度、内存预算，以及监控系统能接受多大的延迟。

---

## 动手实践

### 任务：对比相邻计数器与隔离布局的多线程更新

本实践对应 `practice`：对比相邻计数器与隔离布局的多线程更新，记录 CPU 型号与线程分布。验收对应：

- 能解释无数据竞争仍可有伪共享；
- 优化报告包含内存膨胀成本。

#### 步骤 0：记录环境与缓存行信息

先不要急着跑基准。把环境记录下来，否则不同机器、不同绑核方式的结果没有可比性。

```bash
# CPU 型号、核数、线程分布、NUMA
lscpu

# 更紧凑的 CPU / 核 / 节点分布
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE

# 逻辑 CPU 数量
nproc

# 查询 L1 数据缓存行大小（不同系统可能返回 0 或不同值）
getconf LEVEL1_DCACHE_LINESIZE

# 直接读 sysfs
cat /sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size

# 编译器与内核版本
g++ --version
uname -a
```

记录以下字段：

- CPU 型号名称；
- 逻辑 CPU 数、物理核数、每个核的线程数（是否开启超线程）；
- NUMA 节点分布；
- L1D 缓存行大小，以及你使用的查询方式；
- 编译器版本、标准库版本、内核版本。

> 提示：`getconf` 和 sysfs 在某些环境里可能不可用或返回不同结果。以你实际能交叉验证的值为准，并在报告里写清楚来源。

#### 步骤 1：编写基准程序

下面的程序同时测量两种布局：

- `Adjacent`：8 个计数器紧挨着；
- `Padded`：每个计数器独占一条缓存行。

程序接受三个参数：线程数、每线程迭代次数、试验次数；内部取中位数。

```cpp
// false_sharing_bench.cpp
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <thread>
#include <vector>

#if defined(__cpp_lib_hardware_interference_size)
constexpr std::size_t kLine = std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif

constexpr int kMaxThreads = 8;

static_assert(kLine >= sizeof(std::atomic<long long>),
              "cache line hint is smaller than the counter type");

struct Adjacent {
    Adjacent() { reset(); }

    void reset() {
        for (int i = 0; i < kMaxThreads; ++i)
            c[i].store(0, std::memory_order_relaxed);
    }

    std::atomic<long long> c[kMaxThreads];

    std::atomic<long long>& at(int i) { return c[i]; }

    long long total() const {
        long long sum = 0;
        for (int i = 0; i < kMaxThreads; ++i)
            sum += c[i].load(std::memory_order_relaxed);
        return sum;
    }
};

struct alignas(kLine) PaddedSlot {
    std::atomic<long long> value{0};
};

struct Padded {
    PaddedSlot s[kMaxThreads];

    void reset() {
        for (int i = 0; i < kMaxThreads; ++i)
            s[i].value.store(0, std::memory_order_relaxed);
    }

    std::atomic<long long>& at(int i) { return s[i].value; }

    long long total() const {
        long long sum = 0;
        for (int i = 0; i < kMaxThreads; ++i)
            sum += s[i].value.load(std::memory_order_relaxed);
        return sum;
    }
};

template <typename Counters>
double run_once(Counters& counters, int threads, long long iters) {
    counters.reset();

    std::vector<std::thread> ts;
    ts.reserve(threads);

    const auto t0 = std::chrono::steady_clock::now();
    for (int t = 0; t < threads; ++t) {
        ts.emplace_back([&counters, t, iters] {
            auto& c = counters.at(t);
            for (long long i = 0; i < iters; ++i)
                c.fetch_add(1, std::memory_order_relaxed);
        });
    }
    for (auto& th : ts)
        th.join();
    const auto t1 = std::chrono::steady_clock::now();

    return std::chrono::duration<double, std::milli>(t1 - t0).count();
}

template <typename Counters>
double median_ms(Counters& counters, int threads, long long iters, int trials) {
    std::vector<double> samples;
    samples.reserve(trials);
    for (int i = 0; i < trials; ++i)
        samples.push_back(run_once(counters, threads, iters));

    std::sort(samples.begin(), samples.end());
    return samples[samples.size() / 2];
}

int main(int argc, char** argv) {
    int threads = (argc > 1) ? std::atoi(argv[1]) : 4;
    long long iters = (argc > 2) ? std::atoll(argv[2]) : 10000000LL;
    int trials = (argc > 3) ? std::atoi(argv[3]) : 5;

    if (threads < 1 || threads > kMaxThreads || iters <= 0 || trials <= 0) {
        std::fprintf(stderr,
                     "usage: %s [threads 1..%d] [iters>0] [trials>0]\n",
                     argv[0], kMaxThreads);
        return 1;
    }

    std::printf("cache-line hint = %zu B, sizeof(Adjacent)=%zu, sizeof(Padded)=%zu\n",
                kLine, sizeof(Adjacent), sizeof(Padded));

    Adjacent adjacent;
    Padded padded;

    const double ta = median_ms(adjacent, threads, iters, trials);
    const double tp = median_ms(padded, threads, iters, trials);
    const long long expected = static_cast<long long>(threads) * iters;
    const bool ok = adjacent.total() == expected && padded.total() == expected;

    std::printf("threads=%d iters=%lld trials=%d "
                "adjacent=%.3f ms padded=%.3f ms checksum=%s\n",
                threads, iters, trials, ta, tp, ok ? "ok" : "BAD");
    return ok ? 0 : 1;
}
```

```bash
g++ -O2 -pthread -std=c++17 false_sharing_bench.cpp -o false_sharing_bench
```

> 注意：这里的 `Padded` 使用的是编译期 `kLine`。如果运行机器的实际缓存行比它更大，填充布局仍可能不完整；此时应该用运行时查询到的值重新编译，或者把布局参数做成可配置项，再对比一次。

先确认程序能正确运行：

```bash
./false_sharing_bench 4 1000000 3
```

你应该看到 `checksum=ok`。如果这里不是 `ok`，先不要分析性能，先检查计数逻辑和线程数参数。

#### 步骤 2：绑核，控制线程分布

自动调度会让线程在核心之间迁移，导致结果波动。用 `taskset` 把进程绑定到指定 CPU 集合：

```bash
# 1 线程：只允许 CPU 0
taskset -c 0 ./false_sharing_bench 1 10000000 5

# 2 线程：只允许 CPU 0-1
taskset -c 0-1 ./false_sharing_bench 2 10000000 5

# 4 线程：只允许 CPU 0-3
taskset -c 0-3 ./false_sharing_bench 4 10000000 5

# 8 线程：只允许 CPU 0-7
taskset -c 0-7 ./false_sharing_bench 8 10000000 5
```

记录你的线程分布：是每个物理核一个线程，还是两个逻辑线程共享一个物理核？如果开启了超线程，8 个逻辑 CPU 可能只对应 4 个物理核，伪共享和争用的表现会不同。

> 注意：`taskset` 的 CPU 编号要和 `lscpu -e` 对得上。容器或虚拟机里可用的 CPU 集合可能不是从 0 开始连续编号，先看清楚再绑。

#### 步骤 3：不同线程数、多次运行、取中位数

对每个线程数，至少重复 5 次，记录中位数。程序内部已经做了多次试验和中位数，但你仍然可以整体多跑几轮，观察稳定性：

```bash
for t in 1 2 4 8; do
  case $t in
    1) cpus=0 ;;
    2) cpus=0-1 ;;
    4) cpus=0-3 ;;
    8) cpus=0-7 ;;
  esac
  taskset -c "$cpus" ./false_sharing_bench "$t" 10000000 5
done
```

如果你发现相邻布局和填充布局的差距不明显，先检查：

- 线程是否真的绑定到了不同物理核；
- 每线程迭代次数是否足够大，计时是否被启动开销淹没；
- 计数器是否真的落在同一缓存行（用 `layout_demo` 或打印地址确认）；
- 编译器是否把循环优化掉了（计数器是原子操作，通常不会被优化掉，但仍要确认 `checksum=ok`）；
- CPU 频率、睿频、后台负载是否稳定。

#### 步骤 4：用 `perf` 观察一致性流量

`perf stat` 可以看缓存相关计数：

```bash
perf stat -e cache-references,cache-misses,instructions,cycles \
  taskset -c 0-3 ./false_sharing_bench 4 10000000 5
```

`perf c2c` 更适合观察缓存行级别的争用（需要硬件支持、权限和内核配置）：

```bash
perf c2c record -F 1000 -- taskset -c 0-3 ./false_sharing_bench 4 10000000 5
perf c2c report --stdio
```

`perf c2c` 会给出缓存行级别的命中/失效信息，帮助你判断伪共享是否真的发生。不同平台、不同 `perf` 版本的支持程度不同；如果命令不可用，不要强行解读，改用基准耗时和布局信息做判断，并在报告里注明“未使用 `perf c2c`”。

#### 步骤 5：填写优化报告（必须包含内存膨胀成本）

报告模板如下。不要编造数据，运行后填写实际值。

```markdown
# 伪共享优化报告（模板）

## 1. 环境
- CPU 型号（lscpu 的“型号名称”）：______
- 架构、逻辑 CPU 数、物理核数、是否超线程：______
- NUMA 节点分布：______
- L1D 缓存行大小及查询方式（getconf / sysfs）：______
- 编译器版本、标准库版本、内核版本：______
- 绑核方式（taskset）：______

## 2. 方法
- 布局：相邻计数器 vs 每计数器独占缓存行
- 线程数：1 / 2 / 4 / 8
- 每线程迭代次数：______
- 试验次数：______，统计量：中位数
- 是否使用 perf / perf c2c：______

## 3. 结果
| 线程数 | 相邻布局中位耗时 (ms) | 填充布局中位耗时 (ms) | 校验和 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | ______ | ______ | ok/BAD | ______ |
| 2 | ______ | ______ | ok/BAD | ______ |
| 4 | ______ | ______ | ok/BAD | ______ |
| 8 | ______ | ______ | ok/BAD | ______ |

## 4. 内存膨胀成本
- 每个计数器大小：8 B
- 缓存行提示大小 kLine：______ B
- 相邻布局每个计数器占用：8 B
- 填充布局每个计数器占用：kLine = ______ B
- 计数器数量 N：______
- 相邻布局总内存：8N = ______ B
- 填充布局总内存：kLine × N = ______ B
- 膨胀倍数：kLine / 8 = ______ 倍
- 对工作集、L1/L2 缓存、TLB 的影响：______

## 5. 结论
- 是否观察到伪共享：______
- 填充带来的收益是否值得内存成本：______
- 适用边界（线程数、数据规模、读写比例）：______
- 如果不用填充，还有什么替代方案：______
```

内存膨胀成本可以先用公式算清楚，再填入实测数据。例如：

- 若 `N = 1024`、`kLine = 64`：相邻布局约 `8 KiB`，填充布局约 `64 KiB`，膨胀 8 倍；
- 若 `kLine = 128`：填充布局约 `128 KiB`，膨胀 16 倍；
- 若 `N = 1,000,000`：相邻布局约 `8 MB`，`kLine = 64` 时填充布局约 `64 MB`，`kLine = 128` 时约 `128 MB`。

这些是算术结果，不是性能测量；它们用来提醒你：**填充不是免费的，热点越少越适合填充，冷数据和大数组要谨慎。**

#### acceptance 自检

- [ ] 能解释无数据竞争仍可有伪共享：两个线程分别写不同变量，但变量在同一缓存行，硬件仍会来回转移所有权。
- [ ] 能说明伪共享是性能问题而不是正确性问题：结果正确，只是一致性流量和缓存未命中增加。
- [ ] 优化报告包含内存膨胀成本：写出每个计数器从 8 B 变成 `kLine` B，以及 N 个计数器的总内存和膨胀倍数。
- [ ] 能说出至少两种隔离手段：`alignas` / 填充、`thread_local` 聚合、每线程分片。
- [ ] 能说明为什么不能把 64 字节无条件写死为所有平台事实，以及 `hardware_destructive_interference_size` 的局限。
- [ ] 记录 CPU 型号、缓存行大小、线程分布、绑核方式，并能解释这些信息为什么影响结果。

#### sourceStudy

围绕本节点给出的参考文档 `std::hardware_destructive_interference_size`，建议按以下路径核对实现与边界：

1. **定义**：确认它定义在 `<new>` 中，是 C++17 引入的 `constexpr std::size_t`，语义是“避免破坏性干扰的推荐最小偏移”，而不是“当前机器的精确缓存行大小”。
2. **特性测试宏**：检查 `__cpp_lib_hardware_interference_size`（C++17 特性宏）。在标准库未提供该常量时，代码要能回退到可配置值或运行时查询，而不是直接编译失败。
3. **示例**：对照页面上的 `alignas` 用法，确认它如何与结构体布局、数组步长、填充配合；同时用本课的 `layout_demo.cpp` 打印 `sizeof` 和偏移，验证你理解的布局与编译器实际布局一致。
4. **错误处理与边界路径**：页面会说明该常量是实现定义的“提示”，可能不等于实际缓存行大小；要沿着这些注意事项检查：跨编译目标、运行时机器与编译机器不同、容器迁移、ABI 稳定性、以及标准库是否定义了该常量。不要只抄示例，要记录“示例在什么前提下成立”。
5. **编译器版本**：记录你使用的 GCC/Clang 版本，以及标准库是否定义了该常量。GCC 12 及以后会在 ABI 相关用法上通过 `-Winterference-size` 发出警告；理解警告的含义，而不是简单地用 `-Wno-interference-size` 把它关掉。
6. **版本与边界记录**：在实验笔记里写清 C++ 标准版本（C++17）、编译器版本、目标架构、实际运行机器的缓存行大小，以及你使用的回退方案。若代码要跨平台发布，明确说明缓存行参数是编译期提示还是运行时探测。

把“我实际验证过的平台 + 编译器版本 + 布局结果 + 基准结果”写进实验笔记，比背一个 `64` 有用得多。

---

## 自测题

### 题 1

两个线程分别对两个不同的 `std::atomic<long long>` 变量做 `fetch_add(1, std::memory_order_relaxed)`，这两个变量在内存里相邻。程序是否存在数据竞争？是否可能出现伪共享？结果会不会出错？

**答案：** 不存在数据竞争。`a` 和 `b` 是两个不同的原子对象，两个线程分别只访问其中一个，且 `fetch_add` 本身是原子的；最终计数一定正确，不会因为伪共享而出错。

但可能出现伪共享。两个变量相邻，通常落在同一条缓存行上；线程 0 写 `a`、线程 1 写 `b` 时，硬件仍然需要在该缓存行上转移所有权，导致另一核心的副本失效。程序正确，但性能可能随线程数增加而下降。伪共享是性能问题，不是正确性问题。

### 题 2

有人说：“缓存行就是 64 字节，`std::hardware_destructive_interference_size` 就是当前机器的缓存行大小，直接写死就行。”这句话有哪些问题？应该怎样做？

**答案：** 至少有三个问题：

1. 缓存行大小不是所有平台都等于 64 字节。某些 ARM、POWER 或 Apple Silicon 平台可能是 128 字节，甚至同一台机器上不同层级缓存的实现也有差异。无条件写死 64 字节，在更大的缓存行平台上仍可能发生伪共享。
2. `std::hardware_destructive_interference_size` 是编译期常量，不是运行时从当前 CPU 读出来的真值。它可能与运行机器不同，尤其是在交叉编译、容器迁移、异构集群中；标准库实现也可能给出保守值。
3. 它有 ABI 稳定性问题。GCC 12 及以后会在把该常量用于 ABI 相关布局时通过 `-Winterference-size` 警告，提示这个值可能随编译器版本、目标平台或编译选项变化。

正确做法是：用 `lscpu`、`getconf LEVEL1_DCACHE_LINESIZE`、sysfs 等查询目标机器；把 `std::hardware_destructive_interference_size` 当作编译期提示而不是事实；跨平台库把缓存行大小做成可配置或运行时探测项；记录实际平台与编译器版本，不要用一个写死的常数覆盖所有环境。

### 题 3

有同学说：“缓存一致性已经保证所有核心看到相同的写顺序，所以一个线程写 `data`、另一个线程写 `flag` 时，不需要 `release` / `acquire`，读者也不会看到 `flag == true` 但 `data` 还是旧值。”这句话错在哪里？请结合上一课的内存模型说明。

**答案：** 这句话把“缓存一致性”和“内存模型”混为一谈了。

缓存一致性维护的是**同一个地址/同一缓存行**上的多核观察规则：所有核心最终会对该位置的写顺序达成一致。它不负责不同地址之间的顺序。`data` 和 `flag` 是两个不同的地址，缓存一致性不会自动建立它们之间的先后关系。

C++ 内存模型才负责跨地址、跨线程的可见性与顺序。读者要安全地看到“`flag == true` 时 `data` 已经是新值”，必须存在一条 `happens-before` 边：写者先写 `data`，再用 `release` 写 `flag`；读者用 `acquire` 读到 `flag == true`，然后读 `data`。如果只有 `relaxed` 或普通访问，即使硬件让两个地址各自保持一致，编译器和 CPU 仍可能重排，读者可能看到新 `flag` 配旧 `data`；如果 `data` 是非原子的，还可能构成数据竞争，程序行为未定义。

一句话：**一致性保证“同一位置不打架”，内存模型保证“不同位置之间有序可见”。伪共享又是另一个维度：它甚至不涉及顺序错误，只是缓存行层面的性能干扰。**

---

## 推荐资源

- **缓存一致性与伪共享：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/thread/hardware_destructive_interference_size.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：重点阅读缓存行所有权与一致性与内存模型区别，用于核对本节点实验的语义和边界。

---

## 本课小结

1. **缓存行是硬件搬运和一致性维护的基本单位。** 核心写一个变量前必须先获得所在缓存行的所有权，其他核心的副本会被失效；这就是 MESI 类协议的核心直觉。
2. **伪共享是“共享缓存行，不共享逻辑数据”。** 两个线程分别写不同变量，如果变量落在同一缓存行，所有权仍会在核心之间来回转移，产生额外的一致性流量。
3. **伪共享是性能问题，不是正确性问题。** 没有数据竞争、结果完全正确的程序，也可能因为伪共享而扩展性很差；反过来，缓存一致性也不能替代内存模型，不能消除数据竞争。
4. **缓存一致性管同一地址，内存模型管跨地址的顺序与可见性。** 上一课的 `release` / `acquire`、`happens-before`、`seq_cst` 解决的是正确性；本课的布局手段解决的是性能。
5. **对齐和填充用内存换隔离。** `alignas` 让热点对象从独立缓存行开始，填充把下一个热点字段推到另一条线。它们能减少争用，但会增大工作集，可能降低缓存利用率。
6. **缓存行大小不是所有平台都等于 64 字节。** 有的平台是 128 字节；`std::hardware_destructive_interference_size` 是编译期提示，不是运行时真值，还可能触发 ABI 稳定性警告。不要无条件写死。
7. **线程局部聚合和每线程分片用一致性时效换扩展性。** 每个线程先写自己的局部计数，定期汇总到全局，能显著减少共享写；代价是读数滞后、可能丢失未 flush 的数据、以及额外的汇总和内存成本。
8. **优化必须带成本核算。** 每个计数器从 8 字节变成 64 或 128 字节，N 个计数器的内存膨胀是 `kLine × N`；报告里要写清楚收益、内存成本、适用边界，而不是只贴一个“变快了”的结论。
9. 下一课继续在并发与内存模型的主线上深入：把这些硬件层的性能直觉，与语言层的同步协议、可观测性和工程可维护性结合起来，形成可测量、可解释、可回退的并发优化方法。
