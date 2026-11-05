# 第 51/208 课：Socket服务端基础

- **阶段**：第 2 阶段 · 传输与Socket
- **前置课程**：d-tcp、c-syscall、b-raii
- **关键词**：Socket、bind、listen、accept、getaddrinfo、send、recv、EINTR、EOF、SO_REUSEADDR、backlog、EADDRINUSE、Echo
- **难度**：基础
- **所属领域**：D · 传输与Socket

---

## 导读

先把场景放到微信公众号后台里。

你即将负责的几类服务，几乎都要「在某台机器上监听一个端口，等别人连上来再读写」：

1. **接入层**：把公网或内网流量接到业务进程，做协议解析、鉴权、转发。
2. **长连接网关**：维持大量客户端 TCP 连接，推送消息、心跳、下行通知。
3. **内部 RPC**：模块之间用私有协议或 HTTP/自定义帧在固定端口通信。

上线前，监控和排障会问你一串很「底层」的问题：

- 进程到底有没有在听 `0.0.0.0:8080`？`ss -ltnp` 里是谁？
- `bind` 失败是端口被占（`EADDRINUSE`）还是权限不够？
- 客户端一断开，服务会不会崩？`recv` 返回 0 你处理了吗？
- `listen(1024)` 是不是表示「最多 1024 个已建立连接」？

节点 summary 一句话说清本质：

> Socket API把协议端点暴露为fd，通过绑定、监听、接入和读写实现服务通信。

本课目标不是写出百万并发网关，而是：**独立写出最小可调试的 Linux 网络服务**——所有系统调用检查返回值，端口占用给出明确错误，能同时服务多个客户端 Echo，并正确处理 `EINTR`、EOF 与客户端异常退出。同步版语义清晰，是后续 epoll/线程池/协程的地基。

---

## 学习目标

完成本课学习后，你应当能够：

1. **解释 socket / bind / listen / accept**
   - 说清监听套接字与已连接套接字的区别。
   - 说清 `listen` 的 backlog 管的是什么队列，不是「最大 ESTABLISHED 连接数」。
   - 能用 `ss -ltnp` / `ss -tan` 对照观察监听与连接状态。

2. **独立完成实践：实现多客户端 Echo 基线，处理 EINTR、EOF 及客户端异常退出**
   - 每个连接独立处理（线程或进程均可，本课实践用线程）。
   - `accept` / `recv` / `send` 遇到 `EINTR` 可重试；`recv` 返回 0 当作对端关闭（EOF）。
   - 单客户端异常断开不拖垮整个服务进程。

3. **满足本课验收标准**
   - 所有系统调用均检查返回值。
   - 端口占用时给出明确错误。

---

## 为什么后端要懂

节点里的理由是：**独立写出最小可调试的 Linux 网络服务。**

### 1. 框架会帮你藏细节，排障不会

生产里你会用自研框架、gRPC、Nginx、自研接入层。但告警来时，你面对的是：

- `ss`、`netstat`、`/proc/net/sockstat`
- `EADDRINUSE`、`ECONNRESET`、`EPIPE`、`EINTR`
- 「半连接队列打满」「accept 太慢导致 SYN 丢弃」

如果你只见过「`listen` 一下就 `app.run()`」，这些信号无法翻译成代码语义。

### 2. 公众号后台的三类监听都是同一套 API

接入、长连接、内部 RPC 协议不同，但底层都是：

```text
socket →（可选 setsockopt）→ bind → listen → accept 循环 → send/recv → close
```

把这条链走通，你才能判断：问题在握手、在监听队列、在读写、还是在业务逻辑。

### 3. 最小可调试版本是高并发版本的语义基准

节点 tradeoff：

> 同步版本易验证语义，但不能直接承诺高并发能力。

先写对「一个连接怎么读写、怎么结束」，再谈事件驱动与多路复用。语义错了，并发模型只会把 bug 放大。

### 4. 和 RAII / 系统调用课的衔接

- **b-raii**：socket fd 是资源，异常路径必须 `close`，否则泄漏到 `ulimit -n`。
- **c-syscall**：`EINTR`、短读短写、errno 在成功后不应被误读——网络 fd 上同样成立。
- **d-tcp**：三次握手、半关闭、`TIME_WAIT` 是本课 `accept`/`shutdown`/`close` 的协议背景。

---

## 直觉 → 原理

### 一、socket / bind / listen / accept

#### 1. 把「协议端点」变成一个 fd

```c
int fd = socket(AF_INET, SOCK_STREAM, 0);
```

