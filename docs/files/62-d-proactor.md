# 第 62/208 课：Proactor、完成通知与io_uring

- **阶段**：第 4 阶段 · 高级后台工程师（高性能IO与完成通知）
- **前置课程**：d-reactor、c-vm
- **关键词**：Proactor、完成通知、io_uring、SQ、CQ、SQE、CQE、liburing、缓冲区寿命、取消、就绪通知 vs 完成通知、IORING_OP_ASYNC_CANCEL、user_data
- **难度**：深入
- **所属领域**：D · IO模型与事件（子类：IO模型与事件）
- **标签**：Proactor and io_uring、D、C++

---

## 导读

上一课有栈协程解决的是**调用形态**：用同步写法组织非阻塞 IO，让业务代码别被回调拆碎。再往前，Reactor（d-reactor）解决的是**就绪通知**：`epoll` 告诉你「这个 fd 大概能读了」，你再自己去 `read`。

本课补上另一条轴——**完成通知（Proactor）**：

> 完成通知把操作提交与结果处理分开；io_uring 通过提交和完成队列提供异步接口，但支持范围依内核和操作而异。

拆开是三件事：

1. **SQ/CQ 与完成结果**：把「我要读这段」放进提交队列（SQ）；内核干完后把「读了多少 / 出错码」放进完成队列（CQ）。你不再问「能读吗」，而是等「读完了」。
2. **缓冲区寿命和取消**：SQE 提交后到对应 CQE 出现前，用户缓冲区必须一直有效；取消不是立刻消失，而是再发一条取消请求，**仍要等完成通知**。
3. **就绪与完成语义**：Reactor = 就绪通知 + 应用自己搬数据；Proactor = 提交操作 + 内核/框架搬完再通知。两者都能做高并发，选型看场景与内核能力。

公众号后台的典型场景：

```text
粉丝消息高峰
  → 接入层协程/线程池处理业务
  → 同时要：读本地模板/配置片段、写审计日志、批量刷会话落盘
  → 若全用「epoll 就绪 → read/write」：
       每次就绪仍要系统调用；短读/短写要自己循环；CPU 在用户态/内核态之间来回跳
  → 若对「本地文件 / 批量写」走 io_uring：
       批量提交 SQE → 一次（或很少次）enter → 从 CQ 收完成
       业务侧只处理「这次读到了 N 字节 / 失败码」
```

注意：节点明确说**支持范围依内核和操作而异**。不是「换了 io_uring 一切都异步无阻塞」。部署前要核验内核版本、操作码、安全策略（seccomp、SELinux、容器能力）。

本课承接 d-reactor（就绪模型）和 c-vm（进程地址空间 / 页缓存与用户缓冲区关系），重点建立边界意识：

- 提交成功 ≠ 操作已完成；
- 异步 API ≠ 每个操作在所有场景都不阻塞当前线程；
- 缓冲区在「完成前」不可回收——这是最容易写出 use-after-free 的地方。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 SQ/CQ 与完成结果**
   - 说清 SQ（Submission Queue）、CQ（Completion Queue）、SQE、CQE 各自角色。
   - 能说明 `user_data` 如何把「提交」与「完成」关联起来，以及 `res` 字段的含义（字节数 / 负 errno）。
   - 能口述一次 `io_uring_enter`（或 liburing 封装）大致做了什么。

2. **能独立完成：用 liburing 实现受控文件读取，验证提交失败、短读和取消完成**
   - 搭一个最小程序：打开文件 → 提交 `IORING_OP_READ`/`readv` → 收 CQE。
   - 主动构造三类现象：提交失败（非法 fd / 队列满策略）、短读（读长度小于请求）、取消后仍收到完成。
   - 对照 liburing examples 与当前内核文档，核验取消与完成约定。

3. **满足本课验收标准（acceptance）**
   - 能说明缓冲区何时可回收。
   - 性能比较记录内核、队列深度和数据路径。

---

## 为什么后端要懂

节点里的理由是：

> 区分就绪通知与完成通知并选择合适IO模型。

这句话直接决定你在公众号后台这类高并发服务里，什么时候继续用 epoll/Reactor，什么时候值得引入 io_uring / 完成端口式路径。

### 1. 两种「通知」语义完全不同

