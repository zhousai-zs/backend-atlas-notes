# 第 67/208 课：条件变量与状态谓词

- **阶段**：第 2 阶段 · 并发与内存模型（同步与正确性）
- **前置课程**：e-lock
- **难度**：基础
- **所属领域**：E · 并发与内存模型（子类：同步与正确性）
- **标签**：Condition Variables、E、C++
- **关键词**：条件变量、condition_variable、wait、谓词、虚假唤醒、notify_one、notify_all、优雅停机、有界队列

---

## 导读

上一课讲的是**互斥锁与锁粒度**：互斥锁把临界区串行化，并在 `unlock` 与后续 `lock` 之间建立 happens-before。它解决的是「多个线程不能同时读写同一份共享状态」的问题。

但锁本身不会让线程「等到某个状态出现」。如果任务队列为空，消费者拿着锁反复检查队列，只有两条路：

1. **忙等**：`while (queue.empty()) {}`——占满 CPU，还可能忘记加锁；
2. **自己解锁后睡眠**：先 `unlock`，再 `sleep`，醒来再 `lock`——中间存在窗口，生产者可能恰好在这段时间修改状态并发出通知，消费者醒来后通知已经过去，于是永久睡死。

条件变量就是为这件事设计的原语。节点把它的核心语义总结得很精确：

> 条件变量用于等待共享状态变化，通知不是持久事件；谓词必须在互斥保护下反复检查。

这句话里有三个关键点，本课会逐一拆开：

- **等待共享状态变化**：条件变量本身不保存数据，它只是让线程在某个条件不满足时睡眠；
- **通知不是持久事件**：`notify_one` / `notify_all` 不会像计数器一样累积。通知发出时如果没有等待者，这次通知就丢失了；
- **谓词必须在互斥保护下反复检查**：因为虚假唤醒、因为通知可能丢失、因为状态可能在你醒来前又变了，所以正确写法永远是「拿着锁检查状态」，而不是「睡着等一个消息」。

在腾讯微信公众号后台，条件变量最常见的三个落点是：

- **任务队列**：生产者投递任务，worker 线程池从队列取任务；
- **worker 线程池**：空闲 worker 等待「有新任务」或「要停机」；
- **优雅停机**：进程收到退出信号后，关闭队列，唤醒所有仍在等待的 worker，让它们把手上任务处理完或安全退出。

这三个场景的共同失败模式是**丢唤醒**：生产者以为已经通知了，消费者却还在睡；或者停机时只 `notify_one`，其余 worker 永远等下去。本课的目标，是让你写出「通知可以丢，但状态不会丢」的队列。

本课与 e-lock 直接相连：条件变量永远要和一把互斥锁一起用；锁保护的是**谓词所依赖的共享状态**，条件变量负责的只是**睡眠与唤醒**。把这两件事混在一起，是并发 bug 的高发区。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 wait 的原子释放与重新获取**
   - 能说明 `condition_variable::wait` 在阻塞前会原子地释放互斥锁，被唤醒后再重新获取互斥锁。
   - 能解释为什么这个「原子释放 + 睡眠」的窗口必须由条件变量内部完成，手写 `unlock` + `sleep` 会丢唤醒。
   - 能说明为什么 `wait` 需要 `std::unique_lock<std::mutex>`，而不能直接用 `std::lock_guard`。
   - 能说清「锁保护谓词」和「条件变量负责等待」的分工。

2. **能解释谓词与虚假唤醒**
   - 能说明什么是虚假唤醒（spurious wakeup）：线程可能在没有人调用 `notify` 的情况下从 `wait` 返回。
   - 能写出 `while (!pred()) wait(lock);` 或等价的 `wait(lock, pred)`。
   - 能解释为什么 `if (!pred()) wait(lock);` 在虚假唤醒或状态再次变化时会出错。
   - 能说明谓词必须在持锁状态下读取共享状态，并且谓词本身不应做阻塞或重活。

3. **能解释通知、停止与超时**
   - 能区分 `notify_one` 与 `notify_all`，并说明各自适用场景。
   - 能解释「先通知后等待」为什么不能依赖：通知没有被保存，条件变量不是信号量。
   - 能设计 `closed_` 停止标志，让关闭操作唤醒所有等待的生产者和消费者。
   - 能正确使用 `wait_for` / `wait_until` 及其返回值：超时不是条件成立；有谓词的重载返回 `bool`，无谓词的重载返回 `cv_status`，且可能虚假返回。

4. **能独立完成实践**
   - 实现一个带关闭状态的**有界队列**：容量满时生产者等待，队列空时消费者等待。
   - 覆盖关闭时生产者与消费者都在等待的场景，保证关闭后所有等待线程最终返回。
   - 使用 `g++ -std=c++17 -pthread` 编译，并用 `-fsanitize=thread` 做数据竞争检查。
   - 沿 cppreference 的定义、示例与错误处理路径核对实现，记录适用版本及边界条件。

5. **满足本课验收标准（acceptance）**
   - **使用谓词循环处理唤醒**：所有 `wait` 要么带谓词，要么显式写成 `while` 循环；不存在裸 `wait`。
   - **关闭后所有等待线程最终返回**：生产者在关闭后 `push` 返回失败，消费者在关闭且队列排空后 `pop` 返回空，所有线程都能 `join`。

---

## 为什么后端要懂

节点给的 why 是：

> 实现无丢唤醒的任务队列和停机等待。

这句话落到公众号后台，就是下面几件事。

### 1. 任务队列不能忙等，也不能丢唤醒

一个最朴素的 worker 循环可能是：

```cpp
while (running_) {
    Task task;
    {
        std::lock_guard<std::mutex> lock(mu_);
        if (queue_.empty()) continue;   // 忙等：CPU 空转
        task = queue_.front();
        queue_.pop_front();
    }
    task.run();
}
```

这段代码在空闲时会把 CPU 打满，而且 `continue` 前释放锁、再重新拿锁，存在大量无意义竞争。改成「没任务就睡」之后，问题变成：

- 消费者怎么在「队列空」时睡眠？
- 生产者投递任务后怎么叫醒它？
- 如果消费者在生产者通知之后才进入睡眠，通知会不会保存？

答案就是条件变量加谓词：

```cpp
std::unique_lock<std::mutex> lock(mu_);
not_empty_.wait(lock, [this] {
    return closed_ || !queue_.empty();
});
```

