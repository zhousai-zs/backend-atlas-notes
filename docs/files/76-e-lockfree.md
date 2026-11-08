# 第 76/208 课：无锁进展保证与CAS结构

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 4 阶段 · 并发数据结构（无锁进展保证与 CAS） |
| 前置 | e-order、e-queue |
| 难度 | 深入 |
| 领域 | E |
| 标签 | Lock Free Algorithms、E、C++ |
| 子分类 | 并发数据结构 |
| 节点 ID | e-lockfree |
| 英文名 | Lock Free Algorithms |

> 承接说明：前置节点 e-order（C++内存序与发布订阅，第 70 课）解释了 release/acquire 如何建立 happens-before，让“先写数据、再发布指针/序号”对另一线程可见；前置节点 e-queue（有界并发队列与背压，第 74 课）解释了有界队列、容量、关闭与用互斥锁+条件变量实现 MPMC 队列。本课把两者接到无锁语境：先分清 wait-free / lock-free / obstruction-free 三种进展保证，再讲线性化点，以及 CAS 失败后的重试与退避；实践落地到固定容量 SPSC 环形队列（单生产者单消费者），用序号发布顺序保证读者看到完整槽位，并与互斥队列比较。不要从 MPMC 结构直接删锁。

---

## 导读

先给“无锁”一个不容易被广告词带偏的定义：

> 无锁（lock-free）通常指**系统整体持续推进**：在任意时刻，至少有一个线程能在有限步内完成操作。它**不等于**每个线程都不等待，也不等于每个线程都能在有限步内完成。

这句话是本课的基调。很多团队把“把 mutex 换成 CAS”当成性能优化，结果得到的是更难读、更难测、更难维护的代码，却不一定更快，甚至在分配器、日志、缺页这些看不见的地方继续阻塞。本课要做的是把“无锁”拆成三个可以分别讨论的问题：

1. **进展保证**：wait-free、lock-free、obstruction-free 到底各保证什么？锁为什么通常不属于其中任何一种？
2. **线性化点**：这段代码在哪个原子步骤上“对外生效”？并发历史为什么可以等价于某个串行历史？
3. **CAS 重试与退避**：失败者怎么重试？高竞争下怎么避免把缓存行打热？ABA 又是什么？

然后落到一个足够小、足够可证明的实践：**固定容量 SPSC 环形队列**。它只有一个生产者、一个消费者，正确性可以靠“槽位所有权 + 序号发布”讲清楚，不需要 CAS 循环，也不需要处理动态节点回收。把它写对、测对、证明清楚，再谈 MPMC 和更复杂的无锁结构。

放到微信公众号后台，这些场景非常具体：

1. **接入层网络线程把请求/回调事件丢给业务工作线程**。网络线程负责收包、解析、做初步校验，然后把事件交给固定数量的业务线程。每个“网络线程→业务线程”的通道天然接近 SPSC；如果有多个网络线程投递到同一个工作线程，那就是少量生产者，需要额外同步，不能直接套 SPSC。
2. **模板消息、客服消息的流水线队列**。一条消息要经过校验、模板渲染、风控、发送、回执等多个阶段，每个阶段之间有一个有界队列。阶段内部的并发度不高时，一把 mutex 加条件变量往往比手写无锁队列更稳。
3. **指标/日志异步落盘**。业务线程只负责把一条记录放进队列，后台线程批量取走、聚合、写文件。这里最怕的是“看起来无锁，实际上在队列里 new 了一个大对象，或者在无锁路径里打了一条日志”。
4. **高 QPS 下互斥锁的上下文切换与缓存行乒乓可能成为热点**。当临界区极短、线程数远多于核数时，锁的排队、唤醒和缓存行在核间来回转移会吃掉大量 CPU。这时无锁结构可能有价值，但前提是先用测量证明锁确实是瓶颈。
5. **不要一听“无锁更快”就把 mutex 换成 CAS**。多数业务路径上，锁更简单、正确性更好、出问题更容易定位；无锁的收益只在明确的性能约束和成熟的实现下才成立。

本课反复强调节点 summary 的两句话：

- **无锁通常保证系统整体持续推进，不等于每个线程无等待。**
- **正确性还要求线性化与内存回收。** 一个结构“能跑”不代表它线性化正确；一个节点“弹出”了不代表可以立刻 `delete`。

---

## 学习目标

### goals

- 能解释 wait-free / lock-free / obstruction-free。
- 能独立完成：实现固定容量 SPSC 环形队列并证明发布顺序，与互斥队列比较。

### acceptance

- 能指出每次操作的线性化点。
- 空满边界和长时间运行不覆盖未消费数据。

把这两条验收翻译成可检查的产物：

- **“能指出每次操作的线性化点”**：`push`、`pop` 的成功路径和失败路径都要能说清楚“在哪一行原子操作上生效”。代码注释里直接写“线性化点：……”，教案文字里给出对应的串行历史解释。不能只说“大概是在更新序号的时候”。
- **“空满边界和长时间运行不覆盖未消费数据”**：空队列 `pop` 必须失败；队列填满后 `push` 必须失败且已有元素不变；消费一个之后 `push` 必须成功；长时间压测中消费者看到的序号必须严格递增、无空洞、无重复。若发生覆盖，序号会出现跳跃或重复，校验和也会对不上。

这两条不是“跑一遍没崩就行”，而是接口契约：队列容量是多少、满时返回什么、空时返回什么、元素在什么时刻可以被覆盖、生产者与消费者各自拥有哪些槽位，都要写进注释和测试。

---

## 为什么后端要懂

本节点给出的 why 是：**审慎评估用 CAS 替换锁是否值得。** “审慎”至少包含四层含义，每一层在线上都能变成一次真实事故。

### 1. 锁的瓶颈是真的瓶颈吗

高 QPS 下看到 CPU 高、延迟抖，不一定就是锁的问题。可能是下游 RPC 慢、可能是日志同步写、可能是内存分配、可能是线程数过多导致调度抖动。只有当你测量到：

- 临界区很短，但等锁时间很长；
- `perf` 里大量时间花在 futex 等待或缓存行争用上；
- 减少线程数或改变分片方式后吞吐明显变化；

才有理由考虑无锁。否则把锁换掉，只是把“等锁”换成了“CAS 自旋”，问题可能从睡眠变成空转，CPU 更高、延迟更差。

### 2. 无锁不等于“没有任何阻塞点”

这是本课 pitfalls 的核心：**没有互斥锁的代码仍可能因分配器或系统调用阻塞。** 一条“无锁”路径里如果出现：

- `new` / `malloc`（分配器内部可能加锁，也可能触发系统调用）；
- 日志 `write`、文件 IO、网络 IO；
- 缺页（第一次触碰新内存）；
- 异常抛出与栈展开；
- `std::atomic<T>` 在目标平台上 `!is_lock_free()`，内部可能用锁实现；

那么这条路径就不是真正的无锁。CAS 只保证“这次原子操作本身”的进展，不保证它前后那些普通操作不阻塞。

### 3. 正确性不止“不崩”

无锁结构的正确性至少有两半：

- **线性化**：并发历史必须看起来等价于某个合法的串行历史。每个操作要有明确的生效点。
- **内存回收**：一个线程把节点从结构里摘下来，不代表其他线程不再持有它。立刻 `delete` 可能让另一个线程读到已释放内存。

很多“手写无锁队列”在低并发下能跑，压力一上来就出现丢元素、重复元素、崩溃，原因往往不是 CAS 写错了，而是空满判断、发布顺序或回收没处理干净。

### 4. 复杂度是有价格的

无锁代码的读者需要同时理解内存序、CAS 失败语义、ABA、回收机制和线性化点。排障时，一个偶发的丢消息可能来自两周前一次“优化”。所以 tradeoffs 写得很直接：**无锁复杂度高，只有明确性能约束时优先采用成熟实现。**

对公众号后台这类业务，优先级通常是：