| | 就绪通知（Reactor） | 完成通知（Proactor） |
|--|---------------------|----------------------|
| 内核告诉你什么 | 「这个 fd 可能可读/可写了」 | 「你提交的那次操作做完了，结果是…」 |
| 数据谁搬 | 应用在通知后再 `read`/`write` | 提交时就带上缓冲区；完成时数据已就位（或失败） |
| 典型 Linux 接口 | `epoll`、`poll`、`select` | `io_uring`（以及历史上 AIO 等，能力不同） |
| 短读/短写 | 你自己循环 | 仍可能短读；以 CQE.`res` 为准，可能要再提交 |
| 取消 | 从兴趣集合删掉 fd / 改事件 | 提交取消操作，**等取消或原操作的完成** |

选错模型的常见症状：以为「异步了」就把缓冲区提前 `free`；或在 epoll 路径上幻想「一次就绪等于一次完整消息」。

### 2. 系统调用与批处理是真实成本

高峰时接入层每秒成千上万次小 IO。Reactor 路径常见模式：

```text
epoll_wait → 对每个就绪 fd → read/write（每次至少一次 syscall）
```

io_uring 可以把多次操作放进 SQ，再一次性（或摊销后很少次）进入内核，从 CQ 批量收结果。**可能有收益**——节点 tradeoffs 也提醒：收益不是免费的，安全限制与内核支持必须进部署条件。

### 3. 和协程、线程池怎么拼

```text
┌─────────────────────────────────────────┐
│ 业务协程 / 工作线程                       │
│   「我要读文件 / 写日志」                  │
└───────────────┬─────────────────────────┘
                │ 提交异步操作
                ▼
┌─────────────────────────────────────────┐
│ Proactor / io_uring 层                   │
│   SQ 提交 → 内核执行 → CQ 完成            │
└───────────────┬─────────────────────────┘
                │ 完成回调 / 唤醒协程
                ▼
┌─────────────────────────────────────────┐
│ 继续业务：解析模板、组包、回下游           │
└─────────────────────────────────────────┘
```

协程解决「写法」；完成通知解决「IO 通知语义与批处理」。两者正交：你可以在协程里 `await` 一次 io_uring 读，也可以在纯线程池里收 CQE。

---

## 直觉 → 原理

### 0. 先建立直觉：点菜与上菜

把餐厅当内核、你当应用：

- **Reactor（就绪）**：服务员喊「3 号桌厨房有空了」。你还得自己走去窗口拿菜、盛汤、可能来回跑几趟（短读）。
- **Proactor（完成）**：你填一张单（SQE）交给窗口：「番茄炒蛋一份，装进这个盘子」。菜好了，盘子连同小票（CQE）推到出餐口。你只处理「好了 / 沽清（错误）」。

盘子（缓冲区）从交单到出餐期间不能收回去洗碗——否则厨房往空桌倒菜（use-after-free）。

### 1. SQ/CQ 与完成结果

#### 1.1 两队列一眼看懂

```text
用户态                              内核态
┌──────────────┐                   ┌──────────────┐
│ Submission   │  ── sqe 批量 ──►  │  取出 SQE    │
│ Queue (SQ)   │                   │  执行 IO     │
│  ring + SQEs │                   │  写结果      │
└──────────────┘                   └──────┬───────┘
                                          │
┌──────────────┐                   ┌──────▼───────┐
│ Completion   │  ◄── cqe 批量 ──  │ Completion   │
│ Queue (CQ)   │                   │ 写入 CQ      │
└──────────────┘                   └──────────────┘
```

- **SQE（Submission Queue Entry）**：一条「请做这个操作」的描述：操作码、fd、缓冲区指针与长度、偏移、`user_data` 等。
- **CQE（Completion Queue Entry）**：一条「做完了」的结果：同一个 `user_data`、`res`（成功时为字节数，失败时为负的 errno）、flags。

liburing 把 mmap 出的环形队列和辅助函数包起来，典型节奏：

