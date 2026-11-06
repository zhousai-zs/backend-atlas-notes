# 第 60/208 课：连接池与长连接复用

- **阶段**：第 3 阶段 · 传输与Socket
- **前置课程**：d-tcp、d-timer
- **关键词**：Connection Pooling、连接池、长连接、短连接、借用超时、空闲回收、失效探测、HTTP keep-alive、多路复用、HTTP/2、gRPC、brpc、池容量、下游并发
- **难度**：进阶
- **所属领域**：D · IO模型与事件（子类：调度与资源复用）
- **标签**：Connection Pooling、D、C++

---

## 导读

上一课我们解决的是**服务端侧**的执行资源复用：IO 线程只做读写与协议拆包，CPU/阻塞工作丢进有界工作线程池，结果再 `queueInLoop` 回投。线程池保护的是 EventLoop 的响应性。

本课把镜头转向另一侧——**客户端 / 下游侧**的连接资源复用。

公众号后台处理一条模板消息，往往不止碰一次自己的线程池，还要连续调用多个下游：

```text
粉丝触发 → 接入层 EventLoop
         → 工作线程：签名校验 / JSON 解析
         → 下游 RPC₁：用户资料服务（openid → 昵称/订阅状态）
         → 下游 RPC₂：素材/媒体服务（取图文、缩略图）
         → 下游 RPC₃：客服会话 / 模板消息下发通道
         → 结果回投 IO 线程写回
```

如果每一次下游调用都「新建 TCP → TLS 握手 → 发请求 → 关连接」，成本会非常难看：

| 步骤 | 典型开销（同机房量级，仅示意） |
|------|-------------------------------|
| TCP 三次握手 | 0.2–2 ms |
| TLS 握手 | 1–10+ ms（还可能有证书校验、会话票证未命中） |
| 应用层协议握手 / 鉴权 | 再加几毫秒 |
| 真正业务处理 | 可能只要 1–3 ms |

也就是说：**握手常常比业务本身还贵**。高峰时每秒几万次短连接，等于把 CPU、端口、TIME_WAIT、下游 `accept` 队列一起打满。

节点 summary 给出本课的核心命题：

> 连接池管理可复用连接的建立、借出、健康与回收，复用策略取决于协议是否支持多路复用。

拆开是三件事：

1. **池容量与借用等待**：池多大？满了是阻塞、失败，还是排队到超时？
2. **失效探测和空闲回收**：怎么发现对端已关、半开、空闲太久？怎么替换且不泄漏？
3. **独占连接与多路复用**：HTTP/1.1 keep-alive 一条连接同时只能跑一个请求；HTTP/2 / gRPC / brpc 可在单连接上多路并发——池模型完全不同。

本课承接 d-tcp（连接生命周期）和 d-timer（超时与空闲计时），不追求「池越大越好」，而是建立可上线的边界：

- 池过小会排队、借用超时；
- 池过大会把压力直接砸到下游，并浪费空闲 fd / 内存；
- TCP keepalive 只能证明「链路大致还在」，**不能代替业务截止时间**。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释池容量与借用等待**
   - 说清 `max_size`、空闲连接、借出连接、在建连接如何共同构成容量。
   - 说清借用超时（borrow timeout）的阻塞 / 失败语义，以及为什么在线请求通常要「有上限地等待」。
   - 能用指标回答：当前池深度、等待者数量、借用超时率、握手次数。

2. **能独立完成：实现带借用超时的连接池，比较短连接与长连接压测**
   - 实现：借用超时、空闲回收、broken 替换、RAII/Guard 防止借出不还。
   - 压测：同一负载下对比短连接与池化长连接的握手次数、CPU（或代理指标）、p99 延迟。
   - 验收：过期/失效连接可替换且无泄漏；报告包含握手、CPU、p99 变化。

3. **满足本课验收标准（acceptance）**
   - 过期连接可替换且无泄漏。
   - 报告包含握手、CPU 和 p99 变化。

---

## 为什么后端要懂

节点里的理由是：

> 降低频繁握手开销并限制下游并发。