- `AF_INET`：IPv4（IPv6 用 `AF_INET6`，双栈场景常见）。
- `SOCK_STREAM`：TCP 字节流（对比上一课 UDP 的 `SOCK_DGRAM`）。
- 返回值是文件描述符；失败返回 `-1` 并设置 `errno`。

直觉：socket 还只是「造好了一个端点对象」，既没有地址，也不在听。

#### 2. bind：把端点钉到本地地址+端口

```c
struct sockaddr_in addr = {0};
addr.sin_family = AF_INET;
addr.sin_addr.s_addr = htonl(INADDR_ANY); /* 0.0.0.0 */
addr.sin_port = htons(8080);
if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    perror("bind");
    /* EADDRINUSE: 端口已被占用 */
}
```

要点：

- `INADDR_ANY`：所有本地网卡；只想本机可用则 `127.0.0.1`。
- 端口占用必须**明确报错**（本课验收之一），不要吞掉异常后「假装启动成功」。
- 常在 `bind` 前设置：

```c
int on = 1;
setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
```

`SO_REUSEADDR` 的常见用途：服务重启时，旧连接仍处 `TIME_WAIT`，允许新进程绑定同一本地端口（细节与 `SO_REUSEPORT` 不同，本课先掌握「重启不无谓卡死在 bind」这一层）。

#### 3. listen：声明「我是服务端，开始排队」

```c
if (listen(fd, 128) < 0) {
    perror("listen");
}
```

此后该 fd 成为**监听套接字**。它自己通常不 `send`/`recv` 业务数据，而是等着 `accept`。

用命令确认：

```bash
ss -ltnp | grep 8080
# State  Recv-Q Send-Q Local Address:Port  Peer Address:Port
# LISTEN 0      128        0.0.0.0:8080         0.0.0.0:*    users:(("my_srv",pid=...,fd=3))
```

- `LISTEN`：正在听。
- `Send-Q` 列在监听 socket 上常显示 backlog 相关容量（具体含义随实现/版本略有差异，以 man 与实测为准）。
- `-p` 显示进程，排障「到底谁占了端口」时极有用。

#### 4. accept：取出一个已完成握手的连接

```c
struct sockaddr_in peer;
socklen_t len = sizeof(peer);
int cfd = accept(fd, (struct sockaddr*)&peer, &len);
if (cfd < 0) {
    if (errno == EINTR)
        continue; /* 信号打断，重试 */
    perror("accept");
}
```

- 返回的 `cfd` 是**已连接套接字**，与对端一一对应。
- 监听 fd 继续留在 `accept` 循环里。
- `EINTR`：被信号打断时返回 `-1`，服务端循环应重试，而不是退出进程。

多客户端基线模型（同步+每连接一线程）：

```text
监听线程: while True: cfd = accept(...); spawn handler(cfd)
handler: while True: n = recv(...); if n==0: break; send echo
```

这不是高并发终极形态，但是语义最清楚的「能同时服务多人」的起点。

#### 5. backlog 到底是什么（本课核心误区）

节点 pitfalls：

> listen backlog不是最大已建立连接数。

Linux 上与之相关的是握手过程中的队列（历史与实现细节以当前内核/man 为准），直观理解：

- 客户端 SYN 到来 → 内核完成握手过程中的排队；
- 握手完成、等待 `accept` 取走的连接，在「已完成队列」里；
- `backlog` 影响的是这些**尚未被 accept 取走**的排队能力，**不是**「进程里最多能同时存在多少 ESTABLISHED 业务连接」。

因此：

- 你已经 `accept` 出来、正在业务处理的一万个连接，并不占用 backlog 名额；
- 若业务 `accept` 太慢，已完成队列满，新连接可能失败或体验为超时——看起来像「服务挂了」，其实是接入侧堆积。

可用 `ss -ltn` 看监听 socket 的 Recv-Q（已完成等待 accept 的数量等，需结合文档解读），以及 `ss -tan | grep ESTAB` 看已建立连接。不要把两者混为一谈。

---

### 二、地址结构与 getaddrinfo

#### 1. 为什么不要手写死 sockaddr

硬编码 `sockaddr_in` + `inet_addr` 在示例里很常见，但生产更推荐 `getaddrinfo`：

- 同时处理 IPv4 / IPv6；
- 支持主机名解析；
- 用 `hints` 约束协议与套接字类型，减少歧义。

#### 2. hints 的最小正确姿势（服务端被动打开）

