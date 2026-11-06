# 第 63/208 课：TCP流控、拥塞与队列

- **阶段**：第 4 阶段 · 高级后台工程师（TCP流控与拥塞）
- **前置课程**：d-tcp、a-math
- **关键词**：流控、拥塞控制、接收窗口、拥塞窗口、rwnd、cwnd、RTT、重传、BDP、带宽时延积、Nagle、延迟ACK、ss、tcpdump、队列
- **难度**：深入
- **所属领域**：D · 传输与Socket（子类：传输与Socket）
- **标签**：Flow and Congestion、D、C++

---

## 导读

上一课 Proactor / io_uring 解决的是 **「提交与完成」**：你把一次 IO 丢进 SQ，等 CQ 告诉你结果。那解决的是本机异步接口语义。

本课回到 **TCP 传输层**——即便你已经用上完成通知、协程、连接池，数据能不能「尽快、稳定地」从一端流到另一端，仍然受两道闸门约束：

> 流量控制保护接收方缓冲，拥塞控制适应网络承载能力，两者共同限制实际发送窗口。

拆开是三件事（对应本课 points）：

1. **接收窗口与拥塞窗口**：对端能吞多少（`rwnd`）与网络允许发多少（`cwnd`），发送方真正能发的是 `min(rwnd, cwnd)`。
2. **RTT、重传和带宽时延积**：管道里需要多少 in-flight 字节才能打满带宽；丢包触发重传并让 `cwnd` 回退。
3. **Nagle 和延迟 ACK**：小包合并与 ACK 延迟如何互动，以及何时该开 `TCP_NODELAY`。

微信公众号后台的典型痛点：

```text
模板消息 / 客服消息高峰
  → 接入层到下游推送网关、或网关到长连接节点
  → 监控：单连接吞吐掉崖，p99 延迟飙升，重传计数上涨
  → 可能原因 A：对端应用读得慢 → 接收缓冲占满 → rwnd 变小甚至 0（流控）
  → 可能原因 B：跨机房 / 公网链路拥塞丢包 → cwnd 收缩（拥塞控制）
  → 可能原因 C：本机发送队列（qdisc / socket send buffer）堆积 → 排队延迟放大
  → 不会用 ss / 抓包区分时，最常见的错误动作是：盲目加大 SO_SNDBUF / SO_RCVBUF
```

本课承接 d-tcp（连接生命周期、字节流、重传直觉）和 a-math（数量级与乘积估算），目标不是背拥塞算法名字，而是建立 **可操作的诊断框架**：看到吞吐低时，先分清是接收方慢、链路拥塞，还是本机队列问题——并且记住：**增大 buffer 不是所有延迟问题的解法**。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释接收窗口与拥塞窗口**
   - 说清 `rwnd`（接收窗口）由谁通告、保护什么；`cwnd`（拥塞窗口）由谁维护、保护什么。
   - 能写出有效发送窗口：`flight_size` 受 `min(rwnd, cwnd)`（再扣已在途未确认字节）约束。
   - 能在 `ss -ti` 输出里指出与窗口、RTT、重传相关的字段含义（至少 rwnd / cwnd / rtt / retrans）。

2. **能独立完成：在测试网络注入延迟丢包，观察 ss 与抓包中的窗口和重传变化**
   - 用 `tc netem`（或等价手段）注入延迟与丢包。
   - 用 `ss -ti`、`tcpdump` 观察窗口收缩、重传、RTT 变化。
   - 对照 Linux `tcp(7)` man-page，核对术语与边界条件，并记录内核/工具版本。

3. **满足本课验收标准（acceptance）**
   - **能区分流控与拥塞控制**。
   - **不把增大 buffer 作为所有延迟问题的解法**。

---

## 为什么后端要懂

节点里的理由是：

> 解释吞吐受 RTT、丢包和缓冲影响的原因。

这句话直接决定你在公众号后台排障时，会不会把「慢」一律归咎于业务代码或「机器不够」。

### 1. 吞吐不是「带宽标称值」

