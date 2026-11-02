# 第 31/208 课：C++20无栈协程机制

> 节点：`b-coroutine`｜领域：B｜阶段：3｜难度：进阶  
> 前置：`b-move`、`b-lifetime`｜标签：C++20 Coroutines、B、C++

## 学习目标

学完本课，你应当能够：

1. 解释协程帧里有什么、帧由谁拥有、什么时候销毁，以及为什么必须保证“每个协程帧恰好销毁一次”。
2. 看懂 `promise_type` 与 awaitable 的协议，知道 `co_await` 何时真正暂停、何时直接继续。
3. 解释 `initial_suspend`、`final_suspend` 与异常传播的关系，并能实现一个仅用于教学的 `Task` 和手动恢复器。
4. 识别“协程不等于线程/调度器”的边界，避免临时 Lambda 捕获、重复销毁、恢复已结束协程等工程问题。

## 为什么后端要懂

腾讯微信公众号后台的请求处理经常是：解析请求，等待 Redis/MySQL/RPC，再组装响应。C++20 协程让这些异步流程可以写成接近同步代码的形式：

```cpp
Task handle_request(Request req) {
    auto user = co_await load_user(req.user_id);
    auto messages = co_await load_messages(user.id);
    send_response(build_response(user, messages));
    co_return;
}
```

但语言层只提供“暂停/恢复”和 `promise` 协议。事件循环、I/O 注册、线程池、超时、取消、所有权，都是库或业务框架负责的。把语言协程误当成“自动并发运行时”，就会出现严重误判：

- 以为 `co_await` 一定切换线程，结果在事件循环线程里执行了阻塞操作。
- 协程已经销毁，I/O 回调还持有它的句柄，恢复时访问已释放的帧。
- 请求对象或 Lambda 捕获先于协程结束而销毁，协程恢复后读到悬空引用。
- 同一个帧被析构两次，或者对已经结束的协程再次 `resume()`。

节点 `b-coroutine` 的定位正是：**读懂异步库，并避免把语言协程误当成自动并发运行时**。

---

## 1. 协程帧与生命周期

### 直觉：函数多了一个“书签”

普通函数调用时，参数、局部变量和返回地址放在线程栈上；函数返回，栈帧消失。协程允许函数在中间暂停，并在之后从暂停点继续，所以它必须把“暂停后还需要的状态”保存到另一个地方。这个保存状态的对象就是**协程帧**（coroutine frame）。

可以把协程帧想成一本书：协程体是书的内容，暂停点是一枚书签。线程栈不是这本书；协程暂停后，线程可以去做别的事，书仍然放在那里。

### 原理：编译器把函数改造成状态机

一个包含 `co_await`、`co_yield` 或 `co_return` 的函数会被编译器改造成协程。编译器的核心动作是：

1. 在调用时创建协程帧。
2. 在帧里保存 `promise` 对象、参数副本、跨暂停点仍需使用的局部变量/临时值，以及恢复和销毁所需的控制信息。
3. 把函数体改造成带暂停/恢复入口的状态机。
4. 通过 `promise_type` 决定首次是否暂停、结束是否暂停、如何返回结果、异常如何处理。

“无栈”指协程不拥有独立的线程栈，也不需要像栈式协程那样切换整个栈；它只保存必要的状态。代价是：**不能在普通函数内部随便暂停**，暂停点必须在协程函数体内，并且异步调用链通常需要把协程一路传下去。

协程帧通常分配在堆上，编译器在能证明安全时可以做分配消除；标准不规定具体的存储位置，但帧必须独立于当前线程栈存活。这也是“暂停后线程可以离开”的前提。

### 帧由谁拥有

`std::coroutine_handle` 只是指向协程帧的**非拥有句柄**，类似裸指针。复制 handle 不会复制帧，也不会自动销毁帧。常见做法是让一个 RAII 类型持有唯一句柄，比如教学用的 `Task`：

- `Task` 可移动、不可复制；
- 移动时转移句柄；
- 析构时如果句柄有效，调用一次 `destroy()`；
- `destroy()` 之后把句柄置空，避免重复销毁。

如果 `final_suspend` 返回 `std::suspend_never{}`，协程结束时运行时会自动销毁帧，此时再手动 `destroy()` 就是错误的。教学 `Task` 使用 `final_suspend` 返回 `suspend_always`，让“谁拥有、谁销毁”变得明确。

