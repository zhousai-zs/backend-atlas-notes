# 第 59/208 课：IO线程与工作线程池

- **阶段**：第 3 阶段 · 传输与Socket
- **前置课程**：d-reactor、c-thread
- **关键词**：Server Thread Pools、线程池、任务队列、有界队列、背压、one loop per thread、IO线程、工作线程、结果回投、queueInLoop、退出策略、停机、任务取消、future、eventfd、线程亲和性
- **难度**：进阶
- **所属领域**：D · IO模型与事件（子类：调度与资源复用）
- **标签**：Server Thread Pools、D、C++

---

## 导读

上一课我们给 Reactor 补上了定时器：Poller 等 IO 就绪，TimerQueue 等截止时间，EventLoop 在同一个线程里串行处理「读、写、超时、跨线程任务」。到这里，一个单线程 Reactor 已经能正确服务大量连接——**只要每个回调都很快返回**。

但真实的后端接入层里，回调并不总是「读几个字节、解一个包头」这么轻：

- 公众号回调请求要做**签名校验**，可能涉及多次哈希与证书/密钥查找；
- 大 JSON 包要解析、校验、映射成业务对象；
- 模板消息要组装文案、查用户资料、做敏感词过滤；
- 下游 RPC（用户标签、素材库、客服接口）可能阻塞几十毫秒甚至更久；
- 图片/语音素材要做转码、缩略图、内容安全检查。

这些工作如果直接写在 EventLoop 的 `onMessage` 里，会发生什么？

```text
连接 A 的回调开始做 80ms 的下游 RPC
  └─ 整个 EventLoop 线程被占住 80ms
       ├─ 连接 B/C/D... 的 EPOLLIN 没人处理
       ├─ 定时器到期回调迟到
       ├─ 跨线程 queueInLoop 的任务堆积
       └─ 所有连接一起「假死」，直到 A 的回调返回
```

这就是典型的 **head-of-line blocking（队头阻塞）**：Reactor 的串行模型是正确性资产，但也是稀缺资源。**任何 CPU 密集或可能阻塞的调用，都不能长期占用 EventLoop 线程。**

本课的定位来自节点 summary：

> 线程池复用执行资源；IO线程与工作线程分工需要明确队列、背压、状态归属和退出策略。

拆开看是四件事：

1. **复用执行资源**：不要为每个任务 `pthread_create`，用固定数量的工作线程反复取任务执行；
2. **队列与背压**：IO 线程把任务放进队列，队列必须有界；满了要拒绝、限流或暂停读取，而不是无限堆积；
3. **状态归属**：连接对象只归它的 IO 线程所有；工作线程不能直接 `conn->send()`，只能把结果回投给所属 EventLoop；
4. **退出策略**：停机时先停止接收新任务，再排空或丢弃队列，`join` 工作线程，最后销毁 EventLoop；任何任务都不能在 loop 销毁后还去碰它。

放到腾讯微信公众号后台的接入层，一个常见分工是：

```text
                    ┌─────────────────────────────┐
  粉丝/第三方  ───▶ │  IO 线程（one loop per thread）│
  长连接/HTTP      │  只做：read/write、拆包、协议解析 │
                    │  连接状态只在本线程修改          │
                    └──────────┬──────────────────┘
                               │ 提交任务（有界队列）
                               ▼
                    ┌─────────────────────────────┐
                    │  工作线程池（不跑 EventLoop）  │
                    │  签名校验、大 JSON 解析、       │
                    │  模板消息组装、下游 RPC、转码    │
                    └──────────┬──────────────────┘
                               │ queueInLoop / eventfd 回投
                               ▼
                    ┌─────────────────────────────┐
                    │  连接所属 IO 线程写回响应       │
                    └─────────────────────────────┘
```

注意最后一步：**工作线程不直接写 socket，也不直接改连接状态**。它把「结果」打包成一个回调，投递回连接所属的 EventLoop，由那个线程完成发送、关闭、更新统计。这条纪律和课 57 的线程亲和性一脉相承。

本课不追求「线程越多越好」，而是建立一套可上线的边界：

- 队列多长？满了怎么办？
- 多少工作线程？依据是什么？
- 任务做完怎么把结果送回去？
- 进程退出、连接关闭、任务取消同时发生时，谁先谁后？

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释任务队列和有界容量**
   - 说清队列在 IO 线程与工作线程之间承担「解耦 + 缓冲」的作用。
   - 说清无界队列为什么只是把过载转换成内存增长和长尾延迟。
   - 能列举队列满时的几种策略：阻塞等待、立即拒绝、丢弃最旧、丢弃最新，以及它们分别适合什么场景。
   - 能把「队列满」转换成接入层背压：暂停 `EPOLLIN`、暂停 `accept`、返回 429/503、缩短读窗口等。

2. **能独立完成：实现有界工作线程池，把 CPU 任务从事件循环转移并返回结果**
   - 工作线程只从有界队列取任务，不持有 EventLoop，不直接操作连接。
   - 提交任务返回 `future` 或回调句柄；任务完成后通过 `queueInLoop` 把结果回投到连接所属 loop。
   - 队列满时明确拒绝或限流，并记录指标。
   - 停机顺序正确：停止接收新任务 → 排空或丢弃队列 → `join` 工作线程 → 再销毁 loop。

3. **满足本课验收标准（acceptance）**
   - 队列满时明确拒绝或限流，而不是无限堆积。
   - 停机后无任务访问已销毁的 loop（ASAN/TSAN 干净，弱指针检查生效）。

---

## 为什么后端要懂

节点里的理由是：

> 防止CPU任务或阻塞调用拖慢所有连接。

这句话看似简单，背后至少有四层工程含义。

### 1. 一个慢回调会拖慢整批连接

EventLoop 是单线程串行的。假设一个 loop 上挂着 3000 条长连接，某个 `onMessage` 里同步调用了下游接口，耗时 100ms：

- 这 100ms 内，其他连接的读写全部排队；
- 内核 socket 接收缓冲区可能被填满，对端开始重传；
- 心跳/空闲超时定时器迟到，监控出现「定时器延迟」尖刺；
- 如果下游继续抖动到 1s，连接会被用户感知为「卡死」。

把这类工作丢进工作线程池，loop 只需要做「提交任务」这个微秒级动作，就能继续服务其他连接。**线程池的第一价值不是「并行加速」，而是「隔离阻塞，保护 IO 线程的响应性」。**

### 2. 线程创建很贵，复用才可持续

每来一个任务就 `std::thread` 或 `pthread_create`：

- 创建/销毁线程涉及内核栈、调度实体、TLS 等开销；
- 高峰期可能瞬间创建上万个线程，把内存和调度器压垮；
- 线程数量不可控，无法做容量规划。

线程池用固定数量的 worker 反复取任务，把「创建线程」的成本摊到进程生命周期里。这是**资源复用**，也是节点子类「调度与资源复用」的直接含义。

### 3. 没有背压，过载会变成内存和长尾

如果 IO 线程提交任务的速度长期高于工作线程消费速度，而队列又没有上限：

