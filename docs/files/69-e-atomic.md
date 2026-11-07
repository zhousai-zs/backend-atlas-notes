# 第 69/208 课：原子操作与复合不变量

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 并发与内存模型（内存可见性与状态归属） |
| 前置 | e-race |
| 难度 | 进阶 |
| 领域 | E |
| 标签 | Atomics、E、C++ |
| 子分类 | 内存可见性与状态归属 |
| 节点 ID | e-atomic |
| 英文名 | Atomics |

> 本课承接上一课「死锁、锁顺序与阻塞传播」，也直接建立在「数据竞争」那一课之上。
> 如果说数据竞争讲的是“没同步的并发访问为什么是未定义行为”，那么本课讲的是：
> **怎样用原子操作把单个操作变成不可分割的动作，并让它按指定的内存序参与同步。**
> 同时要牢记一句话：**一个原子操作是原子的，但一组原子操作不是一次事务。**

---

## 导读

在上一课里，我们通过固定锁顺序、缩小临界区、避免持锁阻塞等方式，处理了死锁与阻塞传播问题。那一课的核心矛盾是：锁能保护不变量，但锁会带来等待、顺序依赖和传播风险。于是很多工程师会自然地问：**能不能不用锁？**

原子操作就是这个问题的一半答案。它保证单个 `load`、`store` 或 read-modify-write（RMW）操作不可分割，并且可以携带内存序，参与线程间的同步。它足够轻，足够快，是计数器、状态标志、引用计数、令牌桶、无锁队列里最常见的构件。

但它只解决“单个操作”的问题。后台里真正难的是**复合不变量**：两个字段必须一起变化、检查与更新必须一起完成、状态与数据必须一起发布。多个原子字段各自原子，并不自动组成一个事务。这一点是本课的主线，也是面试和线上事故里最容易翻车的地方。

放到微信公众号后台的真实场景里：

- **文章阅读数**：多台机器、多个线程同时 `+1`，用 `fetch_add` 很自然；但“阅读数不超过某个上限”“阅读数与点赞数满足某个关系”就不是单个 `fetch_add` 能保证的。
- **接口 QPS 限额**：先读剩余令牌，再决定是否放行，再写回。如果“检查”和“递增”分成两个原子操作，两个线程可能都看到“还剩 1”，于是都放行，超限。
- **状态标志发布**：一个 `std::atomic<bool>` 表示“配置已加载”，另一个指针指向配置对象。标志先置 true、指针后写，读者就可能看到 true 却读到空指针。单个原子操作没问题，组合起来却有窗口。
- **无锁协议里的计数器**：引用计数、序列号、版本号，往往需要 CAS 循环才能安全推进。

本课先把默认的 `memory_order_seq_cst` 当作“顺序一致性”的直觉来讲，让你先建立“单个操作原子 + 全局有单一顺序”的模型；内存序的细节、`acquire/release`、`relaxed` 的取舍，留给下一课 **e-order**。

---

## 学习目标

### goals

- 能解释 `load` / `store` 和 RMW（`fetch_add`、`fetch_sub`、`fetch_or`、`exchange`、`compare_exchange` 等）的区别与语义。
- 能独立完成：实现一个限额计数器，先写出“先检查再递增”的错误版本并复现超限，再改成 CAS 循环版本。
- 能说明 `std::atomic<T>` 类型**不一定** lock-free，并会用 `is_lock_free()` / `atomic_is_lock_free()` 检查。
- 能说清原子性、可见性、锁自由是三个不同维度，不能互相替代。
- 能识别“多个原子字段拼成一个逻辑状态”时被破坏的复合不变量，并知道何时该退回锁。

### acceptance

- 能说明 atomic 类型不一定 lock-free，并给出检查方式与平台差异的直觉。
- CAS 失败后能正确处理 `expected` 被更新的语义，写出正确的重试循环，而不是拿旧的 `expected` 死循环。

---

## 为什么后端要懂

原子操作是后端并发编程的“基本粒子”。你几乎每天都在用它，只是有时它藏在库里面：

1. **实现计数**：阅读数、播放数、请求数、错误数、在线连接数。高频、短小、无依赖，原子自增是最合适的工具。用互斥锁也能做，但在热点路径上，原子往往能显著降低开销与阻塞。
2. **状态发布**：服务从“未初始化”到“已初始化”，从“可写”到“只读”，从“旧配置”切换到“新配置”。需要一个所有线程都能安全观察到的标志或指针。
3. **无锁协议**：引用计数、无锁栈、无锁队列、序列锁、乐观读。它们的正确性都建立在单个原子操作与内存序之上。
4. **限流与配额**：令牌桶、滑动窗口计数、单机 QPS 上限。这里最容易出现“检查与更新分离”的竞态。
5. **诊断与可观测性**：很多看似“偶发”的超卖、超限、读到半初始化对象，最后都能追溯到“以为多个原子字段会自动一致”。

