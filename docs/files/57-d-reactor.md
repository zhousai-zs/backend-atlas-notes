# 第 57/208 课：Reactor与事件循环

- **阶段**：第 3 阶段 · 传输与Socket
- **前置课程**：d-epoll、d-framing
- **关键词**：Reactor Pattern、事件循环、Poller、Channel、EventLoop、线程亲和性、跨线程投递、pending functors、连接安全关闭、延迟销毁、重入、one loop per thread、eventfd、queueInLoop、runInLoop
- **难度**：进阶
- **所属领域**：D · IO模型与事件
- **标签**：Reactor Pattern、D、C++

---

## 导读

上一课我们把「何时就绪」落到了 `epoll`，又把「字节流如何切成消息」落到了分帧：`EPOLLIN` 之后 `recv`，再把字节喂给长度前缀解析器。到这里为止，你手里有两块砖——**就绪通知**和**帧边界**——但还缺一座楼：**如何把大量连接上的回调组织成可维护的服务架构**。

节点 summary 对本课的定位是：

> Reactor等待就绪事件并分发回调，事件循环串行维护连接状态；耗时任务需转移或拆分。

这句话拆开有三层意思：

1. **Reactor 的职责**：等待就绪事件，再把事件分发给对应连接上的回调（读、写、关闭、错误）；
2. **事件循环串行维护连接状态**：同一个 loop 线程里，连接对象的状态机按顺序推进，避免「多线程同时改同一条连接」；
3. **耗时任务要转移或拆分**：业务解码、DB、RPC、压缩若堵在 loop 线程，整条线程上的所有连接都会饿死。

放到腾讯微信公众号后台的长连接接入层，典型形态几乎总是：

- **一个或几个 IO 线程**各自跑一个事件循环（one loop per thread）；
- 每个 loop 盯住一批连接的 epoll 就绪事件；
- **工作线程池**承接耗时业务；
- 业务结果再通过「跨线程投递」回到所属 loop，安全地写回 socket 或关闭连接。

本课要建立的不是「用个 while + epoll_wait 就叫 Reactor」这种标签，而是一套可落地的工程分层：

1. **Poller / Channel / Loop 分层**：谁封装 epoll、谁绑定 fd+回调、谁驱动循环；
2. **线程亲和性与回调**：为何回调必须在 loop 线程跑，跨线程如何用 `queueInLoop` + `eventfd` 唤醒；
3. **事件注册、销毁和重入**：在回调里删连接为何不能立刻 `free`，如何用延迟销毁避免悬垂。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 Poller/Channel/Loop 分层**
   - 说清 Poller 如何封装 `epoll_create1` / `epoll_ctl` / `epoll_wait`。
   - 说清 Channel 如何把「一个 fd + 关心的事件掩码 + 读写关闭回调」绑在一起。
   - 说清 EventLoop 如何拥有 Poller、驱动 `loop()`、管理 pending functors 与唤醒。

2. **能独立完成：实现 EventLoop 与 Channel 封装，支持跨线程投递及连接安全关闭**
   - 实现 `runInLoop` / `queueInLoop`：同线程可直接跑，跨线程入队并唤醒。
   - 在读回调中关闭连接时，不立刻销毁对象，而是延迟到本轮事件分发结束或下一轮 functors。
   - 保证事件线程不执行长时间阻塞任务（耗时工作丢线程池）。

3. **满足本课验收标准（acceptance）**
   - 回调中删除连接不触发悬垂访问。
   - 事件线程不执行长时间阻塞任务。

---

## 为什么后端要懂

节点里的理由是：

> 把epoll调用组织成可维护服务架构。

### 1. 裸 epoll 调用撑不住「产品级」接入层

在课 55 你已经能写出 ET Echo Server。但当连接数上千、协议要分帧、还要跨线程回写时，若每个业务模块直接调 `epoll_ctl`、直接持有 raw fd、在任意线程 `close`，很快会出现：

- 谁负责 `DEL`？谁负责 `close`？顺序错了就是 UAF 或漏事件；
- 工作线程算完结果要 `send`，却与 IO 线程的写缓冲并发，需要到处加锁；
- 新同事读不懂「散落在各处的 epoll 调用」，改一处崩一片。

Reactor 把这些收拢成：**Poller 管内核兴趣表，Channel 管单个 fd 的回调，Loop 管循环与跨线程队列**。

