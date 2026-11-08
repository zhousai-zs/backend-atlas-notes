# 第 74/208 课：有界并发队列与背压

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 并发数据结构（有界队列、背压与所有权） |
| 前置 | e-condition、b-move |
| 难度 | 进阶 |
| 领域 | E |
| 标签 | Concurrent Queues、E、C++ |
| 子分类 | 并发数据结构 |
| 节点 ID | e-queue |
| 英文名 | Concurrent Queues |

> 承接说明：前置节点 e-condition 讲条件变量如何用「互斥锁 + 谓词等待」表达“等到某个条件成立”；前置节点 b-move 讲移动语义如何把资源所有权从一个对象转交给另一个对象。本课把两者接起来：用条件变量保护队列的容量与关闭状态，用移动语义把元素安全地送进队列、再交到消费者手里。学完本课，你应该能独立写出一个有界 MPMC 队列，并说清楚它在满、关闭、取消和移动抛异常时分别发生什么。

---

## 导读

先给队列一个够用的定义：

> 并发队列是一个被多个线程同时访问的容器。它不仅要回答“元素怎么放进去、怎么取出来”，还要回答“最多能放多少”“满了怎么办”“关闭以后还能不能取”“取出来的对象归谁”。

这四个问题里，前两个是**可见性与容量**，后两个是**生命周期与所有权**。很多线上事故不是因为队列本身有数据竞争，而是因为这四个问题没有在接口上说清楚：

- 队列无界，生产者比消费者快，内存一直涨，最后 OOM 或触发容器内存限制；
- 队列满了以后生产者继续阻塞，线程池里的 worker 全卡在 `push` 上，连“返回系统繁忙”的线程都没有；
- 服务要优雅退出，`close()` 之后还有生产者往队列里塞任务，或者消费者取到一半不知道“队列已经关闭”还是“暂时没数据”；
- 队列本身线程安全，但入队的是 `shared_ptr` 或裸指针，生产者入队后继续改对象，消费者读到一半状态；
- 元素移动构造抛异常，队列里的元素数量、元素归属和队列状态被破坏。

放到微信公众号后台，这些场景非常具体：

1. **群发消息/模板消息推送高峰**。运营侧一次群发可能产生大量待推送任务，接入层把任务投递给 worker 线程池。如果任务队列无界，推送速度超过下游发送能力时，队列会持续膨胀，内存曲线一路上升，延迟持续升高，最后整台机器被拖垮。有界队列的作用不是“让任务不丢”，而是**让过载在边界上显形**：队列满了，系统必须做一个明确的选择——阻塞、拒绝还是丢弃。
2. **接入层到逻辑层的请求队列**。网关把请求放进逻辑层的队列，逻辑层 worker 消费。队列满时，是让网关线程阻塞等待（把压力反推给上游），还是立刻返回“系统繁忙，请稍后重试”（保留响应性），还是丢弃最旧请求（保新请求）？这不是队列实现能替业务决定的事，但队列必须提供对应的 API，让业务能选。
3. **服务优雅退出**。收到退出信号后，队列里可能还有未处理的任务。`close()` 的语义必须是：不再接受新任务，但已经入队的任务可以被消费者取完；所有阻塞在 `push`/`pop` 上的线程都要被唤醒，否则进程会卡在 `join()` 上，优雅退出变成强制超时。
4. **取消**。上游请求被取消、批次被撤回、服务进入快速失败模式时，队列里剩下的任务可能已经没有意义。这时需要的是“取消并丢弃剩余元素”，而不是“关闭后继续排空”。关闭和取消是两种不同的生命周期终点，接口必须分开。

本课要建立三个核心认识：

1. **SPSC 和 MPMC 是两种不同的并发结构**。单生产单消费可以靠原子下标和 acquire/release 做到无锁；多生产多消费要么用互斥锁把“检查容量 + 入队”变成一个原子步骤，要么用更复杂的无锁算法。不要拿 SPSC 的直觉去写 MPMC。
2. **容量策略就是背压策略**。阻塞 `push` 把压力反推给生产者；`try_push` 失败即拒绝，保留响应性；丢弃最旧/最新则牺牲数据换延迟。三种策略没有绝对优劣，只有业务匹配。
3. **关闭、取消和所有权是接口契约的一部分**。`close()` 是一个线性化点：它之后 `push` 必须失败，`pop` 必须能取完剩余元素再返回“已关闭”；`cancel()` 则丢弃剩余元素。元素通过移动语义进入队列、通过 `unique_ptr` 或移动赋值离开队列，异常路径下队列状态必须保持不变。

---

## 学习目标

### goals

- 能解释 SPSC 与 MPMC 的区别，以及元素写入如何对消费者可见。
- 能独立完成：实现有界 MPMC 队列，覆盖容量 1、取消、关闭及异常元素移动。

### acceptance

- 压力下不丢不重且内存有上限。
- 关闭语义写入接口文档。

把这两条验收翻译成可检查的产物：

- 压力测试里，每个生产者的每个序号都恰好被消费一次，队列的 `size()` 永远不超过 `capacity()`；
- 队列头文件/接口注释里明确写出：`close()` 后 `push` 返回什么、`pop` 在队列空后返回什么、被阻塞的线程如何被唤醒、剩余元素归谁、`cancel()` 与 `close()` 的区别、移动构造抛异常时队列是否仍然可用。

---

## 为什么后端要懂

本节点给出的 why 是：**让线程池在过载时可控而非无界堆积。** 这句话展开就是后端每天都会遇到的三个判断：

### 1. 无界队列会把“过载”变成“内存事故”

线程池的任务队列如果无界，过载时不会立刻报错，而是把压力存进内存。短时间的流量尖峰看起来“扛住了”，实际上延迟在累积、内存在上涨；等到消费者追不上、生产者还在灌入，队列就成了一个不断变大的缓冲区。最终的表现往往不是“队列满”，而是进程 RSS 飙升、分配器变慢、缓存命中率下降，最后 OOM 被杀或触发容器内存限制。有界队列把这种“隐性累积”变成“显性边界”：到容量上限就必须做决策。

### 2. 满队列策略决定系统的失败形态

同样是有界队列，满队列时的策略不同，系统给用户的表现完全不同：

- **阻塞**：生产者等待，背压沿着调用链向上游传。适合上游能等、且你希望“不丢任务”的场景，比如内部批处理。代价是可能把线程池的 worker 全部占住，连健康检查都排不上。
- **拒绝**：`try_push` 立即失败，调用方返回“系统繁忙”。适合面向用户的在线请求，保留响应性，让上游快速重试或降级。代价是调用方必须处理失败，不能假装成功。
- **丢弃**：丢掉最旧或最新的元素。适合日志、指标、可重建的缓存刷新这类“丢一条不影响正确性”的数据。绝不能用在支付、订单、消息必达这类场景。

队列 API 如果不提供这些选择，业务就只能自己用 `size()` 判断，而 `size()` 和随后的 `push` 之间存在竞态——你看到“还有空位”时，别人可能已经填满了。

### 3. 关闭语义决定服务能不能优雅退出

服务收到 `SIGTERM` 后，常见做法是：停止接收新请求、关闭队列、等待 worker 把剩余任务处理完、`join` 所有线程、退出。如果 `close()` 只置一个标志而不 `notify_all`，阻塞在 `pop()` 上的 worker 永远不会醒；如果 `close()` 之后 `push()` 还能成功，生产者会继续制造新任务，排空永远结束不了；如果 `pop()` 在队列空时返回一个无法区分“暂时没数据”和“已关闭”的值，worker 只能靠超时猜。关闭语义写不清楚，优雅退出就会变成“等到超时后强制 kill”。

一句话：**懂有界队列，你才知道过载时系统该慢、该拒还是该丢；不懂它，你只能把线程池配置成一个看起来能扛、实际在积累内存的无底洞。**

---

## 直觉 → 原理

先把本课会反复出现的几个词钉住：

| 术语 | 本课口径 | 关键问题 |
| --- | --- | --- |
| SPSC | Single Producer Single Consumer，单生产者单消费者 | 只有一条线程写、一条线程读，能否不用锁 |
| MPMC | Multiple Producer Multiple Consumer，多生产者多消费者 | 多条线程同时 `push`/`pop`，如何保证不丢不重 |
| 可见性 | 一个线程写入的数据，另一个线程何时能安全读到 | 靠什么建立 happens-before |
| 容量 | 队列最多容纳的元素个数 | 满了阻塞、拒绝还是丢弃 |
| 关闭 | 不再接受新元素，但允许取完剩余元素 | `push`/`pop` 各返回什么，谁被唤醒 |
| 取消 | 不再接受新元素，并丢弃剩余元素 | 与关闭的区别，元素何时析构 |
| 所有权 | 元素从生产者转移到队列、再转移到消费者 | 谁负责析构，异常时归谁 |

---

### 1. SPSC 与 MPMC：元素可见性从哪来

#### 1.1 单生产单消费：原子下标 + acquire/release

SPSC 的关键约束是：**只有一条线程调用 `push`，只有一条线程调用 `pop`。** 在这个约束下，生产者和消费者各自维护一个下标，就不需要互斥锁：

- 生产者只写 `tail_`，消费者只写 `head_`；
- 生产者通过读 `head_` 判断队列是否满；
- 消费者通过读 `tail_` 判断队列是否为空；
- 槽位数据的写入发生在 `tail_` 的 release store 之前，消费者的 acquire load 读到这个 `tail_` 值后，就能看到槽位数据。

下面是一个可编译的 SPSC 环形缓冲（容量为 `N - 1`，留一个空位区分“满”和“空”）：

