# 第 53/208 课：select与fd集合

- **阶段**：第 2 阶段 · IO模型与事件
- **前置课程**：d-nonblock
- **关键词**：select、fd_set、FD_SET、FD_CLR、FD_ISSET、FD_ZERO、nfds、FD_SETSIZE、readfds、writefds、exceptfds、超时、timeval、IO多路复用、事件循环
- **难度**：基础
- **所属领域**：D · IO模型与事件

---

## 导读

上一课你把阻塞 Echo 升级成了**非阻塞 + 输出缓冲状态机**：遇到 `EAGAIN` 不再把线程钉死，而是把「还没发完的字节」存进 `outbuf`，等套接字再次可写。

但状态机只回答了「推进不了时怎么办」，还缺另一半：

> **谁来告诉我：现在哪个 fd 可读、哪个可写？**

如果用「一人一线程」——每个长连接一条阻塞线程——在微信公众号后台的**长连接接入层 / 网关**里立刻撑不住：百万级在线设备、心跳、下行推送，线程栈与调度成本会先把机器拖垮。

节点 summary 一句话说清本课要解决的模型：

> select通过fd集合报告就绪状态，需要每次重建集合并处理集合大小及扫描成本。

`select` 是 Unix 上最早、最广为人知的 **I/O 多路复用**接口：一条线程把一批 fd 交给内核，内核在「其中至少一个就绪」或「超时」时返回，并告诉你**哪些**集合位被置上了。

本课要把上一课的「非阻塞状态机」接到「就绪通知」上，弄清：

1. `readfds` / `writefds` / `exceptfds` 各自表示什么；
2. `nfds = max_fd + 1` 与 `FD_SETSIZE` 硬上限；
3. **每次返回后集合被改写，必须重建**；关闭连接后要从集合摘掉，并可能降低 `max_fd`。

下一课才会走向 `poll` / `epoll`；先把 `select` 的约束吃透，后面那些接口的设计动机才站得住。

---

## 学习目标

完成本课学习后，你应当能够：

1. **解释 read / write / except 集合**
   - 说清三个 `fd_set` 参数各自关心什么就绪条件。
   - 知道 `exceptfds` **并不**代表所有普通 Socket 错误（节点 pitfalls）。
   - 会用 `FD_ZERO` / `FD_SET` / `FD_CLR` / `FD_ISSET` 操作集合。

2. **独立完成实践：实现 select 聊天服务，验证关闭连接后集合更新与最大 fd 变化**
   - 用 `select` 同时监听 listen fd 与多个 client fd。
   - 一人发言、其他人收到广播。
   - 连接关闭后从集合移除；关闭高编号 fd 后 `max_fd`（从而 `nfds`）下降。
   - **不对超出集合范围的 fd 做 `FD_SET`（或其语言等价操作）**。

3. **满足本课验收标准**
   - 能解释 `nfds` 为何是「最大 fd 加一」。
   - 不对超出集合范围的 fd 调用 `FD_SET`。

---

## 为什么后端要懂

节点里的理由是：**理解 IO 多路复用的基本模型和传统接口约束。**

### 1. 接入层的核心问题是「同时盯住很多连接」

公众号后台长连接网关典型职责：

- 维持海量设备在线（心跳、重连）；
- 把下行消息推到正确连接；
- 把上行事件交给业务集群。

你不可能为每条连接开一条阻塞线程。事件驱动模型是：

```text
一条（或少量）事件线程
    │
    ▼
等待「有人就绪」 ──select / poll / epoll──▶ 得到就绪集合
    │
    ▼
对每个就绪 fd：非阻塞 read/write + 状态机推进一点
    │
    └─▶ 再回去等下一轮就绪
```

`select` 是这条链路上**最早、可移植性最好**的一环。即便生产 Linux 网关几乎都用 `epoll`，面试与排障时你仍会遇到 `select`：老代码、跨平台库、嵌入式、教学 demo、以及「为什么 epoll 要这样设计」的对照物。

### 2. 传统接口的约束会直接变成容量上限

节点 tradeoffs：

> 可移植性较好，Linux fd_set限制使其不适合大型连接集合。