### 2. 「串行维护连接状态」是正确性手段，不是性能口号

微信公众号后台长连接上，一条连接往往有：输入缓冲、输出缓冲、分帧状态机、半关闭标志、待发送队列。若多线程同时改，锁粒度要么太粗（吞吐崩），要么太细（容易死锁/漏改）。

**把连接状态的所有权固定在所属 EventLoop 线程**：任何修改要么已在该线程，要么先投递到该线程再改。这是 muduo 一脉的核心纪律，也是节点 points 里「线程亲和性与回调」要讲透的原因。

### 3. 接入层稳定性取决于「销毁时机」

线上最阴的一类 bug：`EPOLLIN` 回调里发现对端关掉了，于是 `delete conn`；但本轮 `epoll_wait` 返回的就绪列表里后面还有这条 Channel 的 `EPOLLOUT`——于是访问已释放对象。节点 practice / acceptance 明确要求：**回调中删除连接不触发悬垂访问**。这不是细节，是 Reactor 能否上线的门槛。

### 4. 耗时任务堵死 loop = 整批连接假死

一个 loop 管几千连接时，任何一个回调里同步打 DB 200ms，这 200ms 内该线程上所有连接的读事件都进不来，表现为「偶发超时」「心跳全断」。节点 summary 后半句就是这条铁律：**耗时任务需转移或拆分**。

---

## 直觉 → 原理

### 0. 先建立直觉：从「裸循环」到「可维护 Reactor」

最小可用事件循环长这样：

```python
# naive_loop.py —— 示意，非生产
while running:
    events = epoll_wait(epfd, timeout)
    for fd, revents in events:
        if revents & EPOLLIN:
            conns[fd].handle_read()
        if revents & EPOLLOUT:
            conns[fd].handle_write()
        if revents & (EPOLLERR | EPOLLHUP):
            conns[fd].handle_close()
```

它能跑 Demo，但缺三块工程能力：

1. **fd → 回调** 的映射散落在 `conns` 字典，事件掩码与业务对象生命周期缠在一起；
2. **其他线程**想安全地往某条连接写数据，没有标准入口；
3. **回调里销毁连接**时，后面的 `events` 列表可能仍指向同一对象。

Reactor 模式（以及 muduo 的实现）把这三块拆开，对应本课三个 points。

### 1. Poller / Channel / Loop 分层

#### 1.1 三层各自干什么

| 组件 | 职责 | 典型操作 |
|------|------|----------|
| **Poller** | 封装 epoll 兴趣表与等待 | `update(channel)`、`remove(channel)`、`poll(timeout) → activeChannels` |
| **Channel** | 绑定「一个 fd + 事件掩码 + 四个回调」 | `enableReading()`、`disableWriting()`、`handleEvent(revents)` |
| **EventLoop** | 拥有 Poller，驱动循环，管理跨线程任务 | `loop()`、`runInLoop(f)`、`queueInLoop(f)`、`quit()` |

直觉图：

```text
                  EventLoop::loop()
                        |
          +-------------+--------------+
          |                            |
     Poller::poll()            doPendingFunctors()
          |                            |
   active Channels[]            跨线程投递的闭包
          |
   Channel::handleEvent()
          |
   readCallback / writeCallback / closeCallback / errorCallback
          |
   TcpConnection / Acceptor / TimerQueue ...
```

#### 1.2 Channel：把「fd 的事件」变成「对象的方法」

```cpp
// 示意结构，贴近 muduo::net::Channel 语义
class Channel {
 public:
  using EventCallback = std::function<void()>;
  Channel(EventLoop* loop, int fd);

  void setReadCallback(EventCallback cb)  { readCb_ = std::move(cb); }
  void setWriteCallback(EventCallback cb) { writeCb_ = std::move(cb); }
  void setCloseCallback(EventCallback cb) { closeCb_ = std::move(cb); }
  void setErrorCallback(EventCallback cb) { errorCb_ = std::move(cb); }

  void enableReading()  { events_ |= kReadEvent;  update(); }
  void disableWriting() { events_ &= ~kWriteEvent; update(); }
  void disableAll()     { events_ = 0; update(); }
  void remove();  // 从 Poller 摘掉

  void handleEvent(uint32_t revents);  // 由 Loop 在 poll 后调用

 private:
  void update();  // loop_->updateChannel(this) → Poller
  EventLoop* loop_;
  const int fd_;
  int events_{0};      // 关心的
  int revents_{0};     // 本轮就绪的
  bool tied_{false};   // 是否用 weak_ptr 绑住 TcpConnection
  // ...
};
```