即使生产者已经通知过、消费者才进入 `wait`，只要队列里有任务，谓词为真，`wait` 会立刻返回，不会睡死。

### 2. 停机等待需要「广播」

优雅停机时，通常会有多个 worker 同时阻塞在队列上。如果关闭时只调用 `notify_one`：

- 只有一个 worker 被唤醒；
- 其余 worker 继续睡，`join` 永远不返回；
- 进程退出流程卡住，表现为「停不干净」。

因此停机路径通常要用 `notify_all`，并且让谓词包含停止条件：

```cpp
not_empty_.wait(lock, [this] {
    return closed_ || !queue_.empty();
});
```

关闭后，所有等「非空」的消费者都会被唤醒，看到 `closed_ == true`，从 `pop` 返回；所有等「未满」的生产者也会被唤醒，看到 `closed_ == true`，从 `push` 返回失败。

### 3. 条件变量保护的是状态，不是「消息」

后端系统里经常把条件变量当消息通道用，写成：

```cpp
producer: cv.notify_one();
consumer: cv.wait(lock);   // 等一条“消息”
```

这是错的。条件变量没有内部队列，通知不会排队：

- 通知早于等待 → 通知丢失；
- 多次通知 → 不保证累积；
- 虚假唤醒 → 没通知也可能醒。

正确心智模型是：**共享状态是事实，通知只是提醒你去重新看事实**。事实写在 `queue_`、`closed_` 这些变量里，由互斥锁保护；条件变量只负责在你没看到事实时让你睡，在事实可能变化时把你叫醒。

### 4. 线上事故常见形态

值班时你可能会遇到：

- **任务积压但 worker 全在睡**：生产者 `notify_one` 时队列里已经有多个任务，但只有一个 worker 被叫醒；其余 worker 没有收到通知，直到下一次投递才可能醒来。
- **停机卡住**：关闭标志设置了，但只通知了一个等待者；或者关闭标志没有放进谓词，线程醒来后继续睡。
- **偶发丢任务**：消费者用 `if` 而不是 `while`，虚假唤醒后直接消费了不存在的任务；或者生产者没有在锁内修改状态，消费者检查谓词和进入等待之间出现窗口。
- **超时被当成成功**：`wait_for` 返回后不检查谓词，直接把空队列当成有任务。

这些问题都能用本课的「锁 + 谓词 + 循环 + 广播关闭」四件套解决。

### 5. 公众号后台你会真正碰到的场景

- **异步消息投递队列**：业务线程投递发送任务，多个 sender worker 消费；
- **本地批处理**：日志、指标、审计事件先入有界队列，后台线程批量落盘；
- **线程池任务队列**：线程池空闲时等待任务，关闭时等待所有 worker 退出；
- **配置/状态变更通知**：配置加载完成后唤醒等待者，但等待者仍要检查版本号或 `closed` 状态；
- **优雅停机**：先停止接收新任务，再关闭队列，再唤醒所有等待者，最后 `join`。

这些场景的共同要求是：**通知可以丢，状态不能错；唤醒可以多，谓词必须查**。

---

## 直觉 → 原理

### 0. 先建立直觉：取餐叫号与取餐屏

把条件变量想成餐厅的**取餐叫号**：

- 你点完餐，拿到一个号，然后等叫号；
- 店员喊「A17 取餐」只是一次**通知**，不是一条会保存的消息；
- 如果你在店员喊完之后才走到取餐口，你不会听到「刚才那声喊」，但你可以看**取餐屏**：如果屏上显示 A17 已就绪，你直接取餐；
- 如果屏上还没好，你就继续等下一次叫号；
- 有时候系统抽风，屏幕闪了一下把你叫醒，但餐还没好——你再确认屏幕，继续等。

对应到 C++：

- **取餐屏上的状态** = 受互斥锁保护的共享变量（`queue_`、`closed_`、`ready`）；
- **店员喊号** = `notify_one` / `notify_all`；
- **看屏幕再决定是否继续等** = 谓词循环；
- **走到取餐口的过程** = 获取互斥锁；
- **在等待区坐下** = `wait` 阻塞。

关键句：**叫号不保存，屏幕才保存**。所以永远根据屏幕（状态）判断，而不是根据「我有没有听到喊号」判断。

### 1. wait 原子释放与重新获取

#### 1.1 为什么不能忙等

```cpp
std::mutex mu;
std::deque<Task> queue;

void worker_busy() {
    while (true) {
        Task task;
        {
            std::lock_guard<std::mutex> lock(mu);
            if (queue.empty()) continue;   // 持续抢锁、空转
            task = std::move(queue.front());
            queue.pop_front();
        }
        task.run();
    }
}
```

问题：

- 空闲时 CPU 100%，白白消耗调度配额；
- 多个 worker 同时空转，会把互斥锁打得更热；
- 若再加一个 `closed` 标志，退出路径也很容易写错。

#### 1.2 为什么不能自己 unlock 后 sleep

一个看似合理的改法是：

```cpp
std::unique_lock<std::mutex> lock(mu);
while (queue.empty()) {
    lock.unlock();          // 先释放锁
    std::this_thread::sleep_for(10ms);  // 再睡
    lock.lock();            // 醒来重新加锁
}
```

这段代码有两个问题：

1. **丢唤醒窗口**：`unlock` 之后、真正睡眠之前，生产者可能已经投递任务并调用 `notify`。等消费者睡着后，通知已经过去，没人再叫它，只能等下一次投递。
2. **睡眠时间不可控**：只能轮询式地醒来，延迟和 CPU 开销都不理想。

条件变量的 `wait` 把「释放锁 + 进入等待」做成一个原子步骤，正是为了消除这个窗口。

#### 1.3 wait 的语义

对 `std::condition_variable` 来说，一次 `wait(lock)` 大致等价于：

```text
1. 断言当前线程持有 lock 关联的 mutex；
2. 原子地释放 mutex，并把当前线程挂到条件变量的等待队列上；
3. 阻塞，直到被 notify 或虚假唤醒；
4. 被唤醒后，重新获取 mutex（可能阻塞在这里）；
5. 返回，此时调用方重新持有锁。
```

「原子地释放并挂起」是核心。这里的原子性不是指「整个等待过程不会被打断」，而是指**释放锁与进入等待之间，不会给通知留下可乘之机**：