```cpp
#include <liburing.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstring>
#include <cstdio>

int main() {
    io_uring ring{};
    // entries: SQ/CQ 容量相关；具体上限看内核与参数
    if (io_uring_queue_init(8, &ring, 0) < 0) {
        perror("queue_init");
        return 1;
    }

    int fd = open("sample.dat", O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    char buf[64];
    memset(buf, 0, sizeof(buf));

    io_uring_sqe* sqe = io_uring_get_sqe(&ring);
    // 受控读取：从偏移 0 读最多 64 字节
    io_uring_prep_read(sqe, fd, buf, sizeof(buf), 0);
    // user_data 用来在完成时认出「这是哪次请求」
    io_uring_sqe_set_data(sqe, (void*)0x1);

    int submitted = io_uring_submit(&ring);
    if (submitted < 0) {
        // 提交失败路径：要能区分「没进队列」vs「进了但后来失败」
        fprintf(stderr, "submit failed: %d\n", submitted);
        return 1;
    }

    io_uring_cqe* cqe = nullptr;
    int ret = io_uring_wait_cqe(&ring, &cqe);
    if (ret < 0) {
        fprintf(stderr, "wait_cqe: %d\n", ret);
        return 1;
    }

    void* data = io_uring_cqe_get_data(cqe);
    int res = cqe->res;   // >=0 字节数；<0 为 -errno
    printf("user_data=%p res=%d\n", data, res);
    if (res >= 0) {
        // 短读：res 可能 < sizeof(buf)，例如文件只剩 10 字节
        printf("got %d bytes (short-read if < %zu)\n", res, sizeof(buf));
    } else {
        fprintf(stderr, "IO error: %s\n", strerror(-res));
    }

    io_uring_cqe_seen(&ring, cqe);
    close(fd);
    io_uring_queue_exit(&ring);
    return 0;
}
```

#### 1.2 `res` 与「完成结果」怎么读

- `res >= 0`：通常表示完成的字节数（读/写）；`0` 对读常表示 EOF。
- `res < 0`：`-errno`，例如 `-EAGAIN`、`-ECANCELED`、`-EINVAL`、`-EBADF`。
- **短读是一等公民**：请求 4K，可能只完成 512；是否继续提交下一次由应用决定。
- **提交失败 vs 完成失败**：
  - `io_uring_get_sqe` 返回空：SQ 暂时没空位（要先 submit / 收 CQ / 扩队列策略）。
  - `io_uring_submit` 负值：这一批没交进去（或部分语义依封装）。
  - CQE.`res` 负值：操作已进入环并完成，但是失败/取消。

实践里验收「提交失败」时，要故意制造可复现条件（非法参数在 prep 阶段、错误 fd、或打满队列后的策略），并记录是哪一层返回的错误。

#### 1.3 `user_data` 是关联纽带

高并发下同时飞着成百上千个 SQE。完成顺序**不保证**等于提交顺序。没有 `user_data`（或等价 cookie），你无法知道 CQE 对应哪次读。

常见做法：

```cpp
struct IoReq {
    enum { READ, WRITE, CANCEL } kind;
    int fd;
    char* buf;
    size_t len;
    // ...
};

IoReq* req = new IoReq{...};
io_uring_sqe_set_data(sqe, req);
// 完成时：
IoReq* done = (IoReq*)io_uring_cqe_get_data(cqe);
```

公众号后台批量刷审计日志时，常用「请求对象池 + user_data 指针」避免每次分配。

#### 1.4 和内核的一次握手：`submit` / `enter`

简化理解（具体标志位以 man page 为准）：

1. 用户往 SQ 环形区写入若干 SQE，更新提交端索引；
2. `io_uring_submit` → 底层 `io_uring_enter`，告诉内核「有新活」；
3. 内核执行（可能走异步 worker，也可能在线完成）；
4. 结果写入 CQ；用户 `wait_cqe` / `peek_cqe` 取走并 `cqe_seen`。

队列深度（init 时的 entries、以及实际 SQ/CQ 大小）直接影响：能飞多少未完成操作、批处理收益、内存占用。**做性能比较时必须记录：内核版本、队列深度、数据路径（缓冲 I/O vs O_DIRECT、是否注册缓冲区等）**——这正是 acceptance 要求。

查看本机能力的常用命令：

```bash
uname -r
# 许多发行版可查 io_uring 相关开关 / 安全策略
grep -i uring /boot/config-$(uname -r) 2>/dev/null | head
man io_uring_setup   # 本地 man
```

### 2. 缓冲区寿命和取消

#### 2.1 寿命规则（acceptance 核心）

**规则**：从 SQE 被提交、到对应 CQE 被你取走并处理完毕之前，SQE 引用的用户缓冲区必须保持有效且不被并发写入打乱语义。

