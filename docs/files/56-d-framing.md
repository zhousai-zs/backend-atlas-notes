# 第 56/208 课：字节流分帧与协议解析

- **阶段**：第 3 阶段 · 传输与Socket
- **前置课程**：d-nonblock、a-encoding
- **关键词**：Message Framing、长度前缀、网络序、粘包、拆包、增量解析、状态机、分隔符、固定长度、最大长度、校验、版本、TCP 字节流、半包、htonl、ntohl
- **难度**：进阶
- **所属领域**：D · 传输与Socket
- **标签**：Message Framing、D、C++

---

## 导读

上一课我们把「大量连接上的就绪通知」落到了 `epoll`：事件循环告诉你某个 fd **现在可读/可写**，你再去做非阻塞 `recv`/`send`。到这里为止，IO 层解决的是「何时读写」；但业务层真正要处理的，是一条条**消息**。

节点 summary 对本课的定位是：

> 应用在TCP字节流上定义消息边界，长度前缀、分隔符或固定长度都需处理不完整与恶意输入。

这句话拆开有三层意思：

1. **TCP 只保证字节流**：它保证有序、可靠地交付字节，**不保证**「一次 `send` 对应一次 `recv`」，也不保证「一条业务消息落在一次系统调用边界上」；
2. **应用必须自己定义帧边界**：常见手段是长度前缀、分隔符或固定长度；
3. **解析器要处理不完整与恶意输入**：半包要缓存，超长/非法长度要立刻拒绝，否则可能内存耗尽或协议混乱。

放到腾讯微信公众号后台的长连接 / RPC / 网关场景里，这几乎天天碰到：

- 设备或上游通过 TCP 长连接上报事件、拉取指令；
- 网关用 `epoll` 拿到 `EPOLLIN` 后 `recv` 一段字节——可能是半条消息、也可能是多条消息粘在一起；
- 上层 Protobuf / 自定义二进制 RPC 需要先「切出完整帧」，再交给业务反序列化；
- 恶意或异常对端可能宣称 `length = 2GB`，若你按字面 `reserve`，进程会被拖垮。

俗称的「粘包 / 拆包」，本质不是 TCP 的 bug，而是**帧边界问题**：

```text
发送端意图：  [MsgA][MsgB][MsgC]
实际字节流：  AAAAAAAABBBBBCCCCC...
一次 recv：   AAAA | AAABBB | BBCCCCC | ...
你要恢复：    MsgA, MsgB, MsgC
```

本课要建立的不是「用 `\n` 切一下就行」这种捷径，而是一套可落地的工程语义：

1. **长度前缀与网络序**：为什么头里的长度要用大端（网络序），`htonl`/`ntohl` 在干什么；
2. **增量解析和状态**：半包缓存 + 状态机（读头 → 读体 → 回调 → 复位）；
3. **最大长度、校验与版本**：如何拒绝超长/非法长度，magic / version / checksum 如何降低误解析风险。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释长度前缀与网络序**
   - 说清「长度前缀」帧格式：固定头里的长度字段 + 变长 body。
   - 说清网络序（大端）与主机序的区别，以及为何跨机器协议必须约定字节序。
   - 能指出常见 2 字节 / 4 字节长度头的取值范围与上限设计点。

2. **能独立完成：实现长度前缀协议，随机切分输入并模糊测试解析器**
   - 实现增量解析器：任意分片组合都能恢复相同消息序列。
   - 对超长、非法长度、截断头、截断体立即拒绝或正确等待。
   - 用随机切分 + 模糊测试证明「帧边界正确性」不依赖运气。

3. **满足本课验收标准（acceptance）**
   - 任意分片组合均恢复相同消息。
   - 超长和非法长度立即拒绝。

---

## 为什么后端要懂

节点里的理由是：

> 正确处理俗称粘包拆包的问题并避免内存耗尽。

### 1. 长连接上「一次事件」几乎从不是「一条业务消息」

在公众号后台接入层，一条连接上可能连续推多条上行；也可能一条大消息被内核拆成多次短读。若你写：

```python
# 错误心智：一次 recv = 一条消息
data = sock.recv(4096)
handle_message(data)
```

那么「粘包」时你会把两条消息当一条解析；「拆包」时你会把半条消息当完整消息。线上表现是偶发解析失败、字段错位、甚至把下一条消息的头当成 body。

