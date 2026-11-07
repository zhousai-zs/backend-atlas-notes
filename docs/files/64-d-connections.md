# 第 64/208 课：海量连接与接入治理

- **阶段**：第 4 阶段 · 高级后台工程师（接入与连接治理）
- **前置课程**：d-threadpool、d-pool、c-limits、d-flow
- **关键词**：海量连接、接入治理、accept预算、连接上限、每连接内存、高低水位、SYN队列、listen backlog、慢客户端、连接风暴、优雅关闭、fd、端口、活跃比例、Reactor
- **难度**：深入
- **所属领域**：D · 传输与Socket（子类：接入与连接治理）
- **标签**：Connection Management、D、C++

---

## 导读

上一课把 TCP 的「单连接能不能跑得快」拆开了：`rwnd` / `cwnd` / 本机队列各自管哪一道闸门。那是**单条管道**的故事。

本课上升到 **海量连接** 层——公众号后台的长连接网关、接入层，面对的不是「一条流慢」，而是「几十万条半活半死的连接同时进来」：

> 海量连接设计同时受 fd、内存、内核队列、端口和业务活跃比例约束，必须控制接入与每连接成本。

拆开是三件事（对应本课 points）：

1. **accept 预算和连接上限**：内核队列再大，进程也有 `ulimit`、fd 表、应用层 `maxConnections`；Reactor 每轮 accept 几次也是预算。
2. **每连接内存与输出水位**：连接对象、读写缓冲、高低水位、写缓冲反压——达到内存预算后必须停止无界增长。
3. **SYN 队列、超时与优雅关闭**：`listen` backlog、SYN queue / accept queue、握手超时、慢客户端、`shutdown`/`close`、TIME_WAIT 与端口耗尽直觉。

微信公众号后台的典型痛点：

```text
模板消息 / 客服消息高峰 + 客户端重连风暴
  → 长连接网关 / 接入层瞬时 SYN 暴涨
  → 监控：连接数一路上涨，但业务吞吐未必涨
  → 可能原因 A：大量慢读 / 半开连接占满 fd 与内存，活跃比例极低
  → 可能原因 B：accept 循环无预算，单轮 accept 饿死业务事件
  → 可能原因 C：写缓冲无水位，对慢客户端无界堆积，RSS 飙升后被 OOM
  → 只看「当前连接数」时，最常见的错误动作是：再加机器、再抬 ulimit
```

本课承接 d-flow（单连接流控/拥塞）、d-threadpool / d-pool（并发与资源池）、c-limits（系统资源上限），目标不是背 backlog 默认值，而是建立 **可操作的接入治理框架**：看到连接风暴时，先分清是内核队列、应用 accept 预算、还是每连接成本失控——并且记住：**监控要报告活跃比例与每连接内存，不只连接数；压测要含拒绝率、p99、恢复时间。**

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 accept 预算和连接上限**
   - 说清 fd 上限（`ulimit -n`、进程内上限）、应用层 `maxConnections`、以及「每轮 accept 次数预算」各自保护什么。
   - 能画出「客户端 SYN → 内核 SYN/accept 队列 → 应用 accept → 连接对象」的路径，并指出每一层的拒绝点。
   - 能在 `ss -ltn` / `ss -s` / `/proc/sys/net/core/somaxconn` 里定位 backlog 与队列相关线索。

2. **能独立完成：对 Reactor 执行连接风暴和慢读压测，加入高低水位与接入拒绝**
   - 构造连接风暴（短连接猛建）与慢读客户端（建连后几乎不读）。
   - 在 Reactor 接入路径加入：每轮 accept 预算、连接数上限、输出高低水位、内存预算硬停。
   - 压测报告至少包含：**拒绝率、p99、恢复时间**；并验证 **达到内存预算后停止无界增长**。

3. **满足本课验收标准（acceptance）**
   - **达到内存预算后停止无界增长**。
   - **测试包含拒绝率、p99 和恢复时间**。

---

## 为什么后端要懂

节点里的理由是：

> 让服务在突发连接和慢客户端下可退化运行。

这句话直接决定你在公众号接入层值班时，会不会把「连接数涨了」一律当成「容量不够」，从而无脑加机器。

### 1. 连接数 ≠ 有效容量

一百万条连接里，可能只有几千条在真正收发。其余是：