```cpp
// spsc_ring.cpp
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <thread>

template <typename T, std::size_t N>
class SpscRing {
public:
    static_assert(N >= 2, "N must be at least 2");

    bool try_push(const T& value) {
        const std::size_t tail = tail_.load(std::memory_order_relaxed);
        const std::size_t next = (tail + 1) % N;
        // 消费者通过 head_ 的 release store 发布“这个槽位已经空了”
        if (next == head_.load(std::memory_order_acquire)) {
            return false;  // 满
        }
        buffer_[tail] = value;
        // release：保证 buffer_[tail] 的写入先于 tail_ 的新值被看到
        tail_.store(next, std::memory_order_release);
        return true;
    }

    bool try_pop(T& out) {
        const std::size_t head = head_.load(std::memory_order_relaxed);
        // acquire：读到 tail_ 的新值，就能看到生产者写入的槽位内容
        if (head == tail_.load(std::memory_order_acquire)) {
            return false;  // 空
        }
        out = buffer_[head];
        // release：保证“读走槽位”先于 head_ 的新值被生产者看到
        head_.store((head + 1) % N, std::memory_order_release);
        return true;
    }

private:
    T buffer_[N]{};
    std::atomic<std::size_t> head_{0};  // 消费者写
    std::atomic<std::size_t> tail_{0};  // 生产者写
};

int main() {
    constexpr std::uint64_t kCount = 1'000'000;
    SpscRing<std::uint64_t, 1024> ring;
    std::uint64_t sum = 0;

    std::thread consumer([&] {
        std::uint64_t value = 0;
        std::uint64_t received = 0;
        while (received < kCount) {
            if (ring.try_pop(value)) {
                sum += value;
                ++received;
            } else {
                std::this_thread::yield();
            }
        }
    });

    for (std::uint64_t i = 0; i < kCount; ++i) {
        while (!ring.try_push(i)) {
            std::this_thread::yield();
        }
    }

    consumer.join();
    std::printf("sum=%llu expected=%llu\n",
                static_cast<unsigned long long>(sum),
                static_cast<unsigned long long>(kCount * (kCount - 1) / 2));
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -pthread -O2 spsc_ring.cpp -o spsc_ring
./spsc_ring
```

这段代码里，**可见性不是靠“原子变量自动同步一切”**，而是靠两条 synchronizes-with 关系：

- 生产者 `tail_.store(release)` ↔ 消费者 `tail_.load(acquire)`：消费者读到新的 `tail_` 后，能看到生产者在此之前写入的 `buffer_[tail]`；
- 消费者 `head_.store(release)` ↔ 生产者 `head_.load(acquire)`：生产者读到新的 `head_` 后，能确定槽位已经空出来，不会覆盖消费者还没读完的数据。

`relaxed` 只用在“自己写、自己读”的下标上：生产者读 `tail_` 不需要同步，消费者读 `head_` 也不需要同步；跨线程的“满/空判断”才用 acquire。

这个教学版为了短小，用了 `T buffer_[N]{}`，要求 `T` 可默认构造、可赋值。生产级 SPSC 环形容器还要处理槽位生命周期：用 `std::optional<T>`、`union` + placement new，或者只允许平凡可复制类型；否则“槽位里什么时候有对象、什么时候该析构”会变成新的 bug 来源。本课的重点是下标与可见性，槽位生命周期先按 `int` 理解即可。

#### 1.2 为什么 SPSC 的做法不能直接搬到 MPMC

SPSC 的前提是“只有一个写者、一个读者”。一旦有多个生产者：

- 两个生产者可能同时读到同一个 `tail`，算出同一个 `next`，然后都往同一个槽位写，或者都执行 `tail_.store(next)`，其中一个的写入被覆盖；
- “读 `head_` 判断满 + 写槽位 + 更新 `tail_`”这三步不再是一个原子操作，中间可能被另一个生产者插入。

多个消费者同理：两个消费者可能同时读到同一个 `head`，都取走同一个元素（重复消费），或者都推进 `head_` 导致元素被跳过（丢失）。

要修成 MPMC，有两条路：

1. **用互斥锁把“检查条件 + 修改队列”包成一个临界区**。这是本课实践采用的方案，正确性好写，适合绝大多数业务队列。
2. **用 CAS 循环做无锁 MPMC**。需要处理 ABA、内存回收、槽位生命周期等复杂问题，除非有明确的性能需求，不建议手写。

#### 1.3 MPMC：互斥锁 + 两个条件变量

MPMC 的最小正确实现需要：

- 一把 `std::mutex`，保护队列、容量计数和关闭状态；
- 一个 `not_full_` 条件变量，让生产者在满时等待；
- 一个 `not_empty_` 条件变量，让消费者在空时等待；
- **谓词等待**：`wait(lock, pred)`，被唤醒后重新检查条件，处理虚假唤醒和“醒来发现条件又不成立”的情况。

下面是一个可编译的最小 MPMC 队列（只演示核心语义，完整版在动手实践）：

```cpp
// mpmc_min.cpp
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

class MpmcQueue {
public:
    explicit MpmcQueue(std::size_t capacity) : capacity_(capacity) {}

    bool push(int value) {
        std::unique_lock<std::mutex> lock(mu_);
        not_full_.wait(lock, [this] {
            return closed_ || queue_.size() < capacity_;
        });
        if (closed_) return false;
        queue_.push_back(value);
        not_empty_.notify_one();
        return true;
    }

    bool pop(int& out) {
        std::unique_lock<std::mutex> lock(mu_);
        not_empty_.wait(lock, [this] {
            return closed_ || !queue_.empty();
        });
        if (queue_.empty()) return false;
        out = queue_.front();
        queue_.pop_front();
        not_full_.notify_one();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            closed_ = true;
        }
        not_empty_.notify_all();
        not_full_.notify_all();
    }

private:
    std::size_t capacity_;
    std::mutex mu_;
    std::condition_variable not_full_;
    std::condition_variable not_empty_;
    std::deque<int> queue_;
    bool closed_ = false;
};

int main() {
    MpmcQueue queue(8);
    std::atomic<long long> total{0};

    std::vector<std::thread> producers;
    for (int p = 0; p < 2; ++p) {
        producers.emplace_back([&queue, p] {
            for (int i = 0; i < 1000; ++i) {
                queue.push(p * 1000 + i);
            }
        });
    }

    std::vector<std::thread> consumers;
    for (int c = 0; c < 2; ++c) {
        consumers.emplace_back([&queue, &total] {
            int value = 0;
            while (queue.pop(value)) {
                total.fetch_add(value, std::memory_order_relaxed);
            }
        });
    }

    for (auto& t : producers) t.join();
    queue.close();
    for (auto& t : consumers) t.join();

    const long long expected =
        2LL * (999LL * 1000 / 2) + 1000LL * 1000;  // 0..1999 的和
    std::printf("total=%lld expected=%lld\n", total.load(), expected);
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 mpmc_min.cpp -o mpmc_min
./mpmc_min
```

> 说明：上面 `main` 的期望值只是演示程序的自检；真正的“不丢不重”验证在动手实践里用逐元素计数完成。

这段代码有三个关键点：

1. **`wait` 必须带谓词**。`not_full_.wait(lock, pred)` 等价于“循环检查 `pred`，不成立就等”；它会在被唤醒后重新检查。不要写裸 `wait(lock)` 然后自己 `if`，否则虚假唤醒会让生产者在队列仍满时继续入队。
2. **条件变量只负责“通知”，不负责“记住通知”**。状态（`queue_.size()`、`closed_`）在锁保护下，通知只是提醒等待者“条件可能变了”。如果先 `notify` 再改状态，等待者可能醒来又睡回去，甚至错过通知。正确顺序是：**持锁改状态 → 释放锁（或持锁）→ notify**。
3. **普通路径 `notify_one`，关闭路径 `notify_all`**。入队一个元素只需要唤醒一个消费者；关闭时所有等待的生产者和消费者都必须醒来，因为每个等待者的谓词都变了。

#### 1.4 元素可见性：mutex 和条件变量提供了什么

在 MPMC 版本里，元素写入对消费者的可见性来自 **mutex 的 unlock/lock 形成的 happens-before**：

- 生产者持有 `mu_`，写入 `queue_.push_back(value)`，然后 `mu_` 解锁；
- 消费者随后锁住同一把 `mu_`，读到队列内容；
- 前一次解锁 synchronizes-with 后一次加锁，因此生产者对队列的写入对消费者可见。

条件变量本身不存储数据，它只是等待/通知机制；真正的互斥和可见性由与之配合的 mutex 提供。`notify_one`/`notify_all` 不要求持有锁，但**修改谓词相关状态必须在锁内**，否则会出现“状态改了但通知丢了”或数据竞争。

还有一个容易忽略的点：**队列的线程安全只覆盖队列内部状态，不覆盖元素对象本身。** 如果 `T` 是 `std::shared_ptr<Foo>`，队列保证的是“这个 `shared_ptr` 的拷贝/移动在队列内部安全”，不保证 `Foo` 的内容不被生产者继续修改。这一点在“常见误区”里展开。

---

### 2. 阻塞、丢弃和拒绝：满队列时到底做什么

队列满时，API 必须给出明确选择。常见三种策略：

| 策略 | 典型 API | 对生产者的影响 | 适用场景 | 主要风险 |
| --- | --- | --- | --- | --- |
| 阻塞（背压） | `bool push(T)` | 等待到有空位、关闭或取消 | 上游能等、任务不能丢 | worker 被占满，延迟累积 |
| 拒绝 | `PushResult try_push(T)` | 立即返回 Full/Closed | 在线请求、快速失败 | 调用方必须处理失败 |
| 丢弃 | `drop_oldest` / `drop_newest` | 立即返回，旧/新元素被丢弃 | 日志、指标、可重建缓存 | 数据丢失，不能用于必达业务 |
| 限时阻塞 | `push_for(T, timeout)` | 最多等一段时间 | 既想背压又不想无限等 | 超时后仍要处理失败 |

#### 2.1 三种 API 的返回值设计

一个实用的队列通常同时提供：