- 如果通知发生在释放锁之前，那么通知方必须持有同一把锁（或至少状态已经改变），等待方在检查谓词时能看到变化；
- 如果通知发生在等待方已经挂起之后，通知能把它叫醒；
- 不存在「状态已改、通知已发，但等待方还没挂起，于是永远睡下去」的窗口。

#### 1.4 为什么必须是 unique_lock

`std::condition_variable::wait` 的签名要求：

```cpp
template<class Predicate>
void wait(std::unique_lock<std::mutex>& lock, Predicate pred);
```

它需要一个能在等待期间**解锁、醒来后重新加锁**的锁对象。`std::lock_guard` 不支持手动 `unlock`，也不支持移动，因此不能用于 `wait`；`std::unique_lock` 支持 `lock` / `unlock` / `owns_lock`，正好满足要求。

```cpp
std::mutex mu;
std::condition_variable cv;
bool ready = false;

void consumer() {
    std::unique_lock<std::mutex> lock(mu);   // 正确：unique_lock
    cv.wait(lock, [&] { return ready; });
    // 此时重新持有 mu，且 ready == true
}
```

错误示例：

```cpp
std::lock_guard<std::mutex> lock(mu);
cv.wait(lock);   // 编译失败：wait 不接受 lock_guard
```

如果用的是 `std::condition_variable_any`，可以配合其他满足 BasicLockable 的锁类型，但本课统一使用 `std::condition_variable` + `std::unique_lock<std::mutex>`，这是后端代码中最常见的组合。

#### 1.5 锁保护的是谓词，不是条件变量

一个常见误解是「条件变量需要锁保护」。准确说法是：

- **共享状态**（队列、`closed_`、`ready`、版本号等）由互斥锁保护；
- **条件变量**负责等待和唤醒，它自己不保存业务状态；
- 修改共享状态时持锁，读谓词时也持锁，这样谓词读取不会与写发生数据竞争。

因此，生产者的正确顺序是：

```cpp
{
    std::lock_guard<std::mutex> lock(mu);
    queue.push_back(task);   // 在锁内改变谓词依赖的状态
}
cv.notify_one();             // 可以在锁外通知
```

注意：通知本身不要求持锁，但**状态修改必须在锁内完成**。如果通知放在锁内，被唤醒的线程会先醒来，再阻塞在 `lock()` 上，等通知方释放锁；这通常也能工作，但可能增加无谓的唤醒后等待。把 `notify` 放在解锁之后，是更常见的写法。

需要强调的是：状态修改和通知的先后关系不能反过来理解成「通知保存了状态」。真正让正确性成立的是锁 + 谓词，`notify` 只是加速唤醒。

### 2. 谓词与虚假唤醒

#### 2.1 什么是虚假唤醒

`condition_variable::wait` 允许在没有对应 `notify` 的情况下返回，这称为**虚假唤醒（spurious wakeup）**。标准允许它发生，底层实现也可能因为信号、调度或其他原因提前唤醒线程。

因此，下面写法是错的：

```cpp
std::unique_lock<std::mutex> lock(mu);
if (!queue.empty()) {
    // ...
}
if (queue.empty()) {
    cv.wait(lock);   // 被虚假唤醒后，直接继续执行，但队列仍然为空
}
auto task = queue.front();   // 未定义行为：空队列取 front
```

`wait` 返回不代表条件成立，只代表「你被叫醒了，应该重新检查」。

#### 2.2 while 循环

正确写法是把检查放进循环：

```cpp
std::unique_lock<std::mutex> lock(mu);
while (queue.empty() && !closed_) {
    not_empty_.wait(lock);
}

if (queue.empty()) {
    // closed_ 为 true，且队列已排空
    return std::nullopt;
}

auto task = std::move(queue.front());
queue.pop_front();
return task;
```

循环的含义：

1. 持锁检查谓词；
2. 谓词为假才进入 `wait`；
3. 被唤醒后重新持锁，再检查一次；
4. 如果谓词仍为假（虚假唤醒，或别的线程先一步消费了任务），继续等；
5. 谓词为真才继续执行业务逻辑。

#### 2.3 wait(lock, pred) 的等价形式

C++ 提供了带谓词的重载，语义等价于：

```cpp
while (!pred()) {
    wait(lock);
}
```

因此下面两种写法是等价的：

```cpp
std::unique_lock<std::mutex> lock(mu);

// 写法 A：显式循环
while (!closed_ && queue_.empty()) {
    not_empty_.wait(lock);
}

// 写法 B：谓词重载
not_empty_.wait(lock, [this] {
    return closed_ || !queue_.empty();
});
```

推荐写法 B：意图更集中，不容易漏掉某个条件。但无论用哪种，**谓词都必须读取受同一把锁保护的状态**。

#### 2.4 谓词里不要做重活

谓词会在持锁状态下被反复调用。下面这些做法都应该避免：

- 在谓词里打日志、格式化字符串；
- 在谓词里做 RPC、磁盘 IO、`sleep`；
- 在谓词里修改共享状态（谓词应当是只读判断）；
- 在谓词里获取另一把锁（容易引入锁顺序问题）。

谓词应该短小、只读、只依赖当前锁保护的状态。重活放到锁外，或放到谓词成立后的处理路径里。

#### 2.5 为什么「先通知后等待」不能靠通知本身兜底

考虑下面的消费者：

```cpp
std::unique_lock<std::mutex> lock(mu);
cv.wait(lock);   // 裸 wait，不检查任何状态
```

如果生产者在这个 `wait` 之前已经完成：

```cpp
{
    std::lock_guard<std::mutex> lock(mu);
    ready = true;
}
cv.notify_one();   // 此时没有等待者，通知被丢弃
```

那么消费者之后调用 `cv.wait(lock)` 时，通知已经过去，条件变量不会记得它。线程可能永远阻塞，除非碰巧发生虚假唤醒或后续又有人通知。

这就是节点所说的：

> 先通知后等待不代表通知会被保存。

修复方式不是「让通知保存」，而是**让谓词成为事实来源**：

```cpp
std::unique_lock<std::mutex> lock(mu);
cv.wait(lock, [&] { return ready; });
```

即使通知丢了，只要 `ready == true`，`wait` 在进入等待前检查谓词就会直接返回。通知可以丢，状态不会丢。

#### 2.6 为什么必须在同一把锁下修改状态

如果生产者这样写：

```cpp
ready = true;        // 没有持锁
cv.notify_one();     // 没有持锁
```

