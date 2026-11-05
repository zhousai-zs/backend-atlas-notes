# 第 55/208 课：epoll事件语义与工程边界

- **阶段**：第 3 阶段 · IO模型与事件
- **前置课程**：d-poll、c-fd
- **关键词**：epoll、epoll_create1、epoll_ctl、epoll_wait、EPOLLIN、EPOLLOUT、EPOLLET、EPOLLONESHOT、EPOLLRDHUP、EPOLLHUP、EPOLLERR、LT、ET、EAGAIN、EINTR、fd 复用、Reactor
- **难度**：进阶
- **所属领域**：D · IO模型与事件
- **标签**：Epoll、D、C++

---

## 导读

上一课我们用 `poll` 把「关注项」表达成一张 `struct pollfd` 数组：每轮仍要把整张表交给内核，内核再逐项检查并填 `revents`，用户态也要扫描返回结果。它消掉了 `select` 的 `FD_SETSIZE` 位图上限，但没有消掉「整表提交 + 线性扫描」的成本。

节点 summary 对 `epoll` 的定位是：

> epoll维护关注集合并返回就绪事件，LT、ET与ONESHOT影响重新通知和并发处理方式。

这句话拆开有三层意思：

1. **维护关注集合**：你把 fd 注册进内核兴趣列表（interest list），不必每轮把整张表再交一遍；
2. **只返回就绪事件**：`epoll_wait` 给你的是就绪列表（ready list），规模通常跟「本轮活跃数」相关，而不是跟「总连接数」相关；
3. **LT / ET / ONESHOT 改变重新通知与并发策略**：同样是「就绪」，不同触发模式决定「还会不会再次通知」「是否允许多个线程同时处理同一 fd」。

放到腾讯微信公众号后台的长连接/网关场景里，这几乎是必经之路：

- 接入层要同时盯住大量设备或上游的 TCP 长连接；
- 每秒真正有流量的连接往往远少于总连接数（稀疏活跃）；
- 单条连接上既有上行、又有下行积压、还有半关闭与错误；
- 事件循环必须把「就绪通知」和「真正读完/写完」严格分开，否则会丢数据、忙循环，或在 fd 复用时把旧事件套到新连接上。

本课要建立的不是「epoll 比 poll 更快」这种标签，而是一套可落地的工程语义：

1. `epoll_create1` / `epoll_ctl` / `epoll_wait` 各自做什么；
2. LT 与 ET 对「排空到 EAGAIN」的要求差在哪里；
3. `EPOLLONESHOT`、`EPOLLRDHUP`、fd 复用如何影响连接对象的生命周期；
4. 为什么「就绪 ≠ IO 完成」，以及 `EPOLLOUT` 为什么必须按输出积压按需启停。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 `create1` / `ctl` / `wait`**
   - 说清 `epoll_create1` 创建 epoll 实例（一个内核兴趣列表句柄）。
   - 说清 `epoll_ctl` 的 `ADD` / `MOD` / `DEL` 如何维护关注集合与 `events` 掩码。
   - 说清 `epoll_wait` 返回的是就绪事件数组，以及 `timeout`、`EINTR`、返回值语义。

2. **能独立完成：实现 ET Echo Server，覆盖短读、半关闭、错误、慢客户端和重新 arm**
   - 非阻塞 `accept` / `read` / `write`，循环处理 `EINTR` 与 `EAGAIN`。
   - 为连接维护输入、输出缓冲；半关闭时先冲完 outbuf 再关。
   - 比较 LT 与 ET；用 `EPOLLONESHOT` 演示一次「漏 rearm → 连接停滞」。

3. **满足本课验收标准（acceptance）**
   - 能解释 ET 为何通常配合非阻塞。
   - 关闭事件不会访问已释放连接。
   - 客户端分片发送、慢速读取、半关闭及连接复用压力下，无数据截断与 use-after-free。
   - 能用 strace 与日志说明一次事件丢失或忙循环原因；**不把就绪通知等同 IO 已完成**。

---

## 为什么后端要懂

节点里的理由是：

> 构建可处理大量连接的Linux Reactor基础。

### 1. 大规模长连接的「通知成本」必须和「活跃数」挂钩

`poll` 每轮提交并扫描整张关注表。连接数到十万、每秒只有几千活跃时，大量时间花在「确认这些 fd 仍然不就绪」上。`epoll` 把兴趣列表留在内核，返回列表按就绪项填充——这是 Reactor 能扛住稀疏活跃的基础。

