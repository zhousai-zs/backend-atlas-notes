# 第 19/208 课：对象生命周期与存储期

> 节点 id：`b-lifetime` · 域：B · 子类：生命周期与所有权 · 难度：基础 · 前置：`b-pointer`
>
> 一句话：对象生命周期从满足初始化条件开始，到析构或存储被释放或复用时结束；存储存在不代表对象可用。

## 1. 学习目标与为什么后端要懂

### 1.1 学习目标

完成本课后，你应该能够：

1. **解释自动、静态和线程存储期。** 说清一个对象“住在哪里、活多久、什么时候构造、什么时候析构”，并区分“存储期”和“对象生命周期”这两个容易混为一谈的概念。
2. **独立完成节点实践。** 用日志记录嵌套对象与临时对象的构造析构，验证 `std::string_view` 绑定临时 `std::string` 后的悬垂问题，并能用 AddressSanitizer（ASan）读懂报告。
3. **解释引用延长的适用范围。** 知道局部 `const T&` 或 `T&&` 直接绑定临时对象时，临时对象会活到引用的作用域结束；也知道这种延长**不会穿过函数返回、函数参数、引用成员或 `std::string_view`**。
4. **画出成员与宿主对象的生命周期。** 能说清基类、成员、构造函数体、析构函数体、成员逆序析构之间的先后关系，并能据此审查后台连接对象、回调对象和异步任务的释放时机。

### 1.2 验收标准回扣

节点给出的验收标准有两条，本课会反复用到：

- **能解释引用延长的适用范围。** 重点不是背“`const T&` 会延长寿命”这一句话，而是知道它只在**局部引用变量直接绑定临时对象**时成立。绑定到函数返回的引用、函数参数、引用成员、`std::string_view` 或裸指针，都不会把临时对象延长到该引用/视图/指针的寿命；函数参数只在调用表达式期间有效。
- **能画出成员与宿主对象的生命周期。** 宿主对象构造时，基类先于成员，成员按**声明顺序**构造，构造函数体最后执行；析构时顺序基本相反：析构函数体先执行，成员按声明逆序析构，基类最后析构。成员子对象有自己的生命周期，不能简单地把“宿主对象还活着”等同于“每个成员都可以随意使用”。

### 1.3 为什么微信公众号后台要懂

节点对“为什么”写得很直接：**定位悬垂引用、临时对象和析构顺序导致的问题。** 放进腾讯微信公众号后台的典型 C++ 服务，就是下面这些具体场景：

- **回调参数只在回调期间有效。** 微信服务器把消息推到后台，框架通常会以 `void on_message(const std::string& body)` 或 `void on_message(std::string_view body)` 的形式把请求体交给业务代码。这个 `body` 的所有者往往是框架或上层缓冲区。如果业务代码把 `body` 的引用、`std::string_view` 字段或指向它的指针存进队列，交给异步线程处理，回调返回后这些“借用”就可能悬垂。线上表现常常是偶发乱码、JSON 解析失败，甚至进程崩溃。
- **临时对象在签名、日志和拼接中大量出现。** 例如验签时拼接 `appid + timestamp + nonce`，日志里拼接 `request_id + " " + openid`。这些临时 `std::string` 默认只活到当前完整表达式结束。把它们直接交给一个返回 `std::string_view` 的函数，或者把 `c_str()` 存起来，都会留下悬垂视图。
- **成员析构顺序决定关闭阶段是否安全。** 一个连接对象可能持有 `logger_`、`socket_`、`timer_`、`on_timeout_` 等成员。如果 `timer_` 的析构函数还会触发回调，而回调捕获了 `this`，或者 `logger_` 已经被先销毁，关闭连接时就可能出现 use-after-free。后台服务的“优雅关闭”问题，很多都出在这里。
- **静态存储期和线程存储期容易制造“看起来是全局、其实是每线程”的误解。** 访问令牌缓存、配置、日志器常做成静态对象；请求上下文常做成 `thread_local`。静态对象的初始化顺序跨翻译单元没有保证，线程局部变量也不能拿来在线程之间共享。线程池复用线程、协程可能切换线程时，这类问题尤其隐蔽。
- **零拷贝解析很诱人，但所有权必须说清楚。** 公众号后台的请求体可能很大，解析 XML/JSON 时用 `std::string_view` 指向原缓冲区能减少拷贝。但视图不拥有数据，必须保证原缓冲区在所有视图使用完之前一直活着；一旦跨异步边界，通常就要复制数据或转移所有权。
- **ASan 能在测试阶段把“悬垂”变成可定位的报告。** 线上靠 core dump 猜成本很高；用 `-fsanitize=address` 跑单测，可以直接看到 `heap-use-after-free`、`stack-use-after-scope`，以及“谁释放了这块内存”的调用栈。

所以本课的目标不是背标准条款，而是建立一条工程习惯：**看到引用、指针、视图、临时对象和成员对象时，先问“谁拥有它、它活多久、谁可能让它提前结束”。**

## 2. 从直觉到原理：三个核心要点

### 2.1 自动、静态和线程存储期

#### 直觉：对象住在哪里、住多久

“存储期”描述的是**存储**能持续多久；“生命周期”描述的是**对象**从什么时候开始可用、到什么时候不能再当作该类型对象使用。两者经常同时发生，但并不总是同一件事。

C++ 里常见的存储期可以先用一张表建立直觉：

| 存储期 | 典型声明 | 何时开始 | 何时结束 | 关键点 |
|--------|----------|----------|----------|--------|
| 自动存储期 | 块作用域变量、函数参数 | 进入作用域并完成初始化 | 离开作用域，包括异常栈展开 | 每次进入作用域都是新对象 |
| 静态存储期 | 命名空间变量、`static` 局部变量、静态数据成员 | 程序启动前，或首次执行声明时 | 程序退出 | 局部静态变量 C++11 起初始化线程安全 |
| 线程存储期 | `thread_local` 变量 | 该线程首次使用前 | 该线程退出 | 每个线程一份，不是共享变量 |
| 动态存储期 | `new` / `malloc` 得到的存储 | `new` 分配并构造后；`malloc` 只分配存储，需 placement new 才开始对象生命周期 | `delete` / 显式析构后 | 本节点不展开，但悬垂问题常来自这里 |

#### 自动存储期：离开作用域就结束

自动对象最常见：函数里的局部变量、函数参数、块作用域里的对象。它们在执行到声明时构造，在离开作用域时析构；如果中途抛异常，栈展开也会调用已经构造完成的自动对象析构函数。

```cpp
#include <iostream>
#include <string>
#include <utility>

struct Loud {
    std::string name;

    explicit Loud(std::string n) : name(std::move(n)) {
        std::cout << "+ " << name << '\n';
    }

    ~Loud() {
        std::cout << "- " << name << '\n';
    }
};

void handle() {
    Loud a{"a"};
    {
        Loud b{"b"};
    }
    Loud c{"c"};
}

int main() {
    handle();
}
```

输出顺序是：