- 心跳极低频的「僵尸」长连接；
- 握手完成却从不读响应的慢客户端；
- 半开、即将超时、即将被踢的连接。

只报连接数，无法回答：「再来一轮重连风暴，我们会不会先死在内存 / fd 上？」**活跃比例**和**每连接内存**才是比较不同机型、不同版本时有意义的量。

### 2. 突发连接是「可退化」问题，不是「要么全接要么全挂」

接入层应能：

- 在预算内尽量接；
- 超预算时**明确拒绝**（或快速关闭），而不是把事件循环拖死；
- 风暴过后在可测的**恢复时间**内回到稳态。

「可退化运行」= 有拒绝策略、有水位、有超时，而不是靠运气撑过去。

### 3. 慢客户端比「连不上」更危险

连不上至少是拒绝可见；慢客户端会：

- 占着 fd；
- 让你的输出缓冲无界增长（上一课的流控在对端，本课的水位在本端应用）；
- 把「看起来还活着」的服务拖进 OOM / 延迟雪崩。

### 4. 公众号后台里你会真正碰到的场景

| 现象 | 更像 accept/上限问题 | 更像每连接内存/水位 | 更像 SYN 队列/超时 |
|------|----------------------|---------------------|-------------------|
| 重连风暴后新连接大量失败，`ss -s` 里 SYN-RECV 飙高 | 可能次生 | 否 | backlog / syn backlog 满 |
| 连接数平稳但 RSS 持续涨 | 否 | 写缓冲无界、慢读堆积 | 否 |
| 事件循环卡顿，业务 p99 炸，但 CPU 不饱和 | accept 无预算饿死业务 | 可能伴随 | 可能 |
| 风暴结束后很久才恢复 | 连接未及时踢除 | 缓冲未释放 | TIME_WAIT / 端口压力 |

不会区分时，值班同学最容易做的三件事：抬 `ulimit`、加大 `somaxconn`、加机器——其中至少有两件可能只是把崩溃推迟几分钟。

---

## 直觉 → 原理

### 0. 先建立直觉：大门、接待大厅、工位

把接入层想成写字楼：

```text
客户端 ──SYN──► 大门安检（SYN 队列）
                  │
                  ▼ 握手完成
              接待大厅（accept 队列 / backlog）
                  │
                  ▼ accept()
              工位（连接对象 + 读写缓冲 + 业务状态）
```

- **大门（SYN queue）**：半连接；满了会丢 SYN 或触发客户端重传。
- **接待大厅（accept queue）**：已完成握手、等应用 `accept`；满了新连接也进不来。
- **工位（连接）**：每个工位占桌椅（fd、内存、定时器）；工位不够或桌子堆满纸（写缓冲）时，必须停办或拒客。

海量连接治理 = **控制进门速率 + 控制每个工位成本 + 及时清退不干活的人**。

### 1. accept 预算和连接上限

#### 1.1 三层上限，缺一不可

| 层 | 典型控制点 | 保护什么 |
|----|------------|----------|
| 内核 / OS | `ulimit -n`、`fs.file-max`、cgroup | 进程/系统 fd 总数 |
| 内核 TCP | `somaxconn`、`tcp_max_syn_backlog`、`listen(backlog)` | SYN / accept 队列深度 |
| 应用 | `maxConnections`、每轮 accept 次数、拒绝策略 | 事件循环公平性与每连接成本 |

只抬 OS 上限而不设应用上限，等于允许「合法地」把自己 OOM 掉。

查看本机线索：

```bash
ulimit -n
cat /proc/sys/fs/file-max
cat /proc/sys/net/core/somaxconn
cat /proc/sys/net/ipv4/tcp_max_syn_backlog

# 监听 socket 的 Recv-Q / Send-Q：对 listen fd，Recv-Q 常表示未 accept 的完成连接数
ss -ltn
ss -s

# 全局 TCP 统计（含 ListenOverflows / ListenDrops 等，视内核）
nstat -az | grep -iE 'Listen|Syn|Overflow|Drop'
```

#### 1.2 为什么需要「accept 预算」

Reactor（或主 IO 线程）典型循环：

```text
epoll_wait → 处理可读/可写 → 若 listen fd 可读则 accept → 注册新连接
```

若一轮里对 listen fd **无上限地 accept 到底**：