要点：

- **Channel 不拥有 fd 的关闭权**（通常由 Socket / TcpConnection 管 `close`）；它只描述「这个 fd 当前关心什么、就绪了调谁」。
- `update()` 最终落到 `epoll_ctl(ADD/MOD)`；`remove()` 落到 `DEL`。
- `handleEvent` 内部按 `revents` 分发：先看 `EPOLLERR`/`EPOLLHUP`，再读，再写——顺序要和半关闭语义一致（课 55 已讲）。

#### 1.3 Poller：epoll 的薄封装

```cpp
class Poller {  // 可有 EPollPoller 子类
 public:
  explicit Poller(EventLoop* loop);
  // 阻塞等待，填 activeChannels
  Timestamp poll(int timeoutMs, ChannelList* activeChannels);
  void updateChannel(Channel* channel);
  void removeChannel(Channel* channel);

 private:
  EventLoop* ownerLoop_;
  int epollfd_;
  std::vector<epoll_event> events_;          // 内核返回缓冲
  std::unordered_map<int, Channel*> channels_; // fd → Channel*
};
```

纪律：**只有所属 EventLoop 线程能调 Poller**（断言 `ownerLoop_->isInLoopThread()`）。这样 `channels_` 映射无需锁。

#### 1.4 EventLoop：把「等待」和「干活」串起来

```cpp
void EventLoop::loop() {
  looping_ = true;
  while (!quit_) {
    activeChannels_.clear();
    pollReturnTime_ = poller_->poll(kPollTimeMs, &activeChannels_);
    eventHandling_ = true;
    for (Channel* ch : activeChannels_) {
      currentActiveChannel_ = ch;
      ch->handleEvent(pollReturnTime_);
    }
    currentActiveChannel_ = nullptr;
    eventHandling_ = false;
    doPendingFunctors();  // 跨线程投递 + 延迟任务
  }
  looping_ = false;
}
```

这就是节点 summary 里「等待就绪事件并分发回调」的代码形态。分帧解析器、业务回调，都挂在某个 Channel 的 `readCallback` 链上，而不是散落在 `loop()` 里写 `switch(fd)`。

#### 1.5 用 Python 伪代码串一遍「新连接」路径

```python
# 伪代码：accept → 建连接 → 注册读
def on_accept(self):
    client_fd, addr = self.listen_sock.accept()
    set_nonblocking(client_fd)
    conn = TcpConnection(self.loop, client_fd, addr)
    conn.channel.set_read_callback(conn.handle_read)
    conn.channel.set_write_callback(conn.handle_write)
    conn.channel.set_close_callback(conn.handle_close)
    conn.channel.enable_reading()  # → Poller.update → epoll_ctl ADD
    self.connections[conn.id] = conn
```

对比裸循环：业务只关心「连接对象的方法」，不再直接碰 epoll API。

---

### 2. 线程亲和性与回调

#### 2.1 One Loop Per Thread

muduo 的纪律可以记成一句话：

> **每个线程至多一个 EventLoop；每个 EventLoop 只在创建它的线程跑 `loop()`。**

推论：

- 连接对象归属某个 loop（通常由 `TcpServer` 的 round-robin 或固定哈希分配）；
- 对该连接的所有状态修改、`send`、`shutdown`，最终都要在该 loop 线程执行；
- IO 线程之间互不抢同一条连接，天然减少锁。

公众号接入层常见拓扑：

```text
                   +-- IO Loop #0  (连接子集 A)
Main / Acceptor ---+-- IO Loop #1  (连接子集 B)
                   +-- IO Loop #2  (连接子集 C)
                          |
                    Worker Thread Pool
                    (解码 / 业务 / 下游 RPC)
```

Acceptor 可单独一个 loop，accept 后把 fd 交给某个 IO loop（通过跨线程投递）。

#### 2.2 为何回调必须在 loop 线程跑

假设工作线程直接改连接输出缓冲：