### 2. 触发模式直接决定你会不会丢事件或忙循环

LT（水平触发）像 `poll`：条件仍成立就反复通知，漏处理一次通常还能被下一轮救回来，但写不好会空转占 CPU。  
ET（边缘触发）只在状态变化时通知：更省通知次数，但**要求你把可读/可写推进到 `EAGAIN`**，否则后续数据可能再也进不来。  
这些不是性能调优细节，而是正确性边界。

### 3. 连接对象生命周期必须和 epoll 注册一致

`EPOLLONESHOT`、fd 编号复用、半关闭、错误路径，都会把「内核里还有没有这条兴趣」「用户态对象还在不在」「旧事件会不会打到新连接」绑在一起。Muduo 一类 Reactor 的核心工作，很大一部分就是把 `Channel` 的注册、分发、注销和连接析构对齐。

### 4. 公众号后台网关就是典型 Reactor 场景

推送长连接、设备接入、上游代理：共同特征是「连接多、单连接状态机复杂、事件线程少」。不会 epoll 语义，就很难读懂、也很难安全地改这类事件循环。

---

## 直觉 → 原理

### 一、create1 / ctl / wait

#### 1. 三个系统调用各自的职责

```text
epoll_create1(flags)  →  epfd（兴趣列表句柄）
        │
        ├── epoll_ctl(epfd, ADD/MOD/DEL, fd, &event)
        │         维护「我关心谁、关心什么」
        │
        └── epoll_wait(epfd, events, maxevents, timeout)
                  取出「谁就绪了、发生了什么」
```

- **`epoll_create1`**：创建一个 epoll 实例。常用 `EPOLL_CLOEXEC`，避免 `fork`+`exec` 后子进程继承多余 fd。老接口 `epoll_create(size)` 的 `size` 在现代内核已被忽略，新代码优先 `create1`。
- **`epoll_ctl`**：对单个 fd 做 `EPOLL_CTL_ADD` / `EPOLL_CTL_MOD` / `EPOLL_CTL_DEL`。`struct epoll_event` 里既有 `events` 位掩码，也有 `data`（常放 fd 或指针）。
- **`epoll_wait`**：阻塞（或超时）等待，把就绪事件写入你提供的数组，返回就绪个数。`timeout` 单位是**毫秒**；`-1` 无限等，`0` 非阻塞探测。

#### 2. 最小 C 骨架

```c
#include <sys/epoll.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>

int epfd = epoll_create1(EPOLL_CLOEXEC);
if (epfd < 0) { /* perror */ }

struct epoll_event ev = {0};
ev.events = EPOLLIN;          /* 默认 LT */
ev.data.fd = listen_fd;
if (epoll_ctl(epfd, EPOLL_CTL_ADD, listen_fd, &ev) < 0) { /* ... */ }

struct epoll_event events[64];
for (;;) {
    int n = epoll_wait(epfd, events, 64, 1000);
    if (n < 0) {
        if (errno == EINTR) continue;
        break;
    }
    if (n == 0) {
        /* timeout：做心跳 / 定时任务 */
        continue;
    }
    for (int i = 0; i < n; ++i) {
        int fd = events[i].data.fd;
        uint32_t re = events[i].events;
        /* 按 re 分发：IN / OUT / RDHUP / HUP / ERR */
        (void)fd; (void)re;
    }
}
```

#### 3. Python 的 `select.epoll`

Linux 上 Python 提供了薄封装，语义与 C 一致，适合本课练习：

```python
import select

ep = select.epoll()
ep.register(listen_fd, select.EPOLLIN)          # LT
# ET：
# ep.register(fd, select.EPOLLIN | select.EPOLLET)
# ONESHOT：
# ep.register(fd, select.EPOLLIN | select.EPOLLONESHOT)

for fd, events in ep.poll(timeout=1.0):  # 秒；底层仍是毫秒
    if events & select.EPOLLIN:
        ...
    if events & (select.EPOLLHUP | select.EPOLLERR):
        ...
ep.unregister(fd)
ep.close()
```

注意：Python `epoll.poll(timeout)` 的 `timeout` 是**秒**（可浮点），和 C 的毫秒不同；`select.EPOLLET` / `EPOLLONESHOT` / `EPOLLRDHUP` 在 Linux 上可用。