1. 先把接口契约写清楚（容量、满时策略、关闭语义）；
2. 能用一把 mutex + 有界队列解决，就先这样解决；
3. 确实有热点，再选成熟实现（Folly、Boost.Lockfree、经过验证的 SPSC）；
4. 自己手写只用于学习、或用于结构足够小且能证明的场景。

---

## 从直觉到原理

### 要点一：wait-free / lock-free / obstruction-free

这三个词经常被混用。先用一张表把它们分开：

| 保证 | 对单个线程的承诺 | 对系统整体的承诺 | 典型手段 |
| --- | --- | --- | --- |
| wait-free | 每个线程的操作都能在有界步数内完成 | 整体当然推进 | 每线程独立槽位、fetch-add 一类无循环算法 |
| lock-free | 不保证单个线程有界完成 | 任意时刻至少有一个线程能在有限步内完成 | CAS 重试循环 |
| obstruction-free | 不保证有竞争时推进 | 某线程独自运行一段时间就能完成 | 事务内存、部分无锁算法 |
| 互斥锁 | 持锁线程被调度走/阻塞/崩溃时，其他线程全部停住 | 不保证 | mutex + 条件变量 |

#### wait-free：最强，也最贵

wait-free 要求**每个线程**的操作都能在**有界步数**内完成，不依赖其他线程是否运行、是否被调度、是否失败。注意“有界”是对每个线程说的，不是对系统整体说的。

一个直观例子是“每个线程有自己的计数器槽位，最后汇总”：线程只写自己的槽，不和其他线程争，步数有界。代价是空间和汇总逻辑。另一个例子是某些 `fetch_add` 实现，但标准并不保证 `std::atomic::fetch_add` 一定 wait-free，只保证它是原子的；是否 lock-free 要用 `is_lock_free()` 查。

#### lock-free：系统整体推进

lock-free 的承诺弱一些：**任意时刻至少有一个线程能在有限步内完成操作。** 个别线程可能因为 CAS 一直失败而长时间重试，但成功的那一方让系统整体前进。

这正好对应 summary 的核心句子：无锁通常保证系统整体持续推进，不等于每个线程无等待。一个线程可能饿很久，但系统不会整体卡死。CAS 重试循环就是典型 lock-free：失败者不断重试，成功者完成操作。

#### obstruction-free：更弱

obstruction-free 只要求：如果某个线程最终能**独自运行一段时间**（其他线程暂停），它就能完成。有竞争时，大家可能互相拆台，谁都无法前进。

它常见于软件事务内存（STM）的讨论：冲突时需要回滚重试，没有“至少一个成功”的整体保证。工程上单独使用较少，但理解它有助于看清“无锁”内部的强弱层次。

#### 锁为什么通常不是 lock-free

互斥锁的进展依赖持锁线程。持锁线程如果：

- 被操作系统调度走；
- 阻塞在 IO；
- 发生缺页；
- 甚至崩溃（没有释放锁）；

其他等锁线程就全部停住。系统整体无法保证“至少一个线程能完成”。所以基于锁的算法通常不属于 lock-free，更不属于 wait-free。

条件变量也一样：等待者依赖通知者运行。通知者不运行，等待者就继续睡。这不是缺陷，而是阻塞同步的语义。

#### 陷阱：没有互斥锁，仍然可能阻塞

这是本课必须单独强调的一点。看下面这段“看起来无锁”的代码：

```cpp
// 反例：没有 mutex，但整条路径仍可能阻塞。
// 这里只补最小定义让它可编译；真实项目里的 new / 日志实现可能阻塞。
#include <atomic>
#include <cstdio>

struct Event { int id; };
struct Node {
    Event event;
    Node* next;
};

static std::atomic<Node*> head{nullptr};

static void log_info(const char* text) {
    // 真实实现可能做格式化、加锁、写文件或发网络请求。
    std::puts(text);
}

void enqueue(const Event& e) {
    Node* n = new Node{e, nullptr};      // 可能阻塞：分配器内部锁、系统调用
    n->next = head.load(std::memory_order_relaxed);
    while (!head.compare_exchange_weak(n->next, n,
                                       std::memory_order_release,
                                       std::memory_order_relaxed)) {
        // CAS 本身是原子的，但 new 已经发生
    }
    log_info("enqueued");                // 可能阻塞：文件 IO、锁、格式化
}
```

CAS 循环是 lock-free 的，但整个函数不是：`new` 和 `log_info` 都可能让线程停住。更隐蔽的是 `std::atomic<T>` 本身：相同类型在不同平台上可能内部用锁实现。

#### 用 is_lock_free 检查“原子”是否真的无锁

C++ 提供了几个检查入口：

- 成员函数 `is_lock_free()`：运行时检查当前平台上该原子类型是否无锁；
- 静态成员 `is_always_lock_free`（C++17 起）：编译期常量，表示该原子类型在目标平台上所有执行中都无锁；
- C 兼容接口 `atomic_is_lock_free`：用于 C 风格原子类型。

下面这个完整例子打印几种类型的 `sizeof` 与无锁属性。**输出依赖平台和标准库，不要背某个固定结果**：

```cpp
// atomic_probe.cpp
// g++ -std=c++17 -O2 -pthread -o atomic_probe atomic_probe.cpp -latomic
#include <atomic>
#include <cstdint>
#include <cstdio>

struct TwoPointers {
    void* a;
    void* b;
};

template <typename T>
void report(const char* name) {
    std::atomic<T> value{};
    std::printf("%-16s sizeof(atomic)=%-3zu is_lock_free=%d is_always_lock_free=%d\n",
                name,
                sizeof(std::atomic<T>),
                value.is_lock_free() ? 1 : 0,
                std::atomic<T>::is_always_lock_free ? 1 : 0);
}

int main() {
    report<int>("int");
    report<std::uint64_t>("uint64_t");
    report<double>("double");
    report<TwoPointers>("TwoPointers");
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -O2 -pthread -o atomic_probe atomic_probe.cpp -latomic && ./atomic_probe
```

> 说明：`std::atomic<TwoPointers>` 这类 16 字节原子类型，在 GCC 上 `is_lock_free()` 可能会调用 libatomic 里的辅助函数，不加 `-latomic` 可能出现链接错误；这本身就提示了“这个原子类型未必由单条硬件指令实现”。

读输出时注意：

- `is_lock_free()` 是运行时结果，同一种类型在不同 CPU/ABI 上可能不同；
- `is_always_lock_free` 是 C++17 的编译期常量，适合做 `static_assert`；它为 `true` 时 `is_lock_free()` 一定为 `true`，但为 `false` 只表示“不能保证所有执行都无锁”，当前平台上运行时仍可能无锁，不能把 `false` 直接当成“一定带锁”；
- `std::atomic<TwoPointers>` 这种较大的类型，很可能不是 lock-free（例如需要 16 字节原子操作，而平台只保证 8 字节），此时内部可能退化成带锁实现。具体结果以你的平台输出为准。

实践建议：对头尾指针、序号这类承担发布职责的原子变量，在目标平台上做运行时检查，必要时加静态断言；不要假设“写了 `std::atomic` 就等于无锁”。

#### 内存回收：正确性的另一半

无锁栈/队列弹出一个节点后，不能立刻 `delete`：

```cpp
Node* old = head.load();
// ... CAS 成功把 old 摘下 ...
// delete old;  // 危险：其他线程可能刚读到 old，正在访问 old->next
```

其他线程可能还握着 `old` 的指针，或者正在读 `old->next`。立刻释放会让它们读到已释放内存。常见解法：

- **hazard pointer**：读者先登记“我正在看这个指针”，回收者检查没有读者登记后才释放；
- **epoch / 延迟回收**：把释放推迟到确认所有线程都离开某个 epoch 之后；
- **引用计数**：每个节点带计数，最后一个持有者释放；
- **不用动态节点**：固定容量环形缓冲，槽位由生产者/消费者按所有权交接，天然避开“弹出节点后何时 delete”的问题。

