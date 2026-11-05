# 第 50/208 课：UDP报文与可靠性设计

- **阶段**：第 2 阶段 · 传输与Socket
- **前置课程**：a-network、c-syscall
- **关键词**：UDP、报文边界、截断、MTU、分片、丢包、重传、序号、拥塞、请求ID、超时、sendto、recvfrom
- **难度**：基础
- **所属领域**：D · 传输与Socket

---

## 导读

先把场景放到微信公众号后台里。

你负责的服务每天要做几件“看起来很轻”的事：

1. **DNS 解析**：调用内部或公网 DNS，把域名解析成 IP，供回调转发、素材下载、第三方接口访问使用。
2. **内部心跳 / 存活探测**：对推送网关、接入节点做延迟敏感的探测，超时就摘流。
3. **自定义轻量协议**：某些机房内状态同步、实时计数、实验流量采样，不想为每个小包维护 TCP 连接。
4. **实时通知旁路**：对“丢一两个包可以接受、但延迟必须低”的旁路信号使用 UDP。

有一天监控出现三类怪现象：

- DNS 客户端偶发“解析成功但内容缺了一截”，像是响应被砍断。
- 探测服务在丢包时疯狂重试，把对端打满，延迟反而更差。
- 有人说“UDP 比 TCP 快，我们把内部 RPC 都改成 UDP 吧”，上线后高峰期丢包和重复响应把业务搞乱。

这些问题的共同根源是：**UDP 传输的是独立数据报，它不保证交付、不保证顺序、不去重；报文有边界，也可能被截断；可靠性和拥塞控制如果需要，必须由应用自己补。**

本课不把 UDP 讲成“不可靠的 TCP 简化版”，而是把它当成一种明确的设计选择：你换来了无连接状态、保留报文边界、通常更低的握手成本，同时把交付、去重、重传、序号和拥塞责任接到了自己手里。

---

## 学习目标

完成本课学习后，你应当能够：

1. **解释报文边界与截断**
   - 说清一次 `sendto` 对应一颗 UDP 数据报，一次 `recvfrom` 取走一颗完整数据报（在未被截断的前提下）。
   - 说清当用户态接收缓冲区小于数据报长度时会发生什么，以及如何用 `MSG_TRUNC` 等手段检测截断。
   - 能对比 TCP 字节流：UDP 不会把两次发送粘成一段，但可能丢、乱序、重复。

2. **独立完成实践：实现 UDP 请求响应并注入丢包/重复，加入请求 ID 与超时**
   - 设计带 `req_id` 的请求-响应协议。
   - 能注入丢包与重复，观察超时重试行为。
   - 用有限次重试和请求 ID 匹配，避免无限发送和误配响应。

3. **满足本课验收标准**
   - 能检测接收缓冲区截断。
   - 重试不会无限发送或误配响应。

---

## 为什么后端要懂

节点里的理由是：**理解 DNS、实时通信及自定义协议的可靠性边界。**

### 1. DNS：最常见的 UDP 业务依赖

DNS 查询默认走 UDP/53。公众号后台几乎所有出站域名访问都依赖它。DNS 的设计假设是：

- 大多数响应能装进较小的 UDP 包；
- 丢了可以重试；
- 需要更大载荷时再考虑 TCP 或扩展机制。

如果你不清楚 UDP 的截断、分片和超时重试，就会在“解析偶发失败”“响应异常短”“重试打爆解析器”这类问题上判断失误。

### 2. 实时通信与探测：允许丢，但不允许失控

心跳、延迟探测、实时状态旁路，往往宁可丢一两个包，也不愿意被 TCP 重传和队头阻塞拖住。这正是 tradeoff 的正面：

> 允许少量丢失可降低时延。

但另一面同样成立：如果你在应用层无脑重试、没有退避、没有序号/请求 ID，UDP 会比 TCP 更容易把网络和对方打死。

### 3. 自定义协议：可靠性边界由你划

