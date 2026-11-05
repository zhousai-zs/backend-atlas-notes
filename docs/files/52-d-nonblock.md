# 第 52/208 课：非阻塞IO与读写状态机

- **阶段**：第 2 阶段 · IO模型与事件
- **前置课程**：d-socket
- **关键词**：O_NONBLOCK、fcntl、EAGAIN、EWOULDBLOCK、部分读写、输出缓冲、非阻塞 connect、EINPROGRESS、SO_ERROR、状态机、select
- **难度**：基础
- **所属领域**：D · IO模型与事件

---

## 导读

上一课你已经能写出「能听端口、能多客户端 Echo、能处理 EOF / EINTR」的同步 Socket 服务端。那套代码语义清楚，适合当基准。

但把它丢进微信公众号后台的**长连接网关 / 接入层**，马上会撞上另一个问题：

> 事件线程（或「负责这一大片连接的那条线程」）里，只要有一个客户端读得很慢，`send` 就会卡住；卡住期间，同线程上的其他连接都得不到调度。

节点 summary 一句话说清本质：

> 非阻塞fd在不能立即推进时返回EAGAIN等状态，应用通过状态机保存尚未完成的读写。

本课要把阻塞 Echo **升级**为：

1. fd 设为 `O_NONBLOCK`；
2. 读/写遇到 `EAGAIN` / `EWOULDBLOCK` 时不傻等、不空转；
3. 用**输出缓冲 + 状态机**保存尚未发完的数据；
4. 故意限制单次 `send` 大小，并拿**慢读客户端**验收：剩余数据不丢不重，且 CPU 不被自旋打满。

这正是后续 epoll / 事件循环课的前置肌肉记忆。

---

## 学习目标

完成本课学习后，你应当能够：

1. **解释 O_NONBLOCK 和 EAGAIN**
   - 说清非阻塞 fd 在「现在推进不了」时返回什么，而不是把线程挂起。
   - 分清 Linux 上 `EAGAIN` 与 `EWOULDBLOCK` 的常见关系。
   - 知道「可读 / 可写通知」**不保证**一次就能读完或写完任意长度。

2. **独立完成实践：将 Echo 改成非阻塞，限制单次发送大小并测试慢读客户端**
   - 监听套接字与已连接套接字都可设非阻塞。
   - 用应用层输出缓冲保存短写剩余；`EAGAIN` 后让出，等下一次可写事件。
   - 慢读客户端场景下：回显完整、不丢不重；同进程仍能服务其他连接；不 busy-loop 占满 CPU。

3. **满足本课验收标准**
   - 发送剩余数据不会丢失或重复。
   - `EAGAIN` 后不会自旋占满 CPU。

---

## 为什么后端要懂

节点里的理由是：**防止一个慢连接阻塞整个事件线程。**

### 1. 长连接网关里，「慢」是常态

公众号后台常见场景：

- 手机切到后台、弱网、TCP 窗口缩小；
- 客户端业务线程卡住，读 socket 很慢；
- 下行推送突发，单连接瞬时积压。

若你的接入线程对某个 fd 做**阻塞 `send`**，这个慢客户端就把整条事件线程「钉死」了——心跳、鉴权、其他用户的消息全部排队。线上表现往往是：P99 延迟飙高、连接超时雪崩，而 CPU 并不高（线程在睡眠等 I/O）。

### 2. 非阻塞把「等 I/O」变成「可调度的状态」

阻塞调用的语义是：调用返回前，这件事已经做完（或失败）。

非阻塞调用的语义是：

- 能推进多少推进多少；
- 现在推进不了 → 返回 `EAGAIN`，**把未完成部分留给应用状态机**；
- 线程可以去处理别的连接，稍后再回来。

### 3. 状态机是代价，也是换并发的门票

节点 tradeoffs 写得很直白：

> 状态机增加复杂度，但让线程可处理更多连接。

你会多维护：`outbuf`、是否仍关心可读/可写、对端是否已 EOF、连接是否该关闭。换来的是：一条线程（或一个事件循环）能同时推进成千上万条连接——这是网关的基本盘。

---

## 直觉 → 原理

### 一、O_NONBLOCK 和 EAGAIN

#### 1. 怎么打开非阻塞

Linux 上常见两种做法（等价目标：给 fd 打上 `O_NONBLOCK`）：

```c
#include <fcntl.h>

int flags = fcntl(fd, F_GETFL, 0);
if (flags < 0) { /* 查 errno */ }
if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) { /* 查 errno */ }
```