本课实践选择最后一种：固定容量环形缓冲，正是为了用**槽位所有权**避开动态节点回收。这不是说环形队列没有生命周期问题，而是说它把问题缩小到“槽位何时可以覆盖”，而这个问题可以用序号发布来证明。

---

### 要点二：线性化点

#### 直觉

并发程序的操作不是瞬间完成的：`push` 可能要先检查容量、写槽位、再更新序号；`pop` 可能要先读序号、读槽位、再推进读序号。在这个过程中的某一刻，操作“对外生效”了。这个原子步骤就是**线性化点（linearization point）**。

形式化一点说：如果每个操作都能在它的执行区间内选一个点，使得把所有操作按这些点排序后，得到的串行历史合法，并且每个操作返回的结果与串行历史一致，那么这个结构就是线性化的。工程上不需要每次写形式化证明，但必须能指着代码说：“这一行是 `push` 的线性化点。”

线性化点帮助我们回答三个问题：

1. 两个操作并发时，谁先谁后？看线性化点的顺序。
2. 一个操作失败时，它在历史上算什么？失败的 `push` 不能被当成成功。
3. 读者看到新序号后，能不能看到对应的数据？取决于发布顺序。

#### SPSC 环形队列的骨架

先看一个带注释的示意，暂不追求完整封装：

```cpp
template <typename T, std::size_t N>
class SpscRing {
public:
    bool push(const T& value) {
        const std::size_t w = write_pos_.load(std::memory_order_relaxed);
        const std::size_t r = read_pos_.load(std::memory_order_acquire);
        if (w - r == N) {
            return false;   // 失败：以这次 acquire load 的观察为线性化点
        }

        slots_[w & (N - 1)] = value;   // 生产者独占该槽，普通写即可
        write_pos_.store(w + 1, std::memory_order_release);
        // 线性化点：上面这次 release store。它让“槽位已填好”对外生效。
        return true;
    }

    bool pop(T& out) {
        const std::size_t r = read_pos_.load(std::memory_order_relaxed);
        const std::size_t w = write_pos_.load(std::memory_order_acquire);
        if (r == w) {
            return false;   // 失败：以这次 acquire load 的观察为线性化点
        }

        out = slots_[r & (N - 1)];       // 消费者独占该槽，普通读即可
        read_pos_.store(r + 1, std::memory_order_release);
        // 线性化点：上面这次 release store。它让“槽位已空出”对外生效。
        return true;
    }

private:
    alignas(64) std::atomic<std::size_t> write_pos_{0};
    alignas(64) std::atomic<std::size_t> read_pos_{0};
    alignas(64) T slots_[N];
};
```

这里有两个“序号”：

- `write_pos_`：生产者已经发布到哪个位置。只有生产者写它。
- `read_pos_`：消费者已经消费到哪个位置。只有消费者写它。

`slots_[w & (N - 1)]` 要求 `N` 是 2 的幂，这样取模可以写成位与。如果 `N` 不是 2 的幂，就老老实实写 `w % N`，不要抄了位运算却忘了容量约束。

#### 空与满

用无符号差值判断：

- 空：`r == w`；
- 满：`w - r == N`。

为什么能这样写？因为 `read_pos <= write_pos`，且两者单调递增，差值最大为 `N`。用 `std::size_t`（无符号）时，`w - r` 在回绕后仍然等于真实差值，只要这个差值不超过 `N`。**不要用 `signed int` 做头尾相减**：溢出是未定义行为，长跑之后可能突然判断错误。

#### 发布顺序：先写槽，再发布序号

生产者：

```text
1. 确认该槽归自己（w - r < N）
2. slots_[w % N] = value        // 写数据
3. write_pos_.store(w + 1, release)  // 发布
```

消费者：

```text
1. acquire load write_pos，确认 r != w
2. out = slots_[r % N]          // 读数据
3. read_pos_.store(r + 1, release)   // 归还空槽
```

happens-before 链如下：

- 生产者的 `slots_[...] = value` **sequenced-before** `write_pos_.store(release)`；
- 消费者的 `write_pos_.load(acquire)` 读到生产者写入的新值，因此 **synchronizes-with** 那次 release store；
- 所以消费者在 `write_pos_.load(acquire)` 之后的 `slots_[...]` 读取，一定能看到生产者写入的完整值。

反过来就是错误：**先发布序号，再写槽**。消费者可能先看到新序号，然后去读一个还没写完的槽，读到旧值、半更新值或未初始化值。这类 bug 在 x86 上可能偶发，在弱内存模型平台上更容易稳定复现。

#### 为什么生产者要 acquire 读 read_pos

生产者判断“不满”时，不只是看数量，还要确认消费者已经**读完**那个即将被覆盖的槽。消费者读完槽后执行 `read_pos_.store(release)`；生产者用 `read_pos_.load(acquire)` 读到新值，二者建立 synchronizes-with，生产者才敢覆盖。

如果生产者用 `relaxed` 读 `read_pos_`，它可能看到“已经空了”的计数，却看不到消费者对槽位的读取已经完成，从而覆盖消费者正在读的数据。这就是“序号发布了，但所有权没交接清楚”。

#### 失败操作的线性化点

成功路径的线性化点很直观：`push` 是发布 `write_pos_` 的 release store，`pop` 是推进 `read_pos_` 的 release store。

失败路径也要有说法：

- `push` 因满失败：可以取“读取 `read_pos_` 并观察到 `w - r == N`”这次 acquire load 作为线性化点；
- `pop` 因空失败：可以取“读取 `write_pos_` 并观察到 `r == w`”这次 acquire load 作为线性化点。

失败操作不能修改任何状态，也不能让后续操作看到它“部分生效”。

#### 用“槽位所有权”复述一遍

成熟 SPSC 算法通常用所有权来组织证明：

- 生产者拥有**空槽**：它可以在槽里写，别人不能读；
- 写完并发布序号后，槽的所有权交给消费者；
- 消费者拥有**已填充槽**：它可以在槽里读，生产者不能覆盖；
- 读完并推进读序号后，空槽的所有权交回生产者。

“交接只通过原子序号完成”这句话，是本课 sourceStudy 的核心。只要你能用所有权讲清楚每个槽在任一时刻归谁，空满和发布顺序就不容易写错。

---

### 要点三：CAS 重试和竞争退避

#### CAS 是什么

CAS（compare-and-swap / compare-and-exchange）的语义：

> 如果当前值等于期望值，就写入新值并返回成功；否则不写，并把当前值回填到期望值里，返回失败。

C++ 里对应：

- `compare_exchange_weak(expected, desired)`：可能**伪失败**（spurious failure），即值明明相等却返回失败；
- `compare_exchange_strong(expected, desired)`：不会伪失败，但可能更贵。

经验规则：

- 放在循环里重试时，通常用 `weak`，因为伪失败会被循环吸收；
- 只尝试一次、失败就返回时，用 `strong`，避免把伪失败误判成真实冲突。

#### 一个 lock-free 栈的典型循环

```cpp
struct Node {
    int value;
    Node* next;
};

std::atomic<Node*> head{nullptr};

void push(int value) {
    Node* n = new Node{value, nullptr};
    n->next = head.load(std::memory_order_relaxed);
    while (!head.compare_exchange_weak(n->next, n,
                                       std::memory_order_release,
                                       std::memory_order_relaxed)) {
        // 失败时 n->next 已被回填为当前 head，直接重试
    }
    // 线性化点：成功的 compare_exchange_weak
}

bool pop(int& out) {
    Node* old = head.load(std::memory_order_acquire);
    while (old && !head.compare_exchange_weak(old, old->next,
                                              std::memory_order_acquire,
                                              std::memory_order_acquire)) {
        // 失败时 old 已被回填为当前 head
    }
    if (!old) {
        return false;   // 空栈
    }
    out = old->value;
    // 注意：这里不能直接 delete old；其他线程可能仍持有它。
    // 生产实现需要 hazard pointer / epoch / 引用计数等回收机制。
    return true;
}
```