### 2. 「粘包 / 拆包」不是 TCP 术语，是应用层责任

TCP 提供的是字节流抽象。Nagle、延迟 ACK、发送缓冲合并、接收窗口、`recv` 缓冲大小，都会让系统调用边界与业务消息边界无关。正确说法是：**应用层必须定义 Message Framing**。

### 3. 恶意长度是真实的内存耗尽向量

攻击者只要连上并发送 `length = 0x7fffffff`，若解析器无条件分配，进程 RSS 会暴涨，最终 OOM 或拖垮同机其他服务。节点明确要求：**超长和非法长度立即拒绝**——这是安全边界，不是优化项。

### 4. 协议演进需要版本与兼容性意识

固定头方便做 magic / version / 长度校验；但扩展字段一旦发到线上，旧客户端如何忽略未知字段、新服务如何拒绝不支持的版本，都要提前设计。节点 tradeoffs 说得很直接：**固定头方便校验，扩展字段需要兼容性设计。**

---

## 直觉 → 原理

### 0. 先建立直觉：字节流没有「消息」

用一段最小实验感受「一次 send ≠ 一次 recv」：

```python
# demo_stream_boundary.py —— 示意，非生产代码
import socket, threading, time

def server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 19560))
    s.listen(1)
    c, _ = s.accept()
    time.sleep(0.2)  # 故意让发送端多写几次再读
    while True:
        chunk = c.recv(7)  # 故意用很小的缓冲
        if not chunk:
            break
        print("RECV", len(chunk), chunk)
    c.close(); s.close()

threading.Thread(target=server, daemon=True).start()
time.sleep(0.05)
cli = socket.create_connection(("127.0.0.1", 19560))
cli.sendall(b"HELLO-1")
cli.sendall(b"HELLO-2")
cli.sendall(b"HELLO-3")
cli.close()
time.sleep(0.3)
```

你很可能看到：三次 `sendall` 被合并或拆成多次 `recv`，边界与「HELLO-n」无关。**这就是为什么需要分帧。**

节点 pitfalls 一句话钉死：

> 一次recv并不对应一次send。

### 1. 长度前缀与网络序

#### 1.1 三种常见分帧策略

| 策略 | 形态 | 优点 | 缺点 |
|------|------|------|------|
| **长度前缀** | `[len][payload...]` | 可携带二进制、可预知 body 大小、易设上限 | 头本身也可能被拆分；长度字段要约定字节序 |
| **分隔符** | `payload...\n` | 文本协议友好、人眼可读 | payload 含分隔符需转义；二进制不友好 |
| **固定长度** | 每帧 N 字节 | 解析极简 | 浪费或截断；消息大小不灵活 |

公众号后台 RPC / 长连接上行，绝大多数是**长度前缀 + 二进制 body**（body 里再放 Protobuf 等）。分隔符常见于行协议（Redis RESP 部分、HTTP/1 的 CRLF）；固定长度常见于心跳包或传感器短帧。

#### 1.2 长度前缀帧的典型布局

一个工程上常用的最小帧：

```text
 0               4               4+N
 +---------------+---------------+
 |  length (u32) |    payload    |
 +---------------+---------------+
        │
        └── 网络序（大端），表示 payload 字节数（不含 length 自身）
```

更接近生产的固定头可能是：

```text
 0      2      4      8     12
 +------+------+------+------+----------------+
 |magic | ver  | len  | crc  |    payload     |
 +------+------+------+------+----------------+
  u16    u16    u32    u32
```

- **magic**：快速拒绝非本协议流量（例如误连到 HTTP 端口）；
- **version**：协议演进；
- **len**：payload 长度；
- **crc/checksum**：可选，防比特翻转或粗检损坏。

#### 1.3 为什么必须用网络序

多字节整数在内存里的字节排列因 CPU 而异：

- **小端（little-endian）**：低地址存低字节，x86/x86_64、常见 ARM 默认如此；
- **大端（big-endian）**：低地址存高字节，网络协议约定采用的「网络序」。

若不转换，一台小端机器把 `length=0x00000100`（256）直接按内存发出，对端按大端解读会得到 `0x00010000`（65536）——帧边界立刻错乱。

C 里：