这句话有两层，缺一不可。

### 1. 降低频繁握手开销

短连接的「正确」只在低频、无状态、对端不值得保活时成立。公众号后台这种「每条业务消息触发 2–N 次下游 RPC」的路径上，握手是纯浪费：

- 本机：更多 `connect`、更多临时端口、更多 TIME_WAIT；
- CPU：TLS 握手特别吃对称/非对称运算；
- 下游：`accept`、建会话、鉴权日志暴涨；
- 延迟：p50/p99 被握手尾巴拉高，业务再快也救不回来。

长连接复用把「建连」摊销到多次请求上。池化后握手次数近似等于「并发所需的连接上界」，而不是「请求总数」。

### 2. 限制下游并发

连接池不只是加速器，更是**舱壁**。`max_size=64` 意味着对某个下游，你最多同时占用 64 条连接（在独占模型下约等于 64 个在飞请求）。这比「工作线程想开多少并发就开多少」安全得多：

- 保护下游不被瞬时打爆；
- 保护本进程 fd / 内存；
- 把过载显式变成「借用等待 / 借用超时」，而不是悄悄把延迟拖到用户超时。

上一课的线程池限制的是**本机执行并发**；本课的连接池限制的是**对下游的占用并发**。两者经常要一起调。

### 3. 和上一课的分工

| 资源 | 上一课：线程池 | 本课：连接池 |
|------|----------------|--------------|
| 复用对象 | 工作线程 | TCP/TLS/RPC 连接 |
| 主要保护 | EventLoop 不被慢任务堵住 | 握手开销 + 下游容量 |
| 满了怎么办 | 有界队列拒绝 / 背压 | 借用等待或超时失败 |
| 泄漏形态 | 任务引用已销毁 loop | 借出不还、broken 不销毁 |
| 典型超时 | 任务执行超时 / 停机 join | 借用超时、空闲超时、RPC deadline |

---

## 直觉 → 原理

### 0. 先建立直觉：连接是「贵且有限的通行证」

把下游服务想成一座只开有限个闸机的场馆：

- 每次买票入场再立刻撕票离场（短连接）→ 闸机前永远在重新检票；
- 办一张可复用的通票，用完还回去（连接池）→ 检票次数大降；
- 通票总数有上限（`max_size`）→ 同时在馆内的人不会无限增长；
- 领票要排队，但有截止时间（borrow timeout）→ 排太久就放弃，别堵死整条业务；
- 通票可能过期或被判定无效（idle / health check）→ 要回收或换新票，不能把坏票继续借给别人。

协议如果支持「一张通票同时带多人」（多路复用），闸机模型又会变：你需要的通票数量可以更少，但单票上的流控、队头阻塞、取消语义更复杂。

---

### 1. 池容量与借用等待

#### 1.1 池里到底有什么

一个可运行的连接池，状态至少要能回答四个数：

```text
max_size        容量上限
idle            空闲可借的连接
in_use          已借出、正在执行请求的连接
creating        正在握手、已占名额但尚未可用的连接
```

容量约束通常是：

```text
idle + in_use + creating ≤ max_size
```

漏掉 `creating` 是常见坑：多个线程同时发现「池未满」一起 `connect`，会瞬时超过你以为的上限，把下游打出尖刺。

#### 1.2 借用（borrow）状态机

```text
borrow()
  ├─ 有健康 idle ？ → 借出，标记 in_use
  ├─ 未达 max_size ？ → 占位 creating → 握手 → 加入池并借出
  └─ 否则 → 等待归还 / 回收
        ├─ 在 borrow_timeout 内等到 → 再走上面分支
        └─ 超时 → 失败（抛错 / 返回错误码 / 返回 503）
```

归还（release）：

```text
release(conn, broken=false)
  ├─ broken 或 ping 失败 → 销毁，腾出名额，唤醒等待者
  ├─ 池已关闭 → 销毁
  └─ 否则 → 回到 idle，更新 last_used，唤醒等待者
```

#### 1.3 借用超时的语义：阻塞，但有界

