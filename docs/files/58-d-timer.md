# 第 58/208 课：定时器、时间轮与单调时钟

- **阶段**：第 3 阶段 · 传输与Socket
- **前置课程**：d-reactor、a-tree
- **关键词**：Timers、定时器、timerfd、单调时钟、steady_clock、CLOCK_MONOTONIC、CLOCK_REALTIME、最小堆、时间轮、hierarchical timing wheel、取消、重复定时、竞态、相对超时、心跳、空闲超时、重试退避
- **难度**：进阶
- **所属领域**：D · IO模型与事件（子类：调度与资源复用）
- **标签**：Timers、D、C++

---

## 导读

上一课我们搭好了 Reactor 骨架：Poller 等就绪、Channel 绑回调、EventLoop 串行维护连接状态，跨线程用 `queueInLoop` + `eventfd` 唤醒。到这里，「有事件就处理」已经通了——但接入层还有一类**没有 socket 字节、却必须准时发生**的事：

- 长连接多久没读到包，判定空闲超时并踢掉；
- 心跳包按固定间隔发出；
- 推送失败后按指数退避重试；
- 半关闭后的 linger、延迟销毁、限流窗口滑动……

这些都依赖**定时器**：根据截止时间触发任务。

节点 summary 对本课的定位是：

> 定时器根据截止时间触发任务，单调时钟避免墙钟跳变影响超时判断；堆和时间轮适合不同规模。

这句话拆开有三层意思：

1. **截止时间驱动**：任务不是「现在立刻跑」，而是「到点才跑」；
2. **单调时钟**：超时判断必须用不受 NTP / 人工改表影响的时钟，否则相对超时会被墙钟跳变毁掉；
3. **堆 vs 时间轮**：少量高精度定时器用最小堆够用；海量粗粒度定时器更适合时间轮批处理。

放到腾讯微信公众号后台的长连接接入层，定时器几乎无处不在：

- **空闲超时**：粉丝侧或第三方回调通道长时间无数据，释放 fd 与会话；
- **心跳**：接入层按间隔发 ping / 期望 pong，超时则关闭；
- **推送重试**：模板消息、客服消息下游失败后的退避队列；
- **限流与滑动窗口**：按秒/分钟重置计数器。

本课要建立的不是「`sleep` 一下再回调」这种玩具语义，而是一套可接入 Reactor 的工程能力：

1. **steady / monotonic 时钟**：为何 `CLOCK_REALTIME` 不能当相对超时基准，`timerfd_create(CLOCK_MONOTONIC)` 如何用；
2. **最小堆与时间轮**：按过期时间的堆、分层时间轮各自适合什么规模；
3. **取消、重复定时和竞态**：取消与触发同时发生时，回调绝不能碰已失效对象；如何用 timerfd Channel 接到 EventLoop。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 steady / monotonic 时钟**
   - 说清 `CLOCK_MONOTONIC` / `std::chrono::steady_clock` 与 `CLOCK_REALTIME` / 墙钟的区别。
   - 说清 NTP 步进、管理员 `date -s`、夏令时调整为何会毁掉「相对超时」。
   - 说清 Linux 上 `timerfd_create(CLOCK_MONOTONIC, ...)` + `timerfd_settime` 的基本用法。

2. **能独立完成：给 Reactor 加入 timerfd 和可取消定时器，测试取消与触发同时发生**
   - 在 EventLoop 中持有一个 timerfd Channel；到期读 timerfd，再跑所有已到期回调。
   - 定时器支持取消；取消与触发竞态时，已取消的回调不执行或不触碰失效对象。
   - 支持一次性与重复（interval）定时器。

3. **满足本课验收标准（acceptance）**
   - 修改系统墙钟不影响相对超时。
   - 取消后的回调不访问失效对象。

---

## 为什么后端要懂

节点里的理由是：

> 实现连接超时、重试和心跳。

### 1. 没有可靠超时，长连接会「假活」

公众号后台接入层常年挂着大量长连接。若没有空闲超时：

- 对端进程崩了、NAT 表项丢了、中间设备静默丢包，你的 epoll 可能很久收不到 `EPOLLRDHUP`；
- fd 与会话对象泄漏，内存与连接表被占满；
- 监控显示「连接数很高」，实际有效会话很少。

**空闲超时定时器**是连接生命周期的安全网：每次读到数据就 `reset`，到期则主动关闭。

### 2. 心跳与重试都是「时间驱动的状态机」

心跳不是「另起一个线程 while sleep」，而是：

- 注册一个 interval 定时器；
- 到点检查「距上次收包是否超过阈值」；
- 必要时发 ping，再设一个「等 pong」的一次性定时器。

推送重试同理：失败 → 按退避表算下次截止时间 → 入定时器队列；成功或超过最大次数 → 取消。这些状态机若用错时钟或取消语义不清，会出现「重试风暴」或「永远不再重试」。

### 3. 墙钟跳变是线上真实事故源

