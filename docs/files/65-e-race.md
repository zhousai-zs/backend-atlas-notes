# 第 65/208 课：数据竞争与线程安全契约

- **阶段**：第 2 阶段 · 并发与内存模型（同步与正确性）
- **前置课程**：c-thread、b-lifetime
- **关键词**：数据竞争、线程安全、线程封闭、happens-before、共享可变状态、逻辑原子性、可重入、TSan、mutex、复合操作、volatile误区
- **难度**：基础
- **所属领域**：E · 并发与内存模型（子类：同步与正确性）
- **标签**：Data Races、E、C++

---

## 导读

上一课讲的是**海量连接与接入治理**：管住「多少连接进来、每条连接花多少内存、什么时候拒绝、什么时候优雅关闭」。那一课的核心是**资源预算**——fd、内存、内核队列、超时。连接对象再多，至少每个对象在逻辑上是「一条连接、一个归属」。

本课换一个维度：**同一个进程里，多个工作线程同时碰同一块内存**。这时的敌人不再是「连接太多」，而是「两个线程对同一块内存的访问没有排好队」。

节点把这件事总结得很精确：

> 不同线程无同步地冲突访问同一内存且至少一个为写，会构成 C++ 数据竞争并导致未定义行为。

微信公众号后台里，这类内存到处都有：

- 阅读数 / 点赞数 / 消息计数：多个工作线程同时 `++`；
- 会话表：线程 A 正在 `find`，线程 B 正在 `erase`；
- 连接状态：IO 线程写 `closed=true`，业务线程还在读 `buf`；
- 限流桶：两个线程同时「先查令牌，再扣令牌」；
- 配置热更新：一个线程替换 `shared_ptr`，另一个线程解引用旧对象；
- 统计上报：多个线程往同一个 `std::map` 里插入。

它们的共同点是：**代码在单线程下完全正确，一上多线程就偶发错数、崩溃、幽灵值**。更麻烦的是，这类 bug 往往：

- 压力小时不出现，压测时出现；
- 换个编译选项就消失；
- 加一行日志就「好了」；
- 在开发机复现不了，只在生产环境偶发。

本课的目标不是让你背「锁的八种用法」，而是把「偶发错误」变成**可检查的共享状态规则**：先问哪些状态被多个线程共享，再问这些访问之间有没有 happens-before，最后才问用什么同步原语。配套工具是 C++ memory model、`std::mutex`、`std::atomic` 和 ThreadSanitizer（TSan）。

本课与 c-thread（线程创建、join、参数传递）和 b-lifetime（对象寿命、RAII、所有权）直接相连：**数据竞争是「访问顺序」问题，而对象寿命是「访问是否还有效」问题，两者经常一起出现**。一个指针上的竞争，可能最终表现为 use-after-free；一个「看起来只是错数」的计数器，背后可能是未定义行为把整个优化假设掀翻。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释共享与线程封闭**
   - 能列出进程内哪些状态是跨线程共享的：全局变量、静态变量、堆对象、被多个线程捕获的 lambda 状态、连接对象里的字段、缓存与注册表。
   - 能说明线程封闭的三种常见做法：栈上局部变量、`thread_local`、对象只被单一线程拥有（对象封闭）。
   - 能判断「什么时候必须共享」：会话表、限流桶、指标聚合、连接注册表、配置快照，这些天然要被多个线程看到，不能简单封闭。
   - 能说出线程封闭的代价：减少同步，但需要消息传递或所有权转移，跨线程交互要设计队列/邮箱。

2. **能解释 happens-before**
   - 能说清「程序顺序」不等于「跨线程可见顺序」：编译器与 CPU 都可以重排，缓存可以不同步。
   - 能列出建立 happens-before 的常见手段：`std::thread` 构造与 `join`、mutex 的 `unlock` 与后续 `lock`、原子操作的 release/acquire、条件变量的通知与等待。
   - 能判断「没有 happens-before 的冲突读写 = 数据竞争」，并解释为什么数据竞争是未定义行为，而不只是「读到旧值」。

3. **能解释线程安全、可重入与逻辑原子性**
   - 能区分「线程安全」与「可重入」：线程安全靠锁也能做到；可重入通常要求不依赖共享可变状态，或在递归/回调路径上不死锁。
   - 能识别 check-then-act、read-modify-write 这类复合操作，知道 `counter++`、`if (!map.count(k)) map[k] = v` 都不是天然原子。
   - 能说明「逻辑原子性」：业务上必须一起成立的不变量，必须在同一个临界区里维护，而不是把几个原子操作拼起来就当原子。

4. **能独立完成实践**
   - 为计数器复现竞态：多线程自增，观察到丢更新。
   - 使用 TSan 复现并读懂报告，再用 `std::mutex` 修复，确认 TSan 干净。
   - 说明复合操作边界：什么时候一个 `std::atomic` 就够，什么时候必须用锁保护多个字段/多个步骤。
   - 修复时同时考虑对象寿命与逻辑不变量，而不是只把一行读写包进锁。

5. **满足本课验收标准（acceptance）**
   - **能识别读写竞争，而不只写写竞争**：看到「一个线程读、另一个线程写」也能判定为数据竞争。
   - **修复包含对象寿命与逻辑不变量**：不只消除 TSan 报告的那一行，还要保证对象在使用期间活着，业务不变量在临界区内始终成立。

---

## 为什么后端要懂

节点给的 why 是：

> 把偶发错误转成可检查的共享状态规则。

这句话听起来抽象，落到公众号后台值班，就是下面几件事。

### 1. 「偶发错数」通常不是业务逻辑错，而是共享状态没有契约

假设消息计数这样写：

```cpp
class Metrics {
public:
    void incr() { ++count_; }
    long get() const { return count_; }
private:
    long count_ = 0;
};
```

单线程压测一切正常。上线后多个 IO 线程同时 `incr()`，监控里出现：

- 总计数比实际少；
- 两个线程各自看到的数不一样；
- 偶尔出现负数或跳变；
- 重启后数据对不上。

这不是「统计口径」问题，而是 `++count_` 被拆成 load / add / store 三个步骤，两个线程的步骤交错，更新互相覆盖。这类错误没有异常、没有日志、没有明确崩溃点，只有**结果不对**。

### 2. 数据竞争是未定义行为，不是「读到旧值」这么温和

很多人对竞争的心理模型是：

> 最坏情况就是读到旧值，业务上能忍。

C++ 标准不是这么规定的。数据竞争导致**未定义行为（undefined behavior）**。这意味着编译器可以假设它不发生，从而做出在单线程看来合理、在竞争下离谱的优化：

- 把循环里的重复读取提到循环外（因为「没人会改它」）；
- 把 `if (flag) ...` 优化成只读一次；
- 让指针指向已经释放的对象；
- 让整个函数的行为不可预测。

所以竞争的风险不是「数据旧一点」，而是**程序语义整体失效**。这也是为什么「加日志就好了」「换个优化级别就好了」不是修复，而是把未定义行为换了一种表现形式。

### 3. 线上事故常常是「读写竞争」，不是「写写竞争」

写写竞争容易理解：两个线程同时写，肯定有一个被覆盖。

更难发现的是读写竞争：

- 线程 A 在遍历会话表，线程 B 插入一个节点，A 的迭代器失效；
- 线程 A 读 `connection->state`，线程 B 同时把它置为 `CLOSED` 并释放缓冲；
- 线程 A 读配置指针，线程 B 替换指针并释放旧配置。

这些场景里，「读」看起来无害，但**只要另一个线程在写同一内存，且两者没有 happens-before，就是数据竞争**。本课验收特别强调「能识别读写竞争而不只写写」，原因就在这里。

### 4. 修复不只是加锁，还要守住对象寿命和业务不变量

一个典型错误修复是：