一条 1 Gbps 链路，并不意味着你的 TCP 连接能跑满 1 Gbps。粗略上，稳态吞吐受限于：

```text
吞吐 ≈ 有效窗口 / RTT
有效窗口 ≈ min(rwnd, cwnd, 本端发送缓冲相关上限…)
```

RTT 翻倍、窗口减半，吞吐都会明显掉。跨城、跨运营商、跨国链路的「带宽看起来很大但业务很慢」，往往是窗口 / RTT / 丢包在作怪，而不是 CPU 不够。

### 2. 丢包的代价不只是「再发一次」

TCP 发现丢包后会重传，并按拥塞控制规则收缩 `cwnd`（具体算法因内核与配置而异：Cubic、BBR 等行为不同）。一次突发丢包可能让连接长时间跑在「半速甚至更低」状态。高峰推送里，这表现为：连接还在、进程也正常，就是单连接吞吐掉崖。

### 3. 缓冲既是解药也是毒药

加大 `SO_SNDBUF` / `SO_RCVBUF`、加大网卡 / qdisc 队列，有时能支撑更高的带宽时延积（BDP），但也会：

- 掩盖应用读慢（流控本该早点反馈）；
- 放大排队延迟（bufferbloat）；
- 让「看起来不丢包」但延迟变得不可接受。

节点 tradeoffs 说得很直白：**大缓冲支持高带宽链路，也可能放大排队延迟。**

### 4. 公众号后台里你会真正碰到的场景

| 现象 | 更像流控 | 更像拥塞 | 更像本机队列 |
|------|----------|----------|--------------|
| 对端进程卡住 / 业务线程池堵死 | `ss` 里对端通告窗口很小，抓包可见 Window=0 / ZeroWindow | 否 | 可能伴随本端 send-q 上涨 |
| 跨机房丢包、链路抖动 | 否 | RTT 变大、retrans 上升、cwnd 下降 | 可能次生 |
| 本机出口打满、qdisc 背压 | 否 | 可能 | `tc -s`、`ip -s link`、send-q 持续高 |

不会区分时，值班同学最容易做的三件事：重启服务、加机器、把 buffer 调到最大值——其中至少有两件可能完全无效，甚至更糟。

---

## 直觉 → 原理

### 0. 先建立直觉：水管、水桶、阀门

把 TCP 发送想象成往对端倒水：

```text
发送方水箱 ──阀门──► 水管（网络）──► 接收方水桶
              ▲                ▲
              │                │
         拥塞阀门           流控阀门
         （cwnd）           （rwnd）
```

- **流控（Flow Control）**：接收方水桶快满了，就告诉你「别倒太快」——通告更小的 `rwnd`。保护的是**对端应用还没读走的缓冲**。
- **拥塞控制（Congestion Control）**：水管中间堵了（路由器队列溢出、丢包），发送方自己把阀门关小——缩小 `cwnd`。保护的是**共享网络**，避免继续把链路挤崩。
- **实际能倒多少**：两道阀门取更严的那道 → `min(rwnd, cwnd)`。

公众号推送高峰时，你看到的「慢」，可能是对端水桶满了（下游读慢），也可能是水管堵了（机房出口拥塞）。**工具上的区分点**后面会落到 `ss` 与抓包。

### 1. 接收窗口与拥塞窗口

#### 1.1 滑动窗口一眼看懂

TCP 用序列号标识字节流。发送方维护「已发送未确认」「可以发送」「还不能发送」三段：

```text
         已确认          在途 (in-flight)        窗口内可发         窗口外
|------|-------------|--------------------|-----------------|---------->
     SND.UNA      已发未ACK末尾         SND.UNA+SND.WND
                                         ↑
                         SND.WND ≈ min(rwnd, cwnd) 再扣策略细节
```

- **rwnd（Receive Window）**：接收方在 ACK 里通告「我现在还能收多少字节」。来自对端 TCP 通告，本质反映对端接收缓冲空闲空间（减去已接收未交付应用的数据等）。
- **cwnd（Congestion Window）**：发送方本地维护的拥塞窗口，单位通常是字节（实现细节看内核）。连接新建时从小开始探测（慢启动），根据 ACK / 丢包信号增减。
- **有效发送窗口**：发送方不会让 in-flight 超过 `min(rwnd, cwnd)`（再叠加本端发送缓冲、Nagle 等策略）。