- 连接风暴时，本轮可能 accept 成千上万次；
- 已建连接的可读/可写事件被推迟 → 业务 p99 爆炸；
- 等于用「接入公平」换「业务饿死」。

因此要有 **每轮 accept 次数预算**（例如 64 / 128 / 按空闲连接名额取 min）。预算用完就先回去处理已有连接，下一轮再继续接。

Muduo 一类 Reactor 接入路径的直觉（教学抽象，不绑定具体版本 API）：

```cpp
// 教学伪代码：Acceptor / TcpServer 接入路径上的预算
void Acceptor::handleRead() {
    while (accepted_this_loop_ < kAcceptPerLoop) {
        InetAddress peer;
        int connfd = acceptSocket_.accept(&peer);
        if (connfd >= 0) {
            ++accepted_this_loop_;
            if (connectionCallback_) {
                connectionCallback_(connfd, peer);  // 内部再检查 maxConnections
            } else {
                ::close(connfd);
            }
        } else {
            // EAGAIN：本轮没有更多完成连接
            // EMFILE：fd 耗尽——需要预留空 fd 应急，或立即进入拒绝模式
            break;
        }
    }
}
```

#### 1.3 连接上限与拒绝策略

应用层 `maxConnections` 到达后，策略要**明确**：

1. **立即 `close` 新 fd**（省状态，客户端看到连接被重置/关闭）；
2. **短暂读完再关**（消耗一点 CPU，换更友好的协议层错误码——视协议而定）；
3. **暂停 listen 可读**（从 epoll 摘掉 listen，或暂不 accept），让内核队列去背压——可能表现为客户端 SYN 重传 / 连不上。

公众号长连接网关更常见的是：**超限直接关 + 打点拒绝率**，避免半开状态堆积。关键是拒绝必须可观测：

```text
metrics:
  connections_active
  connections_active_ratio   = active_with_recent_io / connections_total
  accept_rejected_total
  accept_per_loop
  conn_memory_bytes_avg
  conn_memory_bytes_p99
```

#### 1.4 EMFILE / 预留 fd

当进程 fd 耗尽时，`accept` 可能返回 `EMFILE`。经典应急手法（许多网络库采用）：

```cpp
// 启动时预留一个 idle fd
int idleFd_ = ::open("/dev/null", O_RDONLY | O_CLOEXEC);

// accept 遇到 EMFILE：
::close(idleFd_);
int connfd = ::accept(...);
if (connfd >= 0) ::close(connfd);  // 立刻丢掉，腾出位置
idleFd_ = ::open("/dev/null", O_RDONLY | O_CLOEXEC);
```

目的不是「多接几个」，而是 **避免 listen 队列永久堵死、事件循环卡在错误路径**——属于可退化运行的细节。

#### 1.5 短代码：带预算与上限的 Reactor accept

```cpp
// accept_budget_demo.cpp —— 教学向：每轮预算 + maxConnections
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdio>
#include <unordered_set>
#include <vector>

constexpr int kMaxConn = 1024;
constexpr int kAcceptPerLoop = 64;

int main() {
    int listenfd = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
    int yes = 1;
    ::setsockopt(listenfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(8080);
    ::bind(listenfd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    // backlog 最终还受 somaxconn 等限制，见 listen(2)
    ::listen(listenfd, 512);

    int ep = ::epoll_create1(EPOLL_CLOEXEC);
    epoll_event ev{};
    ev.events = EPOLLIN;
    ev.data.fd = listenfd;
    ::epoll_ctl(ep, EPOLL_CTL_ADD, listenfd, &ev);

    std::unordered_set<int> conns;
    std::vector<epoll_event> events(128);

    for (;;) {
        int n = ::epoll_wait(ep, events.data(), static_cast<int>(events.size()), 1000);
        for (int i = 0; i < n; ++i) {
            if (events[i].data.fd == listenfd) {
                int accepted = 0;
                while (accepted < kAcceptPerLoop) {
                    sockaddr_in peer{};
                    socklen_t len = sizeof(peer);
                    int cfd = ::accept4(listenfd, reinterpret_cast<sockaddr*>(&peer),
                                        &len, SOCK_NONBLOCK | SOCK_CLOEXEC);
                    if (cfd < 0) break;  // EAGAIN 或其他错误

                    if (static_cast<int>(conns.size()) >= kMaxConn) {
                        // 拒绝：有上限才叫治理
                        ::close(cfd);
                        // 这里应 ++accept_rejected_total
                        continue;
                    }
                    conns.insert(cfd);
                    epoll_event cev{};
                    cev.events = EPOLLIN | EPOLLRDHUP;
                    cev.data.fd = cfd;
                    ::epoll_ctl(ep, EPOLL_CTL_ADD, cfd, &cev);
                    ++accepted;
                }
            } else {
                // 简化：可读则读一点；对端关闭则清连接
                char buf[4096];
                ssize_t r = ::recv(events[i].data.fd, buf, sizeof(buf), 0);
                if (r <= 0) {
                    ::epoll_ctl(ep, EPOLL_CTL_DEL, events[i].data.fd, nullptr);
                    ::close(events[i].data.fd);
                    conns.erase(events[i].data.fd);
                }
            }
        }
        // 监控直觉：定期打印 conns.size()、拒绝计数、（真实系统里）活跃比例
    }
}
```