一句话：**懂原子，你才知道哪些共享状态可以不用锁，哪些必须用锁；不懂原子，你就会在“看起来是原子的”地方写出复合竞态。**

---

## 直觉 → 原理

### 1. load / store 与 RMW：单个操作不可分割

先把原子类型想成一个“带特殊规则的变量”：

```cpp
#include <atomic>

std::atomic<int> g_count{0};
```

对它的访问有三类：

- **load（读）**：`g_count.load()`，原子地读出当前值。
- **store（写）**：`g_count.store(42)`，原子地写入新值。
- **RMW（read-modify-write，读-改-写）**：`fetch_add(1)`、`fetch_sub(1)`、`fetch_or(mask)`、`fetch_and(mask)`、`exchange(v)`、`compare_exchange_*`。它们把“读旧值、算新值、写回”合成**一个不可分割的原子步骤**。

这个“不可分割”非常关键。对比一下非原子的“读 + 改 + 写”：

```cpp
int x = shared;   // 读
x = x + 1;        // 改
shared = x;       // 写
```

两个线程同时执行，可能都读到 0，都算出 1，都写回 1，最终结果是 1 而不是 2。这就是上一课说的数据竞争。而：

```cpp
std::atomic<int> counter{0};
counter.fetch_add(1);   // 一个 RMW，不可分割
```

两个线程并发执行后，结果一定是 2。因为不存在“中间被插进去”的时刻。

`fetch_add` 返回的是**修改前的旧值**，这一点常被忽略：

```cpp
int old = counter.fetch_add(1);   // old 是加之前的值
int now = counter.load();         // now 是加之后的值
```

RMW 的价值就在于：**即使你不关心旧值，它也能把“读-改-写”作为一个整体完成，不需要你先 load 再 store。**

默认内存序是 `std::memory_order_seq_cst`（顺序一致性）。本课先建立这个直觉：

- 每个原子操作看起来是按某个**全局单一顺序**发生的；
- 所有线程看到的这个顺序是一致的；
- 因此“先发生的原子写”对“后发生的原子读”是可见的。

内存序的精确规则、更弱的序能放宽什么、会带来什么重排，放到 e-order 展开。本课你只需要先记住：**默认序最安全，也最贵；弱序更快，但需要你证明正确性。**

一个最小可编译示例：

```cpp
// counter_basic.cpp
#include <atomic>
#include <iostream>
#include <thread>
#include <vector>

std::atomic<long long> g_reads{0};

void worker() {
    for (int i = 0; i < 100000; ++i) {
        g_reads.fetch_add(1, std::memory_order_seq_cst);
    }
}

int main() {
    constexpr int kThreads = 8;
    std::vector<std::thread> ts;
    for (int i = 0; i < kThreads; ++i) ts.emplace_back(worker);
    for (auto& t : ts) t.join();

    std::cout << "reads = " << g_reads.load() << "\n";
    std::cout << "expected = " << kThreads * 100000 << "\n";
    return 0;
}
```

编译与运行：

```bash
g++ -std=c++17 -pthread -O2 counter_basic.cpp -o counter_basic
./counter_basic
```

结果稳定为 `800000`。如果把 `std::atomic<long long>` 换成普通 `long long`，再用同样的 `g_reads = g_reads + 1`，结果几乎一定小于 800000，这就是数据竞争的直接后果。

> 注意：`fetch_add` 只保证“加”这个动作原子。它不保证“加完之后的业务判断”也原子。这是后面所有坑的根源。

### 2. compare_exchange：CAS 的强版本与弱版本

`compare_exchange` 是原子操作里最强、也最难用好的一个。它做的是“条件写入”：

> 如果当前值等于 `expected`，就把它替换成 `desired` 并返回 `true`；
> 否则**不修改**当前值，把 `expected` 更新为当前实际值，并返回 `false`。

有两个版本：

- `compare_exchange_strong(expected, desired)`：只有在值真的不等于 `expected` 时才失败，**不会伪失败**。
- `compare_exchange_weak(expected, desired)`：允许“伪失败”（spurious failure），即当前值其实等于 `expected`，但仍可能返回 `false`。它通常映射到 LL/SC 架构上的单条指令，在某些平台上更快，**但必须放在循环里重试**。

