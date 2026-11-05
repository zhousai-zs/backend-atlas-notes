# 第 54/208 课：poll与就绪集合扫描

- **阶段**：第 2 阶段 · IO模型与事件
- **前置课程**：d-select
- **关键词**：poll、pollfd、events、revents、POLLIN、POLLOUT、POLLHUP、POLLERR、POLLNVAL、timeout、超时、nfds、多路复用、描述符数组、水平触发
- **难度**：基础
- **所属领域**：D · IO模型与事件
- **标签**：Poll、D、C++

---

## 导读

上一课我们用 `select` 把「一大批连接」交给内核，一条线程等待「其中至少一个就绪」。`select` 的心智模型是**三张 fd 位图**：

```text
readfds / writefds / exceptfds
        │
        ├── 按 fd 编号寻址（位图）
        ├── nfds = max_fd + 1
        └── 受 FD_SETSIZE（Linux 常见 1024）硬限制
```

它还留下两个让人难受的约束：

1. **fd 编号不能太大**：默认 `fd_set` 管不了编号 ≥ `FD_SETSIZE` 的描述符；
2. **每次返回后集合被改写成「就绪子集」**，下一轮必须整套重建。

本课换成另一种多路复用接口——`poll`。节点 summary 把它说得很清楚：

> poll使用描述符数组消除fd_set固定集合形式，但每轮仍提交并扫描关注项。

这句话有两个重点：

- **描述符数组**：你不再维护一张按 fd 编号寻址的位图，而是维护一个 `struct pollfd` 数组。数组里每一项显式写出「哪个 fd、关心什么事件、实际发生了什么事件」。
- **每轮仍提交并扫描**：`poll` 不是「把关注项注册进内核后就不管了」。每次调用仍要把整个数组交给内核，内核仍要逐项检查并填 `revents`；用户态也仍要扫描返回结果。它消除了 `fd_set` 的固定集合形式，但没有消除线性扫描成本。

放到腾讯微信公众号后台的长连接接入场景里，这仍然是同一类问题：

- 网关要同时盯住大量设备的 TCP 长连接；
- 每条连接随时可能有上行数据、可写空间、对端关闭或错误；
- 不能为每条连接开一条阻塞线程，而要用少量事件线程轮转处理。

`poll` 是这条演进路线上的中间站：比 `select` 更直白、没有 `FD_SETSIZE` 位图上限；比 `epoll` 更简单、更可移植，但要自己管理数组并承担整表扫描。本课要弄清三件事：

1. `pollfd` 的 `events` 与 `revents` 到底谁进谁出；
2. `POLLIN` / `POLLHUP` / `POLLERR` / `POLLNVAL` 同时出现时按什么顺序处理；
3. 数组如何增删、超时如何设置，才能写出不丢数据、不空转的事件循环。

---

## 学习目标

完成本课学习后，你应当能够：

1. **解释 `pollfd` 和 `revents`**
   - 说清 `fd`、`events`、`revents` 三个字段各自的职责。
   - 知道 `events` 是「我关心什么」的输入位掩码，`revents` 是「实际发生了什么」的输出位掩码。
   - 知道 `poll` **不会**像 `select` 那样把输入集合改写成结果集合；但数组的增删仍要你自己维护。
   - 知道 `nfds` 是**数组元素个数**，不是 `max_fd + 1`。

2. **能独立完成实践：把 `select` 版本替换为 `poll`**
   - 用 `poll` 同时监听 listen fd 与多个 client fd。
   - 用 `select.poll()` 的 `register` / `modify` / `unregister`，或用 C 的 `struct pollfd` 数组。
   - 测试「数据与 HUP 同时返回」：对端先发数据再关闭，服务端必须先读完缓冲中的数据，再处理 HUP。
   - 构造并验证 `POLLNVAL`：注册的 fd 已失效时，不做 `recv`/`send`，正确清理并移出数组。

3. **满足本课验收标准**
   - **HUP 时仍能读取缓冲中的剩余数据**。
   - **正确处理 `POLLNVAL`**。
   - 能说明 `poll` 为什么「没有 `FD_SETSIZE` 上限，但仍不适合大集合稀疏活跃」的场景。

---

## 为什么后端要懂

节点里的理由是：

> 比较多路复用接口的语义而非只记复杂度标签。

### 1. 「O(n)」这个标签会掩盖真正的差异

你可能背过一张表：

| 接口 | 复杂度标签 |
|------|------------|
| `select` | O(n) |
| `poll` | O(n) |
| `epoll` | O(活跃数) |

但工程上真正决定你写不写得对的，不是这三个字母，而是**语义**：

- `select` 的 `n` 是「最大 fd 编号」量级，因为你按编号扫位图；
- `poll` 的 `n` 是「数组元素个数」，与 fd 编号大小无关；
- `epoll` 的 `n` 是「本次真正就绪的条目数」，关注项由内核持久保存。

同样是「O(n)」，`select` 和 `poll` 的 `n` 含义不同，容量瓶颈也不同。`poll` 可以轻松管理编号 10000 的 fd，只要数组里只有 3 项；`select` 则连 `FD_SET(10000, ...)` 都不该做。

### 2. 就绪事件的语义直接决定数据会不会丢

节点 pitfalls 只有一句话，但它是生产事故级的问题：

> HUP不意味着可以直接丢弃未读数据。