`FD_SETSIZE`（Linux 上常见 **1024**）意味着：用默认 `fd_set` 时，你基本不能把编号 ≥ 1024 的 fd 放进集合。连接数还没到「业务上的很大」，接口本身就先卡死了。此外每次调用都要**拷贝并扫描**集合，连接越多单次成本越高。

搞不清这些约束，就会：

- 把超过 `FD_SETSIZE` 的 fd `FD_SET` 进去 → 未定义行为 / 崩溃；
- 关闭连接后忘记从集合摘掉 → 对坏 fd 调用 `recv`；
- 关闭了当前最大 fd 却不更新 `nfds` → 多扫无意义的高位，或漏掉本应关注的语义一致性。

### 3. 「集合被修改」是写对事件循环的第一道坎

`select` 返回后，传入的三个集合会被内核**改写成「就绪子集」**。若你下一轮不重建，集合里只剩上一轮就绪的那些位——新连接、其它客户端全都丢了。这是本课第三个核心要点，也是无数新手事件循环的第一只坑。

---

## 直觉 → 原理

### 一、read / write / except 集合

#### 1. 接口长什么样

C 原型（见 `select(2)`）：

```c
int select(int nfds,
           fd_set *readfds,
           fd_set *writefds,
           fd_set *exceptfds,
           struct timeval *timeout);
```

直觉：

| 参数 | 你放进去的含义 | 返回后集合里留下的含义 |
|------|----------------|------------------------|
| `readfds` | 关心「可读」的 fd | 其中**真的可读**（或 listen 可 accept、或读向 EOF）的子集 |
| `writefds` | 关心「可写」的 fd | 其中**真的可写**的子集 |
| `exceptfds` | 关心「异常条件」的 fd | 其中发生异常条件的子集（见下） |
| `timeout` | 最多等多久 | 见第三节 |
| 返回值 | — | 就绪 fd 总数；`0`=超时；`-1`=出错（查 `errno`） |

任一集合指针可为 `NULL`，表示「我不关心这类事件」。

#### 2. 四个宏：集合就是位图

`fd_set` 本质是一块位图。操作约定：

```c
fd_set rfds;
FD_ZERO(&rfds);          /* 清空 */
FD_SET(listen_fd, &rfds); /* 把 listen_fd 加进集合 */
FD_SET(client_fd, &rfds);
FD_CLR(client_fd, &rfds); /* 从集合摘掉 */
if (FD_ISSET(client_fd, &rfds)) {
    /* 返回后：这个 fd 就绪了 */
}
```

Python 标准库把「集合」简化成了**可迭代的文件对象 / 描述符列表**，底层仍走 `select`：

```python
import select
readable, writable, exceptional = select.select(
    [listen_sock, *client_socks],  # 等价 readfds
    [],                             # writefds
    [],                             # exceptfds
    2.0,                            # 超时秒
)
```

教学与生产调试时，**心智模型仍按 C 的 `fd_set` 来想**：位图、上限、每次重建。

#### 3. 三类就绪分别对应什么

**`readfds` 可读**（TCP 常见）：

- 接收缓冲里有数据 → `recv` 能返回 >0；
- 对端关闭（EOF）→ `recv` 返回 0（水平触发下会一直「可读」，必须处理并摘掉）；
- 监听套接字上有新连接 → `accept` 不会阻塞（非阻塞下可能 `EAGAIN`）。

**`writefds` 可写**：

- 发送缓冲有空间 → `send` 通常能推进一些字节；
- 上一课非阻塞 connect 后，套接字变为可写时要用 `SO_ERROR` 确认结果。

**`exceptfds`——节点 pitfalls 的重点**：

> exceptfds并不代表所有普通Socket错误。

在 Linux TCP 上，`exceptfds` 主要对应**带外数据（out-of-band / urgent data）**等少数「异常条件」，**不是** `ECONNRESET`、`EPIPE`、`ETIMEDOUT` 的万能错误通道。普通错误通常在你对 fd 做 `recv`/`send` 时通过返回值 / `errno` 体现，或通过可读侧的 EOF / `recv` 错误体现。