```text
+ a
+ b
- b
+ c
- c
- a
```

`b` 在内部块结束时先析构，`c` 在函数结束时析构，`a` 最后析构。自动对象的生命周期与作用域强绑定，这是最容易推理的一类对象。

#### 静态存储期：活得比函数长

静态存储期包括命名空间作用域变量、静态数据成员和 `static` 局部变量。它们从程序启动附近开始存在，到程序退出时销毁。`static` 局部变量有一个重要特点：**第一次执行到声明时才初始化**，而不是每次调用都初始化。

```cpp
#include <iostream>
#include <string>
#include <thread>
#include <utility>

struct Loud {
    std::string name;

    explicit Loud(std::string n) : name(std::move(n)) {
        std::cout << "+ " << name << '\n';
    }

    ~Loud() {
        std::cout << "- " << name << '\n';
    }
};

Loud global{"global"};

void handle(const std::string& who) {
    Loud automatic{who + "-auto"};
    static Loud process_wide{"static-local"};
    thread_local Loud per_thread{who + "-thread-local"};

    std::cout << "  handling " << who << '\n';
    (void)process_wide;
    (void)per_thread;
}

int main() {
    std::cout << "main starts\n";
    handle("main");

    std::thread worker([] { handle("worker"); });
    worker.join();

    std::cout << "main ends\n";
}
```

一次典型输出是：

```text
+ global
main starts
+ main-auto
+ static-local
+ main-thread-local
  handling main
- main-auto
+ worker-auto
+ worker-thread-local
  handling worker
- worker-auto
- worker-thread-local
main ends
- main-thread-local
- static-local
- global
```

从输出可以读出三件事：

- `global` 在 `main` 之前构造，在 `main` 之后析构。
- `static-local` 只在第一次调用 `handle` 时构造，第二次调用不会重复构造；它在程序退出阶段才析构。
- `main-thread-local` 和 `worker-thread-local` 各自属于对应线程：主线程的在线程退出阶段析构，工作线程的在工作线程退出时析构。

静态存储期有两个经典坑：

1. **跨翻译单元的初始化顺序没有保证。** 一个 `.cpp` 里的全局对象构造函数如果使用另一个 `.cpp` 里的全局对象，后者可能还没构造。常用解法是把全局对象改成函数内静态对象（Meyers singleton），把初始化推迟到首次调用。
2. **程序退出阶段的析构顺序也要小心。** 同一翻译单元内通常按构造逆序销毁，但跨翻译单元的顺序未指定。不要在静态对象的析构函数里依赖另一个可能已经销毁的静态对象，尤其是日志器、配置和连接池。

#### 线程存储期：每个线程一份

`thread_local` 变量在每个线程中有独立实例。它适合保存“当前线程的请求上下文”“线程私有的缓冲区”等内容，但不适合做跨线程共享。

```cpp
#include <iostream>
#include <string>
#include <thread>

thread_local std::string tls_request_id;

void handle_request(const std::string& id) {
    tls_request_id = id;
    std::cout << "thread " << std::this_thread::get_id()
              << " handles " << tls_request_id << '\n';
}

int main() {
    std::thread a([] { handle_request("req-a"); });
    std::thread b([] { handle_request("req-b"); });
    a.join();
    b.join();
}
```

两个线程各自看到自己的 `tls_request_id`，互不干扰。需要注意：

- `thread_local` 的构造发生在该线程首次使用之前，析构发生在线程退出时；主线程的线程局部对象也在程序退出阶段销毁。
- 它绑定的是**线程**，不是逻辑请求。线程池复用线程时，如果不清空上下文，下一个请求可能读到上一个请求的残留值。
- 协程如果可能在不同线程上恢复执行，`thread_local` 不会自动跟着协程走，容易导致上下文错乱。此时应把上下文作为显式参数或协程状态传递。
- 不要把某个线程的 `thread_local` 对象引用传给另一个线程；通过引用访问的是原线程的对象，可能造成数据竞争或悬垂；只有通过变量名访问，才会得到当前线程自己的实例。

#### 后台例子：静态令牌缓存与线程局部请求上下文

访问令牌缓存通常希望全进程共享，可以用函数内静态对象：

```cpp
#include <cstdint>
#include <string>

struct AccessToken {
    std::string value;
    std::int64_t expires_at;
};

AccessToken& token_cache() {
    static AccessToken cache;
    return cache;
}
```

函数内静态对象在 C++11 起初始化线程安全，并且能把初始化推迟到首次调用，避开跨翻译单元的静态初始化顺序问题。

请求上下文则更适合 `thread_local`：

```cpp
#include <string>

struct RequestContext {
    std::string request_id;
    std::string openid;
};

thread_local RequestContext tls_context;

void on_message(const std::string& body) {
    tls_context.request_id = make_request_id();
    tls_context.openid = extract_openid(body);
}
```

但要注意：如果异步任务在线程池的另一个线程执行，它看不到原线程的 `tls_context`。正确做法是把需要的字段复制到任务对象里，或者显式传递一个 `RequestContext`，不要依赖 `thread_local` 自动跨线程传递。

#### 存储期不等于生命周期

节点 summary 强调：**存储存在不代表对象可用。** 这句话至少有三层含义：

1. **原始存储没有对象。** `operator new(sizeof(T))` 或 `std::byte buffer[sizeof(T)]` 只得到一块字节，`T` 的对象还没有开始生命周期。必须用 placement new 构造，之后才能通过 `T*` 使用；直接 `reinterpret_cast<T*>(buffer)->foo()` 是未定义行为（UB）。
2. **容器容量里可能有未构造区域。** `std::vector<T>` 的 `size()` 表示已经构造的元素个数，`capacity()` 表示已分配存储能容纳多少个元素。访问 `size()` 到 `capacity()` 之间的区域不是“读到旧值”，而是访问不存在的对象。
3. **联合体只有活跃成员活着。** `union` 的存储足够大，但同一时间只有当前活跃成员的对象生命周期在进行；读取非活跃成员通常是 UB（标准布局类型的公共初始序列等少数情况除外）。

对象生命周期的粗略规则是：

- 标量类型：存储就位并完成初始化后，对象生命周期开始；存储被释放或复用时结束。
- 类类型：初始化完成、对象可以正常使用时，完整对象可用；完整对象的生命周期在析构函数开始执行时结束。析构函数体和成员析构仍在进行，但这段时间只能做有限操作，不能把对象当成“还完整活着”随意使用。
- 成员子对象有自己的生命周期：它们在宿主构造过程中按顺序开始，在宿主析构函数体执行完后按逆序结束。

这也是为什么“对象还占着内存”不等于“对象还能用”。悬垂引用、悬垂指针、悬垂视图，本质上都是**存储可能还在，但对象生命周期已经结束**。

### 2.2 临时对象与引用延长

#### 直觉：临时对象默认只活到分号

表达式里经常会产生没有名字的临时对象，例如：

```cpp
std::string s = "hello " + std::string("wechat");
```