```cpp
// 错误示范：只锁了写入，读取仍然裸奔
void setState(State s) {
    std::lock_guard<std::mutex> lk(mu_);
    state_ = s;
}
State state() const { return state_; }  // 没有锁
```

这样 TSan 可能只报一半，业务上仍然可能读到撕裂状态。更完整的修复要回答：

- 这个对象在所有访问路径上是否都活着？（对象寿命）
- 多字段之间的一致性由谁保证？（逻辑不变量）
- 锁保护的是「一个字段」还是「一组必须一起变化的状态」？
- 回调、超时、关闭路径会不会在持锁时重入？

### 5. 公众号后台里你会真正碰到的场景

| 现象 | 更像数据竞争 | 更像对象寿命 | 更像逻辑原子性 |
|------|--------------|--------------|----------------|
| 计数偶尔少、监控对不上 | 多线程 `++` 无锁 | 否 | 自增本身不是原子 |
| 压测时偶发 segfault，堆栈在 `std::map` | 遍历与插入并发 | 迭代器失效 | 否 |
| 关闭连接后偶发访问已释放缓冲 | 状态与指针无同步 | 旧指针被解引用 | 关闭与清理不是一个临界区 |
| 限流偶尔放过超额请求 | 查令牌与扣令牌分开 | 否 | check-then-act |
| 热更新配置后偶发崩溃 | 指针读写竞争 | 旧配置被释放 | 替换与引用计数不原子 |

不会区分时，最常见的错误动作是：**看到崩溃就加大日志、加 sleep、换成 `volatile`、或者「重试一下」**。这些都没有建立 happens-before，只是改变时序，让问题更难复现。

---

## 直觉 → 原理

### 0. 先建立直觉：共享白板、私人笔记本、传纸条

把多线程程序想成一间办公室：

```text
        共享白板（全局变量 / 堆对象 / 会话表）
          ▲            ▲              ▲
          │            │              │
       线程 A        线程 B         线程 C
          │            │              │
      私人笔记本    私人笔记本      私人笔记本
      （栈 / thread_local）
```

三种交互方式：

1. **共享白板**：谁都能写。必须约定「什么时候可以写、写完怎么让别人看到」。没有约定，两个人同时擦写，结果不可预测。
2. **私人笔记本**：只有本人能看。线程封闭，不需要锁，也没有竞争。
3. **传纸条**：不共享白板，而是把数据装进消息，投递到对方的收件箱。消息传递，所有权转移。

数据竞争永远发生在第 1 种情况：**共享 + 可变 + 至少一个线程在写 + 没有同步**。四个条件去掉任意一个，就不会有数据竞争：

- 不共享 → 线程封闭；
- 不可变 → 只读共享；
- 没有写 → 全读；
- 有同步 → happens-before 建立。

后端设计的第一反应应该是：**能不能把共享变小、把可变变小、把同步边界画清楚**，而不是一上来就加锁。

### 1. 共享与线程封闭

#### 1.1 进程里到底哪些东西是共享的

C++ 线程共享同一个地址空间。以下状态默认跨线程可见：

| 状态 | 例子 | 是否共享 |
|------|------|----------|
| 全局变量 / 静态变量 | `static std::map<int, Session> g_sessions;` | 共享 |
| 堆对象 | `new Connection(...)`、`std::make_shared<Config>()` | 只要指针被多个线程拿到就共享 |
| 对象的成员 | `Connection::state_`、`Metrics::count_` | 取决于对象被谁持有 |
| 函数内局部变量 | `int local = 0;` | 每个线程独立栈帧，通常不共享 |
| `thread_local` | `thread_local int tls_buf;` | 每线程一份，不共享 |
| 捕获引用/指针的 lambda | `[&counter] { ++counter; }` | 共享被捕获对象 |
| 捕获值的 lambda | `[counter] { ... }` | 捕获副本通常封闭，但副本里的指针仍可能指向共享对象 |

注意最后一行：**「按值捕获」不等于安全**。如果捕获的是一个裸指针或 `shared_ptr` 指向的共享对象，副本只是指针/引用计数，指向的仍是同一块内存。

#### 1.2 线程封闭的三种形态

**形态一：栈上局部变量**

```cpp
void handleRequest(const Request& req, Response& resp) {
    std::string scratch;          // 本线程栈上，天然封闭
    scratch.reserve(256);
    render(req, scratch);
    resp.body = std::move(scratch);
}
```

只要不把 `scratch` 的地址传出去，它就不会被其他线程访问。这是最便宜、最可靠的封闭。

**形态二：`thread_local`**

```cpp
thread_local std::string tls_scratch;

void handleRequest(const Request& req, Response& resp) {
    tls_scratch.clear();          // 每个线程一份，无竞争
    render(req, tls_scratch);
    resp.body = tls_scratch;
}
```

适合「每线程一份、生命周期长、避免反复分配」的缓冲。代价是：

- 每个线程都占一份内存，线程多时内存放大；
- 初始化/析构时机与线程生命周期绑定；
- 不要把 `thread_local` 的地址传给别的线程长期使用。

**形态三：对象封闭（所有权只属于一个线程）**

```cpp
class Connection {
public:
    // 只在 IO 线程调用
    void onReadable();
    void onWritable();
    void close();
private:
    Buffer in_;
    Buffer out_;
    State state_ = State::Open;
};
```

约定：`Connection` 对象从创建到销毁都由同一个 IO 线程处理。其他线程不能直接调用它的方法，只能投递任务：

```cpp
// 业务线程不直接碰 connection，而是把任务投递到它所属的 IO 线程
ioLoopOf(conn)->post([conn, msg = std::move(msg)] {
    conn->send(msg);   // 在 IO 线程执行，天然串行
});
```

这就是很多 Reactor / proactor 框架的核心契约：**连接对象线程封闭，跨线程只传任务**。它把大量锁换成了队列，把「共享内存同步」换成了「消息传递」。

#### 1.3 什么时候必须共享

有些状态无法封闭：

- **会话表**：任意工作线程都可能处理同一用户的下一个请求；
- **限流桶**：按用户/接口计数，多个线程要共同扣减；
- **连接注册表**：管理线程要遍历所有连接做超时清理；
- **全局指标**：所有线程都要上报；
- **配置快照**：热更新后所有线程都要看到新版本；
- **任务队列**：生产者与消费者跨线程。

这些状态必须共享，就必须有明确的同步契约。契约要回答四个问题：

1. 谁可以读？谁可以写？
2. 读和写各自需要持哪把锁？
3. 一次操作涉及哪些字段，必须一起变化？
4. 对象在什么时候销毁，销毁后还有谁会访问？

#### 1.4 线程封闭不是「银弹」，它要求消息传递

封闭的代价是：跨线程交互必须显式化。你需要：

- 每线程一个任务队列（MPSC / MPMC）；
- 任务里携带所有权（`std::unique_ptr`、`std::move`）；
- 处理队列满、关闭、线程退出时的未完成任务；
- 保证投递顺序与业务顺序一致。

所以节点里的 tradeoff 是准确的：

> 线程封闭减少同步成本，但可能需要消息传递。

对公众号后台这种「连接状态多、共享表少」的系统，常见组合是：

```text
连接对象：线程封闭（IO 线程独占）
会话表：共享 + 分片锁
限流桶：共享 + 原子操作 / 分片
指标：每线程本地累加 + 定期合并
配置：共享 + shared_ptr 原子替换
```

### 2. happens-before

#### 2.1 为什么「我写了，你应该能看到」不成立

先看一段单线程看起来毫无问题的代码：

```cpp
int data = 0;
bool ready = false;

// 线程 A
void producer() {
    data = 42;      // (1)
    ready = true;   // (2)
}

// 线程 B
void consumer() {
    if (ready) {    // (3)
        assert(data == 42);  // (4)
    }
}
```

很多人会认为：A 先写 `data` 再写 `ready`，B 看到 `ready == true` 就一定能看到 `data == 42`。

