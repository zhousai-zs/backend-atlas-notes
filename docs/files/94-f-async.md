# 第 94/208 课：异步RPC与生命周期协调

## 元信息

| 项目 | 内容 |
| --- | --- |
| 节点 ID | `f-async` |
| 英文名 | Async RPC |
| 领域 / 阶段 | F / 第 4 阶段 |
| 子类 | 失败与请求语义 |
| 难度 | 深入 |
| 前置节点 | `f-grpc`、`e-cancel`、`b-smart` |
| 标签 | `Async RPC`、`F`、`C++` |
| 参考文档 | [异步RPC与生命周期协调：参考文档 / 原始资料](https://grpc.io/docs/languages/cpp/callback/)（grpc.io） |
| 摘要 | 异步调用允许重叠等待，但必须协调完成、取消、超时和回调可能到达的顺序。 |

> **边界说明**：本课出现的后台系统、服务划分、延迟数字与事故时间线均为教学假设，用于讲解通用后端工程概念，
> 不代表任何企业的内部实现或真实架构。涉及 gRPC C++ callback / 完成队列 API 的具体类名、方法签名与版本差异时，
> 请以你所阅读的文档版本为准。课内 Python 实验只模拟语义，不等于 C++ API。

## 导读

### 一个直觉例子：餐厅点菜

一桌客人点了主菜、饮料、甜品和一份小吃。

- **排队等 vs 同时下单**：笨服务员先去厨房等主菜做好，再去饮品台等饮料……总等待是四段之和。聪明的服务员把四张单子同时递出去，然后去忙别的——这就是**异步调用允许重叠等待**。
- **小票编号 = tag，出餐窗口 = 完成队列**：每张小票上写着"3 号桌·饮料"。哪道菜好了，后厨把它连同小票放到出餐窗口；服务员只盯着窗口，看小票就知道该送到哪桌、这桌还差几道——这就是**完成队列与每请求状态机**。
- **客人等不及走了 = 超时 / 取消**：客人说"20 分钟上不齐我就走"。到点他走了，但厨房里那道菜可能还在锅里，过两分钟照样被端到出餐窗口——这就是**晚到回调**。取消只是"请厨房别做了"，厨房可能已经做完了，所以取消是合作式的。
- **桌子已经收拾给下一桌 = 已销毁的上下文**：晚到的菜不能端到桌上去，因为那张桌子已经换了客人。如果服务员只记得"端到 3 号桌"而不确认 3 号桌还是不是原来那桌，就会把菜放到陌生人面前——这就是**悬垂回调 / use-after-free**。
- **一张订单只能结账一次 = 一次性完成**：客人走时前台按"超时未上齐"打折结账；同一时刻最后一道菜上来，另一个服务员按"全部上齐"又结了一次——这就是**双重完成**。两人都看了一眼"这单结了吗？没结"，然后各自去结，问题出在"看一眼"和"结账"之间的那一小段空隙。

### 接上前置课

- **f-grpc**（gRPC C++ 同步与异步接口）：我们见过同步、完成队列异步、callback 三种 API 形态，知道"选择时需理解回调、完成队列和对象生命周期"。本课就是把那句话展开。
- **e-cancel**（结构化并发、取消与停机）：任务的拥有者管理其启动、取消、等待和资源释放；取消通常是合作式请求而非强制终止。本课里"拥有者"就是那个每请求状态机，它要负责把取消传播给所有在途下游，并等它们真正结束。
- **b-smart**（智能指针与共享所有权）：`unique_ptr` 唯一拥有、`shared_ptr` 共享拥有、`weak_ptr` 不延长寿命地观察。本课用它们回答"晚到的回调手里拿的是什么"。

### 教学时间线（教学假设）

某内容后台的"图文详情聚合服务"每次请求要拿四样东西：文章正文（article）、阅读数（stats）、评论（comment）、关注关系（relation）。

| 时间 | 事件 |
| --- | --- |
| 第 1 周 | 原实现串行调用四个下游，p99 约 180ms，产品要求压到 100ms 以内 |
| 第 2 周 | 改为并行 fan-out + 整体 150ms 超时，p99 降到约 70ms，上线 |
| 第 3 周 | 每天出现几次 core dump，栈都在评论服务的回调里；同时日志里偶发 "duplicate reply for request" |
| 排查 | 超时分支先回包并 `delete` 了请求对象；200ms 后评论服务的回调才到，按捕获的裸指针写入已释放内存。另一类问题：超时定时器与最后一个下游回调几乎同时到达，两边都检查了 `if (!done_)`，都回了包 |
| 修复 | 一次性完成改为原子抢占；回调持 `weak_ptr`（或由引用计数 + 统一释放点管理寿命）；超时/取消后主动取消在途下游；加"在途操作数""存活上下文数"监控 |

| 维度 | 错误做法 | 正确做法 |
| --- | --- | --- |
| 防重复完成 | 普通 `bool done` 先查后设 | 锁内检查并设置 / `compare_exchange` / `call_once` |
| 回调捕获 | 裸指针或引用 `[this]` | `weak_ptr` 先 `lock()`，或 `shared_ptr` 延长到最后一个回调 |
| 释放时机 | 回包即 `delete` | 所有在途操作结束（或框架的最后回调，如 `OnDone`）后才释放 |
| 超时/取消 | 只给上游回包，下游继续干 | 回包 + 取消所有在途下游，并等待它们结束 |
| 部分失败 | 任一下游失败就整体失败，或静默吞掉 | 区分关键/非关键下游，明确降级语义 |

### 本课三条主线

1. **完成队列与状态机**：异步把"执行到哪一步"从栈帧挪到显式状态机里，事件通过 tag 找回状态机。
2. **一次性完成和取消竞态**：完成、超时、取消三路可能并发，必须有唯一的原子裁决点。
3. **响应对象及控制器寿命**："回包"和"释放"是两个时刻，晚到回调必须不能碰到已销毁的对象。

## 学习目标

学完本课，你应当能够：

1. **能解释完成队列与状态机**。
2. **能独立完成：实现异步fan-out聚合，注入超时与部分失败，确保每请求只完成一次。**
3. 画出一次 fan-out 请求在"全部成功 / 部分失败 / 超时先到 / 客户端取消 / 超时与最后响应同时到达"五种时序下，谁回包、谁取消下游、谁释放对象。
4. 说明为什么"普通布尔标记"不能防止双重完成，并给出锁、原子 CAS、`call_once` 三种修法。
5. 用 `shared_ptr` / `weak_ptr` / 引用计数 + 统一释放点三种思路之一，保证回调晚到不会访问已销毁上下文。
6. 设计可观测指标（在途操作数、存活上下文数、重复完成计数、晚到丢弃计数），用测试证明"取消后所有引用最终释放"。
7. 估算 fan-out 对下游瞬时并发的放大，并知道何时该限制并发度或分阶段调用。
8. 读 gRPC C++ callback 文档时，能找出 `OnDone`、`OnCancel`、对象销毁时序相关的约束并核对版本。

## 为什么后端要懂

> **why 原文**：避免高并发调用中的双重完成与悬垂回调。

这两个问题有一个共同点：**低并发、正常路径下几乎不出现，高并发、异常路径下才出现**。所以它们很少在功能测试里暴露，而是在压测、大促、下游抖动时变成线上事故。

- **双重完成**：一个请求被回了两次包。轻则客户端收到两条响应、日志报警；重则副作用执行两次——例如"完成时扣一次配额 / 写一次计数 / 发一条通知"，被执行了两遍。在 RPC 框架里，对同一个调用 `Finish` 两次本身也可能是未定义行为或直接断言失败（以所用框架文档为准）。
- **悬垂回调**：回调在它依赖的对象被释放之后才执行。C++ 里这就是 use-after-free：可能立刻 crash，也可能悄悄写坏另一块刚被复用的内存，几小时后在毫不相关的地方崩溃。core dump 的栈常常指向"无辜"的代码，极难复现。

### 逐句读 summary

**"异步调用允许重叠等待"**——算一笔账（教学假设的延迟）：

| 下游 | 延迟 |
| --- | --- |
| article | 40ms |
| stats | 50ms |
| comment | 60ms |
| relation | 45ms |
| 串行总耗时 | 40+50+60+45 = **195ms** |
| 并行总耗时 | max(40,50,60,45) ≈ **60ms**（再加少量调度开销） |

等待时间重叠了，关键路径从"求和"变成"取最大"。本课实验里，40 个突发请求下串行 p50≈250ms、并行 p50≈124ms（含排队）。

**"但必须协调完成、取消、超时和回调可能到达的顺序"**——一旦并行，一个请求就同时挂着多件"未来会发生的事"，它们的到达顺序不受你控制：

| 场景 | 事件到达顺序 | 谁负责回包 | 谁负责取消下游 | 何时可以释放请求对象 |
| --- | --- | --- | --- | --- |
| 全部成功 | 4 个响应依次到达 → 定时器未触发 | 最后一个响应的回调 | 无需取消，撤销定时器 | 最后一个回调返回后 |
| 非关键下游失败 | relation 报错，其余成功 | 最后一个回调（降级回 PARTIAL） | 无 | 最后一个回调返回后 |
| 超时先到 | 3 个响应 → 定时器触发 → comment 晚到 | 定时器回调 | 定时器回调取消 comment | comment 的回调（或其取消完成通知）结束后 |
| 客户端取消 | 2 个响应 → 取消到达 → 其余晚到/被中止 | 取消处理路径（回 CANCELLED） | 取消处理路径 | 所有在途操作都结束后 |
| 超时与最后响应同时到达 | 定时器与第 4 个响应**几乎同时** | **只能有一个**：谁先原子抢到完成权 | 抢到者负责 | 两条路径都退出后 |

最后一行是本课的核心难点：两条线程同时到达，"先检查再设置"会让两边都认为自己是赢家。下一章从完成队列和状态机讲起，逐步把这张表变成代码。

## 核心知识点

异步 RPC 的难点通常不在「发起调用」，而在「收尾」：同一个请求可能先后或同时被下游成功、下游失败、超时、上游取消命中，而回调还可能在任意时刻晚到。下面用通用的「图文详情页聚合」作为教学假设——一个详情页并行调用文章正文、阅读数、评论、用户关系四个下游，再拼装结果返回。真实系统各有差异，这里只讨论通用的协调语义。

### 1. 完成队列与状态机

#### 1.1 同步、线程池与完成队列

| 方式 | 谁在等 | 并发模型 | 优点 | 代价与风险 |
| --- | --- | --- | --- | --- |
| 同步阻塞调用 | 调用线程 | 一请求一线程 | 代码直白、易调试 | fan-out 时线程数随下游数放大，尾延迟被最慢下游拖住 |
| 线程池 + 同步调用 | 池内工作线程 | 有界并发 | 复用线程、实现简单 | 下游慢时线程被占满，队头阻塞，取消难传播 |
| 完成队列 / 事件循环 | 少量驱动线程 | 海量在途、事件驱动 | 一个线程可推进很多调用，重叠等待成本低 | 状态机与生命周期复杂，回调顺序需自行保证 |

同步写法下，四路 fan-out 要么串行等待、总耗时相加，要么为每路开线程；线程池缓解了创建成本，但慢下游会把线程占满。完成队列把「等待」从线程里拿出来：发起操作时不阻塞，操作完成时把事件投进队列，由少量驱动线程统一取出并推进各自的调用。代价是原来由栈帧隐式保存的「执行到哪一步」，现在必须显式写成状态机。

#### 1.2 完成队列的直觉：tag 与 (tag, ok)

直觉只有两条：**发起时给操作贴一个标识 tag，完成时从队列里取出 (tag, ok)**。tag 用来找回「这是哪个请求的哪一路」，ok 表示这次操作是成功完成还是失败完成。下面的 Python 用 `queue.Queue` + 线程模拟完成队列：几个「下游」线程各自延迟后 `put`，主循环 `get` 后按 tag 找到状态机推进。它是教学模型，不是 gRPC API。

```python
import queue
import threading
import time

cq = queue.Queue()   # 模拟完成队列

def downstream(event_tag, delay, ok=True):
    time.sleep(delay)
    cq.put((event_tag, ok, f"body-of-{event_tag}"))

class PageCall:
    """一个详情页请求的状态机：记录还差几路，收敛到 DONE。"""

    def __init__(self, total):
        self.pending = total
        self.state = "PENDING"
        self.result = {}

    def on_event(self, event_tag, ok, value):
        if self.state == "DONE":          # DONE 之后任何事件都丢弃
            return
        if not ok:                        # 任一路失败，本请求直接收敛
            self.state = "DONE"
            return
        self.result[event_tag] = value
        self.pending -= 1
        if self.pending == 0:
            self.state = "DONE"

call = PageCall(total=3)
deps = [("article", 0.05), ("read_count", 0.08), ("comments", 0.12)]
for event_tag, delay in deps:
    threading.Thread(target=downstream, args=(event_tag, delay), daemon=True).start()

for _ in range(len(deps)):
    event_tag, ok, value = cq.get()       # 阻塞取出一个完成事件
    call.on_event(event_tag, ok, value)

print(call.state, call.result)
```

三点值得记住：主循环不关心下游的调用顺序，只关心事件；`on_event` 是唯一的推进入口，所有状态变化都发生在它里面；`DONE` 是吸收态，之后的事件一律丢弃——这正是后文「一次性完成」的雏形。

#### 1.3 每请求状态机：PENDING → DONE

| 当前状态 | 事件 | 动作 | 下一状态 |
| --- | --- | --- | --- |
| PENDING | 某一路成功返回且仍有未完成 | 记录结果，`pending -= 1` | PENDING |
| PENDING | 最后一路成功返回 | 记录结果，`pending == 0`，回包 | DONE |
| PENDING | 某一路失败 | 标记失败，取消其余在途下游 | DONE |
| PENDING | 整体超时到点 | 标记超时，取消其余在途下游 | DONE |
| PENDING | 上游取消 | 传播取消到所有在途下游 | DONE |
| DONE | 任何事件 | 丢弃，不触碰已释放资源 | DONE |

关键约束：**DONE 只能被进入一次**。上表每次进入 DONE 的动作里都包含「回包给上游」，如果两条路径同时认为自己该回包，上游就会收到两个响应，或者后一条路径访问了已释放的上下文。下一节专门处理这个竞态。

#### 1.4 gRPC C++ 完成队列模式中的 CallData

gRPC C++ 的完成队列（CompletionQueue）模式下，每个异步操作在发起时绑定一个 tag，驱动线程调用 `Next()` 取出完成事件，再根据 tag 找回对应的调用对象。常见写法是「每个调用一个状态机对象」，用小状态枚举推进。下面的伪代码只表达形状，具体类名与方法签名以所读文档版本为准。

```cpp
// 伪代码：具体类名、签名与语义以所读 gRPC 文档版本为准
enum class State { CREATE, PROCESS, FINISH };

class CallData {
public:
    void Proceed() {
        if (state_ == CREATE) {
            state_ = PROCESS;
            service_->RequestRpc(&ctx_, &req_, &responder_, cq_, cq_, this); // this 作为 tag
        } else if (state_ == PROCESS) {
            state_ = FINISH;
            responder_.Finish(reply_, Status::OK, this);
        } else {
            delete this;   // 最后一个完成事件已取出，才释放
        }
    }
    State state_{State::CREATE};
};

void* tag = nullptr;
bool ok = false;
while (cq.Next(&tag, &ok)) {
    static_cast<CallData*>(tag)->Proceed();   // tag 还原对象，再推进状态机
}
```

要点：tag 通常就是对象自身的地址；`Next()` 返回的 `ok` 要结合状态解释（连接断开等情况可能以 false 完成）；**最后一个完成事件处理完之后，才能 `delete this`**，否则框架可能还会用这个 tag 回调。异步流的读写、取消通知会带来更多状态，形状相同，状态更多。

### 2. 一次性完成和取消竞态

#### 2.1 为什么「if not done: done = True; reply()」不安全

这行代码由「检查」和「设置」两步组成，中间存在窗口。两个线程可以同时看到 `done == False`，于是都通过检查、都把 `done` 置为 True、都调用 `reply()`。超时线程和正常完成线程并发时最容易踩中：超时刚检查完还没置位，下游的完成回调就进来了。

有人会拿 GIL 来安慰自己，但 `if not done` 与 `done = True` 是两条字节码，线程仍可能在两者之间被切换；GIL 保证的是同一时刻只有一条字节码在跑，不等于「检查并设置」是原子的。

#### 2.2 用 Python 复现并修复

下面的演示让两个线程分别扮演「下游完成」和「超时」，用 `threading.Barrier` 保证两者都通过检查后再往下走，从而稳定复现重复回包。真实竞态出现频率低，但后果一样。

```python
import threading
import time

class BadCall:
    def __init__(self):
        self.done = False
        self.replies = 0
        self.barrier = threading.Barrier(2)   # 只为稳定复现竞态

    def reply(self, who):
        if not self.done:             # 检查
            self.barrier.wait()       # 两线程都通过检查后再继续
            time.sleep(0)
            self.done = True          # 设置
            self.replies += 1
            print(f"{who} 回包, replies={self.replies}")

call = BadCall()
threads = [
    threading.Thread(target=call.reply, args=("downstream",)),
    threading.Thread(target=call.reply, args=("timeout",)),
]
for t in threads: t.start()
for t in threads: t.join()
print("重复完成次数:", call.replies - 1)   # 期望 0，这里会得到 1
```

把检查与设置收进同一把锁，或者用一个原子的「抢占」操作，就能保证只有一个赢家：

```python
class GoodCall:
    def __init__(self):
        self._lock = threading.Lock()
        self._done = False
        self.replies = 0

    def try_claim(self):
        with self._lock:             # 检查与设置在同一临界区
            if self._done:
                return False
            self._done = True
            return True

    def reply(self, who):
        if self.try_claim():
            self.replies += 1
            print(f"{who} 回包")

good = GoodCall()
ts = [threading.Thread(target=good.reply, args=(w,)) for w in ("downstream", "timeout")]
for t in ts: t.start()
for t in ts: t.join()
print("重复完成次数:", good.replies - 1)   # 恒为 0
```

`try_claim()` 的语义就是「抢占完成权」：返回 True 的线程负责回包，返回 False 的线程必须安静退出。C++ 中等价物是 `std::atomic<bool>::compare_exchange_strong` 或 `std::call_once`，原理相同——把「检查 + 设置」做成一个不可分割的操作。

在 asyncio 里，事件循环是单线程的，没有真正的并行，`if not done` 到 `done = True` 之间只要没有 `await`，就不会被其他任务插入，所以这类竞态天然少很多。但一旦在检查与设置之间 `await`（例如 `await 下游.取消()`），窗口又会打开；跨线程回调（`loop.call_soon_threadsafe`）也不能假设原子。结论不是「asyncio 不用管」，而是「不让 `await` 或线程边界穿过临界区」。

#### 2.3 超时、完成、取消的三方竞争

```
时间轴（→ 表示时间前进）：
下游完成   ──●──────────────────────────────►  完成事件入队
超时定时   ──────────●──────────────────────►  超时到点，想抢占完成权
上游取消   ────────────────●─────────────────►  TryCancel 发出
框架取出   ──────────────────────●───────────►  完成事件被取出，on_event 执行

谁先 try_claim 成功，谁负责回包；其余路径只做清理。
```

三种事件都可能先后到达，甚至并发。「超时一定晚于完成」「取消后就不会再有完成事件」这两个假设都不成立。唯一的裁决点是原子的完成权抢占；时间轴只帮助理解，不能当作顺序保证。

#### 2.4 取消是合作式的

发出取消（客户端 `ClientContext::TryCancel`，或服务端主动终止）只是「请求对方停止」的信号，不是「对方已经停止」的证明。下游可能已经算完、回包在途，或者根本不响应取消。所以取消路径也要 `try_claim`：抢到才回包（通常是 CANCELLED 状态），抢不到说明别处已完成，直接退出。

取消还必须传播到**所有在途下游**：详情页聚合里四路调用，只取消其中一路，另外三路仍占着连接和线程。接 e-cancel 一课的做法，用一个 per-request 的取消集合或 context 统一广播，并在每个下游回调里先检查是否已取消，再决定是否处理结果。

### 3. 响应对象及控制器寿命

#### 3.1 谁拥有请求上下文、响应对象和控制器

| 对象 | 谁创建 | 生命周期归属 | 常见错误 |
| --- | --- | --- | --- |
| 请求上下文（如 `ClientContext` / `ServerContext`） | 调用方或框架 | 必须活到该 RPC 的所有回调结束 | 栈上对象先析构，晚到回调读到野指针 |
| 响应对象 / 消息体 | 调用方持有 | 至少活到写出完成 | 回包未发完就复用缓冲区 |
| reactor / 调用状态机 | 调用方 `new` 出 | 由完成回调统一释放 | 超时路径先删，`OnDone` 再访问 |

这三类对象默认都**不是**线程安全的，谁先被释放由你的代码决定。异步容易出悬垂回调，是因为「发起调用」和「回调到达」之间隔了很久，栈帧早已消失，所有上下文只能靠堆对象和引用计数续命。

#### 3.2 悬垂回调长什么样

```cpp
// 反例（C++ 形状，仅示意）
void Start(Call* call) {
    call->start([call](Result r) { call->OnDone(r); });   // 裸指针进回调
}
// 超时路径 delete call; 之后完成回调仍会执行 -> 悬垂
```

把 `call` 换成 Python 里跨线程传递的裸对象引用，问题等价：对象已经被超时路径释放，晚到的完成回调再进去就是未定义行为。

#### 3.3 三种寿命方案

| 方案 | 做法 | 优点 | 代价 |
| --- | --- | --- | --- |
| `shared_ptr` 持有 | 每个在途回调捕获一份 `shared_ptr`，最后一个释放才销毁 | 不需要精确知道最后一个回调是谁 | 引用环需警惕，释放时机分散 |
| `weak_ptr` 探活 | 回调持 `weak_ptr`，`lock()` 失败即丢弃 | 绝不解引用已销毁对象 | 需要共享控制块，回调可能白跑 |
| 引用计数 + OnDone 统一释放 | 计数在途操作，`OnDone` 里减到 0 才释放 | 释放点集中、可观测 | 计数必须无竞态，漏减会泄漏 |

#### 3.4 weakref 与在途计数

Python 的 `weakref` 对应 `weak_ptr` 的「先探活再使用」：回调只持弱引用，对象被释放后 `ref()` 返回 `None`，晚到回调安全丢弃。

```python
import weakref

class PageContext:
    def __init__(self):
        self.result = {}

ctx = PageContext()
ref = weakref.ref(ctx)

def late_callback(ref=ref):
    obj = ref()                 # 尝试提升为强引用
    if obj is None:             # 对象已释放，安全丢弃
        print("晚到回调：上下文已释放，丢弃")
        return
    obj.result["article"] = "..."

late_callback()                 # 对象还在，正常处理
del ctx                         # 超时路径释放上下文
late_callback()                 # 晚到，ref() 返回 None，安全
```

反过来，「计数 + OnDone 统一释放」用计数器把「在途操作」显式化，取消后逐个 `release`，最终归零才释放上下文：

```python
import threading

class Inflight:
    def __init__(self):
        self._lock = threading.Lock()
        self._n = 0
    def acquire(self):
        with self._lock: self._n += 1
    def release(self):
        with self._lock: self._n -= 1
    def count(self):
        with self._lock: return self._n
```

把 `count()` 暴露为监控指标，就能在测试里断言「取消后最终归零」这个不变量，而不是靠肉眼看日志。

#### 3.5 OnDone 是最后一个回调，完成不等于释放

gRPC C++ callback（reactor）API 中，`OnDone` 是该 RPC 的最后一个回调，之后框架不再访问该 reactor，因此通常在 `OnDone` 中释放对象；`OnCancel` 可能与其他回调并发或在其之后到达，必须线程安全地处理；客户端可用 `ClientContext::TryCancel` 发起取消（具体行为以所读文档版本为准）。把这些约束翻译成一句话：**「回包给上游」（完成）和「所有在途操作结束、可以释放」（释放）是两个不同时刻**，中间可能还夹着取消通知和晚到回调。

### 小结

完成队列把等待从线程里搬进状态机，tag 负责把事件找回到调用，`DONE` 必须是吸收态；一次性完成靠原子抢占而不是布尔检查，超时、完成、取消三路谁抢到谁回包，取消还要广播到全部在途下游；寿命问题则用 `shared_ptr`、`weak_ptr` 或「计数 + `OnDone` 统一释放」把「完成」与「释放」解耦。三件事串起来，异步 fan-out 才能在重叠等待的同时，不出现双重完成与悬垂回调。

## 常见误区

> **pitfalls 原文**：超时处理与正常完成可能并发发生，不能只靠普通布尔标记。

为什么普通布尔不够，有三层原因：

1. **检查与设置不是一个操作**。`if (!done_) { done_ = true; Reply(); }` 是"读 → 比较 → 写"三步，定时器线程和下游回调线程可以同时读到 `false`，于是两边都进入分支。本课实验 C 中，超时与最后一个响应同时到达时，naive 模式 200 个请求里有 184 个被回了两次包。
2. **多线程回调是常态**。gRPC callback API 的回调可能在框架线程池的不同线程上执行（官方 bidi 示例的注释也提醒"reactor 很可能让我们跳线程"）；定时器又通常在另一条线程。不能假设"它们一定串行"。
3. **可见性**。C++ 里非原子 `bool` 的并发读写本身就是数据竞争（未定义行为），编译器和 CPU 都可能重排；即使没撞上同一时刻，一个线程写入也不保证另一个线程及时看到。正确做法是 `std::atomic<bool>::compare_exchange_strong`、`std::call_once`、或把检查和设置放进同一把锁。

下面是另外几个高频误区。

### 误区 1：回调捕获裸指针 / 引用，超时路径直接释放

| 项 | 内容 |
| --- | --- |
| 误区 | `client->async()->Get(&ctx_, &req_, &resp_, [this](Status s){ OnPart(s); });`，超时分支 `Reply(); delete this;` |
| 后果 | 晚到的回调通过悬垂的 `this` 访问 `ctx_`、`resp_`：crash、写坏别的对象、偶发难复现。实验 A 中 naive 模式 10 个超时请求的晚到回调全部访问了已销毁上下文 |
| 正确做法 | 对应 acceptance「**回调晚到不会访问已销毁上下文**」：回调持 `weak_ptr` 并先 `lock()`；或回调持 `shared_ptr`，对象寿命延长到最后一个回调；或用引用计数把释放集中到"最后一个在途操作结束"那一刻。另外注意：每个下游调用的 `ClientContext` 和响应对象本身也必须活到**该调用的**回调执行完 |

注意官方 callback 教程里的一元客户端示例把 `ClientContext` 放在栈上、lambda 按引用捕获局部变量——它之所以安全，是因为函数用条件变量**等到回调执行完**才返回。一旦你去掉等待改成"发出去就返回"，同样的写法立刻变成悬垂引用。

### 误区 2：以为"取消了"就等于"释放了"，或者 shared_ptr 环导致永不释放

| 项 | 内容 |
| --- | --- |
| 误区 A | 调了 `TryCancel()` 就认为下游调用结束、上下文可以销毁 |
| 误区 B | 请求对象持有回调（`std::function`），回调又捕获了请求对象的 `shared_ptr`，形成环 |
| 后果 | A：取消是合作式的，回调仍会到达（通常带 CANCELLED 状态），此时对象已不在；B：引用计数永远不归零，内存泄漏，"存活请求数"监控只升不降 |
| 正确做法 | 对应 acceptance「**取消后所有引用最终释放**」：取消后仍等待每个在途操作的完成通知；用计数器（在途操作数）和存活对象数做监控，测试里断言二者最终归零；打破环：回调里用 `weak_ptr`，或在完成时显式清空持有的回调/定时器 |

### 误区 3：超时只给上游回包，不取消下游

| 项 | 内容 |
| --- | --- |
| 误区 | 定时器触发后返回"超时"给调用方，就算处理完了 |
| 后果 | 下游继续占用连接、线程和 CPU 做一份没人要的工作；在下游已经慢的时候，这恰好是放大故障的方式。实验 D 中 naive 模式取消后 150ms 仍有 30 个在途下游调用，下游一个都没放弃 |
| 正确做法 | 抢到完成权的一方负责取消**所有**在途下游（客户端 `ClientContext::TryCancel()`，或像实验中关闭连接），并把剩余 deadline 传给下游让它自己尽早放弃（接 f-deadline） |

### 误区 4：部分失败的语义没想清楚

| 项 | 内容 |
| --- | --- |
| 误区 | 要么"任何一个下游失败就整体失败"，要么"失败了就当空值静默返回" |
| 后果 | 前者让一个非关键下游（如阅读数）拖垮整个详情页；后者让调用方无法区分"真的没有评论"和"评论服务挂了"，缓存了错误结果 |
| 正确做法 | 事先区分关键与非关键下游：关键下游（正文）失败 → ERROR；非关键下游失败 → PARTIAL，并在响应里标出缺了哪些字段。实验中 relation 失败的 6 个请求返回 PARTIAL，`errors` 字段里写明 `relation` |

### 误区 5：在回调里做阻塞操作

| 项 | 内容 |
| --- | --- |
| 误区 | 在 `OnReadDone` / `OnDone` / 完成队列处理函数里同步调用另一个 RPC、等锁很久、读磁盘 |
| 后果 | 回调线程或完成队列驱动线程被占住，其他成百上千个调用的事件都排队等它，整体延迟飙升甚至死锁 |
| 正确做法 | 官方 callback 教程明确提醒：回调方法应快速返回，不要在其中做阻塞工作（如等待事件）。需要后续工作时再发起一个异步操作，或投递到独立的工作线程池 |

### 误区 6：在 OnDone 之前释放 reactor

| 项 | 内容 |
| --- | --- |
| 误区 | 服务端在调用 `Finish()` 之后立刻 `delete this`，或在 `OnCancel()` 里释放对象 |
| 后果 | `Finish()` 只是发起"结束"操作，框架之后仍会回调该 reactor（`OnDone`，可能还有 `OnCancel`）；提前释放就是悬垂 |
| 正确做法 | 官方示例的统一模式是在 `OnDone()` 中做最终清理并 `delete this`——它是该 RPC 的最后一个回调。`OnCancel()` 只做通知/标记（且要线程安全）。客户端流式调用中如果写操作由外部定时器间接发起，示例使用 `AddHold()` / `RemoveHold()` 防止 RPC 过早结束（具体约束以所读版本为准） |

## 设计权衡

> **tradeoffs 原文**：并行调用降低关键路径时间，也放大下游瞬时并发。

### 先算账：放大有多大

教学假设：详情聚合服务入口 2000 QPS，每次 fan-out 4 路，每路平均 50ms。

| 指标 | 串行 | 全并行 |
| --- | --- | --- |
| 每个下游的平均 QPS | 2000 | 2000（不变） |
| 单请求关键路径 | ≈ 四路之和（≈195ms） | ≈ 最慢一路（≈60ms） |
| 一次突发 N 个入口请求时，**同一瞬间**压向下游的调用数 | N（每个请求同一时刻只在一个下游） | 4N（四个下游同时挨打） |
| 加上对冲/重试（如每路 1 次重试） | 每路最多 ×2 | 同一瞬间最多 8N |

平均流量按利特尔定律其实差不多，差别在**瞬时峰值**：并行把每个请求的工作挤到同一个时间窗口。实验 B 的真实数字：40 个突发请求，串行时聚合层同时在途的下游调用峰值 40，并行时 148（上限 160），p50 从约 250ms 降到约 124ms。

### 几种 fan-out 策略对比（一般经验，非绝对）

| 策略 | 延迟 | 下游瞬时压力 | 实现复杂度 | 适用场景 |
| --- | --- | --- | --- | --- |
| 串行 | 最高（求和） | 最低 | 最低 | 下游之间有数据依赖，或总延迟预算宽松 |
| 全并行 | 最低（取最大） | 最高（×fan-out） | 中（要处理一次性完成、晚到回调） | 下游独立、容量充足、延迟敏感 |
| 限并发度的并行（信号量/许可） | 介于两者，接近并行 | 有上限、可控 | 中高 | 入口流量会突发、下游容量有限（接 f-backpressure） |
| 分阶段（先拿关键数据，再并行拿其余） | 关键数据失败时最快失败 | 关键数据失败就不再打其余下游 | 中 | 正文不存在时其余数据都没意义的场景 |

### 部分失败：降级返回 vs 整体失败

- **降级返回**：可用性高，用户至少看到正文；代价是响应语义变复杂（哪些字段缺了），调用方和缓存层都必须理解 PARTIAL。
- **整体失败**：语义简单；代价是可用性被"最弱"的那个下游决定——4 个各 99.9% 可用的下游，全部成功的概率约 99.6%。
- 一般经验：关键依赖失败整体失败，非关键依赖失败降级，并对降级打点监控，避免"长期静默降级"没人发现。

### 超时预算如何在 fan-out 里分配

- 并行时每路可以拿到"整体剩余预算减去本地处理余量"，而不是"整体预算 / 4"。
- 分阶段时，第一阶段用掉的时间要从第二阶段的预算里扣掉；把**剩余** deadline 传给每个下游（接 f-deadline），下游超时后自行放弃，避免"上游已经超时、下游还在干"。
- 给每路单独设超时比只设整体超时更细，但更多定时器 = 更多可能与完成竞争的事件源，一次性完成机制要覆盖所有这些来源。

### 编程模型：回调 vs 完成队列 vs 协程

| 模型 | 优点 | 代价 |
| --- | --- | --- |
| 完成队列 + 手写状态机 | 线程模型完全自己掌控，性能可预测 | 状态机冗长、tag/对象寿命全靠自己管理，最容易出悬垂 |
| callback / reactor | 框架管理线程和事件分发，`OnDone` 给出明确的最后回调 | 回调分散在多个方法里，跨回调的共享状态要自己加锁 |
| 协程（如 C++20 协程封装、Python asyncio） | 代码像同步一样线性，取消可以结构化地传播 | 需要库/框架支持；`await` 点之间仍可能交错，寿命问题变成"协程帧何时销毁" |

无论选哪种模型，"完成只发生一次""晚到事件不碰已销毁对象""取消后所有引用最终释放"这三条不变量都要你自己守住，框架只是让它们更好写或更难写。

## 动手实践：异步 fan-out 聚合，注入超时与部分失败，确保每请求只完成一次

> 对应 practice 原文：**实现异步fan-out聚合，注入超时与部分失败，确保每请求只完成一次。**
> 代码在 `practice94/`（Python 3 标准库，无第三方依赖），打包为 `practice94.tar.gz`。场景是教学假设的「图文详情聚合」：一次请求并行调用 article（正文，关键）、stats（阅读数）、comment（评论）、relation（关注关系）四个下游。Python 线程 + 回调只用来**模拟** gRPC C++ 异步 API 的生命周期语义，不等于 C++ API 本身。

### 0. 文件一览

| 文件 | 作用 |
| --- | --- |
| `backend.py` | 下游模拟：一行请求 `<rid>`，按配置延迟后回 `OK ...` / `ERR ...`；支持 `--slow-every N --slow-ms M`（每 N 个请求变慢）、`--fail-every N`（每 N 个失败）；等待期间如果上游 `shutdown` 连接（取消），会感知到 EOF 并放弃工作，计入 `aborted` |
| `agg.py` | 聚合服务，`naive` / `safe` 两种模式；前端协议 `REQ <rid> <timeout_ms> [serial]`、`CANCEL <rid>`、`STATS`、`GC`、`RESET` |
| `client.py` | 在一条连接上并发发 N 个请求，统计每个 rid 收到几次响应（`dup_rids`）、p50/p99、状态分布，最后拉取服务端统计 |
| `bstat.py` | 查询/重置下游统计（被取消放弃的次数 `aborted` 等） |
| `test.sh` | 一键跑 A–D 四组验收，最后输出 `ALL_CHECKS_PASSED` |

两种模式的唯一区别就是本课三条主线：

| 维度 | `naive`（反面教材） | `safe`（正确做法） |
| --- | --- | --- |
| 一次性完成 | 普通布尔：`if not done: ...; done = True` | 锁内检查并设置（等价 C++ `compare_exchange_strong`） |
| 回调持有什么 | 上下文的**强引用**（相当于裸指针 `this`） | `weakref.ref`（相当于 `weak_ptr`，用前先 `lock()`） |
| 完成时 | 立即 `ctx.destroy()`（相当于 `delete this`） | 从注册表移除、丢掉唯一强引用；**取消所有在途下游**（`shutdown`） |
| 超时/取消后下游 | 继续干活，结果晚到 | 收到 EOF 放弃工作 |

### 1. 关键代码

一次性完成（`agg.py`）。注意 `RACE_WINDOW = 0.002`：**人为**在检查与设置之间插入 2ms，把真实系统里纳秒级的窗口放大到能在几秒内稳定复现——真实系统里窗口很小，但 QPS 足够高时一样会撞上。

```python
def try_complete_naive(self):
    if not self.done:            # 检查
        time.sleep(RACE_WINDOW)  # 窗口：另一条线程也可能通过检查
        self.done = True         # 设置
        return True
    return False
def try_complete_safe(self):
    with self.lock:              # 检查与设置原子化（等价 compare_exchange）
        if self.done: return False
        time.sleep(RACE_WINDOW)
        self.done = True
        return True
```

完成者有三类，分别来自不同线程：最后一个下游回调（`on_part` 中 `pending` 减到 0）、超时定时器（`threading.Timer`）、客户端 `CANCEL`。它们都调用同一个 `finish(status)`，谁先抢到完成权谁回包。

晚到回调（`on_part`），safe 模式先 "lock" 弱引用：

```python
if MODE == "safe":
    ctx = ctx_ref_or_ctx()            # weak_ptr::lock()
    call = call_ref_or_call()
    if ctx is None or call is None:
        inc("late_dropped"); return   # 请求已结束，安全丢弃
else:
    ctx, call = ctx_ref_or_ctx, call_ref_or_call
    if ctx.destroyed:                 # 真实 C++ 里这里就是悬垂指针写入
        inc("use_after_destroy"); return
```

Python 不会真的 use-after-free，所以 `Ctx.destroy()` 把字段置 `None` 并打上 `destroyed` 标记，**任何**在销毁后读写它的路径都计入 `use_after_destroy`——这个计数就是 C++ 里会 crash 或静默写坏内存的次数。

取消传播（safe 的 `finish`）：拿到完成权后，在锁内取走已登记的下游连接，逐个 `shutdown(SHUT_RDWR)`；`register()` 也在同一把锁内检查 `done`，请求结束后才建立的下游连接会被立即关闭，避免"取消之后又漏发一个"。

```python
with self.lock:
    socks, self.socks = self.socks, []
for sk in socks:
    try: sk.shutdown(socket.SHUT_RDWR)
    except OSError: pass
self.ctx = None                 # 释放唯一强引用；回调只持弱引用
```

"引用最终释放"的观测点：`Ctx.__init__` 里 `weakref.finalize(self, inc, "alive_ctx", -1)`，对象真正被回收时 `alive_ctx` 才减 1；每个下游操作线程进出时增减 `inflight_ops`；注册表大小 `registry`。

### 2. 操作步骤

```bash
tar xzf practice94.tar.gz && cd practice94
./test.sh            # 约 15 秒，端口 19401-19404（下游）、19411（naive）、19412（safe）
```

想手动玩，可以分别起服务：

```bash
python3 backend.py article 19401 40 &
python3 backend.py stats   19402 50 &
python3 backend.py comment 19403 60 --slow-every 4 --slow-ms 400 &
python3 backend.py relation 19404 45 --fail-every 5 &
B='{"article":19401,"stats":19402,"comment":19403,"relation":19404}'
python3 agg.py 19412 safe "$B" &
python3 client.py 19412 40 150                       # 40 个请求，超时 150ms
python3 client.py 19412 30 2000 --cancel-after 100 --wait 0.15   # 100ms 后全部取消
printf 'REQ 7 150\n' | nc -q1 127.0.0.1 19412        # 看一条原始响应（需要 nc）
```

### 3. 真实运行结果（本机一次 `./test.sh`，数字每次略有波动）

**A. 正常 fan-out + 部分失败 + 超时**（comment 每 4 个请求慢到 400ms；relation 每 5 个失败；timeout=150ms；40 个请求）

| 模式 | 回包 | 重复完成 | 状态分布 | 晚到回调 |
| --- | --- | --- | --- | --- |
| safe | 40/40，`dup_rids=0` | 0 | OK 24 / PARTIAL 6 / TIMEOUT 10 | `late_dropped=10`，`use_after_destroy=0` |
| naive | 40/40 | 0 | 同上 | `use_after_destroy=10` |

- rid 为 4 的倍数的 10 个请求 comment 慢到 400ms → 150ms 时超时，带着已到达的部分结果回 `TIMEOUT`；
- rid 为 5 的倍数且不慢的 6 个请求（5/10/15/25/30/35）relation 失败 → 非关键下游失败，降级回 `PARTIAL`（若 article 失败则回 `ERROR`）；
- safe 下这 10 个超时请求的 comment 回调都晚到，全部被弱引用检查丢弃；naive 下同样 10 个回调全部摸到了已销毁上下文。safe p50≈84ms、p99≈154ms（p99 被 150ms 超时封顶）。

**B. 串行 vs 并行**（40 个请求一次性突发进入，timeout=1000 不触发超时）

| 方式 | p50 | p99 | 聚合层同时在途的下游调用峰值 |
| --- | --- | --- | --- |
| 并行 fan-out | 124.4ms | 475.7ms | 148 |
| 串行 | 249.9ms | 595.0ms | 40 |

并行把关键路径从"各下游之和"压到"最慢那个"，p50 减半；代价是同一瞬间压到下游的调用数从 40 涨到 148（理论上限 40×4=160）——这正是 tradeoffs 说的"并行调用降低关键路径时间，也放大下游瞬时并发"。（突发 40 个请求、每个连接一线程，排队使 p50 高于单请求的约 60–70ms。）

**C. 超时与正常完成同时到达**（comment 固定 60ms，timeout 也设 60ms，200 个请求，检查-设置之间 2ms 窗口）

| 模式 | `dup_rids`（客户端收到 ≥2 次响应的请求数） | 服务端 `replies` | `double_complete` |
| --- | --- | --- | --- |
| naive（普通布尔） | **184** | 384 | 184 |
| safe（锁内 CAS） | **0** | 200 | 0 |

这就是 pitfalls「超时处理与正常完成可能并发发生，不能只靠普通布尔标记」的直接证据：定时器线程和最后一个下游回调几乎同时通过 `if not done`，同一请求回了两次包。safe 下 200 个请求恰好 200 次回包（本次 200 个都被定时器抢先，状态为 TIMEOUT；谁赢不重要，重要的是只有一个赢家）。

**D. 客户端取消后引用最终释放**（comment 全部 400ms，发出 100ms 后取消全部 30 个请求，取消后 150ms 采样）

| 模式 | 取消后 150ms `inflight_ops` | `alive_ctx` | `registry` | comment `aborted` / `served` | 之后 |
| --- | --- | --- | --- | --- | --- |
| safe | **0** | **0** | 0 | 30 / 0 | 30 个均 `CANCELLED` 完成一次 |
| naive | 30 | 30 | — | 0 | 再等 500ms 后归零，但 `use_after_destroy=30` |

safe：取消传播到下游，comment 30 次全部放弃在途工作，在途操作、存活上下文、注册表引用全部归零——满足 acceptance「取消后所有引用最终释放」。naive：取消只是回了包，下游继续白干 300ms，回调晚到时全部写向已销毁上下文——违反 acceptance「回调晚到不会访问已销毁上下文」。

最后一行输出：

```
ALL_CHECKS_PASSED
```

### 4. 对照验收标准

| acceptance 原文 | 本实验证据 |
| --- | --- |
| 回调晚到不会访问已销毁上下文 | A：safe `late_dropped=10`、`use_after_destroy=0`；naive 对照 `use_after_destroy=10`；D：naive 取消后 `use_after_destroy=30` |
| 取消后所有引用最终释放 | D：safe 取消后 150ms `inflight_ops=0`、`alive_ctx=0`（由 `weakref.finalize` 在对象真正回收时计数）、`registry=0`，下游 `aborted=30` |

### 5. 延伸练习

1. 把 `RACE_WINDOW` 改成 0，提高请求数到 2000，观察 naive 的 `dup_rids` 是否仍偶发出现——窗口变小只是降低概率，不是消除。
2. 把 safe 的弱引用方案改成"回调持强引用（类 `shared_ptr`）+ 最后一个回调结束才释放"：`alive_ctx` 在 D 中会延迟到下游结束才归零，体会"完成"与"释放"是两个时刻。
3. 给 fan-out 加一个并发度上限（例如全局 `threading.BoundedSemaphore(64)`），重跑 B，观察 `peak_inflight_ops` 与 p99 的变化。
4. 用 C++ 对照：阅读 gRPC callback 示例，找到你的 reactor 在哪里 `delete this`，确认它在 `OnDone` 中（以所读版本为准）。

## 源码与文档阅读

> **sourceStudy 原文**：沿gRPC官方callback示例研究OnDone、取消通知和对象销毁时序，核对当前版本API约束。

### 阅读路线

1. **打开 [Asynchronous Callback API Tutorial](https://grpc.io/docs/languages/cpp/callback/)**，先读 Overview：gRPC C++ 有同步 API 和两种异步 API——较老的基于完成队列（completion-queue based）、较新的基于回调（callback-based）。本课实验的 naive/safe 对照，对应的就是这两种异步 API 都要面对的寿命问题。
2. **一元 RPC · Custom Unary Reactor**：注意 `OnDone()` 里做最终清理并 `delete this`，`OnCancel()` 只记日志；以及 NOTE——回调方法应快速返回，不要做阻塞工作。
3. **一元 RPC · Client**：客户端把 `ClientContext`、请求、响应和一个 `std::function<void(Status)>` 交给 `stub_->async()->GetFeature()`；示例用 mutex + 条件变量等回调完成后才返回，所以栈上的 `context` 和按引用捕获的局部变量是安全的。思考：去掉等待会怎样？
4. **服务端流式 / 客户端流式 / 双向流**：比较 `ServerWriteReactor`、`ServerReadReactor`、`ServerBidiReactor` 中 `OnWriteDone(ok)` / `OnReadDone(ok)` 遇到 `ok == false` 时如何调用 `Finish()`，以及 `Finish()` 之后为什么还要等 `OnDone()` 才释放。
5. **客户端 `ClientWriteReactor` 中的 `AddHold()` / `RemoveHold()`**：示例注释说明，部分 `StartWrite` 是从定时器（`grpc::Alarm`）回调间接发起的，所以用 hold 防止 RPC 提前结束。这是"外部事件源与 RPC 寿命协调"的官方样例。
6. **双向流服务端的注释**："reactor 很可能让我们跳线程"——这就是回调可能在不同线程执行、共享状态需要加锁的证据。
7. **源码仓库**：在 gRPC 仓库中找到 route_guide 的 callback 版本示例（具体目录与文件名以仓库当前版本为准），对照教程逐段读；再在头文件中查 reactor 基类对 `OnDone`、`OnCancel`、`TryCancel` 的注释，核对"`OnCancel` 何时会被调用、能否与其他回调并发"的当前约束。
8. 记录**访问日期**和你核对的 **gRPC 版本 / 提交**。本教程页面底部写有最近修改日期（本课写作时为 2024-06-18 的 C++ Callback API Tutorial 版本），读的时候以你看到的为准。

### 阅读笔记模板

| 问题 | 文档/源码原文要点 | 我的理解 | 待验证（写个小实验） |
| --- | --- | --- | --- |
| `OnDone` 何时调用？之后框架还会访问 reactor 吗？ | | | |
| `OnCancel` 会在什么线程、什么时刻到达？可能与 `OnReadDone` 并发吗？ | | | |
| `Finish()` 调用后还能 `delete this` 吗？ | | | |
| 客户端回调里 `ClientContext` 必须活到什么时候？ | | | |
| `TryCancel()` 之后回调还会不会到？带什么状态？ | | | |
| `AddHold()` 解决了什么问题？不加会怎样？ | | | |
| 完成队列模式下 `Next()` 返回 `ok=false` 代表什么？ | | | |

### 边界

- 公开文档只描述开源 gRPC 的 API 语义，**不能**据此推断任何企业内部 RPC 框架的实现。
- 本课 Python 实验（线程 + 弱引用 + 关闭连接）只模拟"一次性完成 / 晚到回调 / 取消传播"的语义，不等于 C++ API；C++ 中真正的风险（use-after-free）在 Python 里只能用 `destroyed` 标记来计数观测。
- 文档中的类名与约束会随版本变化（callback API 曾长期处于 experimental 命名空间），一切以你核对的版本为准。

## 自测题

**1. 聚合服务用 `if not self.done: self.done = True; reply()` 防重复回包，压测中仍偶发重复回包，为什么？怎么修？**

答案：检查与设置是两步而非一个原子操作，超时定时器线程与最后一个下游回调线程可以同时读到 `done == False`，都进入分支各回一次包。修法：把"检查并设置"变成原子抢占——锁内检查并设置、`std::atomic<bool>::compare_exchange_strong(expected=false, true)`、或 `std::call_once`；只有抢到的一方回包，其余路径只做清理并退出。

解析：这正是 pitfalls「超时处理与正常完成可能并发发生，不能只靠普通布尔标记」。C++ 里非原子 bool 并发读写还是数据竞争（UB）。实验 C 中 naive 模式 200 个请求有 184 个被重复回包，safe 模式恰好 200 次回包。注意：GIL / 单线程事件循环只能降低概率，只要检查与设置之间有线程切换点或 `await`，窗口就在。

**2. 超时先触发并回包、释放了请求对象，200ms 后评论服务回调到达导致 crash。给出至少两种修法，并说明各自对"释放时机"的影响。**

答案：
- **回调持 `weak_ptr`，用前 `lock()`**：请求完成即可释放对象，晚到回调 `lock()` 失败直接丢弃。释放最早，但要确保每个下游调用自己的 `ClientContext`/响应缓冲区由该调用单独持有、活到其回调结束。
- **回调持 `shared_ptr`**：对象寿命延长到最后一个在途回调结束；回包（完成）早，释放晚。要警惕引用环，并配合取消下游以缩短"白等"的时间。
- **引用计数 + 统一释放点**：每个在途操作 +1，完成/取消通知到达 -1，归零时释放（与 callback API 中"在 `OnDone` 里 `delete this`"同一思路）。释放点集中、可监控，但计数必须无竞态。

解析：三者都把"完成（回包）"与"释放（所有在途操作结束）"分成两个时刻，满足 acceptance「回调晚到不会访问已销毁上下文」。实验 A：safe 模式 10 个晚到回调全部被 `late_dropped`，naive 模式 10 个全部 `use_after_destroy`。

**3. 一个请求 fan-out 4 路，客户端取消后，如何证明"取消后所有引用最终释放"？tradeoffs 中的下游并发放大如何影响取消的价值？**

答案：暴露并断言三类指标最终归零——在途下游操作数（进出操作时增减）、存活请求上下文数（对象真正析构/回收时减 1，如 Python `weakref.finalize`、C++ 析构函数计数）、注册表/在途请求表大小；同时在下游侧看"被放弃的工作"计数，证明取消确实传播了。测试方法：让一个下游固定很慢，发请求后中途取消，在一个小于下游耗时的时间点采样，期望已全部归零。实验 D：safe 模式取消后 150ms `inflight_ops=0`、`alive_ctx=0`、`registry=0`，下游 `aborted=30`；naive 模式同一时刻 `inflight_ops=30`，下游一个没放弃。

解析：并行 fan-out 会把一次入口请求变成同一瞬间的 4 个下游调用（实验 B：峰值 40 → 148），所以一次取消如果不传播，浪费的是 4 份下游资源；在下游已经变慢、用户纷纷放弃的时刻，这正是雪崩的燃料。并发放大越大，取消传播的价值越大。

## 推荐资源

- [异步RPC与生命周期协调：参考文档 / 原始资料](https://grpc.io/docs/languages/cpp/callback/)，来源 grpc.io｜文档｜英文｜深入｜免费。阅读重点：重点阅读完成队列与状态机与一次性完成和取消竞态，用于核对本节点实验的语义和边界。建议结合 practice94 核对：先跑 `./test.sh` 看 naive/safe 的对照数字，再回到教程中 `OnDone` / `OnCancel` / `AddHold` 的段落，逐条对应。

## 本课小结

- **summary**：异步调用允许重叠等待（关键路径从"求和"变成"取最大"），但必须协调完成、取消、超时和回调可能到达的顺序。
- **完成队列与状态机**：事件靠 tag 找回每请求状态机；DONE 是吸收态，之后所有事件只做清理。
- **一次性完成和取消竞态**：完成、超时、取消三路可能并发，唯一裁决点必须是原子抢占；取消是合作式的，抢到完成权的一方要取消所有在途下游。
- **响应对象及控制器寿命**："回包"和"释放"是两个时刻；回调用 `weak_ptr` / `shared_ptr` / 引用计数之一管理寿命，callback API 中在 `OnDone` 里做最终清理。
- **acceptance**：回调晚到不会访问已销毁上下文；取消后所有引用最终释放——两条都要用指标和测试证明，而不是靠"看起来没 crash"。
- **pitfalls**：超时处理与正常完成可能并发发生，不能只靠普通布尔标记。
- **tradeoffs**：并行调用降低关键路径时间，也放大下游瞬时并发；需要时限制并发度、分阶段调用，并让超时/取消真正传播到下游。
- **边界**：Python 实验只模拟语义；gRPC API 细节以所读版本为准；文中业务场景均为教学假设，不代表任何企业内部实现。