---

### 2. 每连接内存与输出水位

#### 2.1 每连接成本账单

一条 TCP 连接在用户态至少可能占用：

```text
连接对象 / 状态机
+ 读缓冲（input buffer）
+ 写缓冲（output buffer）——慢客户端时的炸弹
+ 协议上下文、定时器、会话元数据
+ 内核侧 socket 缓冲（SO_RCVBUF / SO_SNDBUF，另计）
```

海量连接时，**平均值 × 连接数** 才是 RSS 的主因。因此监控必须有：

- `conn_memory_bytes_avg` / `p99`
- `active_ratio`（例如最近 N 秒有读写的连接占比）

节点 pitfalls 说得很直白：

> 只报告连接数而不报告活跃比例和每连接内存没有比较意义。

#### 2.2 高低水位：把上一课的「反压」落到应用缓冲

上一课：对端 `rwnd` 变小是内核流控。本课：即便内核还能收一点，**应用写缓冲**也可能被慢客户端无限堆积。

经典高低水位策略（Muduo `TcpConnection` / `setWriteCompleteCallback` / high water mark 一类路径的教学抽象）：

```text
写缓冲长度
    │
    │   超过 highWaterMark → 通知上层：暂停生产 / 断开 / 降级
    │          ▲
    │          │
    │   正常堆积区
    │          │
    │          ▼
    │   低于 lowWaterMark  → 通知上层：可恢复生产（可选）
    └────────────────────────────►
```

示例逻辑：

```cpp
// 教学伪代码：输出水位
class TcpConnection {
 public:
  void send(const void* data, size_t len) {
    outputBuffer_.append(data, len);
    if (outputBuffer_.readableBytes() >= highWaterMark_ &&
        !highWaterMarkTriggered_) {
      highWaterMarkTriggered_ = true;
      if (highWaterMarkCallback_) {
        highWaterMarkCallback_(shared_from_this(),
                               outputBuffer_.readableBytes());
      }
    }
    // 注册 EPOLLOUT，在 handleWrite 里 drain
  }

  void handleWrite() {
    ssize_t n = writeFd(outputBuffer_);
    // ...
    if (outputBuffer_.readableBytes() <= lowWaterMark_ &&
        highWaterMarkTriggered_) {
      highWaterMarkTriggered_ = false;
      if (writeCompleteCallback_) writeCompleteCallback_(shared_from_this());
    }
  }
};
```

**达到内存预算后停止无界增长**——这是本课 acceptance 的硬条件。实现上可以是：

1. 单连接写缓冲硬上限：超过则 `forceClose`；
2. 进程级连接缓冲总账：超过则拒绝新 accept，并对最慢连接踢除；
3. 高水位回调里停止从上游拉模板消息 / 推送任务。

#### 2.3 慢读压测在测什么

慢读客户端：建连成功，几乎不 `recv`。对网关而言：

- 内核 `SO_SNDBUF` 先满 → 应用 `write`/`send` 开始堆积到用户态 output buffer；
- 若无水位 → 缓冲随推送任务无界涨 → RSS 涨 → OOM；
- 有水位 → 触发拒绝/断开/暂停上游 → **可退化**。

对照实验要记的数：

| 指标 | 无水位（坏） | 有水位（好） |
|------|--------------|--------------|
| 连接数 | 可到上限 | 可到上限 |
| RSS / 每连接缓冲 | 持续无界涨 | 触及预算后平台 |
| 拒绝率 | 0 或很晚才崩 | 可控上升 |
| p99 | 崩前突然炸 | 有上限可预期 |
| 恢复时间 | 需重启 | 踢慢连接后可测回落 |