在 C++ 内存模型里，这个推理**不成立**，原因有三层：

1. **编译器重排**：编译器和 CPU 可以交换没有依赖的读写顺序；
2. **缓存/可见性**：不同核心的缓存不保证同步；
3. **没有 happens-before**：`ready` 的写和读之间没有建立同步关系。

更严重的是，`ready` 和 `data` 都是非原子变量，线程 A 写、线程 B 读，**这本身就是数据竞争**，直接触发未定义行为。也就是说，这段代码不是「可能读到旧值」，而是「程序没有定义」。

#### 2.2 happens-before 的直觉定义

C++ 的 happens-before 是一个偏序关系，回答的是：

> 如果操作 X happens-before 操作 Y，那么 X 的结果对 Y 可见，且 X 在 Y 之前发生。

三个基本来源：

**（a）程序顺序（sequenced-before）**

同一线程内，前面的语句先于后面的语句。但注意：这只是**单线程视角**，不自动跨线程。

**（b）线程生命周期**

```cpp
int x = 0;

std::thread t([&] {
    x = 1;          // 线程函数内的写
});

t.join();           // join 返回后
assert(x == 1);     // 一定可见
```

标准规定：

- `std::thread` 构造函数返回时，新线程的启动 happens-after 构造完成；
- `join()` 返回时，线程函数的所有操作 happens-before `join()` 之后的操作。

所以 `join` 之后的读取是安全的。反过来，`detach()` 没有这个保证，主线程不能假设分离线程已经完成。

**（c）同步原语**

mutex：

```cpp
std::mutex mu;
int x = 0;

// 线程 A
{
    std::lock_guard<std::mutex> lk(mu);
    x = 1;
}   // unlock

// 线程 B
{
    std::lock_guard<std::mutex> lk(mu);
    assert(x == 1);  // 可见
}   // lock 与之前的 unlock 建立 synchronizes-with
```

同一把 mutex 的 `unlock` **synchronizes-with** 之后对它的 `lock`，因此 A 在临界区内写的 `x` 对 B 可见。这是「锁不仅互斥，还负责可见性」的准确含义。

原子操作：

```cpp
std::atomic<bool> ready{false};
int data = 0;

// 线程 A
data = 42;
ready.store(true, std::memory_order_release);

// 线程 B
while (!ready.load(std::memory_order_acquire)) {}
assert(data == 42);  // release/acquire 建立 happens-before
```

`release` 写与读到该值的 `acquire` 读建立 synchronizes-with。注意 `data` 本身仍然是非原子变量，但因为 release/acquire 建立了顺序，A 对 `data` 的写 happens-before B 对 `data` 的读，所以不构成数据竞争。

条件变量：

```cpp
std::mutex mu;
std::condition_variable cv;
bool hasTask = false;

// 消费者
{
    std::unique_lock<std::mutex> lk(mu);
    cv.wait(lk, [&] { return hasTask; });
    // 被唤醒并重新拿到锁后，生产者在锁内写的状态可见
}
```

条件变量的 `wait` 返回前会重新加锁，因此与生产者的 `unlock` 建立同样的同步关系。关键点是：**共享状态必须在同一把锁下修改，并用谓词重新检查**，否则会出现丢失唤醒或读到不一致状态。

#### 2.3 数据竞争的准确判定

把上面的直觉写成检查清单：

```text
两个访问是否冲突？
  ├─ 同一内存位置（同一变量、同一对象字段、同一数组元素）
  ├─ 至少一个是写
  └─ 两个访问来自不同线程
        │
        ▼
这两个访问之间是否存在 happens-before？
  ├─ 是 → 不是数据竞争
  └─ 否 → 是数据竞争 → 未定义行为
```

三个容易踩的边界：

1. **「同一内存位置」不是「同一变量名」**：结构体两个不同字段通常不冲突；但相邻的位域可能共享同一个内存位置。
2. **原子操作不参与数据竞争**：`std::atomic<int>` 的读写是原子的，不会构成数据竞争；但「多个原子操作组成的复合逻辑」仍可能不是原子的。
3. **happens-before 可以传递**：A → B、B → C，则 A → C。锁与锁之间、线程创建/join、原子操作可以串成链。

#### 2.4 一个反直觉例子：锁保护了，但读到的是旧值？

```cpp
std::mutex mu;
int x = 0;

// 线程 A
void write() {
    std::lock_guard<std::mutex> lk(mu);
    x = 1;
}

// 线程 B
int read() {
    std::lock_guard<std::mutex> lk(mu);
    return x;
}
```

这段是安全的：B 要么在 A 之前拿锁看到 0，要么在 A 之后拿锁看到 1。不会有数据竞争。

但如果 B 不拿锁：

```cpp
int readUnsafe() {
    return x;   // 与 A 的写冲突，且没有 happens-before
}
```

即使 `x` 是 `int`、即使你「只读一次」、即使 x86 上这个读看起来是单条指令，**它仍然是数据竞争**。这就是「读写竞争」的典型：读的一方经常觉得自己无辜。

#### 2.5 用编译器和 TSan 建立手感

先写一个最小竞争程序：

```cpp
// race_counter.cc
#include <cstdio>
#include <thread>
#include <vector>

long counter = 0;   // 非原子，共享

void worker() {
    for (int i = 0; i < 1000000; ++i) {
        ++counter;   // load + add + store，不是原子操作
    }
}

int main() {
    std::vector<std::thread> threads;
    for (int i = 0; i < 4; ++i) {
        threads.emplace_back(worker);
    }
    for (auto& t : threads) {
        t.join();
    }
    std::printf("counter = %ld\n", counter);
    return 0;
}
```

普通编译并运行：

```bash
g++ -std=c++20 -O2 -pthread race_counter.cc -o race_counter
./race_counter
./race_counter
./race_counter
```

你可能会看到 `4000000`，也可能看到 `3999871`、`3123456` 之类的数。**不要用「跑几次都对」来证明没有竞争**，竞争是否命中取决于调度、缓存、优化和机器负载。

用 TSan 编译：

```bash
g++ -std=c++20 -O1 -g -fsanitize=thread -fno-omit-frame-pointer \
    -pthread race_counter.cc -o race_counter_tsan

TSAN_OPTIONS="halt_on_error=0 history_size=7" ./race_counter_tsan
```

TSan 的报告会包含类似信息（示意，实际行号/地址不同）：

```text
WARNING: ThreadSanitizer: data race (pid=12345)
  Write of size 8 at 0x... by thread T2:
    #0 worker() race_counter.cc:11 (race_counter_tsan+0x...)
    #1 void std::__invoke_impl<...>(...) <null> (libstdc++.so.6+0x...)
  Previous write of size 8 at 0x... by thread T1:
    #0 worker() race_counter.cc:11 (race_counter_tsan+0x...)
  Location is global 'counter' of size 8 at 0x... (race_counter_tsan+0x...)
  Thread T2 (tid=..., running) created by main thread at:
    #0 pthread_create ...
    #1 main race_counter.cc:19 (race_counter_tsan+0x...)
SUMMARY: ThreadSanitizer: data race race_counter.cc:11 in worker()
```

读报告时抓住四件事：

1. **冲突类型**：`Write of size 8` / `Previous write`，说明是两个写；如果是 `Read of size 4` 与 `Write of size 4`，就是读写竞争。
2. **两个栈**：两边的调用栈指向同一处代码或同一块数据。
3. **Location**：全局变量、堆地址还是栈地址；这决定修复策略。
4. **线程创建栈**：确认是哪两个线程，以及它们由谁创建。

TSan 的边界要记住：

- 它只能发现**本次运行中实际发生**的竞争，没命中的路径不会报；
- 对无锁算法、自定义同步，可能需要注解（`__tsan_acquire` / `__tsan_release` 或 `std::atomic` 的原子操作）；
- 不能与 AddressSanitizer 同时链接；对象寿命问题要另开 ASan 构建；
- 编译器优化级别、内联、`-fno-omit-frame-pointer` 都影响可读性，建议 `-O1 -g`。