### 短代码：观察帧的创建、暂停和销毁

```cpp
#include <coroutine>
#include <exception>
#include <iostream>
#include <utility>

struct MiniTask {
    struct promise_type {
        promise_type() { std::cout << "[promise ctor]\n"; }
        ~promise_type() { std::cout << "[promise dtor]\n"; }

        MiniTask get_return_object() {
            return MiniTask{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() noexcept { std::terminate(); }
    };

    explicit MiniTask(std::coroutine_handle<promise_type> handle) noexcept : h(handle) {}
    MiniTask(MiniTask&& other) noexcept : h(std::exchange(other.h, nullptr)) {}
    MiniTask& operator=(MiniTask&& other) noexcept {
        if (this != &other) {
            if (h) h.destroy();
            h = std::exchange(other.h, nullptr);
        }
        return *this;
    }
    MiniTask(const MiniTask&) = delete;
    MiniTask& operator=(const MiniTask&) = delete;
    ~MiniTask() { if (h) h.destroy(); }

    std::coroutine_handle<promise_type> h{};
};

struct Probe {
    ~Probe() { std::cout << "[local dtor]\n"; }
};

MiniTask demo() {
    Probe p;
    std::cout << "[body] A\n";
    co_await std::suspend_always{};
    std::cout << "[body] B\n";
}

int main() {
    auto t = demo();
    t.h.resume();
    std::cout << "[caller]\n";
    t.h.resume();
}
```

编译并运行：

```bash
g++ -std=c++20 -O0 -g -Wall -Wextra -pedantic mini_task.cpp -o mini_task
./mini_task
```

你会看到 `promise` 构造在协程体执行之前；第一次 `resume()` 执行到 `co_await` 后暂停；第二次 `resume()` 执行完函数体，局部对象 `Probe` 析构，随后停在 `final_suspend`；直到 `Task` 析构，帧里的 `promise` 才析构。

关键结论：

- **帧的生命周期可能长于函数体的执行时间。** 函数体结束后，帧仍可因 `final_suspend` 而保留，等待拥有者读取结果或销毁。
- **局部对象和帧不是同一件事。** 协程体内的局部对象在离开作用域时析构；`promise`、帧中参数等随帧销毁而清理。
- **`resume()` 只是让状态机继续运行，不代表新线程。** 谁调用 `resume()`，协程就在谁所在的线程继续。

---

## 2. `promise_type` 和 awaitable

### 直觉：promise 是协程的控制面板

协程体每到一个关键时刻，编译器都会去问 `promise`：

- 刚开始时要暂停吗？——`initial_suspend()`
- 结束时要暂停吗？——`final_suspend()`
- 返回什么给调用者？——`get_return_object()`
- `co_return;` 怎么处理？——`return_void()`
- 抛异常怎么办？——`unhandled_exception()`

`co_await` 则是在问另一个对象：**awaiter**。表达式 `co_await expr` 会先得到一个 awaiter，然后依次调用：

1. `await_ready()`：如果返回 `true`，不暂停，直接继续。
2. `await_suspend(handle)`：如果返回 `true` 或 `void`，暂停；如果返回 `false`，不暂停；如果返回另一个协程句柄，可以做对称转移。
3. `await_resume()`：协程被恢复后调用，它的返回值就是 `co_await` 表达式的结果。

因此，`co_await` 不等于“切线程”。它只表示“这里可能暂停”。是否暂停由 `await_ready()` 决定，暂停后什么时候、在哪个线程恢复，由 `await_suspend()` 和持有句柄的调度器决定。

### promise 的最小协议

一个教学 `Task` 的 `promise_type` 至少需要这些成员：

```cpp
struct promise_type {
    Task get_return_object();
    std::suspend_always initial_suspend() noexcept;
    std::suspend_always final_suspend() noexcept;
    void return_void() noexcept;
    void unhandled_exception() noexcept;
};
```

`co_return value;` 则需要 `return_value(value)` 而不是 `return_void()`。`co_yield value;` 等价于 `co_await promise.yield_value(value);`，常用于生成器；后台任务更常见的是 `Task` 这种一次性协程。

### 完整的教学 `Task`

下面的 `Task` 故意保持很小：单所有权、手动恢复、`initial_suspend` 暂停、`final_suspend` 暂停、异常暂存后重抛。它不追求生产可用，只用于把协议和生命周期看清楚。