#### 4. `data` 字段：fd 还是指针？

`epoll_event.data` 是一个 union，常见两种用法：

- 放 `fd`：简单，查找连接时再 `map[fd]`；
- 放指向 `Channel` / 连接对象的指针：少一次查找，但**必须保证对象在 epoll 注销前仍有效**，否则就是经典 use-after-free。

Muduo 选择「Channel 指针 + 严格生命周期」，正是为了把分发和析构对齐。教学代码用 `fd → Conn` 字典更直观，也更容易演示「关闭后立刻复用 fd 编号」的坑。

---

### 二、LT、ET 与排空到 EAGAIN

#### 1. 两种触发的直觉

| | LT（默认，Level Triggered） | ET（`EPOLLET`，Edge Triggered） |
|--|--|--|
| 通知条件 | 条件**仍然成立**就反复通知 | 条件**从不成到成立**（边沿）时通知 |
| 漏处理一次 | 通常下一轮还会再来 | 可能再也收不到，直到下一次边沿 |
| 典型要求 | 处理一部分即可（但仍建议排空） | **必须把该方向推进到 EAGAIN**（或 EOF/错误） |
| 与阻塞 fd | 勉强可用（易阻塞事件线程） | **几乎必须非阻塞**，否则一次 `read` 就可能卡住整条 Reactor |

节点要点原话：

> LT、ET与排空到EAGAIN

#### 2. 为什么 ET 通常必须非阻塞？

假设 fd 仍是阻塞的，ET 通知「可读」后你调用 `read`：

- 第一次 `read` 读走部分数据；
- 若内核缓冲里还有数据，阻塞 `read` 会**继续阻塞等待更多数据或对端动作**；
- 事件线程被卡住，其他成千上万连接的事件都得不到处理。

非阻塞下：`read`/`write` 在暂时不可继续时返回 `EAGAIN`/`EWOULDBLOCK`，你立刻回到 `epoll_wait`，把 CPU 让给别的就绪连接。这就是「ET 为何通常配合非阻塞」的验收点。

#### 3. 「排空」到底排到什么程度？

正确含义是：**在本轮事件处理中，把该方向的 I/O 推进到内核明确告诉你「暂时没有更多」**——通常是 `EAGAIN`，或者读方向的 `read()==0`（EOF），或者明确错误。

错误做法（节点 pitfalls 要打的）：

> 不能仅因一次read未填满缓冲就认定已排空。

```text
# 错：一次 read 没读满用户缓冲，就以为没数据了
n = read(fd, buf, 4096);
if (n < 4096) { /* 以为排空了 —— 错！可能只是短读 */ }

# 对：循环直到 EAGAIN / EOF / 错误
for (;;) {
    n = read(fd, buf, 4096);
    if (n > 0) { 消费 n 字节; continue; }
    if (n == 0) { 读 EOF; break; }
    if (errno == EINTR) continue;
    if (errno == EAGAIN || errno == EWOULDBLOCK) break; /* 真正暂时没了 */
    /* 其他错误 → 关闭 */
}
```

写方向同理：有 outbuf 时循环 `write`/`send`，直到缓冲写空或遇到 `EAGAIN`。

#### 4. LT 下「不排空」会怎样？

LT 不会轻易「丢事件」，但会**忙循环**：

- 接收缓冲里还有数据 → 每轮 `epoll_wait`（`timeout=0` 或很短）都立刻带着 `EPOLLIN` 返回；
- 你每次只读一点点，甚至读了却没消费完应用层状态；
- CPU 被事件循环占满，看起来像「epoll 很忙」，其实是自己没排空。

所以即便用 LT，生产级 Reactor 也几乎总是「非阻塞 + 读到 EAGAIN」。LT 的宽容是安全网，不是偷懒许可证。

#### 5. 监听套接字上的 accept

无论 LT/ET，listen fd 上的 `EPOLLIN` 表示「至少有一个待接受连接」。正确写法是**循环 `accept` 直到 `EAGAIN`**，否则在突发建连时可能积压在 accept 队列里，ET 下还可能漏接。

```python
while True:
    try:
        conn, addr = srv.accept()
    except BlockingIOError:
        break
    conn.setblocking(False)
    # register conn into epoll ...
```

---

### 三、ONESHOT、RDHUP 和 fd 复用

#### 1. `EPOLLONESHOT`：通知一次后自动解除关注