而消费者这样写：

```cpp
std::unique_lock<std::mutex> lock(mu);
if (!ready) {
    cv.wait(lock, [&] { return ready; });
}
```

仍然可能出问题：

- `ready` 的读写没有 happens-before，是数据竞争；
- 消费者检查 `ready == false` 后，生产者可能在消费者真正进入等待前修改 `ready` 并通知；虽然 `wait` 的原子释放会缩小窗口，但未受锁保护的写本身已经是未定义行为。

正确契约：**所有对谓词依赖状态的写，都必须与谓词读取使用同一把互斥锁**。这不是可选的性能优化，而是正确性前提。

### 3. 通知、停止与超时

#### 3.1 notify_one 与 notify_all

`std::condition_variable` 提供两个通知接口：

| 接口 | 行为 | 适用场景 |
|------|------|----------|
| `notify_one()` | 唤醒至少一个等待线程（具体哪一个由实现决定） | 一次状态变化只够一个等待者继续，例如「队列里多了一个任务」 |
| `notify_all()` | 唤醒所有等待线程 | 状态变化影响所有等待者，例如关闭队列、全局配置切换 |

几个重要语义：

- 通知不会排队，也不会计数。没有等待者时调用 `notify_one`，这次通知直接丢失；
- 多次通知不保证对应多次唤醒。正确性应依赖谓词，而不是通知次数；
- `notify_one` 唤醒的线程可能发现自己的谓词仍为假（例如它等的是另一种条件），于是重新睡下；
- 如果多个线程等待的是**不同的谓词**，却共用一个条件变量，`notify_one` 可能叫错人，导致真正该醒的线程继续睡。此时要么为不同谓词使用不同条件变量，要么使用 `notify_all`。

#### 3.2 停止标志 closed_

停机时，队列通常需要一个 `closed_` 标志。它必须参与所有等待谓词：

```cpp
bool push(Task task) {
    std::unique_lock<std::mutex> lock(mu_);
    not_full_.wait(lock, [this] {
        return closed_ || queue_.size() < capacity_;
    });
    if (closed_) return false;
    queue_.push_back(std::move(task));
    not_empty_.notify_one();
    return true;
}

std::optional<Task> pop() {
    std::unique_lock<std::mutex> lock(mu_);
    not_empty_.wait(lock, [this] {
        return closed_ || !queue_.empty();
    });
    if (queue_.empty()) return std::nullopt;
    Task task = std::move(queue_.front());
    queue_.pop_front();
    not_full_.notify_one();
    return task;
}
```

关闭操作：

```cpp
void close() {
    {
        std::lock_guard<std::mutex> lock(mu_);
        closed_ = true;
    }
    not_empty_.notify_all();
    not_full_.notify_all();
}
```

设计要点：

1. **关闭在锁内设置**：这样等待者要么在检查谓词前看到 `closed_`，要么已经挂起并会被后续通知唤醒；
2. **两个条件变量都要通知**：等「非空」的消费者和等「未满」的生产者都可能阻塞；
3. **使用 notify_all**：关闭影响所有等待者，不能只叫醒一个；
4. **关闭后消费策略要明确**：本课采用「关闭且队列排空后 `pop` 返回空」，这样已经入队的任务不会丢；`push` 在关闭后直接失败。

#### 3.3 超时等待

有时线程不能无限等待，例如：

- 定时批次需要在 100ms 内醒来检查一次；
- 优雅停机有总超时，超时后强制退出；
- 健康检查线程需要周期性上报。

C++ 提供：

```cpp
cv.wait_for(lock, duration);
cv.wait_for(lock, duration, pred);
cv.wait_until(lock, time_point);
cv.wait_until(lock, time_point, pred);
```

两类返回值的区别很重要：

- **无谓词版本**返回 `std::cv_status`：`std::cv_status::timeout` 或 `std::cv_status::no_timeout`。但 `no_timeout` 也可能由虚假唤醒产生，所以不能直接当成条件成立；
- **有谓词版本**返回 `bool`：返回谓词的最终结果。若因超时返回 `false`，说明时间到了但条件仍不成立；若返回 `true`，仍需按业务继续判断。

推荐写法：

```cpp
std::optional<Task> pop_for(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(mu_);

    const bool ready = not_empty_.wait_for(lock, timeout, [this] {
        return closed_ || !queue_.empty();
    });

    if (!ready) {
        return std::nullopt;   // 超时：条件仍未成立
    }
    if (queue_.empty()) {
        return std::nullopt;   // 已关闭且排空
    }

    Task task = std::move(queue_.front());
    queue_.pop_front();
    not_full_.notify_one();
    return task;
}
```

注意：

- `wait_for` 可能比指定时间晚返回，因为线程被唤醒后还要竞争锁、被调度；
- `wait_until` 受系统时钟影响时可能提前或延后；对间隔测量优先用 `std::chrono::steady_clock`；
- 超时返回 `false` 不代表队列关闭，也不代表永久失败，调用方要结合业务状态判断；
- 不要把 `cv_status::no_timeout` 直接当成「有任务」，必须重新检查谓词。

#### 3.4 完整示例：带关闭状态的有界队列

下面是一份可编译的完整实现和测试。它包含：

- 有界队列 `BoundedQueue`；
- `push` / `pop` / `pop_for` / `close`；
- 消费者空等、生产者满等、关闭广播；
- 容量为 0 的边界测试（用于让生产者和消费者同时处于等待状态）。