`std::string("wechat")` 和加法结果都是临时对象。它们通常活到**当前完整表达式**结束，也就是大致到分号为止。下面这种写法是安全的，因为临时对象在 `s` 的初始化完成前一直活着：

```cpp
std::string s = "hello " + std::string("wechat");
```

但把临时对象的“地址”或“视图”留下来，就要小心：

```cpp
const char* p = std::string("hello").c_str();
std::string_view v = std::string("hello");
```

这两行结束后，临时 `std::string` 已经销毁，`p` 和 `v` 都悬垂。指针和 `std::string_view` 都不是引用，不会触发引用延长。

#### 原理：引用延长的规则

C++ 有一个特例：**局部 `const T&` 或 `T&&` 直接绑定到临时对象时，临时对象的生命周期延长到该引用的生命周期结束。**

```cpp
const std::string& a = std::string("hello");
std::string&& b = std::string("wechat");
```

`a` 和 `b` 都是局部引用，临时 `std::string` 会活到 `a` / `b` 离开作用域。这个规则很实用，例如：

```cpp
const std::string& greeting = make_greeting();
```

如果 `make_greeting()` 按值返回 `std::string`，返回的临时对象会直接绑定到 `greeting`，寿命被延长。但如果 `make_greeting()` 返回的是 `const std::string&`，情况就不同了：如果它返回的是临时对象的引用，临时对象是在函数内部的 `return` 表达式里创建并销毁的，`greeting` 只是复制了一个已经悬垂的引用；如果它返回的是静态/全局对象的引用，引用本身有效，但那是对象本身的寿命，不是“引用延长”。

把常见写法列成表：

| 写法 | 是否延长 | 说明 |
|------|----------|------|
| `const T& r = T{};` | 是 | 局部 const 左值引用直接绑定临时对象 |
| `T&& r = T{};` | 是 | 局部右值引用直接绑定临时对象 |
| `const T& r = make();`，`make` 返回 `T` | 是 | 返回的临时对象直接绑定到 `r` |
| `const T& r = make_ref();`，`make_ref` 返回 `const T&` | 否 | 若该引用指向临时/局部对象，临时对象在 `make_ref` 的 `return` 全表达式结束时就销毁；`r` 只是复制了悬垂引用。若返回静态/全局对象的引用，则安全，但这不是“延长” |
| `void f(const T&); f(T{});` | 不需要 | 临时对象活到包含调用的完整表达式结束，函数调用期间安全 |
| `struct S { const T& r; S() : r(T{}) {} };` | 否 | 引用成员绑定临时对象，构造调用所在完整表达式结束后就可能悬垂 |
| `const T& r = f().member;`，`f` 返回值，`member` 是值成员 | 是 | 延长的是 `f` 返回的完整临时对象；若 `member` 本身是引用成员，则不适用 |
| `std::string_view v = make();` | 否 | `string_view` 不是引用，不触发延长 |
| `const char* p = std::string("x").c_str();` | 否 | 指针不是引用，临时销毁后悬垂 |
| `const T& r = std::move(obj);` | 不需要 | `r` 绑定到 `obj` 本身，寿命由 `obj` 决定 |

一个关键结论：**引用延长不是“传染”的。** 它只在引用变量直接绑定临时对象时发生，不会穿过函数返回、函数参数、引用成员、指针或视图。

#### 用 Tracer 看见延长与不延长

先准备一个会打日志的 `Tracer`：

```cpp
#pragma once

#include <iostream>
#include <string>
#include <utility>

class Tracer {
public:
    explicit Tracer(std::string name) : name_(std::move(name)) {
        std::cout << std::string(depth_++, ' ') << "+ " << name_ << '\n';
    }

    ~Tracer() {
        std::cout << std::string(--depth_, ' ') << "- " << name_ << '\n';
    }

    Tracer(const Tracer&) = delete;
    Tracer& operator=(const Tracer&) = delete;
    Tracer(Tracer&&) = delete;
    Tracer& operator=(Tracer&&) = delete;

private:
    std::string name_;
    inline static thread_local int depth_ = 0;
};
```

下面这个函数按值返回 `Tracer`，绑定到局部 `const Tracer&` 后，临时对象会活到引用离开作用域：

```cpp
Tracer make_tracer() {
    return Tracer("returned temporary");
}

int main() {
    std::cout << "--- const ref extension ---\n";
    {
        const Tracer& t = make_tracer();
        std::cout << "t is alive\n";
    }
    std::cout << "--- after scope ---\n";
}
```

输出是：

```text
--- const ref extension ---
+ returned temporary
t is alive
- returned temporary
--- after scope ---
```

析构日志出现在 `t` 离开作用域时，而不是在 `make_tracer()` 返回时，说明延长生效了。

再看一个反例：函数返回的是引用，而不是值：

```cpp
const Tracer& make_dangling() {
    return Tracer("temporary returned by reference");
}

int main() {
    const Tracer& t = make_dangling();
    std::cout << "after make_dangling\n";
    (void)t;
}
```

输出是：

```text
+ temporary returned by reference
- temporary returned by reference
after make_dangling
```

临时对象在 `make_dangling()` 的 `return` 表达式结束时就被销毁了，`main` 里的 `t` 只是拿到了一个悬垂引用。GCC 通常会给出：

```text
warning: returning reference to temporary [-Wreturn-local-addr]
```

这个警告必须当错误处理。更隐蔽的是，如果先写 `const T& r = T{};` 再 `return r;`，有些编译器不一定会警告，但临时对象只延长到 `r` 这个局部引用，函数返回后同样悬垂。

#### 后台例子：解析结果不要保存指向请求体的视图

假设解析函数返回一组 `std::string_view` 字段：

```cpp
struct ParsedMessage {
    std::string_view type;
    std::string_view content;
};

ParsedMessage parse(std::string_view body);
```

如果这样使用：

```cpp
void on_message(const std::string& body) {
    ParsedMessage parsed = parse(body);
    enqueue([parsed] {
        use(parsed);
    });
}
```

`parsed.type` 和 `parsed.content` 指向 `body` 的缓冲区。回调返回后，`body` 可能被框架释放，异步任务再使用 `parsed` 就是 use-after-free。

安全的做法有两种：

1. **让解析结果拥有数据**：字段改成 `std::string`，解析时复制。
2. **让任务持有原缓冲区的所有权**：把请求体复制到 `std::shared_ptr<std::string>`，任务捕获这个 `shared_ptr`，视图只在 `shared_ptr` 存活期间使用。

```cpp
auto owned_body = std::make_shared<std::string>(body);
enqueue([owned_body] {
    ParsedMessage parsed = parse(*owned_body);
    use(parsed);
});
```

第二种做法减少了复制，但把释放时机交给了 `shared_ptr` 的引用计数。节点 tradeoff 提醒的正是这一点：**更短所有权链易推理，过度共享会隐藏释放时机。**

#### 签名场景：临时字符串与 `string_view`

验签代码经常拼接临时字符串：