#### 1.2 用 `ss -ti` 看真实连接

在 Linux 上，对一条 ESTABLISHED 连接：

```bash
# 替换成你的端口或地址
ss -ti '( dport = :8080 or sport = :8080 )'
```

典型输出里你会看到类似字段（具体键名随内核/iproute2 版本略有差异）：

```text
cubic wscale:7,7 rto:204 rtt:12.5/6.2 ato:40 mss:1448
cwnd:40 ssthresh:35 bytes_acked:... segs_out:...
send 46.5Mbps lastsnd:... lastrcv:...
pacing_rate ... delivery_rate ...
retrans:0/3 rcv_rtt:... rcv_space:...
```

阅读时抓住几件事：

| 字段直觉 | 含义 |
|----------|------|
| `cwnd` | 拥塞窗口（段数或与字节相关，看版本/显示） |
| `rtt` | 平滑 RTT / 均值方差一类信息 |
| `retrans` | 重传计数 |
| 对端窗口 / `rcv_space` 等 | 与接收侧空间相关的线索 |
| `send` / `delivery_rate` | 发送与交付速率估计 |

**诊断口诀**：

- 对端窗口很小、本端 `Send-Q` 堆积、抓包出现 `Window Update` / Zero Window → **偏流控**。
- `cwnd` 被压得很低、`retrans` 明显、RTT 抖动大 → **偏拥塞**。
- 两者可以同时发生；先分主因，再动手。

#### 1.3 短代码：观察发送阻塞与窗口

下面是一个教学向的最小 C++ 示例：服务端故意慢读，客户端狂写，观察客户端 `Send-Q` 与对端窗口变化（需在两台机器或 `tc` 环境下配合 `ss`/`tcpdump`）。

```cpp
// slow_reader_server.cpp —— 故意读得很慢，触发对端流控
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <thread>

int main() {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    int yes = 1;
    ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(9000);
    ::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    ::listen(fd, 16);

    int cfd = ::accept(fd, nullptr, nullptr);
    char buf[4096];
    for (;;) {
        // 每次只读一点，然后睡眠 —— 模拟业务线程池堵死
        ssize_t n = ::recv(cfd, buf, 256, 0);
        if (n <= 0) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    ::close(cfd);
    ::close(fd);
    return 0;
}
```

```cpp
// fast_writer_client.cpp —— 尽快写满
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <vector>

int main(int argc, char** argv) {
    const char* ip = argc > 1 ? argv[1] : "127.0.0.1";
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);

    // 教学：可对比开/关 TCP_NODELAY（见第 3 节）
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(9000);
    ::inet_pton(AF_INET, ip, &addr.sin_addr);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        perror("connect");
        return 1;
    }

    std::vector<char> buf(64 * 1024, 'A');
    for (int i = 0; i < 10000; ++i) {
        ssize_t n = ::send(fd, buf.data(), buf.size(), 0);
        if (n < 0) {
            perror("send");
            break;
        }
        // 另开终端：ss -ti dst :9000
    }
    ::close(fd);
    return 0;
}
```

观察命令：

```bash
# 看 Send-Q / Recv-Q 与 TCP 细节
watch -n 0.5 'ss -tin dst :9000 or src :9000'

# 抓窗口字段（Window）与 ZeroWindow
sudo tcpdump -ni any port 9000 -vv | head -n 80
```

当你看到对端 Window 持续变小甚至 0，而本端 `Send-Q` 上涨、应用 `send` 阻塞或返回短期可写字节变少——这就是 **流控在起作用**，不是「网卡坏了」。

#### 1.4 流控 vs 拥塞：一张对照表