- 队列长度持续增长，内存上涨；
- 任务在队列里排队越来越久，P99 延迟不断恶化；
- 最终 OOM 或 GC/分配器抖动，整机一起崩。

**有界队列 + 明确拒绝策略**把「系统性崩溃」转换成「可观测的局部失败」：一部分请求快速收到 429/503，核心连接仍然存活。这是后端稳定性的基本功。

### 4. 停机与取消是最容易被忽略的正确性来源

线上发布、缩容、连接关闭时，任务、线程、EventLoop 的生命周期会交错：

- 工作线程还在跑任务，loop 已经被销毁，任务回调里访问 `loop` → UAF；
- 停机时先 `join` 工作线程，但工作线程正阻塞等待 loop 回应 → 死锁；
- 队列里还有任务持有连接对象的裸指针，连接已析构 → UAF；
- 任务取消后仍把过期结果写回连接 → 协议错乱或越权。

节点 acceptance 明确要求「停机后无任务访问销毁的 loop」。这不是锦上添花，而是线程池能不能上生产的分水岭。

---

## 直觉 → 原理

### 0. 先建立直觉：EventLoop 是「稀缺的串行资源」

把 EventLoop 想成一家只有一个收银员的便利店：

- 收银员负责收钱（读写 socket）、回答常见问题（协议解析）、处理闹钟（定时器）；
- 如果某个顾客要求收银员去后仓找货 10 分钟（CPU/阻塞任务），整条队伍都停了；
- 正确做法是：收银员记下需求，交给后仓的理货员（工作线程）去做，自己继续收钱；
- 理货员做完后，把结果放回柜台，由收银员交给顾客（回投 loop 写回）。

这个类比里有两个关键约束：

1. **柜台容量有限**：后仓待处理单子不能无限堆；堆满了要告诉顾客「稍后再来」或「现在排队太长」。
2. **只有收银员能碰收银台**：理货员不能直接改收银机上的订单状态；结果必须交回收银员处理。

对应到代码：有界队列、连接归属、结果回投。

---

### 1. 任务队列和有界容量

#### 1.1 队列解决什么问题

IO 线程与工作线程是**生产者—消费者**关系：

- 生产者：IO 线程，提交 `Task`；
- 消费者：工作线程，取出 `Task` 执行；
- 队列：解耦两者的速率，提供短暂缓冲。

没有队列，IO 线程就只能同步等任务完成，等于没卸载。队列让 IO 线程在「提交」后立刻返回。

但队列不是越大越好。队列容量决定了系统能吸收多少突发，也决定了过载时任务要等多久。

#### 1.2 无界队列的陷阱

节点 pitfalls 说得很直接：

> 无限队列只把过载转成内存增长和长尾延迟。

设：

- 任务到达速率 λ（个/秒）；
- 工作线程总服务速率 μ（个/秒）；
- 队列容量 Q（无界时 Q=∞）。

当 λ > μ 持续存在：

- 队列长度按 `(λ - μ) × t` 增长；
- 每个任务的排队时间越来越长；
- 内存随队列增长；
- 即使后来 λ 回落，队列里也积压着大量「已经过期」的请求，用户早已超时。

更糟的是，这些任务可能仍然持有大对象（请求体、连接引用、临时 buffer），内存放大远超任务本身。

**结论**：队列必须有界。容量应依据「可接受的最大排队延迟」和「单任务平均内存」来定，而不是拍脑袋。

一个粗略估算：

```text
最大排队延迟 ≈ 队列容量 / 工作线程总服务速率
队列内存     ≈ 队列容量 × 单任务平均持有内存
```

例如 8 个 worker，单任务平均 5ms，则 μ = 1600 任务/秒。若希望排队不超过 200ms，队列容量约 `1600 × 0.2 = 320`。再乘单任务 20KB，队列内存约 6.4MB，可以接受。

这只是一个起点，真实值要用压测和线上直方图校准。

#### 1.3 队列满时的四种策略

| 策略 | 行为 | 优点 | 代价 | 适用 |
|------|------|------|------|------|
| **阻塞等待（block）** | 生产者睡在条件变量上，直到有空位 | 不丢任务、天然背压 | **绝不能用在 EventLoop 线程**，会堵死整个 loop | 后台批处理、非 IO 线程 |
| **立即拒绝（reject）** | `tryPush` 返回 false，调用方返回 429/503 或关闭连接 | 快速失败、保护内存、延迟可控 | 需要业务能接受部分失败 | 在线请求、可重试接口 |
| **丢弃最旧（drop-oldest）** | 队头出队，新任务入队 | 保留最新请求 | 被丢任务的结果永远不返回，语义复杂 | 实时行情、只关心最新值 |
| **丢弃最新（drop-newest）** | 新任务直接丢弃 | 实现最简单 | 高峰新请求全失败 | 非关键遥测、日志 |

对公众号接入层，最常用的是**立即拒绝 + 上层限流**：

- 同步请求：队列满时直接回 503/429，带 `Retry-After`；
- 长连接推送：任务入队失败则记录失败指标，按策略重试或落死信；
- 接入层还可以在队列水位过高时**暂停读取**，把背压传回 TCP 窗口。

#### 1.4 背压如何传到接入层

「队列满」不应只是线程池内部的一个 false，它要向上传导，形成端到端背压。常见手段：

1. **暂停 `EPOLLIN`**：当工作队列水位超过高水位，IO 线程对连接 `disableReading()`；水位回落后再 `enableReading()`。这样内核接收缓冲区填满，TCP 窗口收缩，对端自然降速。
2. **暂停 `accept`**：监听 fd 暂时不读，新连接留在全连接队列；超过 `somaxconn` 后内核拒绝，避免把压力放进用户态。
3. **协议层限流**：返回 429/503，或要求客户端退避。
4. **分级队列/舱壁**：不同任务类型使用不同池和队列，防止大 JSON 解析把轻量心跳任务挤死。

背压的关键是**尽早失败**，而不是让请求在队列里等到超时。用户等 30 秒后收到超时，比 50ms 内收到 503 更糟。

#### 1.5 Linux 上观察队列与线程

```bash
# 看进程线程数与状态
cat /proc/<pid>/status | grep -E 'Threads|VmRSS'

# 按线程看 CPU，找出哪个 worker 在忙
top -H -p <pid>

# 线程级调度与上下文切换
pidstat -t -p <pid> 1

# 看 socket 接收/发送队列是否堆积
ss -lnt
ss -tnp | head

# 看 futex / clone / eventfd 行为（线程池唤醒路径）
strace -f -e trace=clone,futex,eventfd2 -p <pid>

# 卡住时抓所有线程栈
gdb -p <pid> -batch -ex 'thread apply all bt'
```

这些命令能帮你区分：

- 是 worker 不够（CPU 打满、队列长）；
- 还是任务里有阻塞（worker 都睡在 RPC/锁上）；
- 还是 IO 线程被堵（loop 线程栈停在业务函数里）。

#### 1.6 一个有界队列的 C++ 实现