```cpp
// bounded_queue.cpp
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <iostream>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>

using namespace std::chrono_literals;

class BoundedQueue {
public:
    explicit BoundedQueue(std::size_t capacity)
        : capacity_(capacity) {}

    bool push(int value) {
        std::unique_lock<std::mutex> lock(mu_);
        not_full_.wait(lock, [this] {
            return closed_ || queue_.size() < capacity_;
        });
        if (closed_) {
            return false;
        }
        queue_.push_back(value);
        not_empty_.notify_one();
        return true;
    }

    std::optional<int> pop() {
        std::unique_lock<std::mutex> lock(mu_);
        not_empty_.wait(lock, [this] {
            return closed_ || !queue_.empty();
        });
        if (queue_.empty()) {
            return std::nullopt;
        }
        int value = queue_.front();
        queue_.pop_front();
        not_full_.notify_one();
        return value;
    }

    std::optional<int> pop_for(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mu_);
        const bool ready = not_empty_.wait_for(lock, timeout, [this] {
            return closed_ || !queue_.empty();
        });
        if (!ready || queue_.empty()) {
            return std::nullopt;
        }
        int value = queue_.front();
        queue_.pop_front();
        not_full_.notify_one();
        return value;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            closed_ = true;
        }
        not_empty_.notify_all();
        not_full_.notify_all();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(mu_);
        return queue_.size();
    }

    bool closed() const {
        std::lock_guard<std::mutex> lock(mu_);
        return closed_;
    }

private:
    const std::size_t capacity_;
    mutable std::mutex mu_;
    std::condition_variable not_empty_;
    std::condition_variable not_full_;
    std::deque<int> queue_;
    bool closed_ = false;
};

static void basic_test() {
    BoundedQueue queue(4);
    std::atomic<int> sum{0};

    std::vector<std::thread> consumers;
    for (int i = 0; i < 4; ++i) {
        consumers.emplace_back([&queue, &sum] {
            while (auto value = queue.pop()) {
                sum += *value;
            }
        });
    }

    for (int i = 1; i <= 1000; ++i) {
        while (!queue.push(i)) {
        }
    }

    queue.close();
    for (auto& thread : consumers) {
        thread.join();
    }

    std::cout << "basic: sum=" << sum.load()
              << " expected=500500 queue=" << queue.size() << "\n";
}

static void close_wakes_consumers() {
    BoundedQueue queue(8);
    std::atomic<int> returned{0};

    std::vector<std::thread> consumers;
    for (int i = 0; i < 8; ++i) {
        consumers.emplace_back([&queue, &returned] {
            auto value = queue.pop();
            if (!value) {
                ++returned;
            }
        });
    }

    std::this_thread::sleep_for(50ms);
    queue.close();

    for (auto& thread : consumers) {
        thread.join();
    }

    std::cout << "close consumers: returned=" << returned.load()
              << " expected=8\n";
}

static void close_wakes_producers() {
    BoundedQueue queue(1);
    (void)queue.push(42);   // 队列已满

    std::atomic<int> rejected{0};
    std::vector<std::thread> producers;
    for (int i = 0; i < 8; ++i) {
        producers.emplace_back([&queue, &rejected] {
            if (!queue.push(7)) {
                ++rejected;
            }
        });
    }

    std::this_thread::sleep_for(50ms);
    queue.close();

    for (auto& thread : producers) {
        thread.join();
    }

    std::cout << "close producers: rejected=" << rejected.load()
              << " expected=8\n";
}

static void zero_capacity_both_waiting() {
    BoundedQueue queue(0);

    std::thread producer([&queue] {
        (void)queue.push(1);
    });
    std::thread consumer([&queue] {
        (void)queue.pop();
    });

    std::this_thread::sleep_for(50ms);
    queue.close();

    producer.join();
    consumer.join();

    std::cout << "zero capacity: producer and consumer both returned\n";
}

static void timeout_test() {
    BoundedQueue queue(1);
    const auto start = std::chrono::steady_clock::now();
    auto value = queue.pop_for(50ms);
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - start);

    std::cout << "timeout: empty=" << (!value ? "yes" : "no")
              << " elapsed_ms=" << elapsed.count() << "\n";

    (void)queue.push(7);
    auto ready = queue.pop_for(50ms);
    std::cout << "timeout: ready=" << (ready ? *ready : -1) << "\n";
}

int main() {
    basic_test();
    close_wakes_consumers();
    close_wakes_producers();
    zero_capacity_both_waiting();
    timeout_test();
    return 0;
}
```

编译与运行：

```bash
g++ -std=c++17 -O2 -Wall -Wextra -pthread bounded_queue.cpp -o bounded_queue
timeout 20s ./bounded_queue
```

预期输出类似：

```text
basic: sum=500500 expected=500500 queue=0
close consumers: returned=8 expected=8
close producers: rejected=8 expected=8
zero capacity: producer and consumer both returned
timeout: empty=yes elapsed_ms=...
timeout: ready=7
```

再跑一次 ThreadSanitizer：

```bash
g++ -std=c++17 -O1 -g -fsanitize=thread -pthread bounded_queue.cpp -o bounded_queue_tsan
timeout 30s ./bounded_queue_tsan
```

需要说明：ThreadSanitizer 擅长发现数据竞争，但**不擅长发现丢唤醒、逻辑死锁、通知丢失**这类语义错误。条件变量的错误往往表现为「程序偶尔卡住」，因此还需要：

- 用 `timeout` 包住测试，让卡死变成可观测失败；
- 写关闭场景和满/空边界测试；
- 用 pressure / 反复运行放大竞态窗口；
- 在代码审查中强制检查「所有 wait 是否带谓词或 while」。

---

## 常见误区与设计权衡

### 误区 1：先通知后等待，通知会被保存

这是节点 pitfalls 的原话：

> 先通知后等待不代表通知会被保存。

条件变量不是信号量，也不是消息队列。它没有计数，没有缓冲区：

```cpp
// 生产者
{
    std::lock_guard<std::mutex> lock(mu);
    ready = true;
}
cv.notify_one();   // 没有等待者时，这次通知丢失

// 消费者（错误：裸 wait）
std::unique_lock<std::mutex> lock(mu);
cv.wait(lock);     // 可能永久阻塞，即使 ready 已经是 true
```

正确做法是让谓词检查状态：

```cpp
std::unique_lock<std::mutex> lock(mu);
cv.wait(lock, [&] { return ready; });
```

这样通知丢失也不会影响正确性：谓词为真时直接返回。

### 误区 2：用 if 而不是 while

```cpp
if (!queue_.empty()) {
    // ...
}
cv.wait(lock);   // 虚假唤醒后继续执行，队列可能仍为空
```

`wait` 返回只说明「被唤醒」，不说明「条件成立」。必须用 `while` 或带谓词的 `wait`。

### 误区 3：所有等待共用一个条件变量，却只 notify_one

如果生产者等待「队列未满」，消费者等待「队列非空」，两者共用一个 `cv`：

```cpp
cv.wait(lock, [&] { return !queue_.empty(); });   // 消费者
cv.wait(lock, [&] { return queue_.size() < cap; }); // 生产者
```

消费者消费一个任务后调用 `cv.notify_one()`，可能唤醒的是另一个生产者，而真正需要被唤醒的消费者仍在睡。虽然被错误唤醒的线程会检查谓词并重新睡下，但正确的那一方可能一直等不到通知。