把「连接出错」全部押在 `exceptfds` 上，是典型误用。

#### 4. 极简「同时盯 listen + 一个 client」

```c
fd_set rfds;
FD_ZERO(&rfds);
FD_SET(listen_fd, &rfds);
FD_SET(client_fd, &rfds);
int nfds = (listen_fd > client_fd ? listen_fd : client_fd) + 1;

int n = select(nfds, &rfds, NULL, NULL, NULL); /* 阻塞等到有人就绪 */
if (n < 0) { /* EINTR 等 */ }

if (FD_ISSET(listen_fd, &rfds)) {
    /* accept */
}
if (FD_ISSET(client_fd, &rfds)) {
    /* recv；若返回 0 → 关闭并从集合逻辑中摘掉 */
}
```

公众号接入层直觉：`listen_fd` 是「新设备连上来」；每个 `client_fd` 是「这条长连接有上行或对端断开」。一条线程轮转处理，而不是一人一线程。

---

### 二、nfds 和 FD_SETSIZE

#### 1. nfds = max_fd + 1

`nfds` **不是**「集合里有多少个 fd」，而是：

> 内核要检查的描述符编号上界（开区间）：检查 `[0, nfds)`。

因此正确公式永远是：

```text
nfds = （当前关心的所有 fd 中的最大值） + 1
```

例子：

| 关心的 fd | max_fd | nfds |
|-----------|--------|------|
| `{3}`（仅 listen） | 3 | 4 |
| `{3, 5, 7}` | 7 | 8 |
| 关掉 7 后剩 `{3, 5}` | 5 | 6 ← **必须下降** |

为什么是 max+1 而不是「个数」？因为 `fd_set` 是按下标（fd 编号）寻址的位图，内核按编号范围扫描，并不维护「集合元素个数」那种稀疏列表（那是 `poll` 的思路）。

Python 的 `select.select(rlist, ...)` 帮你算了内部 `nfds`，但**你自己用 C、或自己维护教学用的 `max_fd` 日志**时，必须显式更新。本课实践要求你验证：关闭高编号 fd 后 `max_fd` 下降。

#### 2. FD_SETSIZE：硬上限

Linux 上 `FD_SETSIZE` 常为 **1024**（编译进 glibc/`fd_set` 大小）。含义：

- 合法放入 `fd_set` 的 fd 编号应满足 `0 <= fd < FD_SETSIZE`；
- 对 `fd >= FD_SETSIZE` 做 `FD_SET` → **错误 / 未定义行为**（可能栈破坏）。

注意两件容易混的事：

1. **进程能 `open`/`accept` 出更大的 fd**（`ulimit -n` 可以远大于 1024）；
2. **但默认 `select` + `fd_set` 管不了它们**。

所以验收标准强调：**不对超出集合范围的 fd 调用 `FD_SET`。** 工程上要么拒绝该连接并 `close`，要么改用 `poll`/`epoll`。

```c
if (fd < 0 || fd >= FD_SETSIZE) {
    /* 拒绝：关闭连接，不要 FD_SET */
    close(fd);
} else {
    FD_SET(fd, &rfds);
}
```

Python 里可用 `getattr(select, "FD_SETSIZE", 1024)` 做同样的守卫（本课 `practice53/server.py` 即如此）。

#### 3. 扫描成本随 max_fd 涨

即使你只关心 2 个 fd，若它们的编号是 `3` 和 `900`，`nfds=901`，内核仍可能按实现扫描很大范围。连接越多、fd 编号越高，`select` 每次调用的常量成本越明显——这也是 tradeoffs 里「不适合大型连接集合」的一部分。`epoll` 用就绪队列避开了「每次带全集、扫到 max_fd」的模型。

---

### 三、超时及集合被修改

#### 1. timeval 超时语义

```c
struct timeval tv;
tv.tv_sec  = 2;
tv.tv_usec = 0;
int n = select(nfds, &rfds, NULL, NULL, &tv);
```