```cpp
#include <coroutine>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <utility>

class Task {
public:
    struct promise_type {
        Task get_return_object() {
            return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() noexcept { error = std::current_exception(); }

        std::exception_ptr error;
    };

    Task() noexcept = default;
    explicit Task(std::coroutine_handle<promise_type> h) noexcept : h_(h) {}
    Task(Task&& other) noexcept : h_(std::exchange(other.h_, nullptr)) {}
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            reset();
            h_ = std::exchange(other.h_, nullptr);
        }
        return *this;
    }
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    ~Task() { reset(); }

    bool done() const noexcept { return !h_ || h_.done(); }

    void resume() {
        if (!h_) throw std::logic_error("empty Task");
        if (h_.done()) throw std::logic_error("finished Task");
        h_.resume();
        if (h_.done() && h_.promise().error) {
            std::rethrow_exception(h_.promise().error);
        }
    }

private:
    void reset() noexcept {
        if (h_) {
            h_.destroy();
            h_ = {};
        }
    }

    std::coroutine_handle<promise_type> h_{};
};

struct InspectAwaiter {
    bool ready;
    bool await_ready() const noexcept { return ready; }
    void await_suspend(std::coroutine_handle<>) const noexcept {
        std::cout << "suspend on " << std::this_thread::get_id() << '\n';
    }
    int await_resume() const noexcept {
        std::cout << "resume on " << std::this_thread::get_id() << '\n';
        return 7;
    }
};

Task inspect(bool ready) {
    const int value = co_await InspectAwaiter{ready};
    std::cout << "value=" << value << '\n';
}

int main() {
    Task t = inspect(false);
    std::cout << "caller on " << std::this_thread::get_id() << '\n';
    t.resume();
    t.resume();
}
```

编译并运行：

```bash
g++ -std=c++20 -O0 -g -Wall -Wextra -pedantic task_await.cpp -o task_await
./task_await
```

输出中 `suspend`、`caller`、`resume` 的线程 ID 相同，因为它们都在主线程执行。这个例子刻意说明：**`co_await` 可以暂停，但并没有自动创建线程，也没有自动切换线程。** 真正把恢复投递到其他线程的是异步库的 awaiter/executor。

`await_suspend` 的返回类型值得记住：

- `void`：暂停。
- `bool`：`true` 暂停，`false` 不暂停，继续执行。
- `std::coroutine_handle<>`：对称转移到另一个协程，当前协程保持暂停。

生产库常用第三种返回类型把“恢复下一个协程”变成尾调用，减少调用栈增长；但这要求你非常清楚帧的存活关系。

---

## 3. `initial_suspend`、`final_suspend` 与异常传播

### `initial_suspend`：懒启动还是立即启动

`initial_suspend()` 决定协程体是否在调用时立即开始：

- 返回 `std::suspend_always`：**懒启动**。调用协程函数只创建帧和返回对象，函数体要等第一次 `resume()` 才执行。
- 返回 `std::suspend_never`：**立即启动**。协程体在调用点就开始运行，直到遇到第一个暂停点或直接结束。

对后台框架来说，懒启动通常更容易控制生命周期：调用者先拿到 `Task`，可以绑定请求上下文、注册取消、决定在哪个执行器上启动，然后再恢复。立即启动则可能在调用者还没拿到返回对象时就开始访问请求状态，设计时必须额外小心。

但要注意：`get_return_object()` 在 `initial_suspend()` 之前调用。即使是立即启动，返回对象也是先构造好的；不过调用者拿到它之前，协程体可能已经执行了一部分。

### `final_suspend`：结束后帧还在不在

`final_suspend()` 本身必须声明为 `noexcept`，并返回一个 awaitable。它决定协程正常结束或异常处理后，帧是否保留：

- 返回 `std::suspend_always`：协程停在 final suspend，`done()` 为 `true`，帧仍然有效。拥有者可以读取结果/异常，然后调用 `destroy()`；此时不要再次 `resume()`。教学 `Task` 用这一种。
- 返回 `std::suspend_never`：运行时会自动销毁帧；恢复返回后句柄已经失效，拥有者不能再 `destroy()`，也不应再通过它查询状态。

如果使用对称转移把 final suspend 接到下一个协程，也要保证当前帧在转移前不会被错误销毁，并明确谁负责最后销毁。这是异步库设计中最容易出问题的边界之一。

### 异常传播：语言层负责捕获，库负责重抛