| | 流控 Flow Control | 拥塞控制 Congestion Control |
|--|-------------------|-----------------------------|
| 保护对象 | 接收方缓冲 / 接收应用 | 网络路径上的共享资源 |
| 信号来源 | 对端通告的 rwnd（含 Zero Window） | 丢包、ECN、延迟、ACK 节奏等（算法相关） |
| 主要变量 | rwnd | cwnd（及 ssthresh 等） |
| 典型根因 | 应用读慢、业务卡死、下游反压 | 链路丢包、瓶颈队列溢出、跨网抖动 |
| 错误处方 | 只加本端 buffer | 只加本端 buffer |

**验收点回扣**：能区分二者；不要默认「加大 buffer」。

### 2. RTT、重传和带宽时延积

#### 2.1 BDP：管道里要装多少水

带宽时延积（Bandwidth-Delay Product）：

```text
BDP = 带宽 × RTT
```

例子（心算，对应 a-math 的数量级感）：

```text
带宽 1 Gbps = 125 MB/s
RTT  40 ms  = 0.04 s
BDP ≈ 125 MB/s × 0.04 s = 5 MB
```

含义：要想「跑满」这条管道，连接上大约需要 **约一个 BDP 的 in-flight 数据**（未确认字节）。如果 `min(rwnd, cwnd)` 远小于 BDP，吞吐就会被窗口卡住：

```text
吞吐上限 ≈ 窗口 / RTT
若窗口 = 64KB，RTT = 40ms
→ ≈ 64KB / 0.04s ≈ 1.6 MB/s  ≪ 1 Gbps
```

这就是「专线带宽很大，单连接拷文件却只有几 MB/s」的经典原因之一：窗口 / 缓冲没跟上 BDP，或丢包把 `cwnd` 压下去了。

#### 2.2 丢包 → 重传 → cwnd 回退

可靠传输不是「不丢」，而是「丢了能发现并重发」：

```text
发送段 ──► (网络丢了) ──► 接收方缺口
                │
                ├─ 重复 ACK / SACK → 快速重传路径（常见）
                └─ 超时 RTO → 超时重传（更痛，窗口回退通常更狠）
```

对公众号后台的工程含义：

1. **偶发丢包**：一次快速重传，短暂抖动。
2. **持续丢包 / 高 RTT 方差**：`cwnd` 起不来，长尾延迟恶化，模板消息「有的很快、有的很慢」。
3. **误判**：把拥塞当流控，去重启对端业务；或把流控当拥塞，去找网络组——都会浪费时间。

#### 2.3 用命令把 RTT 与重传看见

```bash
# 连接级
ss -ti dst <对端IP>:443

# 系统级重传统计（字段随内核）
nstat -az | grep -iE 'TcpRetrans|TcpExtTCP|TCPTimeout'
# 或
cat /proc/net/snmp | tr ' ' '\n' | paste - - | grep -i tcp
```

抓包看重传（tcpdump / wireshark 都会标 retransmission）：

```bash
sudo tcpdump -ni eth0 host <对端> and tcp -vv -S
# 同一序列号区间反复出现 → 重传嫌疑
```

#### 2.4 和缓冲的关系：为什么「加大 buffer」有时有效有时有害

- **有效时**：原先 `SO_RCVBUF`/`SO_SNDBUF` 太小，撑不起 BDP，窗口上不去；适当增大后 `rwnd` 通告变大，或本端能积更多 in-flight。
- **有害时**：
  - 应用读慢 → 更大的接收缓冲只是延迟 Zero Window，**让问题更晚暴露**，端到端延迟更大；
  - 瓶颈链路队列很长 → 更大缓冲/队列 → **bufferbloat**，RTT 被排队拉高，吞吐公式里分母变大；
  - 掩盖真实反压，上游继续灌数据，最终在别处爆（磁盘、线程池、消息队列）。

节点 pitfalls：

> 吞吐低可能是应用读取慢，也可能是网络拥塞。

节点 tradeoffs：

> 大缓冲支持高带宽链路，也可能放大排队延迟。

把两句话钉在值班手册第一页并不过分。

### 3. Nagle 和延迟 ACK

#### 3.1 问题从哪来：小包太多 vs ACK 太晚

TCP 默认有两套「省包」策略，本意都是减少小报文：