为什么这是 lock-free 而不是 wait-free？失败者可能无限重试，没有任何“有界步数”的保证；但每一轮总有一个 CAS 成功，系统整体在前进。若某个线程一直抢不到，它可能饿很久。

这段代码只用于说明 CAS 循环和线性化点，**不是可直接上线的栈**：`pop` 的回收问题没有解决，ABA 也没有处理。

#### 带退避的 CAS 加法

高竞争时，纯空转 CAS 会反复写同一缓存行，让缓存行在核间来回转移，浪费 CPU。可以加指数退避和 `pause`：

```cpp
// cas_add.cpp
// g++ -std=c++17 -O2 -pthread -o cas_add cas_add.cpp
#include <atomic>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
static inline void cpu_relax() { _mm_pause(); }
#else
static inline void cpu_relax() { std::this_thread::yield(); }
#endif

std::atomic<int> g{0};

void cas_add(int delta) {
    int old = g.load(std::memory_order_relaxed);
    while (!g.compare_exchange_weak(old, old + delta,
                                    std::memory_order_relaxed,
                                    std::memory_order_relaxed)) {
        // 失败时 old 已回填为当前值，下一轮直接用它
    }
}

void cas_add_backoff(int delta) {
    int old = g.load(std::memory_order_relaxed);
    int spins = 1;
    while (!g.compare_exchange_weak(old, old + delta,
                                    std::memory_order_relaxed,
                                    std::memory_order_relaxed)) {
        for (int i = 0; i < spins; ++i) {
            cpu_relax();
        }
        if (spins < 1024) {
            spins *= 2;
        }
    }
}

constexpr int kThreads = 8;
constexpr int kIters = 100000;

// 用同一负载分别跑三种加法：纯 CAS 循环、CAS + 退避、fetch_add。
template <typename Fn>
void run(const char* name, Fn add_one) {
    g.store(0, std::memory_order_relaxed);
    const auto start = std::chrono::steady_clock::now();
    std::vector<std::thread> threads;
    for (int i = 0; i < kThreads; ++i) {
        threads.emplace_back([&] {
            for (int j = 0; j < kIters; ++j) {
                add_one();
            }
        });
    }
    for (auto& t : threads) {
        t.join();
    }
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - start).count();
    const int total = g.load(std::memory_order_relaxed);
    std::printf("%-12s total=%d expected=%d %s time=%.2fms\n",
                name, total, kThreads * kIters,
                total == kThreads * kIters ? "OK" : "WRONG", ms);
}

int main() {
    run("cas_add", [] { cas_add(1); });
    run("cas_backoff", [] { cas_add_backoff(1); });
    run("fetch_add", [] { g.fetch_add(1, std::memory_order_relaxed); });
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -O2 -pthread -o cas_add cas_add.cpp && ./cas_add
```

本课不给出任何“跑了多少 ops/s”的数字。不同机器、不同核数、不同负载下结果差异很大，应该自己在目标环境测。这里只说明结构：CAS 循环 + 可选退避；`cpu_relax()` 在 x86 上是 `pause`，其他平台退化为 `yield`。

`std::atomic<int>::fetch_add` 通常能一条指令完成加法，不需要 CAS 循环；上面的 `cas_add` 是为了展示重试模式，不是替代 `fetch_add` 的建议。实际写计数器优先用 `fetch_add`。

#### ABA 点到为止

CAS 的一个经典陷阱是 ABA：

1. 线程 A 读到值 `X`；
2. 线程 B 把值改成 `Y`，又改回 `X`；
3. 线程 A 的 CAS 看到还是 `X`，以为没人动过，于是成功。

值没变，不代表中间没发生事情。对指针结构，ABA 可能导致节点被释放又复用，CAS 成功但逻辑已经错了。

本课实践用**单调递增序号 + 固定槽位**，天然避开经典 ABA：序号只增不减，`w` 和 `r` 不会回到旧值（除非 2^64 次操作后回绕，工程上可按实际情况评估）。这也是“固定容量 SPSC 比动态节点 MPMC 更适合作为第一课实践”的原因之一。

#### 为什么 SPSC 不需要 CAS 竞争

SPSC 环形队列里：

- 只有一个生产者写 `write_pos_`；
- 只有一个消费者写 `read_pos_`；
- 双方只读对方的序号；
- 没有“多个线程同时改同一个序号”的竞争。

因此不需要 CAS 循环，也不需要退避。正确性完全由“单写者 + release/acquire 发布”保证。这正是“先做对 SPSC、不要从 MPMC 删锁”的原因之一：SPSC 的简单性来自角色约束，不是来自少写几行锁。

---

## 常见误区与设计权衡

### pitfalls：没有互斥锁的代码仍可能阻塞

这是最容易被忽略的一条。判断一段代码是不是 lock-free，不能只看“有没有 mutex”，要看**这条路径上所有操作**是否都能保证进展。常见的隐藏阻塞点：

- `new` / `malloc`：分配器内部可能有锁，也可能通过 `brk`/`mmap` 进入内核；
- 日志：同步写文件、格式化、加锁都可能阻塞；
- 文件 IO、网络 IO、RPC：系统调用本身就可能睡眠；
- 异常：抛出和栈展开不是“无锁”的；
- 缺页：第一次访问新分配的内存可能触发缺页中断；
- `!is_lock_free()` 的“原子”类型：标准允许 `std::atomic<T>` 内部用锁实现，这时所谓无锁路径其实是带锁的；
- `futex` 等待：条件变量、某些锁实现会在竞争时进入内核睡眠。

所以“用了 CAS”只是一个局部事实。要把整条路径的进展保证说清楚，必须把这些点逐个排掉，或者明确承认它们的存在。

### 误以为 lock-free = wait-free = 每个线程都不等待

这是概念层面最常见的混淆：

- lock-free 只保证系统整体至少有一个线程能推进；
- wait-free 才保证每个线程都在有界步数内完成；
- obstruction-free 更弱，只在“独自运行”时保证完成。

一个线程在 CAS 循环里重试一百万次，仍然符合 lock-free 的定义；它不是 wait-free。

### 从 MPMC 队列删掉 mutex 就当无锁

这是 sourceStudy 明确反对的做法。MPMC 队列里：

- 多个生产者可能同时写同一个 `write_pos_`；
- 多个消费者可能同时写同一个 `read_pos_`；
- 多个生产者可能同时写相邻槽位；
- “检查容量 + 写槽 + 更新序号”不是原子步骤。

把 mutex 删掉，这些竞争就变成数据竞争。正确做法要么用 CAS 重新设计每个共享状态，要么保留锁。**SPSC 的正确性建立在“每种角色只有一个线程”的约定上**，不能把这个约定悄悄放宽。

### 先发布序号，再写槽

顺序必须是：

```text
写槽 -> release 发布序号
```

反过来：

```text
release 发布序号 -> 写槽
```

消费者可能在看到新序号后读到尚未写完的槽。release/acquire 只能保证“release 之前的写”对 acquire 之后的读可见；它不会帮你把后面的写提前。

### 用 signed int 做头尾相减判断空满

```cpp
int w, r;
if (w - r == N) { /* 满 */ }
```

`int` 溢出是未定义行为。长时间运行后 `w` 可能超过 `INT_MAX`，判断直接失效。用 `std::size_t` 或 `std::uint64_t`，并保持 `r <= w` 的不变量。

### 容量不是 2 的幂，却仍用位运算取模

```cpp
slots_[w & (N - 1)]   // 只有 N 是 2 的幂时才等价于 w % N
```

如果 `N = 1000`，`w & 999` 不是取模，会访问错误槽位。要么把容量约束成 2 的幂并用 `static_assert` 钉死，要么老实写 `% N`。

### 多生产者误用 SPSC 队列

SPSC 的“单生产者”是硬约束，不是建议。两个生产者同时 `push` 时，可能：