### 3. 线程安全、可重入与逻辑原子性

#### 3.1 线程安全的接口契约

「这个类线程安全吗？」不是一个是非题，必须先问：**在什么使用方式下线程安全？**

常见契约有四档：

| 契约 | 含义 | 例子 |
|------|------|------|
| 不可共享 | 对象只能被一个线程使用 | `Connection`、`Buffer` |
| 可并发读 | 多个线程可同时读，写需要独占 | 只读配置快照 |
| 线程安全 | 多个线程可同时调用公开接口，内部同步 | 加锁的 `SessionTable` |
| 外部同步 | 库不管，调用方负责加锁 | 裸 `std::map`、`std::vector` |

写代码时要把契约写进注释或类型名里，而不是让调用方猜：

```cpp
// 线程安全：所有公开方法内部加锁；但返回的迭代器/引用只在锁内有效。
class SessionTable {
public:
    std::optional<Session> get(UserId id) const;
    void put(UserId id, Session s);
    void erase(UserId id);
    size_t size() const;

private:
    mutable std::mutex mu_;
    std::unordered_map<UserId, Session> map_;
};
```

注意最后一句：**「线程安全」不等于「返回的引用/指针可以带出锁外使用」**。如果 `get` 返回 `Session&`，调用方在锁外继续用，就会与 `erase` 竞争。正确做法是返回值拷贝、`shared_ptr` 快照，或提供「在锁内执行回调」的接口。

#### 3.2 可重入与线程安全的区别

- **线程安全**：多个线程同时调用不会破坏内部状态。可以用锁实现。
- **可重入**：函数在执行过程中被再次调用（同一线程递归、信号处理、回调）仍然正确。通常要求不依赖共享可变状态，或不持有不可重入的锁。

一个线程安全但**不可重入**的例子：

```cpp
class Registry {
public:
    void forEach(const std::function<void(int)>& fn) {
        std::lock_guard<std::mutex> lk(mu_);
        for (int v : values_) {
            fn(v);   // 回调里如果再次调用 forEach，会死锁
        }
    }
private:
    std::mutex mu_;
    std::vector<int> values_;
};
```

这个类是线程安全的（多线程调用不会数据竞争），但不可重入：回调里递归调用 `forEach`，同一线程再次锁同一把非递归 mutex，直接死锁。

修复思路：

- 把数据拷贝到锁外再回调（用快照换可重入）；
- 用递归锁（`std::recursive_mutex`，但通常说明设计有问题）；
- 明确契约：回调中禁止调用本对象方法，并在文档/断言中体现。

公众号后台的典型坑：连接关闭回调里又调用 `close()`，而 `close()` 持有同一把锁；或者指标回调里再次上报指标。**线程安全只保证并发不炸，不保证重入不炸。**

#### 3.3 逻辑原子性：read-modify-write 不是天然原子

`counter++` 的真实步骤：

```text
load  counter  -> 寄存器
add   1
store 寄存器   -> counter
```

两个线程交错：

```text
初始 counter = 0

线程 A: load  -> 0
线程 B: load  -> 0
线程 A: add   -> 1
线程 B: add   -> 1
线程 A: store -> 1
线程 B: store -> 1

结果：1，而不是 2
```

这就是「丢更新」。它可能是写写竞争，也可能是读写竞争（取决于两个线程的操作组合）。`counter++` 本身既不是原子读，也不是原子写，而是一个复合操作。

再看向公众号后台更真实的例子——**限流桶的 check-then-act**：

```cpp
class TokenBucket {
public:
    bool tryAcquire() {
        auto now = Clock::now();
        refill(now);                 // (1) 根据时间补充令牌
        if (tokens_ >= 1) {          // (2) 检查
            tokens_ -= 1;            // (3) 扣减
            return true;
        }
        return false;
    }

private:
    void refill(TimePoint now) {
        // 更新 last_、tokens_，涉及多个字段
    }

    double tokens_ = 0;
    TimePoint last_;
};
```

两个线程同时进入：

```text
初始 tokens_ = 1

A: refill -> tokens_ = 1
B: refill -> tokens_ = 1
A: tokens_ >= 1 -> 真
B: tokens_ >= 1 -> 真
A: tokens_ -= 1 -> 0，返回 true
B: tokens_ -= 1 -> -1，返回 true

结果：一个令牌被发了两次
```

这不是「某个变量不是原子」那么简单，而是**检查与扣减必须整体原子**。单独把 `tokens_` 换成 `std::atomic<double>` 也不够：`refill` 涉及 `last_` 和 `tokens_` 两个字段，`if` 和 `-=` 是两个步骤。

正确修复：

```cpp
class TokenBucket {
public:
    bool tryAcquire() {
        std::lock_guard<std::mutex> lk(mu_);
        refillLocked(Clock::now());
        if (tokens_ >= 1.0) {
            tokens_ -= 1.0;
            return true;
        }
        return false;
    }

private:
    void refillLocked(TimePoint now) {
        double elapsed = std::chrono::duration<double>(now - last_).count();
        tokens_ = std::min(capacity_, tokens_ + elapsed * rate_);
        last_ = now;
    }

    std::mutex mu_;
    double tokens_ = 0;
    double rate_ = 0;
    double capacity_ = 0;
    TimePoint last_ = Clock::now();
};
```

锁保护的不是一个字段，而是**「令牌数 + 上次补充时间 + 检查 + 扣减」这一组不变量**。这就是逻辑原子性。

#### 3.4 什么时候原子操作就够，什么时候必须锁

| 场景 | 够用的手段 | 原因 |
|------|------------|------|
| 单个计数器自增 | `std::atomic<long>` + `fetch_add` | 单个内存位置、单个复合操作可原子化 |
| 停止标志 | `std::atomic<bool>` | 单字布尔读写 |
| 发布只读快照 | `std::shared_ptr` 原子操作 / `atomic_load` | 指针与对象寿命需要配合 |
| 多字段不变量 | mutex | 原子操作不覆盖多个字段 |
| check-then-act | mutex（或 CAS 循环） | 检查与动作必须作为一个整体 |
| 遍历 + 修改容器 | mutex / 分片 / 快照 | 迭代器与结构修改不能靠单变量原子解决 |

一个常见误区是「把所有字段都换成原子变量，就没有竞争了」。这只能消除**单个字段上的数据竞争**，不能保证**业务不变量**。比如账户余额和冻结金额都用原子变量，但「总额守恒」仍然可能在两个原子操作之间被破坏。

#### 3.5 对象寿命：竞争之外的第二种不确定性

数据竞争讲的是「访问顺序」，对象寿命讲的是「访问是否还指向活对象」。两者经常同时出现：

```cpp
class Connection {
public:
    void onReadable() {
        // 使用 in_ / state_
    }
    ~Connection() {
        // 释放缓冲
    }
};

std::shared_ptr<Connection> g_conn;

// IO 线程
void ioLoop() {
    auto conn = g_conn;       // 拷贝 shared_ptr，延长寿命
    if (conn) conn->onReadable();
}

// 管理线程
void closeConn() {
    g_conn.reset();           // 释放最后一个引用，对象析构
}
```

如果 `g_conn` 的读写没有同步，`ioLoop` 可能读到正在被 `reset` 的 `shared_ptr`，这是数据竞争；即使 `shared_ptr` 的引用计数是原子的，**对 `g_conn` 这个 shared_ptr 对象本身的读写**仍然需要同步。正确做法：

```cpp
std::mutex g_mu;
std::shared_ptr<Connection> g_conn;

std::shared_ptr<Connection> snapshot() {
    std::lock_guard<std::mutex> lk(g_mu);
    return g_conn;            // 在锁内拷贝，引用计数 +1
}

void closeConn() {
    std::shared_ptr<Connection> old;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        old = std::move(g_conn);
    }
    // old 在锁外析构，避免在持锁时执行析构函数
}
```