运维改 NTP、虚拟机恢复快照、容器宿主机校时，都会让 `CLOCK_REALTIME` 向前/向后跳。若你用墙钟算「30 秒后超时」：

- 时钟向前跳 1 小时 → 大批定时器瞬间全部到期 → 惊群关闭 / 重试风暴；
- 时钟向后跳 1 小时 → 超时「永远不到」→ 连接永不清理。

节点 acceptance 第一条就是：**修改系统墙钟不影响相对超时**。这是硬门槛，不是风格偏好。

### 4. 定时器与 Reactor 必须同线程协作

上一课强调连接状态只在 loop 线程改。定时器回调通常也要改连接状态（关连接、发心跳、改重试计数）。因此生产级做法是：

- 定时器队列归属某个 EventLoop；
- 用 **一个 timerfd**（或类似机制）把「最近到期点」变成 epoll 可读事件；
- 在 loop 线程里串行执行到期回调——与 Channel 回调同一套线程亲和纪律。

---

## 直觉 → 原理

### 0. 先建立直觉：从 sleep 到「可取消的截止时间队列」

最Naive的写法：

```python
# naive_timer.py —— 示意，千万别用于接入层
import time, threading

def after(seconds, cb):
    def runner():
        time.sleep(seconds)
        cb()
    threading.Thread(target=runner, daemon=True).start()
```

它能跑 Demo，但缺三块工程能力：

1. **时钟错误**：`time.time()` / 墙钟 sleep 受系统时间影响；线程里 sleep 也不受统一调度；
2. **无法高效取消与合并**：一万个连接各起一线程 sleep，资源炸裂；取消要打断 sleep，语义混乱；
3. **与 Reactor 脱节**：回调可能在任意线程跑，直接改连接状态就破坏上一课的亲和性。

正确方向：

```text
所有定时器 → 按过期时间排进一个队列（堆或时间轮）
EventLoop 只关心「最近一个到期点」
用 timerfd 把该点变成 epoll 事件
到期后在 loop 线程批量执行回调
```

下面按节点三个 points 展开。

### 1. steady / monotonic 时钟

#### 1.1 两类时钟，两套用途

| 时钟 | Linux 常量 | C++ | 典型用途 | 会跳变吗 |
|------|------------|-----|----------|----------|
| **墙钟 / 实时时钟** | `CLOCK_REALTIME` | `system_clock` | 日志时间戳、订单「下单于几点」、对外展示 | 会（NTP、手动改表） |
| **单调时钟** | `CLOCK_MONOTONIC` | `steady_clock` | 超时、RTT、间隔、性能计时 | 进程运行期间只向前（不受墙钟调整影响） |

直觉：

- 问「现在几点了？」→ 墙钟；
- 问「从刚才到现在过了多久？再过 30 秒该做什么？」→ **单调时钟**。

#### 1.2 用命令亲眼看墙钟可被改、单调不可被墙钟拖走

```bash
# 读两种时钟（需要 util-linux 较新版本，或用自己的小程序）
# 下面用 Python 演示语义

python3 - <<'PY'
import time
print("wall  ", time.time())
print("mono  ", time.monotonic())
PY

# 若你有权限（容器/实验机），可对比改表前后：
# sudo date -s "2020-01-01 00:00:00"   # 墙钟大跳
# 再跑上面脚本：wall 变了，monotonic 几乎连续
```

验收实验（对应 acceptance）：

1. 启动服务，注册一个 10 秒相对超时；
2. 在第 3 秒把系统墙钟向前拨 1 小时；
3. **正确实现**：定时器仍约在第 10 秒（单调）触发，不会瞬间到期；
4. **错误实现**（用 `CLOCK_REALTIME` 绝对时间）：可能立刻全部触发。

#### 1.3 Linux：`timerfd_create` 绑定单调时钟

节点资源指向 `timerfd_create(2)`。核心 API：

```c
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>
#include <stdint.h>
#include <stdio.h>

int main(void) {
    /* 关键绑定 CLOCK_MONOTONIC 的 timerfd */
    int tfd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    if (tfd < 0) { perror("timerfd_create"); return 1; }

    struct itimerspec its = {0};
    /* 3 秒后首次到期；interval=0 表示一次性 */
    its.it_value.tv_sec = 3;
    its.it_value.tv_nsec = 0;
    its.it_interval.tv_sec = 0;
    its.it_interval.tv_nsec = 0;

    /* flags=0：it_value 为相对时间（相对单调时钟） */
    if (timerfd_settime(tfd, 0, &its, NULL) < 0) {
        perror("timerfd_settime");
        return 1;
    }

    uint64_t exp = 0;
    /* 阻塞读：到期后内核写入「溢出次数」 */
    ssize_t n = read(tfd, &exp, sizeof(exp));
    if (n == sizeof(exp))
        printf("expired, overrun=%llu\n", (unsigned long long)exp);

    close(tfd);
    return 0;
}
```

要点：