```cpp
// bounded_queue.h
#pragma once
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <functional>
#include <mutex>

class BoundedQueue {
public:
    using Task = std::function<void()>;

    explicit BoundedQueue(std::size_t capacity) : capacity_(capacity) {}

    // 非阻塞入队：满或已关闭返回 false，供 IO 线程快速失败。
    bool tryPush(Task task) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (closed_ || queue_.size() >= capacity_) {
                return false;
            }
            queue_.push_back(std::move(task));
        }
        not_empty_.notify_one();
        return true;
    }

    // 工作线程取任务；队列关闭且为空时返回 false。
    bool pop(Task& out) {
        std::unique_lock<std::mutex> lk(mu_);
        not_empty_.wait(lk, [this] { return closed_ || !queue_.empty(); });
        if (queue_.empty()) {
            return false;  // closed_ 且已排空
        }
        out = std::move(queue_.front());
        queue_.pop_front();
        return true;
    }

    // 停机：停止接收新任务；已入队任务仍可被排空。
    void close() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            closed_ = true;
        }
        not_empty_.notify_all();
    }

    // 停机：丢弃所有未执行任务，并返回丢弃数量。
    std::size_t discardAll() {
        std::deque<Task> doomed;
        {
            std::lock_guard<std::mutex> lk(mu_);
            closed_ = true;
            doomed.swap(queue_);
        }
        not_empty_.notify_all();
        return doomed.size();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lk(mu_);
        return queue_.size();
    }

    std::size_t capacity() const { return capacity_; }

private:
    const std::size_t capacity_;
    mutable std::mutex mu_;
    std::condition_variable not_empty_;
    std::deque<Task> queue_;
    bool closed_ = false;
};
```

这里的 `tryPush` 不等待，是给 IO 线程用的；如果真要阻塞生产者，应另提供 `pushBlocking`，并严格限制调用线程不是 EventLoop。**接口上把两种语义分开，能防止误用。**

Python 里可以用 `queue.Queue(maxsize=N)` 体会同样的语义：

```python
import queue
import threading
import time

q: "queue.Queue[tuple[int, str]]" = queue.Queue(maxsize=8)
stop = threading.Event()


def worker(wid: int) -> None:
    while not stop.is_set():
        try:
            req_id, payload = q.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            # 模拟 CPU/阻塞任务
            time.sleep(0.05)
            print(f"worker-{wid} done req={req_id} payload={payload!r}")
        finally:
            q.task_done()


def submit(req_id: int, payload: str) -> bool:
    try:
        q.put_nowait((req_id, payload))
        return True
    except queue.Full:
        # 明确拒绝，交给上层回 429/503
        return False


threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(4)]
for t in threads:
    t.start()

for i in range(100):
    if not submit(i, "x" * 1024):
        print(f"reject req={i}")
    time.sleep(0.005)

time.sleep(0.5)
stop.set()
for t in threads:
    t.join()
```

观察重点：`put_nowait` 满时立刻抛 `queue.Full`，而不是无限扩容；拒绝发生在提交侧，调用方可以立即反馈。

---

### 2. one loop per thread

#### 2.1 模型全貌

「one loop per thread」是 Muduo 等 C++ 网络库的典型设计：

- 每个 **IO 线程**拥有且只拥有一个 `EventLoop`；
- 一个 EventLoop 管理一组连接（Channel）；
- **连接从建立到销毁固定归属某个 loop 线程**，不迁移；
- 工作线程池是另一组线程，**不运行 EventLoop**，只从队列取任务执行。

```text
进程
├─ main / acceptor 线程
│   └─ 负责 listen、accept、把新连接分发给某个 IO loop
├─ IO 线程 0
│   ├─ EventLoop 0
│   └─ 连接 1, 4, 7, ...（固定归属）
├─ IO 线程 1
│   ├─ EventLoop 1
│   └─ 连接 2, 5, 8, ...
├─ IO 线程 2
│   ├─ EventLoop 2
│   └─ 连接 3, 6, 9, ...
└─ 工作线程池
    ├─ worker 0
    ├─ worker 1
    └─ worker 2   ← 没有 EventLoop，只跑 Task
```

为什么 IO 线程要「一线程一 loop」而不是「多线程共享一个 loop」？

- 连接状态不需要锁：同一连接的读写、定时器、关闭都在同一个线程串行执行；
- 减少锁竞争和缓存行弹跳；
- 故障隔离：一个 loop 被慢任务拖住，不会影响其他 loop（但会影响它自己的全部连接）；
- 便于绑定 CPU、观察每线程指标。

#### 2.2 连接归属与禁止跨线程

一条铁律：

> 连接对象（socket、读缓冲、写缓冲、状态机、定时器）只允许其所属 IO 线程访问。

工作线程拿到的是**任务所需的数据副本或共享只读对象**，不是连接本身。常见错误：

```cpp
// 错误：工作线程直接操作连接
pool.submit([conn = raw_conn] {
    auto result = do_heavy_work(conn->request());
    conn->send(result);   // 跨线程写 socket / 改状态，数据竞争
    conn->close();        // 更糟：可能和 IO 线程同时 close
});
```

正确做法有两种：

1. **值传递**：把请求需要的字段拷贝/移动到任务里，任务返回纯数据；
2. **共享所有权 + 回投**：任务持有 `shared_ptr<Conn>` 延长生命，但**仍然只在 loop 线程里修改它**，工作线程只读不可变快照。

```cpp
// 正确：工作线程只处理数据，结果回投
pool.submit([loop = conn->loop(), weak_conn = conn->weak_from_this(),
             request = conn->takeRequestSnapshot()]() mutable {
    Result r = do_heavy_work(std::move(request));

    // 回到连接所属 loop 线程再碰连接
    loop->queueInLoop([weak_conn, r = std::move(r)]() mutable {
        if (auto conn = weak_conn.lock()) {
            conn->send(r.encode());
            conn->updateMetrics(r);
        }
        // 连接已关闭则安全丢弃结果
    });
});
```

注意 `loop` 本身的生命周期：如果 loop 可能先于任务销毁，应使用 `weak_ptr<EventLoop>` 或保证「先 join 工作线程，再销毁 loop」。后文停机部分会展开。

#### 2.3 `runInLoop` / `queueInLoop` 的语义

课 57 已经介绍过，这里回顾并强调差异：

```cpp
void EventLoop::runInLoop(Functor cb) {
    if (isInLoopThread()) {
        cb();                 // 已在 loop 线程：立即执行
    } else {
        queueInLoop(std::move(cb));
    }
}

void EventLoop::queueInLoop(Functor cb) {
    {
        std::lock_guard<std::mutex> lk(mutex_);
        pendingFunctors_.push_back(std::move(cb));
    }
    // 跨线程投递需要唤醒；loop 正在执行 pending 时也要唤醒，
    // 否则新任务可能要等下一次 poll 返回才执行。
    if (!isInLoopThread() || callingPendingFunctors_) {
        wakeup();
    }
}

void EventLoop::loop() {
    quit_ = false;
    while (!quit_) {
        activeChannels_.clear();
        poller_->poll(kPollTimeoutMs, &activeChannels_);
        for (Channel* ch : activeChannels_) {
            ch->handleEvent();
        }
        doPendingFunctors();  // 串行执行跨线程任务
    }
}
```