协程体抛出的异常不会像普通函数那样直接穿过 `resume()` 自动抛给调用者。运行时会捕获它并调用：

```cpp
void unhandled_exception() noexcept {
    error = std::current_exception();
}
```

随后协程走到 `final_suspend`。因为教学 `Task` 的 final suspend 是 `suspend_always`，帧会保留，异常暂存在 `promise` 里。调用者在恢复后检查并重抛：

```cpp
void resume() {
    if (!h_) throw std::logic_error("empty Task");
    if (h_.done()) throw std::logic_error("finished Task");
    h_.resume();
    if (h_.done() && h_.promise().error) {
        std::rethrow_exception(h_.promise().error);
    }
}
```

一个会失败的协程可以这样写：

```cpp
Task failing() {
    co_await std::suspend_always{};
    throw std::runtime_error("rpc timeout");
}

int main() {
    Task t = failing();
    t.resume();
    try {
        t.resume();
    } catch (const std::runtime_error& e) {
        std::cout << "caught: " << e.what() << '\n';
    }
    std::cout << std::boolalpha << "done=" << t.done() << '\n';
}
```

把这段代码放在第 2 节 `Task` 的基础上编译：

```bash
g++ -std=c++20 -O0 -g -Wall -Wextra -pedantic task_exception.cpp -o task_exception
./task_exception
```

执行过程是：

1. 第一次 `resume()` 进入协程，执行到 `co_await` 后暂停，尚未抛异常。
2. 第二次 `resume()` 从暂停点继续，抛出 `runtime_error`。
3. 运行时捕获异常，调用 `unhandled_exception()` 把异常存进 `promise`。
4. 协程进入 final suspend，`resume()` 返回。
5. `Task::resume()` 发现 `done()` 且 `error` 非空，调用 `rethrow_exception()` 把异常交给调用者。
6. `Task` 析构时销毁帧，异常对象也随之释放。

如果 final suspend 使用 `suspend_never`，帧会自动销毁，调用者之后就不能再读 `promise().error`。如果 `unhandled_exception()` 自己抛出异常，异常会向恢复者传播，规则更细；教学实现选择“暂存 + 显式重抛”，便于把异常路径和帧销毁分开观察。

另外，`await_resume()` 抛出异常时，异常发生在协程恢复后的表达式求值中，同样会进入 `unhandled_exception()` 这条路径。不要把 `await_resume()` 当成“不会失败的结果读取”。

### 三个必须记住的结论

1. **`co_await` 不保证暂停。** `await_ready()` 为 `true` 时直接继续；`await_suspend()` 返回 `false` 时也不暂停。
2. **暂停不保证换线程。** 手动恢复器就在同一线程 `resume()`；异步库可能在同一事件循环线程恢复，也可能投递到其他线程。
3. **final suspend 后帧可能还活着。** 异常和返回值都可能放在 `promise` 里，拥有者必须按约定读取并销毁。

---

## 常见误区：临时 Lambda 的捕获可能比协程帧先死

这是 C++20 协程里非常典型的悬空问题。看下面的反例：

```cpp
Task dangerous() {
    int user_id = 42;
    return [&]() -> Task {
        co_await std::suspend_always{};
        std::cout << user_id << '\n';
    }();
}
```

问题有两层：

- `dangerous()` 返回时，局部变量 `user_id` 已经销毁。
- Lambda 闭包是临时对象，在 `return` 的完整表达式结束后也销毁；但协程帧可能在之后才被恢复。

对协程 Lambda 来说，捕获保存在闭包对象里，协程帧并不会自动把闭包“安全复制”成独立生命周期。闭包先死，协程恢复时通过失效的闭包访问捕获，就是未定义行为。

更安全的写法是把需要的数据作为协程参数按值传入：

```cpp
Task safe(int user_id) {
    co_await std::suspend_always{};
    std::cout << user_id << '\n';
}
```

按值参数会进入协程帧，生命周期跟随帧，而不是跟随调用者的局部变量。注意：如果参数是 `const Request&` 或指针，帧里保存的仍然只是引用/指针，不会延长被引用对象的寿命。后台场景里，请求对象、连接对象、会话对象的所有权必须在设计时就写清楚。

如果非要用 Lambda：

- 不要让协程任务活得比闭包对象更久；
- 不要让捕获引用指向已经离开作用域的局部变量；
- 优先把关键数据按值作为参数传入，而不是依赖捕获；
- 对跨线程恢复的协程，还要额外考虑数据竞争和同步。