#### 2.4 短代码：带硬上限的写缓冲

```cpp
// write_watermark_demo.cpp —— 教学向片段
#include <cstddef>
#include <string>

class OutputBufferGuard {
 public:
  explicit OutputBufferGuard(size_t hard_limit)
      : hard_limit_(hard_limit) {}

  // 返回 false 表示触及预算：调用方应停止追加并关闭连接
  bool append(const char* data, size_t len) {
    if (buf_.size() + len > hard_limit_) {
      return false;  // 停止无界增长
    }
    buf_.append(data, len);
    return true;
  }

  size_t size() const { return buf_.size(); }

 private:
  std::string buf_;
  size_t hard_limit_;
};
```

真实 Reactor 里还要配合：可写事件、部分写、`EAGAIN`、以及把「暂停上游生产」接到线程池 / 任务队列（回顾 d-threadpool、d-pool）。

---

### 3. SYN 队列、超时与优雅关闭

#### 3.1 listen backlog 到底限制什么

阅读 [listen(2)](https://man7.org/linux/man-pages/man2/listen.2.html) 时抓住：

- `listen(sockfd, backlog)` 的 `backlog` 语义与内核版本相关；
- 现代 Linux 上，它与 `somaxconn` 等一起影响**已完成连接队列**（accept queue）的上限；
- SYN 洪泛相关还有 `tcp_max_syn_backlog`（半连接队列）。

教学模型（简化）：

```text
SYN 到达
  → SYN queue（半连接，未完成三次握手）
  → 握手完成 → accept queue（满连接，等 accept）
  → accept() → 交给应用
```

两队列任一溢出，都会表现为：客户端连不上、重试、或服务端 `ListenOverflows` 一类计数上涨。

```bash
ss -ltn                     # 看 Listen 与 Recv-Q（未 accept 积压直觉）
cat /proc/sys/net/core/somaxconn
cat /proc/sys/net/ipv4/tcp_max_syn_backlog
nstat -az | grep -i Listen
```

**sourceStudy 要求**：结合 Muduo 接入路径和 Linux `accept`/`listen` 文档，定位 **接入队列、应用队列与输出缓冲** 的边界——三者不是一回事：

| 边界 | 位置 | 满了会怎样 |
|------|------|------------|
| 接入队列（SYN / accept queue） | 内核 | 新连接进不来 / 重传 |
| 应用队列（待处理事件、任务队列） | 用户态 Reactor / 线程池 | p99 涨、任务堆积 |
| 输出缓冲（connection output buffer） | 用户态每连接 | 内存涨、必须靠水位停 |

#### 3.2 超时：别让半死连接长住

接入治理常用超时：

- **握手 / 连接建立超时**（客户端侧常见；服务端对半开也有内核参数）；
- **空闲超时**（多久无读写就踢——长连接网关必做）；
- **写完成超时**（高水位后若迟迟不能 drain，强制关闭）。

公众号长连接场景：心跳间隔已知，空闲超时应略大于心跳容忍倍数，避免误杀；但对「建连后从不读推送」的慢客户端，写超时 / 水位踢除更关键。

#### 3.3 优雅关闭：shutdown 与 close

```text
shutdown(fd, SHUT_WR)  → 发 FIN，半关闭写侧；仍可读
close(fd)              → 释放 fd；若还有未读数据，可能给对端 RST
```

优雅关闭常见路径：

1. 上层决定下线 → 停止从上游拉新消息；
2. 尽量把 output buffer drain 完；
3. `shutdown(SHUT_WR)`；
4. 读到对端 FIN / 超时 → `close`。

连接风暴 + 优雅关闭要平衡：**不能为了优雅而让关闭队列本身成为新的堆积点**。批量踢连接时用预算（每轮踢 N 个），避免事件循环再次被关连接拖死。

#### 3.4 TIME_WAIT 与端口耗尽直觉

短连接风暴下，主动关闭的一方会进 `TIME_WAIT`，占用本地四元组。客户端或对下游短连接的网关可能遇到：

- 临时端口耗尽（`EADDRNOTAVAIL`）；
- 大量 `TIME_WAIT` 让 `ss -s` 看起来「连接很多」但并非业务活跃。

治理直觉（细节回看 d-tcp / c-limits）：

- 能复用连接就复用（连接池，d-pool）；
- 需要时再评估 `SO_REUSEADDR` / 协议设计，而不是先关 `tcp_tw_recycle` 一类危险开关（现代内核已移除或不当）；
- 压测要观察端口与 `TIME_WAIT`，否则「恢复时间」会被四元组耗尽掩盖。

#### 3.5 一张总图：从 SYN 到可退化

```text
连接风暴
  │
  ├─► SYN/accept 队列：backlog / somaxconn —— 内核背压
  │
  ├─► accept 预算：每轮 N 次 —— 保护事件循环公平
  │
  ├─► maxConnections / EMFILE 策略 —— 硬上限与可观测拒绝
  │
  ├─► 每连接写水位 + 进程内存预算 —— 停止无界增长
  │
  └─► 空闲/写超时 + 优雅关闭预算 —— 回收半死连接，缩短恢复时间
```

---

## 常见误区与设计权衡

### 误区 1：只报告连接数

**真相（节点 pitfalls）**：只报告连接数而不报告活跃比例和每连接内存没有比较意义。

**正确动作**：同一张监控面板至少给出：

- 总连接数；
- 活跃比例（近期有 IO 的占比）；
- 每连接内存 avg / p99；
- 拒绝率；
- accept 队列积压 / ListenOverflows。

### 误区 2：抬高 ulimit / somaxconn = 接入治理完成

**真相**：那只是把天花板垫高。没有应用层预算与水位，风暴来时你只是「有资格」撑到 OOM。

### 误区 3：连接风暴时拼命 accept，业务事件稍后说

**真相**：无预算 accept 会饿死已有连接的读写，p99 先炸，客户端更重连，风暴自我加强。

### 误区 4：慢客户端「反正 TCP 有流控」

**真相**：内核流控保护的是 socket 缓冲与对端通告窗口；**用户态输出缓冲**仍可能被业务层无限 `send` 堆积。水位是应用责任。

### 误区 5：压测只看 QPS / 平均延迟

**真相**：接入治理压测必须看 **拒绝率、p99、恢复时间**；以及内存是否在预算处平台。缺这三项，无法证明「可退化」。

### 设计权衡小结

节点 tradeoffs：

> 提高并发容量可能降低单请求效率，应按真实流量模型取舍。

| 选择 | 收益 | 代价 |
|------|------|------|
| 更高 `maxConnections` | 容纳更多在线终端 | 每连接内存与定时器成本↑；单请求缓存局部性变差 |
| 更大 accept 预算 | 风暴下更快排空内核队列 | 单轮可能饿死业务事件 |
| 更严的写水位 / 更小缓冲 | 内存可控、恢复快 | 更多踢连接、短时拒绝率↑ |
| 更长空闲超时 | 少误杀长连接 | 僵尸连接占 fd 更久 |
| 短连接 + 大并发 | 实现简单 | TIME_WAIT / 端口压力；不如池化（d-pool） |

取舍应按**真实流量模型**：公众号长连接往往是「连接多、活跃低、突发重连」——优先优化每连接成本与踢除策略，而不是盲目追最大连接数。

---

## 动手实践

> 实践目标（节点 practice）：对 Reactor 执行连接风暴和慢读压测，加入高低水位与接入拒绝。  
> 验收（acceptance）：达到内存预算后停止无界增长；测试包含拒绝率、p99 和恢复时间。  
> sourceStudy：结合 Muduo 接入路径和 Linux accept/listen 文档，定位接入队列、应用队列与输出缓冲的边界。

### 环境与版本记录（先做）

```bash
uname -r
ulimit -n
cat /proc/sys/net/core/somaxconn
cat /proc/sys/net/ipv4/tcp_max_syn_backlog
man -w 2 listen
man -w 2 accept
ss -V 2>&1 | head -1
# 把输出抄进实验笔记：内核、ulimit、somaxconn、man-pages 路径
```

打开并阅读：

[https://man7.org/linux/man-pages/man2/listen.2.html](https://man7.org/linux/man-pages/man2/listen.2.html)

重点检索：`backlog`、与 `somaxconn` 的关系、历史语义变化。同时对照本机 `man 2 listen`、`man 2 accept`，把「文档语义 vs 本机内核行为」写成 checklist。

### 步骤 1：准备带治理开关的 Reactor 服务

基于本课示例或你现有的 Reactor（可对照 Muduo `Acceptor` / `TcpServer` / `TcpConnection` 源码路径）：

- [ ] `maxConnections` 可配置
- [ ] 每轮 `kAcceptPerLoop` 可配置
- [ ] 输出缓冲 hard limit + high/low water mark
- [ ] 指标：连接数、拒绝计数、估算每连接缓冲、活跃计数

先跑通「正常客户端」基线：记录 QPS、p50/p99、RSS、连接数。

### 步骤 2：连接风暴压测

用多进程/`wrk`-like 或自写客户端瞬间建大量连接（可只 connect 再闲置）：

```bash
# 示例：简单风暴脚本思路（教学）
# 用 go/python/C++ 开 N 个线程各自 dial；或使用现成 TCP 压测工具
# 关键：观察服务端
watch -n 0.5 'ss -ltn sport = :8080; ss -s | head'
nstat -az | grep -iE 'Listen|Syn|Overflow'
```

记录表：

```text
| 配置（maxConn / acceptPerLoop） | 成功连接 | 拒绝数 | 拒绝率 | p99 | ListenOverflows | 备注 |
```

期望直觉：有上限时拒绝率上升但事件循环仍能服务已有连接；无预算时 p99 先坏。

### 步骤 3：慢读压测 + 水位

客户端建连后：对推送数据几乎不读（读极慢或零读），服务端持续 `send`：

```text
对照组 A：无写缓冲上限 —— 观察 RSS / 每连接缓冲是否无界涨
对照组 B：有 hard limit / high water mark —— 观察是否在预算处停止增长并踢除/拒绝
```

验收句：

- [ ] **达到内存预算后停止无界增长**（RSS 或 per-conn buffer 曲线平台，而非直线上升直到 OOM）

### 步骤 4：测量恢复时间

风暴或慢读结束后：

1. 停止压测客户端；
2. 观察连接数回落、拒绝率归零、p99 回到基线的时间；
3. 若有空闲超时 / 强制踢除，记录「从停止压测到指标恢复」的秒数。

```text
恢复时间定义（写进笔记，保持一致）：
  T0 = 压测停止
  T1 = 连接数回到基线 ±5%
  T2 = p99 回到基线 ±10%
  报告 max(T1, T2) - T0
```

### 步骤 5：sourceStudy 核对清单

对照 Muduo（或你采用的 Reactor）接入路径 + `listen(2)` / `accept(2)`：

- [ ] 指出 **接入队列**（内核 SYN/accept queue）在代码中对应的假设（`listen` backlog、是否依赖默认）
- [ ] 指出 **应用队列**（事件循环、待处理 channel、线程池任务队列）边界
- [ ] 指出 **输出缓冲**（connection output buffer）与高低水位回调位置
- [ ] 记录：内核版本、`somaxconn`、实验里实际生效的 backlog
- [ ] 错误路径：`EMFILE`、`EAGAIN`、超限拒绝是否打点

写明：**适用内核版本、发行版、以及你验证过的边界条件**。

### 步骤 6：验收清单

- [ ] 能解释 accept 预算与三层连接上限
- [ ] 完成连接风暴压测，保存拒绝率 / p99 / 队列计数
- [ ] 完成慢读压测，证明内存预算硬停
- [ ] 报告恢复时间
- [ ] 监控面板含活跃比例与每连接内存（不只连接数）
- [ ] sourceStudy 笔记含三边界与版本

---

## 自测题

**题 1.** 用自己的话说明：什么是 accept 预算？为什么连接风暴时「把 listen 上可读事件一次 accept 干净」可能伤害业务 p99？fd 上限、`maxConnections`、内核 backlog 分别卡在哪一层？在公众号长连接网关重连风暴里，若只看到连接数上涨就加机器，可能漏掉什么指标？

**题 2.** 为什么「只报告连接数而不报告活跃比例和每连接内存没有比较意义」？慢客户端场景下，TCP 内核流控已经存在，为何仍要在应用层做输出高低水位？怎样才算满足 acceptance「达到内存预算后停止无界增长」？

**题 3.** 画出 SYN queue / accept queue / 应用输出缓冲的边界。压测报告为什么必须包含拒绝率、p99 和恢复时间？结合 `listen(2)`，说明盲目加大 backlog 的收益与风险；tradeoffs「提高并发容量可能降低单请求效率」在长连接网关里如何体现？

### 自测题参考答案

**题 1.** accept 预算 = 每个事件循环轮次最多 `accept` 的次数（可与剩余连接名额取 min），用于保证已建连接的读写事件仍有机会被处理。一次 accept 干净会在风暴下占用整轮循环，导致业务事件延迟、p99 上升，并可能诱发更多重连。fd 上限在 OS/进程；`maxConnections` 在应用；backlog 在内核接入队列。只看连接数加机器，可能漏掉活跃比例、每连接内存、拒绝率、恢复时间——加机器也不解决无水位导致的内存炸弹。

**题 2.** 连接数不区分僵尸/慢读/真正活跃，也无法反映内存成本；跨版本、跨机型比较需要活跃比例与每连接内存。内核流控限制的是 socket 缓冲与对端窗口，**阻止不了应用层不断往用户态 output buffer 追加**；高低水位把反压推回业务。验收上应看到缓冲/RSS 在设定预算处平台化（随后拒绝或踢连接），而不是持续线性上涨直至 OOM。

**题 3.** SYN queue 管半连接；accept queue 管已握手待 `accept`；应用输出缓冲管「已 accept、业务已 send、但套接字暂时发不出去」的用户态数据——三者满的症状与治理手段不同。拒绝率证明有界接入；p99 证明未饿死业务；恢复时间证明风暴可消退而非需重启。加大 backlog 可多扛一阵握手完成连接，但若应用 accept 慢或无上限，只是把压力延后到内存与事件循环；长连接网关提高最大连接数往往降低缓存命中、增加定时器与遍历成本，单请求效率下降——应按真实在线模型取舍，而非只追连接峰值。

---

## 推荐资源

1. [海量连接与接入治理：Linux man-pages](https://man7.org/linux/man-pages/man2/listen.2.html)  
   - 来源：man7.org｜语言：英文｜类型：文档｜难度：深入｜免费  
   - 重点阅读 **accept 预算和连接上限** 与 **每连接内存与输出水位**，用于核对本节点实验的语义和边界。

建议同时查阅本机 `man 2 listen`、`man 2 accept`、`man 7 tcp`、`man ss`，并对照 Muduo（或你使用的 Reactor）`Acceptor` / `TcpConnection` 源码路径，把内核队列与用户态缓冲边界画进笔记。

---

## 本课小结

- **海量连接**同时受 **fd、内存、内核队列、端口、业务活跃比例**约束；必须控制接入速率与每连接成本。
- **accept 预算 + 连接上限 + 明确拒绝**：保护事件循环公平，让服务在突发下可退化，而不是全接或全挂。
- **每连接内存与输出水位**：慢客户端靠应用层硬停；**达到内存预算后停止无界增长**。
- **SYN/accept 队列、超时、优雅关闭**：分清内核接入队列、应用队列、输出缓冲三边界；用超时与有预算的关闭回收半死连接。
- **误区**：只报连接数没有比较意义；抬 ulimit ≠ 治理完成。
- **权衡**：提高并发容量可能降低单请求效率，按真实流量模型取舍。
- **实践**：连接风暴 + 慢读压测；指标含拒绝率、p99、恢复时间；sourceStudy 对齐 Muduo 接入路径与 `listen(2)`。

---

## 配套练习建议

1. **默写三层上限表**：OS fd、内核 backlog、应用 maxConnections / accept 预算——各满时的症状与指标。
2. **做完两组压测**：连接风暴、慢读；保存拒绝率 / p99 / 恢复时间 / RSS 曲线。
3. **读 listen(2) + Muduo Acceptor**：在笔记里画「接入队列 / 应用队列 / 输出缓冲」三框，标出代码与 sysctl 落点。
4. **监控改造**：给现有接入层补上活跃比例与每连接内存；对比「只看连接数」的值班误判案例。
5. **值班演练**：给新人一个「连接数涨、p99 炸」案例，只允许先问活跃比例、每连接内存、ListenOverflows、拒绝率，禁止先抬 ulimit。

下一课继续在传输与 Socket / 接入治理脉络上推进；先确保你能独立讲清：何时该拒接、何时该靠水位踢慢客户端、以及为什么压测报告里不能没有拒绝率、p99 与恢复时间。