- **`timerfd_create(CLOCK_MONOTONIC, ...)`**：到期判断走单调时钟；
- **`timerfd_settime` 的 `flags`**：
  - `0`：`it_value` 是**相对**当前单调时钟的延时；
  - `TFD_TIMER_ABSTIME`：`it_value` 是绝对 monotonic 时间点；
- **`read(timerfd)`**：返回 8 字节的 `uint64_t`，表示自上次 read 以来到期了多少次（重复定时器可能 >1，即 overrun）；
- 把 timerfd 当成普通 fd：`epoll_ctl` 加 `EPOLLIN`，就能融入上一课的 Reactor。

#### 1.4 C++：`steady_clock` 存截止时间

```cpp
#include <chrono>
#include <iostream>

using Steady = std::chrono::steady_clock;

Steady::time_point deadline_after(std::chrono::milliseconds d) {
    return Steady::now() + d;   // 相对超时 → 单调截止点
}

bool expired(Steady::time_point dl) {
    return Steady::now() >= dl;
}

// 反例：不要用 system_clock 做相对超时
// auto bad = std::chrono::system_clock::now() + 30s;  // 墙钟一跳就错
```

工程纪律：

- **定时器内部一律存 `steady_clock::time_point`（或单调 timespec）**；
- 日志若要打印「人类可读时间」，另取 `system_clock` 仅用于展示，**不要把展示时钟写回超时逻辑**；
- 对外协议里的绝对时间戳（如 JWT exp）是业务语义，与「连接空闲 30s」是两回事——前者用墙钟/约定纪元，后者用单调。

#### 1.5 公众号场景：空闲超时必须相对 + 单调

```text
连接建立 → 启动 idle_timer = now_mono + 60s
每次收到完整帧 → reset idle_timer = now_mono + 60s
到期回调 → 主动关闭连接、清理会话

运维此时 NTP 校时把墙钟拨快 5 分钟：
  正确：idle 仍按单调剩余时间走，不会误杀刚活跃的连接
  错误：若用墙钟绝对 deadline，可能整片连接被判超时
```

### 2. 最小堆与时间轮

#### 2.1 最小堆：按过期时间排序

前置课 `a-tree` 里的堆（优先队列）在这里直接上场：堆顶永远是**最早到期**的定时器。

```cpp
#include <queue>
#include <vector>
#include <functional>
#include <chrono>
#include <cstdint>

using SteadyClock = std::chrono::steady_clock;

struct Timer {
    uint64_t id;
    SteadyClock::time_point expiration;
    std::chrono::milliseconds interval; // 0 = 一次性
    std::function<void()> cb;
    bool canceled = false;
};

struct TimerCmp {
    bool operator()(const Timer* a, const Timer* b) const {
        return a->expiration > b->expiration; // 小顶堆：更早的优先
    }
};

class TimerQueue {
public:
    uint64_t add(SteadyClock::duration after,
                 std::function<void()> cb,
                 SteadyClock::duration interval = SteadyClock::duration::zero()) {
        auto t = new Timer{
            next_id_++,
            SteadyClock::now() + after,
            std::chrono::duration_cast<std::chrono::milliseconds>(interval),
            std::move(cb),
            false
        };
        heap_.push(t);
        id_map_[t->id] = t;
        return t->id;
    }

    void cancel(uint64_t id) {
        auto it = id_map_.find(id);
        if (it == id_map_.end()) return;
        it->second->canceled = true; // 惰性删除：堆内先打标，取出时跳过
    }

    // 返回「距最近到期」的延时；若已有到期则为 0
    SteadyClock::duration time_to_next() const {
        if (heap_.empty()) return SteadyClock::duration{-1}; // 调用方解释为无限等
        auto d = heap_.top()->expiration - SteadyClock::now();
        return d < SteadyClock::duration::zero() ? SteadyClock::duration::zero() : d;
    }

    void run_expired() {
        auto now = SteadyClock::now();
        while (!heap_.empty() && heap_.top()->expiration <= now) {
            Timer* t = heap_.top();
            heap_.pop();
            id_map_.erase(t->id);
            if (!t->canceled) {
                t->cb();
                if (t->interval.count() > 0 && !t->canceled) {
                    t->expiration = now + t->interval;
                    // 注意：重复定时器若在回调里 cancel，下面不应再入堆
                    // 更稳妥：回调后再查 canceled / 用 shared 状态
                    heap_.push(t);
                    id_map_[t->id] = t;
                    continue;
                }
            }
            delete t;
        }
    }

private:
    uint64_t next_id_ = 1;
    std::priority_queue<Timer*, std::vector<Timer*>, TimerCmp> heap_;
    std::unordered_map<uint64_t, Timer*> id_map_;
};
```

复杂度直觉：

| 操作 | 最小堆 |
|------|--------|
| 插入 | O(log n) |
| 查看最近到期 | O(1) |
| 取出/触发 | O(log n) |
| 取消 | O(1) 打标 + 惰性清；或 O(n) 真删 |