加上 `EPOLLONESHOT` 后，该 fd 被送达一次事件后，内核会把它从兴趣列表的有效关注中「卸下」——**必须再次 `epoll_ctl(MOD/ADD)` 才能继续收到事件**。这常用于：

- 多线程 Reactor：保证同一 fd 同一时刻只被一个工作线程处理；
- 处理完后再 arm，避免并发踩同一连接状态机。

代价也很直接：你若忘了 rearm，连接会**永久停滞**——对端还在发数据，你的 `epoll_wait` 却再也看不到它。本课 practice 会故意演示一次「漏 rearm」。

#### 2. `EPOLLRDHUP`：对端关闭了写方向

`EPOLLRDHUP`（Linux）表示对端关闭了连接的写半部（你这边读方向将看到 EOF）。它比泛泛的 `EPOLLHUP` 更贴近「半关闭」语义，便于：

- 继续把本地 outbuf 发完；
- 再 `shutdown(SHUT_WR)` / `close`。

处理时仍要遵守「先读尽再关」：`EPOLLIN|EPOLLRDHUP` 同时出现时，先循环读到 EOF/`EAGAIN`，再进入关闭流程。

#### 3. fd 复用：旧事件不能落到新连接

Linux 关闭 fd 后，编号很快会被 `accept`/`open` 复用。危险序列：

```text
1. 连接 A 使用 fd=7，注册进 epoll
2. 关闭 A，但忘记 epoll_ctl(DEL) 或忘记清掉 map[7]
3. 新连接 B accept 后也拿到 fd=7
4. 内核里残留的兴趣 / 用户态残留指针 → 把 B 的事件当 A 处理
   或对已释放的 Conn 做 use-after-free
```

硬性规则：

- `close(fd)` **之前或同时**，保证 `epoll_ctl(DEL)`（或依赖 `close` 自动从所有 epoll 集合移除——仍要清用户态映射）；
- `map[fd]` / `Channel*` 与 epoll 注册同生共死；
- 不要把「延迟删除队列」拖过 fd 可能被复用的窗口而不做世代号/指针校验。

节点要点：

> EPOLLRDHUP、EPOLLHUP、EPOLLERR 与 read()==0 的处理；fd 复用时避免把旧事件关联到新连接

---

### 四、就绪不是完成

节点原话：

> 就绪不是完成：事件后仍需处理 EAGAIN、EINTR、短读写；ET 读写状态通常推进到 EAGAIN

把这句话落成检查清单：

1. **`EPOLLIN` ≠ 读完了一条消息**  
   TCP 是字节流。你可能读到半包、多包、或一次 `read` 少于你的 buffer。协议边界自己拼。

2. **`EPOLLOUT` ≠ 一次 `write` 能写完 outbuf**  
   短写是常态。必须维护偏移/剩余长度，`EAGAIN` 时停，等下一次可写。

3. **`EINTR` 不是错误**  
   `epoll_wait` / `read` / `write` / `accept` 都可能被信号打断。正确动作是重试（并注意超时要按 deadline 重算）。

4. **ET 下「停在短读」等于丢边沿**  
   若你在 ET 下只读一次就回到 wait，而缓冲里还有数据，通常**不会**再有新的边沿，直到对端再次写入。于是连接「假死」。

5. **日志与 strace 要能证明「通知 ≠ 完成」**  
   一次 `epoll_wait` 返回后，你可能看到多次 `read`/`write`，中间夹杂 `EAGAIN`。若 strace 里每次通知只跟一次短读然后长期空闲，就要怀疑 ET 未排空。

---

### 五、EPOLLRDHUP / EPOLLHUP / EPOLLERR 与 `read()==0`

推荐处理顺序（与上一课 poll 一脉相承）：

```text
1. 查 EPOLLERR（必要时 getsockopt SO_ERROR）
2. 若有 EPOLLIN 或可读迹象：循环 read
      - >0：消费数据
      - =0：对端写方向结束（EOF）
      - EAGAIN：本轮读方向暂尽
      - 其他错误：进入关闭
3. 再看 EPOLLRDHUP / EPOLLHUP
4. 写方向：若 outbuf 非空且 EPOLLOUT，循环写到 EAGAIN 或写空
5. 若读已 EOF 且 outbuf 已空 → 安全关闭并 DEL + 释放 Conn
```

要点：