## 设计权衡

### 无栈协程换来了什么

- **减少阻塞线程的需求**：等待 I/O 时协程暂停，线程可以去处理其他任务，不必一个请求占一个线程。
- **代码形态更直接**：异步流程可以写成顺序代码，减少回调嵌套。
- **暂停成本通常低于栈切换**：不需要切换整个线程栈，只保存协程帧中的状态。

同时要记住：协程是协作式的，它只在暂停点让出执行权；一段长时间不让出的 CPU 循环仍然会卡住事件循环。

### 代价在哪里

- **它不是自动并发运行时**：没有调度器、线程池、超时和取消。框架必须提供这些能力。
- **取消和所有权更复杂**：协程暂停时，外部 I/O 操作可能仍持有回调。销毁帧前必须确保不会再有恢复；否则就是 use-after-free。取消通常是协作式的，需要在暂停点/恢复点检查取消令牌。
- **同步/阻塞式思维会出错**：普通函数里的阻塞调用不能被 `co_await` 自动变成异步；调用链上的异步函数也必须是协程，或者由库封装成 awaiter。
- **调试更分散**：一个逻辑请求可能跨多次恢复，调用栈不再连续，需要请求 ID、trace 和协程生命周期日志。
- **帧分配和异常对象有成本**：编译器可以优化掉部分帧分配，但库不能假设一定零成本；异常路径通常也比错误码更重。

对腾讯微信公众号后台来说，最实际的设计问题是：**请求被取消/连接断开时，谁负责停止协程、注销 I/O、保证帧只销毁一次？** 这个问题不能靠语言协程自动解决，必须在库的 `Task`、取消令牌和 I/O 注册层一起设计。

---

## 动手实践：实现教学 `Task` 和手动恢复器

这个练习只用于理解机制，不用于生产。目标是把三件事看清楚：

1. 帧什么时候创建、什么时候暂停、什么时候销毁。
2. 一个帧只销毁一次。
3. 异常如何被暂存，并在恢复时传播给调用者。

### 步骤 1：创建 `manual_task.cpp`

将下面的完整代码写入文件。代码中的 `ManualRunner` 就是手动恢复器：每次 `step()` 显式推进一次协程，不做任何线程调度。

```cpp
#include <cassert>
#include <coroutine>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>

class Task {
public:
    struct promise_type {
        promise_type() { std::cout << "[promise ctor]\n"; }
        ~promise_type() { std::cout << "[promise dtor]\n"; }

        Task get_return_object() {
            return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
        }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_void() noexcept {}
        void unhandled_exception() noexcept { error = std::current_exception(); }

        std::exception_ptr error;
    };

    Task() noexcept = default;
    explicit Task(std::coroutine_handle<promise_type> h) noexcept : h_(h) {}
    Task(Task&& other) noexcept : h_(std::exchange(other.h_, nullptr)) {}
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            reset();
            h_ = std::exchange(other.h_, nullptr);
        }
        return *this;
    }
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    ~Task() { reset(); }

    bool done() const noexcept { return !h_ || h_.done(); }

    void resume() {
        if (!h_) throw std::logic_error("empty Task");
        if (h_.done()) throw std::logic_error("finished Task");
        h_.resume();
        if (h_.done() && h_.promise().error) {
            std::rethrow_exception(h_.promise().error);
        }
    }

private:
    void reset() noexcept {
        if (h_) {
            h_.destroy();
            h_ = {};
        }
    }

    std::coroutine_handle<promise_type> h_{};
};

class ManualRunner {
public:
    explicit ManualRunner(Task&& task) : task_(std::move(task)) {}

    bool step() {
        task_.resume();
        return task_.done();
    }

    bool done() const noexcept { return task_.done(); }

private:
    Task task_;
};

struct LocalState {
    explicit LocalState(std::string name) : name_(std::move(name)) {
        std::cout << "[local ctor] " << name_ << '\n';
    }
    ~LocalState() {
        std::cout << "[local dtor] " << name_ << '\n';
    }

    std::string name_;
};

Task lifecycle_demo() {
    LocalState state{"request-state"};
    std::cout << "[body] before suspend " << state.name_ << '\n';
    co_await std::suspend_always{};
    std::cout << "[body] after resume\n";
}

Task failing_demo() {
    co_await std::suspend_always{};
    throw std::runtime_error("rpc timeout");
}

int main() {
    {
        ManualRunner runner{lifecycle_demo()};
        std::cout << "[caller] step 1\n";
        runner.step();
        std::cout << "[caller] step 2\n";
        runner.step();
        assert(runner.done());
        std::cout << "[caller] scope end\n";
    }

    std::cout << "--- exception path ---\n";

    {
        ManualRunner runner{failing_demo()};
        runner.step();
        try {
            runner.step();
        } catch (const std::runtime_error& e) {
            std::cout << "[caller] caught " << e.what() << '\n';
        }
        assert(runner.done());
        std::cout << "[caller] scope end\n";
    }
}
```