连接对端关闭时，内核可能已经把「对端发来的最后一段数据」放进了接收缓冲，同时报告 `POLLHUP`。如果你看到 HUP 就立刻 `close`，这段数据就永远读不到了。公众号后台的上行事件、设备最后一条状态、审计日志，都可能是这样丢的。

`select` 课里我们用「可读 + `recv` 返回 0」处理 EOF；到了 `poll`，你必须理解 **HUP 是通道挂断信号，不是「缓冲已空」信号**。后面的 `epoll` 课还会反复出现同样的语义。

### 3. 数组管理是 `poll` 的主要工程成本

`poll` 没有 `FD_SETSIZE` 位图限制，但它把「注册表」交给了用户态：

- 新连接进来，你要往数组里加一项；
- 连接关闭，你要把对应项移除或标记为 `fd = -1`；
- 关注事件变化（比如输出缓冲从空变为非空），你要更新 `events`；
- 每次调用还要把数组整体传给内核。

如果数组里残留了已关闭的 fd，轻则收到 `POLLNVAL`，重则该 fd 编号被系统复用给新连接，你的事件循环就会把「旧连接的条目」当成「新连接」处理。这类 bug 很难靠日志一眼看出。

### 4. 它是理解 `epoll` 的对照物

生产 Linux 网关大多最终会用 `epoll`。但 `epoll` 的很多设计，正是为了修掉 `poll` 的这些问题：

- `poll` 每次提交整张数组 → `epoll_ctl` 只注册一次；
- `poll` 内核每次扫描全部条目 → `epoll_wait` 直接返回就绪链表；
- `poll` 只有水平触发 → `epoll` 支持边缘触发；
- `poll` 的 fd 失效要靠 `POLLNVAL` 发现 → `epoll` 有 `EPOLL_CTL_DEL` 与自动清理规则。

先把 `poll` 的语义吃透，后面才不会把 `epoll` 当成「更快的 poll」死记。

---

## 直觉 → 原理

### 一、pollfd 和 revents

#### 1. 接口长什么样

C 原型（见 `poll(2)`）：

```c
#include <poll.h>

int poll(struct pollfd *fds, nfds_t nfds, int timeout);

struct pollfd {
    int   fd;       /* 要关注的描述符 */
    short events;   /* 输入：我关心哪些事件 */
    short revents;  /* 输出：内核告诉我实际发生了哪些事件 */
};
```

和 `select` 最大的不同：

- `select` 用**位图**表达「关注集合」，fd 编号就是位下标；
- `poll` 用**数组**表达「关注列表」，每一项显式带一个 fd。

直觉上可以把它想成一张「问询表」：

```text
你填：  fd=7  events=POLLIN     ← 我想知道 7 号可不可读
内核填：fd=7  revents=POLLIN    ← 7 号现在可读

你填：  fd=9  events=POLLOUT    ← 我想知道 9 号可不可写
内核填：fd=9  revents=0         ← 9 号这次没事件
```

`events` 和 `revents` 分离，是 `poll` 比 `select` 好用的地方：**输入不会被结果覆盖**。你在下一轮仍然知道「我原本关心的是什么」，不必每轮重新构造关注集合。但这不等于数组永远不用维护——连接增删、关注事件变化时，仍要你手动改数组。

#### 2. nfds 是数组长度，不是 max_fd + 1

这是从 `select` 迁移过来最容易写错的地方：

```text
select: nfds = 所有关心 fd 中的最大编号 + 1
poll:   nfds = fds 数组中的元素个数
```

例子：

| 关注方式 | 数组内容 | `poll` 的 `nfds` |
|----------|----------|------------------|
| 3 个连接，fd 为 3、5、900 | `[{3,...},{5,...},{900,...}]` | `3` |
| 2 个连接 + 1 个占位空项 | `[{3,...},{-1,...},{900,...}]` | `3` |
| 关闭 900 后只留 3、5 | `[{3,...},{5,...}]` | `2` |

`nfds` 的类型是 `nfds_t`，本质是无符号计数。内核只检查数组的前 `nfds` 项，和 fd 的数值大小无关。所以 `poll` 不受 `FD_SETSIZE` 的位图上限约束。

#### 3. fd 为负数：官方的「空位」写法

`poll(2)` 明确允许：

> If the `fd` field is negative, then the `events` field is ignored and the `revents` field is set to zero.

这给你两种数组管理方式：

**方式 A：紧凑数组**

只把存活连接放进数组，删除时用「与末尾交换再缩短」：

```c
fds[i] = fds[nfds - 1];
nfds--;
```

优点是没有空位、`nfds` 始终等于存活连接数；缺点是要维护 `fd -> 下标` 的映射，否则删除时找不到 `i`。

**方式 B：固定槽位**

数组下标对应连接槽位，删除时把 `fd` 置为 `-1`：

```c
fds[i].fd = -1;
fds[i].events = 0;
fds[i].revents = 0;
```

优点是槽位稳定、实现简单；缺点是 `nfds` 可能包含很多空位，内核仍会扫描这些项。对长连接网关来说，如果连接频繁上下线，空位会越来越多，需要定期压缩，否则扫描成本会虚高。

#### 4. 最小 C 骨架

下面只演示「listen + 一个 client」的结构，重点看数组和 `revents`：