```cpp
std::string sign(const std::string& appid,
                 const std::string& timestamp,
                 const std::string& nonce);

void verify_request(const std::string& appid,
                    const std::string& timestamp,
                    const std::string& nonce) {
    verify(sign(appid, timestamp, nonce));
}
```

这里 `sign(...)` 返回的临时 `std::string` 活到 `verify` 调用结束，是安全的。但如果写成：

```cpp
std::string_view bad_signature() {
    return sign("appid", "timestamp", "nonce");
}
```

`sign` 返回的临时字符串在 `return` 表达式结束时销毁，返回的 `string_view` 立刻悬垂。验签失败、偶发乱码，可能就是这样来的。

#### `std::string_view` 为什么特别危险

`std::string_view` 只是一个“指针 + 长度”的非拥有视图。它不管理内存，也不延长任何对象的寿命。常见危险来源有：

- **绑定临时 `std::string`**：临时对象在完整表达式结束时销毁，视图悬垂。
- **绑定局部 `std::string`**：局部对象离开作用域后，视图悬垂。
- **原字符串被修改或扩容**：`std::string` 的 `append`、`push_back` 等操作可能重新分配缓冲区，旧视图指向已释放内存；即使没有重新分配，内容变化也会让视图看到新内容，长度却可能不匹配。
- **原字符串被移动**：移动后原对象仍然存在，但缓冲区所有权可能转移；视图是否有效取决于具体实现和移动后的状态，不能依赖。
- **跨异步边界保存视图**：请求体、局部缓冲区、临时字符串都可能在异步任务执行前消失。

安全的用法是：**视图只在拥有者确定存活的作用域内使用。** 如果函数参数是 `std::string_view`，调用期间临时字符串是安全的；但函数不能把视图存起来跨调用使用。

```cpp
void log_message(std::string_view message);

log_message(make_body());
```

只要 `log_message` 内部不保存 `message`，这次调用是安全的，因为 `make_body()` 返回的临时字符串活到整个调用表达式结束。

### 2.3 成员构造析构顺序

#### 直觉：先地基，后楼层；拆楼顺序相反

一个类对象通常由基类子对象和成员子对象组成。构造时：

1. 虚基类（如果有多继承和虚继承）；
2. 直接非虚基类，按声明顺序；
3. 非静态数据成员，按**声明顺序**；
4. 构造函数体。

析构时大致相反：

1. 析构函数体；
2. 非静态数据成员，按声明**逆序**；
3. 直接非虚基类，按声明逆序；
4. 虚基类。

数组元素按下标递增构造，按下标递减析构。

#### 原理：初始化顺序看声明，不看初始化列表

很多初学者以为构造函数初始化列表里写的顺序就是实际初始化顺序。不是。**实际顺序由成员在类中的声明顺序决定。**

```cpp
#include "tracer.hpp"

#include <iostream>

struct Base {
    Tracer t{"Base"};

    Base() {
        std::cout << "  Base body\n";
    }

    ~Base() {
        std::cout << "  ~Base body\n";
    }
};

struct Inner {
    Tracer t{"Inner"};
};

struct Derived : Base {
    Tracer a{"Derived::a"};
    Inner inner;
    Tracer b{"Derived::b"};

    Derived() : a{"Derived::a"}, inner{}, b{"Derived::b"} {
        std::cout << "  Derived body\n";
    }

    ~Derived() {
        std::cout << "  ~Derived body\n";
    }
};

int main() {
    std::cout << "before Derived\n";
    {
        Derived d;
        std::cout << "inside scope\n";
    }
    std::cout << "after Derived\n";
}
```

输出是：

```text
before Derived
+ Base
  Base body
 + Derived::a
  + Inner
   + Derived::b
  Derived body
inside scope
  ~Derived body
   - Derived::b
  - Inner
 - Derived::a
  ~Base body
- Base
after Derived
```

顺序可以这样读：

- `Base` 先于 `Derived` 的成员构造，所以 `Base` 的 `Tracer` 和 `Base` 构造函数体先出现。
- `Derived` 的成员按声明顺序 `a`、`inner`、`b` 构造，最后执行 `Derived` 构造函数体。
- 析构时先执行 `~Derived` 函数体，然后按逆序销毁 `b`、`inner`、`a`，再执行 `~Base` 函数体，最后销毁 `Base` 的成员。

如果初始化列表写成：

```cpp
Derived() : b{"Derived::b"}, inner{}, a{"Derived::a"} {
}
```

实际顺序仍然是 `a`、`inner`、`b`。GCC 的 `-Wreorder`（Clang 对应 `-Wreorder-ctor`）会提醒你“初始化列表顺序和声明顺序不一致”：

```text
warning: 'Derived::b' will be initialized after [-Wreorder]
warning:   'Tracer Derived::a' [-Wreorder]
warning:   when initialized here [-Wreorder]
```

更危险的是在初始化表达式里使用还没构造的成员：

```cpp
struct BadInit {
    int a;
    int b;

    BadInit() : b(1), a(b + 1) {}
};
```

声明顺序是 `a` 先于 `b`，所以 `a` 先用 `b + 1` 初始化，而此时 `b` 还没有初始化。读取未初始化的 `b` 是 UB。`-Wreorder` 会警告，但不要依赖警告；写初始化列表时保持与声明顺序一致。

#### 构造失败时会发生什么

如果构造函数体抛出异常，完整对象不会构造成功，但**已经构造完成的基类和成员会按逆序析构**。例如 `Derived` 的构造函数体抛异常，已经构造好的 `Base`、`a`、`inner`、`b` 会被销毁。理解这一点有助于排查“构造到一半失败，资源有没有泄漏”的问题。RAII 之所以重要，就是因为成员和基类的析构会在异常路径上自动执行。

#### 后台例子：连接对象的成员顺序

假设一个连接对象持有日志器、套接字、超时回调和定时器：

```cpp
class Connection {
public:
    ~Connection() {
        timer_.stop();
        on_timeout_ = nullptr;
    }

private:
    Logger logger_;
    Socket socket_;
    std::function<void()> on_timeout_;
    Timer timer_;
};
```

成员声明顺序是 `logger_`、`socket_`、`on_timeout_`、`timer_`。构造顺序是 `logger_`、`socket_`、`on_timeout_`、`timer_`；析构顺序是 `timer_`、`on_timeout_`、`socket_`、`logger_`。

这个顺序有几个好处：

- `timer_` 最先析构，它析构时还可以安全使用 `on_timeout_`、`socket_`、`logger_`。
- `logger_` 最后析构，`socket_` 等成员的析构函数如果打日志，日志器还活着。
- 在 `~Connection` 函数体里先 `timer_.stop()` 并清空回调，避免定时器在对象销毁后继续触发回调。

但还要注意：如果 `on_timeout_` 捕获了 `this`，而某个异步操作在 `~Connection` 返回后仍然持有这个回调，那么即使成员顺序正确，也会发生 use-after-free。正确做法是：