内部自定义 UDP 协议很诱人：无连接、好写、好刷流量。真正难的是明确：

- 哪些消息可以丢？
- 哪些必须至少一次送达？
- 重复送达是否可接受（幂等）？
- 乱序是否可接受？
- 谁负责拥塞控制？

不懂这些边界，就会在“看起来能跑”的原型上堆业务，最后在高峰期以丢包、重复、误匹配的形式爆掉。

---

## 直觉 → 原理

### 一、报文边界与截断

#### 1. UDP 是数据报，不是字节流

TCP 里，两次 `send()` 可能在接收方变成一次 `recv()`，也可能一次 `send()` 被拆成多次 `recv()`。应用必须自己做 framing（长度前缀、分隔符等）。

UDP 相反：**内核按数据报交付**。发送方一次 `sendto` 发出一颗 UDP 报文；接收方一次 `recvfrom` 通常取走一颗完整报文。两次 `sendto` 不会在接收方粘成一个缓冲区里的“连续字节流”。

直觉可以记成：

| 维度 | TCP | UDP |
|------|-----|-----|
| 交付单位 | 字节流 | 数据报 |
| 消息边界 | 不保留 | 保留 |
| 连接 | 需要 | 不需要 |
| 交付保证 | 可靠有序 | 尽力而为 |
| 去重 | 协议负责 | 应用负责 |

#### 2. 最小代码直觉

```python
import socket

srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
srv.bind(("127.0.0.1", 18050))

# 客户端两次发送
# sendto(b"PART1")
# sendto(b"PART2")

a, addr = srv.recvfrom(4096)  # 通常得到 b"PART1"
b, addr = srv.recvfrom(4096)  # 通常得到 b"PART2"
# 不会像 TCP 那样轻易读到 b"PART1PART2"
```

在公众号后台的自定义探测协议里，这意味着：你可以把“一条逻辑消息”做成一颗 UDP 包，接收方天然按消息处理。但也意味着：你不能假设这颗包一定到达，也不能假设先发的一定先到。

#### 3. 截断是什么

截断发生在：**数据报已经到达主机，但你传给 `recvfrom`/`recvmsg` 的用户缓冲区太小**，放不下整颗报文。

Linux 上常见行为是：

- `recvfrom(buf)` 把前 `len(buf)` 字节拷给你；
- 多余部分被丢弃；
- 若使用 `recvmsg` 并带上 `MSG_TRUNC` 标志请求，返回的 `flags` 里可能带有 `MSG_TRUNC`，告诉你“实际报文更长，你读短了”。

这和“网络丢包”不是一回事：

- 丢包：对方可能根本没收到，或中途没到；
- 截断：包到了，是你读取方式把内容砍短了。

公众号后台里一个典型坑：DNS 或内部状态响应变长后，客户端仍用固定小缓冲 `recvfrom(512)`，于是“偶发解析残缺”。日志看起来像协议 bug，其实是截断。

#### 4. 用 MSG_TRUNC 检测截断

```python
import socket

MSG_TRUNC = getattr(socket, "MSG_TRUNC", 0x20)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.sendto(b"REQ|1|big:4000", ("127.0.0.1", 18050))
data, anc, flags, addr = s.recvmsg(80, 0, MSG_TRUNC)
truncated = bool(flags & MSG_TRUNC)
print(len(data), hex(flags), truncated)
```

验收标准之一就是：**能检测接收缓冲区截断**。工程上还应该：

- 按协议最大报文长度分配缓冲；
- 对超长响应有明确错误路径（重试走 TCP、扩缓冲、返回错误码）；
- 不要静默使用截断后的半份数据。

#### 5. 观察命令

```bash
ss -uan                     # 看 UDP socket
ss -uan sport = :18050      # 指定端口
cat /proc/sys/net/ipv4/udp_mem
cat /proc/sys/net/ipv4/udp_rmem_min
cat /proc/net/snmp | tr ' ' '\n' | paste - - | grep -i udp
```