```c
#include <arpa/inet.h>
uint32_t host = 256;
uint32_t net  = htonl(host);   // host to network long
uint32_t back = ntohl(net);    // network to host long
```

Python 里用 `struct`：

```python
import struct
# 大端无符号 32 位长度前缀 + UTF-8 body
frame = struct.pack("!I", len(body)) + body
length, = struct.unpack("!I", frame[:4])
```

`!` 就是 network（big-endian）约定；`I` 是 4 字节无符号。2 字节头则用 `!H`（0..65535）。

#### 1.4 长度字段选 2 还是 4 字节？

- **u16**：最大 65535，适合小消息、想省头开销；超长消息要分片或换协议。
- **u32**：理论上到 4GB-1，**必须**配 `MAX_FRAME`（例如 1 MiB / 4 MiB），否则「合法 u32」也能打爆内存。

节点 goals 要求你能解释长度前缀与网络序——面试/入职时要能讲清：**头是固定宽度多字节整数，线上走大端，解析前先 ntohl，并且 length 有业务上限。**

### 2. 增量解析和状态

#### 2.1 半包是常态，不是异常

非阻塞 `recv` 返回的是「当前能拿到的字节」，可能：

- 不够 4 字节头（头被拆开）；
- 头齐了但 body 只到一半；
- 一条半消息 + 下一条完整消息 + 再下一条的半个头。

因此解析器必须是**增量**的：每次把新字节 append 到连接级缓冲，然后**循环尝试**切出尽可能多的完整帧。

#### 2.2 状态机：读头 → 读体 → 回调 → 复位

```text
          ┌────────────┐
          │   READ_HDR │◄──────────────────┐
          └─────┬──────┘                   │
                │ 缓冲 >= HDR_SIZE         │
                │ 解析 length，校验合法     │
                ▼                          │
          ┌────────────┐                   │
          │   READ_BODY│                   │
          └─────┬──────┘                   │
                │ 缓冲 >= HDR+length       │
                │ 取出 payload，回调上层    │
                │ 消费缓冲前缀，复位状态    │
                └──────────────────────────┘
```

伪代码：

```python
HDR = 4
MAX_FRAME = 1 << 20  # 1 MiB

class Framer:
    def __init__(self):
        self.buf = bytearray()
        self.need = HDR          # 当前还差多少字节才够做下一步判断
        self.body_len = None     # None 表示还在读头

    def feed(self, data: bytes):
        self.buf.extend(data)
        out = []
        while True:
            if self.body_len is None:
                if len(self.buf) < HDR:
                    break
                (self.body_len,) = struct.unpack("!I", self.buf[:HDR])
                if self.body_len > MAX_FRAME:
                    raise ValueError(f"frame too large: {self.body_len}")
                # 非法：也可拒绝 length==0 视业务而定；这里允许空 body
            total = HDR + self.body_len
            if len(self.buf) < total:
                break
            payload = bytes(self.buf[HDR:total])
            del self.buf[:total]
            self.body_len = None
            out.append(payload)
        return out
```

要点：

1. **连接级缓冲**：每个 TCP 连接一个 `Framer`，不要全局共享；
2. **循环 drain**：一次 `feed` 可能产出 0..N 条消息；
3. **消费前缀**：切出帧后必须从缓冲删除，避免重复解析；
4. **状态复位**：产出一条后回到「读头」。

#### 2.3 随机切分输入：用测试锁住正确性

节点 practice 要求：

> 实现长度前缀协议，随机切分输入并模糊测试解析器。

核心断言：**同样的消息序列，无论字节流被切成怎样的分片序列，解析结果必须一致。**

```python
import random, struct

def encode_messages(msgs):
    return b"".join(struct.pack("!I", len(m)) + m for m in msgs)

def random_chunks(blob, max_chunk=9):
    i = 0
    while i < len(blob):
        n = random.randint(1, max_chunk)
        yield blob[i:i+n]
        i += n

def roundtrip(msgs, seed=0):
    random.seed(seed)
    fr = Framer()
    got = []
    for chunk in random_chunks(encode_messages(msgs)):
        got.extend(fr.feed(chunk))
    assert got == msgs, (got, msgs)
```

对上千个 seed、空消息、极大接近上限的消息、单字节切分，全部跑过，才算「任意分片组合均恢复相同消息」。

### 3. 最大长度、校验与版本