标准签名（简化）：

```cpp
bool compare_exchange_strong(T& expected, T desired,
                             std::memory_order success,
                             std::memory_order failure);
bool compare_exchange_weak(T& expected, T desired,
                           std::memory_order success,
                           std::memory_order failure);
```

最关键的一条规则，面试和实战都必考：

> **CAS 失败时，`expected` 会被更新为当前实际值。**

所以正确写法通常是循环：

```cpp
std::atomic<int> value{0};

void add_one_cas() {
    int expected = value.load();
    int desired;
    do {
        desired = expected + 1;
    } while (!value.compare_exchange_weak(expected, desired));
    // 失败返回时 expected 已经被更新为最新值，
    // 下一轮直接用新的 expected 重新计算 desired。
}
```

如果你写成这样，就是经典 bug：

```cpp
// 错误：失败后 expected 已经变了，但 desired 没重算
int expected = value.load();
int desired = expected + 1;
while (!value.compare_exchange_weak(expected, desired)) {
    // 死循环或写入错误的值：desired 一直是旧 base+1
}
```

失败时 `expected` 被更新，而 `desired` 还是旧值算出来的。下一轮 CAS 继续拿旧 `desired` 去比，要么一直失败，要么在偶然时刻把值写成一个错误的结果。正确做法一定是：**每轮用最新的 `expected` 重新计算 `desired`。**

为什么需要 `expected` 是左值引用？因为失败时要写回。这意味着你不能传临时量：

```cpp
// value.compare_exchange_strong(0, 1);  // 编译不过：0 不是左值
int exp = 0;
value.compare_exchange_strong(exp, 1);
```

强版本和弱版本怎么选？

- 如果你写的是循环（几乎所有 CAS 用法都是循环），**优先用 weak**，因为伪失败会被循环吸收，且某些平台更快。
- 如果你只做一次 CAS、不打算重试，用 **strong**，因为 weak 的伪失败会让你误判“值不匹配”。
- 语义上，weak 失败后 `expected` 同样会被更新为实际值，不要假设“伪失败时 expected 不变”。

一个把 CAS 用错的典型反例：

```cpp
// 错误：单次 weak CAS，把伪失败当成“值不匹配”
int expected = 0;
bool ok = flag.compare_exchange_weak(expected, 1);
if (!ok && expected == 0) {
    // 这里可能只是伪失败，但被当成别的情况处理
}
```

再强调一次：**CAS 是“比较并交换”，不是“比较并保证成功”。** 它只告诉你这一刻值是否等于你看到的旧值；并发世界里，别人随时可能先改。

### 3. 原子性、锁自由与可见性：三个不同的维度

很多误解来自把这三件事混为一谈。

#### 原子性（atomicity）

单个操作不可分割。`fetch_add` 不会出现“加到一半被看见”的状态。这是 `std::atomic` 的基本承诺。

#### 可见性（visibility）

一个线程写了共享变量，另一个线程能否、以及何时能看到。原子操作配合内存序负责建立 happens-before，从而保证“看到了数据”或“看到了标志”。即使变量本身不是 atomic，只要通过正确的同步（例如 release/acquire，或原子标志 + 适当的序）发布，也能安全可见。

注意：**原子性不自动等于可见性语义上的“顺序”**。一个 `relaxed` 原子操作是原子的，但它不建立跨变量的顺序保证。反过来，互斥锁保护下的普通变量既原子（对临界区而言）又可见，但代价是阻塞。这些细节下一课展开。

#### 锁自由（lock-free）

锁自由指的是：算法整体上不依赖阻塞式锁，至少有一个线程能在有限步内取得进展。**`std::atomic<T>` 类型本身不保证 lock-free。**

标准只保证 `std::atomic_flag` 是 lock-free 的；其它特化是否 lock-free 由实现和平台决定。某些平台上，较大的类型（比如 `std::atomic<struct>`、某些 64 位类型在 32 位平台、或非自然对齐的类型）内部可能用锁或类似机制实现，此时“原子”仍然成立，但“锁自由”不成立。

检查方式：

```cpp
#include <atomic>
#include <iostream>

struct Big {
    long long a;
    long long b;
};

int main() {
    std::atomic<int> ai{0};
    std::atomic<long long> al{0};
    std::atomic<Big> ab{};

    std::cout << std::boolalpha;
    std::cout << "atomic<int>       lock_free = " << ai.is_lock_free() << "\n";
    std::cout << "atomic<long long> lock_free = " << al.is_lock_free() << "\n";
    std::cout << "atomic<Big>       lock_free = " << ab.is_lock_free() << "\n";

    // C 风格接口，可用于运行期查询
    std::cout << "atomic_is_lock_free(&ai) = "
              << std::atomic_is_lock_free(&ai) << "\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 lockfree_check.cpp -o lockfree_check
./lockfree_check
```