```text
时间线：
  prep_read(buf)
  submit ──────────────────────────────► 内核可能正在 DMA/拷贝到 buf
       ▲                                      │
       │                                      ▼
       └── 此区间 buf 不可 free / 不可挪作他用  CQE 出现
                                              │
                                              ▼
                                         你处理完 res 后，才可回收 buf
```

对照 c-vm：那块 `buf` 是用户态虚拟地址；内核执行期间可能通过页缓存拷贝或（O_DIRECT 时）更严格的对齐/固定页要求。提前 `delete[] buf` 就是典型 UAF。

协程场景更坑：

```cpp
task<> read_file() {
    char local[4096];           // 栈上缓冲
    co_await uring_read(fd, local, sizeof(local));
    // 若 await 实现错误地在「仅 submit」后就返回，
    // 而协程栈被销毁 / 切换复用 —— 炸弹
}
```

正确语义：await 必须挂到**对应 CQE 到达**之后。

#### 2.2 取消：发请求，不是按开关

取消常用 `IORING_OP_ASYNC_CANCEL`（liburing：`io_uring_prep_cancel`）。要点：

1. 取消本身是另一条 SQE；
2. 原操作可能已经完成、正在飞、或稍后以 `-ECANCELED` 完成；
3. **你仍要消耗相关的 CQE**（原操作的、取消操作的，具体组合依内核与标志）；
4. 在看到「该请求真正结束」的完成之前，缓冲区仍不可回收。

教学用取消片段（示意）：

```cpp
// 先提交一个可能较慢的读（大文件 / 故意 O_DIRECT 大块等）
io_uring_sqe* sqe = io_uring_get_sqe(&ring);
io_uring_prep_read(sqe, fd, buf, buf_len, 0);
io_uring_sqe_set_data(sqe, req);
io_uring_submit(&ring);

// 立刻取消：按 user_data 匹配
io_uring_sqe* csqe = io_uring_get_sqe(&ring);
io_uring_prep_cancel(csqe, req, 0);  // 取消 user_data == req 的请求
io_uring_sqe_set_data(csqe, (void*)0xCACE); // 标记「这是取消请求本身」
io_uring_submit(&ring);

// 循环收 CQE，直到原 req 与取消请求都有了着落
for (int got = 0; got < 2; ) {
    io_uring_cqe* cqe;
    if (io_uring_wait_cqe(&ring, &cqe) < 0) break;
    void* id = io_uring_cqe_get_data(cqe);
    printf("done id=%p res=%d\n", id, cqe->res);
    // 若 id==req 且 res==-ECANCELED：原读被取消
    // 若 id==req 且 res>=0：取消前已读完（竞态）
    // 取消请求自己的 res 也要看（成功匹配到 vs 没匹配到）
    io_uring_cqe_seen(&ring, cqe);
    ++got;
}
// 此处才允许回收 buf / delete req
```

验收实验建议固定记录：

| 场景 | 期望观察 |
|------|----------|
| 取消赶在完成前 | 原请求 CQE.`res == -ECANCELED`（常见） |
| 取消赶不上 | 原请求已成功/`res>=0`，取消请求报告未匹配或相应错误 |
| 提交失败 | 根本没有原请求 CQE；不要假装「已取消」 |

#### 2.3 注册缓冲区 / 固定文件（扩展认知）

进阶路径里还有 `io_uring_register_buffers` / `register_files`：减少内核反复 pin 页、反复取 fd 的开销。它们进一步收紧寿命与生命周期管理（注册期间缓冲区更要稳定）。本课实践以普通 `prep_read` 为主，但性能比较时若启用了注册路径，必须写进记录表。

### 3. 就绪与完成语义

#### 3.1 同一业务，两种拆解

读一个配置片段 `config.bin`：

**Reactor 路径**

```text
epoll 监听 fd 可读
  → 就绪
  → read(fd, buf, n)   // 可能短读、可能 EAGAIN
  → 拼缓冲 / 再等就绪
```

**Proactor / io_uring 路径**

```text
prep_read + submit
  → （可选）去做别的事 / 挂起协程
  → wait CQE
  → 看 res：短读则再提交后续；错误则走错误路径
```

对**套接字**，Linux 上长期以 epoll 为主；io_uring 对网络操作的支持随内核演进增加，但仍要「按操作核验」。节点 pitfalls：**异步 API 不保证每个操作在所有场景都无阻塞执行**——例如某些文件系统、某些旗标组合、同步回调路径、worker 饥饿等，都可能让「异步」在观测上变成「这次 submit/enter 卡住了」。