```cpp
enum class PushResult { Ok, Full, Closed, Timeout };

bool push(T value);                                      // 阻塞；false = 已关闭/已取消
PushResult try_push(T value);                            // 非阻塞；区分 Ok/Full/Closed
PushResult push_for(T value, std::chrono::milliseconds); // 限时；区分 Ok/Closed/Timeout
```

返回值要能区分“满”和“关闭”，否则调用方无法决定是重试还是放弃。本课的 `try_push` 返回 `PushResult`：`Full` 表示“暂时满，可以稍后重试”，`Closed` 表示“队列已关闭/已取消，不要重试”。如果你沿用只返回 `bool` 的简化版本，调用方只能看到 `false`，无法区分这两种情况；额外查询 `closed()` 也不能消除“查询和操作之间”的竞态，枚举返回值更可靠。

下面是一个可编译的策略演示，容量为 1，专门展示“满时拒绝”“限时超时”“有空位后成功”：

```cpp
// policy_demo.cpp
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <deque>
#include <mutex>

class PolicyQueue {
public:
    enum class Result { Ok, Full, Closed, Timeout };

    explicit PolicyQueue(std::size_t capacity) : capacity_(capacity) {}

    Result try_push(int value) {
        std::lock_guard<std::mutex> lock(mu_);
        if (closed_) return Result::Closed;
        if (queue_.size() >= capacity_) return Result::Full;
        queue_.push_back(value);
        not_empty_.notify_one();
        return Result::Ok;
    }

    Result push_for(int value, std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mu_);
        const bool ready = not_full_.wait_for(lock, timeout, [this] {
            return closed_ || queue_.size() < capacity_;
        });
        if (!ready) return Result::Timeout;
        if (closed_) return Result::Closed;
        queue_.push_back(value);
        not_empty_.notify_one();
        return Result::Ok;
    }

    bool pop(int& out) {
        std::unique_lock<std::mutex> lock(mu_);
        not_empty_.wait(lock, [this] { return closed_ || !queue_.empty(); });
        if (queue_.empty()) return false;
        out = queue_.front();
        queue_.pop_front();
        not_full_.notify_one();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            closed_ = true;
        }
        not_empty_.notify_all();
        not_full_.notify_all();
    }

private:
    std::size_t capacity_;
    std::mutex mu_;
    std::condition_variable not_full_;
    std::condition_variable not_empty_;
    std::deque<int> queue_;
    bool closed_ = false;
};

int main() {
    PolicyQueue queue(1);

    std::printf("try_push(1) = %d\n",
                static_cast<int>(queue.try_push(1)));
    std::printf("try_push(2) = %d (Full)\n",
                static_cast<int>(queue.try_push(2)));
    std::printf("push_for(3, 20ms) = %d (Timeout)\n",
                static_cast<int>(queue.push_for(3, std::chrono::milliseconds(20))));

    int value = 0;
    queue.pop(value);
    std::printf("popped=%d\n", value);

    std::printf("push_for(3, 20ms) = %d (Ok)\n",
                static_cast<int>(queue.push_for(3, std::chrono::milliseconds(20))));
    queue.pop(value);
    std::printf("popped=%d\n", value);
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 policy_demo.cpp -o policy_demo
./policy_demo
```

#### 2.2 容量 1 是边界，不是小事

容量为 1 的队列有两个特点：

- **没有缓冲余量**。只要消费者慢一拍，生产者就会看到“满”。它适合“交接点”语义（生产者把一个任务交给消费者，交接完成前生产者等待），不适合削峰。
- **测试价值高**。很多容量相关的 bug 在容量 1 下最容易暴露：满判断、空判断、`notify_one` 唤醒错对象、关闭时唤醒所有等待者，都能在容量 1 下用很少的元素复现。

在容量 1 下要特别测试：

1. `try_push` 在已有元素时返回 `Full`；
2. `push_for` 在满时返回 `Timeout`，在 `pop` 之后返回 `Ok`；
3. `close()` 之后 `push` 返回 `false`，但已入队的那个元素仍能被 `pop` 取到；
4. 一个生产者阻塞在 `push`、一个消费者阻塞在 `pop` 时，`cancel()` 能否把两者都唤醒。

#### 2.3 用系统工具观察“无界队列”的内存增长

有界队列的容量是代码里的常量或配置；无界队列的“容量”由内存决定。观察一个进程的队列是否在无界堆积，可以用这些命令（具体数值取决于你的程序和机器，本课不编造数字）：

```bash
# 1. 限制进程可用的虚拟内存，让无界增长更早暴露为分配失败
ulimit -v 1048576          # 单位 KB；只在当前 shell 生效
./your_service

# 2. 观察 RSS/VSZ 随时间的变化
ps -o pid,rss,vsz,etime,cmd -p "$(pgrep -n your_service)"

# 3. 持续观察（每 2 秒一次）
watch -n 2 'ps -o pid,rss,vsz,etime,cmd -p '"$(pgrep -n your_service)"

# 4. 看单进程的 top 视图
top -p "$(pgrep -n your_service)"

# 5. 需要更细的内存映射时
pmap -x "$(pgrep -n your_service)" | tail -n 20
```

读法：

- `RSS` 是常驻物理内存，持续上涨通常说明队列/缓存/连接在积累；
- `VSZ` 是虚拟内存，包含尚未触碰的映射，单看它不如 RSS 直观；
- `ulimit -v` 限制的是虚拟内存，设置过小会让正常的 mmap 也失败，测试时要留余量；
- 这些工具只能告诉你“内存在涨”，不能告诉你“是哪个队列在涨”。要定位到具体队列，还需要在代码里给队列的 `size()` 打点，或导出指标。

---

### 3. 关闭线性化与任务所有权

#### 3.1 `close()` 是一个线性化点

“线性化”在这里的意思是：`close()` 看起来是一个瞬间发生的操作，所有并发操作都能按某个顺序排在它之前或之后，不会出现“一半关闭”的状态。

一个正确的 `close()` 语义应该写成：

> `close()` 之后，`push`/`try_push`/`push_for` 必须失败；已经成功入队的元素仍然可以被 `pop` 取完；队列为空后，`pop` 返回 `nullptr`（或明确的“已关闭且为空”）；所有阻塞在 `push`/`pop` 上的线程都被唤醒；`close()` 幂等。

实现要点：

1. 在 `mu_` 保护下把状态从 `Open` 改成 `Closed`；
2. 释放锁后（或持锁）调用 `not_empty_.notify_all()` 和 `not_full_.notify_all()`；
3. 所有等待谓词都要包含“已关闭”这个条件，否则关闭时等待者不会醒；
4. `push` 在等待返回后必须**重新检查状态**，不能因为“被唤醒了”就认为“有空位了”；
5. `pop` 在关闭且队列非空时继续返回元素，在关闭且队列为空时返回“结束”。

为什么关闭时要用 `notify_all` 而不是 `notify_one`？因为关闭改变了**所有**等待者的谓词：每个阻塞的生产者都不能再入队，每个阻塞的消费者都要么取剩余元素、要么看到“已关闭且为空”。只唤醒一个，其余线程可能永远睡下去，`join()` 就卡住了。

#### 3.2 关闭与取消的区别

关闭（close）和取消（cancel）都是“不再接受新元素”，但剩余元素的命运不同：

| 操作 | 新元素 | 已入队元素 | `pop` 行为 | 典型用途 |
| --- | --- | --- | --- | --- |
| `close()` | 拒绝 | 保留，可被取完 | 取完剩余后返回 `nullptr` | 优雅退出、排空 |
| `cancel()` | 拒绝 | 丢弃并析构 | 立即返回 `nullptr` | 快速失败、批次撤回 |

`cancel()` 也必须是线性化点，并且要唤醒所有等待者。一个实现细节：**不要在持锁时析构剩余元素**。元素析构函数可能很慢，甚至可能回调队列或获取其他锁；把剩余元素换到一个局部容器里，释放锁后再让局部容器析构，可以避免“持锁执行用户代码”。

#### 3.3 元素如何进入和离开队列

推荐用移动语义管理元素：

- `push(T value)` 按值接收，调用方 `std::move` 进来；队列内部把 `value` 移动构造到一个节点（如 `std::make_unique<T>(std::move(value))`）；
- `pop()` 返回 `std::unique_ptr<T>`，队列把节点指针移出，所有权转移给消费者；
- 消费者拿到 `unique_ptr` 后，元素的生命周期由消费者控制；消费者处理完，`unique_ptr` 析构，元素销毁。

这样做的原因：

1. **避免拷贝**。大对象、只移类型（`std::unique_ptr`、`std::thread`、`std::promise`）都能入队；
2. **所有权清晰**。元素在队列里时归队列，`pop` 之后归消费者，不会出现“队列和消费者都以为自己该析构”；
3. **异常安全**。把元素移动构造到节点时如果抛异常，节点尚未入队，队列的 `size()` 和内容不变；`unique_ptr` 的移动是 `noexcept`，入队/出队不会因为指针移动而抛异常。

#### 3.4 移动构造抛异常时，队列状态不变

考虑这个顺序：

```text
push(T value):
    等待空位
    检查状态
    auto node = make_unique<T>(std::move(value));   // ← 这里可能抛异常
    queue_.push_back(std::move(node));              // ← unique_ptr 移动，noexcept
```

如果 `T` 的移动构造函数在 `make_unique` 里抛出：

- `node` 没有被构造出来，队列没有插入任何东西；
- 锁由 `unique_lock` 的析构自动释放；
- 队列的 `size()`、元素内容、关闭状态都不变；
- 异常传播给 `push` 的调用方，调用方可以决定重试或放弃。

这就是“强异常保证”的思路：**要么操作成功，要么队列保持操作前的状态。** 注意，`push` 的按值参数在进入函数前可能已经发生了一次移动；如果那次移动抛出，异常发生在函数体之外，队列同样没有被修改。无论哪一次抛出，队列本身都不应该出现“计数加了但元素没进去”或“元素进去了但计数没加”的中间状态。