1. **Nagle 算法**：若已有小块数据在途未确认，新的小 `send` 可能被暂存，凑得更满或等 ACK 再发（细节以实现为准）。
2. **延迟 ACK**：接收方不一定每个段立刻 ACK，可能等待几十毫秒，期望与反向数据「捎带」ACK，或合并多个段的确认。

两者叠在一起，会出现经典交互：

```text
客户端小请求（Nagle 等 ACK）  ←→  服务端延迟 ACK（等更多数据或超时）
结果：每个小 RPC 多出 ~40ms 量级的「空等」（视实现与 sysctl）
```

公众号后台里，若某条内部协议是「大量小包请求/响应」且延迟敏感（实时客服消息、在线状态），这种交互会把 p99 拉高，而带宽其实很空。

#### 3.2 `TCP_NODELAY`：什么时候关 Nagle

```cpp
int one = 1;
if (setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)) < 0) {
    perror("TCP_NODELAY");
}
```

经验法则（不是教条）：

| 场景 | 建议 |
|------|------|
| 大块吞吐（批量落盘、大文件、批量推送体） | 通常保持默认即可；盲目 NODELAY 未必更快 |
| 交互式小消息、RPC、游戏/IM 长连接心跳与信令 | 常开 `TCP_NODELAY`，避免 Nagle×延迟 ACK |
| 应用自己已经做了良好批量组包 | 开不开差异变小；关键在应用层聚合 |

注意：开 `TCP_NODELAY` **不是**拥塞控制的替代品，也 **不会**修复 rwnd=0。它只影响「小段何时允许发出」。

#### 3.3 延迟 ACK 与应用层设计

你能控制的往往是应用行为：

- **请求-响应尽量可捎带**：同连接上尽快有反向数据，ACK 更容易被捎带。
- **避免「写一点就等读一点」的半包乒乓**，除非协议必须如此；可改为流水线或批量。
- 用抓包确认：是否每个小请求都单独成段、是否 ACK 明显滞后。

```bash
# 看小段与 ACK 时间间隔（示例）
sudo tcpdump -ni any port 9000 -tttt
```

#### 3.4 和「队列」放在一起看

本课标题含「队列」，在工程上至少有三层队列，别混为一谈：

```text
应用写缓冲 / 用户态队列
    → socket 发送缓冲（受 SO_SNDBUF 等约束）
        → qdisc / 网卡环（本机出口排队）
            → 路径上路由器/交换机队列（拥塞真正发生处）
                → 对端 socket 接收缓冲（rwnd 相关）
                    → 应用读队列 / 业务线程池
```

- 流控主要卡在「对端接收缓冲 ↔ 应用读」这一端。
- 拥塞控制主要响应「路径队列溢出 / 延迟信号」。
- 本机 qdisc 过深 → 本地 bufferbloat。
- 业务线程池堵死 → 应用读慢 → 通告 rwnd↓ → 对端看起来「网络很慢」。

排障时沿着这张图从上到下（或从下到上）逐层看 `Send-Q`/`Recv-Q`、`ss -ti`、`tc -s qdisc`、抓包 Window/retrans，比只盯业务日志快得多。

---

## 常见误区与设计权衡

### 误区 1：吞吐低 = 网络差

**真相**：吞吐低可能是应用读取慢，也可能是网络拥塞（节点 pitfalls）。还有可能是本端发送队列堆积、CPU softirq、磁盘反压导致业务不读。

**正确动作**：先用 `ss` 看 `Send-Q`/`Recv-Q`、窗口与 `retrans`，再用抓包确认 Zero Window vs 重传，再决定找应用还是找网络。

### 误区 2：把所有延迟问题都用「加大 buffer」解决

**真相**：大缓冲能支撑高 BDP，也可能放大排队延迟（节点 tradeoffs）。对「应用读慢」加大接收缓冲，只是把反压延后；对「已经拥塞」加大发送缓冲，可能让更多数据堆在路上，RTT 更差。

**正确动作**：先识别瓶颈是窗口类型（rwnd/cwnd）还是队列类型（qdisc/应用），再调参；调参后要看 **延迟分布**，不能只看平均吞吐。