- **`read()==0`** 是字节流上「对端写关闭」的权威信号；
- **`EPOLLRDHUP`** 提前提示半关闭，但仍应通过 `read` 把残留读尽；
- **`EPOLLHUP`** 更常表示连接已不可用；仍优先保证不丢未读数据、不碰已释放对象；
- **`EPOLLERR`** 不要当成普通 `EPOLLIN` 去盲目 `read` 完事；记下 `SO_ERROR` 再关。

---

### 六、EPOLLOUT 按需启停；ONESHOT 与连接生命周期

#### 1. 为什么不能永远关注 `EPOLLOUT`？

套接字发送缓冲在多数空闲时间是「可写」的。若一直注册 `EPOLLOUT`：

- LT 下会**持续就绪** → `epoll_wait` 立刻返回 → CPU 空转；
- 即使 ET，错误的状态机也可能在每次写空后再次误 arm，造成无意义唤醒。

正确策略（节点要点）：

> EPOLLOUT 按输出积压按需启停

```text
outbuf 从空 → 非空：MOD 加上 EPOLLOUT
outbuf 写空：     MOD 去掉 EPOLLOUT（只留 EPOLLIN|RDHUP 等）
写遇到 EAGAIN：   保持 EPOLLOUT，等下一次可写
```

#### 2. ONESHOT 的 rearm 必须与对象存活一致

```text
工作线程拿到事件（ONESHOT 已自动卸关注）
    → 处理读写
    → 若连接仍存活：epoll_ctl(MOD, 重新带上需要的 events | ONESHOT)
    → 若连接应销毁：DEL + 释放对象（绝不再 MOD）
```

漏 rearm → 停滞；对已销毁对象 MOD → use-after-free 或 `EBADF`；先释放对象再处理残留事件 → 同理崩溃。把「重新注册」和「连接对象销毁」绑成同一生命周期，是 Reactor 的基本功。

#### 3. 与 Muduo 的对照（sourceStudy）

节点要求：

> 对照 Muduo EPollPoller::poll、updateChannel、EventLoop::loop 与 Channel::handleEvent；追踪一次 fd 注册、事件分发、注销和连接析构，不依赖未标注的内核版本细节。

阅读路径建议：

1. `EPollPoller::updateChannel`：如何把 `Channel` 的 `events_` 翻译成 `epoll_ctl`；
2. `EPollPoller::poll`：`epoll_wait` → 填充 active channels；
3. `EventLoop::loop`：调用 poller，再逐个 `Channel::handleEvent`；
4. 连接关闭路径：`Channel` disableAll / remove，与 `TcpConnection` 析构顺序。

你要自己在笔记里画一条时间线：**ADD → 就绪 → handleEvent → MOD/DEL → 对象析构**，标出哪一步之后指针不得再被使用。

---

## 常见误区与设计权衡

### 误区（pitfalls）

节点原话：

> 不能仅因一次read未填满缓冲就认定已排空。

展开成工程上常见的坑：

**1. 短读当排空（ET 致命）**  
一次 `read` 返回 `n < bufsize` 只表示「这次内核给了你 n 字节」，在 ET 下**不**等价于「已经 EAGAIN」。必须读到 `EAGAIN` 或 EOF。

**2. 把就绪通知当成 IO 完成**  
`EPOLLIN` 后仍可能 `EAGAIN`、短读、`EINTR`。协议层、缓冲层都要能继续。

**3. 永远注册 `EPOLLOUT`**  
导致 LT 忙循环。按 outbuf 积压启停。

**4. ET + 阻塞 fd**  
一次 `read`/`accept` 卡住整条事件线程。

**5. 忘记 ONESHOT rearm**  
连接无声停滞，难查。日志里应明确打印 `arm` / `rearm` / `skip_rearm`。

**6. close 后不清用户态映射**  
fd 复用 → 旧事件打到新连接，或 UAF。

**7. 见 `EPOLLHUP` 立即关，丢掉未读数据**  
与 poll 课相同：先读尽，再关。

**8. 监听 fd 不循环 accept**  
突发连接积压；ET 下可能漏接。

**9. 忽略 `EINTR`**  
信号多的服务里事件循环会「随机」退出或丢超时计算。

**10. 用 `EPOLLONESHOT` 却在多线程无锁改同一 `Conn`**  
ONESHOT 降低了「同 fd 同刻被两个线程 handle」的概率，但不自动保证你对共享结构的所有访问都安全。