解决方案：

- 为不同谓词使用不同条件变量（本课的 `not_empty_` 和 `not_full_`）；
- 或在状态变化影响多个等待者时使用 `notify_all`。

### 误区 4：关闭时只 notify_one

关闭是广播事件：

- 所有等「非空」的消费者都应该醒来并退出；
- 所有等「未满」的生产者都应该醒来并失败返回；
- 只唤醒一个，其余线程可能永远阻塞，`join` 卡死。

因此 `close()` 要同时 `not_empty_.notify_all()` 和 `not_full_.notify_all()`。

### 误区 5：关闭后仍然允许 push 成功

如果 `push` 的谓词只有「未满」，没有 `closed_`：

```cpp
not_full_.wait(lock, [this] {
    return queue_.size() < capacity_;   // 关闭后仍然可能为真
});
```

那么关闭后生产者仍可能往队列里塞任务，消费者已经退出，任务永远无人处理。正确做法是把 `closed_` 放进谓词，并在唤醒后优先检查：

```cpp
not_full_.wait(lock, [this] {
    return closed_ || queue_.size() < capacity_;
});
if (closed_) return false;
```

### 误区 6：等待超时后不检查谓词

```cpp
cv.wait_for(lock, 100ms);
if (queue_.empty()) {
    // 以为超时了，实际上可能只是虚假唤醒
}
```

无谓词版本的 `wait_for` 返回 `cv_status::no_timeout` 也可能来自虚假唤醒。要么使用带谓词的重载，要么在返回后重新检查状态。超时只表示「时间到了」，不表示「条件成立」或「条件不成立」。

### 误区 7：在谓词里做重活或加另一把锁

谓词在持锁状态下执行。如果在谓词里打日志、做 IO、获取第二把锁，会把临界区拉长，甚至引入死锁。谓词只做状态判断；复杂逻辑放到锁外。

### 误区 8：销毁时还有线程在等待

如果 `BoundedQueue` 析构时仍有线程阻塞在 `wait` 上，条件变量和互斥锁被销毁，行为未定义。停机顺序必须是：

1. 停止接收新任务；
2. `close()` 并唤醒所有等待者；
3. `join` 所有 worker；
4. 最后销毁队列、条件变量和互斥锁。

### 设计权衡：notify_one 减少惊群，notify_all 适合广播

节点 tradeoffs：

> notify_one 减少惊群，notify_all 适用于状态改变影响多个等待者的情况。

落地选择清单：

| 场景 | 推荐 | 原因 |
|------|------|------|
| 每放入一个任务，只需要一个消费者处理 | `notify_one` | 避免所有 worker 同时醒来抢锁（惊群） |
| 每次取出一个任务，只需要一个生产者继续投递 | `notify_one` | 一个空位只够一个生产者使用 |
| 关闭队列、停止服务 | `notify_all` | 所有等待的生产者和消费者都必须退出 |
| 配置整体切换、版本号变化影响所有等待者 | `notify_all` | 每个等待者都需要重新检查并可能继续 |
| 多个不同谓词共用一个条件变量 | 优先拆分条件变量；必要时 `notify_all` | `notify_one` 可能叫错等待者 |

工程建议：

1. **先保证正确性**：所有等待都有谓词，关闭用 `notify_all`，状态修改在锁内；
2. **再优化唤醒数量**：只有确认「一次状态变化只服务一个等待者」时，才使用 `notify_one`；
3. **用压力测试验证**：反复运行满/空/关闭边界，使用 `timeout` 捕获卡死；
4. **用 TSan 排除数据竞争**，但要记住它不能证明没有丢唤醒；
5. **为不同谓词拆分条件变量**：`not_empty_` 和 `not_full_` 分开，比共用一个 `cv` 更清晰。

---

## 动手实践

实践任务（节点 practice）：

> 实现带关闭状态的有界队列，覆盖关闭时生产者和消费者同时等待。

验收（acceptance）：

- 使用谓词循环处理唤醒；
- 关闭后所有等待线程最终返回。

并呼应 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

### 环境与版本记录（先做）

```bash
mkdir -p /tmp/atlas-e-condition && cd /tmp/atlas-e-condition
g++ --version
cat > versions.txt << EOF
date: $(date)
cxx: $(g++ --version | head -1)
std: c++17
threading: pthread
sanitizer: -fsanitize=thread
condition_variable: std::condition_variable + std::unique_lock<std::mutex>
EOF
```

打开并阅读（至少浏览定义、成员函数与示例）：

- https://en.cppreference.com/w/cpp/thread/condition_variable.html

重点核对：

- `wait` / `wait_for` / `wait_until` 的参数、返回值与异常；
- 带谓词重载的等价循环语义；
- `notify_one` / `notify_all` 的行为；
- 条件变量与互斥锁、`unique_lock` 的配合要求；
- 销毁、虚假唤醒、超时等边界条件。

把结论追加到 `versions.txt`，例如：

```text
notes:
- wait requires a locked std::unique_lock<std::mutex> for std::condition_variable
- wait atomically unlocks, blocks, then reacquires the mutex before returning
- wait(lock, pred) behaves like while (!pred()) wait(lock)
- notify without a waiter is not saved; condition_variable is not a counter
- notify_one wakes one waiter; notify_all wakes all waiters
- wait_for/wait_until without predicate may return no_timeout on spurious wakeup
- all waiting threads must be joined before destroying the queue
```

### 步骤 1：实现最小可用版本

先写一个只有 `push` / `pop` / `close` 的版本，要求：

- `push` 在队列满时等待；
- `pop` 在队列空时等待；
- `close` 设置 `closed_` 并 `notify_all`；
- 所有 `wait` 都使用谓词；
- 关闭后 `push` 返回 `false`，`pop` 在排空后返回 `std::nullopt`。

可以从本课完整示例中删除 `pop_for`、测试函数，只保留核心类，先确保单线程逻辑正确。

### 步骤 2：编写关闭场景测试

至少覆盖下面三类：