```c
#include <poll.h>
#include <errno.h>

struct pollfd fds[2];
nfds_t nfds = 0;

fds[nfds++] = (struct pollfd){ .fd = listen_fd, .events = POLLIN, .revents = 0 };
fds[nfds++] = (struct pollfd){ .fd = client_fd, .events = POLLIN, .revents = 0 };

for (;;) {
    int n = poll(fds, nfds, 2000);   /* 最多等 2000 ms */
    if (n < 0) {
        if (errno == EINTR) continue; /* 被信号打断：重试 */
        perror("poll");
        break;
    }
    if (n == 0) {
        /* 超时：做心跳、空闲连接清扫 */
        continue;
    }

    for (nfds_t i = 0; i < nfds; i++) {
        short re = fds[i].revents;
        if (re == 0) continue;

        if (fds[i].fd == listen_fd) {
            if (re & POLLIN) {
                /* accept 新连接，追加到数组（下一轮生效） */
            }
            continue;
        }

        if (fds[i].fd == client_fd) {
            if (re & POLLNVAL) {
                /* fd 无效：不要 recv/send，直接清理条目 */
                continue;
            }
            if (re & POLLIN) {
                /* 读数据；注意可能读到 EOF */
            }
            if (re & (POLLHUP | POLLERR)) {
                /* 先尽量读完剩余数据，再关闭 */
            }
        }
    }
}
```

真实工程不会用固定两个元素的数组，而是 `std::vector<pollfd>` 或动态分配；但语义一模一样。

#### 5. Python 的 `select.poll`

Python 在 Unix 上提供了薄封装：

```python
import select

poller = select.poll()
poller.register(listen_fd, select.POLLIN)

while True:
    events = poller.poll(2000)     # 超时单位是毫秒
    if not events:
        print("IDLE heartbeat")
        continue

    for fd, revents in events:     # 只返回 revents 非零的项
        if fd == listen_fd:
            if revents & select.POLLIN:
                conn, addr = listen_sock.accept()
                poller.register(conn.fileno(), select.POLLIN)
        else:
            handle_client(fd, revents)
```

常用方法：

| 方法 | 作用 |
|------|------|
| `register(fd, eventmask)` | 注册 fd 与关注事件；未传掩码时默认 `POLLIN | POLLPRI | POLLOUT` |
| `modify(fd, eventmask)` | 修改关注事件 |
| `unregister(fd)` | 移除 fd；未注册时抛 `KeyError` |
| `poll(timeout=None)` | 等待事件，返回 `(fd, revents)` 列表；`None` 表示永久阻塞，单位毫秒 |

注意：`select.poll` 是 **Unix 专有**，Windows 上没有。跨平台代码要另做适配。

C++ 里通常直接使用系统 `poll`：

```cpp
#include <poll.h>
#include <vector>

std::vector<struct pollfd> fds;
fds.push_back({listen_fd, POLLIN, 0});

int n = ::poll(fds.data(), fds.size(), 2000);
if (n > 0) {
    for (const auto& pfd : fds) {
        if (pfd.revents == 0) continue;
        /* ... */
    }
}
```

注意 `fds.push_back` 可能触发扩容，导致之前的指针/迭代器失效；如果内核调用与异步回调交错，要避免持有旧指针。

---

### 二、POLLIN / HUP / ERR

#### 1. 常见事件位

`events` 和 `revents` 都是位掩码。下面是最常打交道的几位：

| 位 | 含义 | 可放入 `events`？ | 常见场景 |
|----|------|-------------------|----------|
| `POLLIN` | 有数据可读（普通优先级） | 是 | 对端发来数据；监听 fd 有新连接；读到 EOF |
| `POLLOUT` | 现在可以写 | 是 | 发送缓冲有空间；非阻塞 connect 完成 |
| `POLLPRI` | 有紧急/带外数据可读 | 是 | TCP urgent data 等 |
| `POLLRDHUP` | 对端关闭了写方向（半关闭） | 是（Linux 专有） | 对端 `shutdown(SHUT_WR)` |
| `POLLHUP` | 通道挂断 | **否**，只在 `revents` 出现 | 对端完全关闭；管道写端关闭 |
| `POLLERR` | 发生错误条件 | **否**，只在 `revents` 出现 | 套接字错误等 |
| `POLLNVAL` | fd 未打开/无效 | **否**，只在 `revents` 出现 | 数组里残留了已关闭的 fd |

`POLLHUP`、`POLLERR`、`POLLNVAL` 是**输出专用位**。把它们写进 `events` 没有意义；只要对应条件成立，内核就会在 `revents` 里置位，哪怕你的 `events` 是 0。

还有一个容易忽略的点：

> `poll` 返回的是「就绪」，不是「读完了」。

`POLLIN` 只说明「现在调用 `read` 不会阻塞（至少能读到一点，或已经 EOF）」，不说明「一次能读到完整消息」，也不说明「下次还会不会可读」。它默认是**水平触发**：只要接收缓冲里还有未读数据，下一次 `poll` 还会报告 `POLLIN`。所以正确的读法是循环读到 `EAGAIN`（非阻塞）或 EOF（返回 0），或者按应用层消息边界读够为止。

#### 2. 监听套接字上的 POLLIN

对监听 fd 来说，`POLLIN` 表示「有新的连接可以 `accept`」。和上一课一样：

- listen fd 设为非阻塞；
- 在 `POLLIN` 分支里循环 `accept`，直到返回 `EAGAIN`；
- 每个新 fd 注册进 `poll` 数组，关注 `POLLIN`；
- 如果暂时不想收某条连接的读事件，可以用 `modify` 把 `events` 改成 0 或只留 `POLLOUT`。

#### 3. 数据与 HUP 同时返回