Python：

```python
sock.setblocking(False)   # 内部即 O_NONBLOCK
# 或
sock.settimeout(0)        # 对 socket 对象也是非阻塞语义
```

`accept` 出来的已连接套接字**不一定**继承监听套接字的非阻塞标志（取决于平台与调用路径）。工程上习惯：**accept 之后对 conn 再设一次非阻塞**。

#### 2. 不能立即完成时返回什么

对非阻塞 socket：

| 操作 | 「现在做不了」时常见结果 |
|------|--------------------------|
| `recv` / `read` | 无数据可读 → `-1` + `EAGAIN` / `EWOULDBLOCK` |
| `send` / `write` | 发送缓冲暂不可写 → `-1` + `EAGAIN` / `EWOULDBLOCK` |
| `accept` | 无新连接 → 同上 |
| `connect` | 见第三节：`EINPROGRESS` |

**Linux 上对 socket，`EAGAIN` 与 `EWOULDBLOCK` 通常是同一个值。** 可移植代码里两者都判断更稳妥：

```c
if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
    /* 稍后再试：登记到 epoll/select，而不是 while(1) 重试 */
}
```

Python 里常体现为 `BlockingIOError`（其 `errno` 仍是 `EAGAIN`/`EWOULDBLOCK`）。

#### 3. 和「阻塞」对照记

```text
阻塞 fd：  send(大缓冲) ──线程睡眠──▶ 内核慢慢发完或出错后才返回
非阻塞 fd：send(大缓冲) ──立刻返回──▶ 可能只写了 N 字节，或 EAGAIN
                                      剩余字节必须由你保存在状态机里
```

节点 why 的反面教材：你若在事件线程里对非阻塞 fd 写成：

```c
while (send(...) < 0 && errno == EAGAIN) { /* 空转 */ }
```

那是把「阻塞」换成了「烧 CPU 的忙等」——更糟。正确做法是：**离开，等可写事件**。

#### 4. 用命令感受「可写 / 不可写」

连接建立后可用 `ss -tan` 看 Send-Q：

```bash
ss -tan 'sport = :18052 or dport = :18052'
```

慢读客户端会让对端 Receive Window 变小，本端 Send-Q 堆积；此时非阻塞 `send` 更容易 `EAGAIN`。排障时把「应用 outbuf 长度」和「内核 Send-Q」一起看。

---

### 二、部分读写与输出缓冲

#### 1. 短读 / 短写不是异常

TCP 是字节流。即使阻塞 fd，`recv`/`send` 也不保证一次吞吐你请求的全部长度；非阻塞下更是常态。

节点 pitfalls：

> 可读或可写通知不保证任意长度操作立即完成。

翻译成代码纪律：

- epoll 告诉你「可读」→ 你 `recv` 一次或多次，直到 `EAGAIN`；**不要假设**「可读 = 应用层一整帧到齐」。
- 「可写」→ 你 `send` 一段，可能只发出去一部分；**剩余必须留在自己的缓冲区**。

#### 2. 输出缓冲状态机（Echo 最小模型）

把每个连接看成一台小状态机：

```text
                    ┌──────────────┐
         recv 数据   │   outbuf     │   send(限长)
    ────────────────▶│  (bytearray) │────────────────▶ 内核发送缓冲
                    └──────────────┘
                           │
                           │ EAGAIN
                           ▼
                    等待「可写」事件后再 flush
```

伪代码：

```python
SEND_LIMIT = 64  # 实践里故意限小，逼出短写

def on_readable(conn):
    data = conn.sock.recv(4096)
    if not data:
        conn.peer_closed = True   # EOF；若 outbuf 仍有数据，先冲完再关
        return
    conn.outbuf.extend(data)      # 先入队，稍后再发
    try_flush(conn)

def try_flush(conn):
    while conn.outbuf:
        nwant = min(SEND_LIMIT, len(conn.outbuf))
        try:
            n = conn.sock.send(memoryview(conn.outbuf)[:nwant])
        except BlockingIOError:
            return  # EAGAIN：保留 outbuf，等可写
        if n == 0:
            close(conn); return
        del conn.outbuf[:n]       # 只丢掉真正发出去的前缀 —— 不丢不重
```

要点：

1. **不丢**：没发出去的字节仍在 `outbuf`；
2. **不重**：`del outbuf[:n]` 按 `send` 返回值推进，而不是按「我想发多少」；
3. **不自旋**：`EAGAIN` 立刻返回事件循环。