- 在析构函数体里停止所有异步操作，并等待或取消它们；
- 异步回调不要长期捕获裸 `this`，可以使用 `std::weak_ptr` 观察对象是否还活着；
- 如果回调必须延长对象寿命，就明确使用 `std::shared_ptr`，并接受“对象可能晚于预期释放”的代价。

#### 画生命周期图

以 `Derived` 为例，可以画出这样的时间轴：

```text
时间轴 →

Derived 完整对象:
  构造开始 ────────────────────────────── 析构开始
  │ Base 构造 │ Derived 成员构造 │ body │ ~body │ 成员逆序析构 │ Base 析构 │

Base 子对象:
  [Base::t 构造][Base body][Base 使用期][~Base body][~Base::t]

Derived 成员:
  a:      [构造][使用期][析构]
  inner:     [构造][使用期][析构]
  b:            [构造][使用期][析构]
```

关键不是把图画得多漂亮，而是能回答：

- 哪个成员先构造？按声明顺序。
- 哪个成员先析构？按声明逆序。
- 构造函数体执行时，哪些成员已经构造好了？所有成员。
- 析构函数体执行时，哪些成员还活着？所有成员都还活着；成员析构发生在函数体之后。
- 基类和成员谁先谁后？基类先构造、后析构；成员后构造、先析构。

这张图能直接用于审查后台对象：如果 `timer_` 的回调依赖 `socket_`，就要保证 `timer_` 先于 `socket_` 析构；如果某个成员的析构函数要打日志，就要保证日志器比它活得久。

## 3. 常见误区与设计权衡

### 3.1 常见误区

1. **“存储还在，所以对象还能用。”**
   这是本节点最核心的误区。`vector` 的容量、`operator new` 的原始内存、`union` 的非活跃成员、`delete` 之后的堆块，都可能“内存还在”，但对象生命周期已经结束。有效性来自生命周期管理，不来自“地址看起来还有效”。

2. **“绑定到函数返回的引用会自动延长临时对象寿命。”**
   节点 pitfalls 明确点出：**绑定到函数返回引用不会自动延长原临时对象寿命。** `const T& r = f();` 只有在 `f` 按值返回 `T` 时，返回的临时对象才直接绑定到 `r` 并被延长；如果 `f` 返回 `const T&`，`r` 只是复制了 `f` 内部已经悬垂的引用。

3. **“`std::string_view` 是引用，所以会延长寿命。”**
   `std::string_view` 不是引用，它只是一个指针加长度。绑定临时 `std::string`、局部 `std::string` 或被修改过的 `std::string`，都可能悬垂。

4. **“引用成员绑定临时对象没问题。”**
   `struct S { const std::string& r; S() : r(std::string("x")) {} };` 中，临时字符串不会延长到 `S` 对象的寿命。构造结束后 `s.r` 就可能悬垂。需要拥有数据时，用 `std::string` 值成员；需要共享时，用 `std::shared_ptr` 等明确的所有权类型。

5. **“初始化列表里写的顺序就是构造顺序。”**
   实际顺序由声明顺序决定。初始化列表顺序不一致时，`-Wreorder` 会警告；在初始化表达式里使用尚未构造的成员是 UB。

6. **“静态对象一定已经初始化好了。”**
   跨翻译单元的动态初始化顺序没有保证。一个全局对象的构造函数使用另一个翻译单元的全局对象，可能读到未初始化状态。函数内静态对象（Meyers singleton）通常更安全。

7. **“程序退出时静态对象可以随便用。”**
   静态对象析构顺序跨翻译单元未指定。静态对象的析构函数里如果调用另一个可能已经销毁的静态日志器、配置或连接池，仍然是 UB。

8. **“`thread_local` 可以当全局变量共享。”**
   `thread_local` 每个线程一份，不是共享变量。把某个线程的 `thread_local` 引用传给另一个线程，或者在线程池复用线程时不清理上下文，都会导致数据错乱。协程切换线程时也要特别小心。

9. **“返回局部引用/指针，编译器一定会警告。”**
   直接 `return local;` 通常会触发 `-Wreturn-local-addr`，但通过引用变量中转、通过 `std::string_view` 中转时，编译器不一定警告。不要用“没警告”证明代码正确。

10. **“容器扩容不会影响已有引用。”**
    `std::vector` 扩容会重新分配存储，旧元素被移动或复制后销毁，指向旧元素的引用、指针、迭代器和视图全部失效。需要稳定地址时，考虑 `std::deque`、`std::list`、`std::vector<std::unique_ptr<T>>`，或者提前 `reserve`。

11. **“`std::move` 会结束对象生命周期。”**
    被移动的对象仍然活着，只是处于“有效但未指定”的状态；它的析构函数仍会执行。移动不等于销毁，也不等于可以跳过生命周期管理。

12. **“通过基类指针 `delete` 派生对象总是安全的。”**
    如果基类析构函数不是 `virtual`，通过基类指针 `delete` 派生对象是 UB，派生类的析构函数可能不会执行。生命周期没有正确结束，资源可能泄漏。

13. **“过度共享所有权没有代价。”**
    `std::shared_ptr` 能延长对象寿命，但也会隐藏释放时机。回调、缓存、会话对象互相持有 `shared_ptr` 时，容易出现对象迟迟不释放、引用环、关闭后仍被异步任务保活等问题。节点 tradeoff 说得很准确：**更短所有权链易推理，过度共享会隐藏释放时机。**

### 3.2 设计权衡

| 选择 | 好处 | 代价 / 风险 |
|------|------|-------------|
| 值语义、按值返回 | 所有权清晰，生命周期容易推理 | 大对象有拷贝/移动成本 |
| 引用 / 指针 / 视图 | 避免拷贝，适合大请求体、多层解析 | 生命周期约束转移给调用者；跨异步/线程容易悬垂 |
| 局部 `const T&` 延长临时对象 | 写法简洁，适合短期只读使用 | 只对直接绑定的局部引用生效，容易误以为“到处都能延长” |
| `std::string_view` | 零拷贝、接口简洁、可含 NUL | 不拥有数据、不保证 `\0` 结尾、不延长寿命 |
| `std::unique_ptr` | 单一所有权，释放责任明确 | 不能共享；转移所有权需要移动语义 |
| `std::shared_ptr` | 共享所有权，适合异步回调和缓存 | 原子引用计数成本；引用环；释放时机不直观 |
| `std::weak_ptr` | 观察共享对象，不延长寿命 | 使用前必须 `lock()`，可能已经失效 |
| 静态对象 | 全局访问、惰性初始化方便 | 初始化/析构顺序、线程安全、测试隔离问题 |
| `thread_local` | 每线程上下文，无锁访问 | 不是共享变量；线程池复用、协程迁移、析构顺序问题 |
| 显式上下文对象 / 依赖注入 | 生命周期和依赖关系清楚，易测试 | 需要更多参数和接口设计 |
| RAII 守卫 | 异常安全，释放时机确定 | 需要遵守析构函数约束，不能随意抛异常 |

一个实用的取舍原则：