这是本课实践的核心。典型时序：

```text
客户端：send("last-event")  →  close()
                    │
                    ▼
服务端内核接收缓冲：["last-event"]，同时通道挂断
                    │
                    ▼
poll 返回：revents = POLLIN | POLLHUP
```

此时如果你先处理 HUP：

```python
if revents & select.POLLHUP:
    close(fd)          # 错：缓冲里的 "last-event" 还没读
```

就会丢数据。正确顺序是：

1. **先处理 `POLLNVAL`**：fd 已经无效，任何 I/O 都不做，直接清理。
2. **再处理 `POLLIN`**：循环读到 `EAGAIN` 或 EOF，把缓冲里的数据全部取出并交给业务。
3. **最后处理 `POLLHUP` / `POLLERR`**：确认数据已读完（或确认错误不可恢复）后，再关闭并移出数组。

`poll(2)` 对 HUP 的说明正好支持这一点：

> 当从管道或流式套接字读取时，`POLLHUP` 只表示对端关闭了通道；**只有通道中所有未完成的数据都被消费之后**，后续读取才会返回 0（EOF）。

也就是说，HUP 和「缓冲为空」不是一回事。先读后关，是必须遵守的顺序。

可以用一段 Python 复现「数据 + HUP」：

```python
import select
import socket

left, right = socket.socketpair()
right.sendall(b"payload-before-hup")
right.close()                    # 对端关闭，但数据仍在 left 的接收缓冲中

poller = select.poll()
poller.register(left, select.POLLIN)

fd, revents = poller.poll(0)[0]
print("revents =", revents)
print("POLLIN =", bool(revents & select.POLLIN),
      "POLLHUP =", bool(revents & select.POLLHUP))

print("first recv =", left.recv(1024))   # b'payload-before-hup'
print("second recv =", left.recv(1024))  # b''，此时才真正 EOF
print("after EOF =", poller.poll(0))     # HUP 仍会持续报告
```

在 Linux 上你通常会看到 `POLLIN | POLLHUP` 同时置位，而且第一次 `recv` 能拿到完整 payload。这个实验建议亲手跑一遍，比背十遍结论都有效。

#### 4. POLLERR 不是「可读」的同义词

`POLLERR` 表示该 fd 上出现了错误条件。处理原则：

- 不要仅凭 `POLLERR` 就认定「没有数据可读」；
- 可以先尝试把可读数据读出来（如果同时有 `POLLIN`）；
- 再通过 `getsockopt(fd, SOL_SOCKET, SO_ERROR, ...)` 或 `recv`/`send` 的返回值确认错误；
- 错误不可恢复时，关闭连接并移出数组。

`POLLERR` 和 `POLLHUP` 都可能在 `events` 未请求时出现，所以你的循环不能只检查自己注册过的位。

#### 5. POLLNVAL：fd 已经无效

`POLLNVAL` 表示 `fds[i].fd` 当前不是一个打开的描述符。常见原因：

- 连接已经 `close`，但忘记从 `poll` 数组/`poll` 对象中移除；
- 数组槽位被重复关闭；
- 文件描述符被别的逻辑提前关闭。

正确处理：

```python
if revents & select.POLLNVAL:
    # 1. 不要对这个 fd 调用 recv/send/close
    # 2. 从 poller 中移除（若还在）
    try:
        poller.unregister(fd)
    except KeyError:
        pass
    # 3. 清理应用层连接对象
    clients.pop(fd, None)
    continue
```

两个关键细节：

1. **`POLLNVAL` 不是「连接错误」**，它只说明「这个 fd 编号现在无效」。不要把它当成 `POLLERR` 去读 `SO_ERROR`。
2. **不要依赖 `POLLNVAL` 来发现你自己关闭的 fd**。你 `close(fd)` 之后，这个编号可能立刻被新 `accept`/`open` 复用；如果旧条目还留在数组里，`poll` 会把它当成新 fd 来报告，而不是报 `POLLNVAL`。所以正确做法永远是：**关闭前/关闭时就把条目移出数组**，`POLLNVAL` 只是最后一道防线。

#### 6. 处理顺序建议

把每个条目的处理写成一个明确的分支顺序，能避免大多数丢数据和误关闭：

```text
revents 非零
    │
    ├─ POLLNVAL ───────────────▶ 清理条目，绝不做 I/O
    │
    ├─ POLLIN ─────────────────▶ 循环读，直到 EAGAIN 或 EOF
    │                              └─ 有数据则交给业务/广播
    │
    ├─ POLLOUT ────────────────▶ 从输出缓冲继续写，直到 EAGAIN
    │
    └─ POLLHUP / POLLERR ──────▶ 先确认剩余数据已读（或错误不可恢复）
                                   └─ 关闭连接，移出数组
```

注意：这些位可能同时出现。不要写成 `else if` 链，除非你非常确定某个分支必须互斥。

---

### 三、数组管理与超时

#### 1. poll 是「每轮提交整张表」

`select` 和 `poll` 都属于「无状态」的多路复用调用：

```text
用户态数组/集合 ──每次调用拷贝──▶ 内核扫描 ──▶ 填 revents/就绪集合 ──▶ 返回
```

内核不会帮你记住「上次注册了哪些 fd」。所以：

- 新增连接 → 往数组里加一项；
- 删除连接 → 从数组里移除，或置 `fd = -1`；
- 关注事件变化 → 修改 `events`；
- 每轮调用 → 把整个数组和 `nfds` 再传一次。