适合：**每个 EventLoop 上成百上千级定时器**，精度要求到毫秒，实现简单、取消语义清晰。muduo 的 `TimerQueue` 就是「timerfd + 按到期排序的容器」一路。

#### 2.2 分层时间轮：海量粗粒度定时器

当定时器数量到**十万、百万**（例如每个连接一个空闲定时器，接入机单进程几十万连接），堆的 `log n` 与缓存不友好会成为热点。时间轮（timing wheel）把时间离散成一格一格的槽：

```text
简单单层时间轮（示意）：

tick = 10ms，wheel_size = 1024
→ 一轮覆盖约 10.24s

当前 tick 指针每 10ms 前进一步；
落在当前槽里的链表上的定时器全部到期（批处理）。

超长延时：多层级联（hierarchical）
  秒轮 → 分轮 → 时轮，类似水表进位。
```

简化 Python 示意（单层，仅讲结构）：

```python
# simple_timing_wheel.py —— 教学用，非生产
from collections import defaultdict, deque
import time

class TimingWheel:
    def __init__(self, tick_ms=10, size=1024):
        self.tick_ms = tick_ms
        self.size = size
        self.slots = [deque() for _ in range(size)]
        self.current = 0
        self.now_mono_ms = 0  # 用单调毫秒推进，不用墙钟

    def add(self, delay_ms, timer_id, cb):
        ticks = max(1, delay_ms // self.tick_ms)
        if ticks >= self.size:
            raise ValueError("超出单层轮覆盖，需层级联或更大 size")
        idx = (self.current + ticks) % self.size
        self.slots[idx].append((timer_id, cb, True))  # True=active
        return idx

    def cancel(self, timer_id):
        # 生产实现常用 id→节点指针 O(1) 摘链；这里示意扫槽打标
        for slot in self.slots:
            for i, (tid, cb, active) in enumerate(slot):
                if tid == timer_id:
                    slot[i] = (tid, cb, False)

    def advance(self, mono_ms_now):
        """由外部按单调时钟推进；可一次跨多格（追赶）"""
        while self.now_mono_ms + self.tick_ms <= mono_ms_now:
            self.now_mono_ms += self.tick_ms
            self.current = (self.current + 1) % self.size
            due = self.slots[self.current]
            self.slots[self.current] = deque()
            for tid, cb, active in due:
                if active:
                    cb()
```

#### 2.3 何时用堆、何时用轮

| 维度 | 最小堆 + timerfd | 分层时间轮 |
|------|------------------|------------|
| 定时器规模 | 小～中（~10^3–10^4 / loop） | 大（~10^5–10^6） |
| 时间精度 | 可为任意 deadline（受系统调度限制） | 受 tick 粒度限制（如 10ms） |
| 取消 | 打标或直接擦除较直观 | 需要把手柄/链表节点，跨轮迁移更绕 |
| 实现复杂度 | 低 | 中高（级联、降级、重新哈希） |
| 批处理 | 每次弹堆顶 | 整槽链表一次扫完，缓存友好 |
| 典型场景 | 通用 Reactor、muduo 风格 | Netty `HashedWheelTimer`、海量连接空闲检测 |

公众号接入层经验法则：

- **先堆**：单机连接未到极端规模时，堆 + timerfd 足够，正确性更好写；
- **再轮**：空闲超时这种「精度 1s 就够、数量随连接爆炸」的场景，可把 idle 检测拆到时间轮；心跳/少量关键任务仍可留在堆。

节点 tradeoffs 原话的工程翻译：**时间轮批处理高效，但精度、跨轮管理与取消更复杂**——不要为了「看起来高级」一上来就上轮。

#### 2.4 与 timerfd 的衔接：只把「最近到期」塞进内核

无论堆还是轮，Reactor 侧通常只维护**一个** timerfd：

```text
add/cancel 定时器
  → 若「新的最近到期点」变了
  → timerfd_settime(最近到期的相对/绝对 monotonic 时间)

EventLoop::loop:
  poll 返回
  若 timerfd 可读:
      read(timerfd) 清空
      timer_queue.run_expired()   # 堆弹出所有到期；或时间轮 advance
```

伪代码（接上一课 Channel）：

```cpp
void TimerQueue::addOrUpdateTimerfd() {
    auto howlong = timeToNextExpired(); // steady duration
    struct itimerspec its{};
    if (howlong < 0ms) {
        // 无定时器：解除 timerfd
        its.it_value = {0, 0};
    } else {
        auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(howlong);
        its.it_value.tv_sec  = ns.count() / 1000000000;
        its.it_value.tv_nsec = ns.count() % 1000000000;
        if (its.it_value.tv_sec == 0 && its.it_value.tv_nsec == 0)
            its.it_value.tv_nsec = 1; // 已到期：尽快触发
    }
    ::timerfd_settime(timerfd_, 0, &its, nullptr); // 相对 MONOTONIC
}

void TimerQueue::handleRead() {
    uint64_t howmany = 0;
    ::read(timerfd_, &howmany, sizeof howmany); // 必须读，否则水平触发狂喷
    runExpiredTimers(); // 执行到期回调；重复定时器重新插入并可能再次 settime
}
```