- 读到同一个 `w`，写同一个槽，后写者覆盖先写者；
- 都执行 `write_pos_.store(w + 1)`，其中一个更新丢失；
- 容量判断交错，导致越界或覆盖未消费数据。

如果确实有多个生产者，要么加锁，要么使用经过验证的 MPSC/MPMC 实现，不能直接套本课的类。

### 在“无锁”路径里打日志或 new 大对象

日志和分配是典型的阻塞点。更糟的是，它们会掩盖真正的性能问题：你以为在测 CAS 循环，实际在测文件系统和分配器。无锁数据结构的内部路径应尽量不分配、不打印、不做 IO；需要记录时，用独立的异步通道。

### 忽略线性化，只靠“看起来能跑”

低并发下跑通不代表线性化正确。没有明确线性化点的实现，往往在下面这些地方出问题：

- 失败操作修改了部分状态；
- 两个操作的交错顺序无法用任何串行顺序解释；
- 读者看到新序号，但槽位数据还是旧的；
- 回收时机不对，读者读到已释放内存。

验收时要能逐行指出线性化点，并用压力测试验证“严格递增、无空洞、无重复”。

### tradeoffs：无锁复杂度高，优先成熟实现

无锁不是免费的：

- **正确性成本**：空满、ABA、内存回收、内存序，每一项都能独立出错；
- **可读性成本**：CAS 循环、回退路径、所有权交接，比一把 mutex 难懂得多；
- **可维护性成本**：偶发问题难复现，排障需要并发和内存模型知识；
- **平台成本**：`is_lock_free` 依赖平台，弱内存模型平台上更容易暴露顺序错误；
- **性能不确定性**：高竞争下 CAS 自旋可能比锁更差，尤其是临界区不短、线程数过多时。

因此生产环境的优先级应该是：

1. 先确认锁真的是瓶颈，有测量依据；
2. 能用一把 mutex + 有界队列解决，就先用它；
3. 需要无锁时，优先用成熟实现，例如 Folly、Boost.Lockfree、经过验证的 SPSC；
4. 自己手写 MPMC 无锁队列，除非团队有足够的并发经验和测试手段，否则风险很高。

多数后台路径——配置读取、任务分发、日志聚合、消息流水线——一把 mutex + 有界队列就够。把复杂度留给真正需要它的热点，而不是留给每个路过的人。

---

## 动手实践：固定容量 SPSC 环形队列与互斥队列对比

本节的 practice 是：**实现固定容量 SPSC 环形队列并证明发布顺序，与互斥队列比较。**

为了不让“比较”变成不公平的田忌赛马，约定：

- 两个队列容量都固定为 1024；
- 都只支持单生产者、单消费者；
- 满时 `push` 返回 `false`，空时 `pop` 返回 `false`；
- 生产者遇到满就重试，消费者遇到空就重试；
- 同一份负载、同一台机器、同一组编译选项；
- 只比较结构和吞吐，不在教案里编造任何性能数字。

### 文件结构

本实践只有一个文件 `spsc_bench.cpp`，包含：

1. `Payload`：带单调序号和校验和的测试元素；
2. `SpscRing<T, N>`：本课要证明的无锁环形队列；
3. `MutexQueue<T, N>`：互斥锁 + `std::deque` 的对照实现；
4. `boundary_test()`：空满边界与“不覆盖未消费数据”测试；
5. `run_bench()`：同负载吞吐对比；
6. `main()`：先跑边界测试，再跑两组基准。

### 完整代码

```cpp
// spsc_bench.cpp
// g++ -std=c++17 -pthread -O2 -o spsc_bench spsc_bench.cpp && ./spsc_bench
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <mutex>
#include <thread>

// 测试元素：seq 单调递增，check 用于发现“读到了不完整/旧数据”。
struct Payload {
    std::uint64_t seq;
    std::uint64_t check;
};

static std::uint64_t checksum(std::uint64_t seq) {
    return seq ^ 0x9e3779b97f4a7c15ULL;
}

// 固定容量 SPSC 环形队列。
// 约定：只有一个生产者线程调用 push，只有一个消费者线程调用 pop。
template <typename T, std::size_t N>
class SpscRing {
    static_assert(N > 1, "N must be greater than 1");
    static_assert((N & (N - 1)) == 0, "N must be a power of two");

public:
    bool push(const T& value) {
        // 只有生产者写 write_pos_，所以 relaxed 读自己的序号足够。
        const std::size_t w = write_pos_.load(std::memory_order_relaxed);

        // acquire：确认消费者已经读完将要覆盖的槽位。
        const std::size_t r = read_pos_.load(std::memory_order_acquire);
        if (w - r == N) {
            return false;  // 满：以这次 acquire load 的观察为线性化点
        }

        // 此时槽 w % N 归生产者独占，普通写即可。
        slots_[w & (N - 1)] = value;

        // 线性化点：release store 发布“该槽已填好”。
        // 消费者 acquire 读到新值后，一定能看到上面写入的完整 value。
        write_pos_.store(w + 1, std::memory_order_release);
        return true;
    }

    bool pop(T& out) {
        // 只有消费者写 read_pos_，所以 relaxed 读自己的序号足够。
        const std::size_t r = read_pos_.load(std::memory_order_relaxed);

        // acquire：确认生产者已经发布该槽的完整数据。
        const std::size_t w = write_pos_.load(std::memory_order_acquire);
        if (r == w) {
            return false;  // 空：以这次 acquire load 的观察为线性化点
        }

        // 此时槽 r % N 归消费者独占，普通读即可。
        out = slots_[r & (N - 1)];

        // 线性化点：release store 归还空槽。
        // 生产者 acquire 读到新值后，才敢覆盖这个槽。
        read_pos_.store(r + 1, std::memory_order_release);
        return true;
    }

private:
    // 分开缓存行，避免生产者与消费者的序号互相“打乒乓”。
    alignas(64) std::atomic<std::size_t> write_pos_{0};
    alignas(64) std::atomic<std::size_t> read_pos_{0};
    alignas(64) T slots_[N];
};

// 对照实现：互斥锁 + 双端队列，容量同样固定。
template <typename T, std::size_t N>
class MutexQueue {
public:
    bool push(const T& value) {
        std::lock_guard<std::mutex> lock(mu_);
        if (queue_.size() == N) {
            return false;
        }
        queue_.push_back(value);
        return true;
    }

    bool pop(T& out) {
        std::lock_guard<std::mutex> lock(mu_);
        if (queue_.empty()) {
            return false;
        }
        out = queue_.front();
        queue_.pop_front();
        return true;
    }

private:
    std::mutex mu_;
    std::deque<T> queue_;
};

// 空满边界测试：容量取 4，便于手工推演。
static bool boundary_test() {
    SpscRing<Payload, 4> q;
    Payload out{};
    bool ok = true;

    // 1. 空队列 pop 必须失败。
    ok = !q.pop(out) && ok;

    // 2. 连续填满 4 个槽。
    for (std::uint64_t i = 0; i < 4; ++i) {
        ok = q.push(Payload{i, checksum(i)}) && ok;
    }

    // 3. 满时 push 必须失败，且不能覆盖已有数据。
    ok = !q.push(Payload{99, checksum(99)}) && ok;

    // 4. 消费前两个，检查内容完整。
    for (std::uint64_t i = 0; i < 2; ++i) {
        ok = q.pop(out) && out.seq == i && out.check == checksum(i) && ok;
    }

    // 5. 腾出两个空位后，push 必须重新成功。
    ok = q.push(Payload{4, checksum(4)}) && ok;
    ok = q.push(Payload{5, checksum(5)}) && ok;
    ok = !q.push(Payload{100, checksum(100)}) && ok;  // 又满了

    // 6. 剩余元素必须是 2,3,4,5，严格递增、无覆盖。
    for (std::uint64_t i = 2; i < 6; ++i) {
        ok = q.pop(out) && out.seq == i && out.check == checksum(i) && ok;
    }

    // 7. 全部取完后再次 pop 必须失败。
    ok = !q.pop(out) && ok;
    return ok;
}

// 同一份负载下的吞吐对比。
template <typename Queue>
double run_bench(const char* name, Queue& q, std::uint64_t total) {
    std::atomic<std::uint64_t> popped{0};
    std::uint64_t bad = 0;

    const auto start = std::chrono::steady_clock::now();

    std::thread consumer([&] {
        std::uint64_t expected = 0;
        Payload p{};
        while (popped.load(std::memory_order_relaxed) < total) {
            if (!q.pop(p)) {
                std::this_thread::yield();
                continue;
            }
            if (p.seq != expected || p.check != checksum(p.seq)) {
                ++bad;
            }
            expected = p.seq + 1;
            popped.fetch_add(1, std::memory_order_relaxed);
        }
    });

    std::thread producer([&] {
        for (std::uint64_t i = 0; i < total; ++i) {
            Payload p{i, checksum(i)};
            while (!q.push(p)) {
                std::this_thread::yield();
            }
        }
    });

    producer.join();
    consumer.join();
    const auto end = std::chrono::steady_clock::now();

    const double seconds = std::chrono::duration<double>(end - start).count();
    std::printf("%-12s total=%llu bad=%llu time=%.3fs throughput=%.0f ops/s\n",
                name,
                static_cast<unsigned long long>(total),
                static_cast<unsigned long long>(bad),
                seconds,
                seconds > 0.0 ? static_cast<double>(total) / seconds : 0.0);
    return seconds;
}

int main() {
    std::printf("boundary_test: %s\n", boundary_test() ? "PASS" : "FAIL");

#if defined(TSAN_RUN)
    constexpr std::uint64_t kTotal = 200000;
#else
    constexpr std::uint64_t kTotal = 5000000;
#endif

    SpscRing<Payload, 1024> ring;
    MutexQueue<Payload, 1024> mutex_queue;

    run_bench("SpscRing", ring, kTotal);
    run_bench("MutexQueue", mutex_queue, kTotal);
    return 0;
}
```