「阻塞等待」和「立即失败」都有位置，但对在线 RPC，更常见的是**有界等待**：

| 策略 | 行为 | 优点 | 风险 | 适用 |
|------|------|------|------|------|
| 无限阻塞 | 一直等到有连接 | 不主动失败 | 调用方线程/协程被挂死，延迟无上界 | 几乎不适合在线路径 |
| 立即失败 | 无 idle 且已满则马上错 | 失败快 | 突发时误杀太多 | 可快速重试且有上游限流 |
| **借用超时** | 最多等 T | 吸收短突发，又有截止 | T 要和上游 deadline 对齐 | **默认推荐** |

关键点：借用等待只是整条调用 deadline 的一部分。若上游 RPC 总超时 100ms，借用再等 80ms，留给真实业务只剩 20ms——成功率会断崖下跌。借用超时应明显小于业务超时，并在日志里分开打点。

#### 1.4 代码骨架（Python）

```python
class ConnectionPool:
    def borrow(self) -> FakeConn:
        deadline = time.monotonic() + self.borrow_timeout
        with self._cv:
            while True:
                if self._closed:
                    raise PoolClosed()
                self._reclaim_idle_locked(time.monotonic())
                if self._idle:
                    c = self._idle.popleft()
                    if self.health_check_on_borrow and not c.ping():
                        self._destroy_locked(c)
                        continue
                    c.in_use = True
                    return c
                if len(self._all) + self._creating < self.max_size:
                    self._creating += 1
                    break  # 去锁外握手
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BorrowTimeout()
                self._cv.wait(timeout=remaining)
        # 锁外 connect/handshake，避免堵死借还
        ...
```

注意两件实现细节：

1. **握手必须在锁外**：`connect` + TLS 可能要数毫秒到数十毫秒；持锁握手会让所有借还串行化。
2. **占位 `_creating`**：名额在发起握手前就占上，防止超卖。

#### 1.5 Linux 上观察连接与等待

```bash
# 看本进程到某下游的连接数（ESTABLISHED）
ss -tnp | grep ':port' | grep pid=<pid> | wc -l

# 看 TIME_WAIT 是否被短连接打爆
ss -s

# 看连接状态分布
ss -ant | awk '{print $1}' | sort | uniq -c | sort -nr

# 粗看握手/连接建立延迟（对端需配合；或用应用埋点）
# 应用指标更可靠：handshake_count、borrow_wait_ms、borrow_timeout_total
```

应用层至少要有：

```text
pool_idle                 空闲连接数
pool_in_use               借出连接数
pool_creating             在建连接数
pool_size / pool_max      当前/最大
borrow_wait_seconds       借用等待直方图
borrow_timeout_total      借用超时次数
handshake_total           握手次数
handshake_seconds         握手耗时
conn_reclaimed_idle_total 空闲回收次数
conn_replaced_broken_total 失效替换次数
```

没有 `borrow_wait` 和 `handshake_total`，你分不清「池太小」还是「下游太慢」。

---

### 2. 失效探测和空闲回收

#### 2.1 连接为什么会「看起来活着，其实已经死了」

长连接复用后，连接会在空闲时躺在池里。下列情况都会让你借到「坏连接」：

- 对端重启、进程崩溃，RST 还没到达或被中间设备吃掉；
- NAT / L4LB / 防火墙空闲超时悄悄丢掉会话；
- 对端主动 close，本端还没读到 EOF；
- TLS 会话或应用层登录态过期；
- 半开连接：本端还能 `write`，但响应永远不来（直到超时）。

所以池必须有：**空闲回收 + 借用时探测 + 使用中失败后标记 broken**。

#### 2.2 空闲回收（idle timeout）

```text
若 now - last_used > idle_timeout：
  从 idle 队列移除并 close
  腾出名额，允许后续重建
```

`idle_timeout` 通常要**小于**中间设备的空闲砍线，又不能太短以至于每分钟都在重建。常见起点：

- 同机房直连：30s–5min；
- 经过云 LB / NAT：更保守，先对齐对方文档的 idle 上限（例如 60s）再留余量。