#### 3. 限单次发送大小的教学意义

生产里不一定要把 `SEND_LIMIT` 设成 64。本课故意设小，是为了：

- 稳定复现「一次可写也发不完」；
- 强迫你写出正确的剩余缓冲逻辑；
- 让慢读客户端更容易把路径走到 `EAGAIN`。

真实网关可能按公平性、写入配额、syscall 次数做批量；但状态机骨架相同。

#### 4. 水平触发下的一个隐蔽忙等

若用 `select`/`poll` 的水平触发语义：对端 `shutdown(WR)` 后，fd **会一直显示可读**（EOF）。如果你在 `peer_closed` 之后仍把该 fd 放进读集合，事件循环会「立刻醒来 → recv 得到空 → 再 select → 再立刻醒来」，**CPU 被空转打满**——看起来像「非阻塞」，其实是 bug。

正确做法：EOF 后若只需冲刷 `outbuf`，就只监听**可写**；冲完再 `close`。

这正好对应本课验收：**EAGAIN 后不会自旋占满 CPU**（以及更广的「事件循环不得空转」）。

---

### 三、非阻塞 connect 与 SO_ERROR

服务端 Echo 用 `accept`；客户端或内部 RPC 连接池则要会**非阻塞 connect**。

#### 1. 流程

```c
/* fd 已 O_NONBLOCK */
int rc = connect(fd, addr, addrlen);
if (rc == 0) {
    /* 极少见：本机立刻完成 */
} else if (errno == EINPROGRESS) {
    /* 握手进行中：等套接字可写（或 epoll OUT/ERR） */
} else {
    /* 真正的失败 */
}
```

Python：`sock.connect()` 在非阻塞下常抛 `BlockingIOError`，其 errno 为 `EINPROGRESS`（或已连接时的 `EISCONN` 等，需按状态处理）。

#### 2. 如何知道成功还是失败：SO_ERROR

可写并不能直接等同于「连接成功」。必须查待处理错误：

```c
int err = 0;
socklen_t len = sizeof(err);
if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) < 0) {
    /* getsockopt 本身失败 */
}
if (err != 0) {
    /* connect 失败，err 即失败原因（如 ECONNREFUSED） */
} else {
    /* connect 成功，进入收发状态机 */
}
```

直觉：

```text
connect(非阻塞)
    ├─ 立刻成功 ────────────────────▶ 可收发
    ├─ EINPROGRESS ─(等可写)─┬─ SO_ERROR==0 ──▶ 可收发
    │                        └─ SO_ERROR!=0 ──▶ 失败关闭
    └─ 其他 errno ──────────────────▶ 失败
```

#### 3. 和读写状态机如何拼在一起

一个客户端连接的生命周期常常是：

1. `CONNECTING`：非阻塞 `connect`，等可写，查 `SO_ERROR`；
2. `ESTABLISHED`：按协议读写；`outbuf` 管理短写；
3. `CLOSING`：冲完缓冲或双方 EOF 后关闭。

服务端则是 `LISTENING → ACCEPTED → ESTABLISHED → CLOSING`。本课实践聚焦服务端 Echo 的读写状态机；`connect`/`SO_ERROR` 必须理解，为下一层客户端与连接池打底。

---

### 四、把三块拼成「事件线程里的一帧」

公众号接入层一条工作线程，每一轮通常只做有限几步（示意）：

```text
1. 等待事件（select/epoll_wait）
2. 若监听 fd 可读 → accept（循环到 EAGAIN）→ 新连接设 O_NONBLOCK、挂入连接表
3. 若连接可读 → recv 到 EAGAIN 或 EOF → 字节入协议解析 / Echo 则入 outbuf
4. 若连接可写且 outbuf 非空 → 限量 send → 按返回值推进
5. 清理：peer_closed 且 outbuf 空 → close；错误 → close
```

注意第 3、4 步都可能「只推进一部分」。**一帧事件循环的目标不是「服务完某个连接」，而是「让所有就绪连接各推进一步」。** 这与节点 why 完全同向：慢连接只能让自己的 outbuf 变长，不能让整线程停住。

### 五、和上一课（d-socket）的分工

| 话题 | 第 51 课 d-socket | 本课 d-nonblock |
|------|-------------------|-----------------|
| 目标 | 最小可调试同步服务 | 慢连接不堵事件线程 |
| 并发模型 | 每连接一线程（易验证） | 单线程事件循环 + 状态机 |
| send/recv | 循环直到完成或错误 | 可能 partial + EAGAIN |
| 复杂度 | 低 | 连接对象 / outbuf / 事件兴趣 |
| 下一步 | 本课 | epoll、定时器、协议状态机 |