在常见的 x86-64 Linux 上，`int` 和 `long long` 通常是 lock-free，而 16 字节的 `Big` 很可能不是（可能用内部锁或 libatomic 辅助）。这不是 bug，是事实。**如果你的无锁算法依赖“必须 lock-free”，就必须在目标平台上用 `is_lock_free()` 验证，而不是假设。**

`is_lock_free()` 是运行期查询（实现可能返回常量），`std::atomic_is_lock_free(ptr)` 也是运行期。标准还提供了 `ATOMIC_INT_LOCK_FREE` 之类的宏，用于编译期粗判。工程上更稳妥的做法是：**在启动时断言/记录，不要凭直觉。**

#### 多个原子字段 ≠ 一个事务

这是本课最重要的一句话。看一个后台里很常见的“配额”状态：

```cpp
struct Quota {
    std::atomic<int> used{0};
    std::atomic<int> limit{100};
};
```

`used` 和 `limit` 各自原子。但业务不变量“`used <= limit`”是**跨字段**的。如果线程 A 在读 `used` 和 `limit` 之间，线程 B 把 `limit` 改小了，A 就可能得出错误结论。每个字段都原子，不等于组合状态一致。

再比如“余额 + 流水号”“状态 + 数据指针”“阅读数 + 点赞数”这类成对字段，如果读者需要同时看到两个字段的一致快照，两个独立原子变量做不到。你需要：

- 一把锁把整组更新包起来；或者
- 把多个字段压进一个足够小、能作为单个原子对象操作的类型（例如用位域打包进一个整数，前提是能 lock-free 且对齐正确）；或者
- 设计一个带版本号/序列号的协议（seqlock 思路），让读者能检测到“读的过程中被改过”并重试。

**没有一种方法是“免费”的。** 这就是 tradeoff。

---

### 场景一：文章阅读数

阅读数是最适合原子操作的场景：高频、只增、无跨字段不变量（单纯计数）。

```cpp
// read_count.cpp
#include <atomic>
#include <cstdint>
#include <iostream>
#include <thread>
#include <vector>

class ArticleStats {
public:
    void on_read() {
        reads_.fetch_add(1, std::memory_order_relaxed);
    }

    std::uint64_t reads() const {
        return reads_.load(std::memory_order_relaxed);
    }

private:
    std::atomic<std::uint64_t> reads_{0};
};

int main() {
    ArticleStats stats;
    constexpr int kThreads = 8;
    constexpr int kPerThread = 200000;
    std::vector<std::thread> ts;
    for (int i = 0; i < kThreads; ++i) {
        ts.emplace_back([&stats] {
            for (int j = 0; j < kPerThread; ++j) stats.on_read();
        });
    }
    for (auto& t : ts) t.join();

    std::cout << "reads = " << stats.reads() << "\n";
    std::cout << "expected = " << kThreads * kPerThread << "\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 read_count.cpp -o read_count
./read_count
```

这里用了 `memory_order_relaxed`：因为我们只关心计数本身的原子性，不需要用它去发布别的数据。`relaxed` 依然是原子的，只是不建立跨线程的顺序约束。**但注意：阅读数一旦要参与“限额”“排行榜快照一致性”等复合判断，就不能只靠 `fetch_add` 了。** 例如“阅读数达到 10 万就触发一次抽奖”，你需要 CAS 或专门的“只触发一次”协议，否则并发下可能触发多次。

### 场景二：接口 QPS 限额（错误版本）

假设单机限流：每秒最多放行 `limit` 个请求，用简单计数器。

**错误写法：先 load 检查，再 store / fetch_add。**