这里有两个要点：

1. **快照延长寿命**：`snapshot()` 返回的 `shared_ptr` 保证调用期间对象活着；
2. **析构移出锁外**：避免在持锁时调用用户析构函数，防止死锁或长时间持锁。

如果对象本身还有可变状态，`shared_ptr` 只解决寿命，不解决状态竞争。你仍然需要对象内部的锁，或者更强的线程封闭契约。

#### 3.6 把三者串起来：一个会话表的正确契约

```cpp
struct Session {
    std::string openid;
    std::string token;
    std::chrono::steady_clock::time_point expireAt;
};

class SessionTable {
public:
    // 返回拷贝，避免把内部引用带出锁外
    std::optional<Session> find(const std::string& openid) const {
        std::lock_guard<std::mutex> lk(mu_);
        auto it = map_.find(openid);
        if (it == map_.end()) return std::nullopt;
        return it->second;          // 拷贝
    }

    // 复合操作：存在则更新，不存在则插入
    void upsert(const std::string& openid, Session s) {
        std::lock_guard<std::mutex> lk(mu_);
        map_[openid] = std::move(s);
    }

    // 逻辑原子性：检查过期并删除必须一起完成
    size_t purgeExpired(TimePoint now) {
        std::lock_guard<std::mutex> lk(mu_);
        size_t removed = 0;
        for (auto it = map_.begin(); it != map_.end(); ) {
            if (it->second.expireAt <= now) {
                it = map_.erase(it);
                ++removed;
            } else {
                ++it;
            }
        }
        return removed;
    }

private:
    mutable std::mutex mu_;
    std::unordered_map<std::string, Session> map_;
};
```

这个类的契约：

- 所有公开方法内部加锁；
- `find` 返回拷贝，调用方拿不到内部迭代器/引用；
- `upsert` 的「查找 + 插入/覆盖」在一个临界区；
- `purgeExpired` 的「检查 + 删除」在一个临界区，迭代器不会与并发修改冲突；
- 锁不跨越回调，避免重入死锁；
- 对象寿命由 `SessionTable` 持有，调用方只拿值。

如果要提高并发度，可以按 `openid` 哈希分片，每片一把锁；但分片后跨片的「总数统计」又变成新的复合操作，需要额外设计。**没有免费的原子性。**

---

## 常见误区与设计权衡

### 误区 1：`volatile` 能消除数据竞争

这是本课明确点名的坑：

> volatile不能消除数据竞争。

`volatile` 在 C++ 里的语义是「这个对象的读写有可观察的副作用，不要优化掉/不要合并」，主要用于内存映射 I/O、`sig_atomic_t` 与信号处理等场景。它**不建立 happens-before**，**不提供原子性**，**不保证跨线程可见性**，也**不阻止 CPU 重排**。

```cpp
volatile bool stop = false;   // 错误：不能作为线程停止标志

// 线程 A
while (!stop) { /* ... */ }

// 线程 B
stop = true;
```

这段代码仍然是数据竞争（一个读、一个写、无同步），仍然是未定义行为。正确写法：

```cpp
#include <atomic>

std::atomic<bool> stop{false};

// 线程 A
while (!stop.load(std::memory_order_relaxed)) { /* ... */ }

// 线程 B
stop.store(true, std::memory_order_relaxed);
```

如果停止标志还要与其它数据配合，使用 `acquire/release` 或直接上锁/条件变量。记住：**`volatile` 是给「编译器别动这块内存」用的，不是给「线程之间排队」用的。**

### 误区 2：只修「写写竞争」，忽略「读写竞争」

很多人看到两个线程都写才警觉；看到「一个读、一个写」会觉得读是安全的。实际上，只要冲突访问没有 happens-before，读的一方也在参与数据竞争。

典型：

```cpp
// 线程 A
connection->state = State::Closed;

// 线程 B
if (connection->state == State::Open) {
    connection->send(...);   // 读状态，但与写竞争
}
```

修复不是「读的时候多读几次」，而是让状态读写进入同一同步域：锁、原子变量、或线程封闭 + 消息传递。

### 误区 3：`counter++` 换成 `std::atomic` 就万事大吉

单个计数器可以：

```cpp
std::atomic<long> counter{0};
counter.fetch_add(1, std::memory_order_relaxed);
```

但如果业务是「先检查上限，再自增」：

```cpp
if (counter.load() < limit) {
    counter.fetch_add(1);   // 检查和自增之间可能被插队
}
```

这仍然是 check-then-act，可能超限。正确做法是 CAS 循环或锁：

```cpp
long expected = counter.load(std::memory_order_relaxed);
while (expected < limit) {
    if (counter.compare_exchange_weak(expected, expected + 1,
                                      std::memory_order_relaxed)) {
        break;
    }
}
```

或者直接用锁，把「检查 + 自增」放进临界区。**原子操作保证单个操作的原子性，不保证一组操作的逻辑原子性。**

### 误区 4：锁保护了数据，就等于线程安全

锁只能保护「所有访问都走同一把锁」的路径。常见漏洞：

- 一个方法加锁，另一个方法忘加；
- 返回内部引用/迭代器，调用方在锁外使用；
- 锁保护的字段和实际读写的字段不是同一组；
- 回调在持锁时执行，导致死锁或重入；
- 对象析构时没有等待正在使用它的线程。

线程安全是**接口级契约**，不是「某一行加了锁」。

### 误区 5：用 `sleep`、日志、重试来「修复」竞争

这些手段只改变时序：

- `sleep` 可能让竞争更难命中，也可能让它更容易命中；
- 日志会引入额外的函数调用和内存操作，改变优化与调度；
- 重试无法修复未定义行为造成的状态破坏。

正确顺序是：**先用 TSan/ASan 定位，再建立 happens-before，最后用压力测试验证**。

### 设计权衡：线程封闭 vs 共享加锁 vs 消息传递

| 方案 | 优点 | 代价 | 适用 |
|------|------|------|------|
| 线程封闭 | 无锁、缓存友好、逻辑简单 | 跨线程交互要投递任务；线程退出要处理残留 | 连接对象、每线程缓冲、事件循环 |
| 共享 + mutex | 语义直观、容易保证多字段不变量 | 竞争、上下文切换、死锁风险、持锁时间 | 会话表、配置、需要强一致的复合操作 |
| 共享 + 原子 | 无锁、单字段高效 | 只能保证单操作；复杂逻辑要 CAS；ABA 问题 | 计数器、标志位、无锁队列 |
| 分片锁 | 降低单锁竞争 | 跨片操作不原子；分片策略要稳定 | 大表、按 key 限流 |
| 消息传递 | 所有权清晰、易推理 | 队列成本、延迟、背压与关闭协议 | 跨线程状态变更、actor 风格 |

节点给的权衡是：

> 线程封闭减少同步成本，但可能需要消息传递。

在公众号后台，推荐的心智模型是：

```text
默认线程封闭（连接、缓冲、每线程统计）
  → 必须共享的，明确契约（锁 / 原子 / 分片）
  → 跨线程变更，优先消息传递而不是直接改对方状态
  → 所有共享状态都能回答：谁写、谁读、哪把锁、活到什么时候
```

---

## 动手实践

本实践的目标：**为计数器复现竞态，使用 TSan 并用锁修复，说明复合操作边界**。同时覆盖 acceptance 的两条：识别读写竞争、修复包含对象寿命与逻辑不变量。

### 环境与版本记录（先做）

```bash
g++ --version
clang++ --version 2>/dev/null || true
ldd --version | head -n 1
uname -a
```

记录：