```c
struct addrinfo hints = {0}, *res = NULL;
hints.ai_family   = AF_UNSPEC;     /* 或 AF_INET / AF_INET6 */
hints.ai_socktype = SOCK_STREAM;
hints.ai_flags    = AI_PASSIVE;    /* 用于 bind 的通配地址 */
hints.ai_protocol = 0;

int err = getaddrinfo(NULL, "8080", &hints, &res);
if (err != 0) {
    fprintf(stderr, "getaddrinfo: %s\n", gai_strerror(err));
    return 1;
}

int fd = -1;
for (struct addrinfo *p = res; p; p = p->ai_next) {
    fd = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
    if (fd < 0) continue;
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
    if (bind(fd, p->ai_addr, p->ai_addrlen) == 0) {
        break; /* success */
    }
    close(fd);
    fd = -1;
}
freeaddrinfo(res);
if (fd < 0) { /* 全部失败，报告原因 */ }
```

注意：

- `AI_PASSIVE` + `node=NULL`：得到用于 `bind` 的通配地址。
- 客户端主动连接时通常**不加** `AI_PASSIVE`，而是传入对端主机名。
- `freeaddrinfo` 成对调用，避免泄漏（呼应 b-raii）。

#### 3. Python 里的等价物

```python
import socket
infos = socket.getaddrinfo(
    None, 8080, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
)
# infos: list of (family, type, proto, canonname, sockaddr)
```

实践课为聚焦语义，示例使用 `AF_INET` + `127.0.0.1`；你应能对照 man 页把同一逻辑改成 `getaddrinfo` 版本。

#### 4. 排障命令

```bash
ss -ltnp                 # 谁在 LISTEN
ss -tan 'sport = :8080'  # 该端口相关 TCP 状态
cat /proc/net/sockstat   # 全局 sockets 用量概览
```

微信公众号后台机器上，这些命令是「接入层是否真的起来了」的第一眼。

---

### 三、send / recv 和错误处理

#### 1. recv 的三种「正常」结果

| 返回值 | 含义 | 服务端典型动作 |
|--------|------|----------------|
| `> 0` | 读到若干字节（可能短读） | 处理数据；TCP 无消息边界，注意粘包/拆包 |
| `0` | EOF：对端关闭了写半部（或连接已结束） | 结束该连接的读循环，清理资源 |
| `-1` | 出错，看 `errno` | `EINTR` 重试；`EAGAIN`/`EWOULDBLOCK`（非阻塞）稍后再读；其余多数情况关连接或记日志 |

节点要求处理 **EOF**：客户端正常离开时，`recv==0` 不应当成崩溃，也不应空转。

#### 2. send 的短写

`send` 不保证一次把缓冲区全部发出：

```c
ssize_t send_all(int fd, const void *buf, size_t len) {
    const char *p = buf;
    size_t left = len;
    while (left > 0) {
        ssize_t n = send(fd, p, left, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) return -1; /* 极少见，视为失败 */
        p += n;
        left -= n;
    }
    return (ssize_t)len;
}
```

Echo 服务「读多少写多少」时，必须用循环处理短写，否则客户端会偶发少字节。

#### 3. EINTR：信号与慢系统调用

`accept` / `recv` / `send` / `connect` 都可能在信号到来时返回 `-1` 且 `errno==EINTR`（是否自动重启取决于 `SA_RESTART` 等，见 c-signal / c-syscall）。服务端循环的稳健写法是：**对 EINTR 重试，不要直接退出**。

Python 3 在部分路径会把 `EINTR` 转成自动重试或 `InterruptedError`；C/C++ 生产代码必须显式处理。实践服务器两种都兜住。

#### 4. 客户端异常退出

对端崩溃、`SO_LINGER` 立即关闭、中间设备重置等，可能表现为：

- `recv` 返回 `-1`（如 `ECONNRESET`）
- 随后 `send` 失败（`EPIPE`，并可能收到 `SIGPIPE`——服务端常设 `MSG_NOSIGNAL` 或忽略 `SIGPIPE`）

基线要求：**单个连接的异常用 try/except（或检查返回值）隔离，记录日志后关闭该 fd，监听循环继续**。不要让未捕获异常冒泡杀死整个进程。

#### 5. 端口占用的明确错误

```python
try:
    srv.bind((host, port))
except OSError as e:
    if e.errno == errno.EADDRINUSE:
        print(f"ERROR: port {host}:{port} already in use (EADDRINUSE)", file=sys.stderr)
        sys.exit(2)
    raise
```