| `timeout` | 行为 |
|-----------|------|
| `NULL` | 一直阻塞，直到有就绪或信号打断 |
| `{0, 0}` | 轮询：立即返回当前就绪情况（非阻塞探测） |
| 正值 | 最多等待这么久；到期返回 `0`，集合被清空为「无就绪」 |

注意：许多实现会**改写** `timeval` 为「剩余时间」；可移植代码若要循环等待固定总时长，应自己用单调时钟算 deadline，而不是假设 `tv` 不变。

Python：`select.select(..., timeout=2.0)`；`timeout=0` 为轮询；省略 / `None` 为永久等。

本课实践用约 2 秒超时打印 `IDLE heartbeat`，就是为了让你亲眼看见「没有人就绪时走超时分支」，而不是死堵在 `select` 上（网关还要做空闲检测、定时踢连接等）。

#### 2. 返回后集合被改写 → 每轮必须重建

这是写对 `select` 循环的**铁律**：

```text
循环 {
    FD_ZERO + 把「当前仍存活的 fd」全部 FD_SET   ← 重建
    select(...)
    根据 FD_ISSET 处理
    若某连接关闭 → 从「存活集合」摘掉，更新 max_fd
}
```

反例：

```c
FD_ZERO(&rfds);
FD_SET(listen_fd, &rfds);
FD_SET(c1, &rfds);
for (;;) {
    select(nfds, &rfds, NULL, NULL, NULL); /* 第一轮后 rfds 只剩就绪位！ */
    /* 第二轮起：其它 fd 的位已经没了 */
}
```

正确骨架：

```c
for (;;) {
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(listen_fd, &rfds);
    int max_fd = listen_fd;
    for (/* each live client fd */) {
        FD_SET(fd, &rfds);
        if (fd > max_fd) max_fd = fd;
    }
    int n = select(max_fd + 1, &rfds, NULL, NULL, &tv);
    if (n == 0) { /* 超时：心跳 / 空闲扫描 */ continue; }
    if (n < 0)  { /* EINTR → continue；其它错误处理 */ continue; }

    if (FD_ISSET(listen_fd, &rfds)) { /* accept，加入 live 集合 */ }
    for (/* each live client */) {
        if (!FD_ISSET(fd, &rfds)) continue;
        /* recv；EOF 或错误 → close，从 live 摘掉，必要时降低 max_fd */
    }
}
```

#### 3. 关闭连接：摘集合 + 可能降低 max_fd

关闭步骤缺一不可：

1. `close(fd)`（或先从应用层 map 删掉再 close）；
2. 下一轮重建时**自然不再 `FD_SET`**（不要对已关闭 fd 再 `FD_ISSET` 去读）；
3. 若关掉的是当前 `max_fd`，重新计算 `max_fd = max(剩余 fd)`，从而 `nfds` 下降。

否则会出现：对已关闭编号做 I/O、或 `nfds` 长期虚高。实践课会打印：

```text
STATE after_close fd=6 clients=1 max_fd=4 nfds=5
```

让你用日志断言「高编号关闭后 max_fd 真的降了」。

#### 4. 和上一课非阻塞的衔接

`select` 说「可读/可写」≠「一次读写任意长度完成」（第 52 课 pitfalls）。正确组合是：

- `select` 给出就绪；
- 非阻塞 `recv`/`send` 推进一点；
- `EAGAIN` → 离开，等下一轮事件；
- 输出缓冲状态机保存剩余。

本课聊天服务为聚焦「集合与 max_fd」，广播用 `sendall` 简化；你心里要清楚：生产接入层仍要接上第 52 课的短写状态机。

---

## 常见误区与设计权衡

### 误区（pitfalls）

**1. 以为 `exceptfds` 能捕获所有 Socket 错误**

节点原话：`exceptfds并不代表所有普通Socket错误。`  
Linux 上更常见的是 OOB；`ECONNRESET` 等仍应在 `recv`/`send`/`getsockopt(SO_ERROR)` 路径处理。

**2. 不重建集合**

第一轮还能工作，第二轮开始「丢连接」。这是 `select` 特有坑（`epoll` 用另套注册模型）。

**3. `nfds` 写成「fd 个数」**