---

### 证明发布顺序

现在把“为什么消费者一定能看到完整槽位”写成可检查的链条。

生产者 `push` 成功路径：

1. `w = write_pos_.load(relaxed)`：读取自己的写序号；
2. `r = read_pos_.load(acquire)`：确认槽位空闲。这次 acquire 与消费者归还空槽的 release store 建立同步，保证消费者已经读完该槽；
3. `slots_[w & (N - 1)] = value`：普通写。此刻该槽归生产者独占，没有其他线程读它；
4. `write_pos_.store(w + 1, release)`：**线性化点**。这次 release 把“槽位已填好”发布出去。

消费者 `pop` 成功路径：

1. `r = read_pos_.load(relaxed)`：读取自己的读序号；
2. `w = write_pos_.load(acquire)`：确认有数据。若读到生产者发布的新值，则与第 4 步的 release store 建立 synchronizes-with；
3. `out = slots_[r & (N - 1)]`：普通读。因为第 2 步已经 acquire，生产者第 3 步的写入对它可见；
4. `read_pos_.store(r + 1, release)`：**线性化点**。这次 release 把“槽位已空出”发布回生产者。

happens-before 链：

```text
生产者: slots_[i] = value
        sequenced-before
        write_pos_.store(w + 1, release)
                 ||
                 || synchronizes-with（消费者 acquire 读到了 w + 1）
                 vv
消费者: write_pos_.load(acquire) 返回 w + 1
        sequenced-before
        out = slots_[i]
```

因此消费者读到新序号后，读到的 `slots_[i]` 一定是生产者本次发布的完整值，而不是旧值、半更新值或未初始化值。

反向交接同样成立：

```text
消费者: out = slots_[i]
        sequenced-before
        read_pos_.store(r + 1, release)
                 ||
                 || synchronizes-with（生产者 acquire 读到了 r + 1）
                 vv
生产者: read_pos_.load(acquire) 返回 r + 1
        sequenced-before
        slots_[i] = next_value   // 可以安全覆盖
```

这两条链合起来，就是“槽位所有权”的完整证明：

- 空槽归生产者，生产者写完并 release 后，所有权交给消费者；
- 已填充槽归消费者，消费者读完并 release 后，所有权交回生产者；
- 所有权交接只通过 `write_pos_` / `read_pos_` 的 release/acquire 完成；
- 任一时刻，一个槽要么归生产者、要么归消费者，不会同时被两边访问。

这也解释了为什么 SPSC 不需要 CAS：`write_pos_` 只有生产者写，`read_pos_` 只有消费者写，不存在“多个线程争同一个变量”的情况。需要 CAS 的是多写者场景，不是这里。

### 功能测试：长时间运行不覆盖未消费数据

`run_bench()` 里消费者做三件事：

1. 检查 `p.seq == expected`。`expected` 从 0 开始，每消费一个加一。任何覆盖、丢失、重复都会让序号对不上；
2. 检查 `p.check == checksum(p.seq)`。即使序号碰巧对，校验和也能发现“读到了旧数据/半更新数据”；
3. 统计 `bad`。只要 `bad != 0`，就说明队列在压力下违反了发布顺序或所有权约定。

默认跑 500 万条。这个规模足以覆盖：

- 队列反复填满、排空；
- 生产者等待消费者腾出空位；
- 消费者等待生产者发布新数据；
- 序号跨越多个 1024 的边界；
- 长时间运行下的空满判断。

如果发生“覆盖未消费数据”，典型表现是消费者看到重复序号、跳号，或者校验和不匹配。测试不需要读内部状态，只看外部可观察的序号和内容，这也是线性化测试的优点。

### 边界测试：空、满、再腾空

`boundary_test()` 用容量 4 的小队列，逐步验证：

| 步骤 | 操作 | 期望 |
| --- | --- | --- |
| 1 | 空队列 `pop` | 返回 `false`，不修改 `out` 的语义由调用方决定 |
| 2 | 连续 `push` 4 个 | 全部返回 `true` |
| 3 | 第 5 个 `push` | 返回 `false`，已有 4 个元素不变 |
| 4 | `pop` 两个 | 依次得到序号 0、1，校验和正确 |
| 5 | 再 `push` 两个 | 返回 `true`，因为已腾出两个空位 |
| 6 | 再 `push` 一个 | 返回 `false`，又满了 |
| 7 | 依次 `pop` 剩余 | 得到 2、3、4、5，严格递增、无覆盖 |
| 8 | 全部取完后 `pop` | 返回 `false` |

这里最关键的是第 3 步和第 7 步：满时 `push` 失败后，原来的 0、1、2、3 必须原样保留；消费两个再写入 4、5 后，剩余元素必须是 2、3、4、5，不能出现 4 覆盖 2 的情况。只要 `push` 在满时错误地写了槽，第 7 步就会暴露。

### 对比测试：SpscRing 与 MutexQueue

`run_bench()` 对两个队列使用完全相同的负载：

- 生产者连续 `push` 0 到 `total - 1`，失败就 `yield` 后重试；
- 消费者连续 `pop`，失败就 `yield` 后重试；
- 两边都校验序号和校验和；
- 用 `std::chrono::steady_clock` 统计从启动到两个线程都结束的墙钟时间；
- 打印总耗时和 `ops/s`。

需要注意的公平性问题：

1. `MutexQueue` 内部用 `std::deque`，每次 `push_back` 可能分配内存；`SpscRing` 是预分配数组。这是结构差异的一部分，比较的是“两种队列方案”，不是“纯锁开销 vs 纯 CAS 开销”。
2. 两个队列都只有一个生产者和一个消费者，锁竞争只发生在两个线程之间；线程数更多的场景结论可能不同。
3. `yield` 的行为依赖操作系统调度，负载和机器状态会影响结果。
4. 优化级别、CPU 频率、是否绑定核、是否有其他进程，都会影响数字。