`ss -uan` 里 UDP 常显示为 `UNCONN`（未 connect）或已 `connect` 的对端地址。UDP 也可以 `connect()`，那只是给 socket 绑定默认对端，方便 `send`/`recv`，并不建立 TCP 那样的握手连接。

---

### 二、MTU、分片和丢包

#### 1. MTU 决定“一跳能装多大”

MTU（Maximum Transmission Unit）是链路层一帧能承载的最大 IP 包大小。以太网常见 1500。UDP 载荷还要减去 IP 头、UDP 头；若走封装（VXLAN、VPN），有效 MTU 更小。

```bash
ip link                 # 看各接口 mtu
ip route get 1.1.1.1    # 看实际出接口与相关信息
```

如果应用发送的 UDP 报文让 IP 包超过路径 MTU，就会触发分片（IPv4 允许中间分片；IPv6 通常要求端到端处理）。

#### 2. 分片为什么危险

分片的直觉成本：

- 一颗逻辑 UDP 报文变成多个 IP 分片；
- **任何一个分片丢失，整颗 UDP 报文作废**；
- 防火墙/NAT 对分片支持不佳时，表现为“大包必挂、小包正常”；
- 重装占用内存与 CPU，成为攻击面（分片重装队列）。

所以自定义协议和 DNS 扩展都倾向：**尽量让单报文保持在路径 MTU 以内**，常见实践是把载荷控制在更保守的大小（例如数百到一千多字节量级，视路径而定），超长则改设计（分割业务消息、改走 TCP、或做应用层分片并自带重组与重传）。

#### 3. 丢包从哪里来

UDP 丢包可以来自：

- 链路误码、无线干扰；
- 交换机/路由器队列溢出（拥塞）；
- 本机 socket 接收队列满（应用读太慢）；
- 防火墙策略；
- 分片丢失导致整包丢弃。

TCP 丢了会重传；UDP 丢了默认就没了。DNS 客户端、你的探测客户端、你的自定义 RPC，都必须假设“发出去不等于到达”。

#### 4. ping / traceroute 的概念位置

`ping`（ICMP）和 `traceroute` 不是 UDP 业务本身，但常用来理解路径与丢包：

- 延迟是否抖动；
- 哪一跳开始丢；
- 大包（按包长探测）是否更差。

它们帮助你区分“应用协议写错了”还是“路径/MTU/拥塞有问题”。

---

### 三、重传、序号和拥塞责任

#### 1. UDP 把可靠性交给应用

节点 summary 说得很清楚：

> UDP传输独立数据报，不保证交付、顺序或去重，应用需按业务决定补偿机制。

补偿通常包括：

- **请求 ID / 序号**：匹配请求与响应，去重，识别乱序；
- **超时**：单次等待上限；
- **重传**：超时未达则重发，但必须有次数上限与退避；
- **幂等**：同一请求重试多次，业务结果可接受；
- **拥塞控制**：不能丢了就立刻打满速率。

#### 2. 请求 ID 为什么是硬需求

没有请求 ID 时，超时重试会发生经典误配：

1. 发请求 A，超时；
2. 重发 A'（或误当成新请求 B）；
3. 迟到的 A 响应回来，被当成 B 的响应。

在公众号后台，这可能把“用户 X 的状态”套到“用户 Y”上。所以验收标准要求：**重试不会误配响应**。

最小协议可以长这样：

```text
REQ|<req_id>|<payload>
RSP|<req_id>|<payload>
```

接收时：只接受 `req_id` 匹配的响应；不匹配的计入 mismatch 并丢弃。

#### 3. 超时与有限重试

```text
attempts = 0
sends = 0
while attempts <= max_retries and sends < max_sends:
    send request
    waits up to timeout for matching rsp
    if matched: success
    attempts += 1
fail
```

关键点：