`wakeup()` 通常向 `eventfd` 写 8 字节，让阻塞在 `epoll_wait` 的 loop 立刻返回。没有这一步，工作线程投递的结果可能要等到下一个 socket 事件才被处理，尾延迟会明显变差。

两个常见细节：

- **`callingPendingFunctors_` 为真时也要唤醒**：因为 `doPendingFunctors` 可能已经取走了当前快照，新加入的任务需要再触发一轮；
- **`doPendingFunctors` 应交换出本地列表再执行**：避免执行回调时递归修改同一个 vector，也方便控制单轮执行数量。

```cpp
void EventLoop::doPendingFunctors() {
    std::vector<Functor> functors;
    callingPendingFunctors_ = true;
    {
        std::lock_guard<std::mutex> lk(mutex_);
        functors.swap(pendingFunctors_);
    }
    for (auto& f : functors) {
        f();
    }
    callingPendingFunctors_ = false;
}
```

#### 2.4 工作线程池不跑 EventLoop 的意义

工作线程如果也跑 EventLoop，会出现两个问题：

1. **连接归属混乱**：连接到底归 IO loop 还是 worker loop？跨 loop 操作又要加锁；
2. **资源竞争**：worker 本应执行短任务，却被 poll 阻塞或处理 IO，池的容量语义失真。

因此本课采用清晰分层：

- IO 线程：`poll` + 读写 + 拆包 + 轻量协议解析 + 定时器；
- 工作线程：纯计算/阻塞调用，不 `poll`，不持有 Channel，不直接写 fd；
- 两者之间只有任务队列和回投队列。

如果任务本身是 IO 密集（例如大量下游 HTTP），也可以使用专门的 IO 线程池或异步客户端；但那是另一种模型，不应把「工作线程池」和「IO 线程池」混为一谈。

#### 2.5 线程池大小的直觉

工作线程数量不是越多越好：

- 太少：队列堆积，CPU 闲置；
- 太多：上下文切换、栈内存、锁竞争、缓存失效，吞吐反而下降。

一个常用的工程近似（来自 Brian Goetz 的线程池 sizing 讨论）：

```text
N_threads = N_cpu × U_cpu × (1 + W / C)
```

其中：

- `N_cpu`：可用核数；
- `U_cpu`：目标 CPU 利用率（如 0.8）；
- `W`：单任务平均等待时间（阻塞 IO、锁、RPC）；
- `C`：单任务平均计算时间。

对纯 CPU 任务，`W≈0`，线程数接近核数；对阻塞型任务，`W/C` 可能很大，线程数可以显著高于核数。但这不是机械公式，必须结合压测、内存和下游容量校准。

---

### 3. 任务取消与结果回投

#### 3.1 为什么需要「回投」

工作线程执行完任务后，结果必须回到连接所属 IO 线程才能安全使用。回投路径：

```text
worker 执行 task
  └─ 生成 result（纯数据）
       └─ loop->queueInLoop([weak_conn, result] { ... })
            └─ eventfd 唤醒 loop
                 └─ loop 线程执行回调
                      ├─ weak_conn.lock() 成功：写回/更新状态
                      └─ 失败：连接已关闭，丢弃结果
```

关键点：

- **结果对象必须自包含**：不要携带指向连接内部可变状态的裸指针；
- **回投回调要检查对象是否还活着**：用 `weak_ptr`、连接 id + 世代号，或「loop 未销毁」的结构性保证；
- **回投也要限流**：如果 worker 产生结果的速度远高于 loop 处理速度，回投队列同样会膨胀。可在回投侧做批量合并或水位控制。

#### 3.2 用 `packaged_task` / `future` 表达结果

C++11 起，`std::packaged_task<R()>` 把可调用对象包装成「执行后自动设置 future」的任务：

```cpp
#include <future>
#include <memory>
#include <stdexcept>
#include <utility>

template <class F>
auto submitWithFuture(F&& f) -> std::future<decltype(f())> {
    using R = decltype(f());
    auto task = std::make_shared<std::packaged_task<R()>>(std::forward<F>(f));
    std::future<R> fut = task->get_future();

    bool ok = queue_.tryPush([task] { (*task)(); });
    if (!ok) {
        throw std::runtime_error("worker queue full");
    }
    return fut;
}
```

注意 `packaged_task` 是 move-only，而 `std::function` 要求可拷贝，所以这里用 `shared_ptr` 包一层再捕获。

使用：

```cpp
auto fut = pool.submitWithFuture([] {
    return heavyParse(bigJson);
});

// 千万不要在 EventLoop 线程里 fut.get()！
// 正确：注册完成回调，或把 future 交给需要它的非 IO 线程。
```

`future.get()` 是阻塞的。如果在 EventLoop 线程调用，等于把「异步卸载」又变回「同步等待」，而且可能死锁：worker 完成后需要 `queueInLoop` 回投，但 loop 正卡在 `get()` 上，回投永远得不到执行。

需要超时/取消时，可以用 `wait_for` + 原子取消标志：

```cpp
struct CancelToken {
    std::atomic<bool> canceled{false};
};

auto token = std::make_shared<CancelToken>();

auto fut = pool.submitWithFuture([token, req = std::move(req)]() mutable {
    if (token->canceled.load(std::memory_order_acquire)) {
        return Result::Canceled();
    }
    // 长任务分段检查取消
    for (auto& part : req.parts) {
        if (token->canceled.load(std::memory_order_acquire)) {
            return Result::Canceled();
        }
        process(part);
    }
    return Result::Ok();
});

// 连接关闭时：
token->canceled.store(true, std::memory_order_release);
```

取消是协作式的：线程池无法安全地「杀死」一个正在执行的线程。任务必须在合适的检查点主动退出。

#### 3.3 完整的有界线程池

把前面的队列和 future 组合起来：

```cpp
// bounded_thread_pool.h
#pragma once
#include <atomic>
#include <cstddef>
#include <future>
#include <memory>
#include <stdexcept>
#include <thread>
#include <vector>

#include "bounded_queue.h"

class QueueFullError : public std::runtime_error {
public:
    QueueFullError() : std::runtime_error("worker queue full") {}
};

class BoundedThreadPool {
public:
    BoundedThreadPool(std::size_t workers, std::size_t queueCapacity)
        : queue_(queueCapacity) {
        if (workers == 0) {
            throw std::invalid_argument("workers must be > 0");
        }
        workers_.reserve(workers);
        for (std::size_t i = 0; i < workers; ++i) {
            workers_.emplace_back([this] { workerLoop(); });
        }
    }

    ~BoundedThreadPool() {
        stop(/*drain=*/false);
    }

    BoundedThreadPool(const BoundedThreadPool&) = delete;
    BoundedThreadPool& operator=(const BoundedThreadPool&) = delete;

    template <class F>
    auto submit(F&& f) -> std::future<decltype(f())> {
        using R = decltype(f());
        auto task = std::make_shared<std::packaged_task<R()>>(std::forward<F>(f));
        std::future<R> fut = task->get_future();

        if (!queue_.tryPush([task] { (*task)(); })) {
            // 明确拒绝：调用方决定返回 429/503、重试还是落死信
            throw QueueFullError();
        }
        return fut;
    }

    // drain=true：停止接收新任务，执行完已入队任务再退出；
    // drain=false：丢弃未执行任务，尽快退出。
    void stop(bool drain) {
        bool expected = false;
        if (!stopped_.compare_exchange_strong(expected, true)) {
            return;  // 幂等
        }

        if (drain) {
            queue_.close();       // pop 会把剩余任务执行完后返回 false
        } else {
            queue_.discardAll();  // 未执行任务被丢弃，future 变为 broken_promise
        }

        for (auto& t : workers_) {
            if (t.joinable()) {
                t.join();
            }
        }
    }

    std::size_t queueSize() const { return queue_.size(); }
    std::size_t queueCapacity() const { return queue_.capacity(); }

private:
    void workerLoop() {
        BoundedQueue::Task task;
        while (queue_.pop(task)) {
            try {
                task();
            } catch (...) {
                // packaged_task 会把异常存进 future；这里兜底防止线程退出。
                // 生产环境应记录日志/指标。
            }
        }
    }

    BoundedQueue queue_;
    std::vector<std::thread> workers_;
    std::atomic<bool> stopped_{false};
};
```