同步版仍然有价值：当事件驱动版本行为诡异时，用同步版对照「协议语义是否正确」。

### 六、最小 C 片段：fcntl + 非阻塞 send

```c
#include <fcntl.h>
#include <errno.h>
#include <sys/socket.h>

static int set_nonblock(int fd) {
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0) return -1;
    return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

/* 返回：已写入字节数；*would_block=1 表示还需等待可写 */
static ssize_t send_some(int fd, const void *buf, size_t len, int *would_block) {
    *would_block = 0;
    ssize_t n = send(fd, buf, len, 0);
    if (n >= 0) return n;
    if (errno == EAGAIN || errno == EWOULDBLOCK) {
        *would_block = 1;
        return 0;
    }
    return -1; /* 真错误 */
}
```

应用层应维护 `buf + offset + total`，每次只对 `buf+offset` 调用 `send_some`，成功则 `offset += n`，`would_block` 则改听 `EPOLLOUT`。

---

## 常见误区与设计权衡

### 误区：可读 / 可写 = 一次能读写任意长度

这是节点 pitfalls 的原文。事件只表示「现在有机会推进」，不表示「应用层消息边界到齐」或「你的 1MB 缓冲能一次写完」。

### 误区：`EAGAIN` 就原地 while 重试

那是 busy-wait。正确路径：保存状态 → 等事件 → 再试。

### 误区：短写后把「整段缓冲」再 send 一遍

若第一次 `send` 返回了 `n`，却不推进偏移，下一次会**重复发送**前缀——慢读客户端会看到错乱/重复，验收直接挂。

### 误区：EOF 后仍监听可读（水平触发）

导致空转占 CPU。EOF 后按需只听可写，冲完再关。

### 误区：忘记查非阻塞 connect 的 SO_ERROR

「可写了」就当握手成功，可能把 `ECONNREFUSED` 当成已连接，后续读写异常难查。

### 权衡：状态机复杂度 vs 线程可服务连接数

节点 tradeoffs：

> 状态机增加复杂度，但让线程可处理更多连接。

同步多线程 Echo（上一课）好懂，但一连接一线程在百万连接下不可接受。非阻塞 + 状态机 + 后续 epoll，是用**显式复杂度**换**显式调度能力**。工程上要用清晰的连接对象、明确的状态枚举、统一的关闭路径，把复杂度关进笼子。

---

## 动手实践

节点 practice：

> 将Echo服务改成非阻塞，限制单次发送大小并测试慢读客户端。

验收：

- 发送剩余数据不会丢失或重复；
- `EAGAIN` 后不会自旋占满 CPU。

并呼应 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

请对照 `fcntl(2)` / `send(2)` / `connect(2)` / `getsockopt(2)` 核对本节语义；记录你实验机内核与 Python 版本。

### 步骤拆解

1. **从上一课阻塞 Echo 出发**  
   保留 `SO_REUSEADDR`、`bind`/`listen`、端口占用明确报错。

2. **全面非阻塞**  
   监听 socket 与每个 `accept` 出的连接都 `setblocking(False)`。

3. **事件循环**  
   用 `select`（本课够用；生产多用 epoll）区分：可读（accept/recv）、可写（flush outbuf）。

4. **输出缓冲 + 限长 send**  
   例如 `SEND_LIMIT=64`；`send` 后按返回值推进；`BlockingIOError` 计数并返回。

5. **慢读客户端**  
   每次 `recv` 很少字节并 `sleep`；断言回显与发送完全一致。

6. **并发冒烟**  
   慢读进行中，另起普通客户端，确认事件线程未被堵住。

7. **CPU 采样**  
   在「有积压、客户端暂缓读」的窗口采样服务端 CPU，确认没有空转。

8. **对照 man page**  
   核对 `O_NONBLOCK`、`EAGAIN`、短写语义；把边界记进笔记（例如水平触发 EOF）。


### 预期现象（对照验收）

- 普通小包：`RESULT ok ... match=True`
- `ABCDEFGH` 重复 500 次（4000 字节）在 `SEND_LIMIT=64` 下仍完整回显
- `slow_client` 与普通 `client` 交错时两者都成功
- 服务日志关闭连接时可见 `eagain=N`（N 可为正，取决于缓冲与时序）
- CPU 采样窗口 ticks 远低于「占满单核」
- 再次启动同端口服务：打印 `EADDRINUSE`，退出码 2