- **timeout**：单次等待；
- **max_retries / max_sends**：防止无限发送；
- 可选指数退避：`sleep(base * 2^attempt)`，避免雷群。

#### 4. 拥塞责任在你

节点 pitfall：

> UDP不自动比TCP快，也不能免除拥塞控制。

很多人以为“无握手 = 更快”。在局域网空闲时，小包 UDP 的确常常延迟更低。但在共享拥塞路径上：

- TCP 会降窗、退避；
- 裸 UDP 不会，除非你写；
- 于是 UDP 可能持续丢包、持续重试、进一步拥塞，形成恶性循环。

实时音视频会在应用层做拥塞控制（如基于延迟/丢包的码率适应）。你的内部探测如果在丢包时把 QPS 翻倍，就是反面教材。

#### 5. 和 TCP 的职责对比

| 职责 | TCP | UDP 应用 |
|------|-----|----------|
| 交付 | 有 | 可选自建 |
| 顺序 | 有 | 可选自建 |
| 去重 | 有 | 需要请求 ID/序号 |
| 流控 | 有 | 需要自建或限速 |
| 拥塞控制 | 有 | **必须自己考虑** |
| 消息边界 | 无 | 有 |

---



### 附：C 语言最小 sendto/recvfrom

后端入职后你更常在 C/C++ 服务里见到这些调用。最小形态如下（示意，省略完整错误处理）：

```c
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>

int main() {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(18050);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    const char *msg = "REQ|c0001|echo:hi";
    sendto(fd, msg, strlen(msg), 0, (struct sockaddr*)&addr, sizeof(addr));

    char buf[2048];
    struct sockaddr_in peer;
    socklen_t peerlen = sizeof(peer);
    ssize_t n = recvfrom(fd, buf, sizeof(buf), 0, (struct sockaddr*)&peer, &peerlen);
    if (n > 0) {
        fwrite(buf, 1, n, stdout);
        fputc('\n', stdout);
    }
    close(fd);
    return 0;
}
```

注意：

- `sizeof(buf)` 决定你最多拷贝多少；若数据报更长，余下被丢弃（截断）。
- 若需要截断标志，应使用 `recvmsg` 并检查 `msg_flags` 中的 `MSG_TRUNC`。
- 不要假设 `sendto` 成功等于对端已处理；成功只表示本机协议栈接受了发送。

### 内核参数与队列直觉

```bash
uname -r
cat /proc/sys/net/ipv4/udp_mem          # 页面数：min pressure max
cat /proc/sys/net/ipv4/udp_rmem_min
cat /proc/sys/net/ipv4/udp_wmem_min
sysctl net.core.rmem_default net.core.rmem_max
sysctl net.core.wmem_default net.core.wmem_max
```

当接收方应用读不过来时，UDP 报文在 socket 接收队列里堆积，队列溢出就会丢。这和 TCP 不同：TCP 会通过窗口让发送方变慢；UDP 发送方默认感知不到你的慢，除非你在应用层做 ACK/评分/限速。

公众号后台的一个真实风险是：某台机器上的 UDP 探测客户端在对方变慢时疯狂重试，把对方 `Recv-Q` 打满，导致正常 DNS 或其他 UDP 业务也被连坐。

### 路径 MTU 的实用判断

```bash
ip link show
ip route get 10.0.0.1
# 概念上：大包探测可用 ping -M do -s <size>（需权限/网络允许）
```

设计自定义 UDP 协议时建议：

1. 先假定有效载荷不超过较保守阈值（考虑封装后的路径 MTU）；
2. 协议里带长度字段，接收方校验；
3. 超长消息不要指望 IP 分片“顺便帮你可靠送达”；
4. 需要大数据时，优先业务层分片 + 序号 + 选择性重传，或直接改 TCP。


## 常见误区

### 误区 1：UDP 自动比 TCP 快

不对。UDP 省去了连接管理和可靠性状态机，**在特定场景**可以降低延迟和 CPU。但它不自动更快：