### 3. 取消、重复定时和竞态

#### 3.1 取消与触发的竞态长什么样

经典危险时间线：

```text
T0  定时器 A 即将到期，已在「本轮到期列表」快照中
T1  业务线程/同 loop 上一段逻辑 cancel(A)，并销毁 A 关联的 Connection
T2  到期分发执行到 A 的回调 → 回调里 conn->close() → UAF
```

节点 acceptance 第二条：**取消后的回调不访问失效对象**。

对策组合（可叠加）：

1. **惰性取消 + 执行前再检查 `canceled`**（同线程串行时很有效）；
2. **回调只捕获 `weak_ptr` / 世代号**，锁升级失败则直接 return；
3. **取消时若已在「正在执行」列表，置标，执行入口统一判断**；
4. **绝不在定时器对象析构路径里假设回调不会再入**。

```cpp
// 推荐：回调持有弱引用
void Connection::startIdleTimer() {
    auto weak = weak_from_this();
    idle_timer_id_ = loop_->runAfter(60s, [weak] {
        if (auto self = weak.lock()) {
            self->forceClose();  // 对象还在才碰
        }
        // weak 升失败：说明连接已销毁，安全空过
    });
}

void Connection::onMessage() {
    loop_->cancel(idle_timer_id_);
    startIdleTimer();  // reset
}
```

#### 3.2 重复定时器（interval）

语义要先定清楚：

| 模式 | 含义 | 适用 |
|------|------|------|
| **固定间隔（fixed rate）** | 理想触发点 = t0 + n*interval，赶不上就追赶或合并 overrun | 心跳采样、指标 scrape |
| **固定延迟（fixed delay）** | 下次 = 本次回调结束时刻 + interval | 避免重入堆叠的业务轮询 |

`timerfd` 的 `it_interval` 是内核侧 fixed-rate；用户态堆实现里常见写法是「回调后再 `expiration = now + interval`」（偏 fixed delay）或「`expiration += interval` 并 while 追赶」（偏 fixed rate）。

公众号心跳示例：

```cpp
// 每 30s 检查一次；若 90s 无读事件则踢连接
heartbeat_id_ = loop_->runEvery(30s, [weak] {
    auto self = weak.lock();
    if (!self) return;
    if (Steady::now() - self->last_read_ > 90s)
        self->forceClose();
    else if (Steady::now() - self->last_read_ > 30s)
        self->sendPing();
});
```

取消重复定时器：`cancel(id)` 必须保证**后续轮次不再跑**；若回调正在执行中，本轮跑完后不再入队。

#### 3.3 与 Reactor 集成的完整数据路径

```text
                  EventLoop::loop()
                        |
              Poller::poll(timeout可交给timerfd)
                        |
        +---------------+----------------+
        |                                |
  socket Channels                  timerfd Channel
  handleRead/Write                 TimerQueue::handleRead
                                         |
                                   read(timerfd)
                                         |
                                   弹出所有 expiration<=now
                                         |
                                   逐个 cb()（仍在 loop 线程）
                                         |
                          可能 cancel / 重置 / 关连接 / queueInLoop
```

纪律回顾（衔接课 57）：

- 定时器回调默认在 **loop 线程**——可直接改连接状态；
- 若回调里有耗时工作，丢线程池，结果再 `queueInLoop`；
- 关连接仍走延迟销毁，避免与本轮 Channel 列表打架。

#### 3.4 用 Linux 命令辅助理解 timerfd

```bash
# man 页（本课唯一指定资源）
man 2 timerfd_create
man 2 timerfd_settime
man 2 timerfd_gettime

# 编译并跑本节 1.3 的最小示例
gcc -O2 -o tfd_demo tfd_demo.c && ./tfd_demo
# 约 3 秒后打印 expired

# 观察：进程打开了 timerfd
ls -l /proc/self/fd   # 在你的服务进程里看 timerfd 是否存在
```

阅读 man 页时重点核对：

- `clockid` 取 `CLOCK_MONOTONIC` vs `CLOCK_REALTIME` 的语义；
- `TFD_TIMER_ABSTIME` 与相对模式；
- `read` 返回的 overrun 计数在 interval 模式下的含义；
- 非阻塞 timerfd 在尚未到期时 `read` → `EAGAIN`。

#### 3.5 推送重试退避：定时器状态机小品

```python
# push_retry.py —— 公众号模板消息下游失败后的退避示意
BACKOFF = [1, 2, 5, 10, 30]  # 秒，单调相对延时

class PushRetry:
    def __init__(self, loop, send_fn):
        self.loop = loop
        self.send_fn = send_fn
        self.attempt = 0
        self.timer_id = None

    def on_failure(self):
        if self.attempt >= len(BACKOFF):
            self.give_up()
            return
        delay = BACKOFF[self.attempt]
        self.attempt += 1
        # 先取消未触发的旧重试，避免重复调度
        if self.timer_id is not None:
            self.loop.cancel(self.timer_id)
        self.timer_id = self.loop.run_after(delay, self._fire)

    def _fire(self):
        self.timer_id = None
        ok = self.send_fn()
        if not ok:
            self.on_failure()

    def on_success(self):
        if self.timer_id is not None:
            self.loop.cancel(self.timer_id)
            self.timer_id = None
        self.attempt = 0

    def give_up(self):
        # 记失败指标、告警、落死信——不要再 schedule
        pass
```