出队方向的强保证要难一些：如果 `pop(T& out)` 用移动赋值把元素写到调用方的 `out`，而 `T` 的移动赋值抛出，元素可能已经被移走一半。实践里更推荐返回 `std::unique_ptr<T>`，让“出队”只涉及指针移动（`noexcept`），把元素本身的移动/赋值留给消费者在队列外处理。

#### 3.5 一个可编译的关闭语义演示

```cpp
// close_demo.cpp
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <deque>
#include <memory>
#include <mutex>
#include <utility>

template <typename T>
class ClosingQueue {
public:
    explicit ClosingQueue(std::size_t capacity) : capacity_(capacity) {}

    bool push(T value) {
        std::unique_lock<std::mutex> lock(mu_);
        not_full_.wait(lock, [this] {
            return closed_ || queue_.size() < capacity_;
        });
        if (closed_) return false;
        queue_.push_back(std::make_unique<T>(std::move(value)));
        not_empty_.notify_one();
        return true;
    }

    std::unique_ptr<T> pop() {
        std::unique_lock<std::mutex> lock(mu_);
        not_empty_.wait(lock, [this] {
            return closed_ || !queue_.empty();
        });
        if (queue_.empty()) return nullptr;
        std::unique_ptr<T> node = std::move(queue_.front());
        queue_.pop_front();
        not_full_.notify_one();
        return node;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            closed_ = true;
        }
        not_empty_.notify_all();
        not_full_.notify_all();
    }

private:
    std::size_t capacity_;
    std::mutex mu_;
    std::condition_variable not_full_;
    std::condition_variable not_empty_;
    std::deque<std::unique_ptr<T>> queue_;
    bool closed_ = false;
};

int main() {
    ClosingQueue<int> queue(4);

    queue.push(1);
    queue.push(2);
    queue.close();

    std::printf("push after close: %s\n", queue.push(3) ? "true" : "false");

    while (auto value = queue.pop()) {
        std::printf("drained: %d\n", *value);
    }
    std::printf("pop after drain: %s\n", queue.pop() ? "item" : "nullptr");
    return 0;
}
```

```bash
g++ -std=c++17 -pthread -O2 close_demo.cpp -o close_demo
./close_demo
```

预期行为：`push after close` 打印 `false`；`drained` 依次打印 `1`、`2`；最后一次 `pop` 打印 `nullptr`。注意 `pop` 的等待谓词是 `closed_ || !queue_.empty()`：关闭后即使队列为空，谓词也为真，`pop` 不会继续睡，而是走到 `queue_.empty()` 返回 `nullptr`。

---

## 场景示例

### 场景一：群发消息高峰，任务队列无界堆积

运营后台发起一次模板消息群发，接入层把每个 openid 的发送任务投递到 worker 线程池。线程池使用的是无界 `std::deque` + 互斥锁，`push` 永远成功。下游发送接口在高峰期变慢，worker 消费速度下降，但接入层仍然以入口流量持续投递。队列长度持续增长，每个任务对象还带着模板参数、用户信息、重试上下文，内存持续上涨。表现是：接口延迟先升高，然后 GC/分配器压力变大，最后进程被 OOM kill 或触发容器内存限制。

把队列改成有界之后，满队列时业务必须选一种策略：

- 如果是内部批处理、可以等：`push` 阻塞，背压传回接入层，接入层降低拉取速度；
- 如果是在线请求、不能等：`try_push` 失败，立即返回“系统繁忙，请稍后重试”，保护 worker 不被拖死；
- 如果是可丢弃的提醒类消息：丢弃最旧或最新，并记录丢弃计数，保证延迟。

有界队列本身不解决问题，它只是把“无限堆积”变成“在边界上做选择”。选择必须是业务显式做出的，而不是队列实现偷偷替业务决定。

### 场景二：接入层到逻辑层的请求队列满了

网关线程把请求放进逻辑层队列，逻辑层有固定数量的 worker。队列容量是 1024。某次活动流量突增，队列满了：

- 如果网关阻塞在 `push` 上，网关线程被占住，新连接无法接入，健康检查也可能超时；
- 如果网关调用 `try_push`，失败后返回 503/“系统繁忙”，用户可以重试，网关线程保持可用；
- 如果网关丢弃最旧请求，新请求能进来，但被丢弃的请求可能已经给用户返回了“受理成功”，造成状态不一致。

这个场景说明：**满队列策略要和“接口对用户的承诺”一致**。承诺了受理就必须保证入队，承诺了快速响应就允许拒绝，承诺了最终一致才可以考虑丢弃。队列 API 要能让业务表达这些承诺。

### 场景三：优雅退出时队列里剩余任务的归属

服务收到 `SIGTERM`，主线程执行：

```text
停止接收新请求
queue.close()
等待 worker 把剩余任务处理完
join 所有 worker
退出
```

如果 `close()` 只设置 `closed_ = true` 而没有 `notify_all`，阻塞在 `pop()` 上的 worker 不会被唤醒，`join` 永远等不到；如果 `close()` 之后 `push` 还能成功，生产者会继续制造任务，排空永远结束不了；如果 `pop()` 在队列空时返回一个普通值而不是“结束”信号，worker 只能靠超时猜测。正确的关闭语义要把这三件事都写进接口文档，并用测试覆盖。

### 场景四：队列线程安全，但元素被生产者继续修改

队列里放的是 `std::shared_ptr<Request>`。生产者入队后，仍然持有同一个 `shared_ptr`，并在另一个线程里修改 `Request` 的字段；消费者 `pop` 出来读取，读到一半字段被改。队列的互斥锁保护的是 `shared_ptr` 这个指针的拷贝/移动，不保护 `Request` 对象的内容。这是本课 pitfalls 的原话：**队列线程安全不代表其中对象的后续访问安全。**

修复方向：

- 入队后生产者放弃所有权（`std::move(shared_ptr)`，并且不再保留副本）；
- 或者入队的是不可变对象/值拷贝；
- 或者用 `std::unique_ptr` 明确“所有权转移”，让生产者无法继续访问；
- 如果确实需要共享，元素内部自己要有同步，或者用 `shared_mutex` 等机制保护，而不是指望队列的锁。

---

## 常见误区与设计权衡

### 误区 1：队列线程安全，所以元素也安全

这是本课 pitfalls 的核心。队列的互斥锁只保护队列内部结构（链表指针、`size`、头尾下标）以及元素的“移动/拷贝动作”。元素入队后，如果生产者还持有指向同一对象的指针/引用/`shared_ptr` 副本，生产者对对象内容的修改不受队列锁保护，消费者读到的可能是撕裂的、半更新的状态。

具体反例：

```cpp
struct Payload {
    int status = 0;
    std::string body;
};

auto p = std::make_shared<Payload>();
queue.push(p);                 // 队列里存的是 shared_ptr 的副本
p->status = 1;                 // 生产者继续改对象
p->body = "updated";           // 消费者可能同时读
```

修复：`queue.push(std::move(p));` 并确保生产者不再使用 `p`；或者入队 `Payload` 的值/`unique_ptr`；或者把 `Payload` 设计成不可变对象，更新时创建新对象。

### 误区 2：关闭时忘记 `notify_all`

只改 `closed_` 不通知，阻塞的线程不会醒。更隐蔽的是只 `notify_one`：关闭时所有等待者的谓词都变了，只唤醒一个，其余线程可能永远等待。规则：**普通入队/出队用 `notify_one`，关闭/取消用 `notify_all`。**

### 误区 3：在持锁时执行任务

消费者 `pop` 到任务后，如果还在锁里就执行任务（比如发网络请求、写数据库），会把整个队列的吞吐锁死：其他生产者/消费者都在等这把锁。正确做法是：**锁内只做队列操作，锁外执行任务。**

```cpp
// 错误：持锁执行任务
{
    std::unique_lock<std::mutex> lock(mu_);
    not_empty_.wait(lock, pred);
    auto task = std::move(queue_.front());
    queue_.pop_front();
    task->run();              // ← 持锁执行，慢操作阻塞所有人
}

// 正确：锁内取出，锁外执行
std::unique_ptr<Task> task;
{
    std::unique_lock<std::mutex> lock(mu_);
    not_empty_.wait(lock, pred);
    if (queue_.empty()) return;
    task = std::move(queue_.front());
    queue_.pop_front();
    not_full_.notify_one();
}
task->run();                  // ← 锁外执行
```

### 误区 4：用 `size()` 做“能不能 push”的决策

```cpp
if (queue.size() < queue.capacity()) {   // 检查和操作之间有时间窗
    queue.push(value);                    // 可能已经被别人填满
}
```

`size()` 返回的是某一瞬间的快照，检查之后、`push` 之前，其他生产者可能已经填满队列。如果 `push` 是阻塞的，这个检查没有意义；如果 `push` 是非阻塞的，应该直接用 `try_push` 的返回值，而不是先查 `size()`。`size()` 适合监控和调试，不适合做并发决策。

### 误区 5：容量 0 或容量 1 不测试

容量 0 的队列在构造时就应该拒绝（抛 `std::invalid_argument`），否则 `push` 会永远等待。容量 1 是边界：满/空判断、`notify_one` 唤醒、关闭时排空都容易在这里暴露。实践里要专门覆盖容量 1。

### 误区 6：关闭和取消混为一谈

`close()` 的承诺是“排空”，`cancel()` 的承诺是“丢弃”。如果用一个布尔 `closed_` 同时表示两者，消费者无法知道“队列空是因为暂时没数据，还是因为已关闭，还是因为被取消”。至少要有三态：`Open`、`Closed`、`Cancelled`；或者用两个独立的标志并明确优先级。

### 设计权衡

**权衡 1：阻塞施加背压 vs 拒绝保留响应性。** 这是本课 tradeoffs 的原话。阻塞让生产者等待，系统整体变慢但任务不丢；拒绝让生产者快速失败，系统保持响应但调用方要处理失败。选择依据是“任务能不能丢”和“上游能不能等”：