- 大消息触发分片后可能更慢更脆；
- 丢失后的应用重试可能比 TCP 重传更暴力；
- 你自己实现的可靠性如果很笨，会全面慢于 TCP。

### 误区 2：用了 UDP 就可以不管拥塞

不对。网络队列不区分“这是合法探测”还是“失控重试”。没有拥塞控制的 UDP 流量是公害，也容易被运维限速或直接丢掉。

### 误区 3：recvfrom 返回了数据就一定完整且属于我期待的那次请求

不一定。可能是截断后的半份；也可能是上一次迟到的响应；还可能是重复包。必须做长度/截断检查和请求 ID 匹配。

### 误区 4：把 TCP 的连接思维原封搬到 UDP

UDP 没有 `ACCEPT` 队列那一套。`connect()` 在 UDP 上只是默认对端。关闭也不需要四次挥手。资源模型、超时模型、重试模型都要按数据报重新设计。

---

## 设计权衡

节点 tradeoffs：

> 允许少量丢失可降低时延，可靠传输则需额外协议复杂度。

可以落成一张决策表：

| 业务 | 更合适的方向 | 原因 |
|------|--------------|------|
| DNS 查询 | UDP 为主 | 短请求短响应，丢了重试 |
| 内部延迟探测 | UDP | 宁可丢，也不要握手与队头阻塞 |
| 支付/写配置 | TCP 或带强可靠的应用协议 | 不能丢、不能默默重复下单 |
| 实时音视频 | UDP + 应用层拥塞/编码控制 | 过期帧重传无意义 |
| 大块文件 | TCP 或 QUIC 等成熟方案 | 自建可靠 UDP 成本高 |

对微信公众号后台：**默认业务 RPC 走 TCP/HTTP；DNS 与部分探测走 UDP；若自建 UDP 协议，必须显式写出丢失、重复、乱序、超时和拥塞策略。**

---

## 动手实践

实践目标（对应节点 practice / acceptance / sourceStudy）：

> 实现 UDP 请求响应并注入丢包重复，加入请求 ID 与超时。  
> 验收：能检测接收缓冲区截断；重试不会无限发送或误配响应。  
> 沿 man 页定义、示例与错误路径核对实现，并记录适用版本及边界条件。

### 步骤 1：起一个 UDP echo 服务

协议：

```text
REQ|<req_id>|<payload>
RSP|<req_id>|<payload>
```

服务端支持：

- `echo:text`：回显；
- `big:n`：回 n 字节，便于测截断；
- `drop:n`：前 n 次同 req_id 请求静默丢弃；
- `delay:ms:text`：延迟响应；
- `dup2`：故意回两个响应（重复）。

### 步骤 2：客户端带超时与有限重试

- 每次请求生成 `req_id`；
- `socket.settimeout(timeout)`；
- 超时则重发，但 `retries` 与 `max_sends` 封顶；
- 只接受 `req_id` 匹配的 `RSP`。

### 步骤 3：检测截断

用 `recvmsg(..., MSG_TRUNC)`：当 `flags & MSG_TRUNC` 时判定截断。对比小缓冲与大缓冲。

### 步骤 4：注入丢包/重复并验收

- 服务端 `drop:2`：前两次丢，第三次成功 → 发送次数约为 3，且不超过上限；
- 超时场景：`delay` 大于 timeout，确认失败退出且发送次数 ≤ 上限；
- 同 socket 先收错 `req_id` 再收对的：错配被忽略；
- 慢响应与快请求并存：快请求只匹配自己的响应；
- `dup2`：接受首个匹配，不把重复当新事务。

### 步骤 5：对照 man 页

阅读 https://man7.org/linux/man-pages/man7/udp.7.html ，核对：

- 数据报边界语义；
- 截断相关说明；
- 错误路径（如 `ECONNREFUSED` 在 connect 过的 UDP socket 上 ICMP 端口不可达的行为，注意版本与路径差异）；
- 记录本机内核版本：`uname -r`。

