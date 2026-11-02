# 第 22/208 课：智能指针与共享所有权

> 领域 B · 阶段 1 · 子类：生命周期与所有权 · 难度：基础 · 前置：b-move（拷贝、移动与值类别）

一句话：`unique_ptr` 表达唯一拥有，`shared_ptr` 通过控制块管理共享拥有，`weak_ptr` 提供不延长寿命的观察。

## 一、学习目标与为什么后端要懂

学完这一课，你应该能做到：

1. 讲清控制块里有什么、引用计数怎么变化；
2. 独立完成节点实践：实现"异步回调持有连接"的例子，并修复循环引用和提前析构；
3. 分清 "shared_ptr 对象本身线程安全" 和 "被指对象线程安全" 是两回事；
4. 解释 `weak_ptr::lock()` 为什么会失败、失败时该怎么处理。

为什么后端要懂：节点给的理由是"明确异步任务、连接及缓存对象的释放责任"。后台服务里，一个请求往往跨好几个异步阶段：收包、调下游 RPC、等回包、写回客户端。连接对象该在什么时候释放？回调触发时连接是否还活着？缓存里的对象被淘汰时，正在使用它的请求怎么办？这些问题答错的结果是 use-after-free（随机崩溃）或内存泄漏（进程慢慢涨到被 OOM），而智能指针就是用类型把"谁负责释放"写清楚。

## 二、从直觉到原理

### 1. 先选对：unique_ptr 是默认，shared_ptr 是例外

直觉：一个对象只有一个主人时，用 `unique_ptr`；它只能移动不能拷贝（上一课的移动语义在这里直接用上），零额外开销。

```cpp
auto req = std::make_unique<Request>();
queue.push(std::move(req));   // 所有权转给队列，req 变成空
```

只有当"谁最后用完谁释放"在编译期确定不了时（比如多个异步回调都可能最后一个结束），才用 `shared_ptr`。

### 2. 控制块与引用计数

`shared_ptr` 内部是两个指针：一个指向对象，一个指向**控制块**。控制块里存：

- 强引用计数（有多少个 `shared_ptr` 拥有它）；
- 弱引用计数（有多少个 `weak_ptr` 在观察它）；
- 删除器（和分配器）。

规则：

- 拷贝 `shared_ptr`，强计数 +1；析构或 reset，强计数 -1；
- 强计数归零时，调用删除器销毁**对象**；
- 强计数和弱计数都归零时，才释放**控制块**本身。

```cpp
auto a = std::make_shared<Conn>(7);  // 强计数 1
auto b = a;                           // 2
std::weak_ptr<Conn> w = a;            // 强计数仍是 2，弱计数 +1
b.reset();                            // 1
a.reset();                            // 0 → Conn 被析构；w.expired() == true
```

`make_shared` 把对象和控制块放在一次分配里，少一次 malloc；代价是只要还有 `weak_ptr` 在，那块内存（包括对象占的空间）就不能归还。直接 `std::shared_ptr<T>(new T)` 则是两次分配。

计数的增减是原子操作，所以多线程里各自拷贝、各自析构 `shared_ptr` 是安全的。但原子操作不是免费的，这就是节点说的"计数开销"。

### 3. 循环引用与 weak_ptr

后台常见结构：`Session` 持有 `Connection`，`Connection` 又想知道自己属于哪个 `Session`。如果两边都用 `shared_ptr`：

```cpp
struct Conn    { std::shared_ptr<Session> session; };
struct Session { std::shared_ptr<Conn> conn; };

auto s = std::make_shared<Session>();
s->conn = std::make_shared<Conn>();
s->conn->session = s;   // s 的强计数变成 2
// 离开作用域：s 的计数 2→1，永远到不了 0，两个对象都泄漏
```

修复：把"反向"那条边改成 `weak_ptr`，它只观察、不拥有：

```cpp
struct Conn { std::weak_ptr<Session> session; };

if (auto s = conn->session.lock()) {   // 尝试升级成 shared_ptr
    s->onData(...);                    // 在 s 作用域内对象一定活着
} else {
    // Session 已经没了：丢弃、记日志、关闭连接
}
```

**`lock()` 为什么会失败**：它检查强计数，为 0 说明对象已经销毁，就返回空的 `shared_ptr`。这不是 bug，而是 `weak_ptr` 的本意：告诉你"对象已经不在了"。注意不要先 `expired()` 再 `lock()` 分两步判断，中间对象可能被别的线程释放；直接 `lock()` 并检查结果是原子的。

经验：画出对象之间的所有权图，强引用必须是一个**无环**的图（树或 DAG），反向边、父指针、观察者列表、缓存索引一律用 `weak_ptr` 或裸指针（在能保证寿命时）。