- 编译器版本与标准库版本（libstdc++ / libc++）；
- 是否安装 TSan 运行时（`libtsan`）；
- 操作系统与内核版本；
- 编译使用的 C++ 标准（建议 `-std=c++20`，最低 `-std=c++17`）。

TSan 的可用性检查：

```bash
cat > /tmp/tsan_probe.cc <<'EOF'
#include <thread>
int main() { std::thread t([]{}); t.join(); }
EOF

g++ -std=c++20 -fsanitize=thread -pthread /tmp/tsan_probe.cc -o /tmp/tsan_probe \
  && /tmp/tsan_probe && echo "TSan OK"
```

如果链接失败，先安装对应运行库（例如 Debian/Ubuntu 的 `libtsan` 包），或改用 `clang++ -fsanitize=thread`。

### 步骤 1：准备可复现的计数器

```cpp
// race_counter.cc
#include <cstdio>
#include <thread>
#include <vector>

long counter = 0;

void worker(int iters) {
    for (int i = 0; i < iters; ++i) {
        ++counter;
    }
}

int main(int argc, char** argv) {
    int threads = (argc > 1) ? std::atoi(argv[1]) : 4;
    int iters = (argc > 2) ? std::atoi(argv[2]) : 1000000;

    std::vector<std::thread> pool;
    pool.reserve(threads);
    for (int i = 0; i < threads; ++i) {
        pool.emplace_back(worker, iters);
    }
    for (auto& t : pool) {
        t.join();
    }

    long expected = static_cast<long>(threads) * iters;
    std::printf("counter = %ld, expected = %ld, diff = %ld\n",
                counter, expected, expected - counter);
    return 0;
}
```

运行：

```bash
g++ -std=c++20 -O2 -pthread race_counter.cc -o race_counter
./race_counter 4 1000000
./race_counter 8 2000000
```

观察 `diff`。它可能为 0，也可能为正数。**多跑几次**，并尝试不同线程数与迭代数。不要因为一次为 0 就下结论。

### 步骤 2：用 TSan 复现并读报告

```bash
g++ -std=c++20 -O1 -g -fsanitize=thread -fno-omit-frame-pointer \
    -pthread race_counter.cc -o race_counter_tsan

TSAN_OPTIONS="halt_on_error=0 history_size=7 report_bugs=1" \
    ./race_counter_tsan 4 1000000
```

建议多跑几轮：

```bash
for i in $(seq 1 10); do
  TSAN_OPTIONS="halt_on_error=0" ./race_counter_tsan 4 200000 >/dev/null 2>>tsan.log || true
done
grep -c "WARNING: ThreadSanitizer: data race" tsan.log
```

报告解读练习：

1. 找到 `Write of size ...` 与 `Previous write of size ...`，确认冲突的是 `counter`；
2. 找到 `Location is global 'counter'`，确认是全局变量；
3. 找到两个线程的创建栈，确认它们来自 `main`；
4. 把报告里的行号映射回源码，标出 `++counter` 的 load/add/store 三步。

再做一个**读写竞争**版本，证明读也会参与数据竞争：

```cpp
// race_read_write.cc
#include <atomic>
#include <cstdio>
#include <thread>

long value = 0;
std::atomic<bool> start{false};

void writer() {
    while (!start.load(std::memory_order_relaxed)) {}
    value = 42;                       // 写
}

void reader() {
    while (!start.load(std::memory_order_relaxed)) {}
    std::printf("read = %ld\n", value);  // 读，与写冲突
}

int main() {
    std::thread a(writer);
    std::thread b(reader);
    start.store(true, std::memory_order_relaxed);
    a.join();
    b.join();
    return 0;
}
```

用 TSan 编译运行，报告会显示 `Read of size 8` 与 `Write of size 8`。这就是 acceptance 里「识别读写竞争而不只写写」的直接证据。

注意：这里用 `std::atomic<bool>` 只是为了让两个线程同时起跑；`value` 本身仍是非原子共享变量。不要误以为「有一个 atomic 就整体安全」。

### 步骤 3：用锁修复计数器

```cpp
// fixed_counter_mutex.cc
#include <cstdio>
#include <mutex>
#include <thread>
#include <vector>

class Counter {
public:
    void incr() {
        std::lock_guard<std::mutex> lk(mu_);
        ++value_;
    }

    long get() const {
        std::lock_guard<std::mutex> lk(mu_);
        return value_;
    }

private:
    mutable std::mutex mu_;
    long value_ = 0;
};

Counter counter;

void worker(int iters) {
    for (int i = 0; i < iters; ++i) {
        counter.incr();
    }
}

int main(int argc, char** argv) {
    int threads = (argc > 1) ? std::atoi(argv[1]) : 4;
    int iters = (argc > 2) ? std::atoi(argv[2]) : 1000000;

    std::vector<std::thread> pool;
    for (int i = 0; i < threads; ++i) {
        pool.emplace_back(worker, iters);
    }
    for (auto& t : pool) {
        t.join();
    }

    long expected = static_cast<long>(threads) * iters;
    std::printf("counter = %ld, expected = %ld, diff = %ld\n",
                counter.get(), expected, expected - counter.get());
    return 0;
}
```

验证：

```bash
g++ -std=c++20 -O1 -g -fsanitize=thread -fno-omit-frame-pointer \
    -pthread fixed_counter_mutex.cc -o fixed_counter_mutex_tsan

TSAN_OPTIONS="halt_on_error=1" ./fixed_counter_mutex_tsan 8 1000000
```

期望：`diff = 0`，且 TSan 没有任何 `data race` 报告。再用 `-O2` 跑普通构建，确认结果稳定为 0。

对照实验：把 `long` 换成 `std::atomic<long>` 并只用 `fetch_add`，TSan 也应该干净。但要说明边界：

```cpp
// 这个自增是原子的
std::atomic<long> n{0};
n.fetch_add(1, std::memory_order_relaxed);

// 但下面这个「检查 + 自增」不是逻辑原子的
if (n.load(std::memory_order_relaxed) < limit) {
    n.fetch_add(1, std::memory_order_relaxed);
}
```

### 步骤 4：复合操作边界实验

构造一个「上限计数器」：最多允许 `limit` 次成功，超过则拒绝。先写错误版本：

```cpp
// limit_race.cc
#include <atomic>
#include <cstdio>
#include <thread>
#include <vector>

constexpr long kLimit = 1000;
std::atomic<long> admitted{0};
long rejected = 0;

void worker(int iters) {
    for (int i = 0; i < iters; ++i) {
        if (admitted.load(std::memory_order_relaxed) < kLimit) {
            admitted.fetch_add(1, std::memory_order_relaxed);
        } else {
            ++rejected;   // 这里也有竞争
        }
    }
}

int main() {
    std::vector<std::thread> pool;
    for (int i = 0; i < 8; ++i) pool.emplace_back(worker, 100000);
    for (auto& t : pool) t.join();

    std::printf("admitted = %ld (limit %ld), rejected = %ld\n",
                admitted.load(), kLimit, rejected);
    return 0;
}
```

运行后观察：

- `admitted` 可能大于 `kLimit`，因为检查和自增之间被插队；
- `rejected` 本身无锁自增，TSan 会报另一个竞争。

用锁修复「检查 + 自增 + 拒绝计数」这一组逻辑：

```cpp
class Admission {
public:
    explicit Admission(long limit) : limit_(limit) {}

    bool tryAdmit() {
        std::lock_guard<std::mutex> lk(mu_);
        if (admitted_ < limit_) {
            ++admitted_;
            return true;
        }
        ++rejected_;
        return false;
    }

    long admitted() const {
        std::lock_guard<std::mutex> lk(mu_);
        return admitted_;
    }

    long rejected() const {
        std::lock_guard<std::mutex> lk(mu_);
        return rejected_;
    }

private:
    mutable std::mutex mu_;
    long limit_;
    long admitted_ = 0;
    long rejected_ = 0;
};
```

验证时不变量：

