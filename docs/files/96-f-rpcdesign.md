# 第 96/208 课：轻量RPC框架设计与边界

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `f-rpcdesign` |
| 英文名 | RPC Framework Design |
| 领域 / 阶段 | F / 第 4 阶段 |
| 子类 | 协议与框架 |
| 难度 | 深入 |
| 前置节点 | `f-async`、`f-evolution`、`d-reactor` |
| 标签 | `RPC Framework Design`、`F`、`C++` |
| 参考资料 | [轻量RPC框架设计与边界：参考文档 / 原始资料](https://github.com/apache/brpc/tree/master/src/brpc)（github.com，源码） |
| 摘要 | 教学RPC把协议、连接、调度、超时和可观测性组合起来，复杂度主要来自失败与生命周期管理。 |

> **边界说明**：本课中出现的「文章服务」「计数服务」「公众号后台」等场景都是**教学假设**，用来让概念落地，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python asyncio 写的教学 RPC，用来暴露语义问题；brpc 等生产框架的具体实现以所读源码版本为准。

## 导读

### 一个直觉：取餐号

想象一家快餐店：你点完餐拿到 **37 号**小票。后厨同时做十几份餐，汉堡快、套餐慢，**谁先做好谁先出**。柜台喊「37 号」，你上去取——靠的是号码，而不是「我是第几个排队的」。

- 如果柜台按排队顺序发餐，第一个人会拿到第二个人的汉堡（**乱序响应错配**）；
- 你等太久走了，小票要作废，否则之后做好的餐没人领、堆满出餐台（**超时要清理**）；
- 店突然停电关门，店员得通知所有还在等的人「今天出不了了」，而不是让大家一直站着（**断连要唤醒所有等待者**）；
- 有人递来一张写着「我要 20 亿份薯条」的单子，正确做法是直接拒绝，而不是开始备料（**协议上限**）。

RPC 框架做的就是这家店的「叫号系统」。

### 接上前置

- **d-reactor**：事件循环怎样在一个线程里同时读写很多连接——这是 RPC 的「传输引擎」；
- **f-async**：异步调用怎样用 Future/回调表达「以后才有结果」，以及回调和上下文的生命周期——这是 RPC 的「调用语义」；
- **f-evolution**：协议要带版本、字段要能演进——这是 RPC 的「契约」。

前面每一块单独看都学过，本课把它们**组装成一个框架**，然后看组装后才出现的问题。

### 一次 RPC 调用的生命周期

```
 调用方协程                     客户端框架                         网络           服务端框架
 ─────────                     ─────────                         ────           ─────────
 stub.get_read_count(42) ──▶ ① 序列化参数
                             ② 分配 req_id=1007
                             ③ pending[1007] = Future
                                + 登记超时（例如 200ms）
                             ④ 编帧：16B头 + body ──写入连接──▶  ⑤ 拆帧：读满头→校验 magic/版本/长度→读满 body
                                                                  ⑥ 按 method 查注册表，分发给 handler（并发执行）
                                                                  ⑦ 编响应帧（带回 req_id=1007）
                             ⑧ 读循环收帧 ◀──────────写回────────
                             ⑨ pending.pop(1007) → 找到 Future
                             ⑩ 取消超时，set_result
 await 返回结果 ◀────────────
```

正常路径只有这 10 步。本课真正要讲的是：第 ③ 步登记的东西，在**超时、断连、晚到、畸形帧、handler 异常**时分别由谁、在什么时候释放。

### 教学场景（教学假设）

文章服务渲染一个文章列表，需要对 20 篇文章调用计数服务 `get_read_count(article_id)`。为了省连接，它在**一条长连接**上并发发出 20 个请求。计数服务内部并发处理，有的命中缓存 1ms 就回，有的要查存储 15ms。

如果客户端「按发送顺序」匹配响应，A 文章的阅读数就会显示到 B 文章上——而且监控上没有任何错误。本课实践中，单连接 5000 个并发请求用 FIFO 匹配，**4989 个**拿到了别的文章的数字。

### 本课三条主线

1. **请求ID与响应匹配**：多路复用的连接上，靠 req_id 而不是顺序找回调用方；
2. **超时表和连接关闭**：每个在途请求都要有「结束」的那一刻——成功、超时或连接关闭，三者必居其一，且只发生一次；
3. **注册、错误模型与协议上限**：服务端怎么分发、错误怎么分层、不可信输入怎么设上限。

## 学习目标

学完本课，你应该能够：

1. **能解释请求ID与响应匹配**（goals 原文）；
2. **能独立完成：实现教学RPC，覆盖并发请求、乱序响应、超时、断连与畸形消息。**（goals 原文）；
3. 画出一次 RPC 调用从 stub 到响应的生命周期，并指出每个阶段登记了什么资源、在哪里释放；
4. 说清「超时」「连接关闭」「晚到响应」三种情况下 pending 表应如何变化，以及为什么必须用 `pop` 保证只完成一次；
5. 区分传输错误、协议错误、应用错误三层，知道哪一层必须关闭连接；
6. 为不可信输入设计协议上限（magic、版本、body 长度、每连接在途数）；
7. 用压测数据证明「不丢不重且资源有界」，并列出教学 RPC 相对生产框架（如 brpc）缺少的能力清单。

## 为什么后端要懂

> why 原文：「将网络和序列化知识串联成可验证系统。」

前面几十课分别学过 TCP 分帧、序列化、事件循环、超时与重试、背压。每一块单独测都没问题，但**组合起来才会出现交互问题**：

- 分帧对了、序列化也对了，但多路复用后响应乱序，FIFO 匹配就错；
- 超时实现了，但超时后没清 pending，晚到响应被投给下一个请求；
- 事件循环跑得很好，但连接断开时没人通知等待者，协程永远挂着；
- 背压做了，但对端发来一个声称 2GB 的头，服务端先把内存分配了。

入职后你多半直接用公司成熟的 RPC 框架，不会从零写一个。但懂它的内部结构，排查问题时才知道去看哪：「偶发串数据」先怀疑匹配；「内存缓慢上涨 + 大量超时」先怀疑 pending/超时表；「下游重启后调用方卡住很久」先怀疑断连通知。

### summary 逐句拆解

「教学RPC把**协议、连接、调度、超时和可观测性**组合起来」——五个部件各自的职责：

| 部件 | 职责 | 本课实践中的实现 |
|---|---|---|
| 协议 | 定义帧边界、请求/响应如何关联、错误如何表示、版本与上限 | 16B 头（magic/version/type/req_id/body_len）+ JSON body |
| 连接 | 建连、在一条连接上多路复用、读写循环、关闭与通知 | asyncio StreamReader/Writer，客户端一个读循环 |
| 调度 | 服务端把帧分发到 handler 并发执行，控制每连接并发 | 方法注册表 + `create_task` + 每连接 64 在途信号量 |
| 超时 | 每个请求有截止时间，到期释放资源并告诉调用方 | `wait_for` + `finally: pending.pop` |
| 可观测性 | 让失败可以被看见：在途数、超时数、晚到响应数、错误码分布 | `pending` 大小、`late_or_unknown`、`peak_inflight` 计数 |

「**复杂度主要来自失败与生命周期管理**」——在配套代码里，正常路径（编帧、发送、查表、回包）只占几十行；剩下的大部分代码都在处理下面这张表：

| 失败情况 | 谁负责发现 | 必须释放 / 通知什么 |
|---|---|---|
| 请求超时 | 客户端超时机制 | 从 pending 删除该 req_id；取消定时器；给调用方 RpcTimeout（但服务端可能已执行） |
| 对端断连（EOF/RST） | 客户端读循环 | 所有 pending 以 ConnectionClosed 失败并清空；拒绝新调用或触发重连 |
| 半包 / 畸形包 | 服务端拆帧 | 回连接级错误（req_id=0）后关闭连接；不能继续在同一字节流上读 |
| 晚到响应 | 客户端读循环 | 查不到 req_id → 丢弃并计数，不能投给别人、不能崩 |
| 服务端 handler 异常 | 服务端分发层 | 转成 INTERNAL 错误回给这一个请求；释放在途名额；连接继续服务 |
| 未注册方法 | 服务端分发层 | 回 METHOD_NOT_FOUND；连接继续可用 |

记住这张表：一个 RPC 框架是否可靠，几乎就取决于每一行有没有被认真处理。

## 核心知识点

### 1. 请求ID与响应匹配

#### 直觉：为什么要在一条连接上跑很多请求

文章服务每秒要调用计数服务几千次。如果每个请求一条短连接，就要付出三次握手、慢启动、TIME_WAIT 的成本（回顾 d-pool）；如果一条连接上「一问一答」严格串行，那这条连接的吞吐就是 `1 / RTT+处理时间`——处理 15ms 的请求，一条连接每秒最多 66 次。

所以 RPC 框架几乎都会在一条连接上**多路复用**：不等上一个响应回来就发下一个请求，同时有几十上百个请求「在途」。

#### FIFO 匹配什么时候成立

「按发送顺序匹配响应」只在一个前提下正确：**服务端保证按接收顺序回包**。例如严格一问一答，或 HTTP/1.1 pipelining 那样协议规定响应必须按请求顺序返回——代价是前面一个慢请求会堵住后面所有请求（队头阻塞）。

只要服务端并发处理请求（这正是我们想要的），完成顺序就由处理时长决定：

```
发送顺序:   A(查存储 15ms)  B(命中缓存 1ms)  C(5ms)
完成顺序:   B → C → A
FIFO 匹配:  A←B的结果  B←C的结果  C←A的结果     全错，且没有任何报错
```

#### req_id 的设计

给每个请求一个编号，服务端原样带回，客户端靠编号找人：

- **作用域**：连接内唯一即可（不同连接各有自己的 pending 表），用一个连接级计数器 `itertools.count(1)`；
- **单调递增、不复用**：64 位计数器在连接生命周期内实际不会回绕。如果复用了刚超时请求的编号，晚到的旧响应会被投给新请求；
- **保留 0**：用于「连接级」消息，例如协议错误通知，它不属于任何一个请求；
- **只在本端有意义**：服务端不应该解释 req_id，只负责带回。

#### pending 表：登记、取出、只完成一次

```python
self.pending = {}                      # req_id -> Future

# 发送前登记（先登记后发送，否则极快的响应可能先于登记到达）
rid = next(self.ids)
fut = loop.create_future()
self.pending[rid] = fut
writer.write(encode(T_REQ, rid, body))

# 读循环收到响应
fut = self.pending.pop(rid, None)      # pop，不是 get
if fut is None or fut.done():
    self.stat['late_or_unknown'] += 1  # 晚到、未知、重复：丢弃 + 计数
    continue
fut.set_result(result)
```

为什么是 `pop`：一个 req_id 对应的 Future 只能被完成一次。用 `get` 的话，服务端 bug 发了两次同一个响应，第二次 `set_result` 会抛 `InvalidStateError`，把整个读循环搞崩——一个请求的问题变成整条连接的问题。

三种「找不到主人」的响应都要**丢弃并计数**，而不是崩溃或投给别人：

| 情况 | 原因 | 处理 |
|---|---|---|
| 晚到响应 | 客户端已超时并删除了 pending | 丢弃，`late` 计数 +1（这个指标升高说明超时设得太紧或下游变慢） |
| 未知 req_id | 对端 bug、协议错乱 | 丢弃并计数；大量出现可考虑断开连接 |
| 重复响应 | 对端重复发送 | `pop` 后已不存在，自然丢弃 |

#### 帧格式与拆帧

本课实践用一个 16 字节的定长头：

```
 0      2   3   4                12              16
 +------+---+---+----------------+---------------+------------------+
 | 'RP' |ver|typ|    req_id      |   body_len    |   body (JSON)    |
 +------+---+---+----------------+---------------+------------------+
  magic  1B  1B   8B 无符号        4B 无符号          body_len 字节
```

| 字段 | 作用 |
|---|---|
| magic `RP` | 快速识别「这不是我的协议」（比如有人拿 HTTP 客户端打过来，前两个字节是 `GE`） |
| version | 协议版本，为演进留口子（回顾 f-evolution） |
| type | 0 请求 / 1 响应 / 2 错误 |
| req_id | 请求编号，响应原样带回 |
| body_len | 后面 body 的长度，解决粘包/半包 |

```python
import struct
HDR = struct.Struct('!2sBBQI')        # ! 网络序；2s+B+B+Q+I = 2+1+1+8+4 = 16
frame = HDR.pack(b'RP', 1, 0, 1007, len(body)) + body
```

拆帧循环（服务端和客户端共用）：

```
loop:
    hdr  = 读满 16 字节        # 不够就等（半包）
    校验 magic / version / body_len <= 上限    ← 先校验，再决定读多少
    body = 读满 body_len 字节
    交给上层；剩下的字节属于下一帧（粘包）
```

asyncio 的 `readexactly(n)` 正好就是「读满 n 字节，不够就等」。在 C++ 的 reactor 里，这一步变成：可读事件到来 → 读进连接缓冲区 → 循环尝试从缓冲区切出完整帧 → 不够就保留在缓冲区等下一次可读（回顾 d-reactor / d-framing）。

#### 客户端核心骨架

```python
async def call(self, method, params, timeout):
    if self.closed: raise ConnectionClosed(self.closed)
    rid = next(self.ids)
    fut = loop.create_future()
    self.pending[rid] = fut
    self.writer.write(encode(T_REQ, rid, {'method': method, 'params': params}))
    try:
        await self.writer.drain()
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    finally:
        self.pending.pop(rid, None)

async def _read_loop(self):
    try:
        while True:
            ftype, rid, obj = await read_frame(self.reader)
            fut = self.pending.pop(rid, None)
            if fut is None or fut.done(): self.stat['late_or_unknown'] += 1; continue
            if ftype == T_RESP: fut.set_result(obj['result'])
            else:               fut.set_exception(RpcError(obj['code']))
    finally:
        self._fail_all(...)
```

一个连接只有**一个**读循环（所有响应都从这里进来再分发），写可以来自很多协程（需要保证一帧不被另一帧插进来——asyncio 单线程下一次 `write` 整帧即可；多线程 C++ 实现需要写队列）。

### 2. 超时表和连接关闭

#### 每个在途请求都必须「结束」

把 pending 表里的每一项看成一个**有生命周期的资源**。它必然以下面三种方式之一结束，而且**只结束一次**：

```
            ┌── 响应到达 ──────▶ 成功 / 应用错误
登记 ───────┼── 截止时间到 ────▶ RpcTimeout
            └── 连接关闭 ──────▶ ConnectionClosed
```

漏掉任何一条出路，都会出现「永远在途」的条目：内存只增不减，调用方协程永远挂起。

#### 超时怎么实现

- **每请求一个定时器**：asyncio 的 `wait_for`，简单直接，本课实践就用它；
- **定时器堆**：所有截止时间放进最小堆，一个定时任务每次只看堆顶；
- **时间轮**：把时间切成槽，请求按截止时间落槽，每个 tick 处理一个槽，适合海量请求、精度要求不高的场景。

大量在途请求时选哪种以具体框架实现为准；语义上它们要做的事一样：到期时**从 pending 删除** + 通知调用方。

```python
try:
    return await asyncio.wait_for(asyncio.shield(fut), timeout)
except asyncio.TimeoutError:
    self.stat['timeout'] += 1
    raise RpcTimeout(...)
finally:
    self.pending.pop(rid, None)     # 关键：超时也删
```

实践对照：10 个 300ms 的请求用 100ms 超时。safe 超时后 pending=0，之后的 20 个 echo 全对，10 个晚到响应被计入 `late_or_unknown`；naive 不删，20 个 echo **全部拿到错位结果**。

#### 超时 ≠ 没执行

客户端超时只代表「我不等了」。服务端可能还没收到、正在执行、或已经执行完只是响应在路上。实践中那 10 个 slow 请求服务端都执行完了。所以超时后能否重试，取决于接口是否幂等（回顾 f-retry、f-idempotency）。理想情况下还应该把剩余时间（deadline）传给服务端，让它在调用方已放弃时不再白做（回顾 f-context）。

#### 连接关闭：fail_all

连接可能因为对端进程重启（EOF）、被内核 RST、网络中断、读到畸形帧而结束。此时**这条连接上的所有在途请求都不可能再收到响应了**，必须立刻全部失败：

```python
def _fail_all(self, reason):
    self.closed = reason                       # 之后的 call() 直接快速失败
    for rid, fut in list(self.pending.items()):
        if not fut.done():
            fut.set_exception(ConnectionClosed(reason))
    self.pending.clear()
```

放在读循环的 `finally` 里，保证不管读循环因为什么退出都会执行。实践对照：20 个 2s 请求在途时服务端断开，safe 20 个全部立即收到 ConnectionClosed；naive 1 秒后 20 个仍然挂着——在生产里，这就是「下游重启一下，上游一堆请求卡到超时（甚至永远）」。

#### 竞态：读循环退出 vs 正在 call 的协程

`fail_all` 执行的同时，可能有协程正要 `call()`：它检查 `self.closed` 时还是 None，刚登记完 pending，读循环就退出了并清空了表——这个新请求就漏掉了。处理思路：

- 登记前检查 `closed`，登记后**再检查一次**（或在 C++ 里用同一把锁保护「检查状态 + 登记」）；
- 写失败（连接已关）时也要立即把自己从 pending 删除并失败；
- 单线程 asyncio 中检查与登记之间没有 `await` 就不会被打断，这是本实践能写得简单的原因。

#### 优雅关闭

主动关闭（发布、缩容）时不应该直接断：

1. 停止接收新连接与新请求（服务端停 accept、停读新帧；客户端标记不可用，新调用走别的实例）；
2. 等在途请求完成，最多等一个上限时间；
3. 超过上限仍未完成的，按连接关闭处理（fail_all）；
4. 关闭连接、释放资源。

#### 可观测性

这些指标让上面的生命周期「看得见」：

| 指标 | 看什么 |
|---|---|
| pending 数（在途数） | 持续上涨 = 超时没清理或下游卡住 |
| 超时数 / 超时率 | 下游变慢或超时设置不合理 |
| 晚到响应数 | 与超时数对照：超时后响应还会来 → 下游其实在执行 |
| ConnectionClosed 次数 | 下游重启、网络抖动 |
| 按方法的延迟直方图 | 哪个方法拖慢了整个连接 |

### 3. 注册、错误模型与协议上限

#### 方法注册表

服务端把「方法名 → 处理函数」放进一张表，拆帧后查表分发：

```python
REGISTRY = {'get_read_count': get_read_count, 'echo': echo, ...}

h = REGISTRY.get(method)
if h is None:
    send_error(req_id, 'METHOD_NOT_FOUND')     # 回错误，不断开连接
```

未注册方法是**调用方的问题**，而且只涉及这一个请求：可能只是调用方比服务端先上线了新方法（灰度期很常见，回顾 f-evolution）。为此断开整条连接，会把同连接上其他正常请求一起拖下水。

生产框架里注册表通常由 IDL（如 .proto 的 service 定义）生成，还会挂上每个方法的并发上限、超时默认值、鉴权要求等元数据。

#### 错误模型分三层

| 层 | 例子 | 调用方知道什么 | 连接怎么办 |
|---|---|---|---|
| 传输错误 | 超时、连接断开 | **不知道服务端是否执行了** | 断开的连接不能再用；超时的连接仍可用 |
| 协议错误 | magic 错、版本不支持、body 超长、body 不是合法 JSON | 请求没被处理 | **必须关闭**：帧边界已不可信 |
| 应用错误 | METHOD_NOT_FOUND、INTERNAL、业务错误码（如「文章不存在」） | 服务端收到并处理了 | 继续可用 |

本课实践的错误码：

| 错误码 | 层 | 含义 |
|---|---|---|
| `BAD_MAGIC` / `BAD_VERSION` / `FRAME_TOO_LARGE` / `BAD_BODY` | 协议 | 回给 req_id=0 后关闭连接 |
| `METHOD_NOT_FOUND` | 应用 | 方法未注册 |
| `INTERNAL` | 应用 | handler 抛出了未处理的异常 |
| `RpcTimeout` / `ConnectionClosed` | 传输 | 客户端本地产生，不在线上传输 |

为什么协议错误必须断开：长度前缀协议靠「头里的长度」找到下一帧的起点。头本身已经不可信时，接下来的字节从哪开始是下一帧已无从判断，继续读只会把垃圾当成请求。实践中 naive 服务端对 version=9 的帧照常处理、回了一个 METHOD_NOT_FOUND，就是这种「把垃圾当请求」。

#### handler 异常不能杀死连接

```python
try:
    res = await h(params)
except Exception as e:
    send_error(req_id, 'INTERNAL')     # 只影响这一个请求
```

一个方法的 bug（空指针、除零、依赖返回意外数据）只应让这一个请求失败。实践中 naive 服务端 handler 异常就 abort 连接，同连接上无辜的 11 个请求全部失败；safe 只有 boom 收到 INTERNAL，其他 10 个正常完成。（在 C++ 里还要考虑 handler 崩进程的情况，这超出了框架能兜住的范围，需要进程隔离与监控。）

#### 协议上限：不可信输入一律设上限

网络另一端发来的每个字节都是不可信的——可能是 bug、版本不匹配、扫描器，也可能是攻击。

| 上限 | 防什么 | 实践中的做法 |
|---|---|---|
| body_len 上限 | 头部声称 2GB，服务端按此分配内存或一直等待 | 1MB，超过回 `FRAME_TOO_LARGE` 并关闭 |
| magic / 版本校验 | 非本协议流量、不兼容版本 | 读完头立即校验 |
| 每连接最大在途请求 | 单个客户端发海量请求占满服务端 | 64，满了暂停读（背压，回顾 f-backpressure） |
| 读超时 / 空闲超时 | 慢速攻击：每隔几秒发一个字节，占住连接 | 本实践未实现，属于缺失能力 |
| 最大连接数 | 连接耗尽 fd 和内存 | 本实践未实现 |

```python
magic, ver, ftype, req_id, blen = HDR.unpack(hdr)
if magic != MAGIC:   raise ProtocolError('BAD_MAGIC')
if ver != VERSION:   raise ProtocolError('BAD_VERSION')
if blen > MAX_BODY:  raise ProtocolError('FRAME_TOO_LARGE')
body = await reader.readexactly(blen)   # 校验通过后才读 body
```

实践对照：`body_len=0x7FFFFFFF` 的帧，safe 0.4ms 内回 FRAME_TOO_LARGE 并关闭；naive 把连接挂住 1 秒毫无回应——如果实现是「先按长度分配缓冲区」，就是一颗内存炸弹。

### 三条主线的收束

- **请求ID与响应匹配**解决「结果给谁」：req_id + pending 表 + pop，只完成一次；
- **超时表和连接关闭**解决「什么时候结束」：每个在途请求必有出路，超时删、断连全失败；
- **注册、错误模型与协议上限**解决「出错怎么分层、坏输入怎么挡」：应用错误留连接，协议错误断连接，所有不可信长度都有上限。

三者合起来，就是 summary 说的：复杂度主要来自失败与生命周期管理。

## 常见误区

> pitfalls 原文：「能完成Echo调用不代表具备生产RPC可靠性。」

Echo 测试通常是：一个客户端、一个请求、服务端立即原样返回、连接全程不断。它只覆盖了 happy path 的那 10 步，而本课的失败路径表一行都没测到——没有并发（测不出匹配错误）、没有慢请求（测不出超时清理）、没有断连（测不出 fail_all）、没有坏输入（测不出协议上限）。实践中 naive 实现完全能通过 Echo 测试，但在 6 组对照里每一组都出问题。

### 误区 1：按发送顺序匹配响应

- **误区**：「我发的是 A、B、C，回来的肯定也是 A、B、C。」
- **后果**：服务端并发处理时响应乱序，结果张冠李戴，而且没有任何报错。实践中 5000 个请求 4989 个错配，错误率监控却是 0。
- **正确做法**：每个请求带连接内唯一的 req_id，服务端原样带回，客户端用 pending 表 `pop(req_id)` 匹配。

### 误区 2：超时只返回错误，不删 pending

- **误区**：「超时了就给调用方抛个异常，任务完成。」
- **后果**：pending 只增不减（下游变慢时内存持续上涨）；晚到响应可能被误投（在队列式实现中，实践里超时后 20 个 echo 全部拿错结果）；在途数指标失真。
- **正确做法**：在 `finally` 里删除；晚到响应丢弃并计数；同时记住超时不代表没执行。

### 误区 3：断连后不通知等待者

- **误区**：读循环遇到 EOF 就安静退出。
- **后果**：在途请求永远等不到响应。没设超时的调用永远挂起；设了超时的要白等到超时，下游重启一次，上游就有一批请求卡满超时时间。实践中 naive 20 个请求 1 秒后仍全部挂着。
- **正确做法**：读循环 `finally` 中 fail_all，标记连接不可用，新调用快速失败或重连。

### 误区 4：信任对端头部的长度字段

- **误区**：头里说 body 多长就读多长、分配多大。
- **后果**：一个 `body_len=2GB` 的帧就能让服务端分配巨量内存或一直等待；大量这种连接能拖垮服务。
- **正确做法**：读完头先校验 magic、版本、长度上限，不通过就回错误并关闭连接。

### 误区 5：handler 异常拖垮连接或进程

- **误区**：handler 里的异常没人接，冒泡到读循环。
- **后果**：一个方法的 bug 让整条连接上所有请求失败（实践中同连接 11 个无辜请求陪葬）；更糟时整个进程退出。
- **正确做法**：分发层捕获异常，转成 INTERNAL 回给这一个请求，其他请求不受影响；同时打日志、计数报警。

### 误区 6：把协议错误当应用错误，继续读同一连接

- **误区**：「magic 不对？回个错误码，接着读下一帧。」
- **后果**：帧边界已不可信，后续字节被错误切分，可能把半截数据当成请求执行（实践中 naive 对 version=9 的帧照常处理）。
- **正确做法**：协议错误回一个连接级错误（req_id=0）后关闭连接；应用错误才保留连接。

## 设计权衡

> tradeoffs 原文：「自研有教学价值，生产采用须评估生态、维护和安全成本。」

写一遍教学 RPC 的价值在于：你会亲手碰到匹配、超时、断连、畸形输入这些问题，以后读成熟框架的源码或排查线上问题时，知道每个机制在防什么。但「能跑」到「能在生产用」之间还有很长的距离。

### 教学RPC 已有 vs 生产 RPC 还需要（验收：明确缺少哪些生产能力）

| 能力 | 本课教学RPC | 生产 RPC 通常还需要 |
|---|---|---|
| 请求匹配 | req_id + pending 表 | 同左，另需高并发下的无锁/分片实现 |
| 超时 | 每请求 `wait_for` | 定时器堆/时间轮；deadline 跨服务传递 |
| 断连 | fail_all + 快速失败 | 自动重连、健康检查、熔断摘除 |
| 服务发现与负载均衡 | 无，写死地址 | 名字服务、多种 LB 策略（回顾 f-discovery、f-lb） |
| 重试与熔断 | 无 | 重试预算、退避、backup request、熔断（回顾 f-retry） |
| 安全 | 无，任何人都能调 | TLS/mTLS、身份鉴权、方法级授权（回顾 f-security） |
| 限流与背压 | 每连接 64 在途 | 全局限流、按调用方配额、优先级 |
| 序列化与兼容 | JSON，无 schema | IDL（protobuf 等）、兼容性检查（回顾 f-evolution） |
| 协议 | 单一自定义协议 | 多协议共端口、与 HTTP/gRPC 互通 |
| 流式与大附件 | 无，单帧 1MB | 流式 RPC、附件、压缩 |
| 性能 | 单线程 asyncio | 多线程/协程调度、零拷贝缓冲、批量写 |
| 可观测性 | 几个计数器 | 分方法延迟直方图、链路追踪、内置诊断页面 |
| 生命周期 | 无优雅关闭 | 优雅启停、连接排空 |
| 工程配套 | 一个 test.sh | 压测与混沌工具、多语言 SDK、运维文档、安全漏洞响应 |

### 自研 vs 采用成熟框架

| 维度 | 自研 | 采用成熟框架（如 brpc、gRPC） |
|---|---|---|
| 生态 | 需要自己做服务发现、监控、各语言客户端对接 | 已有大量插件、工具与使用经验 |
| 维护人力 | 长期需要专人修 bug、做性能优化、跟进新需求 | 社区/上游共同维护，团队只需跟进版本 |
| 安全响应 | 协议解析漏洞要自己发现、自己修 | 有公开的漏洞披露与修复流程（仍需及时升级） |
| 定制性 | 想怎么改就怎么改 | 受框架扩展点限制，深度定制要读懂源码 |
| 踩坑成本 | 本课所有失败路径都要自己踩一遍 | 很多坑已被前人踩过并修复 |

（以上为一般经验，具体取舍以团队情况为准。）

### 什么时候适合自研

- **教学与理解**：本课的目的；
- **极特殊的协议或环境**：例如嵌入式设备、极度受限的资源、与某个遗留系统的私有协议对接；
- **团队已有长期维护能力**：大公司往往有自己的 RPC 框架，背后是专门的团队和多年的线上打磨——这不是「写一个」，而是「养一个」。

入职后更常见的情况是：使用公司已有框架，遇到问题时能读懂它在匹配、超时、断连、上限上做了什么——这正是本课和源码阅读环节要练的能力。

## 动手实践：实现教学RPC，并用 naive vs safe 对照验证

> 节点 practice 原文：「实现教学RPC，覆盖并发请求、乱序响应、超时、断连与畸形消息。」
> 验收（acceptance）：「压测不丢不重且资源有界」「明确缺少哪些生产能力」。
>
> 配套代码在 `practice96/`（打包为 `practice96.tar.gz`），纯 Python 3 标准库（asyncio），无需安装任何依赖。下文所有数字均来自本机真实运行 `./test.sh` 的输出（2026-11-11，Python 3，回环地址 127.0.0.1）；你机器上的耗时会不同，但 PASS/FAIL 结论应一致。

### 0. 文件结构

```
practice96/
├── proto.py      # 帧格式：16B 头 + JSON body；编码、拆帧、协议校验、错误码
├── server.py     # 服务端：方法注册表、并发分发、错误模型、协议上限、每连接在途上限；naive|safe 两种模式
├── client.py     # SafeClient（req_id + pending 表 + 超时清理 + fail_all）与 NaiveClient（FIFO 匹配）
├── test_rpc.py   # 6 组对照实验 + 断言
└── test.sh       # 同时启动 safe(19601) 与 naive(19602) 两个服务端，跑 test_rpc.py
```

运行：

```bash
tar xzf practice96.tar.gz && cd practice96
./test.sh            # 约 8 秒，最后一行应为 ALL_CHECKS_PASSED
# 端口冲突时：SP=29601 NP=29602 ./test.sh
```

### 步骤 1：定义帧格式（proto.py）

```python
HDR = struct.Struct('!2sBBQI')   # magic 2B | version 1B | type 1B | req_id 8B | body_len 4B = 16 字节，网络序
MAGIC, VERSION = b'RP', 1
T_REQ, T_RESP, T_ERR = 0, 1, 2
MAX_BODY = 1 << 20               # 1MB 协议上限

async def read_frame(reader, max_body=MAX_BODY, check=True):
    hdr = await reader.readexactly(HDR.size)              # 1) 读满头
    magic, ver, ftype, req_id, blen = HDR.unpack(hdr)
    if check:                                             # 2) 先校验，再决定读多少
        if magic != MAGIC: raise ProtocolError(E_BAD_MAGIC, repr(magic))
        if ver != VERSION: raise ProtocolError(E_BAD_VERSION, str(ver))
        if blen > max_body: raise ProtocolError(E_FRAME_TOO_LARGE, str(blen))
    body = await reader.readexactly(blen)                 # 3) 读满 body
    ...
```

要点：`readexactly` 自动处理半包（不够就继续等），粘包则由「长度前缀」切开（回顾 d-framing）。`check=False` 是 naive 版本——头里写多长就信多长。

### 步骤 2：客户端 pending 表与 req_id 匹配（client.py / SafeClient）

```python
async def call(self, method, params=None, timeout=1.0):
    if self.closed: raise ConnectionClosed(self.closed)    # 连接已死：快速失败
    rid = next(self.ids)                                   # 连接内单调递增，从 1 开始，0 保留
    fut = asyncio.get_running_loop().create_future()
    self.pending[rid] = fut                                # 先登记，再发送（否则响应可能先到却找不到人）
    self.writer.write(encode(T_REQ, rid, {...}))
    try:
        await self.writer.drain()
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError:
        raise RpcTimeout(...)
    finally:
        self.pending.pop(rid, None)                        # 成功/超时/断连都删除 -> pending 有界

async def _read_loop(self):
    try:
        while True:
            ftype, rid, obj = await read_frame(self.reader)
            fut = self.pending.pop(rid, None)              # pop 保证同一个 req_id 只完成一次
            if fut is None or fut.done():
                self.stat['late_or_unknown'] += 1          # 晚到/未知/重复：丢弃 + 计数，不崩
                continue
            ...set_result / set_exception(RpcError(code))
    except (IncompleteReadError, ConnectionError, ProtocolError) as e:
        reason = type(e).__name__
    finally:
        self._fail_all(reason)                             # 连接没了：唤醒所有等待者
```

NaiveClient 是常见错误写法的合集：所有请求的 Future 放进一个 `deque`，收到任何响应就 `popleft()` 给最早的请求；超时只抛异常、不出队；读循环出错静默退出、不通知任何人。

### 步骤 3：服务端注册表、错误模型与上限（server.py）

```python
REGISTRY = {'get_read_count': ..., 'echo': ..., 'slow': ..., 'boom': ..., 'stats': ...}

while True:
    if safe: await sem.acquire()              # 每连接在途 >= 64 时先不读 -> 背压传回 TCP 窗口
    try:
        ftype, req_id, obj = await read_frame(reader, check=safe)
    except ProtocolError as e:                # 协议错误：帧边界已不可信
        await send(encode(T_ERR, 0, {'code': e.code, ...}))   # req_id=0：连接级错误
        break                                 # 回一个错误后关闭连接，绝不继续读
    create_task(run(req_id, obj))             # 并发处理 -> 响应天然乱序

# run() 里：
h = REGISTRY.get(m)
if h is None: -> T_ERR METHOD_NOT_FOUND（应用层错误，连接继续可用）
try: res = await h(params)
except Exception as e: -> T_ERR INTERNAL（handler bug 只影响这一个请求）
```

`get_read_count` 是教学假设的「计数服务」：随机睡 0~20ms 后返回 `read_count = article_id * 7`，于是同一连接上的响应顺序与请求顺序不同，正确答案又可以被客户端校验。naive 模式下 handler 抛异常会 `abort()` 整条连接，且不做任何协议校验。

### 步骤 4：运行并解读 6 组对照（真实输出）

**实验 1｜单连接并发 5000 请求（最多 200 在途），服务端乱序回包**

```
naive {'n': 5000, 'mismatch': 4989, 'errors': 0, 'answered': 5000, 'dup': 0, 'pending_after': 0, 'peak_pending': 200, 'secs': 0.3}
safe  {'n': 5000, 'mismatch': 0,    'errors': 0, 'answered': 5000, 'dup': 0, 'pending_after': 0, 'peak_pending': 200, 'secs': 0.85}
```

- naive「每个请求都拿到了响应、没有报错」，但 5000 个里 4989 个拿到的是**别的文章**的阅读数（三次运行分别为 4989/4990/4982）。这是最危险的失败：监控上错误率 0，业务数据全错——相当于把 A 文章的阅读数显示到 B 文章上。
- safe 0 错配、0 错误、5000 个全部应答、0 重复，pending 峰值 200（= 调用方并发上限）且结束后归零 —— 对应验收「压测不丢不重且资源有界」。

**实验 2｜10 个 slow(300ms) 用 100ms 超时，超时后立刻并发 20 个 echo**

```
naive {'slow_timeouts': 10, 'pending_after_timeout': 10, 'echo_ok': 0,  'echo_wrong': 20, 'late': 0}
safe  {'slow_timeouts': 10, 'pending_after_timeout': 0,  'echo_ok': 20, 'echo_wrong': 0,  'late': 10, 'pending_final': 0}
```

- naive 超时后 10 个「僵尸」Future 还排在队头，20 个 echo 的响应被它们吞掉，echo 拿到的是错位的结果：**20 个全错**。
- safe 超时即从 pending 删除（`pending_after_timeout=0`）；300ms 后到达的 10 个 slow 响应找不到主人，被丢弃并计入 `late_or_unknown=10`；20 个 echo 全对。
- 注意：slow 的超时只是客户端「不等了」，服务端其实执行完了——超时 ≠ 没执行（回扣 f-retry / f-idempotency）。

**实验 3｜20 个 slow(2s) 在途时服务端断开连接**

```
naive {'woken': 0,  'hung_after_1s': 20, 'pending_after': 21, 'new_call': 'RpcTimeout'}
safe  {'woken': 20, 'hung_after_1s': 0,  'closed_errors': 20, 'pending_after': 0, 'new_call': 'ConnectionClosed'}
```

- naive 的 20 个调用（timeout=None）1 秒后仍全部挂着，没人通知；断连后的新调用也只能干等到自己的超时（RpcTimeout），而且又往队列里塞了一个 —— `pending_after=21`，泄漏只增不减。
- safe 读循环收到 EOF 后 `fail_all`：20 个等待者全部立即以 `ConnectionClosed` 失败，pending 归零；之后的新调用不发送、直接快速失败，上层可以据此重连或切换实例（回扣 f-discovery / f-lb）。

**实验 4｜畸形帧（用原始 socket 发送）**

| 输入 | naive 服务端 | safe 服务端 |
|---|---|---|
| `body_len=0x7FFFFFFF`（声称 2GB） | 无回应，连接被挂住 1000ms（直到客户端放弃） | `FRAME_TOO_LARGE` 并关闭，0.4ms |
| magic=`GE`（比如有人用 HTTP 客户端打过来） | 读完 5 字节后 JSON 解析失败，静默关连接，调用方不知道原因 | `BAD_MAGIC` 并关闭，0.1ms |
| version=9 | **当成正常请求处理**，回了 `METHOD_NOT_FOUND`，连接保持 | `BAD_VERSION` 并关闭，0.1ms |

safe 处理完 3 个畸形连接后，正常客户端调用 echo 仍然成功（服务没被拖垮）。naive 对 2GB 长度头的处理更糟：本实验里 readexactly 只是一直等数据，如果实现是「先按头部长度分配缓冲区」，就直接变成内存炸弹；攻击者只要开很多这种连接就能占满连接与内存。

**实验 5｜同一连接：1 个 boom（handler 抛异常）+ 1 个未注册方法 + 10 个正常调用**

```
naive {'boom': ('fail', 'RpcTimeout'), 'unknown': ('fail', 'RpcTimeout'), 'echo_ok': 0}
safe  {'boom': ('rpcerr', 'INTERNAL'), 'unknown': ('rpcerr', 'METHOD_NOT_FOUND'), 'echo_ok': 10}
```

naive：一个 handler 的 bug 把整条连接 abort，同连接上无辜的 11 个请求一起陪葬（而 naive 客户端又不会被通知，只能等超时）。safe：异常转成 `INTERNAL`、未注册方法转成 `METHOD_NOT_FOUND`，都是应用层错误，连接照常服务其余 10 个请求。

**实验 6｜每连接在途上限（背压）**：同一连接一次性发 300 个 slow(200ms)，safe 服务端记录的同时执行 handler 峰值 `peak_inflight = 64`，正好等于 `MAX_INFLIGHT`。超过上限时服务端暂停读 socket，多出来的请求留在内核缓冲 / TCP 窗口里，压力传回客户端（回扣 f-backpressure）。

最终输出：`ALL_CHECKS_PASSED`（连续运行 3 次结果一致）。

### 步骤 5：写出「还缺什么」清单（验收第二条）

跑通之后，对照「设计权衡」中的表，逐条写下本教学 RPC **明确没有**的东西，例如：

- 没有服务发现、负载均衡、连接池与重连（断连后只会 ConnectionClosed）；
- 没有重试、熔断、deadline 跨跳传递（只有本地超时）；
- 没有 TLS / 鉴权 / 限流（任何人连上都能调）；
- JSON body，没有 schema 与兼容演进约束（回扣 f-evolution）；
- 没有流式、大附件、压缩，单帧 1MB 上限；
- 单线程 asyncio，没有多核扩展与零拷贝；
- 只有几个计数器，没有分方法延迟直方图、链路追踪；
- 没有优雅关闭（服务端停机时在途请求直接断）。

### 步骤 6：自己动手的扩展练习

1. 把 `SafeClient` 的 `wait_for` 换成「最小堆 + 单个定时任务」的超时表，压测 10 万在途请求时比较 CPU 占用。
2. 给服务端加优雅关闭：收到 SIGTERM 后停止 accept 与读新帧，等在途请求完成（最多 N 秒）后再关连接；改写实验 3 验证客户端不再收到 ConnectionClosed。
3. 给 `stats` 增加按方法的延迟分布与 `late_or_unknown` 指标，故意注入慢 handler 观察。
4. 用 `ss -tnp | grep 1960` 观察实验 6 中客户端 Send-Q 的堆积，直观看到背压。

## 源码阅读

> sourceStudy 原文：「阅读brpc的input_messenger、socket和controller相关实现，逐一对照教学RPC缺少的治理能力。」

入口：<https://github.com/apache/brpc/tree/master/src/brpc>。下面只给「带着什么问题去读」，具体文件名、类名和函数名**以所读 commit 为准**，请在仓库里自己定位并记录，不要背诵二手描述。

**1. input_messenger 相关实现（对照：教学RPC 的 `read_frame` + 服务端读循环 + 客户端 `_read_loop`）**

- 建议关注：连接可读后，字节是怎样被读进缓冲、再被切成一条条消息的？
- 一个端口上可以支持多种协议时，它如何尝试识别「这段字节属于哪种协议」？识别失败、数据不完整、数据明显错误分别怎么处理（继续等 / 关闭连接）？
- 切出的消息是在当前线程处理，还是交给其他执行单元？
- 记录待验证：它与我们「协议错误 → 回错误并关闭连接」的策略是否一致？

**2. socket 相关实现（对照：教学RPC 的连接对象 + `fail_all`）**

- 建议关注：一个连接从建立、可用、失败到回收的状态如何表示？
- 多个线程同时往一个连接写时，写请求如何排队、谁负责真正写到 fd？（教学RPC 只用了一个 asyncio 锁）
- 连接失败时，在途请求是如何被通知的？连接对象被很多地方引用时，如何保证不会「用到已释放的连接」？（找到引用计数/延迟回收相关的位置并记录）
- 记录待验证：断连后在途调用是立刻失败还是触发重试？

**3. controller 相关实现（对照：教学RPC 的 `call()` 里的 Future + 超时 + 错误码）**

- 建议关注：一次调用的状态（超时、重试次数、错误码、错误文本、远端地址、耗时）存在哪里？
- 超时、响应到达、连接失败几乎同时发生时，如何保证完成回调**只触发一次**？（找到这段逻辑并记录它用的手段）
- 调用如何被取消？重试和 backup request 在哪里决定？
- 记录待验证：它的错误码如何区分「传输层失败」与「服务端返回的业务错误」？

**阅读笔记模板**

| 部件 | brpc 文件位置（commit: ______） | 机制要点 | 教学RPC 对应 | 教学RPC 缺失能力 |
|---|---|---|---|---|
| 拆帧/协议识别 | | | `proto.read_frame` | 多协议识别、零拷贝缓冲 |
| 连接生命周期 | | | `SafeClient._read_loop/_fail_all` | 重连、健康检查、引用计数回收 |
| 写队列 | | | `wlock + drain` | 多线程无锁写、批量写 |
| 调用状态 | | | `call()` 的 Future + `wait_for` | 重试、backup request、取消、deadline 传递 |
| 错误码 | | | `RpcError.code` | 完整错误码体系、跨语言一致 |

记录：仓库 commit 号、访问日期、你看的文件列表。边界：C++ 源码细节以所读 commit 为准；本课的 Python 实验只模拟语义，不代表 brpc 的实现方式与性能。

## 自测题

**题 1**：客户端在一条连接上并发发出请求 A（req_id=11）和 B（req_id=12），服务端先完成 B 并回包。若客户端按 FIFO 匹配会怎样？用 req_id 匹配时 pending 表如何操作？晚到/重复响应怎么处理？

<details><summary>答案</summary>

- FIFO：B 的响应被交给 A，A 的响应再交给 B——两个调用都「成功」但结果互换，错误率监控为 0。实践中 5000 个并发请求有 4989 个错配。
- req_id 匹配：发送前 `pending[11]=futA, pending[12]=futB`；收到 req_id=12 的响应时 `pending.pop(12)` 取出 futB 完成；之后 req_id=11 同理。
- 晚到（已超时被删）、未知或重复的 req_id：`pop` 返回 None（或 Future 已 done）→ 丢弃并计数（如 `late_or_unknown`），不能投给别人，也不能让读循环崩溃。

解析：`pop` 而不是 `get`，保证一个 req_id 只完成一次；req_id 在连接内单调递增、不复用，晚到响应才不会撞上新请求。
</details>

**题 2**：某请求设置 200ms 超时，客户端在 200ms 返回错误，但服务端响应在 500ms 到达。如果超时时没有从 pending 删除，会出现哪些问题？连接断开时还要做什么？

<details><summary>答案</summary>

- 不删除的问题：① pending 只增不减，下游变慢时内存持续上涨；② 若用 FIFO/队列，僵尸条目会吞掉后续请求的响应，造成误投（实践中超时后 20 个 echo 全部拿到错位结果）；③ 可观测性失真——看不出哪些是真在途。
- 正确做法：超时（以及成功、断连）都在 `finally` 中 `pending.pop(rid)`；500ms 到达的响应查不到主人，丢弃并计数。还要记住超时 ≠ 服务端没执行，重试需要幂等。
- 连接断开时：读循环退出后 `fail_all`——把所有 pending 以 ConnectionClosed 失败并清空，取消相关定时器，标记连接不可用，让新调用快速失败或触发重连。实践中 safe 20 个等待者立即被唤醒，naive 1 秒后仍全部挂起。
</details>

**题 3**：服务端收到头部 `body_len=0x7FFFFFFF`，或 magic 错误的帧，应如何处理？为什么不能只回个错误继续读这个连接？请说明错误模型的三层。

<details><summary>答案</summary>

- 处理：读完头立即校验 magic、版本和 body_len 上限（如 1MB），不通过就回一个连接级错误帧（如 req_id=0，`FRAME_TOO_LARGE`/`BAD_MAGIC`）然后关闭连接；绝不按头部长度分配内存或等待 2GB 数据。实践中 safe 0.4ms 内回错并关闭，naive 挂住连接 1 秒无回应。
- 不能继续读：长度前缀协议靠「头里的长度」找下一帧的起点，头本身不可信时，后面的字节流从哪里开始是下一帧已经无法判断，继续读只会把垃圾当请求（naive 对 version=9 的帧照常处理就是例子）。
- 三层错误：**传输错误**（超时、断连——调用方不知道服务端是否执行，需结合幂等决定重试）；**协议错误**（畸形帧、版本不支持——帧边界不可信，必须关闭连接）；**应用错误**（METHOD_NOT_FOUND、INTERNAL、业务错误码——只影响这一个请求，连接继续可用）。
</details>

## 推荐资源

- [轻量RPC框架设计与边界：参考文档 / 原始资料](https://github.com/apache/brpc/tree/master/src/brpc)，来源 github.com｜源码｜英文｜深入｜免费。阅读重点：「重点阅读请求ID与响应匹配与超时表和连接关闭，用于核对本节点实验的语义和边界。」建议结合 practice96 逐项核对。

## 本课小结

- 教学RPC把协议、连接、调度、超时和可观测性组合起来，复杂度主要来自失败与生命周期管理——正常路径很短，失败路径才是主体。
- **请求ID与响应匹配**：多路复用连接上响应天然乱序，必须用连接内唯一的 req_id + pending 表 `pop` 匹配；晚到/未知/重复响应丢弃并计数。
- **超时表和连接关闭**：每个在途请求必以「成功 / 超时 / 连接关闭」之一结束且只结束一次；超时要删 pending，断连要 fail_all。
- **注册、错误模型与协议上限**：未注册方法和 handler 异常是应用错误，连接继续；畸形帧是协议错误，回错后关连接；body 长度、版本、每连接在途数都要有上限。
- 验收：压测不丢不重且资源有界（实践 5000 请求 0 错配 0 丢失 0 重复、pending 峰值 200 结束归零、服务端在途峰值 64），并明确列出缺少的生产能力。
- 误区：能完成Echo调用不代表具备生产RPC可靠性。
- 权衡：自研有教学价值，生产采用须评估生态、维护和安全成本。
- 边界：业务场景为教学假设；brpc 细节以所读源码版本为准。