### 4. shared_from_this：异步回调里保活自己

最典型的提前析构：

```cpp
void Conn::asyncRead() {
    loop.post([this] { handle(fd); });   // 只捕获裸 this
}
// 回调还没执行，外部最后一个 shared_ptr 已释放 → Conn 析构
// 回调执行时访问 this → heap-use-after-free
```

修复：让类继承 `std::enable_shared_from_this<Conn>`，在回调里捕获一份 `shared_ptr`：

```cpp
struct Conn : std::enable_shared_from_this<Conn> {
    void asyncRead() {
        auto self = shared_from_this();
        loop.post([self] { self->handle(self->fd); });  // 回调持有所有权
    }
};
```

不能自己写 `std::shared_ptr<Conn>(this)`：那会新建第二个控制块，两个控制块各自归零时各删一次，就是 double free。`shared_from_this()` 的原理是基类里藏了一个 `weak_ptr`，在对象第一次交给 `shared_ptr` 管理时被设置，调用时对它 `lock()`，所以复用的是同一个控制块。

两个限制：

- 对象必须已经被某个 `shared_ptr` 拥有，在构造函数里调用不行；C++17 起没被拥有时调用会抛 `std::bad_weak_ptr`。
- 保活要有终点：回调要么执行要么被取消，否则 `self` 永远在队列里，连接永远不释放。

### 5. 删除器：管理不是 new 出来的资源

智能指针不只管内存，配合删除器可以管任何"需要释放"的东西，这就是上一课 RAII 的延伸：

```cpp
auto closer = [](FILE* f) { if (f) std::fclose(f); };
std::unique_ptr<FILE, decltype(closer)> fp(std::fopen("/tmp/a.log", "w"), closer);

std::shared_ptr<int> fd(new int(::open(...)), [](int* p) { ::close(*p); delete p; });
```

区别：`unique_ptr` 的删除器是**类型的一部分**（不同删除器是不同类型，但可以零开销）；`shared_ptr` 的删除器存在控制块里、类型被擦除，所以 `shared_ptr<T>` 不管用什么删除器都是同一个类型，更方便放进容器和接口里。

## 三、常见误区

1. **以为 shared_ptr 线程安全就万事大吉**（节点特别强调的坑）："同一控制块线程安全不意味着多个线程可无锁修改同一个 shared_ptr 实例。"
   - 安全：线程 A、B 各自持有自己的 `shared_ptr` 拷贝，各自拷贝、析构；
   - 不安全：两个线程同时对**同一个** `shared_ptr` 变量赋值或 reset（比如一个全局 `std::shared_ptr<Config> g_cfg` 被热更新线程写、工作线程读）。这是数据竞争，要用锁、C++20 的 `std::atomic<std::shared_ptr<T>>`，或旧标准的 `std::atomic_load/atomic_store` 重载；
   - 也不安全：多个线程无锁修改**被指对象**本身。shared_ptr 只保证计数安全，不保证 `Config` 的成员读写安全。
   这就是验收标准里的"区分 shared_ptr 对象安全与被指对象安全"。
2. **用裸 this 或引用捕获进异步回调**：回调执行时对象可能已析构。
3. **用同一个裸指针构造两个 shared_ptr**：两个控制块，double free。始终优先 `make_shared` / `make_unique`。
4. **在构造函数里调 shared_from_this()**：对象还没被 shared_ptr 拥有。
5. **处处 shared_ptr**：所有权变得模糊，谁都不知道对象什么时候死，排查泄漏非常痛苦。
6. **weak_ptr 先 expired() 再 lock()**：有竞态，直接 lock 并判空。

## 四、设计权衡

节点给的权衡是："共享所有权便捷，但计数开销与隐含寿命会增加复杂度。"

- **计数开销**：每次拷贝和析构都是原子增减，热路径上按值传 `shared_ptr` 很浪费。只是使用不需要保存时，传 `const T&` 或 `T*`；需要接管所有权时才按值传再 `std::move`。
- **隐含寿命**：对象什么时候释放取决于最后一个持有者，可能是某个排队很久的回调，导致析构发生在意想不到的线程、意想不到的时刻（例如析构里关闭 fd 发生在网络线程里）。
- 能用 `unique_ptr` 明确一个主人时就不要用 `shared_ptr`；必须共享时，把强引用图画出来，确认无环、有终点。

后台场景举例：

- 异步 RPC 回调持有连接：回调里捕获 `shared_from_this()`，保证回包到达时连接还在；连接被主动关闭时，用超时或取消机制让回调清掉，避免保活变泄漏。
- 本地缓存：缓存表持有 `shared_ptr<Item>`，正在处理的请求也拿一份拷贝；淘汰时缓存只是删掉自己那份，正在用的请求不受影响，用完自然释放。缓存的观察者或反向索引用 `weak_ptr`。