这就是 summary 里「每轮仍提交并扫描关注项」的含义。`poll` 解决的是**表达形式**问题（数组 vs 位图），不是**注册与通知**问题（那是 `epoll` 的路线）。

#### 2. 增删的两种实现

**紧凑数组 + fd 到下标的映射：**

```c
/* fd -> index 的映射由应用层维护 */
size_t idx = index_of[fd];
fds[idx] = fds[nfds - 1];
index_of[fds[idx].fd] = idx;
nfds--;
index_of.erase(fd);
```

删除是 O(1)，但遍历 `poll` 结果时要注意：如果你在循环中交换了元素，当前下标可能变成「刚从末尾换过来的项」，需要重新检查同一个下标，或者先收集要关闭的 fd，循环结束后再统一删除。

**固定槽位 + `fd = -1`：**

```c
fds[i].fd = -1;      /* poll 会忽略，revents 置 0 */
fds[i].events = 0;
```

实现简单，适合连接数稳定、槽位复用的服务器。但空位会一直参与内核扫描，`nfds` 也不会因为删除而下降。长连接频繁重连时，记得定期压缩数组。

#### 3. 关注事件要跟着状态走

一个常见的性能陷阱：**永远注册 `POLLOUT`**。

`POLLOUT` 表示「发送缓冲有空间」。大多数时候它都是真的，于是 `poll` 每轮都立刻返回、你的循环每轮都发现「没什么可写」，CPU 直接空转。正确做法是：

- 输出缓冲为空时，`events` 只留 `POLLIN`（以及你关心的 HUP/RDHUP）；
- 应用产生待发数据、或上次 `send` 返回 `EAGAIN` 时，用 `modify` 加上 `POLLOUT`；
- 输出缓冲写空后，再用 `modify` 去掉 `POLLOUT`。

这和第 52 课的非阻塞输出状态机是配套的：`poll` 负责通知「现在可写」，状态机负责「还有多少没写」。

#### 4. 超时语义

C 的 `poll`：

```c
int n = poll(fds, nfds, timeout_ms);
```

| `timeout` | 行为 |
|-----------|------|
| 负值（常用 `-1`） | 无限等待，直到有事件或信号打断 |
| `0` | 立即返回，相当于非阻塞探测 |
| 正值 | 最多等待这么多毫秒；超时返回 0 |

注意：

- `poll` 的超时单位是**毫秒**，不是 `select` 的 `timeval`；
- 超时时间会被系统时钟粒度向上取整，且受调度延迟影响，可能略长于设定值；
- 返回值 `0` 表示超时，`-1` 表示出错（查 `errno`），正数表示 `revents` 非零的条目数；
- `EINTR` 表示被信号打断，通常应重新计算剩余时间后继续，而不是直接把超时当成「没有事件」。

Python 的 `poll.poll(timeout)` 同样使用毫秒；`timeout=None` 表示永久阻塞，`timeout=0` 表示立即返回。

#### 5. 周期任务的超时写法

如果每轮都固定传 `2000`，处理事件耗时会让心跳周期不断漂移。更稳的写法是用单调时钟维护 deadline：

```python
import time

deadline = time.monotonic() + 2.0

while running:
    remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
    events = poller.poll(remaining_ms)

    if not events:
        do_heartbeat()
        deadline = time.monotonic() + 2.0
        continue

    for fd, revents in events:
        handle(fd, revents)

    if time.monotonic() >= deadline:
        do_heartbeat()
        deadline = time.monotonic() + 2.0
```

这样即使中间处理了很多事件，也不会把「每 2 秒一次」拖成「每 5 秒一次」。

#### 6. 返回后仍要扫描

`poll` 的返回值 `n` 只是「有几个条目有事件」，不是「哪几个条目有事件」。你仍然要遍历数组，检查每一项的 `revents`：

```c
int n = poll(fds, nfds, 1000);
if (n > 0) {
    for (nfds_t i = 0; i < nfds; i++) {
        if (fds[i].revents == 0) continue;
        /* 处理 fds[i] */
    }
}
```

Python 的 `poll()` 帮你做了这一步，直接返回 `(fd, revents)` 列表；但底层仍然是线性扫描。

#### 7. 为什么大集合稀疏活跃时仍然贵

假设你有 10 万条长连接，但每秒只有 100 条活跃：

- `poll` 每次调用都要把 10 万个 `pollfd`（每项通常 8 字节，约 800 KB）拷贝进内核；
- 内核要逐项检查 10 万次；
- 用户态拿到结果后还要再扫一遍数组。

真正干活的只有 100 条，但成本按 10 万条算。这就是节点 tradeoffs 说的：

> 少量连接时poll简单足够，大集合稀疏活跃时需评估扫描成本。

注意，这不是说 `poll` 的复杂度标签「更差」，而是说它的 `n` 是**关注项总数**，而 `epoll` 的 `n` 是**本次活跃数**。选型时要看的是「活跃比例」和「集合规模」，不是只看大 O。

---

## 常见误区与设计权衡

### 误区（pitfalls）

**1. 看到 `POLLHUP` 就直接关闭，丢掉未读数据**

节点原话：

> HUP不意味着可以直接丢弃未读数据。

`POLLHUP` 只表示对端关闭了通道；接收缓冲里可能还有数据。必须先读到 `EAGAIN` 或 EOF，再关闭。正确顺序是 `POLLNVAL` → `POLLIN` → `POLLHUP/POLLERR`。