```cpp
// quota_bug.cpp
#include <atomic>
#include <iostream>
#include <thread>
#include <vector>

class BadQuota {
public:
    explicit BadQuota(int limit) : limit_(limit) {}

    bool try_acquire() {
        // 错误：检查与更新是两个独立的原子操作
        if (used_.load(std::memory_order_relaxed) >= limit_) {
            return false;
        }
        // 这里可能已经被其他线程填满
        used_.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    int used() const { return used_.load(std::memory_order_relaxed); }

private:
    const int limit_;
    std::atomic<int> used_{0};
};

int main() {
    constexpr int kLimit = 100;
    constexpr int kThreads = 8;
    constexpr int kAttemptsPerThread = 10000;

    BadQuota quota(kLimit);
    std::atomic<int> granted{0};

    std::vector<std::thread> ts;
    for (int i = 0; i < kThreads; ++i) {
        ts.emplace_back([&] {
            for (int j = 0; j < kAttemptsPerThread; ++j) {
                if (quota.try_acquire()) {
                    granted.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }
    for (auto& t : ts) t.join();

    std::cout << "limit   = " << kLimit << "\n";
    std::cout << "granted = " << granted.load() << "\n";
    std::cout << "used    = " << quota.used() << "\n";
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 quota_bug.cpp -o quota_bug
./quota_bug
```

运行后经常会看到 `granted` 大于 `limit`，例如 `limit = 100` 而 `granted = 103` 甚至更多。原因很直接：

1. 线程 A 读 `used_` 得到 99，判断 `99 < 100`，准备放行；
2. 线程 B 也读 `used_` 得到 99，判断 `99 < 100`，也准备放行；
3. A 执行 `fetch_add`，`used_` 变成 100；
4. B 也执行 `fetch_add`，`used_` 变成 101。

**“检查”和“递增”不是一个原子操作。** 这就是复合不变量被破坏：不变量是 `used_ <= limit_`，但它跨越了两次原子访问。

有人会把 `fetch_add` 放在检查前面：

```cpp
int old = used_.fetch_add(1);
if (old >= limit_) {
    used_.fetch_sub(1);   // 回滚
    return false;
}
return true;
```

这在“只增不减”的简单限额里勉强能用（会短暂超限再回滚，`used_` 的中间值可能超过 limit），但回滚本身又是另一个复合操作，且如果还有别的逻辑读 `used_`，会看到瞬时超限。更糟的是，如果限额逻辑还涉及“回滚时可能被别人抢先消耗”，就可能永远回滚不干净。**这种“先占后还”的写法只适合非常明确、单一写者的场景，不要当成通用方案。**

### 场景三：用 CAS 循环实现限额计数器（正确版本）

正确做法是：把“检查 + 递增”合成一次 CAS 尝试。失败就重来，直到成功或确定超限。

```cpp
// quota_cas.cpp
#include <atomic>
#include <iostream>
#include <thread>
#include <vector>

class CasQuota {
public:
    explicit CasQuota(int limit) : limit_(limit) {}

    bool try_acquire() {
        int expected = used_.load(std::memory_order_relaxed);
        while (expected < limit_) {
            const int desired = expected + 1;
            // 只有当前值仍是 expected 时才写入 desired
            if (used_.compare_exchange_weak(
                    expected, desired,
                    std::memory_order_seq_cst,
                    std::memory_order_relaxed)) {
                return true;
            }
            // 失败：expected 已被更新为当前实际值，循环条件会重新判断
        }
        return false;
    }

    int used() const { return used_.load(std::memory_order_relaxed); }
    int limit() const { return limit_; }

private:
    const int limit_;
    std::atomic<int> used_{0};
};

int main() {
    constexpr int kLimit = 100;
    constexpr int kThreads = 8;
    constexpr int kAttemptsPerThread = 10000;

    CasQuota quota(kLimit);
    std::atomic<int> granted{0};

    std::vector<std::thread> ts;
    for (int i = 0; i < kThreads; ++i) {
        ts.emplace_back([&] {
            for (int j = 0; j < kAttemptsPerThread; ++j) {
                if (quota.try_acquire()) {
                    granted.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }
    for (auto& t : ts) t.join();

    std::cout << "limit   = " << kLimit << "\n";
    std::cout << "granted = " << granted.load() << "\n";
    std::cout << "used    = " << quota.used() << "\n";
    return granted.load() == kLimit ? 0 : 1;
}
```

```bash
g++ -std=c++17 -pthread -O2 quota_cas.cpp -o quota_cas
./quota_cas
```

现在 `granted` 和 `used` 都稳定等于 `kLimit`。关键点：

- `expected` 是循环变量，每一轮都可能是 CAS 失败后被更新的最新值；
- `desired` 每轮根据最新 `expected` 重新计算；
- 循环条件 `expected < limit_` 把“检查”也纳入了重试逻辑：一旦发现已经满了，直接返回 `false`；
- 使用 `weak` + 循环是标准推荐写法。

如果限额需要“释放”配额（比如请求结束归还），就再加一个 `release()`：

```cpp
void release() {
    used_.fetch_sub(1, std::memory_order_release);
}
```