```text
admitted <= limit
admitted + rejected == 总尝试次数（如果每次调用只走一条路径）
```

这正是「逻辑原子性」：**多个字段和多个步骤必须一起变化**。你可以把 `admitted_` 做成原子变量，但 `admitted_ < limit_` 与 `++admitted_` 之间的窗口仍会破坏不变量。

进阶：用 CAS 实现无锁版本，并讨论 ABA、饥饿与可读性代价。

```cpp
bool tryAdmitLockFree() {
    long cur = admitted_.load(std::memory_order_relaxed);
    while (cur < limit_) {
        if (admitted_.compare_exchange_weak(
                cur, cur + 1,
                std::memory_order_relaxed,
                std::memory_order_relaxed)) {
            return true;
        }
    }
    rejected_.fetch_add(1, std::memory_order_relaxed);
    return false;
}
```

结论不是「无锁更好」，而是：**简单计数器可以无锁；一旦涉及多字段不变量、调试成本、可读性，锁往往更合适。**

### 步骤 5：对象寿命与逻辑不变量修复

写一个「连接注册表 + 快照」实验：

```cpp
// lifetime_race.cc
#include <cstdio>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>

struct Connection {
    int fd;
    ~Connection() { std::printf("connection %d destroyed\n", fd); }
};

std::mutex g_mu;
std::unordered_map<int, std::shared_ptr<Connection>> g_conns;

void ioWorker(int fd) {
    std::shared_ptr<Connection> conn;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        auto it = g_conns.find(fd);
        if (it != g_conns.end()) conn = it->second;  // 锁内快照
    }
    if (conn) {
        // 锁外使用；conn 保证对象活着
        std::printf("use connection %d\n", conn->fd);
    }
}

void closer(int fd) {
    std::shared_ptr<Connection> old;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        auto it = g_conns.find(fd);
        if (it != g_conns.end()) {
            old = std::move(it->second);
            g_conns.erase(it);
        }
    }
    // old 在锁外析构
}

int main() {
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_conns[1] = std::make_shared<Connection>(Connection{1});
    }

    std::thread a(ioWorker, 1);
    std::thread b(closer, 1);
    a.join();
    b.join();
    return 0;
}
```

用两种 sanitizer 分别验证：

```bash
# 数据竞争
g++ -std=c++20 -O1 -g -fsanitize=thread -pthread lifetime_race.cc -o lifetime_tsan

# 对象寿命（与 TSan 不能同时开）
g++ -std=c++20 -O1 -g -fsanitize=address -pthread lifetime_race.cc -o lifetime_asan

TSAN_OPTIONS="halt_on_error=1" ./lifetime_tsan
ASAN_OPTIONS="detect_leaks=1" ./lifetime_asan
```

然后做「错误版本」对照：把 `shared_ptr` 换成裸指针 `Connection*`，在 `closer` 里 `delete`，`ioWorker` 仍可能使用它。ASan 会报 heap-use-after-free；TSan 可能报指针读写竞争。**两种工具关注不同问题，不能互相替代。**

修复后的契约要写清楚：

1. 注册表的读写都在 `g_mu` 下；
2. 使用方拿 `shared_ptr` 快照，保证对象寿命；
3. 删除方把 `shared_ptr` 移出锁外析构，避免持锁执行析构；
4. 业务不变量「表里没有的 fd 不能再被使用」由「锁内查找 + 快照」共同保证；
5. 如果需要「关闭后不再处理新事件」，还要在 `Connection` 内部加状态机与同步，不能只靠注册表。

### 步骤 6：sourceStudy 核对清单

沿节点资源 `https://en.cppreference.com/w/cpp/language/multithread.html` 阅读，并记录：

- **定义**：找到 data race / conflicting actions / happens-before / synchronizes-with 的原文定义，抄下关键句并翻译。
- **示例**：核对页面中关于 `std::thread`、mutex、atomic 的示例，确认哪些操作建立 happens-before。
- **错误处理路径**：关注 `std::mutex::lock` 抛异常、`try_lock` 失败、`std::thread` 构造失败、`join` 前对象析构等边界。
- **适用版本**：记录 C++11 起的内存模型；`std::atomic`、`std::thread`、`std::mutex` 的最低标准版本；TSan 对编译器/标准库版本的要求。
- **边界条件**：`volatile` 与原子/同步的区别；原子操作与数据竞争的关系；`memory_order_relaxed` 不建立跨线程顺序；`detach` 不提供 join 的同步保证。
- **与本课实验的对应**：把页面定义映射到 `race_counter.cc`、`limit_race.cc`、`lifetime_race.cc` 的具体行，写明「哪两个访问冲突、缺少哪条 happens-before」。

记录模板：

```text
资源：
版本/访问日期：
定义摘录：
实验对应：
边界条件：
仍不确定的问题：
```

### 步骤 7：验收清单

完成以下检查才算通过：

- [ ] 普通构建能复现 `diff != 0`（至少一次），并记录线程数、迭代数、机器负载。
- [ ] TSan 能报出 `counter` 上的 data race，且报告包含两个访问栈。
- [ ] 读写竞争实验能报出 `Read of size ...` 与 `Write of size ...`，而不只是 write/write。
- [ ] 加锁修复后 TSan 干净，`diff == 0`，多次运行稳定。
- [ ] 能解释为什么 `std::atomic` 修好了计数器，却修不好 `check-then-act` 上限逻辑。
- [ ] 修复后的代码包含对象寿命处理（`shared_ptr` 快照 / 所有权转移）与逻辑不变量（`admitted <= limit`、`admitted + rejected == total`）。
- [ ] 能说清 `volatile` 为什么不能替代 `std::atomic` 或 mutex。
- [ ] 记录了编译器、标准库、TSan 版本，以及资源页面的适用版本与边界条件。

---

## 自测题

### 题目 1：`counter++` 是原子操作吗？

下面代码用 4 个线程各执行 100 万次 `++counter`，为什么结果可能小于 400 万？请说明 `counter++` 的步骤、属于哪种竞争，并给出两种修复方式。

```cpp
long counter = 0;

void worker() {
    for (int i = 0; i < 1000000; ++i) {
        ++counter;
    }
}
```

### 题目 2：`volatile bool stop` 能作为线程停止标志吗？

下面的代码有什么问题？请说明 `volatile` 的语义，并给出正确写法。

```cpp
volatile bool stop = false;

void loop() {
    while (!stop) {
        // do work
    }
}

void shutdown() {
    stop = true;
}
```

### 题目 3：线程安全和可重入有什么区别？

请分别解释「线程安全」和「可重入」，并构造一个「线程安全但不可重入」的例子。再说明在公众号后台的连接关闭回调里，为什么这个区别很重要。

---

## 自测题参考答案

### 答案 1

`counter++` 不是原子操作，它通常被拆成三步：

```text
load  counter -> 寄存器
add   1
store 寄存器 -> counter
```

多个线程的这三步可能交错。例如两个线程都读到 0，都加 1，都写回 1，最终结果为 1 而不是 2。这属于**读-改-写（read-modify-write）复合操作**导致的竞争；从访问组合看，既可能是写写竞争，也可能是读写竞争，本质是缺少同步导致的丢更新。更严格地说，`counter` 是非原子共享变量，多个线程无 happens-before 地访问且至少一个写，构成 C++ 数据竞争，导致未定义行为。

两种修复：

**方式一：mutex**

```cpp
std::mutex mu;
long counter = 0;

void worker() {
    for (int i = 0; i < 1000000; ++i) {
        std::lock_guard<std::mutex> lk(mu);
        ++counter;
    }
}
```

**方式二：`std::atomic`**

```cpp
std::atomic<long> counter{0};

void worker() {
    for (int i = 0; i < 1000000; ++i) {
        counter.fetch_add(1, std::memory_order_relaxed);
    }
}
```