几个设计点：

- `tryPush` 失败立刻抛 `QueueFullError`，不在 IO 线程等待；
- `stop(true)` 先 `close`，生产者立刻失败，消费者继续排空；
- `stop(false)` 直接丢弃队列，适合故障快速退出；
- `join` 在 `stop` 内完成，避免析构时还有线程访问成员；
- worker 里 `catch (...)` 兜底，防止某个任务异常导致 `std::terminate`。

#### 3.4 停机顺序：这是 acceptance 的核心

正确顺序：

```text
1. 接入层停止接收新请求（停止 accept / 关闭监听 / 标记 shutting_down）
2. 线程池停止接收新任务（close 队列）
3. 选择排空（drain）或丢弃（discard）队列
4. join 所有工作线程
   └─ 此时所有任务已经执行完，所有 queueInLoop 回投已经入队
5. 让 IO loop 处理完剩余回投任务
6. loop->quit()
7. 销毁连接与 EventLoop
```

为什么不能反过来？

- 先销毁 loop，再 join worker：worker 可能正在 `queueInLoop` 访问已释放的 loop → UAF；
- 先 join worker，但 worker 阻塞等待 loop 回应：死锁；
- 不排空也不 join 就析构线程池：`std::thread` 析构时若仍 joinable 会 `std::terminate`；
- 队列里任务持有连接裸指针，连接先析构：任务执行时 UAF。

一个安全的停机编排：

```cpp
class Gateway {
public:
    void shutdown() {
        // 1. 停止新连接与新请求
        acceptor_.stop();
        for (auto& loop : ioLoops_) {
            loop->runInLoop([loop = loop.get()] {
                loop->stopAccepting();
            });
        }

        // 2. 停止接收新任务；选择排空
        pool_.stop(/*drain=*/true);  // 内部 join 所有 worker

        // 3. worker 已全部结束，不会再产生新的回投
        //    让每个 loop 把 pending functors 跑完再退出
        for (auto& loop : ioLoops_) {
            loop->runInLoop([loop = loop.get()] {
                loop->quit();
            });
            loop->wait();
        }

        // 4. 此时才可以销毁连接与 loop
        connections_.clear();
        ioLoops_.clear();
    }

private:
    Acceptor acceptor_;
    BoundedThreadPool pool_;
    std::vector<std::unique_ptr<EventLoop>> ioLoops_;
    std::vector<std::shared_ptr<Connection>> connections_;
};
```

如果任务里捕获的是 `weak_ptr<EventLoop>`，即使编排有误，也能在 loop 销毁后安全失败：

```cpp
std::weak_ptr<EventLoop> weakLoop = loop;

pool_.submit([weakLoop, weakConn = conn->weak_from_this(), req] {
    auto result = process(req);
    if (auto loop = weakLoop.lock()) {
        loop->queueInLoop([weakConn, result = std::move(result)]() mutable {
            if (auto conn = weakConn.lock()) {
                conn->send(result);
            }
        });
    }
    // loop 已销毁：结果直接丢弃，绝不访问悬空指针
});
```

但要注意：**弱指针只是最后一道保险，不能替代正确的停机顺序。** 如果 loop 已销毁，结果被静默丢弃，业务上可能仍有未完成请求；正确做法是停机前排空，或明确返回「服务关闭」。

#### 3.5 取消与停机的竞态

考虑这条时间线：

```text
t0  IO 线程提交任务 T
t1  连接关闭，IO 线程设置 cancel(token)
t2  worker 开始执行 T（尚未检查 cancel）
t3  进程停机，pool.stop(drain=true)
t4  worker 完成计算，准备 queueInLoop 回投
t5  loop 已 quit 并销毁
t6  回投回调访问 loop → UAF
```

防线有三层：

1. **任务内检查取消标志**，尽早返回 `Canceled`；
2. **回投使用 `weak_ptr`**，对象失效则丢弃；
3. **停机先 join worker，再销毁 loop**，从结构上消除 t5 < t4 的可能。

只靠第 1 层不够：取消是协作式的，worker 可能已经越过检查点。只靠第 2 层也不够：弱指针能防 UAF，但不能保证业务结果被处理。三层叠加才是工程解。

#### 3.6 回投队列也要有界

工作线程把结果投回 loop 时，`pendingFunctors_` 也是一个队列。如果 loop 处理不过来：

- worker 持续 `queueInLoop`，pending 列表膨胀；
- 内存上涨，loop 下一轮要执行大量回调，进一步延迟 IO；
- 形成正反馈。

工程手段：

- 限制单个连接的 pending 结果数；
- 合并结果（例如同一连接的多次统计更新合并成一次）；
- 在 worker 侧检查 loop 的水位，过高则暂停/降级；
- 监控 `pendingFunctors_` 长度与单轮执行耗时。

---

## 常见误区

节点 pitfalls：

> 无限队列只把过载转成内存增长和长尾延迟。

围绕这条主线，列出上线前必排的坑。

### 误区 1：「队列越大越安全，干脆不设上限」

无界队列不会让任务消失，只会让任务**更晚**执行：

- 内存随积压增长；
- 排队延迟无上界；
- 请求早已超过客户端超时，执行它没有意义；
- 积压任务持有请求体、连接引用，内存放大。

**对策**：有界队列 + 明确拒绝/限流；用「最大可接受排队延迟」反推容量；监控队列深度和拒绝率。

### 误区 2：「工作线程可以直接 `conn->send()`，反正 socket 是线程安全的」

`socket` 系统调用本身可以跨线程，但**连接对象的状态机不是线程安全的**：

- 读缓冲、写缓冲、协议解析状态可能正被 IO 线程修改；
- 关闭与发送并发会导致 `EBADF`、半包、状态错乱；
- 定时器、统计字段出现数据竞争。

**对策**：连接只在所属 loop 线程操作；worker 只处理纯数据，结果通过 `queueInLoop` 回投。