```cpp
// 危险：worker 与 IO 线程并发
void TcpConnection::sendInAnyThread(const string& msg) {
  outputBuffer_.append(msg);          // 竞态！
  channel_->enableWriting();          // 可能与 IO 线程同时 epoll_ctl
}
```

正确做法：把「真正改状态」的动作封进闭包，投递回 loop：

```cpp
void TcpConnection::send(const string& msg) {
  if (loop_->isInLoopThread()) {
    sendInLoop(msg);
  } else {
    loop_->runInLoop([this, msg] { sendInLoop(msg); });
  }
}
```

#### 2.3 `runInLoop` / `queueInLoop` + `eventfd` 唤醒

```cpp
void EventLoop::runInLoop(Functor f) {
  if (isInLoopThread()) {
    f();                    // 同线程：立刻执行
  } else {
    queueInLoop(std::move(f));
  }
}

void EventLoop::queueInLoop(Functor f) {
  {
    std::lock_guard<std::mutex> g(mutex_);
    pendingFunctors_.push_back(std::move(f));
  }
  // 若不在 loop 线程，或正在执行 pending，需要唤醒卡在 poll 的线程
  if (!isInLoopThread() || callingPendingFunctors_) {
    wakeup();  // write 1 字节到 eventfd
  }
}
```

唤醒通道：

```cpp
// 构造 EventLoop 时
wakeupFd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
wakeupChannel_ = new Channel(this, wakeupFd_);
wakeupChannel_->setReadCallback([this]{ handleWakeup(); });
wakeupChannel_->enableReading();

void EventLoop::wakeup() {
  uint64_t one = 1;
  ::write(wakeupFd_, &one, sizeof one);  // 让 epoll_wait 返回
}

void EventLoop::handleWakeup() {
  uint64_t one;
  ::read(wakeupFd_, &one, sizeof one);   // 清掉可读，避免 LT 忙循环
}
```

`doPendingFunctors` 的经典写法（交换队列，缩小持锁时间）：

```cpp
void EventLoop::doPendingFunctors() {
  std::vector<Functor> functors;
  callingPendingFunctors_ = true;
  {
    std::lock_guard<std::mutex> g(mutex_);
    functors.swap(pendingFunctors_);
  }
  for (auto& f : functors) f();
  callingPendingFunctors_ = false;
}
```

为何 `callingPendingFunctors_` 为真时也要 `wakeup`？因为本轮正在跑的 functors 里若再 `queueInLoop`，新任务要等下一轮；若不唤醒，可能卡在下一次 `poll` 里迟迟不跑——跨线程「关闭连接」的延迟销毁尤其依赖这条路径。

#### 2.4 小实验：跨线程投递延迟

```python
# demo_cross_thread.py —— 示意
import threading, time, queue

pending = queue.Queue()
wake = threading.Event()

def loop_thread():
    while True:
        # 模拟 epoll_wait：无事件时阻塞，被 wake 打断
        wake.wait(timeout=1.0)
        wake.clear()
        while True:
            try:
                f = pending.get_nowait()
            except queue.Empty:
                break
            f()

def worker():
    time.sleep(0.1)
    def task():
        print("ran in", threading.current_thread().name)
    pending.put(task)
    wake.set()  # 等价于 eventfd write

threading.Thread(target=loop_thread, name="IOLoop", daemon=True).start()
threading.Thread(target=worker, name="Worker").start()
time.sleep(0.5)
```

期望：打印 `ran in IOLoop`——任务在 IO 线程执行，而不是 Worker。

#### 2.5 耗时任务如何转移

节点 summary：「耗时任务需转移或拆分」。工程上常见两档：

1. **整段业务丢线程池**：`readCallback` 里只做分帧；完整消息 `pool.submit(handle_msg)`；结果用 `runInLoop` 回写。
2. **可拆的计算切片**：若必须在 loop 做（极少），把大任务切成多次 `queueInLoop`，每片限制微秒级，避免饿死其他 Channel。

验收标准写得很硬：**事件线程不执行长时间阻塞任务**。自测时可用：在回调里故意 `sleep(1)`，观察同 loop 其他连接的心跳是否集体超时——用来建立「痛感」。

---

### 3. 事件注册、销毁和重入

#### 3.1 注册路径回顾

```text
new TcpConnection
  → Channel(loop, fd)
  → set*Callback
  → enableReading()
      → EventLoop::updateChannel
          → Poller::updateChannel
              → epoll_ctl(ADD / MOD)
```