补充：如果业务是「检查上限再自增」，仅用 `fetch_add` 不够，因为检查与自增不是一个原子操作，需要 CAS 循环或锁。

### 答案 2

不能。`volatile` 的语义是「这个对象的读写具有可观察副作用，编译器不要优化掉、不要合并」，它主要用于内存映射 I/O、信号处理等场景。它**不建立 happens-before**、**不提供原子性**、**不保证跨线程可见性**、**不阻止 CPU 重排**。

因此 `volatile bool stop` 仍然构成数据竞争（一个线程读、另一个线程写，且没有同步），仍然是未定义行为。所谓「加了 volatile 就能停」只是某些平台/编译器下的偶然现象。

正确写法：

```cpp
#include <atomic>

std::atomic<bool> stop{false};

void loop() {
    while (!stop.load(std::memory_order_relaxed)) {
        // do work
    }
}

void shutdown() {
    stop.store(true, std::memory_order_relaxed);
}
```

如果 `stop` 还要与其它数据配合（例如停止前发布一批结果），应使用 `release`/`acquire`，或用 mutex + 条件变量：

```cpp
std::mutex mu;
std::condition_variable cv;
bool stop = false;

void loop() {
    std::unique_lock<std::mutex> lk(mu);
    cv.wait(lk, [&] { return stop; });
}

void shutdown() {
    {
        std::lock_guard<std::mutex> lk(mu);
        stop = true;
    }
    cv.notify_all();
}
```

### 答案 3

- **线程安全**：多个线程同时调用对象的公开接口，不会破坏内部状态，不会产生数据竞争。通常通过内部加锁、原子操作或无共享状态实现。
- **可重入**：函数在执行过程中被再次调用（同一线程递归、信号处理、回调）仍然正确。通常要求不依赖共享可变状态，或不持有不可重入的锁。

「线程安全但不可重入」的例子：

```cpp
class Registry {
public:
    void forEach(const std::function<void(int)>& fn) {
        std::lock_guard<std::mutex> lk(mu_);
        for (int v : values_) {
            fn(v);   // 回调里再次调用 forEach -> 同一线程重复锁 mu_ -> 死锁
        }
    }

private:
    std::mutex mu_;
    std::vector<int> values_;
};
```

多线程调用 `forEach` 不会数据竞争（有锁），所以它是线程安全的；但回调中递归调用 `forEach` 会死锁，所以它不可重入。

在公众号后台的连接关闭回调里，这个区别很关键：关闭流程可能持有一把连接锁，然后调用用户回调；如果回调里又触发 `close()`、`send()`、指标上报，而它们也需要同一把锁，就会在同一线程上死锁。正确做法是：

- 明确契约：回调中禁止调用本对象方法；
- 或把回调移到锁外执行（先做快照，再回调）；
- 或使用消息传递，把回调变成投递到 IO 线程的任务，避免持锁重入。

---

## 推荐资源

- **数据竞争与线程安全契约：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/language/multithread.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 免费：是
  - 阅读重点：**共享与线程封闭**、**happens-before**、data race 的定义、`std::thread` 的同步语义、mutex 与原子操作如何建立同步关系。
  - 用法：对照本课的 `race_counter.cc`、`limit_race.cc`、`lifetime_race.cc`，把页面里的定义逐条映射到实验中的两个冲突访问；记录适用标准版本（C++11 起的 memory model）与边界条件（`volatile` 不参与同步、`relaxed` 不建立跨线程顺序、`detach` 不提供 join 的同步保证）。

建议阅读顺序：

1. 先读 data race / conflicting actions 的定义，写下判定清单；
2. 再读 happens-before / synchronizes-with，画出「线程创建 → 写 → unlock → lock → 读 → join」的箭头图；
3. 最后读 `std::thread`、`std::mutex`、`std::atomic` 的同步说明，核对本课示例；
4. 把仍不确定的边界条件记进 sourceStudy 模板，下一课带着问题验证。

---

## 本课小结

本课从「海量连接」转向「并发正确性」，核心只有一句话：

> 不同线程无同步地冲突访问同一内存且至少一个为写，会构成 C++ 数据竞争并导致未定义行为。

把这句话拆成可执行的规则：

1. **先分清共享与封闭**：栈变量、`thread_local`、对象封闭能消除大量竞争；必须共享的状态（会话表、限流桶、注册表、配置、指标）要写清契约。
2. **再问 happens-before**：线程创建/join、mutex 的 unlock/lock、原子 release/acquire、条件变量，都是建立可见性与顺序的手段；没有 happens-before 的冲突读写就是数据竞争。
3. **最后看逻辑原子性**：`counter++`、check-then-act、多字段不变量，都不是「换个原子变量」就能解决的；锁保护的是不变量，不是某一行代码。
4. **线程安全不等于可重入**：锁能保证并发不炸，但回调重入、持锁调用用户代码仍可能死锁；接口契约要写明。
5. **对象寿命是第二条战线**：`shared_ptr` 快照、所有权转移、锁外析构，能避免「竞争 + use-after-free」叠加。
6. **`volatile` 不是同步原语**：它不建立 happens-before、不提供原子性，不能消除数据竞争。
7. **工具要配套**：TSan 找数据竞争，ASan 找寿命问题，二者不能同时链接；TSan 只报实际发生的竞争，需要压力与多轮运行。
8. **修复要可验证**：TSan 干净、`diff == 0`、不变量成立、多次运行稳定，才算修完；不要用 `sleep`、日志、重试掩盖问题。

对公众号后台工程师来说，最重要的转变是：**看到「偶发错数/偶发崩溃」时，不要先怀疑业务逻辑，先画共享状态图**——谁写、谁读、哪把锁、对象活到什么时候、不变量是什么。把「偶发」变成「可检查的规则」，才是本课真正要带走的能力。

---

## 配套练习建议

1. **计数器三连**：分别用「无锁」「mutex」「atomic」实现同一个计数器，记录普通构建与 TSan 构建下的结果，写一页对比：性能、可读性、正确性边界。
2. **读写竞争专项**：构造「一个线程写结构体字段，另一个线程读同一字段」的例子，用 TSan 证明读也参与数据竞争；再分别用 mutex、atomic、线程封闭三种方式修复。
3. **限流桶改造**：把本课的 `TokenBucket` 改成「按用户分片 + 每片一把锁」，测量不同分片数下的吞吐；讨论跨片统计总数时如何保持逻辑原子性。
4. **会话表分片**：实现 `SessionTable` 的 16 片哈希版本，写压力测试：并发 `find` / `upsert` / `purgeExpired`；验证 `size()` 的统计口径，并说明它为什么可能只是一个近似值。
5. **对象寿命实验**：用裸指针、`shared_ptr`、`weak_ptr` 三种方式管理连接注册表，分别用 ASan 验证；记录每种方式的析构时机与回调安全性。
6. **可重入修复**：把 `Registry::forEach` 改成「锁内拷贝快照、锁外回调」，用递归回调测试不再死锁；再讨论快照带来的内存与一致性代价。
7. **TSan 报告归档**：收集至少 5 份不同类型的 TSan 报告（write/write、read/write、堆、全局、栈），每份写出「冲突内存、两个访问栈、缺少的 happens-before、修复方案」。
8. **sourceStudy 对照**：逐条抄写 cppreference 中 data race、happens-before、synchronizes-with 的定义，标注对应的 C++ 标准版本，并与自己的实验行号一一对应。
9. **代码审查清单**：为团队写一份「共享状态审查清单」：新增共享变量时必答的 6 个问题（谁写、谁读、哪把锁、寿命、不变量、回调重入），在下次 CR 中实际使用。
10. **压测复现**：在 2 核、4 核、8 核机器上分别跑竞争计数器，记录 `diff` 分布与 TSan 报告数量，讨论「为什么核数越多越容易暴露」以及「为什么不能靠增加迭代次数保证复现」。