#### 3.2 为什么还要保留 Reactor

1. **生态与心智**：大量中间件、开源框架、老代码是 epoll 就绪模型。
2. **语义匹配**：协议解析常要「有多少读多少、边读边状态机」；就绪模型很自然。
3. **能力矩阵**：不是每个操作在你的内核版本上都有稳定的 uring 操作码与取消语义。
4. **安全与运维**：容器默认 seccomp、旧内核、云厂商定制内核可能限制 io_uring。

#### 3.3 公众号后台怎么选（经验法则，非教条）

| 场景 | 更常见选择 | 原因 |
|------|------------|------|
| 接入层连接、HTTP/自定义 RPC 套接字 | Reactor（epoll）+ 协程/线程池 | 生态成熟；协议状态机友好 |
| 本地模板/素材小文件批量读 | 可评估 io_uring | 减少 syscall；完成语义直接 |
| 审计日志 / 指标落盘批量写 | 可评估 io_uring 或现有异步日志库 | 看库是否已封装；避免重复造轮 |
| 跨内核版本混部 | 先 epoll，能力探测后再开 uring | 符合 tradeoffs：内核支持纳入部署条件 |

#### 3.4 一张对比总表

| 维度 | 就绪通知 | 完成通知 |
|------|----------|----------|
| 核心问题 | 何时可以安全尝试 IO | 某次 IO 是否已结束及结果 |
| 缓冲区 | 就绪后才填；寿命由你的 read 循环管理 | 提交前提供；完成前不可回收 |
| 取消 | 改 epoll 兴趣 / 关闭 fd（仍有竞态） | 取消 SQE + 等 CQE |
| 批处理 | 一次 wait 多个 fd，但每人仍要 syscall | SQ/CQ 天然批量 |
| 风险口吻 | 误把就绪当「完整消息」 | 误把提交当「可释放缓冲」 |

---

## 常见误区

节点 pitfalls：

> 异步API不保证每个操作在所有场景都无阻塞执行。

展开成可排查的清单：

### 误区 1：`submit` 返回成功 = 数据已进 buf

否。只表示请求进入了提交路径（或已在线完成并入 CQ，取决于时机）。缓冲区内容以 CQE 到达且 `res` 解释之后为准。

### 误区 2：取消后立刻 `free(buf)`

否。取消是异步的；原操作可能已完成；即使取消成功，也要通过 CQE 确认。**缓冲区何时可回收**：对该请求而言，相关完成已处理完毕（含取消结果）之后。

### 误区 3：io_uring 一定比 epoll 快

不一定。小并发、已缓存的命中路径、错误的队列深度、频繁 enter、错误的数据路径（每次 O_DIRECT 随机读）都可能更慢。acceptance 要求性能比较时记录：**内核版本、队列深度、数据路径**。

### 误区 4：所有 fd / 所有操作都能一视同仁

支持范围依内核和操作而异。网络、超时、取消、固定缓冲、多 shot 等能力要按版本核验。sourceStudy 要求：阅读 liburing 示例与当前内核文档，**对每种操作核验取消与完成约定**。

### 误区 5：CQE.`res` 当无符号长度用

`res` 为负是错误码。写成 `size_t n = cqe->res` 会变成巨大长度，是安全事故。

### 误区 6：忽略短读

文件读、套接字读都可能短。Proactor 不替你把「逻辑消息」拼完，只告诉你这次操作的结果。

---

## 设计权衡

节点 tradeoffs：

> 减少系统调用可能有收益，但安全限制与内核支持需纳入部署条件。

### 收益侧

- 批量提交 / 批量完成，摊销用户态↔内核态往返。
- 完成语义减少「就绪后再 read」的竞态与额外 syscall。
- 与轮询模式（`IORING_SETUP_IOPOLL` 等，视场景）结合时，存储路径可能进一步降延迟。

### 成本与约束侧

- **内核版本矩阵**：混部机器必须能力探测；老内核回退 epoll。
- **安全策略**：seccomp、容器 runtime、部分云镜像默认限制 io_uring。
- **复杂度**：请求对象寿命、取消竞态、错误码、资源注册，都比「epoll + read」更重。
- **可观测性**：要有「未完成 SQE 数、CQ 溢出、取消率、worker 延迟」等指标，否则线上难查。