| 业务特征 | 建议策略 | 理由 |
| --- | --- | --- |
| 内部批处理、任务不能丢、上游可降速 | 阻塞 `push` | 背压向上游传播，保护下游 |
| 在线请求、用户可重试、要求快速响应 | `try_push` + 503/繁忙 | 不占住网关线程，失败可重试 |
| 日志/指标/可重建缓存 | 丢弃最旧或最新 + 计数 | 延迟优先，丢数据可接受 |
| 需要有限等待 | `push_for` | 兼顾背压和响应性，超时可降级 |

**权衡 2：全局队列 vs 每生产者队列。** 全局有界队列实现简单、容量统一，但所有生产者/消费者争一把锁，竞争激烈；每生产者一个队列（或分片队列）降低竞争，但容量总和、负载均衡、关闭时要遍历所有队列，复杂度上升。大多数业务先用全局队列，测出锁竞争后再考虑分片。

**权衡 3：有界 vs 无界。** 无界队列实现简单、`push` 永不失败，但把过载风险转成内存风险；有界队列强制业务面对过载，但需要选择策略、处理失败。后端服务默认应该有界，除非你能证明生产速率永远不会超过消费速率（通常不能）。

**权衡 4：`notify_one` vs `notify_all`。** 普通路径 `notify_one` 减少惊群，但前提是“一个元素只需要一个消费者”；关闭/取消必须 `notify_all`。如果队列里一次放入多个元素（批量入队），可能需要 `notify_all` 或按数量通知，否则只唤醒一个消费者，其余元素暂时没人处理。

**权衡 5：锁内构造 vs 锁外构造。** 在锁内 `make_unique` 并移动元素，临界区包含一次可能抛异常/较慢的移动构造；在锁外先构造节点再入队，缩短临界区，但等待期间节点已经占用内存。实践里通常先等待空位，再在锁内构造并入队，保证容量上限严格；如果元素构造很慢，可以考虑锁外构造 + 入队时只移动 `unique_ptr`，但要接受“等待中的生产者各持有一个节点”的内存开销。

---

## 动手实践

### 任务：实现有界 MPMC 队列，覆盖容量 1、取消、关闭及异常元素移动

本实践严格对应节点 `practice`：

- 实现一个有界 MPMC 队列；
- 覆盖容量 1；
- 覆盖取消；
- 覆盖关闭；
- 覆盖“元素移动构造抛异常”的异常路径。

验收对应节点 `acceptance`：

- 压力下不丢不重且内存有上限；
- 关闭语义写入接口文档。

### 步骤 0：准备环境

```bash
g++ --version
```

确认编译器支持 C++17 和 pthread。本实践只依赖标准库，不需要额外库。

### 步骤 1：编写 `queue_lab.cpp`

下面这份代码把“队列实现”和“测试”放在一个文件里。队列用 `std::deque<std::unique_ptr<T>>` 存元素：`unique_ptr` 的移动是 `noexcept`，入队/出队不会因为指针移动而抛异常；元素本身的移动构造只在 `make_unique` 时发生，抛异常时队列尚未插入，状态不变。

```cpp
// queue_lab.cpp —— 第 74 课动手实践：有界 MPMC 队列
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

template <typename T>
class BoundedQueue {
public:
    enum class PushResult { Ok, Full, Closed, Timeout };
    enum class State { Open, Closed, Cancelled };

    explicit BoundedQueue(std::size_t capacity) : capacity_(capacity) {
        if (capacity_ == 0) {
            throw std::invalid_argument("BoundedQueue capacity must be > 0");
        }
    }

    ~BoundedQueue() {
        cancel();
    }

    BoundedQueue(const BoundedQueue&) = delete;
    BoundedQueue& operator=(const BoundedQueue&) = delete;
    BoundedQueue(BoundedQueue&&) = delete;
    BoundedQueue& operator=(BoundedQueue&&) = delete;

    // 阻塞入队：成功返回 true；队列已关闭/已取消返回 false。
    // 元素移动构造抛异常时，队列内容与 size() 不变。
    bool push(T value) {
        std::unique_lock<std::mutex> lock(mu_);
        not_full_.wait(lock, [this] {
            return state_ != State::Open || queue_.size() < capacity_;
        });
        if (state_ != State::Open) return false;
        auto node = std::make_unique<T>(std::move(value));
        queue_.push_back(std::move(node));
        not_empty_.notify_one();
        return true;
    }

    // 非阻塞入队：Full = 暂时满；Closed = 已关闭/已取消；Ok = 成功。
    PushResult try_push(T value) {
        std::unique_lock<std::mutex> lock(mu_);
        if (state_ != State::Open) return PushResult::Closed;
        if (queue_.size() >= capacity_) return PushResult::Full;
        auto node = std::make_unique<T>(std::move(value));
        queue_.push_back(std::move(node));
        not_empty_.notify_one();
        return PushResult::Ok;
    }

    // 限时入队：区分 Ok / Timeout / Closed。
    PushResult push_for(T value, std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mu_);
        const bool ready = not_full_.wait_for(lock, timeout, [this] {
            return state_ != State::Open || queue_.size() < capacity_;
        });
        if (!ready) return PushResult::Timeout;
        if (state_ != State::Open) return PushResult::Closed;
        auto node = std::make_unique<T>(std::move(value));
        queue_.push_back(std::move(node));
        not_empty_.notify_one();
        return PushResult::Ok;
    }

    // 阻塞出队：返回元素所有权；已关闭且为空、或已取消时返回 nullptr。
    std::unique_ptr<T> pop() {
        std::unique_lock<std::mutex> lock(mu_);
        not_empty_.wait(lock, [this] {
            return state_ == State::Cancelled ||
                   state_ == State::Closed ||
                   !queue_.empty();
        });
        if (state_ == State::Cancelled) return nullptr;
        if (queue_.empty()) return nullptr;
        auto node = std::move(queue_.front());
        queue_.pop_front();
        not_full_.notify_one();
        return node;
    }

    // 非阻塞出队：空、已取消返回 nullptr；关闭但仍有元素时返回元素。
    std::unique_ptr<T> try_pop() {
        std::unique_lock<std::mutex> lock(mu_);
        if (state_ == State::Cancelled || queue_.empty()) return nullptr;
        auto node = std::move(queue_.front());
        queue_.pop_front();
        not_full_.notify_one();
        return node;
    }

    // 限时出队：超时、已关闭且为空、已取消都返回 nullptr。
    std::unique_ptr<T> pop_for(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mu_);
        const bool ready = not_empty_.wait_for(lock, timeout, [this] {
            return state_ == State::Cancelled ||
                   state_ == State::Closed ||
                   !queue_.empty();
        });
        if (!ready) return nullptr;
        if (state_ == State::Cancelled) return nullptr;
        if (queue_.empty()) return nullptr;
        auto node = std::move(queue_.front());
        queue_.pop_front();
        not_full_.notify_one();
        return node;
    }

    // 优雅关闭：不再接受新元素；已入队元素仍可被取完；唤醒所有等待者。
    void close() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            if (state_ == State::Open) state_ = State::Closed;
        }
        not_empty_.notify_all();
        not_full_.notify_all();
    }

    // 取消：不再接受新元素；剩余元素被丢弃；唤醒所有等待者。
    void cancel() {
        std::deque<std::unique_ptr<T>> discarded;
        {
            std::lock_guard<std::mutex> lock(mu_);
            state_ = State::Cancelled;
            discarded.swap(queue_);
        }
        not_empty_.notify_all();
        not_full_.notify_all();
        // discarded 在锁外析构，避免在持锁时运行元素的析构函数。
    }

    bool closed() const {
        std::lock_guard<std::mutex> lock(mu_);
        return state_ != State::Open;
    }

    bool cancelled() const {
        std::lock_guard<std::mutex> lock(mu_);
        return state_ == State::Cancelled;
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(mu_);
        return queue_.size();
    }

    std::size_t capacity() const { return capacity_; }

private:
    const std::size_t capacity_;
    mutable std::mutex mu_;
    std::condition_variable not_full_;
    std::condition_variable not_empty_;
    std::deque<std::unique_ptr<T>> queue_;
    State state_ = State::Open;
};

namespace {

void check(bool condition, const char* what) {
    if (!condition) {
        std::fprintf(stderr, "CHECK FAILED: %s\n", what);
        std::abort();
    }
}

struct Item {
    std::uint64_t value;
};

constexpr int kProducers = 4;
constexpr int kConsumers = 4;
constexpr int kPerProducer = 20000;
constexpr int kTotal = kProducers * kPerProducer;

// 压力测试：每个元素的 value 全局唯一，消费者统计每个 value 被消费的次数。
// 期望：总数等于 kTotal，且每个 value 恰好一次 —— 不丢不重。
void test_mpmc_stress() {
    BoundedQueue<Item> queue(64);
    std::unique_ptr<std::atomic<int>[]> seen(new std::atomic<int>[kTotal]);
    for (int i = 0; i < kTotal; ++i) {
        seen[i].store(0, std::memory_order_relaxed);
    }

    std::atomic<int> consumed{0};
    std::vector<std::thread> consumers;
    for (int c = 0; c < kConsumers; ++c) {
        consumers.emplace_back([&] {
            while (auto item = queue.pop()) {
                check(item->value < static_cast<std::uint64_t>(kTotal),
                      "item value in range");
                seen[item->value].fetch_add(1, std::memory_order_relaxed);
                consumed.fetch_add(1, std::memory_order_relaxed);
            }
        });
    }

    std::vector<std::thread> producers;
    for (int p = 0; p < kProducers; ++p) {
        producers.emplace_back([&, p] {
            for (int i = 0; i < kPerProducer; ++i) {
                Item item{static_cast<std::uint64_t>(p * kPerProducer + i)};
                check(queue.push(std::move(item)), "push before close");
            }
        });
    }

    for (auto& t : producers) t.join();
    queue.close();
    for (auto& t : consumers) t.join();

    check(consumed.load() == kTotal, "consumed count equals total");
    for (int i = 0; i < kTotal; ++i) {
        check(seen[i].load() == 1, "each item consumed exactly once");
    }
    check(queue.size() == 0, "queue empty after drain");
    std::printf("[stress] producers=%d consumers=%d items=%d: no loss, no duplicate\n",
                kProducers, kConsumers, kTotal);
}

// 容量 1：满时 try_push 返回 Full、push_for 超时；pop 之后 push_for 成功。
void test_capacity_one() {
    BoundedQueue<int> queue(1);
    check(queue.push(1), "push 1");
    check(queue.try_push(2) == BoundedQueue<int>::PushResult::Full,
          "try_push returns Full when full");
    check(queue.push_for(2, std::chrono::milliseconds(20)) ==
              BoundedQueue<int>::PushResult::Timeout,
          "push_for times out when full");

    auto first = queue.pop();
    check(first && *first == 1, "pop 1");
    check(queue.push_for(2, std::chrono::milliseconds(20)) ==
              BoundedQueue<int>::PushResult::Ok,
          "push_for succeeds after pop");
    auto second = queue.pop();
    check(second && *second == 2, "pop 2");
    check(queue.size() == 0, "empty after capacity-1 test");
    std::printf("[capacity1] full -> reject/timeout; after pop -> ok\n");
}

// 关闭：push 失败，但已入队的 1、2 仍可被取完，之后 pop 返回 nullptr。
void test_close() {
    BoundedQueue<int> queue(4);
    check(queue.push(1), "push 1");
    check(queue.push(2), "push 2");

    queue.close();
    check(!queue.push(3), "push after close fails");
    check(queue.try_push(3) == BoundedQueue<int>::PushResult::Closed,
          "try_push after close returns Closed");
    check(queue.push_for(3, std::chrono::milliseconds(10)) ==
              BoundedQueue<int>::PushResult::Closed,
          "push_for reports closed");

    auto first = queue.pop();
    auto second = queue.pop();
    check(first && *first == 1, "drain 1");
    check(second && *second == 2, "drain 2");
    check(queue.pop() == nullptr, "pop returns nullptr after drain");
    check(queue.size() == 0, "empty after close test");
    std::printf("[close] push rejected; remaining items drained; then nullptr\n");
}

// 取消唤醒阻塞的消费者。
void test_cancel_wakes_blocked_consumer() {
    BoundedQueue<int> queue(1);
    std::atomic<bool> done{false};

    std::thread consumer([&] {
        auto value = queue.pop();
        check(value == nullptr, "blocked consumer sees cancel");
        done.store(true, std::memory_order_relaxed);
    });

    std::this_thread::sleep_for(std::chrono::milliseconds(10));
    queue.cancel();
    consumer.join();
    check(done.load(std::memory_order_relaxed), "consumer woke up");
    check(queue.pop() == nullptr, "pop after cancel returns nullptr");
    std::printf("[cancel/consumer] blocked consumer woken by notify_all\n");
}

// 取消唤醒阻塞的生产者，并丢弃队列中剩余元素。
void test_cancel_wakes_blocked_producer() {
    BoundedQueue<int> queue(1);
    check(queue.push(1), "fill capacity 1");
    std::atomic<bool> done{false};

    std::thread producer([&] {
        const bool ok = queue.push(2);
        check(!ok, "blocked producer sees cancel");
        done.store(true, std::memory_order_relaxed);
    });

    std::this_thread::sleep_for(std::chrono::milliseconds(10));
    queue.cancel();
    producer.join();
    check(done.load(std::memory_order_relaxed), "producer woke up");
    check(queue.size() == 0, "cancel discards queued items");
    std::printf("[cancel/producer] blocked producer woken; queued item discarded\n");
}

// 移动构造在第二次移动时抛异常：第一次是进入 push 参数，第二次是进入节点。
// 期望：异常传播，队列 size() 仍为 0，随后仍可正常使用。
struct ThrowingMove {
    static int moves_before_throw;

    int id;

    explicit ThrowingMove(int value) : id(value) {}

    ThrowingMove(const ThrowingMove&) = delete;
    ThrowingMove& operator=(const ThrowingMove&) = delete;

    ThrowingMove(ThrowingMove&& other) : id(other.id) {
        if (moves_before_throw > 0 && --moves_before_throw == 0) {
            throw std::runtime_error("move constructor boom");
        }
        other.id = -1;
    }

    ThrowingMove& operator=(ThrowingMove&& other) {
        if (moves_before_throw > 0 && --moves_before_throw == 0) {
            throw std::runtime_error("move assignment boom");
        }
        id = other.id;
        other.id = -1;
        return *this;
    }
};

int ThrowingMove::moves_before_throw = 0;

void test_throwing_move() {
    BoundedQueue<ThrowingMove> queue(2);
    ThrowingMove value(7);

    ThrowingMove::moves_before_throw = 2;
    bool threw = false;
    try {
        queue.push(std::move(value));
    } catch (const std::runtime_error&) {
        threw = true;
    }

    check(threw, "move constructor threw");
    check(queue.size() == 0, "queue unchanged after throwing move");
    check(queue.try_push(ThrowingMove(8)) ==
              BoundedQueue<ThrowingMove>::PushResult::Ok,
          "queue still usable");
    auto recovered = queue.pop();
    check(recovered && recovered->id == 8, "pop recovered element");
    std::printf("[throwing-move] element not enqueued; queue remains usable\n");
}

}  // namespace

int main() {
    test_mpmc_stress();
    test_capacity_one();
    test_close();
    test_cancel_wakes_blocked_consumer();
    test_cancel_wakes_blocked_producer();
    test_throwing_move();
    std::printf("all tests passed\n");
    return 0;
}
```