**2. 把 `nfds` 当成 `max_fd + 1`**

这是从 `select` 带过来的习惯。`poll` 的 `nfds` 是数组元素个数。3 个 fd（编号 3、5、900）对应 `nfds = 3`，不是 901。

**3. 用 `events` 去读结果**

`events` 是你填的输入，`revents` 才是内核填的输出。不要写 `if (fds[i].events & POLLIN)` 来判断就绪。

**4. 永远注册 `POLLOUT`**

可写条件通常长期成立，会导致 `poll` 立即返回、事件循环空转。只在确实有数据要写或上次 `send` 遇到 `EAGAIN` 时才关注 `POLLOUT`。

**5. 关闭 fd 后不清理数组条目**

后果有两种：

- fd 没有复用 → 下次 `poll` 返回 `POLLNVAL`；
- fd 被复用 → 旧条目指向新连接，事件处理错乱，甚至把新连接的数据当成旧连接的。

两者都很糟。关闭连接时同步删除/失效数组条目，是硬性要求。

**6. 把 `POLLNVAL` 当成普通错误去 `recv`**

`POLLNVAL` 说明 fd 编号无效，任何 I/O 都可能失败或作用到错误对象上。正确动作是清理条目，不是读 `SO_ERROR`，也不是再 `close` 一次。

**7. 以为 `POLLIN` 代表「一条完整消息」**

TCP 是字节流。`POLLIN` 只保证「现在读不会阻塞」，不保证半包/粘包已经被处理好。应用层协议边界仍要自己维护。

**8. 忽略 `EINTR`**

信号打断时 `poll` 返回 `-1` 且 `errno == EINTR`。如果不处理，事件循环可能直接退出。正确做法是重新计算超时并继续。

**9. 用 `poll` 监听普通文件**

普通文件的读写通常总是「就绪」，`poll` 会立即返回。它适合套接字、管道、终端等会阻塞/可等待的对象，不适合用来做磁盘文件的事件驱动。

**10. 以为 `poll` 是边缘触发**

Linux 的 `poll` 默认是**水平触发**：未读数据会反复报告 `POLLIN`，未接受的连接会反复报告监听 fd 的 `POLLIN`。必须主动排空，否则会忙循环。边缘触发是 `epoll` 的选项，不是 `poll` 的。

### 权衡（tradeoffs）

节点原话：

> 少量连接时poll简单足够，大集合稀疏活跃时需评估扫描成本。

| 维度 | select | poll | epoll（后续课程） |
|------|--------|------|-------------------|
| 关注项表达 | 三张 fd 位图 | `pollfd` 数组 | 内核兴趣列表 |
| fd 编号上限 | `FD_SETSIZE`（常 1024） | 无位图上限；受内存/`RLIMIT_NOFILE` 约束 | 同左 |
| `nfds` 含义 | `max_fd + 1` | 数组元素个数 | 注册项数（由内核管理） |
| 输入是否被改写 | 是，每轮重建 | 否，`events`/`revents` 分离 | 不适用 |
| 每次调用成本 | 拷贝 + 扫描到 max_fd | 拷贝 + 扫描数组 | 返回就绪列表，通常按活跃数 |
| 触发方式 | 水平触发 | 水平触发 | 水平/边缘触发 |
| 可移植性 | 最广 | Unix 常见 | Linux 专有 |
| 适合场景 | 小规模、教学、老代码 | 中等规模、语义清晰、跨平台 | 大规模长连接网关 |

`poll` 的甜点区是「连接数不大，或者活跃比例较高，且你希望代码简单可移植」。当连接数到十万级、每秒活跃比例很低时，整表拷贝与扫描会成为瓶颈，此时应评估 `epoll`。但即便迁移到 `epoll`，本课关于 `POLLIN`/`POLLHUP`/`POLLERR`/`POLLNVAL` 的处理顺序仍然适用——`epoll` 只是换了通知机制，没有改变「就绪不等于读完」的语义。

---

## 动手实践

节点 practice：

> 把select版本替换为poll，测试数据与HUP同时返回的情况。

验收（acceptance）：

- **HUP 时仍能读取缓冲中的剩余数据**；
- **正确处理 `POLLNVAL`**。

并呼应 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