### 误区 3：`cwnd` 变小一定是「被攻击了」或「内核 bug」

**真相**：丢包、超时、路径变化都会让拥塞控制主动降速——这是保护网络的正常行为。先看丢包率、RTT、ECN/队列，而不是先怀疑 TCP 实现。

### 误区 4：开了 `TCP_NODELAY` 就等于优化完 TCP

**真相**：NODELAY 只触及 Nagle。流控、拥塞、BDP、应用读速、线程池，一个都不会因此自动变好。

### 误区 5：单连接跑不满带宽 = 必须多连接「堆并行」

**真相**：有时确实需要多流（尤其应用层已分片），但应先检查单连接是否窗口不足、丢包过多、CPU 单核瓶颈。盲目多连接可能把拥塞恶化，并放大服务端 accept/fd 压力——公众号接入层尤其要小心 fd 与连接状态堆积（回顾 d-tcp）。

### 设计权衡小结

| 选择 | 收益 | 代价 |
|------|------|------|
| 增大 socket buffer | 更易打满高 BDP 链路 | 延迟↑、掩盖反压、内存占用↑ |
| 减小 qdisc / 启用 AQM（如 fq / fq_codel 等，视环境） | 降低排队延迟 | 配置复杂；错误配置可能损吞吐 |
| `TCP_NODELAY` | 降低小消息交互延迟 | 小包增多，带宽浪费与 pps 压力 |
| 应用层批量 | 提高效率、减少交互延迟陷阱 | 增加组包复杂度与尾延迟（等批量） |
| 多连接并行 | 可能提高总吞吐 | 连接管理、拥塞叠加、故障面变大 |

---

## 动手实践

> 实践目标（节点 practice）：在测试网络注入延迟丢包，观察 ss 与抓包中的窗口和重传变化。  
> 验收（acceptance）：能区分流控与拥塞控制；不把增大 buffer 作为所有延迟问题的解法。  
> sourceStudy：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

### 环境与版本记录（先做）

```bash
uname -r
ip -V
ss -V 2>&1 | head -1
man -w 7 tcp
# 把上面输出抄进实验笔记：内核、iproute2、man-pages 版本
```

打开并阅读：