集合里 3 个 fd 编号为 3、7、20 时，`nfds` 必须是 `21`，不是 `3`。

**4. 对 `fd >= FD_SETSIZE` 仍 `FD_SET`**

进程 `ulimit` 很大 ≠ `select` 能管。超范围应拒绝并关闭，或换接口。

**5. EOF 后仍把该 fd 留在 read 集合**

水平触发下会反复「可读」，`recv` 一直返回 0，变成软自旋。关闭并从 live 集合摘掉。

**6. 把 `select` 当「消息边界」**

TCP 仍是字节流；一次可读可能是半包或粘包。广播聊天 demo 用换行只是教学简化。

### 权衡（tradeoffs）

节点原话：

> 可移植性较好，Linux fd_set限制使其不适合大型连接集合。

| 维度 | select | 后续你会学到的方向 |
|------|--------|-------------------|
| 可移植性 | 几乎处处都有 | epoll 偏 Linux；kqueue 偏 BSD |
| 集合大小 | `FD_SETSIZE` 硬上限 | poll 无此位图上限；epoll 按兴趣列表 |
| 每次调用成本 | 拷贝集合 + 与 max_fd 相关的扫描 | epoll_wait 返回就绪列表 |
| API 心智 | 简单，适合教学与小连接数 | 生产网关主流 epoll |

公众号接入层若误用 `select` 扛十万级长连接，会在 fd 编号与扫描成本上先翻车——不是业务逻辑写错，而是**模型选错**。

---

## 动手实践

节点 practice：

> 实现select聊天服务，验证关闭连接后集合更新与最大fd变化。

验收（acceptance）：

- 能解释 `nfds` 为何是最大 fd 加一；
- 不对超出集合范围的 fd 调用 `FD_SET`。

并呼应 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