- **优先缩短所有权链。** 能用值就用值，能 `unique_ptr` 就不要 `shared_ptr`；共享只在确实需要多个所有者时使用。
- **跨边界不借。** 回调返回、进入队列、跨线程、进缓存的数据，要么复制，要么转移所有权；不要把栈上或回调参数的引用/视图带出去。
- **在接口上写清寿命。** `std::string_view` 参数表示“只在本次调用期间有效”；`const T&` 参数也是借用；`shared_ptr` 表示共享所有权。命名和注释要让人一眼看出谁负责让对象活着。
- **用工具兜底，但不依赖工具。** `-Wall -Wextra`、`-Wreturn-local-addr`、ASan 都很有用，但优化后的 UB 可能绕过检测；代码审查和类型设计仍然是第一道防线。

## 4. 动手实践：Tracer + 引用延长 + `string_view` 悬垂

本实践对应节点 practice：**用日志记录嵌套对象与临时对象的构造析构，验证 `string_view` 悬垂。** 建议在 `/workspace/atlas/lessons/practice19` 下完成。

### 步骤 0：准备目录与工具链

```bash
mkdir -p /workspace/atlas/lessons/practice19
cd /workspace/atlas/lessons/practice19
g++ --version
```

需要支持 C++17 和 `-fsanitize=address` 的 GCC 或 Clang。本课示例命令按 GCC 14 编写。

### 步骤 1：写 `tracer.hpp`

```cpp
#pragma once

#include <iostream>
#include <string>
#include <utility>

class Tracer {
public:
    explicit Tracer(std::string name) : name_(std::move(name)) {
        std::cout << std::string(depth_++, ' ') << "+ " << name_ << '\n';
    }

    ~Tracer() {
        std::cout << std::string(--depth_, ' ') << "- " << name_ << '\n';
    }

    Tracer(const Tracer&) = delete;
    Tracer& operator=(const Tracer&) = delete;
    Tracer(Tracer&&) = delete;
    Tracer& operator=(Tracer&&) = delete;

private:
    std::string name_;
    inline static thread_local int depth_ = 0;
};
```

`Tracer` 在构造时打印 `+`，析构时打印 `-`，并用 `depth_` 做缩进。`depth_` 是 `thread_local`，避免多个线程同时打日志时缩进互相干扰。它只避免 `depth_` 的数据竞争，不保证多线程下整行输出不交错；生产日志应使用日志库或加锁。注意：这个缩进假设对象严格按栈式嵌套构造/析构；静态对象和线程局部对象的析构会晚于作用域，缩进不再代表调用栈，这是正常的。demo 也假设同一个 `Tracer` 的构造和析构发生在同一线程；跨线程传递对象需要更严谨的日志方案。

### 步骤 2：观察嵌套对象和成员顺序

新建 `nested_order.cpp`：

```cpp
#include "tracer.hpp"

#include <iostream>

struct Base {
    Tracer t{"Base"};

    Base() {
        std::cout << "  Base body\n";
    }

    ~Base() {
        std::cout << "  ~Base body\n";
    }
};

struct Inner {
    Tracer t{"Inner"};
};

struct Derived : Base {
    Tracer a{"Derived::a"};
    Inner inner;
    Tracer b{"Derived::b"};

    Derived() : a{"Derived::a"}, inner{}, b{"Derived::b"} {
        std::cout << "  Derived body\n";
    }

    ~Derived() {
        std::cout << "  ~Derived body\n";
    }
};

int main() {
    std::cout << "before Derived\n";
    {
        Derived d;
        std::cout << "inside scope\n";
    }
    std::cout << "after Derived\n";
}
```

编译运行：

```bash
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -g -O0 \
    nested_order.cpp -o nested_order
./nested_order
```

预期输出：

```text
before Derived
+ Base
  Base body
 + Derived::a
  + Inner
   + Derived::b
  Derived body
inside scope
  ~Derived body
   - Derived::b
  - Inner
 - Derived::a
  ~Base body
- Base
after Derived
```

检查点：

- `Base` 是否先于 `Derived` 的成员构造？
- `a`、`inner`、`b` 是否按声明顺序构造？
- 析构是否按 `b`、`inner`、`a` 逆序？
- `~Derived` 函数体是否在成员析构之前执行？
- `~Base` 函数体是否在 `Derived` 成员析构之后执行？

再故意把初始化列表顺序写反，观察 `-Wreorder`：

```cpp
Derived() : b{"Derived::b"}, inner{}, a{"Derived::a"} {
}
```

即使这样写，实际构造顺序仍然是 `a`、`inner`、`b`，但编译器会警告。编译时先去掉 `-Werror`，否则警告会直接变成错误。不要用初始化列表顺序去“控制”构造顺序。

### 步骤 3：验证引用延长与函数返回引用

新建 `lifetime_ext.cpp`：

```cpp
#include "tracer.hpp"

#include <iostream>

Tracer make_tracer() {
    return Tracer("returned temporary");
}

int main() {
    std::cout << "--- const ref extension ---\n";
    {
        const Tracer& t = make_tracer();
        std::cout << "t is alive\n";
    }
    std::cout << "--- after scope ---\n";
}
```

编译运行：

```bash
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -g -O0 \
    lifetime_ext.cpp -o lifetime_ext
./lifetime_ext
```

预期输出：

```text
--- const ref extension ---
+ returned temporary
t is alive
- returned temporary
--- after scope ---
```

析构日志在作用域结束时才出现，说明 `const Tracer& t = make_tracer();` 延长了返回临时对象的寿命。

新建 `return_ref.cpp`：

```cpp
#include "tracer.hpp"

#include <iostream>

const Tracer& make_dangling() {
    return Tracer("temporary returned by reference");
}

int main() {
    const Tracer& t = make_dangling();
    std::cout << "after make_dangling\n";
    (void)t;
}
```

编译时**不要**加 `-Werror`，因为这里故意触发警告：

```bash
g++ -std=c++17 -Wall -Wextra -g -O0 \
    -fsanitize=address -fno-omit-frame-pointer \
    return_ref.cpp -o return_ref
./return_ref
```

你应看到类似警告：

```text
warning: returning reference to temporary [-Wreturn-local-addr]
```

运行输出：

```text
+ temporary returned by reference
- temporary returned by reference
after make_dangling
```

临时对象在 `make_dangling()` 返回时就析构了，`main` 里的 `t` 已经悬垂。这个例子故意不读取 `t`，所以 ASan 不一定会报错；日志已经足以证明生命周期没有延长。

### 步骤 4：验证 `string_view` 悬垂

新建 `view_dangling.cpp`：

```cpp
#include <iostream>
#include <string>
#include <string_view>

std::string make_body() {
    return std::string("{\"msg\":\"a long body that does not fit in small-string optimization\"}");
}

int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "scope";

    if (mode == "safe") {
        std::string body = make_body();
        std::string_view view = body;
        std::cout << view << '\n';
        return 0;
    }

    if (mode == "temporary") {
        std::string_view view = make_body();
        std::cout << view << '\n';
        return 0;
    }

    std::string_view view;
    {
        std::string body = make_body();
        view = body;
    }
    std::cout << view << '\n';
}
```