### 步骤 2：编译与运行

```bash
g++ -std=c++17 -pthread -O2 -Wall -Wextra queue_lab.cpp -o queue_lab
./queue_lab
```

预期输出形如：

```text
[stress] producers=4 consumers=4 items=80000: no loss, no duplicate
[capacity1] full -> reject/timeout; after pop -> ok
[close] push rejected; remaining items drained; then nullptr
[cancel/consumer] blocked consumer woken by notify_all
[cancel/producer] blocked producer woken; queued item discarded
[throwing-move] element not enqueued; queue remains usable
all tests passed
```

这里没有给出“每秒多少条”的性能数字：不同机器、不同优化等级、不同标准库实现的差异很大，编造一个数字没有意义。本实践关心的是**正确性**和**边界行为**，不是吞吐排名。

### 步骤 3：用 ThreadSanitizer 再跑一遍

```bash
g++ -std=c++17 -pthread -O1 -g -fsanitize=thread queue_lab.cpp -o queue_lab_tsan
./queue_lab_tsan
```

ThreadSanitizer 会报告它检测到的数据竞争。注意：**TSan 干净不等于逻辑正确**。队列的互斥锁让 TSan 看不到竞争，但“关闭后还能不能 push”“取消后剩余元素归谁”“移动抛异常后队列是否一致”这些是接口语义问题，TSan 不会替你检查。正确性要靠压力测试的计数、关闭/取消测试和异常测试来验证。

如果 TSan 报告了竞争，优先检查：

- 是否有共享状态在锁外被读写（例如测试里的 `done` 用了普通 `bool` 而不是 `std::atomic<bool>`）；
- 是否在 `pop` 之后仍访问队列内部元素；
- 是否在生产者入队后继续修改元素对象（本课 pitfalls）。

### 步骤 4：把关闭语义写进接口文档

验收要求“关闭语义写入接口文档”。下面是一份可以直接放在头文件里的注释示例，字段与实现一一对应：

```cpp
/**
 * BoundedQueue<T> —— 有界 MPMC 队列
 *
 * 线程安全：
 *   - 所有 public 成员函数可被多个线程并发调用；
 *   - 队列内部由 std::mutex 保护，元素通过 std::unique_ptr 转移所有权。
 *
 * 容量：
 *   - 构造时固定，capacity() 返回该值，且必须 > 0；
 *   - 任意时刻 size() <= capacity()；
 *   - 满时 push 阻塞，try_push 返回 PushResult::Full，push_for 可能返回 Timeout。
 *
 * 关闭（close）：
 *   - close() 是线性化点：调用之后 push 返回 false，
 *     try_push/push_for 返回 PushResult::Closed；
 *   - 已成功入队的元素仍可被 pop/try_pop/pop_for 取出；
 *   - 队列为空后，pop/try_pop/pop_for 返回 nullptr；
 *   - close() 幂等；会 notify_all 唤醒所有阻塞的生产者和消费者；
 *   - 关闭后队列不会自动析构元素，元素在 pop 后归消费者，
 *     或在队列析构时随队列一起销毁。
 *
 * 取消（cancel）：
 *   - cancel() 之后不再接受新元素：push 返回 false，
 *     try_push/push_for 返回 PushResult::Closed；
 *   - 队列中剩余元素被丢弃并在锁外析构；
 *   - pop/try_pop/pop_for 立即返回 nullptr；
 *   - cancel() 幂等，且优先于 close()：先 close 再 cancel 会丢弃剩余元素；
 *   - cancel() 会 notify_all 唤醒所有阻塞的生产者和消费者。
 *
 * 所有权：
 *   - push 成功后，元素归队列所有；
 *   - pop 返回 std::unique_ptr<T>，所有权转移给调用方；
 *   - 返回 nullptr 表示：已取消，或已关闭且队列为空，或限时等待超时
 *     （pop_for 无法区分这三者，需要区分时请结合 cancelled()/closed() 判断）。
 *
 * 异常：
 *   - push/try_push/push_for 在元素移动构造抛出时，队列内容与 size() 不变，
 *     异常传播给调用方；队列仍可继续使用；
 *   - pop/try_pop/pop_for 只移动 std::unique_ptr，不抛出；
 *   - 元素析构函数不应抛出异常；cancel() 在锁外析构剩余元素。
 *
 * 阻塞与唤醒：
 *   - push/pop 阻塞直到成功、关闭或取消；
 *   - 普通入队/出队使用 notify_one；
 *   - close()/cancel() 使用 notify_all，保证所有等待者都能观察到状态变化。
 */
```