1. **消费者空等后关闭**：启动 N 个消费者，队列为空，它们阻塞在 `not_empty_`；调用 `close()` 后，所有消费者都应返回 `nullopt`，`join` 成功；
2. **生产者满等后关闭**：把容量设为 1，先放入一个元素，再启动 N 个生产者，它们阻塞在 `not_full_`；调用 `close()` 后，所有生产者都应返回 `false`，`join` 成功；
3. **生产者和消费者同时等待**：使用容量 0 的边界配置，生产者因「无法满足 `size < 0`」而等待，消费者因「队列为空」而等待；调用 `close()` 后，两边都应返回。容量 0 只用于测试双方同时等待，不是生产环境推荐配置。

运行方式：

```bash
g++ -std=c++17 -O2 -Wall -Wextra -pthread bounded_queue.cpp -o bounded_queue
timeout 20s ./bounded_queue
```

如果程序卡住，`timeout` 会返回非零退出码，这比无限等待更容易定位问题。

### 步骤 3：故意制造裸 wait，观察问题

把 `pop` 中的谓词等待改成裸 `wait`：

```cpp
// 错误版本，仅用于实验
std::unique_lock<std::mutex> lock(mu_);
not_empty_.wait(lock);
if (queue_.empty()) {
    return std::nullopt;
}
```

然后构造「先通知、后等待」的测试：

```cpp
BoundedQueue queue(4);
{
    // 模拟生产者先完成
    std::thread producer([&] { (void)queue.push(1); });
    producer.join();
}
// 此时通知已经发生过，但消费者还没开始等待
std::thread consumer([&] {
    auto value = queue.pop();   // 裸 wait 可能永久阻塞
});
```

用 `timeout` 运行，观察卡死。然后把 `pop` 改回谓词版本，确认同样场景能立即取到值。

这个实验的目的不是让你记住「不要裸 wait」这句话，而是亲眼看到：**通知不是持久事件，状态才是**。

### 步骤 4：TSan 与压力测试

```bash
g++ -std=c++17 -O1 -g -fsanitize=thread -pthread bounded_queue.cpp -o bounded_queue_tsan
timeout 30s ./bounded_queue_tsan
```

再写一个压力循环：

```bash
for i in $(seq 1 50); do
    timeout 10s ./bounded_queue > /dev/null || {
        echo "run $i failed";
        exit 1;
    }
done
echo "stress ok"
```

自检清单：

- [ ] 所有 `wait` 都带谓词，或显式 `while` 循环；
- [ ] `close()` 同时通知 `not_empty_` 和 `not_full_`；
- [ ] 关闭后 `push` 不会继续入队；
- [ ] 关闭后消费者会排空已有任务，再返回空；
- [ ] 所有线程在条件变量销毁前 `join`；
- [ ] TSan 无 data race 报告；
- [ ] 反复运行不出现卡死。

### 步骤 5：超时与返回值实验

实现或保留 `pop_for`，验证：

1. 空队列 + 短超时：返回空，耗时不少于超时时间；
2. 有任务时：立即返回任务；
3. 关闭且空队列：立即返回空；
4. 关闭但队列仍有任务：先返回任务，排空后返回空。

注意区分「超时」和「关闭」：本课的简化实现都返回 `std::nullopt`。生产代码中如果需要区分，可以返回更丰富的结构：

```cpp
struct PopResult {
    enum class Status { Ok, Timeout, Closed };
    Status status;
    std::optional<int> value;
};
```

这也是一个设计权衡：返回值越精确，调用方分支越多；返回值越简单，调用方越容易误判。

### 步骤 6：sourceStudy 核对清单

对照 https://en.cppreference.com/w/cpp/thread/condition_variable.html 逐项打勾：

1. [ ] `condition_variable` 的定义与头文件；
2. [ ] `wait` 的原子释放/重新获取语义；
3. [ ] `wait(lock, pred)` 与 `while (!pred()) wait(lock)` 的等价关系；
4. [ ] `wait_for` / `wait_until` 的参数、`cv_status` 与 `bool` 返回值；
5. [ ] `notify_one` / `notify_all` 的行为与「无等待者时通知丢失」；
6. [ ] 对锁对象的要求：`std::condition_variable` 配合 `std::unique_lock<std::mutex>`；
7. [ ] 虚假唤醒的允许性，以及为什么必须用谓词循环；
8. [ ] 异常路径：`wait` 可能抛出 `std::system_error`，谓词抛出的异常会向外传播；
9. [ ] 销毁条件变量时仍有等待线程属于未定义行为；
10. [ ] 记录你的 `g++` 版本、`-std=c++17`、libstdc++ 实现，以及你实际验证过的边界条件。

把「适用版本」写清楚：

```text
适用版本:
- 标准: C++11 起提供 std::condition_variable
- 本课编译: g++ -std=c++17 -pthread
- 未使用 C++20 专属接口
- 所有结论均在上述编译选项与本机 libstdc++ 下验证
```

把「边界条件」也写清楚：

```text
边界条件:
- 没有等待者时 notify 丢失
- 虚假唤醒可能发生，必须重新检查谓词
- wait_for 可能晚于超时返回
- 关闭后 push 失败，pop 排空后返回空
- 销毁前必须 join 所有等待线程
```

### 步骤 7：验收清单

- [ ] 有界队列实现完整，`push` / `pop` / `close` 均可用；
- [ ] 所有等待都使用谓词循环；
- [ ] 消费者空等 + 关闭：所有消费者返回并 `join`；
- [ ] 生产者满等 + 关闭：所有生产者返回失败并 `join`；
- [ ] 生产者和消费者同时等待的场景有测试；
- [ ] 关闭后所有等待线程最终返回，`timeout` 不会触发；
- [ ] TSan 运行通过；
- [ ] sourceStudy 笔记包含版本与边界条件；
- [ ] 能用自己的话解释「通知不是持久事件」和「谓词才是事实来源」。

---

## 自测题

### 题目 1：为什么 wait 必须使用 unique_lock？

请解释：

1. `condition_variable::wait` 在等待期间对互斥锁做了什么？
2. 为什么手写 `unlock` + `sleep` 会丢唤醒？
3. 为什么 `lock_guard` 不能用于 `wait`？

### 题目 2：虚假唤醒与谓词循环

下面的代码有什么问题？请给出两种正确写法。

```cpp
std::unique_lock<std::mutex> lock(mu);
if (queue.empty()) {
    not_empty_.wait(lock);
}
auto task = queue.front();
queue.pop_front();
```

如果 `not_empty_.wait(lock)` 在没有通知的情况下返回，会发生什么？为什么 `wait(lock, pred)` 能解决？

### 题目 3：notify_one、notify_all 与停机