#### 3.1 最大长度：第一道硬门禁

```python
MAX_FRAME = 1 << 20  # 按业务定：推送指令可能 64KiB，媒体元数据可能更大

length, = struct.unpack("!I", hdr)
if length > MAX_FRAME:
    # 立即拒绝：关连接、打点、不要分配
    raise ProtocolError("length exceeds MAX_FRAME")
```

注意：

- **在分配 body 缓冲之前**检查；
- 拒绝后应**关闭连接**（或进入错误状态），不要试图「跳过」不明长度的数据——你已经失去同步；
- 日志里记录 peer、length、已读字节，便于追查。

非法长度还包括：按你的协议约定不允许的值（例如强制 `length > 0`、或 length 必须 4 字节对齐等）。节点 acceptance：**超长和非法长度立即拒绝。**

#### 3.2 magic / version / checksum

```text
magic  == 0xWXPB   → 不是本协议，直接关
version 不在支持集 → 回错误帧或关连接（策略二选一，要文档化）
checksum 失败      → 关连接（已失同步时不要瞎救援）
```

校验和放在头还是尾：

- **头内 CRC**：只能覆盖头字段，或要求先收齐 body 再验（那 CRC 就该在尾）；
- **尾部 checksum**：验完整帧，实现稍繁，但更有意义。

对微信公众号后台这类内网 RPC，很多团队用「magic + version + length」就够了，把强完整性格在 TLS / 业务签名上。但**长度上限**无论如何不能省。

#### 3.3 固定头与扩展字段的兼容性

节点 tradeoffs：

> 固定头方便校验，扩展字段需要兼容性设计。

实践建议：

1. **头长度本身固定**（或头里再有 `hdr_len`），解析器先读齐头再读 body；
2. **version 升级**：
   - 小版本：新增可选尾部字段，旧端按 `length` 跳过未知字节；
   - 大版本：magic/version 不兼容则拒绝；
3. **不要在中途插入字段**导致旧解析器把新字段当 length；
4. Protobuf 等做 body 时，利用其「未知字段可忽略」特性，把演进压力放到 body，头保持极简。

sourceStudy 要求：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