这份注释就是验收要求的“关闭语义写入接口文档”的产物。实际项目里可以把它拆成头文件注释 + README 的“队列契约”小节，但关键字段不能少：关闭后 `push` 返回什么、`pop` 何时返回 `nullptr`、剩余元素归谁、谁被唤醒、`cancel` 与 `close` 的区别、异常时的保证。

### 步骤 5：验收对照

| 验收项 | 本实践如何覆盖 |
| --- | --- |
| 压力下不丢不重 | `test_mpmc_stress`：4 生产者 × 20000 个唯一 value，4 消费者统计每个 value 的消费次数；总数等于 `kTotal`，每个 value 恰好一次 |
| 内存有上限 | 队列容量固定为 64；`size() <= capacity()`；阻塞的生产者各自最多持有一个待入队参数，不会在队列内部无限堆积 |
| 容量 1 | `test_capacity_one`：满时 `try_push` 返回 `Full`、`push_for` 超时；`pop` 后 `push_for` 成功 |
| 取消 | `test_cancel_wakes_blocked_consumer` / `test_cancel_wakes_blocked_producer`：`cancel()` 唤醒阻塞线程，剩余元素被丢弃，`size()` 归零 |
| 关闭 | `test_close`：关闭后 `push` 失败、`try_push`/`push_for` 返回 `Closed`；已入队的 1、2 被取完；之后 `pop` 返回 `nullptr` |
| 异常元素移动 | `test_throwing_move`：移动构造在入队节点构造时抛出，队列 `size()` 仍为 0，随后仍可正常入队/出队 |
| 关闭语义写入接口文档 | 步骤 4 的头文件注释，逐条对应实现 |

如果要把这份练习扩展成生产代码，建议再补三类测试：

1. **多轮随机容量**：容量从 1 到 8 循环，每轮都做小规模压力测试，覆盖满/空边界；
2. **关闭与生产并发**：生产者持续 `push` 的同时主线程 `close()`，验证每个生产者的 `push` 要么成功、要么返回 `false`，不会卡死；
3. **析构顺序**：队列析构时仍有元素，确认元素析构函数被调用且不在持锁状态下执行（可以在元素析构里检查一个标志，或者用日志观察）。

---

## sourceStudy：对照 cppreference 核对条件变量语义

本节点 `sourceStudy` 的要求是：**沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。**

资源是 cppreference 的 `std::condition_variable` 页面：

```text
https://en.cppreference.com/w/cpp/thread/condition_variable.html
```

不要只读“`wait` 会等待、`notify` 会唤醒”这两句。按下面清单逐项对照本课实现，把结论记进实验笔记。

### 1. 定义与头文件

- 确认 `std::condition_variable` 定义在 `<condition_variable>`，是 C++11 引入的同步原语。
- 确认它必须与 `std::unique_lock<std::mutex>` 配合使用；如果要配合其他锁类型（如 `std::shared_lock`），页面会提到 `std::condition_variable_any`。本课只用 `std::unique_lock<std::mutex>`，对应最常用的 `condition_variable`。
- 记录你使用的编译器版本、标准库实现（libstdc++/libc++）、目标平台；条件变量的底层实现（futex 等）和性能特征与平台相关，但接口语义以标准为准。

### 2. `wait` 与谓词

- 对照页面上的 `wait(lock)` 和 `wait(lock, pred)` 两种形式。
- 确认带谓词的 `wait` 语义等价于“循环检查 `pred`，不成立就等待”；它会在每次被唤醒后重新检查谓词，因此能处理**虚假唤醒**（spurious wakeup）和“被唤醒但条件仍不成立”的情况。
- 检查本课代码：所有 `wait` 都带了谓词吗？`push` 的谓词是否同时包含“队列未满”和“状态不是 Open”？`pop` 的谓词是否同时包含“队列非空”和“已关闭/已取消”？
- 如果代码里出现裸 `wait(lock)` 后再 `if`，把它改成谓词形式；裸 `wait` 无法区分虚假唤醒和真实通知。

### 3. `wait_for` / `wait_until` 的返回值

- 对照页面上的 `wait_for(lock, rel_time)`、`wait_for(lock, rel_time, pred)`、`wait_until` 系列。
- 确认带谓词的 `wait_for` 返回 `bool`：超时且谓词仍为假时返回 `false`；谓词为真时返回 `true`。本课的 `push_for` 正是用这个返回值区分 `Timeout` 和成功/关闭。
- 注意：超时返回 `false` 不等于“永远不会成功”，只是“在这段时间内谓词没有变成真”；调用方要自己决定重试、拒绝还是降级。
- 检查本课代码：`push_for` 在 `wait_for` 返回 `false` 时返回 `PushResult::Timeout`；返回 `true` 后还要再检查一次 `state_`，因为谓词为真可能是“关闭”而不是“有空位”。这一步不能省。

### 4. `notify_one` 与 `notify_all`

- 对照页面：`notify_one` 唤醒一个等待线程，`notify_all` 唤醒所有等待线程。
- 确认“通知”本身不携带数据，也不保证哪个线程被选中；被唤醒的线程仍然要重新检查谓词。
- 检查本课代码：普通入队/出队用 `notify_one`；`close()`/`cancel()` 用 `notify_all`。为什么关闭必须 `notify_all`？因为关闭改变了所有等待者的谓词，只唤醒一个会让其余线程继续睡。
- 确认通知与状态修改的顺序：先在锁内修改 `queue_`/`state_`，再通知。页面通常会说明通知操作本身不要求持有锁；但“改状态”和“通知”之间的顺序要保证等待者不会错过。本课采用“锁内改状态，锁外 notify”的写法，原因是让等待者尽快醒来后能拿到锁；这不是唯一写法，但要能解释为什么不会丢通知。
- 检查是否存在“持锁调用 notify”导致被唤醒线程立刻阻塞在锁上的性能问题；这不是正确性问题，但值得在笔记里记录。

### 5. 虚假唤醒与“丢失唤醒”

- 虚假唤醒：线程没有收到通知也可能从 `wait` 返回。带谓词的 `wait` 会重新检查，所以本课实现是安全的；裸 `wait` + 单次 `if` 不安全。
- 丢失唤醒：如果通知发生在等待之前，且没有谓词/状态记录，等待者可能永远睡。本课用 `state_`、`queue_.size()` 这些**持久状态**配合谓词，通知只是“提醒”，即使通知早了，等待者也会在进入 `wait` 前先检查谓词。
- 把这两个概念写进笔记，并各举一个本课代码中的对应位置。

### 6. 析构与生命周期

- 对照页面关于析构函数的说明：记录页面写出的销毁前提条件；不同标准库文档可能补充额外要求，以你实际使用的版本为准，不要凭记忆下结论。
- 检查本课代码：`BoundedQueue` 的析构函数调用 `cancel()` 唤醒等待者，但**这不能替代“调用方保证没有线程仍在使用队列”**。如果对象已经析构，而某个线程还在 `pop()` 里等，那是对象生命周期错误，不是队列能修复的。
- 生产代码里，队列通常由 `std::shared_ptr` 持有，或者由所有者保证 `join` 完所有线程后再析构；不要依赖析构函数去“通知还在等待的线程”。

### 7. 异常路径

- 对照页面：`wait`/`wait_for` 可能抛出 `std::system_error`（例如底层系统调用失败）；谓词本身抛出的异常会传播给调用方。
- 检查本课代码：`push` 中 `make_unique` 抛异常时，`unique_lock` 析构释放锁，队列状态不变；`wait_for` 抛异常时同样由 RAII 释放锁。调用方需要决定是否捕获、重试或让异常继续传播。
- 记录“异常安全等级”：`push` 对元素移动构造提供强保证（队列不变）；`pop` 只移动 `unique_ptr`，不抛出；`cancel` 在锁外析构元素，避免析构异常/慢操作影响锁。

### 8. 版本与边界条件记录

在实验笔记里写清楚：

```text
编译器：g++ <版本>
标准：-std=c++17
标准库：libstdc++ <版本>
平台：<Linux 发行版/架构>
条件变量页面访问日期：<日期>
已验证边界：容量 1、close 后 push、close 后排空、cancel 唤醒阻塞生产者/消费者、
            移动构造抛异常、4 生产者 × 4 消费者压力测试
未验证项：<例如跨 NUMA、特定实时调度策略、超大容量下的内存行为>
```

这样记录的价值在于：条件变量的接口语义是标准的，但底层实现、调度行为、性能特征与版本/平台相关；边界条件必须可复现，结论才能复用。

### sourceStudy 检查清单

- [ ] 确认 `condition_variable` 需要 `<condition_variable>` 和 `std::unique_lock<std::mutex>`
- [ ] 所有 `wait` 都使用谓词形式，能处理虚假唤醒
- [ ] `wait_for` 的 `bool` 返回值被正确解释为“谓词是否在超时前成立”
- [ ] `push_for` 在 `wait_for` 返回 `true` 后重新检查关闭状态
- [ ] 普通路径用 `notify_one`，关闭/取消用 `notify_all`
- [ ] 状态修改在锁内，通知在状态修改之后
- [ ] 能解释“丢失唤醒”为什么被持久状态 + 谓词避免
- [ ] 析构前由调用方保证没有线程仍在使用队列
- [ ] 记录了编译器、标准库、平台、验证日期和未验证项
- [ ] 把本课实现与页面上的示例逐段对照，记录差异和理由

---

## 自测题

### 题 1

一个 SPSC 环形缓冲用 `std::atomic<std::size_t> head_` 和 `tail_`，生产者执行：