### 误区 3：「提交任务后 `future.get()` 等一下就好」

在 EventLoop 线程 `get()`：

- 直接堵死 loop，等于没有卸载；
- 若 worker 完成后需要回投 loop，而 loop 正在等 future → **死锁**；
- 超时控制困难。

**对策**：不在 IO 线程阻塞等待；用完成回调、`then` 风格或消息回投；必须等待时放到非 IO 线程。

### 误区 4：「线程数 = CPU 核数，永远正确」

这只对纯 CPU 任务近似成立。若任务里有阻塞 RPC、磁盘 IO、锁等待，线程数需要更大；若任务内存占用高，线程太多反而 OOM。

**对策**：按任务性质分池；用 `N_cpu × U × (1 + W/C)` 估算后再压测；监控 CPU、上下文切换、队列深度。

### 误区 5：「`stop()` 里直接 `join`，不需要管队列」

- `stop` 时队列里还有任务持有连接/loop 指针，join 后执行仍可能 UAF；
- 不排空就销毁 loop，回投丢失；
- 从 loop 线程调用 `stop()` 并等待 worker，而 worker 等待 loop → 死锁。

**对策**：按「停新任务 → 排空/丢弃 → join worker → 再停 loop」编排；`stop` 不要在 loop 线程同步等待。

### 误区 6：「任务抛异常会自己消失」

如果任务不是通过 `packaged_task` 包装，而是直接放进 `std::function`，异常会逃出 `workerLoop`，触发 `std::terminate`，整个进程退出。

**对策**：worker 内 `try/catch`；`packaged_task` 的异常通过 `future` 传递；记录异常指标。

### 误区 7：「取消就是 `future.cancel()`」

`std::future` 没有通用的取消能力。线程无法被安全强杀。

**对策**：协作式取消（原子标志/`stop_token`），任务在检查点主动退出；取消后仍要处理已产生的结果（丢弃或标记过期）。

---

## 设计权衡

节点 tradeoffs：

> 线程池大小取决于任务性质，不能机械等于CPU数。

### 权衡 A：CPU 密集 vs 阻塞 IO

| 任务类型 | 线程数建议 | 原因 |
|----------|------------|------|
| 纯 CPU（哈希、序列化、图像处理） | 约等于可用核数 | 超过核数只会增加上下文切换 |
| 阻塞 IO（同步 RPC、磁盘、数据库） | 可显著大于核数 | 线程大量时间在等待，需要更多并发掩盖延迟 |
| 混合型 | 分池隔离，分别定容 | 防止阻塞任务挤占 CPU 任务 |
| 内存重型 | 更少，且限制队列 | 每线程栈 + 任务持有内存，需算总账 |

### 权衡 B：固定池 vs 弹性池

- **固定池**：容量可预测、延迟稳定、便于压测；突发时靠队列吸收，满了拒绝。推荐作为接入层默认。
- **弹性池**：低峰省资源，高峰扩容；但扩容有延迟，可能抖动，且线程上限难定。适合离线/批处理。

公众号接入层通常选固定池 + 有界队列 + 水平扩容（加实例），而不是在单机无限加线程。

### 权衡 C：线程池 vs 进程池

| 维度 | 线程池 | 进程池 |
|------|--------|--------|
| 隔离性 | 弱，一个任务崩溃可能拖垮进程 | 强，崩溃隔离 |
| 通信成本 | 低（共享内存） | 高（IPC/序列化） |
| 创建成本 | 较低 | 较高 |
| 适合 | 可信代码、低延迟、共享连接状态 | 不可信插件、强隔离、CPU 密集批处理 |

接入层的工作线程通常处理自家代码，线程池更合适；若要做不可信内容转码/沙箱，进程池或独立服务更安全。

### 权衡 D：线程池 vs 协程

| 维度 | 线程池 | 协程 |
|------|--------|------|
| 并发粒度 | 线程级，栈内存较大 | 用户态，轻量，可百万级 |
| 阻塞调用 | 会占住线程 | 需异步化/改造，阻塞会卡住调度器 |
| 编程模型 | 同步代码 + 队列/回投 | async/await，传染性强 |
| 调试 | 线程栈成熟 | 调用栈与调度器相关，工具链要求高 |

如果下游全是同步阻塞 SDK，线程池是最低改造成本；如果追求极高并发且能全面异步化，协程更优。两者也可以组合：协程跑在少量 IO 线程上，阻塞任务仍丢线程池。

### 权衡 E：拒绝策略与用户体验

- 立即拒绝：延迟可控，但需要业务支持重试；
- 排队等待：成功率高，但尾延迟不可控；
- 丢弃最旧：适合「只要最新」的场景，不适合支付/消息类请求；
- 降级：返回缓存/默认值，可能牺牲一致性。

没有万能策略，必须和业务语义绑定，并在协议层写清楚错误码与重试建议。

---

## 动手实践

节点 practice：

> 实现有界工作线程池，把CPU任务从事件循环转移并返回结果。

下面给一条可验收的最小路径。

### 步骤 1：定义任务与结果类型

```cpp
struct ParseRequest {
    std::uint64_t connId;
    std::string body;      // 已从连接读出的完整请求体
    std::uint64_t generation;  // 连接世代，防止旧结果写回新连接
};

struct ParseResult {
    std::uint64_t connId;
    std::uint64_t generation;
    bool ok;
    std::string response;
};
```

原则：任务和结果都**不持有连接内部指针**，只携带值数据与标识。

### 步骤 2：实现有界队列

使用本课 `BoundedQueue`：

- `tryPush` 非阻塞，满/关闭返回 false；
- `pop` 阻塞等待，关闭且空返回 false；
- `close` 与 `discardAll` 支持两种停机语义。

写单元测试：

```text
[ ] 容量为 1 时，第二次 tryPush 返回 false
[ ] close 后 tryPush 返回 false
[ ] close 后 pop 仍能取完已入队任务，再返回 false
[ ] discardAll 返回丢弃数量，之后 pop 返回 false
```

### 步骤 3：实现线程池

使用本课 `BoundedThreadPool`：

- 构造时指定 worker 数与队列容量；
- `submit` 返回 `future`，队列满抛 `QueueFullError`；
- `stop(true)` 排空，`stop(false)` 丢弃；
- worker 内捕获异常，记录指标。

验收点：

```text
[ ] 提交 1000 个短任务，全部完成
[ ] 队列容量 8、worker 1、任务 sleep 50ms 时，能观察到拒绝
[ ] stop(true) 后，已入队任务都执行完
[ ] stop(false) 后，未执行任务被丢弃，future 得到 broken_promise
```

### 步骤 4：接入 EventLoop

在连接所属 loop 线程里提交任务：