销毁时必须**逆序且延迟**：

```text
发现该关（对端关闭 / 协议错误 / 主动踢）
  → channel.disableAll() + remove()   // 先从 epoll 摘掉
  → 把「真正 delete / 从 map 擦除」queueInLoop
  → 本轮 activeChannels 遍历结束后才释放对象
```

#### 3.2 经典陷阱：在回调里立刻销毁

```cpp
void TcpConnection::handleClose() {
  channel_->disableAll();
  channel_->remove();
  // 危险：若此时仍在 EventLoop 遍历 activeChannels_
  // 后面还有本 Channel 的 pending handleEvent，就会 UAF
  connectionCallback_(shared_from_this());  // 通知上层
  // 错误示范：
  // delete this;  或  owner_->remove(this); 立刻释放
}
```

正确思路（muduo 风格）：

- `TcpConnection` 用 `shared_ptr` 管理；
- Channel 用 `tie(std::weak_ptr<void>)` 在 `handleEvent` 开头 `lock()`，保证回调期间对象活着；
- 上层 `TcpServer` 在 `removeConnection` 里 **`queueInLoop` 真正从 map 擦除并释放最后的 shared_ptr**，确保：
  - 当前 `handleEvent` 栈帧已返回；
  - 本轮 `activeChannels` 不再触碰该指针。

伪代码：

```cpp
void TcpServer::removeConnection(const TcpConnectionPtr& conn) {
  loop_->runInLoop([this, conn] { removeConnectionInLoop(conn); });
}

void TcpServer::removeConnectionInLoop(const TcpConnectionPtr& conn) {
  connections_.erase(conn->name());
  // 再把「conn 析构」丢到连接所属 IO loop，避免在错误线程析构
  ioLoop->queueInLoop([conn] { /* conn 引用计数到 0 → 析构 */ });
}
```

节点 acceptance：**回调中删除连接不触发悬垂访问**——用「延迟销毁 + shared_ptr/tie」落地。

#### 3.3 重入：同一轮里同一 Channel 被多次触及？

通常一轮 `poll` 每个 fd 至多出现一次。更危险的是**逻辑重入**：

- `handleRead` → 用户回调 → 用户主动 `conn->forceClose()` → 再进 `handleClose`；
- 或 `handleRead` 里 `send` 很大 → 同步写失败 → `enableWriting` → 同一函数栈里又改 Channel 状态。

纪律：

1. Channel 状态机要容忍「回调里 disable/remove」；
2. `eventHandling_` 期间禁止立刻释放 Channel 对象；
3. 关闭路径统一走「标记 + 延迟」，不要在深层业务回调里直接 `delete`。

#### 3.4 用日志证明「延迟销毁」

建议在练习里打时间线：

```text
t0  poll 返回, active=[fd=10, fd=11]
t1  fd=10 handleRead → 发现对端 FIN → queueInLoop(destroy fd10)
t2  fd=11 handleRead ...
t3  doPendingFunctors → 真正 erase fd10
```

若在 t1 就 `delete`，t2 之后若列表里还有 fd10（或 Channel 内部还有未完成逻辑），必然踩雷。

#### 3.5 与课 55 / 56 的衔接

- **课 55 epoll**：告诉你就绪语义、ET 要排空、ONESHOT 要 rearm、关闭事件别碰已释放连接——本课把「别碰已释放」升级成**架构级延迟销毁**。
- **课 56 分帧**：`handleRead` 里应是 `recv` → `framer.feed` → 完整消息列表；分帧状态机也是连接状态的一部分，必须只在 loop 线程改。

---

## 常见误区

节点 pitfalls：

> 事件循环单线程不意味着整个系统不存在竞态。

展开几条上线前必排的坑：

### 误区 1：「IO 单线程，所以不用想并发」

Loop 内串行 ≠ 系统无竞态。竞态常出现在：

- 工作线程与 IO 线程共享连接对象、共享全局统计、共享下游客户端；
- 多个 IO loop 误操作了不属于自己的连接；
- 信号、定时器、后台任务直接碰 socket。

**对策**：跨线程只传「不可变消息」或「投递到正确 loop 的闭包」；共享可变状态加明确归属。

### 误区 2：「在回调里 close + delete 最干净」

最脏。本轮就绪列表、Channel::tie、上层 map 迭代器都可能还握着指针。