所以不要从一次运行得出“无锁一定快多少”的结论。正确做法是：在目标环境、目标负载、目标编译选项下多跑几次，记录环境，观察趋势和尾部延迟，再决定是否值得替换。教案里不提供任何跑出来的数字，避免把某台机器的结果当成普遍规律。

### 编译与运行

常规构建（性能对比用这一份）：

```bash
g++ -std=c++17 -pthread -O2 -o spsc_bench spsc_bench.cpp && ./spsc_bench
```

ThreadSanitizer 构建（只用于找数据竞争，不要拿它的输出去比较性能）：

```bash
g++ -std=c++17 -pthread -O1 -g -DTSAN_RUN -fsanitize=thread -o spsc_tsan spsc_bench.cpp && ./spsc_tsan
```

说明：

- `-DTSAN_RUN` 把总元素数降到 20 万，避免插桩后跑太久；它不改变队列逻辑；
- TSAN 对原子操作本身有理解，主要能帮你发现“槽位访问没有正确同步”这类非原子数据竞争；
- TSAN 会显著拖慢程序，不要用它测吞吐；
- 如果 TSAN 报告竞争，先检查是不是把 `release`/`acquire` 写成了 `relaxed`，或者是不是有第二个线程误用了同一端。

### 验收清单

| 验收项 | 检查方式 | 通过标准 |
| --- | --- | --- |
| 能指出每次操作的线性化点 | 代码注释 + 口头解释 | `push` 成功是发布 `write_pos_` 的 release store；`pop` 成功是推进 `read_pos_` 的 release store；失败操作以对应的 acquire load 为观察点 |
| 空队列 `pop` 不返回数据 | `boundary_test()` 第 1 步 | 返回 `false`，不推进 `read_pos_` |
| 满队列 `push` 不覆盖 | `boundary_test()` 第 3、7 步 | 返回 `false`，已有元素内容不变 |
| 腾出空位后能继续写 | `boundary_test()` 第 5 步 | 返回 `true` |
| 长时间运行不覆盖未消费数据 | `run_bench()` 的 `bad` 计数 | `bad == 0`，序号严格递增、无空洞、无重复 |
| 发布顺序成立 | 代码走查 + 内存序检查 | 先写槽，再 release 发布序号；读侧先 acquire 再读槽 |
| SPSC 约束没有被破坏 | 代码走查 | 只有一个生产者线程调用 `push`，只有一个消费者线程调用 `pop` |

---

## sourceStudy：先研究槽位所有权，再核对内存序

节点给出的 sourceStudy 是：**先研究成熟 SPSC 算法的槽位所有权，再核对原子内存序；不要从 MPMC 结构直接删锁。** 把它拆成一份对照清单，写完代码后逐项打勾。

### 清单 1：槽位所有权

- 每个槽在任一时刻只属于生产者或只属于消费者；
- 生产者拥有空槽：可以在里面写，消费者不能读；
- 消费者拥有已填充槽：可以在里面读，生产者不能覆盖；
- 交接只通过原子序号的发布完成：生产者 release 发布 `write_pos_`，消费者 acquire 接收；消费者 release 归还 `read_pos_`，生产者 acquire 接收；
- 所有权不依赖“大家约定不碰”，而依赖“只有一方会写这个序号”这个结构约束。

对照本课代码：`push` 在 `w - r < N` 时独占 `slots_[w & (N - 1)]`；`pop` 在 `r != w` 时独占 `slots_[r & (N - 1)]`。两个条件分别由对方发布的序号保证。

### 清单 2：内存序

- 写数据用普通写：槽此时由生产者独占，不需要原子操作；
- 发布序号用 `release`：保证“之前的槽位写入”不会被重排到发布之后；
- 读取序号用 `acquire`：保证“之后的槽位读取”不会被重排到读取之前；
- `relaxed` 只能用于不同时承担发布职责的计数：例如生产者读自己的 `write_pos_`、消费者读自己的 `read_pos_`；
- 双方读对方序号时不能省 `acquire`：生产者的 `read_pos_.load(acquire)` 防止覆盖消费者正在读的槽；消费者的 `write_pos_.load(acquire)` 防止读到未发布完的槽。

### 清单 3：为什么不能从 MPMC 删锁

- 多个生产者可能同时写相邻槽或同一个 `write_pos_`；
- 多个消费者可能同时写相邻槽或同一个 `read_pos_`；
- “检查容量 + 写槽 + 更新序号”在 MPMC 下不是原子步骤；
- 删掉 mutex 但没有用 CAS 重新设计每个共享状态，就会产生数据竞争；
- SPSC 的正确性建立在“每种角色只有一个线程”的约定上，不能悄悄放宽。

如果业务确实有多个生产者，应该：

1. 先考虑用锁把多个生产者串行化，再接到 SPSC；
2. 或者使用经过验证的 MPSC/MPMC 实现；
3. 不要在本课的类上直接加一个“多生产者也能用”的注释。

### 清单 4：对照 is_lock_free 页面

- 确认 `write_pos_` / `read_pos_` 所用的 `std::atomic<std::size_t>` 在目标平台是 lock-free；
- 若不是，所谓“无锁队列”内部可能退化成带锁原子；
- 记录标准版本：`is_always_lock_free` 是 C++17 的静态成员；`is_lock_free()` 是运行时成员函数；C 兼容接口是 `atomic_is_lock_free`；
- 记录平台、编译器、标准库版本和实际输出，不把某次输出当成所有平台的结论；
- 页面没有展开的算法语义（例如线性化点如何选、SPSC 如何证明），以本课讲解和实验为准，不要张冠李戴。

---

## 自测题

### 题 1：三种进展保证与锁

某团队把一个互斥锁保护的队列改成 CAS 重试队列，宣称“现在每个线程都不会等待了”。上线后发现个别线程在极端竞争下延迟很高，但系统整体没有卡死。

请回答：

1. 这个队列最可能属于 wait-free、lock-free 还是 obstruction-free？为什么？
2. “每个线程都不会等待”这个说法错在哪里？
3. 原来的互斥锁实现为什么通常不算 lock-free？请给出一个具体场景。
4. 如果要求每个线程都在有界步数内完成，需要什么级别的保证？代价是什么？

**答案：**

1. 最可能是 lock-free。CAS 重试循环保证“至少有一个线程能成功推进”，但不保证每个线程都在有界步数内完成；个别线程可能一直失败、延迟很高，但系统整体没有卡死。它不是 wait-free，因为没有“每线程有界步数”的承诺；也不是 obstruction-free 的典型形态，因为 obstruction-free 只在“其他线程暂停、某线程独自运行”时才保证完成，而 CAS 循环在持续竞争下仍能保证整体推进。
2. 错在把 lock-free 当成了 wait-free。lock-free 只承诺系统整体推进，不承诺每个线程不等待；CAS 失败重试本身就是一种等待。个别线程可能饿很久，只是系统不会整体停住。
3. 互斥锁的进展依赖持锁线程。比如持锁线程发生缺页、阻塞在文件 IO、被调度走，甚至崩溃没有释放锁，其他等锁线程就全部停住，系统无法保证“至少一个线程能完成”。因此基于锁的算法通常不是 lock-free。
4. 需要 wait-free：每个线程的操作都能在有界步数内完成，不依赖其他线程是否运行或失败。代价通常是更高的空间开销、更复杂的算法、更多的复制/汇总逻辑，而且很多常见结构很难做到 wait-free。

### 题 2：线性化点与发布顺序

下面是一段 SPSC 环形队列的生产者代码：

```cpp
void push_bad(const T& value) {
    std::size_t w = write_pos.load(std::memory_order_relaxed);
    std::size_t r = read_pos.load(std::memory_order_acquire);
    if (w - r == N) return;

    write_pos.store(w + 1, std::memory_order_release);
    slots[w % N] = value;
}
```

请回答：

1. 这段代码的发布顺序有什么问题？在弱内存模型平台上可能出现什么现象？
2. 正确的顺序是什么？请写出正确的两行，并标明线性化点。
3. 如果把生产者的 `read_pos.load(acquire)` 改成 `relaxed`，会有什么风险？
4. 消费者侧的线性化点通常选在哪里？为什么不是“读到数据”的那一刻？