这里的字符串故意写得较长，超过常见 `std::string` 的小字符串优化（SSO）阈值，让数据落在堆上，便于 ASan 稳定报告 `heap-use-after-free`。

编译：

```bash
g++ -std=c++17 -Wall -fsanitize=address -Wextra -g -O0 \
    -fno-omit-frame-pointer \
    view_dangling.cpp -o view_dangling
```

先运行安全版本：

```bash
./view_dangling safe
```

应正常输出 JSON 内容。再运行两个悬垂版本：

```bash
./view_dangling scope
./view_dangling temporary
```

两者都应以非 0 退出，并出现类似报告：

```text
ERROR: AddressSanitizer: heap-use-after-free on address ...
READ of size ... at ...
    #0 ... std::basic_ostream...
    #1 ... main ... view_dangling.cpp:<行号>

freed by thread T0 here:
    #0 ... operator delete(void*)
    #1 ... std::string::~basic_string...
```

报告里先看错误类型，再看 `READ` 的调用栈（谁在越界读），最后看 `freed by`（谁提前释放了字符串）。`scope` 场景中 `body` 离开作用域时释放；`temporary` 场景中 `make_body()` 返回的临时字符串在完整表达式结束时释放。

如果字符串很短，可能落在栈上，ASan 报告会变成 `stack-use-after-scope`；也可能因为优化或实现细节没有被捕获。**ASan 没报不代表代码正确**，但一旦报出来，就是明确的 UB 证据。

### 步骤 5：修复悬垂问题

针对上面的实验，修复思路如下：

1. **返回引用改为按值返回。**

```cpp
Tracer make_tracer() {
    return Tracer("returned temporary");
}
```

如果调用方需要长期持有，就让返回值拥有数据，而不是返回内部临时对象的引用。

2. **让视图的拥有者活得更久。**

```cpp
std::string body = make_body();
std::string_view view = body;
use(view);
```

只要 `body` 在 `view` 使用期间一直活着，并且不被修改到重新分配，视图就是安全的。

3. **跨异步边界时复制或转移所有权。**

```cpp
auto owned_body = std::make_shared<std::string>(body);
enqueue([owned_body] {
    ParsedMessage parsed = parse(*owned_body);
    use(parsed);
});
```

或者直接解析成拥有数据的结构：

```cpp
struct ParsedMessage {
    std::string type;
    std::string content;
};
```

4. **引用成员改为值成员或明确的所有权类型。**

```cpp
struct Session {
    std::string token;
};
```

如果确实要共享，使用 `std::shared_ptr`，并保证被引用对象比 `Session` 活得久；如果只是观察，使用 `std::weak_ptr` 并在使用前 `lock()`。

5. **成员顺序按依赖关系排列。**

依赖者先声明还是后声明，取决于构造和析构的方向：

- 构造按声明顺序，析构按声明逆序。
- 如果 `A` 的析构函数要使用 `B`，那么 `B` 必须比 `A` 晚析构，也就是 `B` 要声明在 `A` 前面。
- 如果 `A` 的构造函数要使用 `B`，那么 `B` 必须比 `A` 早构造，也就是 `B` 要声明在 `A` 前面。

这两个条件通常可以同时满足：**把被依赖的对象声明在前面，把依赖者声明在后面。**

### 步骤 6：用 Clang AST dump 看隐式临时对象

节点 sourceStudy 建议：对照 cppreference 的临时对象规则，使用 Clang AST dump 查看隐式临时对象与析构位置。装好 Clang 后可以运行：

```bash
clang++ -std=c++17 -Xclang -ast-dump -fsyntax-only lifetime_ext.cpp \
  | grep -E 'MaterializeTemporaryExpr|CXXBindTemporaryExpr|CXXTemporaryObjectExpr'
```

可以重点观察：

- `CXXTemporaryObjectExpr`：显式创建的临时对象，例如 `Tracer("returned temporary")`。
- `MaterializeTemporaryExpr`：prvalue 被物化为临时对象的位置。
- `CXXBindTemporaryExpr`：临时对象绑定到析构清理的位置。

AST dump 的输出通常很大，建议先用 `grep` 过滤，再对照源码行号看“临时对象在哪里创建、在哪里注册析构”。这比只背规则更能建立直觉。

### 步骤 7：一键验证

可以把下面的脚本保存为 `test.sh`，然后执行 `chmod +x test.sh && ./test.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

CXX=${CXX:-g++}
BASE=(-std=c++17 -Wall -Wextra -g -O0)
ASAN=(-fsanitize=address -fno-omit-frame-pointer)

$CXX "${BASE[@]}" -Wpedantic -Werror nested_order.cpp -o nested_order
./nested_order > nested_order.out
grep -q "+ Base" nested_order.out
grep -q -- "- Base" nested_order.out

$CXX "${BASE[@]}" -Wpedantic -Werror lifetime_ext.cpp -o lifetime_ext
./lifetime_ext > lifetime_ext.out
grep -q -- "- returned temporary" lifetime_ext.out

$CXX "${BASE[@]}" "${ASAN[@]}" return_ref.cpp -o return_ref 2> return_ref.log
cat return_ref.log
./return_ref > return_ref.out

$CXX "${BASE[@]}" "${ASAN[@]}" view_dangling.cpp -o view_dangling
./view_dangling safe > view_safe.out

set +e
./view_dangling scope > view_scope.out 2>&1
scope_status=$?
./view_dangling temporary > view_temp.out 2>&1
temp_status=$?
set -e

if [ "$scope_status" -eq 0 ] || [ "$temp_status" -eq 0 ]; then
    echo "expected ASan failures for dangling string_view"
    exit 1
fi

grep -q "AddressSanitizer" view_scope.out
grep -q "AddressSanitizer" view_temp.out

echo "all practice19 checks passed"
```

看到 `all practice19 checks passed` 即完成基本实践。注意：`return_ref.cpp` 是故意保留 UB 的演示，真实项目里不能保留这样的代码；它只用于观察编译警告和析构日志。

## 5. 自测题

### 题 1：存储期与生命周期

下面四个对象分别是什么存储期？它们大致在什么时候构造、什么时候析构？

```cpp
struct Loud {
    std::string name;
    explicit Loud(std::string n) : name(std::move(n)) {}
    ~Loud() {}
};

Loud g{"global"};

void f() {
    Loud a{"automatic"};
    static Loud s{"static local"};
    thread_local Loud t{"thread local"};
}
```

**答案：**

- `g` 是静态存储期。它在 `main` 之前（或程序启动阶段的动态初始化中）构造，在程序退出阶段析构。
- `a` 是自动存储期。每次调用 `f` 时构造，离开 `f` 的作用域时析构；如果 `f` 抛异常，栈展开也会析构它。
- `s` 是静态存储期。第一次执行到声明时构造，之后调用 `f` 不会重复构造；在程序退出阶段析构。C++11 起局部静态变量的初始化是线程安全的。
- `t` 是线程存储期。每个线程首次使用前构造自己的实例，线程退出时析构；主线程的实例在程序退出阶段析构。它不是跨线程共享变量。