空闲回收可以用：

- 借用路径懒回收（实现简单，本课实践采用）；
- 后台定时扫描（更及时，但要和借还锁协调）。

这和 d-timer 一脉相承：空闲截止时间是定时器问题，不是「有空再看一眼」。

#### 2.3 失效探测（health check）

常见三档：

| 档位 | 做法 | 成本 | 漏检风险 |
|------|------|------|----------|
| 借用时轻量检查 | 看 socket 错误、`poll` 是否可读到 EOF、应用 `ping` | 低–中 | 仍可能有 TOCTOU |
| 周期性心跳 | 空闲连接定时 ping | 中 | 误杀：短暂抖动当死亡 |
| 失败驱动 | RPC 超时/协议错 → mark broken → 销毁 | 低 | 第一次请求可能踩坑 |

生产系统通常组合使用：**失败驱动为主，借用前轻量检查为辅，必要时加低频心跳**。

伪代码：

```python
def release(self, c, *, broken=False):
    with self._cv:
        if broken or not c.ping():
            self.replaced_broken += 1
            self._destroy_locked(c)
            self._cv.notify()
            return
        c.in_use = False
        c.last_used_at = time.monotonic()
        self._idle.append(c)
        self._cv.notify()
```

验收要求「过期连接可替换且无泄漏」：

- 过期 / broken 的连接必须 `close` 并从 `_all` 移除；
- 借出必须有对称的归还（Guard / `defer` / `finally`）；
- 池 `close()` 后，空闲连接销毁；仍在途的连接在 `release` 时销毁，不能回流 idle。

#### 2.4 泄漏的典型样子

```text
borrow() 成功
  → 业务异常 / 提前 return / 忘了 finally
  → 连接既不在 idle，也不再被任何人 release
  → in_use 只增不减
  → 最终所有名额耗尽，所有人 BorrowTimeout
```

这比「池太小」更隐蔽：监控上 `pool_in_use == max_size`，但下游 QPS 很低。务必用 RAII：

```python
with ConnectionGuard(pool) as conn:
    do_rpc(conn)
# 异常也会 release；可 mark_broken 后销毁
```

C++ 里同理：`std::unique_ptr` 自定义 deleter，或 `absl::Cleanup`。

#### 2.5 用实验验证替换与无泄漏

本课实践目录 `/workspace/atlas/practice60/` 中：

- `test_idle_reclaim_and_replace_broken`：空闲超时后旧连接被回收，broken 归还后被替换；
- `test_no_leak_with_guard`：多线程借用，即使抛错也不留 `in_use`。

---

### 3. 独占连接与多路复用

#### 3.1 复用策略取决于协议

节点 summary 的后半句往往被忽略：

> 复用策略取决于协议是否支持多路复用。

两种极端：

**A. 独占连接（HTTP/1.1 keep-alive 典型模型）**

```text
一条连接同一时刻只承载 1 个请求（或严格串行 pipelining，实践中很少用）
并发 N 个在飞请求 ≈ 需要约 N 条连接
池的 max_size ≈ 对该下游的最大并发
```

**B. 多路复用（HTTP/2、gRPC、许多 RPC 框架如 brpc）**

```text
一条连接上可同时跑多个 stream / 相关请求
并发主要由「每连接流控窗口 + 连接数」共同决定
池可能很小（每实例 1–4 条），甚至「单连接 + 多路」
```

把 HTTP/2 的池按 HTTP/1.1 的直觉调到几百，往往会：

- 浪费握手和内存；
- 在 LB 上制造不必要的连接风暴；
- 反而失去多路复用的队头优化收益（或踩到另一类队头阻塞）。

#### 3.2 HTTP/1.1 keep-alive：长连接，但仍是独占

```http
Connection: keep-alive
```

它解决的是「别每次短连接」，**不是**「一条连接并行打多个请求」。浏览器对每主机连接数有上限，服务端客户端库也通常是：

```text
borrow conn → 发请求 → 读完整响应 → release conn
```