## 五、动手实践：异步回调持有连接，修复循环引用与提前析构

实践代码在附带的 `practice22/` 目录（`conn.cpp` 和 `test.sh`），已在 g++ C++17 + AddressSanitizer 下跑通。建议先自己写，再对照。

1. **搭一个极简事件循环**：一个 `std::queue<std::function<void()>>`，`post` 往里放回调，`run_loop()` 依次执行，模拟 epoll 回调或 RPC 回包的"稍后执行"。
2. **写 Connection**：构造和析构里增减一个全局存活计数并打印日志，方便观察寿命。
3. **重现提前析构**：`async_read_bad` 捕获裸 `this`，在作用域内创建连接、投递回调、离开作用域，再 `run_loop()`。用 `-fsanitize=address` 编译，应看到 `heap-use-after-free`。
4. **修复**：继承 `enable_shared_from_this`，改成捕获 `self = shared_from_this()`。检查投递后 `use_count() == 2`，外部释放后连接仍存活，回调执行完才析构。
5. **重现循环引用**：`SessionCycle` 和 `ConnCycle` 互相持有 `shared_ptr`，离开作用域后存活计数仍不为 0；开启 LeakSanitizer（`ASAN_OPTIONS=detect_leaks=1`）应报告 `detected memory leaks`。
6. **修复循环引用**：反向边改成 `weak_ptr`，确认 `use_count()` 为 1、离开作用域后两个对象都析构。
7. **验证 lock 失败**：保留一个外部 `weak_ptr`，对象释放后检查 `expired()` 为真、`lock()` 返回空。
8. **加分项**：用 `unique_ptr<FILE, 删除器>` 管理文件句柄，确认离开作用域自动 `fclose`。

运行：

```bash
cd practice22
./test.sh
# 期望最后一行：ALL_CHECKS_PASSED
```

验收（来自节点）：能区分 shared_ptr 对象安全与被指对象安全；能解释 weak_ptr.lock 失败。

## 六、自测题

**题 1**：下面代码最后 `Conn` 会被析构吗？为什么？

```cpp
auto a = std::make_shared<Conn>();
std::weak_ptr<Conn> w = a;
auto b = w.lock();
a.reset();
```

<details><summary>答案</summary>
不会。`lock()` 成功返回了一个新的 shared_ptr `b`，强计数为 2；`a.reset()` 后降到 1，`b` 仍拥有对象。只有 `b` 也释放后对象才析构。weak_ptr `w` 本身不影响对象寿命，只让控制块继续存在。
</details>

**题 2**：一个全局 `std::shared_ptr<Config> g_cfg`，配置热更新线程执行 `g_cfg = std::make_shared<Config>(...)`，多个工作线程执行 `auto c = g_cfg; c->get(...)`，不加锁。有问题吗？

<details><summary>答案</summary>
有。引用计数的原子性只保证"不同的 shared_ptr 实例共享同一个控制块"时安全；这里多个线程同时读写的是**同一个** `g_cfg` 实例，属于数据竞争。要加互斥锁，或用 C++20 的 `std::atomic<std::shared_ptr<Config>>`（旧标准用 `std::atomic_load/atomic_store`）。另外，如果 `Config` 发布后不再修改，工作线程读被指对象是安全的；若会被修改，被指对象也要自己保证线程安全。
</details>

**题 3**：为什么不能在 `Conn` 的成员函数里写 `std::shared_ptr<Conn>(this)` 来给回调保活？`shared_from_this()` 又是怎么避免问题的？

<details><summary>答案</summary>
`shared_ptr<Conn>(this)` 会新建一个独立的控制块，和外面已有的控制块各自计数，两边都归零时各删一次对象，造成 double free（或其中一边提前删掉对象造成 use-after-free）。`enable_shared_from_this` 在基类里保存一个 weak_ptr，对象第一次交给 shared_ptr 时被关联到同一个控制块，`shared_from_this()` 就是对它 lock，返回共享同一控制块的 shared_ptr。前提是对象已被 shared_ptr 拥有，否则 C++17 起抛 `std::bad_weak_ptr`。
</details>

## 七、推荐资源

- [cppreference：Memory management library](https://en.cppreference.com/w/cpp/memory.html)（英文，文档，基础，免费）：重点阅读控制块与引用计数、循环引用与 weak_ptr，用于核对本节点实验的语义和边界。可从这里进入 `unique_ptr`、`shared_ptr`、`weak_ptr`、`enable_shared_from_this` 各自的页面。

源码阅读建议（节点 sourceStudy）：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件，比如 `shared_from_this` 在 C++17 前后对"未被拥有"情况的不同行为。