```cpp
void Connection::onRequest(std::string body) {
    // 本函数在 IO 线程执行
    auto weakSelf = weak_from_this();
    auto loop = loop_;  // 生命周期由 Gateway 保证，或改用 weak_ptr

    try {
        pool_->submit([loop, weakSelf, body = std::move(body),
                       connId = id_, gen = generation_]() mutable {
            ParseResult r = parseAndValidate(std::move(body));
            r.connId = connId;
            r.generation = gen;

            // 回投到连接所属 loop
            loop->queueInLoop([weakSelf, r = std::move(r)]() mutable {
                if (auto self = weakSelf.lock()) {
                    if (self->generation_ != r.generation) {
                        return;  // 旧连接世代，丢弃
                    }
                    self->send(r.response);
                }
            });
        });
    } catch (const QueueFullError&) {
        // 背压：明确拒绝或限流
        rejectWith(StatusCode::kTooManyRequests);
        metrics_.inc("worker_queue_rejected");
    }
}
```

### 步骤 5：验收 A —— 队列满时明确拒绝或限流

构造场景：

```text
worker = 1
queue capacity = 4
每个任务 sleep 100ms
IO 线程连续提交 100 个任务
```

期望：

- 前 5 个进入执行/队列；
- 后续提交立即抛 `QueueFullError`；
- 接入层返回 429/503，或对连接 `disableReading()`；
- 日志/指标出现 `worker_queue_rejected`；
- 内存不随提交数线性增长。

错误现象：

- 100 个任务全部入队，内存上涨，最后一批延迟接近 10s；
- 或 IO 线程阻塞在 `push` 上，整个 loop 卡死。

验收对照：

```text
[ ] 队列满时明确拒绝或限流
```

### 步骤 6：验收 B —— 停机后无任务访问销毁的 loop

构造场景：

```text
1. 提交一个 sleep 200ms 的任务，任务完成后 queueInLoop
2. 主线程立即执行停机流程
3. 用 ASAN/TSAN 编译运行
```

正确实现：

- `pool.stop(true)` 等待任务完成；
- 任务回投已入 loop 队列；
- loop 再处理完 pending 后退出；
- 全程无 UAF/数据竞争。

对照错误实现：

- 先 `loop.reset()` 再 `pool.stop()`；
- 任务里捕获 `EventLoop*` 裸指针；
- ASAN 应报 heap-use-after-free。

还可以加弱指针测试：

```cpp
auto loop = std::make_shared<EventLoop>();
std::weak_ptr<EventLoop> weak = loop;
loop.reset();  // 故意先销毁

pool.submit([weak] {
    if (auto l = weak.lock()) {
        l->queueInLoop([] { /* ... */ });
    }
    // 锁定失败：安全丢弃
});
```

验收对照：

```text
[ ] 停机后无任务访问销毁的loop
```

### 步骤 7：sourceStudy（对照 Muduo）

节点 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

打开本课唯一资源：