某服务有一个有界任务队列，多个 worker 在队列为空时等待。关闭服务时，代码只做了：

```cpp
{
    std::lock_guard<std::mutex> lock(mu);
    closed_ = true;
}
not_empty_.notify_one();
```

请回答：

1. 这段关闭代码可能导致什么问题？
2. 关闭时生产者和消费者都可能等待，应该怎样通知？
3. 如果生产者只投递了一个任务，应该用 `notify_one` 还是 `notify_all`？为什么？
4. `wait_for` 返回后，为什么不能直接把 `cv_status::no_timeout` 当成条件成立？

---

## 自测题参考答案

### 答案 1

1. `wait` 会原子地释放 `unique_lock` 持有的互斥锁，把当前线程挂到条件变量的等待队列上并阻塞；被通知或虚假唤醒后，它会重新获取互斥锁，然后才从 `wait` 返回。这个「释放 + 阻塞」的原子性消除了「解锁后、睡着前」的丢唤醒窗口。
2. 手写 `unlock` + `sleep` 时，生产者可能正好在「已经解锁、还没睡着」的窗口里修改状态并发出通知。等消费者真正睡着后，通知已经过去，且条件变量不保存通知，消费者只能等下一次通知，可能永久阻塞。而且轮询式睡眠还会带来延迟和 CPU 开销。
3. `lock_guard` 只提供构造加锁、析构解锁，不支持在等待期间手动 `unlock` / `lock`，也不可移动；`wait` 需要能够临时解锁并重新加锁的锁对象，所以必须使用 `std::unique_lock`（或 `condition_variable_any` 配合其他满足要求的锁类型）。

### 答案 2

问题：

- `wait` 可能虚假唤醒，返回时队列仍然为空；
- 即使不是虚假唤醒，其他消费者也可能先一步取走任务，导致本线程醒来后队列为空；
- 随后 `queue.front()` 对空队列调用是未定义行为；
- 另外，`wait` 返回后没有重新检查关闭状态。

正确写法 A：显式循环。

```cpp
std::unique_lock<std::mutex> lock(mu);
while (queue.empty() && !closed_) {
    not_empty_.wait(lock);
}
if (queue.empty()) {
    return std::nullopt;
}
auto task = std::move(queue.front());
queue.pop_front();
```

正确写法 B：带谓词。

```cpp
std::unique_lock<std::mutex> lock(mu);
not_empty_.wait(lock, [this] {
    return closed_ || !queue_.empty();
});
if (queue.empty()) {
    return std::nullopt;
}
auto task = std::move(queue_.front());
queue_.pop_front();
```

`wait(lock, pred)` 等价于 `while (!pred()) wait(lock);`。它会在进入等待前检查谓词，在每次被唤醒后重新检查，只有谓词为真才返回，因此能同时处理虚假唤醒、通知丢失和其他线程抢先消费的情况。

### 答案 3

1. 只调用 `not_empty_.notify_one()` 只会唤醒一个消费者。其余 worker 可能永远阻塞在 `wait` 上，导致 `join` 卡住、进程无法优雅退出。如果还有生产者等待「队列未满」，它们也完全收不到通知。
2. 关闭是影响所有等待者的状态变化，应该：
   - 在锁内设置 `closed_ = true`；
   - 锁外调用 `not_empty_.notify_all()` 和 `not_full_.notify_all()`；
   - 所有等待谓词都包含 `closed_`，保证被唤醒后能看到关闭状态并返回。
3. 如果只投递一个任务，通常用 `notify_one`：一次状态变化只够一个消费者处理，`notify_one` 可以避免所有 worker 同时醒来抢锁（惊群）。但前提是消费者等待的是同一个「非空」谓词；如果同一个条件变量上还混有别的谓词，可能需要拆分条件变量或使用 `notify_all`。
4. `cv_status::no_timeout` 只表示这次等待不是因为超时返回，它可能来自虚假唤醒，也可能来自一次与该线程谓词无关的通知。条件是否成立必须重新检查谓词。带谓词的 `wait_for` 返回 `bool`，更不容易误用；无谓词版本应写成循环并在返回后检查状态。

---

## 推荐资源

- **条件变量与状态谓词：参考文档 / 原始资料**  
  https://en.cppreference.com/w/cpp/thread/condition_variable.html  
  - 来源：en.cppreference.com  
  - 语言：英文 · 类型：文档 · 难度：进阶 · 免费  
  - 阅读重点：`wait` 原子释放与重新获取、谓词与虚假唤醒，用于核对本节点实验的语义和边界。  
  - 建议把页面中 `wait` / `wait_for` / `wait_until` / `notify_one` / `notify_all` 的说明与示例抄进 `versions.txt`，并记录你使用的标准版本与边界条件。

---

## 本课小结

1. **条件变量用于等待共享状态变化**，它不保存业务状态；共享状态由互斥锁保护，条件变量只负责睡眠与唤醒。
2. **`wait` 原子地释放锁、阻塞、被唤醒后重新获取锁**；这个原子性消除了「解锁后、睡着前」的丢唤醒窗口，也是必须使用 `std::unique_lock` 的原因。
3. **通知不是持久事件**：没有等待者时 `notify` 会丢失；不要把条件变量当信号量或消息队列。正确性来自谓词，而不是通知次数。
4. **谓词必须在锁下反复检查**：使用 `while (!pred()) wait(lock)` 或 `wait(lock, pred)`，不要用 `if`，也不要写裸 `wait`。
5. **关闭是广播事件**：`closed_` 必须进入所有等待谓词；关闭时同时通知「非空」和「未满」两个条件变量，并使用 `notify_all` 唤醒所有等待者。
6. **`notify_one` 减少惊群，`notify_all` 适用于状态变化影响多个等待者**；选择依据是「一次状态变化能服务几个等待者」，而不是盲目追求唤醒数量。
7. **超时不是条件成立**：`wait_for` / `wait_until` 返回后仍要检查谓词；无谓词版本可能因虚假唤醒返回 `no_timeout`。
8. **验证条件变量代码不能只靠 TSan**：TSan 查数据竞争，丢唤醒和逻辑死锁要靠谓词审查、关闭/满/空边界测试、压力运行和 `timeout` 兜底。
9. 下一课会继续沿着同步原语扩展；本课的「锁保护状态 + 谓词循环 + 通知只是提醒」这套模型，是任务队列、线程池和优雅停机的基础。