### 权衡（tradeoffs）

节点原话：

> ET可减少重复通知，也要求严格状态管理与公平性预算。

| 维度 | LT | ET |
|------|----|----|
| 实现难度 | 较低，漏处理有机会被下一轮救回 | 较高，必须排空到 EAGAIN，状态机要完整 |
| 通知次数 | 条件成立时可能反复通知 | 边沿通知，通常更少 |
| CPU | 未排空时易空转 | 排空正确时更省唤醒 |
| 公平性 | 仍需应用层限制单连接本轮读写量，避免饿死其他连接 | 同样需要「公平性预算」：单连接本轮最多读/写 N 次或 M 字节，再到下一轮 |
| 适用 | 语义简单、先求正确 | 高连接密度、想减少重复通知，且团队吃得准状态机 |

实战建议：

- 先用 **非阻塞 + LT + 读到 EAGAIN** 把正确性做稳；
- 再在压测下切 ET，用日志/strace 证明没有漏排空、没有假死；
- 无论 LT/ET，都给单连接设置本轮 I/O 预算，避免一个热点 fd 饿死整个 Reactor（这就是 tradeoffs 里的「公平性预算」）。

---

## 动手实践

节点 practice：

> 实现 C++17 非阻塞多客户端 Echo Server：accept/read/write 循环处理 EINTR 和 EAGAIN，为连接维护输入、输出缓冲；增加半关闭、慢读客户端与超大消息测试。比较 LT 与 ET，记录 fd 数、CPU、吞吐及 p99；最后以 EPOLLONESHOT 演示一次遗漏 rearm 的停滞故障。

验收（acceptance）：

- 能解释 ET 为何通常配合非阻塞；
- 关闭事件不会访问已释放连接；
- 客户端分片发送、慢速读取、半关闭及连接复用压力下无数据截断与 use-after-free；
- 能用 strace 与日志说明一次事件丢失或忙循环原因；不把就绪通知等同 IO 已完成。

并呼应 sourceStudy：对照 Muduo `EPollPoller::poll`、`updateChannel`、`EventLoop::loop` 与 `Channel::handleEvent`，追踪一次 fd 注册、事件分发、注销和连接析构。

本课配套练习用 Python `select.epoll` 实现同等语义（ET Echo、短读到 EAGAIN、半关闭、慢客户端、按需 EPOLLOUT、ONESHOT 漏 rearm、EADDRINUSE），便于在无编译器依赖的环境里跑通验收。

### 步骤拆解

1. **创建 epoll 与监听套接字**  
   `socket` + `SO_REUSEADDR` + 非阻塞 + `listen`。  
   `epoll_create1` / `select.epoll()`，listen fd 注册 `EPOLLIN`（可先 LT，再加 ET）。

2. **连接对象**  
   每条连接：`sock`、`inbuf`/`outbuf`、读是否 EOF、是否已关闭、oneshot 模式标记。  
   `fd → Conn` 字典；关闭路径统一：`DEL/unregister` → `close` → `dict.pop`。

3. **accept 循环**  
   listen 可读时 `accept` 直到 `EAGAIN`；新连接设非阻塞，注册 `EPOLLIN|EPOLLRDHUP`（ET 则或上 `EPOLLET`；演示 ONESHOT 则或上 `EPOLLONESHOT`）。

4. **读路径（ET 核心）**  
   循环 `recv` 直到 `EAGAIN` 或 0。  
   数据追加到 outbuf（Echo）或先入 inbuf 再按行回显。  
   **不要**因为一次 `recv` 未填满缓冲就停止。  
   读到 0：标记 peer EOF；若 outbuf 空则关闭。

5. **写路径与 EPOLLOUT**  
   outbuf 非空时 `MOD` 加上 `EPOLLOUT`；循环 `send` 到写空或 `EAGAIN`。  
   写空后去掉 `EPOLLOUT`。  
   可用较小的 `SO_SNDBUF` + 慢读客户端稳定触发 `EAGAIN`。

6. **半关闭**  
   客户端 `send` 后 `shutdown(SHUT_WR)`：服务端应读尽、回显、再关。  
   日志打印 `RDHUP`/`EOF`/`drained` 字节数。