请对照 [poll(2)](https://man7.org/linux/man-pages/man2/poll.2.html) 核对：`pollfd` 三字段、`nfds` 是数组长度、`timeout` 毫秒语义、`POLLIN/HUP/ERR/NVAL` 的输入输出方向、`fd < 0` 的忽略规则、`EINTR` 与返回值。记录你实验机的内核版本、Python 版本，以及 `POLLRDHUP` 是否可用。

### 步骤拆解

1. **准备基线**  
   把上一课 `practice53/` 的 `select` 聊天服务复制为 `practice54/`，保留客户端、测试脚本和日志格式。先确认原版能跑通，再动手替换，避免把旧 bug 和新 bug 混在一起。

2. **创建 poller**  
   Python 用 `select.poll()`；C/C++ 用 `std::vector<struct pollfd>`。  
   监听 fd 注册 `POLLIN`。  
   维护 `fd -> 连接对象` 的字典/映射，并确保关闭时同步移除。

3. **替换事件循环**  
   把 `select.select(rlist, [], [], 2.0)` 改成 `poller.poll(2000)`。  
   返回空列表时打印 `IDLE heartbeat`，验证超时分支。  
   返回 `(fd, revents)` 列表时，按 fd 分发，而不是按列表下标猜测。

4. **处理 accept**  
   listen fd 的 `POLLIN` 分支里循环 `accept` 直到 `EAGAIN`。  
   每个新连接设非阻塞，`register(fd, POLLIN)`。  
   如果 fd 数量超过你设定的上限，明确拒绝并 `close`；`poll` 虽然没有 `FD_SETSIZE` 位图限制，但你的内存和 `RLIMIT_NOFILE` 仍然有上限。

5. **处理读事件**  
   对 client 的 `POLLIN`，循环 `recv` 直到 `EAGAIN` 或返回 0。  
   有数据就按聊天协议广播给其他连接。  
   读到 EOF 时记录「读方向结束」，但不要立刻丢弃还没发完的输出缓冲（如果该连接还有下行数据要发，可进入半关闭状态）。

6. **处理 HUP / ERR**  
   在 `POLLIN` 之后检查 `POLLHUP` / `POLLERR`。  
   先确保接收缓冲已经读空（或 `recv` 已经返回错误），再关闭。  
   日志里打印 `HUP fd=... drained=... bytes=...`，方便验收「HUP 前读到了剩余数据」。

7. **测试「数据 + HUP」**  
   给客户端加一个 `--half-close` 或 `--send-then-close` 模式：  
   `sendall(b"last-event\n")` 后立即 `shutdown(SHUT_WR)` 或 `close()`。  
   服务端应广播 `last-event`，然后才清理连接。  
   也可以直接用 `socketpair` 写一个 20 行的小脚本，不经过完整服务，先验证你对 `revents` 的理解。

8. **测试 POLLNVAL**  
   构造一个残留条目：注册一个 fd 后关闭它，但不调用 `unregister`，下一轮 `poll` 应返回 `POLLNVAL`。  
   服务端必须打印 `POLLNVAL fd=...`，跳过所有 I/O，移除条目，并且不重复 `close`。  
   更进阶的做法是：关闭后立刻 `accept` 一个新连接，观察 fd 编号是否被复用，从而理解「为什么不能依赖 `POLLNVAL` 兜底」。

9. **更新关注事件**  
   为输出缓冲增加 `POLLOUT` 动态开关：  
   有数据待发 → `modify(fd, POLLIN | POLLOUT)`；  
   写空 → `modify(fd, POLLIN)`。  
   观察 CPU 占用是否从「空转 100%」降下来。

10. **对照 man page 记录边界**  
    在笔记里写清：

    - `nfds` 是数组长度，不是 `max_fd + 1`；
    - `POLLHUP/POLLERR/POLLNVAL` 只在 `revents` 中出现；
    - `fd < 0` 的条目会被忽略；
    - `timeout` 单位是毫秒，`-1` 无限等，`0` 立即返回；
    - `EINTR` 要重试；
    - `POLLRDHUP` 是 Linux 扩展，需要相应头文件/宏；
    - `poll` 是水平触发，必须排空。

### 预期现象

- 两个以上客户端：一人发消息，其他人收到广播；
- 客户端「发完即关」：服务端日志先出现收到 `last-event`，再出现 `HUP` 与清理；
- 空闲时周期性出现 `IDLE heartbeat`；
- 构造失效 fd 时出现 `POLLNVAL`，且服务不崩溃、不对该 fd 做读写；
- 关闭高编号连接后，服务端数组长度（或存活项数）下降；`poll` 不要求 `nfds = max_fd + 1`；
- 动态开关 `POLLOUT` 后，空闲 CPU 占用明显下降；
- 二次绑定同端口仍能给出明确的 `EADDRINUSE` 错误。

### 验收清单

```text
[ ] HUP 时先读数据、后关闭，日志能证明数据已收到
[ ] POLLNVAL 分支不做 recv/send，条目被清理且不重复 close
[ ] nfds 使用数组元素个数，而不是 max_fd + 1
[ ] events 与 revents 没有混用
[ ] POLLOUT 只在需要时注册
[ ] 超时、EINTR 都有明确分支
[ ] 关闭连接时同步移除 poll 条目
```

---

## 自测题

**题 1**  
`poll` 的 `fds` 数组里有 3 项，fd 分别是 3、5、900。调用 `poll(fds, nfds, 1000)` 时，`nfds` 应该传多少？

A. 3  
B. 900  
C. 901  
D. 908

**题 2**  
某连接 `poll` 返回 `revents = POLLIN | POLLHUP`，此时正确的处理顺序是？

A. 立即 `close`，HUP 表示连接已经不可用，缓冲数据可以丢弃  
B. 先处理 HUP 关闭，再尝试从已关闭的 fd 读数据  
C. 先循环读取直到 `EAGAIN` 或 EOF，确认剩余数据读完后再关闭  
D. 只处理 `POLLHUP`，忽略 `POLLIN`，因为 HUP 优先级更高

**题 3**  
`poll` 对某个条目返回 `POLLNVAL`，最合适的处理是？

A. 调用 `recv` 确认错误码，再决定是否关闭  
B. 从数组中移除/失效该条目，不做 I/O，也不重复 `close`，并清理应用层对象  
C. 把 `events` 改成 `POLLOUT` 后继续等待  
D. 认为连接只是暂时不可读，下一轮会自动恢复

---

### 自测题参考答案

**题 1：A**  
`poll` 的 `nfds` 是数组元素个数，与 fd 编号大小无关。数组有 3 项就传 3。C 是 `select` 的思维（`max_fd + 1`），在这里是错的。

**题 2：C**  
`POLLHUP` 只表示对端关闭通道；接收缓冲中可能还有未读数据。正确顺序是先处理 `POLLIN` 把数据读空（读到 `EAGAIN` 或 EOF），再处理 `POLLHUP` 并关闭。A 会丢数据，B 是对已关闭 fd 做 I/O，D 忽略了可读数据。

**题 3：B**  
`POLLNVAL` 表示 fd 编号当前无效。不能对它做 `recv`/`send`，也不应重复 `close`。正确处理是清理 poll 条目和应用层连接对象。还要记住：不要依赖 `POLLNVAL` 发现「自己关闭但忘记移除」的 fd，因为该编号可能已被复用。

---

## 推荐资源

- [poll与就绪集合扫描：Linux man-pages — poll(2)](https://man7.org/linux/man-pages/man2/poll.2.html)（man7.org，英文文档，进阶）
  - 重点阅读：`struct pollfd` 的 `fd`/`events`/`revents`、`nfds` 的数组语义、`timeout` 的毫秒与负值/零值语义、`POLLIN`/`POLLHUP`/`POLLERR`/`POLLNVAL` 的输入输出方向、`fd < 0` 的忽略规则、返回值与 `EINTR`。
  - 特别核对 `POLLHUP` 段落：对端关闭后，**未读数据消费完之前**，后续读取才会返回 EOF。这是本课「HUP 时仍能读取缓冲中的剩余数据」的文档依据。
  - 同时留意 `POLLRDHUP` 的版本说明与 `_GNU_SOURCE` 要求，记录它在你的实验环境是否可用。

---

## 本课小结

1. **`poll` 用描述符数组替代 `fd_set` 位图**：每个 `struct pollfd` 显式包含 `fd`、`events`（输入关注）、`revents`（输出结果）。它消除了 `FD_SETSIZE` 的位图上限，`nfds` 是数组元素个数，不是 `max_fd + 1`。
2. **`events` 和 `revents` 分离**：输入不会被结果覆盖，但连接增删和关注事件变化仍要你手动维护数组。`fd < 0` 的条目会被内核忽略。
3. **`POLLIN` 表示就绪，不表示读完**：`poll` 是水平触发，必须循环读到 `EAGAIN` 或 EOF；监听 fd 上则要循环 `accept` 到 `EAGAIN`。
4. **`POLLHUP` 不意味着可以丢弃未读数据**：对端关闭时缓冲里可能还有最后一段数据。正确处理顺序是 `POLLNVAL` → `POLLIN` → `POLLHUP/POLLERR`：先读空，再关闭。
5. **`POLLNVAL` 是 fd 无效信号**：不做 I/O，清理条目，不重复 `close`；并且不要依赖它来兜底「关闭后忘记移除」的问题，因为 fd 编号可能被复用。
6. **数组管理与超时是工程重点**：新增/删除连接、动态开关 `POLLOUT`、处理 `EINTR`、用 deadline 避免超时漂移。少量连接时 `poll` 简单够用；大集合稀疏活跃时，整表拷贝与扫描成本会显现，应评估 `epoll`。
7. **复杂度标签不如语义重要**：`select` 的 `n` 与最大 fd 编号相关，`poll` 的 `n` 是数组长度，`epoll` 的 `n` 是活跃条目数。选型看活跃比例和集合规模，写对则取决于对就绪、HUP、ERR、NVAL 语义的把握。

后续课程会继续深入 IO 模型与事件；请先把 `practice54` 的 HUP 与 `POLLNVAL` 两个用例跑到通过，再进入后续内容。

---

## 配套练习

目录：[`practice54/`](./practice54/)

| 文件 | 作用 |
|------|------|
| `server.py` | `select.poll` 聊天服务；HUP/EOF 时先读尽再关；`__INJECT_NVAL__` 注入 stale fd 演示 `POLLNVAL`；空闲心跳 |
| `client.py` | 可连接、发送、按期望子串等待广播；`--send-then-close` / `--send-then-shutdown` 制造数据+HUP；可 `--hold` |
| `test.sh` | 自动化验收：互广播、**HUP 残留数据**、**POLLNVAL**、关闭后集合更新、空闲心跳、端口占用 |

运行：

```bash
cd practice54 && ./test.sh
```

期望末行：`ALL_CHECKS_PASSED`。

实测要点（Asia/Shanghai，本机验证时对照）：

- 2+ 客户端互相收到广播；
- 客户端 `send-then-close` 后，服务端日志出现 `RECV ... last-event-payload` 与 `HUP fd=... drained>0`，其他客户端仍收到该残留数据；
- 发送 `__INJECT_NVAL__` 后出现 `INJECT_NVAL` 与 `POLLNVAL fd=... skip_io cleanup`，服务不崩溃且继续广播；
- 关闭一个客户端后服务继续，剩余客户端仍可用；
- 出现 `IDLE heartbeat`（`poll` 超时毫秒语义）；
- 二次绑定同端口打印 `EADDRINUSE`，退出码 2。

NOTE：Python `select.poll` 映射 `POLLIN/POLLHUP/POLLERR/POLLNVAL`；本练习用「注册后立刻 close、故意不 unregister」构造 `POLLNVAL`。生产代码应在 `close` 时同步摘掉 poll 条目，把 `POLLNVAL` 当作最后防线而非常规路径。海量长连接请使用后续课程的 `epoll`。