**对策**：`disableAll` + `remove` + `queueInLoop(真正释放)`。

### 误区 3：「跨线程直接 write(fd) 更快」

绕过输出缓冲与事件掩码，会与 loop 内的 `handleWrite` 打架，出现乱序、重复写、漏 `EPOLLOUT`。

**对策**：统一 `conn->send` → `runInLoop` → `sendInLoop`。

### 误区 4：「反正有锁，loop 里也可以打 RPC」

锁解决不了「事件线程被阻塞」——acceptance 明确禁止长时间阻塞。持锁打 RPC 只会把延迟放大成死锁风险。

### 误区 5：「pending functors 里再 queueInLoop 会丢任务」

不会丢，但可能延后一轮；若忘记在 `callingPendingFunctors_` 时 `wakeup`，可能延后到下一次 IO 事件才跑——关闭路径会「偶发挂死」。

---

## 设计权衡

节点 tradeoffs：

> 串行状态简化推理，但需控制每轮工作量避免饥饿。

### 权衡 A：连接状态只在 loop 线程 vs 全丢给线程池

| 方案 | 优点 | 代价 |
|------|------|------|
| **状态只在 loop**（推荐，muduo 路线） | 连接状态机无锁、推理简单、销毁时机清晰 | 必须把耗时业务移出；跨线程投递有队列延迟 |
| **连接对象到处加锁，多线程直接操作** | 看似「任意线程都能 send」 | 锁顺序、可重入、与 epoll_ctl 并发，极难测 |
| **纯线程池 per-request，无长连接状态** | 适合短连接 HTTP | 不适合公众号长连接、推送、会话态 |

公众号后台接入层几乎总选第一行：**串行状态 + 线程池算业务**。

### 权衡 B：每轮做多少工作

`handleEvent` + `doPendingFunctors` 若一次处理几千条消息或执行超长 functor 列表，后续连接的延迟会抖动。

工程手段：

- 限制单次 `read` 循环的最大字节 / 最大消息数，余下留到下一轮（注意 ET 下要结合缓冲策略，避免「未排空却不再通知」——课 55）；
- pending 队列过长时监控告警，回压到业务提交侧；
- 多 IO 线程分摊连接，而不是把单 loop 塞爆。

### 权衡 C：`runInLoop` 立刻执行 vs 一律 `queueInLoop`

- 同线程 `runInLoop` 立刻执行：延迟最低，但可能加深调用栈、加重入；
- 一律入队：重入更安全，但多一次延迟、关闭路径更「晚」。

muduo 选择：同线程立刻跑，跨线程入队；并在文档与实现里小心 `callingPendingFunctors_`。你实现练习时应先对齐这一语义，再谈优化。

---

## 动手实践

节点 practice：

> 实现EventLoop与Channel封装，支持跨线程投递及连接安全关闭。

下面给一条可验收的最小路径（C++17 或 Python 伪实现均可；语义对齐即可）。

### 步骤 1：实现 Channel

- 字段：`loop`、`fd`、`events`、`revents`、读写关闭错误四个回调。
- 方法：`enableReading` / `disableWriting` / `disableAll` / `remove` / `handleEvent`。
- `update`/`remove` 转发到 `EventLoop`。

### 步骤 2：实现 Poller（Epoll 版）

- `epoll_create1(EPOLL_CLOEXEC)`。
- `updateChannel`：按「是否已在 map」选 `ADD` 或 `MOD`；`events==0` 可走 `DEL`。
- `poll`：`epoll_wait` → 把 `data.ptr` 转成 `Channel*` 填入 `activeChannels`，并 `set_revents`。
- 仅 loop 线程调用（可用 `threading.get_ident()` / `pthread_self` 断言）。

### 步骤 3：实现 EventLoop

- 构造时创建 Poller、`eventfd`、wakeup Channel。
- `loop()`：`poll` → 分发 → `doPendingFunctors`。
- `runInLoop` / `queueInLoop` / `wakeup` / `quit`。
- 提供 `isInLoopThread()`。

### 步骤 4：跨线程投递验收

```text
主线程创建 loop，在 IO 线程跑 loop()
工作线程调用 loop.runInLoop(lambda: flag.append(1))
断言：flag 的修改发生在 IO 线程 tid
断言：从 queueInLoop 到执行的延迟 < 合理上限（例如 50ms，视机器而定）
```