但注意：一旦有“借出 + 归还”，`used_` 的不变量就不只是上限，还涉及“不能减到负数”“总量守恒”。这些是**跨操作**的复合不变量，CAS 循环要重新设计，或者直接考虑用锁 + 条件变量，把语义写清楚。**不是所有计数都适合无锁。**

### 场景四：开关标志发布

后台经常有这样的需求：配置加载完成后，把流量切到新配置。

错误版本（多个独立原子字段，顺序不当）：

```cpp
struct Config {
    int timeout_ms;
    int max_retry;
};

std::atomic<Config*> g_config{nullptr};
std::atomic<bool> g_ready{false};

void publisher_bad() {
    Config* c = load_new_config();
    g_config.store(c, std::memory_order_relaxed);
    g_ready.store(true, std::memory_order_relaxed);  // 顺序反了/序太弱
}

void reader_bad() {
    if (g_ready.load(std::memory_order_relaxed)) {
        Config* c = g_config.load(std::memory_order_relaxed);
        use(*c);   // 可能看到 true 但 c 仍是旧指针或半初始化
    }
}
```

这里至少有两个问题：

1. `g_ready = true` 与 `g_config = c` 是两个独立原子操作，读者可能看到 `true` 却拿到旧/空指针；
2. `relaxed` 不建立跨变量顺序，即使代码顺序看起来对，编译器/CPU 也可能重排。

更安全的发布协议（本课先用 seq_cst 直觉，细节留到 e-order）：

```cpp
void publisher_safe() {
    Config* c = load_new_config();
    g_config.store(c, std::memory_order_seq_cst);
    g_ready.store(true, std::memory_order_seq_cst);
}

void reader_safe() {
    if (g_ready.load(std::memory_order_seq_cst)) {
        Config* c = g_config.load(std::memory_order_seq_cst);
        if (c) use(*c);
    }
}
```

但更推荐的做法是**只用一个原子指针表达整个状态**，避免两个字段之间的窗口：

```cpp
std::atomic<Config*> g_current{nullptr};

void publish(Config* c) {
    g_current.store(c, std::memory_order_seq_cst);
}

Config* current() {
    return g_current.load(std::memory_order_seq_cst);
}
```

指针为空就是“未发布”，指针非空就是“已发布”，状态和负载在**同一个原子对象**里，读者不需要读两个字段再拼。这就是“把复合状态压进一个原子对象”的思路。当然，旧配置的回收（什么时候 delete）又变成一个独立的生命周期问题，需要 RCU/引用计数/延迟回收等机制，不在本课展开。

**结论：能用单个原子对象表达的状态，就别拆成多个原子字段；拆开之后，跨字段不变量就要你自己负责。**

---

### 补充：atomic 不一定 lock-free 的完整示例

```cpp
// atomic_not_lockfree.cpp
#include <atomic>
#include <cstdio>
#include <cstdint>

struct alignas(16) Pair {
    std::uint64_t a;
    std::uint64_t b;
};

int main() {
    std::atomic<Pair> p{};
    Pair zero{0, 0};
    Pair one{1, 1};

    std::printf("sizeof(Pair) = %zu\n", sizeof(Pair));

    Pair expected = zero;
    bool ok = p.compare_exchange_strong(expected, one);
    std::printf("cas ok = %s\n", ok ? "true" : "false");
    std::printf("is_lock_free = %s\n", p.is_lock_free() ? "true" : "false");
    std::printf("atomic_is_lock_free = %s\n",
                std::atomic_is_lock_free(&p) ? "true" : "false");
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 atomic_not_lockfree.cpp -o atomic_not_lockfree
./atomic_not_lockfree
```

在 32 位平台或某些 ABI 上，16 字节的原子对象可能不是 lock-free；此时 `is_lock_free()` 会返回 `false`，内部可能用 libatomic 的锁实现。**这仍然是合法的原子操作，只是不再是“锁自由”。** 如果你的无锁队列依赖它，性能模型和阻塞行为都会变化。

---

## 常见误区与设计权衡

### 误区 1：用多个 atomic 代替一把锁，破坏跨字段不变量

这是本课的核心误区。典型症状：

```cpp
std::atomic<int> balance{0};
std::atomic<int> version{0};

// 期望“余额和版本一起变”，但两个 store 之间永远有窗口
void update(int new_balance) {
    balance.store(new_balance);
    version.fetch_add(1);
}
```

读者可能看到新余额配旧版本，或者旧余额配新版本。两个字段各自原子，组合起来却不一致。**只要读者需要一致快照，多个独立原子就不够。**