[https://man7.org/linux/man-pages/man7/tcp.7.html](https://man7.org/linux/man-pages/man7/tcp.7.html)

重点检索与本课对齐的关键词：`TCP_NODELAY`、buffer、window、cong（拥塞相关 socket 选项，若文档有）、`TCP_MAXSEG` 等。把「本机内核是否支持某选项」写成 checklist，而不是抄博客默认值。

### 步骤 1：准备一条可观测的 TCP 流

任选其一：

- 用本课「慢读服务端 + 快写客户端」；
- 或 `iperf3` / `nc` 产生稳定流量。

```bash
# 终端 A
iperf3 -s -p 5201

# 终端 B
iperf3 -c 127.0.0.1 -p 5201 -t 60
```

另开终端持续观察：

```bash
watch -n 0.5 'ss -tin sport = :5201 or dport = :5201'
```

先记录「基线」：rtt、cwnd、retrans、吞吐。

### 步骤 2：注入延迟（只加延迟，先不加丢包）

需要 `NET_ADMIN` 权限（本地网卡或 netns）。示例对 `lo` 或某 veth（**不要在生产网卡上盲跑**）：

```bash
# 示例：给 lo 加 50ms 延迟（实验完务必删掉）
sudo tc qdisc add dev lo root netem delay 50ms

# 观察 ss 中 rtt 是否上升，吞吐如何变
ss -ti dport = :5201

# 清理
sudo tc qdisc del dev lo root
```

期望直觉：RTT↑ → 同样窗口下吞吐↓；若要维持吞吐，需要更大的 in-flight（更大有效窗口 / 缓冲），这正是 BDP 的含义。

### 步骤 3：注入丢包，观察重传与 cwnd

```bash
sudo tc qdisc add dev lo root netem delay 20ms loss 2%

# 再跑 iperf3 / 自写客户端，同时：
ss -ti dport = :5201
nstat -az | grep -i retrans

sudo tcpdump -ni lo port 5201 -vv -c 100

# 清理
sudo tc qdisc del dev lo root
```

在笔记里回答：

1. `retrans` 是否上升？
2. `cwnd` 相对基线是变大还是被压住？
3. 这更像流控还是拥塞？为什么？（提示：你并没有让接收方变慢。）

### 步骤 4：构造流控对照实验

跑「慢读服务端 + 快写客户端」（无 netem 丢包）：

```bash
# 终端 A：慢读
./slow_reader_server

# 终端 B：快写
./fast_writer_client 127.0.0.1

# 终端 C：
watch -n 0.2 'ss -tin sport = :9000 or dport = :9000'
sudo tcpdump -ni lo port 9000 -vv | tee /tmp/flow-win.txt
```

在笔记里回答：

1. 是否出现 Window 变小 / Zero Window？
2. 本端 `Send-Q` 是否上涨？
3. 此时加大客户端 `SO_SNDBUF` 能否从根上解决问题？为什么不能作为「所有延迟问题的解法」？

### 步骤 5：对比 Nagle / TCP_NODELAY（可选但推荐）

写一个「每次 send 1 字节请求、等 1 字节响应」的小 ping-pong，分别开关 `TCP_NODELAY`，量 1000 次 RTT。

```text
记录表：
| 配置           | 平均 RTT | p99   | 抓包是否见延迟 ACK 间隙 |
|----------------|----------|-------|-------------------------|
| 默认           |          |       |                         |
| TCP_NODELAY=1  |          |       |                         |
```

### 步骤 6：sourceStudy 核对清单

对照 man7 `tcp(7)` 与本机 `man 7 tcp`：

- [ ] 文档中关于发送/接收缓冲的说明与默认行为
- [ ] `TCP_NODELAY` 语义
- [ ] 与窗口/拥塞相关的可调选项（若有）及版本边界
- [ ] 错误路径：`setsockopt` 失败时你的程序如何处理（不要静默忽略）

写明：**适用内核版本、发行版、以及你验证过的边界条件**（例如容器里无 `NET_ADMIN` 时如何改用 netns 或同学协助）。

### 步骤 7：验收清单

- [ ] 能用自己的话区分流控与拥塞控制（保护对象、信号、典型现象）
- [ ] 完成「延迟 + 丢包」注入实验，并保存 `ss`/抓包关键片段
- [ ] 完成「慢读」流控对照，能指出 Window / Send-Q 证据
- [ ] 明确写出：**至少一种「加大 buffer 无效甚至有害」的情形**
- [ ] sourceStudy 笔记含版本与边界

---

## 自测题

**题 1.** 用自己的话说明：流量控制与拥塞控制分别保护什么？有效发送窗口为什么常常写成与 `min(rwnd, cwnd)` 相关？在公众号推送高峰时，若 `ss` 显示对端窗口很小且抓包有 Zero Window，优先怀疑应用问题还是链路拥塞？

**题 2.** 带宽 400 Mbps、RTT 80 ms，BDP 大约多少字节？若有效窗口只有 128 KB，粗算吞吐上限约为多少？若此时有人建议「把 SO_RCVBUF 调到 64 MB 一定能好」，你如何用本课的 pitfalls / tradeoffs 回应？

**题 3.** 解释 Nagle 与延迟 ACK 可能如何叠加拉高小 RPC 延迟。`TCP_NODELAY` 解决的是哪一层问题？它能否替代拥塞控制或修复 rwnd=0？动手实践里，如何用 `tc netem` + `ss`/`tcpdump` 区分「丢包导致的拥塞回退」与「慢读导致的流控」？

### 自测题参考答案

**题 1.** 流控保护接收方缓冲，防止发送方超过对端处理能力；拥塞控制保护网络共享路径，避免把瓶颈链路挤崩。发送方 in-flight 受两道限制中更严者约束，故与 `min(rwnd, cwnd)` 相关。Zero Window / 对端窗口极小优先怀疑**对端应用读慢或反压（流控）**，而不是先怪链路；若同时看到大量 retrans 与 cwnd 塌缩，再并行排查拥塞。

**题 2.** \(400\ \mathrm{Mbps} = 50\ \mathrm{MB/s}\)，\(80\ \mathrm{ms}=0.08\ \mathrm{s}\)，BDP \(\approx 50\times 0.08 = 4\ \mathrm{MB}\)。窗口 128 KB 时吞吐粗算 \(\approx 128\mathrm{KB}/0.08\mathrm{s} \approx 1.6\ \mathrm{MB/s}\)。「无脑调到 64 MB」可能对高 BDP 有帮助，但若根因是应用读慢或路径拥塞，大会放大排队延迟、掩盖反压——**不能作为所有延迟问题的解法**；应先分清 rwnd/cwnd/队列，再按需调整并观察延迟分布。

**题 3.** Nagle 可能因等待 ACK 而暂存小段，延迟 ACK 又可能故意晚回 ACK，两者叠加造成「你等我、我等你」的几十毫秒级空转。`TCP_NODELAY` 关闭 Nagle，改善小消息何时发出；**不**负责拥塞窗口，也**不**修复对端 rwnd=0。区分实验：`tc netem loss` 下应看到 retrans↑、cwnd 受压（拥塞侧）；慢读服务下应看到 Window↓/ZeroWindow、Send-Q↑（流控侧）。

---

## 推荐资源

1. [TCP流控、拥塞与队列：Linux man-pages](https://man7.org/linux/man-pages/man7/tcp.7.html)  
   - 来源：man7.org｜语言：英文｜类型：文档｜难度：深入｜免费  
   - 重点阅读 **接收窗口与拥塞窗口** 与 **RTT、重传和带宽时延积**，用于核对本节点实验的语义和边界。

建议同时查阅本机 `man 7 tcp`、`man tc-netem`、`man ss`，并把内核版本与工具版本写进实验记录。

---

## 本课小结

- **流量控制**保护接收方缓冲（`rwnd`）；**拥塞控制**适应网络承载能力（`cwnd`）；两者共同限制实际发送窗口：有效发送 ≈ 受 `min(rwnd, cwnd)` 约束。
- **吞吐受 RTT、丢包和缓冲影响**：粗算吞吐 ≈ 窗口 / RTT；BDP = 带宽 × RTT，决定「管道要多少 in-flight」；丢包触发重传并回退 `cwnd`。
- **Nagle 与延迟 ACK** 可能叠加抬高小消息延迟；`TCP_NODELAY` 按场景使用，不是万能开关。
- **队列有多层**：应用、socket、qdisc、路径、对端接收——排障要分层，不要只看业务日志。
- **误区**：吞吐低可能是读慢也可能是拥塞；**大缓冲能撑高 BDP，也可能放大排队延迟**——验收标准要求你真能区分，且不把加 buffer 当万金油。
- **实践**：`tc netem` 注入延迟/丢包 + 慢读对照；用 `ss -ti` 与抓包看窗口与重传；对照 `tcp(7)` 记录版本与边界。

---

## 配套练习建议

1. **默写对照表**：流控 vs 拥塞（保护对象、信号、`ss`/抓包证据、错误处方）。
2. **做完三组实验**：基线、netem 延迟、netem 丢包、慢读流控；把关键输出贴进笔记。
3. **BDP 算题**：挑一条你们机房到某下游的实测 RTT，按链路标称带宽算 BDP，对比当前 `ss` 窗口是否数量级匹配。
4. **代码审查**：在接入层/推送路径搜 `TCP_NODELAY`、`SO_SNDBUF`、`SO_RCVBUF`，对每一处写下「为什么这样配、对应哪类延迟」。
5. **值班演练**：给新人一个「吞吐掉崖」案例，只允许先问 `ss`/`tcpdump`/`nstat`，禁止先改 buffer。

下一课继续在传输与 Socket 脉络上推进；先确保你能独立讲清：何时是 rwnd、何时是 cwnd、何时是本机队列，以及为什么加大 buffer 不能包治百病。