验收标准要求「端口占用时给出明确错误」——运维和同学一眼能看懂，而不是笼统的 `OSError`。

#### 6. 最小 C 服务端骨架（示意）

```c
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#include <stdio.h>
#include <errno.h>
#include <string.h>

int main() {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(18051);
    if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        if (errno == EADDRINUSE)
            fprintf(stderr, "ERROR: port in use (EADDRINUSE)\n");
        else
            perror("bind");
        return 2;
    }
    if (listen(fd, 128) < 0) { perror("listen"); return 1; }

    for (;;) {
        int cfd = accept(fd, NULL, NULL);
        if (cfd < 0) {
            if (errno == EINTR) continue;
            perror("accept");
            continue;
        }
        char buf[4096];
        for (;;) {
            ssize_t n = recv(cfd, buf, sizeof(buf), 0);
            if (n < 0) {
                if (errno == EINTR) continue;
                break;
            }
            if (n == 0) break; /* EOF */
            ssize_t off = 0;
            while (off < n) {
                ssize_t m = send(cfd, buf + off, (size_t)(n - off), 0);
                if (m < 0) {
                    if (errno == EINTR) continue;
                    goto done;
                }
                off += m;
            }
        }
done:
        close(cfd);
    }
}
```

上面是单线程串行版，便于理解；实践目录里的 Python 版用线程同时服务多客户端。

---

## 常见误区与设计权衡

### 误区：把 backlog 当成最大连接数

「`listen(1024)` 所以我最多 1024 个用户」——错。

- backlog ≈ 握手完成前后、等待 `accept` 的排队能力；
- 真正同时服务多少连接，取决于你 `accept` 之后是否持有、线程/协程/事件循环模型、`ulimit -n`、内存与业务逻辑。

公众号长连接网关动辄数十万连接，靠的是事件驱动与连接管理，不是把 backlog 调到几十万。

### 误区：忽略 recv==0

把 EOF 当成错误狂打日志，或完全不处理导致空转，都会在接入层放大噪声。客户端正常离开是主路径之一。

### 误区：不检查 bind/listen/accept 返回值

「本地 demo 从没失败过」——到了共享开发机、容器端口冲突、权限不足，第一次失败就会变成难以理解的现象。验收要求：**所有系统调用均检查返回值**。

### 权衡：同步易验证 vs 高并发

节点 tradeoffs：

> 同步版本易验证语义，但不能直接承诺高并发能力。

| 方案 | 优点 | 代价 |
|------|------|------|
| 每连接阻塞线程（本课基线） | 逻辑直白、易测 Echo/EOF/异常 | 线程成本高，不适合海量长连接 |
| 单线程 select/poll/epoll | 高并发潜力 | 状态机复杂，易写出难调 bug |
| 进程池 / 多进程 accept | 隔离好 | 运维与共享状态成本 |

先用同步版把语义钉死，再替换 I/O 模型——这是后台工程师的稳妥路径。

---

## 动手实践

实践目标（节点 practice）：

> 实现多客户端Echo基线版本，处理EINTR、EOF及客户端异常退出。

验收（节点 acceptance）：

1. 所有系统调用均检查返回值；
2. 端口占用时给出明确错误。

### 步骤拆解

1. **建监听**
   - `socket(AF_INET, SOCK_STREAM)`
   - `SO_REUSEADDR`
   - `bind(127.0.0.1, PORT)`；捕获 `EADDRINUSE` 并打印明确中文/英文错误后非零退出
   - `listen(backlog)`（如 128）
   - 用 `ss -ltnp` 确认 `LISTEN`

2. **accept 循环**
   - `EINTR` / `InterruptedError` 时 continue
   - 每个新连接交给独立线程（或进程）处理，避免串行阻塞导致「假死」

3. **Echo 读写**
   - `recv`：`n>0` 则 echo；`n==0` 记 EOF 并关闭；错误则记日志并关闭
   - `send`：循环直到发完（短写）
   - 外层 try/except，保证异常断开不抛出到主线程

4. **验证多客户端**
   - 同时开多个 client，各自发送不同 payload，确认互不干扰
   - 一个 client 半关闭（`shutdown(SHUT_WR)`）后，服务仍接受新连接
   - 第二个进程再 `bind` 同端口，应看到明确占用错误

5. **对照 sourceStudy**

节点 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