参考实现目录：`practice50/`（本课附录给出已跑通脚本）。

---



### 实践里的错误路径清单（呼应 sourceStudy）

对照 man 页与本机行为，建议你亲自记录：

| 场景 | 期望观察 | 你的记录 |
|------|----------|----------|
| 小缓冲收大包 | `MSG_TRUNC` 或短读 | |
| 对端未监听且 socket 已 connect | 可能收到 ICMP 后 `ECONNREFUSED`（行为依赖 connect 与路径） | |
| 未 connect 的 socket 发往未监听端口 | 常常“发送成功”，无错误返回 | |
| 超时重试 | 发送次数有上限 | |
| 迟到响应 | 不匹配则忽略 | |
| 重复响应 | 只采纳首次匹配 | |

`uname -r` 与发行版不同，错误路径细节可能不同。**不要只背结论，要在目标内核上复核。**

### 把协议做成“可运营”

上线一个 UDP 自定义协议前，至少具备：

1. **指标**：发送数、超时数、重试数、mismatch 数、截断数、平均/分位延迟；
2. **限速**：每主机/每目的的最大 QPS 与最大 in-flight；
3. **退避**：超时后的指数退避 + 抖动；
4. **预算**：单请求最大重试次数与最大总发送次数；
5. **降级**：连续失败时切换备解析器/备探测路径，或暂时摘除依赖；
6. **幂等**：服务端对重复 `req_id` 可安全处理。

这些不是“加分项”，而是 UDP 可靠性设计的基本盘。


## 自测题

### 题目 1

为什么说 UDP 保留报文边界？若接收方 `recvfrom` 缓冲区只有 100 字节，而数据报长 4000 字节，会发生什么？如何检测？

**参考答案**

UDP 以数据报为单位交付，一次 `sendto` 对应一颗报文，接收方一次成功的 `recvfrom`/`recvmsg` 取走一颗报文的数据（在未被截断的前提下），不会像 TCP 那样把两次发送隐式粘成连续字节流。若用户缓冲区只有 100 字节而报文 4000 字节，Linux 上通常返回前 100 字节，其余丢弃，造成截断。可用 `recvmsg` 并检查 `MSG_TRUNC` 标志检测；工程上应按最大报文长度分配缓冲，避免静默使用半份数据。

### 题目 2

自定义 UDP 请求-响应为什么必须带请求 ID，并且重试必须有上限？请结合误配与拥塞说明。

**参考答案**

UDP 可能乱序、重复、迟到。超时后重发时，旧响应可能在新请求发出后到达；若无请求 ID，就会把旧响应当成新请求的结果，造成误配。重试若无上限，在丢包拥塞时会持续加速发送，进一步恶化网络，这正是“UDP 也不能免除拥塞控制”的含义。正确做法是：请求 ID 匹配、有限次重试、必要时退避，并对业务做幂等设计。

### 题目 3

有人提议把公众号后台内部配置写入接口从 TCP 改成裸 UDP，理由是“UDP 更快”。你如何评价？

**参考答案**

配置写入通常要求可靠、有序、可确认，且重复提交需要严格幂等。裸 UDP 不提供这些保证，要在应用层重做序号、重传、去重和拥塞控制，复杂度接近自建一套可靠协议，收益往往远小于风险。UDP 更适合 DNS、探测、实时媒体等“可丢或自带应用层策略”的场景。节点 tradeoff 已说明：允许少量丢失可降低时延；可靠传输则需额外协议复杂度。配置写入应继续走 TCP/HTTP 或成熟 RPC。

---

## 推荐资源

- **UDP报文与可靠性设计：Linux man-pages**
  - URL：https://man7.org/linux/man-pages/man7/udp.7.html
  - 来源：man7.org
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 说明：重点阅读报文边界与截断、与 MTU/分片/丢包相关的行为说明，用于核对本节点实验的语义和边界。阅读时结合本机内核版本，记录错误路径与边界条件（对应 sourceStudy）。