补充：静态对象的析构顺序在同一翻译单元内通常与构造相反，跨翻译单元的顺序未指定。不要依赖不同 `.cpp` 中静态对象的相对析构顺序。

### 题 2：哪些引用延长是安全的？

下面几种写法，哪些是安全的？哪些会悬垂？说明理由。

```cpp
std::string make();
const std::string& make_ref();

const std::string& a = make();
const std::string& b = make_ref();
std::string_view c = make();

void take(const std::string&);
take(make());

struct S {
    const std::string& r;
    S() : r(std::string("x")) {}
};
S s;
```

**答案：**

- `const std::string& a = make();` **安全**。`make` 按值返回 `std::string`，返回的临时对象直接绑定到局部引用 `a`，生命周期延长到 `a` 离开作用域。
- `const std::string& b = make_ref();` **不一定安全**。`make_ref` 返回的是引用，不是值。它内部如果返回的是临时对象或局部对象的引用，那个对象在 `make_ref` 的 `return` 表达式结束时就已经销毁；`b` 只是复制了一个悬垂引用。引用延长不会穿过函数返回。如果 `make_ref` 返回的是静态对象或全局对象的引用，则 `b` 在那些对象存活期间是安全的，但这是对象本身的寿命，不是“引用延长”。
- `std::string_view c = make();` **不安全**。`std::string_view` 不是引用，不触发引用延长。`make()` 返回的临时字符串在完整表达式结束时销毁，`c` 悬垂。
- `take(make());` **安全**。临时字符串活到包含这次函数调用的完整表达式结束，整个 `take` 调用期间都有效。前提是 `take` 不把引用或视图保存到调用之后。
- `S s;` **不安全**。引用成员 `r` 在构造函数初始化列表中绑定到临时 `std::string("x")`，临时对象不会延长到 `S` 对象的寿命。构造调用所在完整表达式结束后 `s.r` 就可能悬垂。应把成员改成 `std::string` 值类型，或用明确的所有权类型。

记忆方法：**延长只发生在“局部引用变量直接绑定临时对象”这一种形态；函数返回、函数参数、引用成员、指针、视图都不在延长范围内。**

### 题 3：成员构造析构顺序

下面代码的输出顺序是什么？`Derived` 的初始化列表写成 `b` 在前、`a` 在后，会改变实际顺序吗？如果 `a` 的析构函数要使用 `b`，当前声明顺序安全吗？

```cpp
struct Base {
    Tracer t{"Base"};
    Base() { std::cout << "Base body\n"; }
    ~Base() { std::cout << "~Base body\n"; }
};

struct Derived : Base {
    Tracer a{"a"};
    Tracer b{"b"};

    Derived() : b{"b-init"}, a{"a-init"} {
        std::cout << "Derived body\n";
    }

    ~Derived() {
        std::cout << "~Derived body\n";
    }
};
```

**答案：**

实际构造顺序是：

1. `Base::t`
2. `Base` 构造函数体
3. `Derived::a`
4. `Derived::b`
5. `Derived` 构造函数体

析构顺序是：

1. `~Derived` 函数体
2. `Derived::b`
3. `Derived::a`
4. `~Base` 函数体
5. `Base::t`

初始化列表写成 `b{"b-init"}, a{"a-init"}` **不会改变实际顺序**。成员初始化顺序始终由声明顺序决定：`a` 在 `b` 前声明，所以 `a` 先构造。GCC 的 `-Wreorder`（Clang 对应 `-Wreorder-ctor`）会警告初始化列表顺序与声明顺序不一致。

如果 `a` 的析构函数要使用 `b`，当前声明顺序**不安全**。析构按声明逆序进行：`b` 先析构，`a` 后析构。`a` 析构时 `b` 已经销毁，访问它就是 use-after-free。应把被依赖的 `b` 声明在 `a` 前面，这样 `b` 先构造、后析构，`a` 析构时 `b` 还活着。换句话说：**被依赖者声明在前，依赖者声明在后。**

## 6. 推荐资源

- **对象生命周期与存储期：参考文档 / 原始资料**
  - 链接：<https://en.cppreference.com/w/cpp/language/lifetime.html>
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：基础
  - 说明：重点阅读自动、静态和线程存储期与临时对象与引用延长，用于核对本节点实验的语义和边界。读的时候把示例中的临时对象按“完整表达式结束”“局部引用延长”“函数返回不延长”三类拆开，再对照本课的 `lifetime_ext.cpp`、`return_ref.cpp` 和 `view_dangling.cpp`，确认自己对生命周期边界的理解一致。
- **C++ Core Guidelines · Lifetime safety**
  - 链接：<https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#S-lifetime>
  - 来源：ISO C++ Core Guidelines
  - 语言：英文
  - 类型：工程指南
  - 难度：进阶
  - 说明：对照悬空指针与视图的规则，审查后台回调的生命周期。重点看“不要返回局部对象的引用/指针”“不要让视图比拥有者活得更久”“明确所有权”等建议，并尝试用这些规则检查本课的 `on_message`、`parse` 和连接对象示例。

## 7. 验收回扣

完成本课后，用下面这张清单做最终自检：

| 验收项 | 你应该能给出的证据 |
|--------|-------------------|
| 能解释引用延长的适用范围 | 能说出“局部 `const T&` / `T&&` 直接绑定临时对象才延长”，并能举出函数返回引用、引用成员、`std::string_view` 三个反例 |
| 能画出成员与宿主对象的生命周期 | 能画出基类先于成员构造、成员按声明顺序构造、析构函数体先于成员析构、成员按声明逆序析构、基类最后析构的时间轴 |
| 能解释自动、静态和线程存储期 | 能说明 `Loud` 实验中 `global`、`static-local`、`thread-local`、`automatic` 的构造/析构时机 |
| 能独立完成节点实践 | 能运行 `nested_order`、`lifetime_ext`、`return_ref`、`view_dangling`，并解释输出和 ASan 报告 |
| 能定位 `string_view` 悬垂 | 能说明视图不拥有数据、不延长寿命，并能给出“复制数据”或“让拥有者活得更久”的修复方案 |
| 能审查后台生命周期问题 | 能指出回调参数、异步任务、线程池上下文、连接对象成员析构顺序中的风险，并给出所有权/生命周期上的修改建议 |

最后再回到节点 summary：

> 对象生命周期从满足初始化条件开始，到析构或存储被释放或复用时结束；存储存在不代表对象可用。

以及节点 pitfalls：

> 绑定到函数返回引用不会自动延长原临时对象寿命。

只要你能用本课的 Tracer 实验、`string_view` 悬垂实验和成员顺序图把这两句话讲清楚，本节点就算真正掌握了。