这里「取消旧 timer 再加新 timer」是防止竞态的基本动作：若不去掉旧的，网络抖动下可能多个重试回调叠加，变成重试风暴。

---

## 常见误区

节点 pitfalls：

> 定时器触发时间受调度影响，不能当硬实时保证。

展开几条上线前必排的坑：

### 误区 1：「定时器到点就会准时回调」

用户态定时器受：进程调度、loop 被业务回调堵住、`run_expired` 里慢回调拖死后续到期任务、容器 CPU throttle 等影响。

**对策**：把超时当「不会早于、但可能晚于」的软期限；硬实时请走内核实时路径/专用硬件，不在本课 Reactor 定时器范畴。心跳超时阈值要留余量（如间隔 30s、超时 90s）。

### 误区 2：「用 `system_clock` / `gettimeofday` 存 deadline」

墙钟一跳，相对超时全毁。这是 acceptance 明确反对的。

**对策**：内部截止时间只用 monotonic / steady；墙钟仅用于日志。

### 误区 3：「cancel 之后回调绝不会再跑，所以可以直接 free 对象」

取消与「已进入到期快照 / 正在执行」之间仍有窗口；跨线程 cancel 更宽。

**对策**：回调用 `weak_ptr`；执行前查 `canceled`；对象释放仍遵守课 57 延迟销毁。

### 误区 4：「每个定时器一个 timerfd / 一个线程」

十万连接 × 一个 timerfd = fd 耗尽；一线程一 sleep = 调度崩溃。

**对策**：每 EventLoop **一个** timerfd + 用户态队列（堆或轮）。

### 误区 5：「时间轮很酷，精度也可以当无限高」

tick=100ms 时，延时 1ms 与 99ms 可能落在同一槽；跨轮降级写错会丢定时器或重复触发。

**对策**：按业务选粒度；空闲 60s 用 1s tick 足够；先堆后轮。

### 误区 6：「重复定时器回调里再手动 sleep 对齐」

在 loop 线程 sleep = 整 loop 假死（课 57）。

**对策**：只用队列调度下一次 expiration，绝不在回调里阻塞等时间。

---

## 设计权衡

节点 tradeoffs：

> 时间轮批处理高效，但精度、跨轮管理与取消更复杂。

### 权衡 A：最小堆 vs 时间轮

| 方案 | 优点 | 代价 |
|------|------|------|
| **堆 + 单 timerfd**（推荐起点） | 实现简单、任意精度 deadline、取消打标清晰 | 大规模时 log n 与内存局部性变差 |
| **分层时间轮** | 海量定时器摊还 O(1)、槽内批处理快 | tick 精度、级联、取消手柄、追赶逻辑复杂 |
| **每个任务 `timer_create` POSIX 信号定时器** | 内核支持 | 信号与 Reactor 难和谐；不适合高并发网络服务 |

公众号接入层：通用超时/心跳先堆；若 idle 检测成为 CPU 热点，再对 idle 专用时间轮。

### 权衡 B：相对 settime vs 绝对 ABSTIME

- 相对：每次根据「现在→最近到期」重新 `timerfd_settime`，实现直观；
- 绝对 monotonic：减少「计算 howlong 时的竞态窗口」，但要统一时间基。

二者都应用 `CLOCK_MONOTONIC`；不要用 `CLOCK_REALTIME` + ABSTIME 做连接超时。

### 权衡 C：到期回调里做多少工作

与课 57「控制每轮工作量」同一哲学：一次弹出一万个到期回调、每个关连接又同步刷盘，会让后续 IO 饿死。

工程手段：

- 限制单次 `run_expired` 最大回调数，余下留待下一轮（并立刻再 arm timerfd）；
- 重回调丢线程池；
- 监控「定时器延迟 = 实际触发 − 期望截止」直方图。

### 权衡 D：惰性删除 vs 堆内真删

- 惰性打标：实现简单，堆可能堆积已取消节点，需定期或在 pop 时回收；
- 真删：需要句柄指向 heap index，交换删除，代码更易错。

教学与多数生产实现先惰性；取消率极高时再优化。

---

## 动手实践

节点 practice：

> 给Reactor加入timerfd和可取消定时器，测试取消与触发同时发生。

下面给一条可验收的最小路径（C++17 或 Python 语义对齐即可）。

### 步骤 1：创建 timerfd 并挂到 Channel