7. **ONESHOT 漏 rearm 故障演示**  
   提供开关：处理完读事件故意不 `MOD` rearm。  
   预期：后续客户端数据再也进不来，日志可看到「已停滞」。  
   对照正确路径：每次处理完若连接仍存活则 rearm。

8. **比较 LT / ET（可选记录）**  
   同一负载下记录：fd 数、CPU、吞吐、p99。  
   重点不是刷分，而是解释差异是否来自「重复通知」或「未排空导致的假死」。

9. **strace / 日志**  
   对服务进程 `strace -e epoll_wait,recvfrom,sendto,accept4`（环境若无 strace 则用详细日志）。  
   证明：一次就绪后有多轮短读直到 `EAGAIN`；漏 rearm 时不再出现该 fd 的事件。

10. **对照 man page 与 Muduo**  
    阅读 [epoll(7)](https://man7.org/linux/man-pages/man7/epoll.7.html) 的 LT/ET/ONESHOT 段落；打开 Muduo `EPollPoller.cc` 对照 `update`/`poll`。

### 预期现象

- 多客户端同时 echo，分片发送最终拼回完整 payload；
- 慢读客户端不导致服务端丢数据或忙自旋；
- 半关闭：服务端先读尽并回显，再关闭；
- ET 下日志出现多次 `RECV` 并以 `EAGAIN` 结束本轮；
- 故意漏 rearm 后，该连接后续数据不再被处理（停滞故障）；
- 二次 bind 同端口 → `EADDRINUSE`，退出码 2；
- 关闭连接后无 UAF、无把旧 fd 事件套到新连接。

### 验收清单

```text
[ ] 能口头解释：ET 为什么必须非阻塞 + 读/写到 EAGAIN
[ ] 短读不等于排空；代码以 EAGAIN/EOF 为停条件
[ ] EPOLLOUT 仅在 outbuf 非空时关注
[ ] EPOLLRDHUP/HUP/ERR 与 read()==0 路径不访问已释放 Conn
[ ] ONESHOT 漏 rearm 可复现停滞；修复后恢复
[ ] EADDRINUSE 有明确错误与退出码
[ ] 对照过 epoll(7) 与 Muduo EPollPoller 的注册/分发/注销
```

---

## 自测题

**题 1**  
在 ET（`EPOLLET`）模式下，一次 `EPOLLIN` 之后执行了一次 `read`，返回值 `n` 大于 0 且 `n < 用户buffer大小`。此时最稳妥的下一步是？

A. 认定已经排空，回到 `epoll_wait`  
B. 继续循环 `read`，直到 `EAGAIN`/`EWOULDBLOCK` 或 EOF/错误  
C. 立刻 `close`，因为短读表示对端不会再发  
D. 改为阻塞 `read` 把剩余数据读完

**题 2**  
为什么说「就绪不是完成」？下列哪一项正确？

A. `epoll_wait` 返回时，对应的 `read`/`write` 一定已经在内核完成  
B. `EPOLLIN` 只表示现在读大概率不阻塞，仍可能短读、遇 `EAGAIN`/`EINTR`，应用必须自己推进状态  
C. 只要注册了 `EPOLLIN`，TCP 消息边界就会自动对齐  
D. `EPOLLOUT` 保证一次 `write` 能写完整个 outbuf

**题 3**  
对某 fd 使用 `EPOLLONESHOT`，处理完事件后忘记 `epoll_ctl(MOD)` rearm。最可能的现象是？

A. 该 fd 会在 LT 语义下不断通知，导致 CPU 空转  
B. 该 fd 后续事件不再送达，连接表现为停滞，直到再次被正确 arm  
C. 内核自动把该 fd 改为 ET  
D. `close(fd)` 会被内核禁止，直到 rearm

---

### 自测题参考答案

**题 1：B**  
节点 pitfalls 明确：不能仅因一次 read 未填满缓冲就认定已排空。ET 下必须把读方向推进到 `EAGAIN` 或 EOF/错误，否则可能失去后续边沿通知。A 是典型事故；C/D 更错。

**题 2：B**  
就绪通知只说明「现在可以尝试 IO」，不保证一次做完，也不保证消息边界。短读、`EAGAIN`、`EINTR` 都要处理。A/C/D 都把通知误当成完成。

**题 3：B**  
`EPOLLONESHOT` 送达一次后会卸下有效关注，必须主动 rearm。忘记 rearm 的经典故障就是连接停滞。A 描述的是「永远关注 EPOLLOUT/未排空」类问题，不是 ONESHOT 漏 arm。

---

## 推荐资源

- [epoll事件语义与工程边界：Linux man-pages](https://man7.org/linux/man-pages/man7/epoll.7.html)（man7.org，英文文档，进阶）
  - 重点阅读：`epoll_create1` / `epoll_ctl` / `epoll_wait` 与 **LT、ET 与排空到 EAGAIN**，用于核对本节点实验的语义和边界。
  - 同时核对 `EPOLLONESHOT`、`EPOLLRDHUP`、`EPOLLERR`/`EPOLLHUP` 的说明，记录与你实验环境一致的行为。

- [Muduo · EPollPoller 实现](https://github.com/chenshuo/muduo/blob/master/muduo/net/poller/EPollPoller.cc)（chenshuo/muduo，英文源码，进阶）
  - 追踪 Poller 如何注册 fd、转译事件并把活动 Channel 交给事件循环。
  - 结合 `EventLoop::loop` 与 `Channel::handleEvent`，走通「注册 → 分发 → 注销 → 连接析构」整条链，避免只看局部 API。

---

## 本课小结

1. **`epoll` 维护内核兴趣列表，只返回就绪事件**：`create1` 建实例，`ctl` 增删改关注，`wait` 取就绪；这是海量连接下 Reactor 的基础，对比 `poll` 每轮整表提交扫描。
2. **LT / ET / ONESHOT 改变的是重新通知与并发策略**：ET 必须配合非阻塞，并把读写推进到 `EAGAIN`；ONESHOT 必须显式 rearm，且与连接对象同生命周期。
3. **就绪不是完成**：事件后仍要处理短读写、`EAGAIN`、`EINTR`；不能把一次未填满缓冲的 `read` 当成排空。
4. **关闭与错误路径要先读尽、再释放**：`EPOLLRDHUP`/`HUP`/`ERR` 与 `read()==0` 协同；`close` 与 `DEL`、用户态 map 一致，防止 fd 复用误关联与 UAF。
5. **`EPOLLOUT` 按输出积压按需启停**：避免 LT 下可写空转；慢客户端用 outbuf + 状态机消化背压。
6. **权衡**：ET 可减少重复通知，但要求严格状态管理与公平性预算；先正确，再谈切 ET 与刷指标。
7. **对照 Muduo**：用 `EPollPoller` + `EventLoop` + `Channel` 把注册、分发、注销、析构看成一条链，而不是四个孤立 API。

后续课程会继续 IO 模型与事件主题；请先把本课 ET Echo 与「ONESHOT 漏 rearm」两个实验跑懂，再进入后续内容。

---

## 配套练习

目录：[`practice55/`](./practice55/)

| 文件 | 作用 |
|------|------|
| `server.py` | `select.epoll` ET Echo；短读循环到 `EAGAIN`；半关闭 EOF；按需 `EPOLLOUT`；`--oneshot --leak-rearm` 演示漏 rearm 停滞 |
| `client.py` | 分片发送、慢读、`shutdown(WR)` 半关闭、期望回显 |
| `test.sh` | 自动化验收：分片 echo、`RECV_EAGAIN`、半关闭、慢读、IDLE、ONESHOT 漏 rearm、EADDRINUSE |

运行：

```bash
cd practice55 && ./test.sh
```

期望末行：`ALL_CHECKS_PASSED`。

实测要点（Asia/Shanghai，本机验证时对照）：

- 分片发送后客户端收到完整 payload；
- 服务日志出现 `RECV_EAGAIN`（ET 排空到 EAGAIN）；
- 客户端 `shutdown(WR)` 后仍完整回显，服务端有 `EOF`/`CLOSE`；
- 慢读客户端无截断；
- 空闲出现 `IDLE heartbeat`；
- `--leak-rearm` 下首次事件后打印 `LEAK_REARM`，后续数据不再回显（`STALL_OK`）；
- 二次绑定同端口打印 `EADDRINUSE`，退出码 2。

NOTE：Python `select.epoll` 映射 `EPOLLIN/EPOLLOUT/EPOLLET/EPOLLONESHOT/EPOLLRDHUP/EPOLLHUP/EPOLLERR`。生产代码切勿故意漏 rearm；本练习仅用于复现 ONESHOT 停滞故障。C++17 版本可按节点 practice 自行扩展，并对照 Muduo `EPollPoller`。