请打开 [socket(2)](https://man7.org/linux/man-pages/man2/socket.2.html)，并沿同站 `bind(2)` / `listen(2)` / `accept(2)` / `getaddrinfo(3)` / `recv(2)` / `send(2)` 核对：

- 返回值与关键 `errno`；
- backlog 语义描述；
- `AI_PASSIVE` 行为；
- 你实验所用内核与 Python/glibc 版本下的边界（记在笔记里）。

配套可运行代码见课末「配套练习」`practice51/`。

---

## 自测题

**题 1**：`listen(fd, 128)` 中的 `128` 是否表示该服务最多只能有 128 个已建立（ESTABLISHED）的客户端连接？为什么？

<details><summary>答案</summary>

不是。backlog 影响的是连接建立过程中、等待 `accept` 取走的排队能力，不是业务上已 `accept` 并正在服务的最大 ESTABLISHED 连接数。已接入并由你持有的连接数受 fd 上限、内存与并发模型约束，与 backlog 不是同一概念。

</details>

**题 2**：TCP Echo 服务端 `recv` 返回 0 通常意味着什么？此时是否应视为进程级故障并退出 `accept` 循环？

<details><summary>答案</summary>

通常表示该连接上对端已关闭写侧（EOF）或连接结束。应结束**该连接**的读循环并释放 fd，而不是退出整个服务进程的 `accept` 循环。进程级退出只应发生在监听 socket 本身不可恢复的错误或显式停机时。

</details>

**题 3**：服务重启时 `bind` 报 `EADDRINUSE`，你应检查什么？`SO_REUSEADDR` 解决的是哪类常见情况？

<details><summary>答案</summary>

先用 `ss -ltnp` / `ss -tan` 看是否仍有进程在 `LISTEN`，或存在占用该本地端口的连接（含 `TIME_WAIT` 等情形）。`SO_REUSEADDR` 常用于允许新进程在旧连接仍处 `TIME_WAIT` 时绑定同一本地端口，避免无谓的重启失败；若另一进程仍在监听同一地址，则仍会冲突，需要找到并停掉占用者，而不是只靠选项「硬抢」。

</details>

---

## 推荐资源

- [Socket服务端基础：Linux man-pages — socket(2)](https://man7.org/linux/man-pages/man2/socket.2.html)（man7.org，英文文档，进阶）
  - 重点阅读：`socket` / `bind` / `listen` / `accept` 与地址结构、`getaddrinfo` 相关页面，用于核对本节点实验的语义和边界。
  - 建议连带阅读同站：`bind(2)`、`listen(2)`、`accept(2)`、`recv(2)`、`send(2)`、`getaddrinfo(3)`、`ip(7)`。

---

## 本课小结

1. **Socket API** 把协议端点暴露为 fd：`socket → bind → listen → accept → send/recv → close`。
2. **监听套接字**与**已连接套接字**职责不同；`ss -ltnp` 是确认「谁在听」的第一工具。
3. **backlog 不是最大已建立连接数**；不要用它估算业务容量。
4. **getaddrinfo + AI_PASSIVE** 是可移植绑定方式；生产代码优先于手写死 `sockaddr_in`。
5. **错误处理是服务端的一部分**：`EINTR` 重试、`recv==0` 当 EOF、短写循环、`EADDRINUSE` 明确报错、单连接异常隔离。
6. **同步多客户端 Echo** 易验证语义，但不承诺高并发；它是后续事件驱动课的基准实现。

下一课将在此基础上继续加深传输与 Socket 主题；请先把 `practice51` 跑到 `ALL_CHECKS_PASSED`。

---

## 配套练习

目录：[`practice51/`](./practice51/)

| 文件 | 作用 |
|------|------|
| `server.py` | 多客户端 TCP Echo；检查 bind 错误；处理 EOF / 异常断开；`EINTR` 重试 |
| `client.py` | 单连接 echo 客户端辅助 |
| `test.sh` | 自动化验收：多客户端、EOF 后仍服务、端口占用明确错误、异常断开不拖垮 |

运行：

```bash
cd practice51 && ./test.sh
```

期望末行：`ALL_CHECKS_PASSED`。

实测要点（Asia/Shanghai，本机验证时对照）：

- `ss -ltnp` 可见监听端口；
- 8 客户端并发 echo 全部成功；
- 半关闭/EOF 后服务仍存活并可接受新连接；
- 二次启动同端口打印 `EADDRINUSE` / already in use，退出码 2；
- `SO_LINGER` 异常断开后服务仍可 echo。

NOTE：若环境无 `tcpdump` / `strace`，抓包与系统调用跟踪请在有权限的机器上按课文执行。