- `timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC)`；
- 包装为 Channel，`enableReading`，`readCallback = TimerQueue::handleRead`；
- 确认只在 loop 线程操作 timerfd / 堆。

### 步骤 2：实现 TimerQueue（最小堆版）

- `add(delay, cb)` / `addEvery(interval, cb)` → 返回 `timer_id`；
- `cancel(timer_id)` → 置 `canceled`；
- `runExpired`：弹出所有 `expiration <= now`，跳过已取消，执行回调；重复者重新入堆；
- 每次队列变更后 `timerfd_settime` 对准新的最近到期点。

### 步骤 3：EventLoop API 糖

```cpp
TimerId EventLoop::runAfter(double delay_sec, Functor cb);
TimerId EventLoop::runEvery(double interval_sec, Functor cb);
void    EventLoop::cancel(TimerId id);
```

内部全部转到本 loop 的 `TimerQueue`（跨线程调用则先 `runInLoop`）。

### 步骤 4：验收 A —— 墙钟不影响相对超时

```text
1. loop.runAfter(5.0, flag_set)
2. 启动后约 1s，把系统墙钟向前拨 1 小时（实验机/容器权限）
3. 断言：flag 仍在大约第 5 秒（单调）被设置，而不是立刻或永远不
4. 对照反例：故意用 CLOCK_REALTIME 的坏实现，应能复现「误触发」
```

验收对照：

```text
[ ] 修改系统墙钟不影响相对超时
```

### 步骤 5：验收 B —— 取消与触发竞态

```text
1. 注册 runAfter(0.05, cb)，cb 内访问 shared_ptr 对象字段
2. 在即将到期的同一时间窗口：cancel(id) 并 reset shared_ptr
3. 正确：cb 不跑，或 weak_ptr 升级失败后空过；ASAN 无 UAF
4. 错误：cb 仍跑且解引用已释放对象 → ASAN 报错
```

建议构造「已进入到期列表之后才 cancel」的路径：例如在另一个定时器回调或 `queueInLoop` 里 cancel，与 `runExpired` 交错。

验收对照：

```text
[ ] 取消后的回调不访问失效对象
```

### 步骤 6：业务小场景三条

1. **空闲超时**：mock 连接，3s 无 `onMessage` 则关闭标志置位；中途 `onMessage` 应 reset。
2. **心跳**：`runEvery(1s)` 计数，`cancel` 后计数停止增加。
3. **推送退避**：失败三次分别在 1s/2s/5s 左右重试，成功则取消后续。

### 步骤 7：sourceStudy（对照 man 页）

节点 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

打开本课唯一资源：