### 步骤 2：编译并运行

```bash
g++ -std=c++20 -O0 -g -Wall -Wextra -pedantic manual_task.cpp -o manual_task
./manual_task
```

预期输出：

```text
[promise ctor]
[caller] step 1
[local ctor] request-state
[body] before suspend request-state
[caller] step 2
[body] after resume
[local dtor] request-state
[caller] scope end
[promise dtor]
--- exception path ---
[promise ctor]
[caller] caught rpc timeout
[caller] scope end
[promise dtor]
```

### 步骤 3：读数输出，确认帧的生命周期

重点观察三组输出：

- **`[promise ctor]` 在第一次 `step()` 之前出现。** 说明协程帧和 `promise` 在函数调用时就建好了，即使协程体还没开始。
- **`[local dtor]` 出现在第二次 `step()` 中，而 `[promise dtor]` 出现在作用域结束。** 说明协程体局部对象和协程帧的销毁时机不同。
- **`[promise dtor]` 只出现一次。** `ManualRunner` 析构时带着 `Task` 析构，`Task` 的唯一句柄执行一次 `destroy()`。

### 步骤 4：验证移动语义没有制造双重销毁

把生命周期演示段临时改成手动转移所有权：

```cpp
Task a = lifecycle_demo();
Task b = std::move(a);
b.resume();
b.resume();
```

编译运行后仍应只看到一次 `[promise dtor]`。原因是移动构造函数用 `std::exchange` 把源句柄置空；源对象析构时不会再次 `destroy()`。

不要为了“观察两次销毁”而故意调用两次 `destroy()`。第二次调用是未定义行为，可能崩溃、可能静默破坏堆，这不能作为测试手段。

### 步骤 5：检查异常路径

在异常段中确认：

- 第一次 `step()` 只走到 `co_await`，不会抛异常。
- 第二次 `step()` 中异常被 `unhandled_exception()` 暂存，协程进入 final suspend，随后 `Task::resume()` 重抛。
- `catch` 之后 `runner.done()` 为 `true`。
- 作用域结束时 `promise` 仍然恰好析构一次。

可以临时把 `unhandled_exception()` 改成 `std::terminate()`，观察异常不再被重抛而是直接终止程序；然后改回“暂存 + 重抛”。这能直观看出：**异常如何传播是 `promise_type` 的实现选择，不是 `co_await` 自动完成的魔法。**

### 步骤 6：阅读编译器降低后的代码

节点建议阅读编译器协程降低后的代码。可以在本地生成汇编：

```bash
g++ -std=c++20 -O0 -S manual_task.cpp -o manual_task.s
```

在生成的汇编或 Compiler Explorer 中标记：

- 帧分配和 `promise` 构造的位置；
- 参数、局部状态在帧中的保存位置；
- `co_await` 对应的暂停点和恢复入口；
- `final_suspend` 后的销毁路径；
- 异常路径中 `unhandled_exception` 的调用点。

不要求读懂每条指令，重点是确认：**编译器确实把协程函数改写成了状态机，暂停点附近会保存/恢复帧状态，而 `destroy()` 会走帧销毁路径。**

### 验收清单

- [ ] 能说明 `co_await` 不一定切换线程：是否暂停由 `await_ready()` 决定，恢复线程由调度器决定。
- [ ] 能说明每个协程帧恰好销毁一次：句柄非拥有，`Task` 用移动语义保证唯一所有权，析构只 `destroy()` 一次。
- [ ] 能指出教学 `Task` 中 `initial_suspend`、`final_suspend` 分别返回什么，以及为什么。
- [ ] 能解释异常从协程体到 `promise` 再到调用者的路径。
- [ ] 能说清临时 Lambda 捕获为什么可能悬空，并给出按值传参的替代方案。

---

## 自测题