- [IO线程与工作线程池：参考文档 / 原始资料](https://github.com/chenshuo/muduo/tree/master/muduo/base)

重点阅读 `muduo/base` 下的：

- `ThreadPool.h` / `ThreadPool.cc`：任务队列、`setMaxQueueSize`、`start`、`run`、`stop`；注意默认 `maxQueueSize_ = 0` 表示**不限制**队列长度，`run()` 在满时会**阻塞等待**（`notFull_`），而不是本课接入层推荐的非阻塞拒绝；
- `BlockingQueue.h`：**无界**阻塞队列（只有 `put` / `take`，无容量上限）——用来对照「无界 = 过载变内存/长尾」；
- `BoundedBlockingQueue.h`：**有界**阻塞队列（`boost::circular_buffer`，`put` 在 `full()` 时等待，`capacity` / `full` 可查）——对照有界语义；注意它也是阻塞式 `put`，IO 线程不宜直接调用；
- `Thread.h` / `Thread.cc`：线程启动、`join`、线程 id 与名字；
- `Mutex.h` / `Condition.h`：条件变量等待与唤醒。

跨线程回投（`runInLoop` / `queueInLoop` / `wakeup` / `eventfd`）在 **`muduo/net/EventLoop`**，不在 `base` 目录；本课资源指向 `base` 时，先把队列与 ThreadPool 语义核对清楚，再自行联读 `net` 里的回投路径。

追踪清单（写进笔记）：

| 问题 | 你在源码/实验里找到的答案 |
|------|---------------------------|
| `ThreadPool` 的 `maxQueueSize_` 默认值是多少？0 表示有界还是无界？ | |
| 队列满时 `ThreadPool::run()` 是等待、拒绝还是丢弃？`notFull_` 如何配合？ | |
| `BlockingQueue` 与 `BoundedBlockingQueue` 的容量语义差异？后者 `put` 满时行为？ | |
| `ThreadPool::stop()` 是否排空队列？是否 `join` 所有线程？`running_` 如何打断等待？ | |
| 未 `start` / `threads_` 为空时 `run()` 如何处理任务？ | |
| 若联读 `muduo/net`：`queueInLoop` 何时 `wakeup()`？`doPendingFunctors` 是否交换列表？ | |
| 你阅读的 commit/tag 是什么？与实验环境的 C++ / boost 依赖是否一致？ | |

记录边界条件：

- `maxQueueSize_ == 0`（无界）与 `== 1`（极小有界）时 `run()` 的行为；
- `stop` 后再次 `run()`：注释写明会立刻返回；
- 任务抛异常：`runInThread` 是否 `try/catch`（对照 `Exception`）；
- 对比：Muduo `run()` 阻塞等待 vs 本课接入层 `tryRun` 立即拒绝——各自适用场景。

### 步骤 8：加上可观测性

至少记录：

```text
worker_queue_depth          当前队列长度
worker_queue_capacity       队列容量
worker_queue_rejected_total 拒绝总数
worker_busy                 忙碌 worker 数
task_wait_seconds           任务排队时间直方图
task_run_seconds            任务执行时间直方图
loop_pending_functors       loop 回投队列长度
```

没有这些指标，你无法判断「线程池该扩还是该限流」。

### 预期现象

- 单个 100ms 任务提交后，IO 线程在微秒级返回，其他连接继续被服务；
- 队列满时提交侧立即失败，接入层返回 429/503；
- `stop(true)` 后所有已入队任务完成，future 正常就绪；
- `stop(false)` 后未执行任务的 future 得到 `broken_promise`；
- 停机压力测试下 ASAN/TSAN 无报错；
- 线程数、队列深度与 CPU 使用率符合预期。

### 验收清单

```text
[ ] 能解释任务队列、有界容量与无界队列的风险
[ ] 实现了有界队列，满时 tryPush 明确失败
[ ] 实现了固定大小工作线程池，任务复用线程执行
[ ] 任务结果通过 queueInLoop 回投到连接所属 loop
[ ] 工作线程不直接访问连接对象 / 不直接写 socket
[ ] 队列满时接入层明确拒绝或限流
[ ] stop 顺序正确：停新任务 → 排空/丢弃 → join → 停 loop
[ ] 停机后无任务访问销毁的 loop（ASAN/TSAN 干净）
[ ] 对照 muduo/base 完成 sourceStudy 表格
[ ] 记录了队列深度、拒绝数、任务延迟等指标
```

---

## 自测题

**题 1**  
IO 线程向工作线程池提交任务，队列使用无界 `std::deque`。持续过载时，最可能发生什么？

A. 工作线程会自动扩容，系统始终保持稳定  
B. 队列无限增长，内存上涨；任务排队时间无上界，P99 延迟恶化  
C. IO 线程会被内核自动限速，队列长度保持恒定  
D. 无界队列会让任务执行更快，因为减少了锁竞争

**题 2**  
工作线程完成了一个连接的请求处理，下列哪种回投方式最符合 one loop per thread？

A. 工作线程直接调用 `conn->send(result)`，因为 socket 是线程安全的  
B. 工作线程直接修改连接的写缓冲，然后由 IO 线程自动发送  
C. 工作线程把结果封装成纯数据，通过连接所属 loop 的 `queueInLoop` 投递，在 loop 线程检查连接有效性后写回  
D. 工作线程调用 `future.get()` 等待 IO 线程处理

**题 3**  
进程停机时，下列哪种顺序最安全？

A. 先销毁 EventLoop，再 `join` 工作线程  
B. 先 `join` 工作线程，同时让 IO 线程继续接收新任务  
C. 停止接收新任务 → 停止线程池接收新任务并排空/丢弃 → `join` 工作线程 → 处理完 loop 回投后退出并销毁 loop  
D. 直接 `std::terminate`，让操作系统回收所有资源

---

### 自测题参考答案

**题 1：B**  
对应 pitfalls「无限队列只把过载转成内存增长和长尾延迟」。无界队列不会自动扩容 worker，也不会触发内核限速；它只是把「服务不过来」转化为「排队越来越久 + 内存越来越大」。

**题 2：C**  
对应 one loop per thread 与结果回投。A/B 都让工作线程直接碰连接状态，违反线程亲和性；D 把异步又变回同步，且容易死锁。C 使用纯数据 + `queueInLoop` + 有效性检查，是正确路径。

**题 3：C**  
对应 acceptance「停机后无任务访问销毁的 loop」。A 会产生 UAF；B 会不断引入新任务，永远停不干净；D 不可接受。C 的顺序保证 worker 结束后不再产生回投，loop 处理完 pending 再销毁。

---

## 推荐资源

- [IO线程与工作线程池：参考文档 / 原始资料](https://github.com/chenshuo/muduo/tree/master/muduo/base)（github.com，英文，源码，进阶，免费）
  - 重点阅读任务队列和有界容量与 one loop per thread，用于核对本节点实验的语义和边界。
  - 建议从 `ThreadPool.h/.cc`、`BlockingQueue.h`、`BoundedBlockingQueue.h`、`Thread.h/.cc` 入手；`queueInLoop` / `wakeup` 在 `muduo/net/EventLoop`，可另行联读。
  - 阅读时记录：你查看的 commit/tag、`maxQueueSize_` 默认与满时阻塞语义、`stop` 是否 join、异常处理路径，以及与本课「IO 线程 tryRun 非阻塞拒绝」的差异。

---

## 本课小结

1. **EventLoop 是稀缺的串行资源**：CPU 密集或阻塞调用必须移出 IO 线程，否则一个慢回调会拖慢整批连接。
2. **线程池的核心是复用与隔离**：固定 worker 反复取任务，避免频繁创建线程；工作线程不跑 EventLoop，不直接操作连接。
3. **队列必须有界**：无界队列只是把过载转成内存增长和长尾延迟（pitfalls）；容量按「可接受排队延迟」和「单任务内存」估算。
4. **队列满要有明确策略**：立即拒绝、阻塞等待、丢弃最旧/最新；IO 线程绝不能用阻塞等待。背压可通过暂停 `EPOLLIN`、暂停 `accept`、返回 429/503 传导。
5. **one loop per thread**：连接固定归属某个 IO 线程，状态不加锁串行修改；跨线程只传数据，结果用 `queueInLoop` + `eventfd` 回投。
6. **取消是协作式的**：原子标志/检查点，不能强杀线程；回投时用 `weak_ptr` 或世代号防止旧结果写回已关闭连接。
7. **停机顺序决定正确性**：停止接收新任务 → 排空或丢弃队列 → `join` 工作线程 → 处理完回投 → 停 loop → 销毁对象；验收要求「停机后无任务访问销毁的 loop」。
8. **线程数取决于任务性质**：CPU 密集≈核数，阻塞 IO 可更大；用 `N_cpu × U × (1 + W/C)` 估算后压测，不能机械等于 CPU 数。
9. **对照 muduo/base 做 sourceStudy**：核对 `ThreadPool`、`BlockingQueue` / `BoundedBlockingQueue` 的真实语义与边界；回投路径联读 `muduo/net` 的 `queueInLoop`。

下一课会继续在传输与 Socket 主题上深入。请先把有界线程池接入你的 Reactor，完成「队列满明确拒绝」与「停机无 UAF」两条验收，再进入后续内容。

---

## 配套练习建议

若课程仓库后续补充 `practice59/`，建议最小文件集：

| 文件 | 作用 |
|------|------|
| `bounded_queue.h` | 有界队列，`tryPush` / `pop` / `close` / `discardAll` |
| `bounded_thread_pool.h/.cc` | 固定 worker、future 返回、`stop(drain)` |
| `event_loop.*`（可基于 practice57/58） | `queueInLoop` + `eventfd` 唤醒；连接归属 |
| `connection.*` | 请求快照、结果回投、世代号/弱指针检查 |
| `test_queue_bounds.*` | 容量、满拒绝、close 后行为 |
| `test_pool_backpressure.*` | 慢任务 + 小队列，验证拒绝与延迟上界 |
| `test_shutdown.*` | 停机顺序 + ASAN/TSAN，验证无 UAF/竞争 |
| `demo_offload.*` | 模拟签名校验/大 JSON 解析从 loop 转移到 worker |
| `run.sh` | 一键编译、跑测试、输出验收结果 |

运行期望：

```bash
./run.sh
# 末行：ALL_CHECKS_PASSED
```

建议的压测参数：

```text
worker 数：2 / 4 / 8 三组
队列容量：4 / 32 / 256 三组
任务：1ms CPU + 可配置 sleep 模拟阻塞
负载：逐步提高提交速率，直到出现拒绝
观察：队列深度、拒绝率、P99 排队延迟、CPU、上下文切换
```

实测要点：

- 队列容量很小时，拒绝率上升但 P99 排队延迟可控；
- 队列容量过大时，拒绝率下降但 P99 与内存显著上升；
- worker 数超过核数后，CPU 密集任务吞吐可能下降；
- 阻塞任务占比高时，增加 worker 能提升吞吐，但要受下游容量约束；
- 停机压力下，正确实现 ASAN/TSAN 干净，错误实现能稳定复现 UAF 或死锁。

进阶练习：

1. 给线程池加**优先级队列**，让心跳/控制类任务优先于大包解析；
2. 按任务类型拆成多个池（CPU 池、下游 RPC 池），观察舱壁隔离效果；
3. 实现「队列水位高 → 暂停 `EPOLLIN`」的端到端背压，并用 `ss` 观察接收队列；
4. 用 `std::stop_token`（C++20）重写协作式取消；
5. 对比线程池与协程方案在同一压测下的内存、延迟与吞吐。