请对照 [select(2)](https://man7.org/linux/man-pages/man2/select.2.html) 核对：三个集合、`nfds`、`FD_SETSIZE`、超时、返回后集合被修改；记录你实验机内核与 Python 版本。

### 步骤拆解

1. **监听套接字**  
   `SO_REUSEADDR`、`bind`/`listen`；端口占用给出明确错误（如 `EADDRINUSE`，退出码 2）。listen fd 设非阻塞，便于 `accept` 循环直到 `EAGAIN`。

2. **事件循环用 select**  
   每轮重建：`rlist = [listen] + 所有 client`。  
   `max_fd = max(所有关心的 fd)`，心里记着 `nfds = max_fd + 1`（Python 自动算，但日志要打印）。

3. **accept**  
   listen 在 readable 中时循环 `accept`；新 fd 若 `>= FD_SETSIZE` → 打印拒绝、`close`，**绝不**放进下一轮集合。

4. **广播聊天**  
   某 client `recv` 到数据 → 发给**其他**所有 client（不回环给自己亦可）。  
   `recv` 返回 0 或错误 → `close`，从 dict 摘掉，重算 `max_fd`。

5. **超时心跳**  
   `select(..., timeout=2.0)`；返回空列表时打印 `IDLE heartbeat`，演示 timeval。

6. **验证 max_fd 下降**  
   顺序建立多个连接，关掉编号最大的那个，断言服务日志里 `STATE after_close ... max_fd=` 小于被关 fd。

7. **对照 man page**  
   标出：集合改写、`nfds` 开区间、`FD_SETSIZE`、except 非普通错误。把边界写进笔记。

### 预期现象

- 两个以上客户端：一人发、其他人收到同一内容；
- 关掉其中一个后，服务继续，剩余客户端仍能收广播；
- 日志出现 `CLOSE fd=...` 与 `STATE after_close ... max_fd=...`，且高编号关闭后 `max_fd` 下降；
- 空闲时出现 `IDLE heartbeat`；
- 二次绑定同端口：`EADDRINUSE`，退出码 2；
- 超范围 fd 不会被加入集合。

配套目录见文末 `practice53/`。

---

## 自测题

**题 1**  
当前关心 fd `{3, 5, 9}`。调用 `select` 时 `nfds` 应取？

A. 3  
B. 9  
C. 10  
D. 15

**题 2**  
`select` 成功返回后，下一轮循环前最关键的一步是什么？

A. 把 `nfds` 改成返回值  
B. 重新 `FD_ZERO` 并再次 `FD_SET` 所有仍存活的 fd  
C. 清空 `exceptfds` 以外的集合即可  
D. 调用 `FD_CLR` 清掉所有位后直接再次 `select` 而不加入任何 fd

**题 3**  
下列关于 `exceptfds` 的说法，哪项正确？

A. 所有 `ECONNRESET` 都会只通过 `exceptfds` 通知  
B. 它主要覆盖带外等异常条件，不能当成普通 Socket 错误的全集  
C. Linux 上应把所有 client 都放入 `exceptfds` 才能发现断连  
D. `exceptfds` 与 `readfds` 语义完全相同

---

### 自测题参考答案

**题 1：C**  
`nfds = max_fd + 1 = 9 + 1 = 10`。A 是个数；B 是 max_fd 本身（开区间上界应再 +1）。

**题 2：B**  
返回后集合被改写成就绪子集，必须重建。A 混淆了返回值与 `nfds`；D 会变成空等待。

**题 3：B**  
对应节点 pitfalls：`exceptfds并不代表所有普通Socket错误。` 断连与重置多在 `recv`/`send` 路径体现。

---

## 推荐资源

- [select与fd集合：Linux man-pages — select(2)](https://man7.org/linux/man-pages/man2/select.2.html)（man7.org，英文文档，进阶）
  - 重点阅读：read/write/except 集合、`nfds` 与 `FD_SETSIZE`、超时与返回后集合被修改，用于核对本节点实验的语义和边界。
  - 建议连带阅读同站：`poll(2)`、`epoll(7)`、以及上一课相关的 `fcntl(2)` / `recv(2)` / `send(2)`。

---

## 本课小结

1. **`select` 用三份 fd 集合报告就绪**：`readfds` / `writefds` / `exceptfds`；用 `FD_ZERO`/`FD_SET`/`FD_CLR`/`FD_ISSET` 操作。
2. **`nfds = max_fd + 1`**，是编号开区间上界，不是集合元素个数；关闭高编号 fd 后必须降低 `max_fd`。
3. **`FD_SETSIZE`（常 1024）是硬上限**；不对超范围 fd 做 `FD_SET`，应拒绝或换 `poll`/`epoll`。
4. **每次返回后集合被改写，下一轮必须重建**；关闭连接要从存活集合摘掉。
5. **`exceptfds` ≠ 普通错误全集**（多为 OOB 等）；超时用 `timeval`（或 Python 浮点秒）做空闲/心跳。
6. **可移植但不宜大型连接集合**——这是 IO 多路复用的入门模型，也是后续 epoll 课的对照基线。

下一课将继续 IO 模型与事件主题；请先把 `practice53` 跑到 `ALL_CHECKS_PASSED`。

---

## 配套练习

目录：[`practice53/`](./practice53/)

| 文件 | 作用 |
|------|------|
| `server.py` | `select` 聊天服务；每轮重建集合；广播；关闭后更新 `max_fd`；拒绝 `fd >= FD_SETSIZE`；空闲心跳 |
| `client.py` | 可连接、发送、按期望子串等待广播；可 `--hold` 占住连接 |
| `test.sh` | 自动化验收：互广播、关闭后集合更新、`max_fd` 下降、空闲心跳、端口占用 |

运行：

```bash
cd practice53 && ./test.sh
```

期望末行：`ALL_CHECKS_PASSED`。

实测要点（Asia/Shanghai，本机验证时对照）：

- 2+ 客户端互相收到广播；
- 关闭一个客户端后服务继续，剩余客户端仍可用；
- 关闭高编号 fd 后日志中 `max_fd` 下降；
- 出现 `IDLE heartbeat`；
- 二次绑定同端口打印 `EADDRINUSE`，退出码 2。

NOTE：Python `select` 仍受平台 `FD_SETSIZE` 约束；本练习用显式守卫模拟「不对超范围 fd 做 FD_SET」。生产海量长连接请使用 `epoll` 等（后续课程）。