```cpp
buffer_[tail] = value;
tail_.store(next, std::memory_order_release);
```

消费者执行：

```cpp
if (head == tail_.load(std::memory_order_acquire)) return false;
out = buffer_[head];
head_.store((head + 1) % N, std::memory_order_release);
```

请回答：

1. 生产者的 `buffer_[tail] = value` 为什么对消费者可见？依赖哪一对操作？
2. 如果把生产者的 `tail_.store` 改成 `memory_order_relaxed`，可能出现什么问题？
3. 这个实现为什么不能直接支持多个生产者？请给出一个具体的交错。
4. 多生产者多消费者版本用互斥锁 + 两个条件变量时，元素可见性由什么保证？条件变量本身负责什么？

**答案：**

1. 依赖 `tail_.store(release)` 与消费者 `tail_.load(acquire)` 的 synchronizes-with 关系。消费者读到生产者写入的新 `tail_` 值后，生产者在该 release 之前的槽位写入（`buffer_[tail] = value`）对消费者可见。
2. 如果 `tail_.store` 是 `relaxed`，编译器和 CPU 可以重排槽位写入与下标更新，消费者可能先看到新的 `tail_`，再去读槽位时读到旧值/未初始化值。`relaxed` 不建立跨线程的可见性顺序，不能用来发布数据。
3. 两个生产者可能同时读到同一个 `tail`，算出同一个 `next`，然后都写 `buffer_[tail]`，后写者覆盖先写者；或者都执行 `tail_.store(next)`，其中一个更新丢失。即使槽位写入不冲突，“读 `head_` 判断满 + 写槽位 + 更新 `tail_`”也不是一个原子步骤，另一个生产者可以插在中间。SPSC 的正确性依赖“只有一个写者”这个前提。
4. 元素可见性由互斥锁的 unlock/lock 保证：生产者持锁写入队列后解锁，消费者随后锁住同一把锁，前一次解锁 synchronizes-with 后一次加锁，因此队列内容对消费者可见。条件变量只负责等待/通知，不存储数据、不提供互斥；它必须与 mutex 和谓词配合使用。

### 题 2

微信公众号后台的群发消息 worker 队列容量为 1024。高峰期队列满了，下游发送接口变慢。请回答：

1. 如果选择阻塞 `push`，系统会发生什么？背压如何传播？
2. 如果选择 `try_push` 失败后返回“系统繁忙”，对用户和系统分别意味着什么？
3. 如果选择丢弃最旧元素，哪些业务可以接受，哪些绝对不能？
4. 为什么不能先用 `size()` 判断“还有空位”再调用 `push`？
5. 容量为 1 时，这个队列适合什么场景？不适合什么场景？

**答案：**

1. 生产者（接入层线程）在 `push` 上等待，队列不再增长，内存有上限；背压沿着调用链传回上游，上游要么降低投递速度，要么也阻塞。风险是如果大量线程都阻塞在 `push` 上，线程池/网关线程被占满，健康检查、拒绝请求、超时处理都可能没有线程可用。
2. 对用户：请求快速失败，可以提示“系统繁忙，请稍后重试”，响应时间可控；对系统：worker 不被慢下游拖死，队列保持有界，但调用方必须正确处理失败，不能把失败当成功。拒绝策略适合在线请求，不适合“必须受理”的任务。
3. 可以接受：日志、指标、可重建的缓存刷新、非关键的提醒类消息（丢一条不影响最终正确性，且有计数/补偿）。绝对不能：支付、订单、消息必达、扣减库存等一旦丢失就造成资金或状态错误的业务。丢弃策略必须和业务承诺一致。
4. `size()` 返回的是某一瞬间的快照；检查之后、`push` 之前，其他生产者可能已经填满队列。这个“检查-操作”窗口是竞态。应该直接用 `try_push` 的返回值，或者用阻塞/限时 `push` 让队列内部在锁保护下做判断。
5. 容量 1 适合“交接点”语义：生产者把一个任务交给消费者，交接完成前生产者等待；也适合测试满/空边界、关闭唤醒、取消唤醒。不适合削峰：没有缓冲余量，消费者慢一拍生产者就满，吞吐受限于最慢的一环。

### 题 3

某队列的 `close()` 实现如下：

```cpp
void close() {
    std::lock_guard<std::mutex> lock(mu_);
    closed_ = true;
}
```

`pop()` 的等待谓词是 `!queue_.empty()`，`push()` 的等待谓词是 `queue_.size() < capacity_`。请回答：

1. 这个 `close()` 有什么问题？阻塞在 `pop()` 的消费者会怎样？
2. 阻塞在 `push()` 的生产者会怎样？
3. 正确的 `close()` 应该怎么写？为什么要用 `notify_all`？
4. 如果元素移动构造在 `push` 内抛出异常，队列应该保持什么状态？本课的实现如何做到？
5. `close()` 和 `cancel()` 的语义区别是什么？剩余元素分别归谁？

**答案：**

1. 只改 `closed_` 不通知，阻塞在 `pop()` 的消费者不会被唤醒；它的谓词 `!queue_.empty()` 也不包含 `closed_`，即使被虚假唤醒也会继续睡。结果是队列关闭后消费者永远等不到“结束”，`join()` 卡死。
2. 阻塞在 `push()` 的生产者同样不会被唤醒；它的谓词只看容量，不看 `closed_`，关闭后即使有空位也会继续入队，或者永远等待。`push` 在关闭后必须失败，而不是继续接受元素。
3. 正确写法：在锁内把状态改为 `Closed`，释放锁后调用 `not_empty_.notify_all()` 和 `not_full_.notify_all()`；所有等待谓词都要包含“已关闭”。用 `notify_all` 是因为关闭改变了**所有**等待者的谓词：每个生产者都不能再入队，每个消费者都要么取剩余元素、要么看到“已关闭且为空”；只唤醒一个会让其余线程继续睡。
4. 队列应保持操作前的状态：`size()` 不变、元素内容不变、关闭状态不变，异常传播给调用方，队列仍可继续使用。本课实现先在锁内 `make_unique<T>(std::move(value))` 构造节点，成功后才 `queue_.push_back(std::move(node))`；如果移动构造抛出，节点未入队，`unique_lock` 析构释放锁，队列没有被修改。`unique_ptr` 的移动是 `noexcept`，所以入队本身不会因元素移动而抛异常。
5. `close()` 是优雅关闭：不再接受新元素，但已入队元素保留，消费者可以取完，队列空后 `pop` 返回 `nullptr`；剩余元素最终归消费者（被 `pop` 取走）或随队列析构销毁。`cancel()` 是取消：不再接受新元素，剩余元素被丢弃并在锁外析构，`pop` 立即返回 `nullptr`。两者都要 `notify_all`，但剩余元素的命运不同。

---

## 推荐资源

- **有界并发队列与背压：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/thread/condition_variable.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：重点阅读SPSC与MPMC与阻塞、丢弃和拒绝，用于核对本节点实验的语义和边界。（阅读建议：结合本课，重点对照页面中 `wait` 的谓词形式、`notify_one`/`notify_all` 与 `wait_for` 的返回值。）

---

## 本课小结

1. **有界队列首先是一份接口契约。** 它不只定义“怎么存、怎么取”，还定义容量、满时策略、关闭/取消语义和元素所有权。契约写不清楚，队列实现再正确也会在业务层出问题。
2. **SPSC 可以无锁，但前提是单写单读。** 原子下标 + acquire/release 建立“槽位写入 → 下标发布 → 消费者读取”的可见性；`relaxed` 不能用来发布数据。
3. **MPMC 不能照搬 SPSC。** 多生产者会争同一个 `tail`，多消费者会争同一个 `head`；“检查容量 + 入队”必须是原子的。互斥锁 + 两个条件变量是最实用的方案，无锁 MPMC 需要 CAS、ABA 和内存回收等额外复杂度。
4. **条件变量只负责通知，不负责状态。** 状态（队列长度、关闭标志）在锁内维护，等待必须用谓词形式，处理虚假唤醒和“醒来条件又不成立”。普通路径 `notify_one`，关闭/取消 `notify_all`。
5. **满队列策略就是背压策略。** 阻塞把压力反推给上游，适合不能丢且上游能等的任务；拒绝保留响应性，适合在线请求；丢弃牺牲数据换延迟，只适合可重建/非关键数据。选择要和业务承诺一致。
6. **容量 1 是重要边界。** 它没有缓冲余量，适合交接点和边界测试；满/空判断、唤醒对象、关闭排空都能在这里暴露。
7. **关闭是线性化点。** `close()` 后 `push` 必须失败，`pop` 必须能取完剩余元素再返回 `nullptr`，所有阻塞线程都要被 `notify_all` 唤醒，`close()` 幂等。
8. **取消不同于关闭。** `close()` 排空，`cancel()` 丢弃；两者都要唤醒所有等待者，但剩余元素的命运和 `pop` 的返回行为不同。不要在持锁时析构剩余元素。
9. **移动语义让所有权清晰。** 元素按值/移动进入队列，`pop` 返回 `unique_ptr` 把所有权交给消费者；`unique_ptr` 的移动是 `noexcept`，元素移动构造抛异常时队列状态不变（强异常保证）。
10. **队列线程安全不代表元素安全。** 入队 `shared_ptr`/裸指针后，生产者继续修改对象，消费者仍可能读到半更新状态；要么转移所有权并放弃访问，要么用不可变对象/值语义，要么在元素内部自己做同步。
11. **验收要可执行。** “压力下不丢不重且内存有上限”要用唯一序号 + 逐元素计数验证；“关闭语义写入接口文档”要落到头文件注释，写清楚 `push`/`pop` 返回值、剩余元素归属、唤醒规则和异常保证。

下一课会继续在并发数据结构里处理更细的同步问题；本课留下的工作台是：**先写清楚契约，再选 SPSC/MPMC，再选满队列策略，最后用压力、容量 1、关闭、取消、异常移动五类测试把契约钉死。**