- [timerfd_create(2) — Linux man-pages](https://man7.org/linux/man-pages/man2/timerfd_create.2.html)

追踪清单（写进笔记）：

| 问题 | 你在 man 页 / 实验里找到的答案 |
|------|-------------------------------|
| `CLOCK_MONOTONIC` 与 `CLOCK_REALTIME` 作 clockid 时，墙钟调整各有何影响？ | |
| `timerfd_settime` flags=0 与 `TFD_TIMER_ABSTIME` 区别？ | |
| 未到期时非阻塞 `read` 的 errno？ | |
| interval 模式下 `read` 返回值 >1 代表什么？ | |
| 为何必须在事件里 `read` 清空 timerfd？ | |
| 你的内核 / glibc 版本与 man 页注明的特性是否一致？ | |

### 预期现象

- 改墙钟后，相对超时仍按单调时间触发；
- 高压 cancel/fire 交错下 ASAN 干净；
- `runEvery` 在 cancel 后不再回调；
- loop 被故意 `sleep(1)` 堵住时，定时器「迟到」——印证非硬实时。

### 验收清单

```text
[ ] 能解释 monotonic/steady 与 realtime/system 的区别与用途
[ ] EventLoop 集成了单一 timerfd Channel
[ ] 支持 add / cancel / 一次性与重复定时器
[ ] 修改系统墙钟不影响相对超时
[ ] 取消与触发竞态下回调不访问失效对象
[ ] 对照 man7 timerfd_create 完成 sourceStudy 表格
[ ] 明白定时器非硬实时；时间轮与堆的选型权衡
```

---

## 自测题

**题 1**  
关于相对超时应使用的时钟，下列哪一项最准确？

A. 一律用 `CLOCK_REALTIME`，因为可读性好、便于对日志  
B. 用 `CLOCK_MONOTONIC` / `steady_clock` 存截止与算间隔，避免墙钟跳变影响超时；墙钟仅用于展示  
C. 用 CPU 时间戳计数器（TSC）裸读即可，不必走内核时钟  
D. 用 `time()` 返回的秒数加延时，再转成 `itimerspec` 绝对时间

**题 2**  
接入层单 EventLoop 上有约 2000 个毫秒级定时器（心跳、重试），下列哪种结构最合适作为**默认**实现？

A. 每个定时器 `pthread_create` + `sleep`  
B. 每个定时器一个 `timerfd` 全部丢进 epoll  
C. 单 timerfd + 按 expiration 排序的最小堆（或等价有序容器），到期批量回调  
D. 必须上分层时间轮，否则无法工作

**题 3**  
定时器回调关联的连接对象可能在 cancel 后被销毁。下列哪项最符合本课 acceptance？

A. cancel 后立刻 `delete conn`，并假定回调不会再进入  
B. 回调通过 `shared_ptr` 延长对象生命到回调结束即可，无需 cancel 标志  
C. cancel 置位；回调用 `weak_ptr`（或等价世代检查），升级失败或不该跑则直接返回，保证不访问失效对象  
D. 在任意工作线程直接跑定时器回调，加快取消生效

---

### 自测题参考答案

**题 1：B**  
节点 points 第一项即 steady/monotonic 时钟；acceptance 要求改墙钟不影响相对超时。A/D 把超时绑到墙钟；C 的 TSC 有频率与跨核问题，且不能替代本课 timerfd 语义教学。

**题 2：C**  
两千级规模堆完全可承受；单 timerfd 融入 Reactor。A 线程爆炸；B fd 与 epoll 项爆炸；D 时间轮是大规模/粗粒度优化，不是默认必选项（见 tradeoffs）。

**题 3：C**  
对应 acceptance「取消后的回调不访问失效对象」与 points「取消、重复定时和竞态」。A 忽视竞态窗口；B 只靠 shared_ptr 可能导致对象被定时器意外延长生命，且仍需处理「逻辑上已取消」；D 破坏 loop 线程亲和性。

---

## 推荐资源

- [定时器、时间轮与单调时钟：Linux man-pages](https://man7.org/linux/man-pages/man2/timerfd_create.2.html)（man7.org，英文，文档，进阶，免费）
  - 重点阅读 steady/monotonic 时钟与最小堆与时间轮相关实践所依赖的 timerfd 语义，用于核对本节点实验的边界。
  - 精读 `timerfd_create` 的 `clockid`、`timerfd_settime` 相对/绝对模式、`read` 的 overrun 计数与错误码；把结论记进 sourceStudy 笔记，并标注你实验环境的内核版本。

---

## 本课小结

1. **定时器按截止时间触发任务**：接入层用它做空闲超时、心跳、重试退避——是连接生命周期与可靠性的基础设施。
2. **相对超时必须用单调时钟**：`CLOCK_MONOTONIC` / `steady_clock`；`timerfd_create(CLOCK_MONOTONIC)`；改墙钟不得影响超时（acceptance）。
3. **最小堆适合中小规模高精度调度；时间轮适合海量粗粒度批处理**：先堆后轮；轮的精度、跨轮与取消更复杂（tradeoffs）。
4. **每 EventLoop 一个 timerfd + 用户态队列**：到期 `read` 清空再跑回调；融入课 57 的 Channel / 线程亲和模型。
5. **取消、重复与竞态**：cancel 打标 + 回调 `weak_ptr`；取消后的回调不访问失效对象（acceptance）。
6. **非硬实时**：触发时间受调度与 loop 负载影响（pitfalls）；超时阈值留余量。
7. **对照 man7 timerfd 做 sourceStudy**：用官方语义核对你的实现与边界条件。

下一课会在 IO 模型与事件主题上继续向前；请先把 timerfd 挂进你的 EventLoop，完成「墙钟无关」与「取消竞态安全」两条验收，再进入后续内容。

---

## 配套练习建议

若课程仓库后续补充 `practice58/`，建议最小文件集：

| 文件 | 作用 |
|------|------|
| `timer_queue.h/cc` 或 `timer_queue.py` | 堆 + cancel + 一次性/重复 |
| `event_loop.*`（可基于 practice57） | 挂载 timerfd Channel；`runAfter`/`runEvery`/`cancel` |
| `test_timer_monotonic.*` | 改墙钟（或 mock 时钟）后相对超时仍正确 |
| `test_timer_cancel_race.*` | 取消与触发交错；ASAN / weak_ptr 断言 |
| `demo_idle_heartbeat.*` | 空闲超时 + 心跳 + 推送退避三条业务示意 |
| `run.sh` | 一键跑验收 |

运行期望：

```bash
./run.sh
# 末行：ALL_CHECKS_PASSED
```

实测要点（Asia/Shanghai，本机验证时对照）：

- `timerfd_create(CLOCK_MONOTONIC)` 后，拨动墙钟不影响 `runAfter(5s)` 的触发间隔；
- cancel 与 fire 压力交错下无 UAF；
- `runEvery` 在 cancel 后计数停止；
- 故意在回调里 `sleep` 可观察到后续定时器迟到（非硬实时体感）；
- man 页中 overrun、`EAGAIN`、相对/绝对模式能对应到你代码里的分支。

NOTE：本课重点是时钟语义、与 Reactor 的集成、取消竞态与堆/轮选型；不要求一次性实现生产级分层时间轮。若实现轮，请单独加测试覆盖跨轮降级与取消手柄，避免「能跑 Demo、一上规模就丢定时器」。