### 误区 2：把 CAS 失败当成“值不匹配”而退出

CAS 失败只说明“这一刻值不等于 expected”，`expected` 已被更新为实际值。你应当基于新的 `expected` 重试或判断，而不是用旧值下结论。

### 误区 3：忘记 RMW 返回旧值

`fetch_add` 返回加之前的值。需要新值时要么自己 `+ n`，要么再 `load` 一次（但再 load 可能已经不是那一刻的值）。别把返回值当“加完的值”。

### 误区 4：认为 `std::atomic` 一定 lock-free

前面已说明。`is_lock_free()` 是你的朋友，尤其是跨平台、跨架构、结构体原子。

### 误区 5：以为“原子”等于“线程安全的复合逻辑”

原子只保证单步不可分割。你的业务不变量往往由多步组成。**原子是砖，不是房子。**

### 误区 6：在热点路径上用 CAS 循环硬扛高竞争

CAS 循环在低/中竞争下很好；极高竞争下，大量线程不断失败重试，会浪费 CPU（所谓“活锁式忙等”）。此时一把锁或分片计数（sharded counter）可能更稳。要测量，不要凭感觉。

### 设计权衡

| 方案 | 适合 | 代价 |
| --- | --- | --- |
| 单个原子计数 / 标志 | 小状态、单一不变量、高频 | 无法表达跨字段事务 |
| CAS 循环 | 小状态的“检查并更新” | 高竞争下重试开销、正确性难写 |
| 互斥锁 | 复杂更新、多个字段、需要清晰语义 | 阻塞、死锁风险、上下文切换 |
| 分片计数 | 极高并发热点计数 | 读需要汇总、内存换性能 |
| 单原子打包状态 | 状态与负载可压进一个机器字 | 受类型大小/对齐/lock-free 限制 |

一句话总结 tradeoff：**原子适合小状态；复杂更新用锁可能更清晰可靠。** 不要为了“无锁”三个字，把一个本来用锁十行就能写对、且容易维护的逻辑，改成一个需要严格证明内存序的 CAS 协议。

---

## 动手实践

### 任务：限额计数器从错误到正确

**步骤 1：复现超限。**

1. 把上面的 `quota_bug.cpp` 编译运行，多跑几次：
   ```bash
   g++ -std=c++17 -pthread -O2 quota_bug.cpp -o quota_bug
   for i in $(seq 1 20); do ./quota_bug; done
   ```
2. 观察 `granted` 是否大于 `limit`。记录至少一次超限的数值。
3. 把线程数调大、每线程尝试次数调大，观察超限是否更容易出现（竞争越激烈越明显）。
4. 思考：如果把 `limit` 调到 1，是否更容易看到 `granted > 1`？为什么？

**步骤 2：改成 CAS 循环。**

1. 按 `quota_cas.cpp` 实现 `try_acquire()`。
2. 运行同样的压力测试，确认 `granted == limit` 且 `used == limit`。
3. 把 `compare_exchange_weak` 换成 `compare_exchange_strong`，确认结果仍正确，并思考为什么循环里两者都可以。
4. 故意写一个“失败后不更新 desired”的错误版本，观察它会死循环还是写错值，并解释原因。

**步骤 3：加入释放与边界。**

1. 增加 `release()`，让配额可以归还。
2. 设计测试：先占满，再释放若干，再尝试获取，确认不变量仍然成立。
3. 思考：如果两个线程同时 `release()`，`used_` 会不会减到负数？你需要什么额外约束？
4. 尝试把 `used_` 和 `limit_` 改成普通 `int` + `std::mutex`，比较代码可读性与性能。**这不是倒退，而是权衡。**

**步骤 4：验证 lock-free。**

1. 编译运行 `atomic_not_lockfree.cpp`，记录你的平台上 `atomic<Pair>` 的 `is_lock_free()` 结果。
2. 尝试不同大小/对齐的结构体，观察结果变化。
3. 记录你的编译器版本、平台架构与结果，作为“适用版本及边界条件”。

### acceptance 自检

- [ ] 能说明 `std::atomic<T>` 不一定 lock-free，并说出 `is_lock_free()` 的用法。
- [ ] CAS 失败后能正确利用被更新的 `expected`，循环中用最新值重算 `desired`。
- [ ] 能解释“先检查再递增”为什么在并发下会超限，并能画出两个线程的交错。
- [ ] 能说清“多个原子字段不构成事务”，并给出至少一种修复思路（锁 / 打包成单个原子 / 版本协议）。

### sourceStudy