---

## 本课小结

本课围绕 UDP 的三个核心点展开。

第一，**报文边界与截断**。UDP 按数据报交付，保留消息边界；但用户缓冲区过小会截断，应用必须检测并避免使用半份数据。

第二，**MTU、分片和丢包**。超大 UDP 报文可能分片，分片丢失等于整报文丢失。尽量控制报文大小，区分“网络丢包”和“本地截断”。

第三，**重传、序号和拥塞责任**。UDP 不保证交付、顺序、去重；请求 ID、超时、有限重试和拥塞控制都在应用侧。UDP 并不自动比 TCP 快，也不能免除拥塞控制。

两条验收标准请记住：

- **能检测接收缓冲区截断**；
- **重试不会无限发送或误配响应**。

在微信公众号后台，把 DNS、探测、实时旁路和可靠业务写入区分开：该丢的允许丢，该可靠的不要拿裸 UDP 赌运气。

---

## 附：已验证的实践脚本（practice50）

本课正文中的抓包步骤如需 `tcpdump`，请在有权限的机器上执行。本 box 用 `ss` + Python socket 验证同一套语义（报文边界、截断、丢包重试、请求 ID 匹配、超时上限、重复响应），已在本环境跑通并输出 `ALL_CHECKS_PASSED`。

目录：`/workspace/atlas/lessons/practice50/`

### server.py

```python
#!/usr/bin/env python3
"""UDP demo server for Backend Atlas lesson 50 (d-udp)."""
import socket
import sys
import threading
import time

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18050
drop_map = {}
drop_lock = threading.Lock()

srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
srv.bind((HOST, PORT))
print(f"udp listening on {HOST}:{PORT}", flush=True)


def send_later(sock, data, addr, delay_ms):
    time.sleep(max(0, delay_ms) / 1000.0)
    try:
        sock.sendto(data, addr)
    except Exception as e:
        print("send_later err", e, flush=True)


while True:
    try:
        data, addr = srv.recvfrom(65535)
    except Exception as e:
        print("recv err", e, flush=True)
        continue
    try:
        text = data.decode("utf-8", errors="replace")
        parts = text.split("|", 2)
        if len(parts) != 3 or parts[0] != "REQ":
            srv.sendto(b"RSP|0|ERR:bad-format", addr)
            continue
        _, req_id, payload = parts

        if payload.startswith("drop:"):
            n = int(payload.split(":", 1)[1] or "1")
            with drop_lock:
                left = drop_map.get(req_id, n)
                if left > 0:
                    drop_map[req_id] = left - 1
                    print(f"drop req_id={req_id} left={left-1}", flush=True)
                    continue
                drop_map.pop(req_id, None)
            resp = f"RSP|{req_id}|dropped-then-ok"
            srv.sendto(resp.encode(), addr)
            continue

        if payload.startswith("delay:"):
            rest = payload[len("delay:"):]
            ms_s, _, body = rest.partition(":")
            resp = f"RSP|{req_id}|{body}".encode()
            threading.Thread(
                target=send_later, args=(srv, resp, addr, int(ms_s)), daemon=True
            ).start()
            continue

        if payload.startswith("big:"):
            n = int(payload.split(":", 1)[1])
            n = max(0, min(n, 60000))
            body = "B" * n
            resp = f"RSP|{req_id}|{body}"
            srv.sendto(resp.encode(), addr)
            continue

        if payload.startswith("echo:"):
            body = payload[len("echo:"):]
            resp = f"RSP|{req_id}|{body}"
            srv.sendto(resp.encode(), addr)
            continue

        if payload == "dup2":
            srv.sendto(f"RSP|{req_id}|first".encode(), addr)
            srv.sendto(f"RSP|{req_id}|second".encode(), addr)
            continue

        resp = f"RSP|{req_id}|{payload}"
        srv.sendto(resp.encode(), addr)
    except Exception as e:
        print("handle err", e, flush=True)
        try:
            srv.sendto(b"RSP|0|ERR:exception", addr)
        except Exception:
            pass
```