### 工程建议

1. 用**能力探测**（试 init、试 prep 某操作、看文档/内核配置）决定是否启用。
2. 先覆盖「本地文件受控读写」这类边界清晰的路径，再扩展到网络。
3. 任何性能结论旁边写三列：内核、队列深度、数据路径（含是否 register buffers/files、buffered vs direct）。

---

## 动手实践

节点 practice：

> 用liburing实现受控文件读取，验证提交失败、短读和取消完成。

sourceStudy：

> 阅读liburing示例与当前内核io_uring文档，对每种操作核验取消与完成约定。

### 步骤 0：环境

```bash
uname -r
sudo apt-get install -y liburing-dev   # 或等价包名
pkg-config --exists liburing && echo ok
```

编译示例：

```bash
c++ -O2 -std=c++17 controlled_read.cpp -luring -o controlled_read
```

### 步骤 1：准备受控输入文件

```bash
# 小文件：便于制造短读（请求长度 > 文件大小）
printf 'hello-proactor' > /tmp/atlas62_short.txt
# 较大文件：便于观察取消竞态
dd if=/dev/urandom of=/tmp/atlas62_big.bin bs=1M count=16 status=none
```

### 步骤 2：实现受控读取（最小功能清单）

程序建议支持三种子命令或编译期开关：

1. **正常读 / 短读**：对 `/tmp/atlas62_short.txt` 请求 64 字节，断言 `0 < res < 64`（短读）或打印明确说明。
2. **提交失败**：对非法 fd（如 `-1`）或故意不 `get_sqe` 处理满队列，打印失败发生在哪一层。
3. **取消完成**：对大文件提交大块读，随即 `prep_cancel`，循环收 CQE，打印原请求与取消请求的 `res`，并**在全部完成后才 free 缓冲**。

伪代码骨架：

```cpp
// controlled_read.cpp 核心逻辑（教学简化）
bool do_short_read(io_uring& ring) {
    int fd = open("/tmp/atlas62_short.txt", O_RDONLY);
    std::vector<char> buf(64, 0);
    auto* sqe = io_uring_get_sqe(&ring);
    if (!sqe) return false;
    io_uring_prep_read(sqe, fd, buf.data(), buf.size(), 0);
    io_uring_sqe_set_data(sqe, buf.data());
    if (io_uring_submit(&ring) < 0) { /* 提交失败 */ close(fd); return false; }
    io_uring_cqe* cqe{};
    io_uring_wait_cqe(&ring, &cqe);
    int res = cqe->res;
    fprintf(stderr, "short-read path: res=%d (expect < %zu if file smaller)\n",
            res, buf.size());
    io_uring_cqe_seen(&ring, cqe);
    close(fd);
    // CQE 已处理 → 此刻可回收 buf（离开作用域即可）
    return res >= 0;
}
```

### 步骤 3：对照官方示例

打开节点指定资源：