**答案：**

1. 它先发布 `write_pos`，再写槽位。消费者可能先看到新序号，然后去读一个还没写完的槽，读到旧值、半更新值或未初始化值。在 x86 上可能偶发，在弱内存模型平台上更容易稳定复现。release 只能保证“release 之前的写”对 acquire 之后的读可见，不能把后面的槽位写入提前到发布之前。
2. 正确顺序是先写槽、再 release 发布：

```cpp
slots[w % N] = value;
write_pos.store(w + 1, std::memory_order_release);  // 线性化点
```

这次 release store 是 `push` 的线性化点，它让“槽位已填好”对外生效。

3. 生产者用 `relaxed` 读 `read_pos` 时，可能看到“已经空了”的计数，却看不到消费者对槽位的读取已经完成，从而覆盖消费者正在读的数据。生产者需要 acquire 读 `read_pos`，与消费者归还空槽的 release store 建立同步，确认该槽真的已经归还。
4. 消费者的线性化点通常选在“推进 `read_pos` 的 release store”。读到数据只是操作过程中的一步，此时操作还没有对外生效；只有把读序号推进并发布，其他线程（这里主要是生产者）才能观察到“这个槽已经被消费”，操作才在串行历史中生效。失败 `pop` 则可以取观察到 `r == w` 的那次 acquire load 作为线性化点。

### 题 3：没有锁也会阻塞，以及边界与选型

某服务把日志队列换成“无锁”实现后，发现 P99 延迟反而变差，偶尔还有线程卡住。代码里没有任何 mutex，只有一个 CAS 循环。

请回答：

1. 列出至少四个可能让这条“无锁”路径阻塞的原因。
2. 如果队列满时直接覆盖最旧元素，会破坏本课的哪条验收？为什么？
3. 为什么本课用固定容量环形队列，而不是动态节点链表？请从内存回收角度回答。
4. 团队想自己手写一个 MPMC 无锁队列替换现有 mutex 队列。按照本课 tradeoffs，你会给出什么建议？

**答案：**

1. 可能的阻塞点包括：`new`/`malloc` 分配节点（分配器内部锁、系统调用）；日志格式化或同步 `write`（文件 IO）；缺页中断；异常抛出与栈展开；`std::atomic<T>` 在目标平台上 `!is_lock_free()`，内部用锁；条件变量或 futex 等待；下游 RPC/网络 IO。CAS 只保证那次原子操作的进展，不保证它前后的普通操作不阻塞。
2. 会破坏“空满边界和长时间运行不覆盖未消费数据”。覆盖最旧元素意味着生产者在队列满时仍然写槽，消费者可能还没读就被覆盖，外部可观察的序号会出现空洞、重复或校验和不匹配；这违反了“消费一个之前该槽归消费者所有”的所有权约定。
3. 动态节点链表弹出节点后不能立刻 `delete`，因为其他线程可能还持有指针；需要 hazard pointer、epoch、引用计数等回收机制。固定容量环形队列用预分配槽位，把问题转化为“槽位何时可以覆盖”，而这个问题可以用序号发布和所有权交接证明，避开了动态节点回收。代价是容量固定、元素需要可默认构造/可赋值。
4. 建议先确认现有 mutex 队列是否真的是瓶颈，有测量依据再动；即使要换，也优先用成熟实现（Folly、Boost.Lockfree、经过验证的 MPMC），不要自己从 SPSC 或“删掉锁的 MPMC”改起。手写 MPMC 极易在空满、ABA、内存回收、内存序上出错，复杂度和维护成本很高；多数后台路径一把 mutex + 有界队列就够。

---

## 推荐资源

- **无锁进展保证与CAS结构：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/atomic/atomic/is_lock_free.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：深入
  - 是否免费：是
  - 推荐理由：按节点建议，把 wait-free / lock-free / obstruction-free 与线性化点作为本课主线；该页面用于核对“原子类型是否真的无锁”这一关键边界。重点阅读成员函数 `is_lock_free()`、C++17 的静态成员 `is_always_lock_free`，以及 C 兼容的 `atomic_is_lock_free`，并用本课的 `atomic_probe.cpp` 在目标平台实际验证 `std::atomic<std::size_t>` 等类型。页面没有展开的 SPSC 槽位所有权、线性化点选择和 CAS 退避策略，以本课讲解和实验为准，不把页面没有的内容当成页面结论。

---

## 本课小结

1. **无锁的核心是进展保证，不是“没有 mutex”这个外观。** lock-free 保证系统整体至少有一个线程能推进；wait-free 保证每个线程都在有界步数内完成；obstruction-free 只在“独自运行”时保证完成。
2. **lock-free 不等于每个线程无等待。** CAS 失败者可能长时间重试，个别线程可能饿；系统整体不卡死，不代表每个请求的延迟都稳定。
3. **锁通常不是 lock-free。** 持锁线程被调度走、阻塞在 IO 或缺页、甚至崩溃，都会让其他线程停住。锁的语义就是“等持锁者”，这与“至少一个线程能完成”是冲突的。
4. **没有互斥锁的代码仍可能阻塞。** `new`/`malloc`、日志、文件 IO、异常、缺页、`!is_lock_free()` 的原子类型、futex 等待，都会破坏“无锁”的承诺。判断进展保证要看整条路径，不能只看 CAS。
5. **线性化点是正确性的坐标。** 每个成功操作都要能指出“在哪一行原子操作上生效”；失败操作也要有观察点。SPSC 中 `push` 的线性化点是发布 `write_pos_` 的 release store，`pop` 的线性化点是推进 `read_pos_` 的 release store。
6. **发布顺序必须是“先写槽，再发布序号”。** release/acquire 建立 happens-before，让消费者看到新序号时一定看到完整槽位；反过来先发布再写槽，就是数据竞争和读到半成品的根源。
7. **槽位所有权是 SPSC 的证明骨架。** 空槽归生产者，写完后通过 release 交给消费者；已填充槽归消费者，读完后通过 release 交回生产者。交接只通过原子序号完成，任一时刻一个槽只属于一方。
8. **SPSC 不需要 CAS 竞争。** 只有一个生产者写 `write_pos_`、一个消费者写 `read_pos_`，双方只读对方序号；这正是“先做对 SPSC、不要从 MPMC 删锁”的原因。多生产者/多消费者会引入对同一序号的竞争，必须重新设计。
9. **内存回收是正确性的另一半。** 动态节点弹出后不能立刻 `delete`，需要 hazard pointer、epoch、引用计数或延迟回收；固定容量环形缓冲用槽位所有权避开了这部分问题，代价是容量固定。
10. **CAS 重试要配合退避，ABA 要单独处理。** 高竞争下纯空转会把缓存行打热，可用 `pause`/`yield` 和指数退避；CAS 看到值没变不代表中间没被改过，本课用单调递增序号 + 固定槽位避开经典 ABA。
11. **无锁复杂度高，优先成熟实现。** 只有在明确的性能约束和测量依据下，才考虑用无锁替换锁；自己手写 MPMC 极易在空满、ABA、回收、内存序上出错，生产环境应优先用 Folly、Boost.Lockfree 或经过验证的 SPSC。
12. **验收要可执行。** 能指出每次操作的线性化点；空队列 `pop` 失败；满队列 `push` 失败且不覆盖；腾空后能继续写；长时间运行中序号严格递增、无空洞、无重复、校验和正确。用 `boundary_test()` 和 `run_bench()` 把这几条钉死，再谈性能。

下一课会继续在并发数据结构里处理更细的同步与生命周期问题；本课留下的工作台是：**先判断需要哪种进展保证 → 再写清楚槽位所有权和线性化点 → 用 release/acquire 发布序号 → 用边界与长跑测试验证不覆盖 → 最后才在目标环境比较是否值得替换锁。**