阅读 [protobuf techniques](https://protobuf.dev/programming-guides/techniques/) 时，重点对照：**长度前缀如何与流式传输结合、以及错误/边界如何处理**——把文档里的约定映射回你自己的 `Framer` 状态机与 `MAX_FRAME`。

---

## 常见误区与设计权衡

### 误区 1：一次 recv 就是一次业务消息

这是节点 pitfalls 点名的坑。正确心智：

```text
EPOLLIN / 可读
    → recv 得到 0..N 字节（或 EAGAIN）
    → 喂给 Framer.feed
    → 得到 0..M 条完整消息
    → 逐条交给业务
```

### 误区 2：阻塞读「读够 length 再返回」就万事大吉

在线程-per-conn 模型里，你可以循环读到齐；但：

- 仍要处理对端只发了半截就挂起；
- 仍要有 `MAX_FRAME`；
- 在事件驱动（epoll）模型里，阻塞读会卡死整个线程/循环。

增量状态机是统一解法。

### 误区 3：长度用主机序「我们都是 x86」

今天是 x86，明天混部 ARM；抓包工具、跨语言客户端（Go/Java/Python）默认都按网络序文档来。协议字节序必须写进规范，并用 `!I`/`htonl` 落实。

### 误区 4：超长时「先分配再拒绝」

错误顺序：

```python
buf = bytearray(length)  # 可能 length=2GB
if length > MAX: reject
```

正确顺序：先校验，再分配。

### 误区 5：失同步后仍试图「搜索下一个 magic」

对长度前缀二进制协议，失同步后盲目扫描 magic 可能误对齐到 payload 内部的巧合字节，造成更隐蔽的错乱。更稳妥：**关闭连接，让客户端重连并重置状态。**（文本分隔符协议有时用「读到下一个分隔符」救援，那是另一类协议。）

### 权衡：长度前缀 vs 分隔符 vs 固定长度

| 维度 | 长度前缀 | 分隔符 | 固定长度 |
|------|----------|--------|----------|
| 二进制安全 | 好 | 需转义 | 好 |
| 实现复杂度 | 中（状态机） | 低～中 | 低 |
| 内存安全 | 需 MAX | 需行长上限 | 天然有界 |
| 扩展性 | 好 | 一般 | 差 |
| 典型场景 | RPC、长连接、Protobuf 外层 | 日志、行协议 | 心跳、传感器 |

节点 tradeoffs 强调固定头便于校验，但扩展要讲兼容——这正是「长度前缀 + 固定头 + 版本字段」成为后台默认选项的原因。

---

## 动手实践

### 实践目标

> 实现长度前缀协议，随机切分输入并模糊测试解析器。

验收：

1. 任意分片组合均恢复相同消息；
2. 超长和非法长度立即拒绝。

并按 sourceStudy：对照资源文档中的定义、示例与错误处理路径，记录你实现适用的版本假设与边界条件。

### 步骤

#### 步骤 1：定义帧格式与常量

在 `practice56/` 约定：

```text
frame  := u32_be length || bytes[length]
MAX_FRAME := 1048576   # 1 MiB
HDR := 4
非法：length > MAX_FRAME → ProtocolError
```

空 body（`length=0`）允许，便于测边界。

#### 步骤 2：实现 `encode` / `Framer`

- `encode(msg: bytes) -> bytes`
- `encode_many(msgs) -> bytes`
- `Framer.feed(data) -> list[bytes]`，内部状态机 + `MAX_FRAME` 检查

#### 步骤 3：随机切分往返测试

```text
for seed in 0..999:
  msgs = 随机生成 0..20 条，每条 0..200 字节
  blob = encode_many(msgs)
  chunks = 按 seed 切成 1..max_chunk 的片
  依次 feed，断言输出 == msgs
  断言 Framer 缓冲最终为空（无残留半包）
```

再加专项：

- **单字节切分**：`max_chunk=1`，最易暴露「头被拆开」bug；
- **整包一次喂入**；
- **半头 + 半体 + 多包粘连**手工用例。

#### 步骤 4：模糊 / 负面测试

| 用例 | 期望 |
|------|------|
| `length = MAX_FRAME+1` | 立即 `ProtocolError`，不分配巨缓冲 |
| `length = 0xFFFFFFFF` | 立即拒绝 |
| 只喂 3 字节头 | 不产出消息、不抛错（等待更多数据） |
| 头声明 length=10，只喂 5 字节 body | 等待 |
| 合法帧后粘非法超长头 | 先产出合法消息，再在超长处拒绝 |

#### 步骤 5：对照资源做 sourceStudy 笔记

打开 <https://protobuf.dev/programming-guides/techniques/>，记录：

1. 文档中与「streaming / length prefix / large data」相关的建议；
2. 你的 `MAX_FRAME`、字节序、空 body 策略是否与团队规范一致；
3. 错误路径：超长、截断、版本不匹配时是关连接还是回错误帧。

#### 步骤 6：（可选）接到非阻塞 socket

把 `Framer` 接到上一课的事件循环：`EPOLLIN` → `recv` → `feed` → 处理消息列表。观察：短读不会破坏消息完整性。

### 预期现象

- 1000 个随机 seed 全部 `OK`；
- 单字节切分仍能恢复消息；
- 超长长度在 `feed` 解析出 length 的当下抛错，进程内存无明显跳升；
- 半包等待时无异常、无空消息误报。

### 验收清单

```text
[ ] 能口头解释：网络序、htonl/ntohl、为何不能主机序上线
[ ] Framer 状态机：读头→读体→回调→复位，一次 feed 可多条
[ ] 任意分片（含 1 字节）往返与原文一致
[ ] length > MAX_FRAME / 非法长度立即拒绝，且先检查后分配
[ ] 对照过 protobuf techniques 文档中与流式/边界相关的段落
[ ] 清楚粘包拆包本质是帧边界，而不是 TCP bug
```

---

## 自测题

**题 1**  
在小端主机上，要把 payload 长度 `256` 写成 4 字节长度前缀（网络序），线上字节序列应是？

A. `00 00 01 00`  
B. `00 01 00 00`  
C. `01 00 00 00`  
D. `00 00 00 01` 以外任意，因为 TCP 会自动改字节序

**题 2**  
非阻塞连接上 `recv` 返回 6 字节。协议是 4 字节大端长度前缀 + body。这 6 字节是某新帧的开头，且 length 字段解出为 100。解析器此刻最正确的行为是？

A. 认定消息已齐，把 6 字节当完整帧交给业务  
B. 缓存这 6 字节，保持「还需 98 字节 body」的状态，等后续 `feed`  
C. 立刻 `close`，因为一次 recv 必须拿到整帧  
D. 按分隔符从这 6 字节里找 `\n` 强行切分

**题 3**  
对端发来长度前缀 `0x7FFFFFFF`（约 2GB），你的 `MAX_FRAME=1MiB`。应如何处理？

A. `bytearray(0x7FFFFFFF)` 分配成功后再慢速读取  
B. 解析出 length 后立即拒绝（抛错/关连接），不按该长度分配  
C. 忽略长度字段，改用固定 4096 读  
D. 把长度截断为 `MAX_FRAME` 并继续解析，静默丢弃多余字节

---

### 自测题参考答案

**题 1：A**  
256 = `0x00000100`，网络序（大端）低地址存高字节 → `00 00 01 00`。C 是小端内存序常见形态；D 错误，TCP 不负责应用层多字节整数的字节序。

**题 2：B**  
头已齐（4 字节），body 还需 100，当前只多拿到 2 字节 body，属于半包。增量解析应缓存并等待。A 把半包当整帧；C 误解了非阻塞短读；D 把长度前缀协议错当成分隔符协议。

**题 3：B**  
节点 acceptance 要求超长和非法长度立即拒绝。必须在分配前检查；截断后继续解析会失同步（D）；按宣称长度分配是 OOM 向量（A）。

---

## 推荐资源

- [字节流分帧与协议解析：参考文档 / 原始资料](https://protobuf.dev/programming-guides/techniques/)（protobuf.dev，英文文档，进阶）
  - 重点阅读长度前缀与网络序与增量解析和状态，用于核对本节点实验的语义和边界。
  - 沿文档中的定义、示例与错误处理路径核对你的 `Framer` 实现，并记录适用版本及边界条件（呼应 sourceStudy）。

---

## 本课小结

1. **TCP 是字节流，不是消息流**：粘包/拆包的本质是应用层帧边界问题；一次 `recv` 并不对应一次 `send`。
2. **长度前缀 + 网络序**是后台 RPC/长连接的主流分帧方式：固定宽度长度头用大端，`htonl`/`ntohl` 或 `struct "!I"` 落实。
3. **增量解析靠状态机**：读头 → 校验长度 → 读体 → 回调 → 复位；连接级缓冲，一次 `feed` 可产出多条消息。
4. **最大长度是安全边界**：超长/非法长度立即拒绝，先检查后分配，避免内存耗尽。
5. **magic / version / checksum** 降低误解析与演进风险；固定头方便校验，扩展字段要做兼容性设计。
6. **用随机切分与模糊测试锁正确性**：任意分片组合恢复相同消息，才算分帧实现合格。
7. **对照权威文档做 sourceStudy**：把外部资料中的错误处理路径映射到自己的拒绝策略与版本假设。

下一课会在分帧之上继续传输与 Socket 主题；请先把本课 `Framer` 与随机切分测试跑绿，再进入后续内容。

---

## 配套练习

目录：[`practice56/`](./practice56/)

| 文件 | 作用 |
|------|------|
| `framer.py` | 长度前缀编解码 + 增量 `Framer` 状态机 + `MAX_FRAME` 拒绝 |
| `test_framer.py` | 随机切分往返、单字节切分、超长/非法长度、半包等待 |
| `run.sh` | 一键跑完全部验收 |

运行：

```bash
cd practice56 && ./run.sh
```

期望末行：`ALL_CHECKS_PASSED`。

实测要点（Asia/Shanghai，本机验证时对照）：

- 1000 个随机 seed 往返一致；
- `max_chunk=1` 单字节切分通过；
- `length=MAX_FRAME+1` 与 `0xFFFFFFFF` 立即 `ProtocolError`；
- 半包输入不抛错、不产出假消息；
- 合法帧后粘超长头：先产出合法，再拒绝。

NOTE：本练习用纯 Python 验证分帧语义，不绑定具体 epoll 循环；接到非阻塞 socket 时只需在可读事件里 `feed(recv_bytes)`。C++17 版本可用 `std::vector<uint8_t>` + `ntohl` 按同样状态机实现。