若你自己改写为 C/`epoll`，验收标准不变：剩余数据不丢不重；`EAGAIN` 路径不空转。

配套目录见文末 `practice52/`。


---

## 自测题

**题 1**  
非阻塞 `send` 返回 -1 且 `errno == EAGAIN`。下列哪项正确？

A. 连接一定断了  
B. 应立刻 `close`  
C. 保留未发送数据，等套接字可写后再发  
D. 在 while 里疯狂重试直到成功  

**题 2**  
epoll/select 提示某 TCP socket「可读」。是否意味着一次 `recv(4096)` 一定能读到完整业务帧？为什么？

**题 3**  
非阻塞 `connect` 返回 `EINPROGRESS`，随后 socket 变为可写。下一步最关键的检查是什么？

---

### 自测题参考答案

**题 1：C**  
`EAGAIN` 表示「现在不能继续写」，不是致命错误。应把剩余字节留在输出缓冲，注册/等待可写事件。A/B 把暂时不可写误判成断开；D 是自旋，违反本课验收。

**题 2：不意味着。**  
可读只说明内核接收缓冲里**至少有一些字节**（或 EOF）。TCP 无消息边界；业务帧可能跨多次 `recv`，也可能一次 `recv` 含多帧。需要应用层再组帧。这正是 pitfalls：「可读通知不保证任意长度操作立即完成」。

**题 3：用 `getsockopt(SOL_SOCKET, SO_ERROR, ...)` 查看待处理错误。**  
`SO_ERROR == 0` 才表示连接建立成功；非 0 则是失败原因（如 `ECONNREFUSED`）。不能仅凭「可写」断定握手成功。

---

## 推荐资源

- [非阻塞IO与读写状态机：Linux man-pages — fcntl(2)](https://man7.org/linux/man-pages/man2/fcntl.2.html)（man7.org，英文文档，进阶）
  - 重点阅读：`O_NONBLOCK`、与 `EAGAIN` / 部分读写相关的语义，用于核对本节点实验的边界。
  - 建议连带阅读同站：`send(2)`、`recv(2)`、`connect(2)`、`getsockopt(2)`（`SO_ERROR`）、`select(2)` / `poll(2)` / `epoll(7)`。

---

## 本课小结

1. **非阻塞 fd** 在不能立即推进时返回 `EAGAIN`/`EWOULDBLOCK`（Linux socket 上二者常等价），而不是把线程挂起。
2. **部分读写是常态**：用输出缓冲状态机保存未完成发送；按 `send` 返回值推进，才能不丢不重。
3. **可读/可写事件 ≠ 一次完成任意长度操作**；事件循环必须能「推进一点就走」。
4. **`EAGAIN` 后必须让出**，等下一轮事件；禁止忙等；水平触发下 EOF 后勿继续无意义地监听可读。
5. **非阻塞 connect** 用 `EINPROGRESS` + 可写 + `SO_ERROR` 确认结果。
6. **状态机更复杂，但能避免一个慢连接堵死整个事件线程**——这是长连接网关的生死线。

下一课将继续 IO 模型与事件主题；请先把 `practice52` 跑到 `ALL_CHECKS_PASSED`。

---

## 配套练习

目录：[`practice52/`](./practice52/)

| 文件 | 作用 |
|------|------|
| `server.py` | 非阻塞 Echo；`SEND_LIMIT` 限短写；`outbuf` 状态机；`EAGAIN` 不自旋；EOF 后只冲写 |
| `client.py` | 普通客户端；支持 `--repeat` 构造大包 |
| `slow_client.py` | 慢读客户端：小 `recv` + `sleep`，验收不丢不重 |
| `test.sh` | 自动化验收：大包拼回、慢读并发、CPU 采样、EAGAIN 计数、端口占用 |

运行：

```bash
cd practice52 && ./test.sh
```

期望末行：`ALL_CHECKS_PASSED`。

实测要点（Asia/Shanghai，本机验证时对照）：

- 限 `send` 下大包回显完整匹配；
- 慢读进行中其他连接仍可 echo；
- 积压窗口内服务端 CPU 采样不占满单核；
- 日志可见 `eagain>0`；
- 二次绑定同端口打印 `EADDRINUSE`，退出码 2。

NOTE：教学实现将已连接套接字 `SO_SNDBUF` 调小，便于稳定触发 `EAGAIN`；生产环境请按容量规划设置，勿照搬。