### trunc_probe.py

```python
#!/usr/bin/env python3
"""Probe UDP truncation with MSG_TRUNC (Linux)."""
import socket
import sys
import uuid

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 18050
recvbuf = int(sys.argv[3]) if len(sys.argv) > 3 else 100
big_n = int(sys.argv[4]) if len(sys.argv) > 4 else 5000

req_id = uuid.uuid4().hex[:8]
msg = f"REQ|{req_id}|big:{big_n}".encode()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2.0)
s.sendto(msg, (host, port))

# recvmsg with MSG_TRUNC: flags in return will include MSG_TRUNC if truncated
MSG_TRUNC = getattr(socket, "MSG_TRUNC", 0x20)
data, ancdata, flags, addr = s.recvmsg(recvbuf, 0, MSG_TRUNC)
text = data.decode("utf-8", errors="replace")
parts = text.split("|", 2)
ok_id = parts[1] if len(parts) == 3 else "?"
truncated = bool(flags & MSG_TRUNC)
print(
    f"trunc_probe req_id={req_id} got_id={ok_id} recv_len={len(data)} "
    f"recvbuf={recvbuf} flags=0x{flags:x} truncated={int(truncated)}"
)
s.close()
sys.exit(0 if truncated else 1)
```

### client.py（职责摘要）

完整文件见同目录 `client.py`。核心行为：

- 构造 `REQ|<req_id>|<payload>`，`sendto` 发出；
- `timeout` 内 `recvfrom`，只接受 `req_id` 匹配的 `RSP`；
- `retries` + `max_sends` 防止无限发送；
- `--inject-loss` / `--inject-dup` 在客户端注入丢失与重复发送。

### test.sh（摘要结果）

运行：`cd practice50 && ./test.sh`

实测结果（Asia/Shanghai，本机验证）：

- PASS: server listening on 127.0.0.1:18050 (pid=875118)
- PASS: ss -uan 可见 UDP 监听 :18050
- PASS: 基本 REQ/RSP 成功
- PASS: 响应 body 正确
- PASS: 能检测接收缓冲区截断 (MSG_TRUNC)
- PASS: 足够大的 recvbuf 不截断
- PASS: 丢包后有限重试最终成功
- PASS: 重试次数受控 sends=3 (期望约3, 上限6)
- PASS: 超时/上限后停止发送（不无限重试）
- PASS: 总发送次数受硬上限约束 sends=3 <=3
- PASS: 只接受匹配 req_id 的响应 (got B)
- PASS: 错配响应被计数忽略 (mismatched>=1)
- PASS: 请求ID匹配：快请求配到 FAST 而非迟到慢响应
- PASS: 重复响应：接受首个匹配，body=first
- PASS: 客户端注入丢包后有限重试仍可成功
- PASS: UDP 保留报文边界：两次 sendto 不会粘成一段
- ALL_CHECKS_PASSED

补充观察：

- `drop:2` 场景下 `sends=3` 后成功（前两次被服务端静默丢弃）
- 超时场景 `sends<=3` 后 `RESULT fail_timeout`，未无限重试
- 错 `req_id` 计入 `mismatched` 并忽略；快请求匹配 `FAST`，稍后才收到 `SLOW`
- `dup2` 场景 body 为 `first`（接受首个匹配）
- 两次 `sendto` 分别收到 `PART1` / `PART2`，不会粘成一段
- `MSG_TRUNC`：`recvbuf=80` 时 `truncated=1`；`recvbuf=20000` 时 `truncated=0`

NOTE：若环境无 `tcpdump`，抓包步骤请在有权限的机器上按课文执行。