沿 `https://en.cppreference.com/w/cpp/atomic/atomic.html` 阅读时，建议按以下路径核对：

1. **定义与成员**：`atomic<T>` 的 `load`、`store`、`exchange`、`fetch_add`、`fetch_sub`、`fetch_or`、`fetch_and`、`compare_exchange_weak/strong` 的签名与返回值语义。
2. **`compare_exchange` 的期望更新规则**：重点核对失败时 `expected` 被替换为当前值这一条，和本课代码逐一对照。
3. **`is_lock_free` / `atomic_is_lock_free` / `ATOMIC_*_LOCK_FREE`**：核对它们的适用范围（运行期 vs 编译期宏）、返回值含义、以及“实现可能返回常量”的说明。
4. **内存序参数**：本课默认用 `seq_cst`，先确认每个函数的默认内存序，以及 `success`/`failure` 两个序的约束（例如 `failure` 不能比 `success` 更强）。更细的规则留到 e-order。
5. **错误处理与边界**：核对 `fetch_add` 对整数/指针特化的行为、返回值、以及是否有未定义或实现定义的部分。记录你实际使用的标准版本（C++17）与该页面标注的版本差异。
6. **适用版本**：记录哪些 API 是 C++11 起、哪些是 C++20 起（如果页面有标注），避免在旧编译器上误用。

把“我实际验证过的平台 + 编译器版本 + 结果”写进你的实验笔记，这比背结论更有价值。

---

## 自测题

### 题 1

下面这段代码在并发下能否保证 `counter` 的最终值等于线程数 × 每线程次数？为什么？

```cpp
std::atomic<int> counter{0};

void worker() {
    int cur = counter.load();
    counter.store(cur + 1);
}
```

**答案：** 不能。`load` 和 `store` 是两个独立的原子操作，中间存在窗口。两个线程可能都读到相同的 `cur`，再分别写回 `cur + 1`，导致丢失更新。原子性只保证单步不可分割，不保证“读-改-写”整体原子。应使用 `counter.fetch_add(1)` 或 CAS 循环。

### 题 2

`compare_exchange_weak(expected, desired)` 返回 `false` 时，`expected` 的值是什么？这对循环写法有什么要求？

**答案：** 失败时（包括伪失败），`expected` 会被更新为当前实际值。因此循环中必须基于最新的 `expected` 重新计算 `desired`，并在下一轮继续尝试；不能沿用失败前算出的 `desired`。如果只做一次 CAS 且不想处理伪失败，应使用 `compare_exchange_strong`。

### 题 3

有人说：“我用两个 `std::atomic<int>` 分别存 `used` 和 `limit`，所以配额逻辑是线程安全的。”这句话对吗？请说明理由，并给出至少一种修复方式。

**答案：** 不对。两个字段各自原子只保证单字段访问不可分割，不保证跨字段的一致性。业务不变量 `used <= limit` 需要“读两个字段 + 判断 + 更新”作为一个整体，独立原子字段之间仍有窗口。修复方式：用互斥锁保护整组更新；或把状态压缩进一个可原子操作的类型（如位域打包的整数）；或设计带版本号的协议让读者检测并重试。

---

## 推荐资源

- **原子操作与复合不变量：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/atomic/atomic.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：重点阅读 `load` / `store` 和 RMW 与 `compare_exchange` 强弱版本，用于核对本节点实验的语义和边界。

---

## 本课小结

1. **原子操作保证单个 `load` / `store` / RMW 不可分割。** `fetch_add` 这类 RMW 把“读-改-写”合成一步，因此不会丢失更新。
2. **CAS 是条件写入。** 失败时 `expected` 会被更新为当前值；循环中必须用最新 `expected` 重算 `desired`。有循环优先用 `weak`，只做一次用 `strong`。
3. **原子性、可见性、锁自由是三件事。** `std::atomic<T>` 不一定 lock-free，必须用 `is_lock_free()` / `atomic_is_lock_free()` 在目标平台验证。
4. **多个原子字段不自动组成事务。** “先检查再递增”“状态 + 数据分两个字段发布”都会破坏复合不变量。要么用锁把整组更新包起来，要么把状态压进单个原子对象，要么设计带版本的重试协议。
5. **原子适合小状态，复杂更新用锁可能更清晰可靠。** 选择方案时优先考虑正确性与可维护性，再考虑性能。
6. 下一课 **e-order** 会展开内存序：`relaxed`、`acquire`、`release`、`acq_rel`、`seq_cst` 各自保证什么、能重排什么，以及如何用更弱的序写出正确且更快的同步代码。