[https://github.com/axboe/liburing/tree/master/examples](https://github.com/axboe/liburing/tree/master/examples)

重点阅读与本课 points 对齐的部分：

- SQ 提交、CQ 完成、`user_data` 关联；
- 读操作完成结果解释；
- 任何涉及 cancel 的示例或测试（若 examples 中较少，辅以 liburing 测试与 `man io_uring_prep_cancel`）。

写一页笔记，只回答：

1. 一次 read 的 SQE 关键字段有哪些？
2. 短读时应用下一步该做什么？
3. 取消后最少要等几份完成？缓冲区最早在哪一行代码后回收？

### 步骤 4：性能比较（写进实验记录）

不要只报「快了 30%」。固定表格：

```text
| 项目         | 记录值                |
|--------------|-----------------------|
| 内核         | 6.x.x-...             |
| 队列深度     | init entries = 32/256 |
| 数据路径     | buffered / O_DIRECT   |
| 文件大小     | 16MB                  |
| 并发未完成数 | 8 / 32 / 128          |
| 对比基线     | pread 循环 / epoll+read |
| 指标         | 吞吐 MB/s、p99 延迟   |
```

同一机器改一个变量再测；结论里写清「在什么内核、什么深度、什么路径下」。

### 步骤 5：验收清单

- [ ] 能解释 SQ/CQ/SQE/CQE 与 `res`/`user_data`
- [ ] 短读有可复现输出
- [ ] 提交失败路径有可复现输出（且不与完成失败混淆）
- [ ] 取消后仍等待完成；**能说明缓冲区何时可回收**
- [ ] 性能比较表含**内核、队列深度、数据路径**
- [ ] sourceStudy 笔记覆盖 liburing examples + 当前内核文档中的取消/完成约定

---

## 自测题

**题 1.** 用一句话区分「就绪通知」与「完成通知」。为什么说 epoll 下「fd 可读」并不等于「一次 `read` 得到完整业务消息」？io_uring 下「收到 CQE」又保证了什么、不保证什么？

**题 2.** 协程里栈上分配了 `char buf[4096]`，发起 io_uring 读后在完成前 `co_await` 到别的逻辑，期间协程栈被回收（或错误地在 submit 后就返回）。会怎样？正确的缓冲区寿命规则是什么？

**题 3.** 你对一次读提交了 `ASYNC_CANCEL`，紧接着看到取消请求的 CQE 成功，能否立刻 `free(buf)`？若原读的 CQE 随后才到且 `res>0`，说明什么竞态？性能对比实验最少要记录哪三要素？

### 自测题参考答案

**题 1.** 就绪通知告诉你「可以尝试 IO 了」；完成通知告诉你「某次已提交的 IO 结束了及结果」。epoll 可读只是边缘/水平触发下的可尝试信号，TCP 粘包、短读、信号打断都会让单次 `read` 不等于完整消息。io_uring 的 CQE 保证「这一次操作有了最终结果（字节数或 -errno）」；不保证业务消息完整，也不保证从未短读，更不保证所有操作在所有内核上都是无阻塞执行。

**题 2.** 内核仍可能向已失效地址写入 → 内存破坏 / 崩溃。规则：SQE 提交后到对应 CQE 处理完毕前，缓冲区必须有效；await 必须对齐到完成，不能只对齐到 submit。

**题 3.** 不能仅凭取消请求的 CQE 就 `free`；还要确认原请求的完成已处理（或文档明确的等价约定）。原读 `res>0` 说明取消与完成竞态，数据可能已写入 buf。性能实验最少记录：**内核、队列深度、数据路径**。

---

## 推荐资源

1. [Proactor、完成通知与io_uring：参考文档 / 原始资料](https://github.com/axboe/liburing/tree/master/examples)  
   - 来源：github.com｜类型：源码｜难度：深入｜免费  
   - 重点阅读 **SQ/CQ 与完成结果** 与 **缓冲区寿命和取消**，用于核对本节点实验的语义和边界。

建议同时查阅本机 `man io_uring`、`man io_uring_setup`、`man io_uring_enter` 以及当前内核文档中关于取消与完成的章节，按操作码逐项核验。

---

## 本课小结

- **完成通知（Proactor）**把「提交操作」与「处理结果」分开；Linux 上以 **io_uring** 的 SQ/CQ 为代表。
- **SQE** 描述请求，**CQE** 带回 `user_data` + `res`；短读与负 errno 都是正常完成形态。
- **缓冲区寿命**：完成处理前不可回收；**取消**仍要等完成，不能当同步开关。
- **就绪 vs 完成**是不同语义；异步 API **不保证**一切场景无阻塞。
- **权衡**：少 syscall 可能有收益，但**安全限制与内核支持**必须进入部署与回退条件。
- 实践：liburing 受控文件读，验证提交失败、短读、取消完成；性能表记内核、队列深度、数据路径。

---

## 配套练习建议

1. **读 examples**：从 [liburing examples](https://github.com/axboe/liburing/tree/master/examples) 挑一个最小 read 示例，对照本课代码标出 SQE/CQE 字段。
2. **跑三类实验**：短读、提交失败、取消完成；把输出贴进笔记。
3. **寿命审查**：给团队代码写一条规则——「所有 uring 请求对象与 buf 的 free 点必须在对应 CQE 之后」，并用 sanitizer 跑取消路径。
4. **能力探测**：在目标机上写 20 行探测：`queue_init` 是否成功、`prep_cancel` 是否可用；失败则回退 epoll 路径。

下一课继续在 IO 模型脉络上推进；先确保你能独立讲清：何时用就绪通知、何时用完成通知，以及 io_uring 下缓冲区与取消的真实约定。