### 步骤 5：连接安全关闭验收

构造两信道场景（可用两个 pipe/socketpair 模拟）：

1. 注册 ch1、ch2，都关心读；
2. 让 ch1、ch2 同时可读，使一轮 `poll` 返回二者；
3. 在 ch1 的 `readCallback` 里触发「关闭并销毁 ch1」；
4. **错误实现**：立刻 `delete ch1` → 观察 ch1 后续事件或析构后访问是否崩；
5. **正确实现**：`queueInLoop` 延迟销毁 → 跑完本轮分发与 functors，无 UAF，无 ASAN 报错。

验收对照节点 acceptance：

```text
[ ] 回调中删除连接不触发悬垂访问
[ ] 事件线程不执行长时间阻塞任务（用代码审查 + 故意 sleep 反例演示）
```

### 步骤 6：sourceStudy（阅读 Muduo）

节点 sourceStudy：

> 阅读Muduo EventLoop、Channel与TcpConnection，追踪事件回调和延迟销毁。

请打开：

1. [muduo/net 目录](https://github.com/chenshuo/muduo/tree/master/muduo/net) —— 总览 Poller / Channel / EventLoop / TcpConnection 文件布局；
2. [EventLoop.cc](https://github.com/chenshuo/muduo/blob/master/muduo/net/EventLoop.cc) —— 精读 `loop`、`queueInLoop`、`doPendingFunctors`、`wakeup`。

追踪清单（写进你的笔记）：

| 问题 | 你在源码里找到的答案 |
|------|----------------------|
| `poll` 返回后谁调 `Channel::handleEvent`？ | |
| `runInLoop` 与 `queueInLoop` 分支条件？ | |
| 为何 `callingPendingFunctors_` 时也要 wakeup？ | |
| `TcpConnection::handleClose` 如何通知 TcpServer？ | |
| `TcpServer::removeConnection` 为何再 queue 一次？ | |
| Channel::tie 解决什么 UAF？ | |

### 预期现象

- 跨线程投递后，任务始终在 loop 线程执行；
- 关闭连接的压力测试（高频短连接）下 ASAN/Valgrind 干净；
- 在 read 回调里 `sleep(1)` 时，同 loop 另一连接的 ping-pong 延迟明显升高（反例）；移到线程池后延迟恢复。

### 验收清单

```text
[ ] 能白板画出 Poller / Channel / EventLoop 调用关系
[ ] 能解释 one loop per thread 与连接状态归属
[ ] 实现了 eventfd 唤醒与 pending functors
[ ] 回调中销毁连接改为延迟释放，无悬垂访问
[ ] 事件线程无长阻塞；耗时任务在线程池
[ ] 对照阅读过 muduo EventLoop.cc 与相关类
```

---

## 自测题

**题 1**  
关于 Poller / Channel / EventLoop 分层，下列哪一项描述最准确？

A. Channel 负责 `epoll_wait`，EventLoop 只保存配置  
B. Poller 封装 epoll，Channel 绑定 fd 与回调，EventLoop 拥有 Poller 并驱动循环与 pending 任务  
C. Poller、Channel、EventLoop 是同一对象的三种别名  
D. 业务线程应直接调用 Poller::poll 以提高吞吐量

**题 2**  
工作线程算完业务结果后，要向某条 TCP 连接发送响应。最符合本课纪律的做法是？

A. 工作线程直接 `::write(conn->fd(), ...)`  
B. 工作线程锁住 `outputBuffer_` 并 `enableWriting`  
C. 调用 `conn->send(...)`，内部 `runInLoop`/`queueInLoop` 回到连接所属 EventLoop 再改缓冲与事件  
D. 工作线程 `epoll_ctl` 加上 `EPOLLOUT` 后立刻返回

**题 3**  
在 `Channel::handleEvent`（由本轮 `activeChannels` 遍历调用）里发现连接应关闭。下列哪项最安全？

A. 立刻 `delete tcpConnection` 并 `erase` 出全局 map  
B. `disableAll` + `remove`，并把真正释放 `queueInLoop` 到本 EventLoop，确保当前分发栈与本轮列表不再使用该对象  
C. 在工作线程异步 `delete`，越快越好  
D. 只 `close(fd)`，对象泄漏到进程退出再回收即可

---

### 自测题参考答案

**题 1：B**  
节点 points 第一项即 Poller/Channel/Loop 分层：Poller 封装 epoll，Channel 绑定 fd+回调，Loop 拥有 Poller 并驱动循环。A 把职责弄反；C 抹平分层；D 破坏线程亲和性。

**题 2：C**  
线程亲和性要求连接状态只在 loop 线程修改；跨线程通过 `runInLoop`/`queueInLoop` + eventfd 唤醒。A/B/D 都会引入与 IO 线程的竞态或绕过输出缓冲语义。

**题 3：B**  
对应 acceptance「回调中删除连接不触发悬垂访问」与 points「事件注册、销毁和重入」。立刻释放会在本轮后续事件或栈上 UAF；跨线程 delete 更不可控；只 close fd 不释放对象会泄漏且易把旧事件套到 fd 复用后的新连接（课 55）。

---

## 推荐资源

- [Reactor与事件循环：参考文档 / 原始资料](https://github.com/chenshuo/muduo/tree/master/muduo/net)（github.com，英文源码，进阶）
  - 重点阅读 Poller/Channel/Loop 分层与线程亲和性与回调，用于核对本节点实验的语义和边界。
  - 在目录中定位 `EventLoop`、`Channel`、`EPollPoller`、`TcpConnection`，对照本课分层表。

- [Muduo · EventLoop 实现](https://github.com/chenshuo/muduo/blob/master/muduo/net/EventLoop.cc)（chenshuo/muduo，英文源码，进阶）
  - 阅读事件分发、跨线程唤醒和 pending functors，比较状态归属。
  - 精读 `loop`、`queueInLoop`、`doPendingFunctors`、`wakeup`/`handleRead`，把源码行号记进 sourceStudy 笔记。

---

## 本课小结

1. **Reactor = 等待就绪 + 分发回调**：事件循环串行维护连接状态；耗时任务必须转移或拆分。
2. **Poller / Channel / Loop 分层**把裸 epoll 调用收成可维护架构：Poller 管内核兴趣表，Channel 管 fd 与回调，Loop 驱动循环与跨线程队列。
3. **One loop per thread**：回调与连接状态归属固定线程；跨线程用 `runInLoop`/`queueInLoop` + `eventfd` 唤醒。
4. **销毁必须延迟**：回调里不能立刻 free；`disableAll` + `remove` + `queueInLoop` 释放，避免活跃列表与重入 UAF。
5. **单线程 loop ≠ 无竞态**：系统级竞态仍在共享状态与错误的跨线程 IO 上；串行简化的是连接状态推理。
6. **权衡**：串行状态好推理，但要控制每轮工作量，防止饥饿；对比「全丢线程池乱改连接」与「状态只在 loop」。
7. **对照 muduo 做 sourceStudy**：用真实源码验证你对 pending functors 与延迟销毁的理解。

下一课会在 Reactor 骨架之上继续 IO 模型与事件主题；请先把本课 EventLoop/Channel 跑通，并完成跨线程投递与安全关闭两条验收，再进入后续内容。

---

## 配套练习建议

若课程仓库后续补充 `practice57/`，建议最小文件集：

| 文件 | 作用 |
|------|------|
| `event_loop.py` 或 `EventLoop.h/cc` | loop / queueInLoop / eventfd 唤醒 |
| `channel.*` / `poller.*` | Channel + Epoll Poller |
| `test_reactor.*` | 跨线程投递 tid 断言；关闭连接 UAF 回归 |
| `run.sh` | 一键跑验收 |

运行期望：

```bash
./run.sh
# 末行：ALL_CHECKS_PASSED
```

实测要点（Asia/Shanghai，本机验证时对照）：

- 工作线程投递的任务，`threading.get_ident()` / `pthread_self` 与 loop 线程一致；
- 同轮多 Channel 就绪时，在首个回调里销毁连接，ASAN 无报；
- 回调内 `sleep(1)` 的反例会使同 loop 延迟升高；移出后恢复；
- 对照 `EventLoop.cc` 能指出 `doPendingFunctors` 与 `wakeup` 的对应行。

NOTE：本课重点是架构语义与生命周期，不要求一次性写出完整 muduo；但命名与职责划分建议向 muduo 对齐，便于你入职后阅读微信系常见的 Reactor 风格代码。