### 题 1：`co_await` 一定会切换线程吗？

**答案：不一定，通常也不会仅因为 `co_await` 就切换线程。**

`co_await` 只是可能暂停。若 `await_ready()` 返回 `true`，协程直接继续；若 `await_suspend()` 返回 `false`，也不暂停。即使暂停，恢复线程也由持有句柄的调度器决定。教学 `ManualRunner` 在主线程 `resume()`，所以暂停和恢复都在同一线程。异步库可以把恢复投递到 I/O 线程或线程池，但那是库的行为，不是语言的保证。

### 题 2：协程抛出异常，`final_suspend` 返回 `suspend_always`，谁负责销毁帧？

**答案：拥有协程句柄的返回对象负责，通常是 `Task` 的析构函数；必须恰好销毁一次。**

异常发生时，运行时调用 `unhandled_exception()`，教学实现把异常存进 `promise`，然后协程停在 final suspend。此时帧仍然有效，局部对象可能已在栈展开中析构，但 `promise`、参数和帧本身仍在。调用者恢复并收到重抛的异常后，帧不会自动消失；`Task` 析构时调用 `destroy()`，清理帧。若忘记销毁会泄漏；若句柄被复制后两个对象都销毁，就是重复销毁，属于未定义行为。

### 题 3：为什么临时协程 Lambda 的捕获可能比帧先失效？

**答案：捕获存在闭包对象里，而临时闭包会在完整表达式结束时销毁；协程帧可能之后才恢复。**

例如 `return [&]() -> Task { co_await ...; use(x); }();`，闭包是临时对象，`return` 表达式结束就析构；被捕获的局部变量也可能已经离开作用域。协程恢复时再通过失效的闭包/引用访问捕获，就是悬空访问。修复方式是把数据按值作为协程参数传入，让参数副本进入帧；或者确保闭包和所有捕获的生命周期严格长于 `Task`。引用和指针参数不会自动延长被引用对象的寿命，这一点在请求对象和连接对象上尤其危险。

---

## 推荐资源

以下资源来自本课节点：

| 资源 | 来源 | 语言 | 类型 | 难度 | 费用 | 说明 |
|---|---|---|---|---|---|---|
| [C++20无栈协程机制：参考文档 / 原始资料](https://en.cppreference.com/w/cpp/language/coroutines.html) | en.cppreference.com | 英文 | 文档 | 进阶 | 免费 | 重点阅读协程帧与生命周期、`promise_type` 和 awaitable，用于核对本节点实验的语义和边界。 |
| [C++20 Coroutines 语言机制](https://en.cppreference.com/cpp/language/coroutines) | cppreference | 英文 | 文档 | 进阶 | 免费 | 区分编译器提供的暂停恢复协议与库负责的调度器、执行器。 |

阅读时建议带着三个问题：帧何时创建和销毁？`co_await` 的暂停条件是什么？哪些职责属于语言、哪些属于库？把答案对照本课的 `Task` 和 `ManualRunner`，你会更容易看懂真实异步框架的 `Task<T>`、`Executor`、取消令牌和 I/O awaiter。





---

## 附：一键验收脚本（已在 g++ 14 + ASan/UBSan 下跑通）

`practice31/` 下备有两个文件，可直接对照验收标准：

- `task.cpp`：教学 `Task`（`return_value` 版）+ 手动恢复器。通过重载 `promise_type::operator new/delete` 统计帧的创建与销毁次数，覆盖 4 条路径：正常跑完、异常暂存后 `get()` 重抛、**没跑完就析构**（挂起中的帧也要 destroy）、移动后只销毁一次。并记录 `co_await` 前后的线程 id。
- `lambda_bug.cpp`：临时协程 Lambda 按值捕获 `std::string`，lambda 析构后再 `resume()`，ASan 报错；改成按值协程参数后正常。

```bash
cd practice31 && ./test.sh
```

实测输出要点：

```text
resumes_until_done=3 value=5
same_thread_after_co_await=yes
rethrown=db timeout
frames_created=4 frames_alive=0
ALL_CHECKS_PASSED
user=64
good_version_ok
stack-use-after-return
lambda_capture_bug_detected
```

注意 `lambda_bug.cpp` 里捕获是**按值**的，照样出事：按值捕获的副本存在闭包对象里，而协程帧只保存指向闭包的 `this`，所以“按值捕获”救不了，只有“按值参数”才会被拷进帧。