因此 HTTP/1.1 客户端连接池几乎就是本课的独占池模型。

#### 3.3 HTTP/2 / gRPC：池的「容量」含义变了

```text
max_concurrent_streams   单连接最大并行流
connection_window        连接级流量窗口
stream_window            流级窗口
```

这时：

- 「借连接」可能变成「在已有连接上开 stream」；
- 连接数可以很少；真正限制吞吐的是窗口与下游处理能力；
- 单连接故障的爆炸半径更大（一条连接挂掉，上面所有 stream 一起失败）——所以仍常保留少量连接做分散。

#### 3.4 brpc 客户端视角

本课推荐资源是 brpc 的 Client Basics：

- [https://brpc.apache.org/docs/client/basics/](https://brpc.apache.org/docs/client/basics/)

阅读时重点对照：

- 连接建立与复用如何配置；
- 超时（连接超时、RPC 超时）与重试；
- 连接池 / 单连接 / 连接类型对并发的影响；
- 错误与健康检查路径。

brpc 这类框架通常把「协议是否多路复用」消化在客户端通道里，但**你仍然要理解**：你设的「连接数」到底限制的是 socket 数还是在飞请求数。

#### 3.5 同一业务，两种池模型对比

假设下游 QPS 目标 8000，平均在途时间 10ms，则平均在飞约 `8000 × 0.01 = 80`。

| 协议模型 | 粗算连接数 | 备注 |
|----------|------------|------|
| HTTP/1.1 独占池 | ≈ 80–120（含余量） | `max_size` 直接约等于并发 |
| HTTP/2 多路 | 例如 4 条连接 × 每连接几十 stream | 还受 `max_concurrent_streams` 限制 |
| 短连接 | ≈ 请求数（无复用） | 握手与 TIME_WAIT 爆炸 |

粗算只是起点；最终仍靠压测看握手次数、p99、下游 CPU、错误率。

#### 3.6 短连接 vs 长连接压测在证明什么

本课实践 `bench.py` 在同一 `workers × reqs` 下对比：

```text
短连接：每次请求都付出 handshake_ms
池化：  预热后握手次数 ≈ pool_size，请求延迟接近 work_ms
```

一次本地实测（`workers=6, reqs=12, handshake=4ms, work=1ms, pool_size=6`）：

```text
short : handshakes=72  p99≈5.3ms  qps≈1122  cpu_proxy≈372ms
pooled: handshakes=6   p99≈1.2ms  qps≈4665  cpu_proxy≈78ms
```

解读：

- **握手**：72 → 6，降了一个数量级；
- **p99**：从「握手+业务」变成接近「业务」；
- **CPU 代理（累计活跃时间）**：显著下降——对应真实世界里 TLS/系统调用开销下降；
- **QPS**：同硬件下池化吞吐明显更高。

这正是 acceptance 要的报告：握手、CPU、p99 变化。

---

## 常见误区

### 1. TCP 保活不能代替业务截止时间（节点 pitfalls）

节点 pitfalls：

> TCP保活不能代替业务截止时间。

展开讲：

- **TCP keepalive**（`SO_KEEPALIVE`、`tcp_keepalive_time` 等）默认往往是**小时级**探测，目的是清理死连接，不是做 RPC 超时；
- 即便你把 keepalive 调到秒级，它也只能回答「对端 TCP 栈是否还响应探测包」，**不能**回答「这个业务请求是否在 50ms 内返回」；
- 半开故障、应用线程死锁、下游队列堵住时，TCP 连接仍可能显示 ESTABLISHED，keepalive 也长期成功；
- 业务必须有自己的 **RPC deadline / 读超时 / 总超时**，并且超时后要取消或丢弃结果，避免迟到响应写回。

正确分层：

```text
TCP keepalive     → 清理长时间死亡的空闲连接（分钟～小时）
池 idle timeout   → 主动回收空闲，避免被中间设备默默砍线（秒～分钟）
借用超时          → 拿不到连接就快失败（毫秒～百毫秒）
RPC/业务 deadline → 单次调用的真正截止时间（毫秒～秒）
```

四者职责不同，**用 keepalive 「代替」业务超时是结构性错误**。

### 2. 借了不还 / 池泄漏

表现：`in_use` 缓慢爬到 `max_size`，之后全部 `BorrowTimeout`，但下游监控很闲。

根因：异常路径没有 `release`。

对策：Guard/RAII、`defer`、静态检查、泄漏检测指标（`in_use` 长期等于 `max` 且业务 QPS 低）。

### 3. 健康检查误杀

过于激进的 ping（短超时、高频率）会把短暂抖动当成死亡，导致：

- 连接被批量销毁；
- 紧接着握手风暴；
- 下游与本端 CPU 同时升高。

建议：失败要**连续多次**或**结合业务错误码**再标记 broken；抖动期用退避，而不是立刻清空池。

### 4. 把池当无限队列

有人把 `max_size` 调到很大，再把 `borrow_timeout` 调到很长，以为「总能等到连接」。这和上一课的无界队列同一类错误：

- 等待中的调用占着线程/协程；
- 延迟无上界；
- 下游一旦变慢，本端所有人一起陪葬。

池满时应该**快速失败或有界等待**，并把失败传导为限流 / 降级，而不是无限排队。

### 5. 无视协议模型，按 HTTP/1.1 经验调 HTTP/2 池

多路复用下「连接数 = 并发」不再成立。乱加连接可能提高尾延迟、打散缓存亲和，又吃掉内存。先问协议，再定池。

### 6. 只池化、不设下游超时

池化降低了握手，但请求仍可能在下游排队。没有业务超时，长连接只会让失败来得更晚、更同步。

---

## 设计权衡

节点 tradeoffs：

> 池过小排队，过大增加下游压力和空闲资源。

### 池过小

症状：

- `borrow_wait` 升高，`borrow_timeout` 增多；
- 本端线程池可能因等连接而阻塞（若借用在工作线程里同步做）；
- 用户侧 p99 变差，但下游未必满载。

处理：

- 先看是不是泄漏；
- 再看借用超时是否过短；
- 最后才谨慎增大 `max_size`，并观察下游 CPU / 排队。

### 池过大

症状：

- 空闲连接占 fd 与内存；
- 突发时瞬时并发打穿下游；
- 对端 `accept`、线程池、DB 连接被连带打满；
- 故障恢复时「重连风暴」更猛。

处理：

- 用 `max_size` 做硬舱壁；
- 结合限流、重试预算、熔断；
- 空闲回收避免长期占着峰值容量。

### 怎么选起点

独占模型粗算：

```text
所需并发 ≈ QPS × 平均在途秒数
max_size ≈ 所需并发 × (1.2 ~ 2.0)   # 余量看抖动
borrow_timeout << 业务超时
idle_timeout < 中间设备空闲砍线
```

然后压测校准：看握手次数是否仍过高（可能 idle 太短）、借用超时是否过高（可能池太小或下游太慢）、下游错误率是否随扩池上升（可能已经过大）。

### 与线程池联调

工作线程里同步 `borrow + RPC` 时：

- 连接池过小 → 工作线程堵在借用上 → 上一课的有界队列开始拒绝；
- 连接池过大 → 工作线程全部打下游 → 下游崩。

所以两者的容量要一起设计：**线程池限制本机执行，连接池限制出站占用**。

---

## 动手实践

实践目标（节点 practice / acceptance）：

> 实现带借用超时的连接池，比较短连接与长连接压测。  
> 过期连接可替换且无泄漏；报告包含握手、CPU 和 p99 变化。

配套目录：`/workspace/atlas/practice60/`

| 文件 | 作用 |
|------|------|
| `connection_pool.py` | 带借用超时、空闲回收、broken 替换的连接池 + `ConnectionGuard` |
| `bench.py` | 短连接 vs 池化长连接压测与报告 |
| `test_pool.py` | 验收用例 |
| `test.sh` / `run.sh` | 一键运行 |

### 步骤 1：跑通验收

```bash
cd /workspace/atlas/practice60
./test.sh
# 末行：ALL_CHECKS_PASSED
```

### 步骤 2：读懂借用超时

`test_borrow_timeout`：`max_size=1` 时借住不还，第二次借用应在 `borrow_timeout` 附近失败。

自行改超时时间，观察等待是否有界。

### 步骤 3：过期替换与无泄漏

`test_idle_reclaim_and_replace_broken`：

- 归还后睡眠超过 `idle_timeout`，再借用应触发回收/重建；
- `mark_broken` 归还后不得回流 idle；
- `leaked() == 0`。

`test_no_leak_with_guard`：多线程下即使抛错，`in_use` 最终为 0。

### 步骤 4：短连 vs 长连报告

`bench.py` 输出类似：

```text
=== short vs pooled report ===
short:  n=72 handshakes=72 ... p99=5.338ms ... cpu_proxy=371.64ms
pooled: n=72 handshakes=6  ... p99=1.238ms ... cpu_proxy=78.07ms
BENCH_OK
```

请在笔记中记录三列变化：

| 指标 | 短连接 | 池化长连接 | 变化 |
|------|--------|------------|------|
| 握手次数 | | | |
| CPU 代理 / 实际 CPU | | | |
| p99 | | | |

### 步骤 5：对照 brpc 文档做 sourceStudy

节点 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

打开：

- [连接池与长连接复用：参考文档 / 原始资料](https://brpc.apache.org/docs/client/basics/)

追踪清单：

| 问题 | 你在文档/实验里找到的答案 |
|------|---------------------------|
| 客户端如何配置连接超时与 RPC 超时？二者谁更接近「业务截止时间」？ | |
| 连接复用 / 连接池相关选项叫什么？默认偏向单连接还是池？ | |
| 文档如何描述错误处理与重试？失败连接是否自动淘汰？ | |
| 协议若支持多路复用，连接数与并发的关系如何表述？ | |
| 你阅读的文档版本/日期？与本课模拟池的语义差异？ | |

### 步骤 6：自己改参数做一张权衡表

建议矩阵：

```text
max_size: 2 / 6 / 32
borrow_timeout: 50ms / 500ms / 5s
idle_timeout: 100ms / 2s / 30s
负载: workers=8, reqs=30, handshake=5ms, work=2ms
```

观察：

- 池过小：timeouts 上升、p99 上升；
- 池过大：handshakes 在突发时上升（对下游压力的代理）、空闲资源占用上升；
- idle 过短：握手次数无意义地升高；
- borrow 过长：失败变慢，线程被挂住。

### 验收清单

```text
[ ] 能解释 max_size / idle / in_use / creating / borrow_timeout
[ ] 实现了带借用超时的连接池
[ ] 空闲超时连接可回收替换
[ ] broken 连接可销毁替换
[ ] Guard/finally 保证无泄漏
[ ] 短连接 vs 长连接报告含握手、CPU、p99
[ ] ./test.sh 输出 ALL_CHECKS_PASSED
[ ] 对照 brpc Client Basics 完成 sourceStudy 表格
[ ] 理解 TCP keepalive ≠ 业务 deadline
```

---

## 自测题

**题 1**  
某下游客户端连接池 `max_size=32`，监控长期显示 `in_use=32`、`idle=0`，但下游服务 CPU 很低、QPS 也不高。最可能的原因是？

A. 池过大，把下游压垮了  
B. 大量连接借出后未归还（泄漏），名额被占满  
C. TCP keepalive 间隔太短，连接被误杀  
D. 短连接比长连接更适合该场景，应关掉池

**题 2**  
关于 TCP keepalive 与业务超时，下列哪项正确？

A. 打开 `SO_KEEPALIVE` 后可以不再设置 RPC 超时  
B. keepalive 默认通常是秒级，足以替代读超时  
C. keepalive 用于清理长时间死亡连接；业务截止时间必须单独设置  
D. keepalive 失败就等于业务失败，二者语义相同

**题 3**  
HTTP/1.1 keep-alive 客户端池与 gRPC（HTTP/2）客户端在「需要多少连接才能支撑 100 个在飞请求」上，更合理的理解是？

A. 两者都大约需要 100 条连接  
B. HTTP/1.1 独占模型大约需要约 100 条连接；HTTP/2 可能用少得多的连接承载多路 stream  
C. HTTP/2 必须比 HTTP/1.1 使用更多连接才能多路复用  
D. 连接数只由 `idle_timeout` 决定，与协议无关

---

### 自测题参考答案

**题 1：B**  
下游很闲但本端 `in_use` 顶满，典型是借出未归还。A 会表现为下游忙；C 通常导致频繁握手而不是长期 `in_use` 占满；D 与现象无关。

**题 2：C**  
对应 pitfalls「TCP保活不能代替业务截止时间」。keepalive 与 RPC deadline 分层，职责不同。

**题 3：B**  
复用策略取决于协议是否支持多路复用。HTTP/1.1 keep-alive 仍是独占；HTTP/2/gRPC 可在单连接多路并发。

---

## 推荐资源

- [连接池与长连接复用：参考文档 / 原始资料](https://brpc.apache.org/docs/client/basics/)（brpc.apache.org，英文，文档，进阶，免费）
  - 重点阅读池容量与借用等待与失效探测和空闲回收，用于核对本节点实验的语义和边界。
  - 建议同时弄清：连接超时 vs RPC 超时、连接复用选项、错误/重试路径、以及多路复用协议下「连接数」的真实含义。
  - 阅读时记录文档版本与和本课模拟池的差异（本课是教学用独占池模型）。

---

## 本课小结

1. **连接池是下游侧的资源复用与舱壁**：降低握手开销，同时限制对下游的并发占用。
2. **容量 = idle + in_use + creating ≤ max_size**：握手要占位，且应在锁外进行。
3. **借用要有超时**：无限等连接会把过载变成无界延迟；借用超时必须嵌在业务 deadline 之内。
4. **空闲回收 + 失效替换 + 借出必还**：过期/broken 可替换且无泄漏，是上线底线。
5. **TCP keepalive ≠ 业务截止时间**：保活、idle、borrow、RPC deadline 分层配置。
6. **协议决定池模型**：HTTP/1.1 keep-alive 独占；HTTP/2/gRPC/brpc 多路复用下连接数与并发不再一一对应。
7. **池过小排队，过大压下游**：用压测看握手、CPU、p99 与下游错误率，而不是只看本端 QPS。
8. **和线程池联调**：线程池管执行，连接池管出站；任一无限化都会把问题转移到另一侧。

下一课会继续在传输与 Socket / 资源复用主题上推进。请先完成 `practice60` 验收，并对照 brpc 文档填完 sourceStudy 表，再进入后续内容。

---

## 配套练习建议

| 文件 | 作用 |
|------|------|
| `connection_pool.py` | 池实现与 Guard |
| `bench.py` | 短连/长连对比报告 |
| `test_pool.py` | 借用超时、回收替换、无泄漏、压测断言 |
| `test.sh` | 一键验收 |

运行期望：

```bash
./test.sh
# 末行：ALL_CHECKS_PASSED
```

建议压测参数：

```text
workers: 4 / 8 / 16
handshake_ms: 2 / 5 / 10
work_ms: 1 / 5
pool_size: 2 / 8 / 32
borrow_timeout: 0.05 / 0.5 / 2
```

观察：握手次数、借用超时次数、p50/p99、wall 时间、cpu_proxy（或 `time -v` / `perf`）。

进阶练习：

1. 把 FakeConn 换成真实 TCP（本地 echo server），用 `ss` 观察 ESTABLISHED / TIME_WAIT；
2. 为池增加「借出最长租期」，超时强制视为 broken；
3. 模拟 HTTP/2：单连接多 stream 许可桶，对比独占池的 `max_size` 语义；
4. 注入 1% 半开连接，比较「仅失败驱动」与「借用前 ping」的尾延迟；
5. 与上一课线程池组合：worker 内同步 borrow+RPC，画出双池容量的过载曲面。
