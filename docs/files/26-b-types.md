# 第 26/208 课：类型系统、转换与接口设计

> 领域 B · 阶段 2 · 难度：基础 ｜ 子类：语法与类型 ｜ 前置：b-template

**一句话**：类型系统限制哪些操作合法；强类型接口把单位、状态和所有权写进签名，让错误组合尽量在编译期失败；但运行时输入和生命周期契约仍要显式检查。

## 一、学习目标 & 为什么后端要懂

学完你应该能：
1. 解释显式转换与 `explicit`；
2. 独立完成：为超时、字节数和请求 ID 设计不可混用的类型接口。

验收标准（节点原文）：
- 非法单位组合不能编译；
- 能解释 `reinterpret_cast` 为何不构造目标对象。

**为什么后端要懂**：公众号后台的请求上下文里经常同时出现 `timeout`、`body_size`、`request_id`、`msg_id`，它们在配置、日志和协议里往往只是裸整数或字符串。`timeout=3` 是秒还是毫秒？`size=4096` 是字节还是 KB？两个都叫 `id` 的字符串，谁会拿到用户 ID、谁会拿到请求 ID？如果函数签名只写 `int64_t` / `std::string`，编译器帮不了你，错误可能到线上才以“超时早了三倍”“查错用户”“把订单 ID 当用户 ID”的形式出现。类型系统的作用，就是把这些语义放到接口上，让错误组合在编译期暴露。

## 二、从直觉到原理

类型系统是编译器对“哪些值能参与哪些操作”的检查。它知道 `int` 和 `std::string` 不同，却不知道两个 `int64_t` 一个是毫秒、一个是字节；强类型的任务就是补上后者。接口还能表达所有权：`std::unique_ptr<T>` 按值传递表示转移，`const T&` 表示只读借用。下面聚焦节点要求的三个核心点。

以下短代码为突出类型关系，省略重复的 `#include`；动手实践部分会给出完整文件。

### 1. 显式转换与 `explicit`

**直觉**：`int` 到 `double` 这类标准转换是语言内建的，通常不会丢语义；但用户自定义类型之间的转换如果也能隐式发生，就可能把“毫秒”悄悄变成“秒”，或者把裸数字直接塞进一个有业务含义的参数。

先看没有类型保护的接口：

```cpp
void send_request(int timeout_ms);

send_request(5);
```

调用点只写了 `5`，编译器不知道它代表 5 毫秒、5 秒还是 5 次重试。把单位做成类型后，接口就有了约束：

```cpp
class Milliseconds {
public:
    explicit constexpr Milliseconds(std::int64_t value) noexcept
        : value_(value) {}

    constexpr std::int64_t count() const noexcept { return value_; }

private:
    std::int64_t value_;
};

void send_request(Milliseconds timeout);

send_request(Milliseconds{5});
```

此时 `send_request(5)` 不再能通过编译：`Milliseconds` 的构造函数是 `explicit` 的，裸 `int` 不能隐式转换成 `Milliseconds`。调用点必须明确写出单位，代码审查时也更容易发现“这里传的到底是毫秒还是秒”。

`explicit` 的准确边界是：

- 它禁止**隐式转换**和**复制初始化**，例如 `Milliseconds t = 5;` 失败，`send_request(5)` 失败；
- 它不禁止**直接初始化**和**显式转换**，例如 `Milliseconds t{5};`、`send_request(Milliseconds{5})`、`static_cast<Milliseconds>(5)` 都可以；
- 它只控制“转换能不能悄悄发生”，**不校验值是否合理**。`Milliseconds{-1}` 仍然可以构造，除非你在构造函数或边界解析里检查范围。

`explicit` 也可以用在转换运算符上，避免对象被悄悄转成 `bool` 或其他类型：

```cpp
class Ready {
public:
    explicit operator bool() const noexcept { return true; }
};

if (Ready{}) {
}

bool b = Ready{};
```

`if` 是语境转换，允许调用 `explicit operator bool`；`bool b = Ready{}` 是复制初始化，不允许，因此第二行会编译失败。

**`static_cast` 与 `reinterpret_cast` 的关键区别**：

```cpp
std::int64_t raw_ms = 3000;
Milliseconds timeout{raw_ms};
auto n = static_cast<std::int64_t>(timeout.count());
```

上面的 `Milliseconds{raw_ms}` 和 `static_cast` 走的是**值转换/构造**：目标类型的对象会被构造出来，构造函数的规则和校验都会执行。

而 `reinterpret_cast` 只是重新解释指针、引用或整数的位模式，它**不调用构造函数，也不开始目标类型的对象生命周期**：

```cpp
std::uint64_t raw = 3000;
auto* p = reinterpret_cast<Milliseconds*>(&raw);
auto n = p->count();
```

`&raw` 指向的存储里只有一个 `std::uint64_t`，并没有 `Milliseconds` 对象。`reinterpret_cast` 只是把这个地址按 `Milliseconds*` 来解释；随后解引用并调用成员函数是未定义行为。即使两个类型大小相同、布局看起来一样，也不能把它当成“把字节构造成对象”的手段。需要按值转换时用构造或 `static_cast`；确实要按位重解释时，必须先确认对象表示、对齐、生命周期和别名规则，不能拿 `reinterpret_cast` 当序列化或构造工具。

### 2. `enum class` 与强类型包装

**直觉**：状态是有限集合，适合用枚举表达；数量和 ID 的底层表示可能都是整数或字符串，但它们有不同的语义，适合用强类型包装。两者解决的问题不同。

旧式 `enum` 的枚举名会泄漏到外层作用域，而且会隐式转成整数：

```cpp
enum DeliveryState { Pending, Sent, Failed };

int state = Pending;
```

这会让“状态”退化成魔法数字，也容易把两个不同枚举的值混在一起比较。`enum class` 把名字收进作用域，并取消隐式转整数：

```cpp
enum class DeliveryState : std::uint8_t {
    Pending,
    Sent,
    Failed
};

DeliveryState state = DeliveryState::Pending;
auto raw = static_cast<std::uint8_t>(state);
```

`enum class` 的好处是：

- 必须写 `DeliveryState::Pending`，不会和别的 `Pending` 冲突；
- 不能隐式转成 `int`，`int n = state;` 会编译失败；
- 不同 `enum class` 之间不能直接比较或赋值；
- 可以指定底层类型，便于和协议字段的宽度对应。

但 `enum class` 只适合表达**有限状态**。超时和字节数都是数量，不能靠枚举穷举；请求 ID 和用户 ID 都是字符串，也不能靠枚举区分。这时需要强类型包装：

```cpp
class Bytes {
public:
    explicit constexpr Bytes(std::uint64_t value) noexcept
        : value_(value) {}

    constexpr std::uint64_t count() const noexcept { return value_; }

private:
    std::uint64_t value_;
};

void configure(Milliseconds timeout, Bytes max_body);

configure(Bytes{4096}, Milliseconds{3000});
```

最后一行故意把参数顺序写反了，编译器会报“不能把 `Bytes` 转成 `Milliseconds`”，因为两个包装类型之间没有转换路径。`configure(3000, 4096)` 也会失败，因为两个构造函数都是 `explicit`。这就是“非法单位组合不能编译”的核心机制。

请求 ID 也能用同样方式包起来：

```cpp
class RequestId {
public:
    explicit RequestId(std::string value)
        : value_(std::move(value)) {
        if (value_.empty()) {
            throw std::invalid_argument("request id must not be empty");
        }
    }

    const std::string& str() const noexcept { return value_; }

private:
    std::string value_;
};
```

请求 ID 和用户 ID 都可能用 `std::string` 表示，但包装成不同类型后，把请求 ID 传给只接受用户 ID 的接口会直接编译失败。包装还能把“非空”这类不变量放进构造函数；不过构造函数目前只检查非空，格式校验仍要按实际协议补充。

设计时可以记住这个分工：

- **有限状态**用 `enum class`，例如 `Pending`、`Sent`、`Failed`；
- **数量、单位、ID**用强类型包装，例如 `Milliseconds`、`Bytes`、`RequestId`；
- 类型多时可以抽成带 Tag 参数的模板，但关键不是模板，而是**默认不提供跨类型构造和转换**。

### 3. `const` 正确性与重载

**直觉**：`const` 在接口上表达“只读借用”：这个函数不会通过这个引用修改对象。它让调用者更放心，也让编译器能检查出误写。

```cpp
class Frame {
public:
    char& operator[](std::size_t i) { return data_[i]; }
    const char& operator[](std::size_t i) const { return data_[i]; }

private:
    std::vector<char> data_{'x'};
};

Frame frame;
const Frame& view = frame;

frame[0] = 'A';
char ch = view[0];
```

`frame` 是非 `const` 对象，调用非 `const` 版本的 `operator[]`，返回可写的 `char&`；`view` 是 `const` 引用，只能调用 `const` 版本，返回 `const char&`。如果 `const` 版本返回了非 `const` 引用，类型系统就会被绕过，调用者可以修改一个本应只读的对象，所以不要这么做。

对后端接口来说，常见写法是：

```cpp
void audit(const RequestId& id, const Bytes& body);
```

`const RequestId&` 和 `const Bytes&` 表示只读借用，避免拷贝，也禁止函数内部改调用者的数据。注意，这种借用只在调用期间有效；如果函数把引用或 `std::string_view` 存起来，调用方仍需保证被引用对象的生命周期。

**重载**可以按 `const` 限定区分，也可以按强类型参数区分：

```cpp
void enqueue(Milliseconds delay);
void enqueue(Bytes payload);
```

因为 `Milliseconds` 和 `Bytes` 的构造都是 `explicit`，`enqueue(3000)` 不能悄悄选中某一个重载，调用点必须写清单位。重载不是越多越好：参数类型太接近、隐式转换太多，都会让重载决议变得难懂，甚至产生歧义。

**必须强调：`const` 不是并发同步机制。**

```cpp
struct Counter {
    int read() const { return value_; }
    void bump() { ++value_; }

private:
    int value_ = 0;
};
```

`read()` 是 `const` 成员函数，只说明“通过这个接口不修改成员”。如果一个线程调用 `read()`，另一个线程同时调用 `bump()`，仍然会发生数据竞争（data race）。`const` 不会插入锁、原子操作或内存屏障；`mutable` 允许 `const` 成员函数修改缓存，也不会自动带来线程安全。要并发访问，就使用互斥量、原子变量，或者让数据在发布后保持不可变。

## 三、常见误区与设计权衡

### 常见误区

1. **把 `const` 当同步机制。** `const` 是编译期的访问约束，不是运行时同步。一个 `const` 成员函数和另一个写操作并发执行，仍然可能读到撕裂的数据或触发未定义行为。
2. **以为 `explicit` 会校验值。** `explicit` 只阻止隐式转换；`Milliseconds{-1}`、`Bytes{0}` 是否合法，取决于构造函数或边界解析有没有做范围检查。
3. **用 `reinterpret_cast` 当构造或序列化工具。** 它不会构造目标对象，也不会开始生命周期；把网络字节直接 `reinterpret_cast` 成结构体再访问，常见结果是未定义行为、对齐问题和跨版本不兼容。
4. **随意 `static_cast` 枚举值。** `static_cast<DeliveryState>(99)` 可能得到一个没有对应枚举量的值。协议字段进入系统时，应显式映射并拒绝未知值，而不是假设所有整数都合法。
5. **强类型只包一层，边界却直接 `memcpy`。** 类型安全只在 C++ 接口内部有效；跨 JSON、Protobuf 或二进制协议时，必须逐字段序列化、反序列化并校验。
6. **从 `const` 方法返回内部可变引用。** 这会破坏只读承诺；应返回 `const&`、值，或者提供明确的非 `const` 重载。

### 设计权衡

**节点原文：强类型提高安全性，边界序列化仍需显式校验。**

- **安全性 vs 使用成本**：每个超时都写 `Milliseconds{3000}` 比裸 `3000` 啰嗦，但换来的是参数顺序、单位错误在编译期暴露。通常只在跨模块、跨进程的接口边界使用强类型，内部短小的局部变量不必全部包装。
- **编译期 vs 运行时**：类型系统能阻止 `Bytes` 和 `Milliseconds` 互换，却不能判断一个 `RequestId` 是否真的来自可信请求，也不能判断 `3000` 是否超过业务上限。**编译期约束可以提前阻止错误组合，但仍需明确运行时输入与生命周期契约。**
- **接口清晰 vs 重载/模板膨胀**：强类型参数让重载更清楚，但过多的包装类型和重载会增加样板代码、编译时间和调试成本。优先给真正容易混淆的语义建类型，而不是给每个 `int` 都套壳。
- **序列化与 ABI**：不要依赖包装类型的内存布局做网络协议或持久化。显式写出 `.count()`、`.str()`，读入时逐字段校验并重新构造；这样也不会把填充字节、字节序和版本差异带进协议。
- **`const` 正确性 vs 同步成本**：`const` 让接口更容易推理，但不替代并发设计。共享可变状态要有明确的同步策略；能做成不可变快照时，优先不可变。

## 四、动手实践：为超时、字节数和请求 ID 设计不可混用的类型接口

目标（节点原文）：为超时、字节数和请求 ID 设计不可混用的类型接口。

### 步骤 1：建目录

```bash
mkdir -p ~/practice26
cd ~/practice26
```

### 步骤 2：写类型边界 `types.h`

三个类型都只提供 `explicit` 构造和 `const` 访问器，不提供到裸类型的隐式转换。

```cpp
#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>

class Milliseconds {
public:
    explicit constexpr Milliseconds(std::int64_t value) noexcept
        : value_(value) {}

    constexpr std::int64_t count() const noexcept { return value_; }

private:
    std::int64_t value_;
};

class Bytes {
public:
    explicit constexpr Bytes(std::uint64_t value) noexcept
        : value_(value) {}

    constexpr std::uint64_t count() const noexcept { return value_; }

private:
    std::uint64_t value_;
};

class RequestId {
public:
    explicit RequestId(std::string value)
        : value_(std::move(value)) {
        if (value_.empty()) {
            throw std::invalid_argument("request id must not be empty");
        }
    }

    const std::string& str() const noexcept { return value_; }

private:
    std::string value_;
};

struct RequestBudget {
    RequestId id;
    Milliseconds timeout;
    Bytes max_body;
};
```

### 步骤 3：写接口 `api.h` 和 `api.cpp`

接口把三个语义不同的参数分开，请求 ID 只读借用，数量和字节数按值传递。

```cpp
#pragma once

#include "types.h"

void handle_request(const RequestId& id, Milliseconds timeout, Bytes max_body);
```

```cpp
#include "api.h"

#include <iostream>

void handle_request(const RequestId& id, Milliseconds timeout, Bytes max_body) {
    std::cout << "request_id=" << id.str()
              << " timeout_ms=" << timeout.count()
              << " max_body_bytes=" << max_body.count() << '\n';
}
```

### 步骤 4：写边界解析 `parse.h` 和 `parse.cpp`

外部输入仍然是裸整数，因此在进入类型系统之前必须显式校验；校验通过后再构造强类型。

```cpp
#pragma once

#include "types.h"

#include <cstdint>
#include <string>

RequestBudget parse_budget(std::string id,
                           std::int64_t timeout_ms,
                           std::uint64_t max_body_bytes);
```

```cpp
#include "parse.h"

#include <stdexcept>
#include <utility>

RequestBudget parse_budget(std::string id,
                           std::int64_t timeout_ms,
                           std::uint64_t max_body_bytes) {
    if (timeout_ms <= 0 || timeout_ms > 60'000) {
        throw std::invalid_argument("timeout out of range");
    }
    if (max_body_bytes == 0 || max_body_bytes > 8 * 1024 * 1024) {
        throw std::invalid_argument("body size out of range");
    }
    return RequestBudget{
        RequestId{std::move(id)},
        Milliseconds{timeout_ms},
        Bytes{max_body_bytes}
    };
}
```

这里的上限只是示例，实际值应按业务配置调整；重点是**先校验裸值，再构造类型**。

### 步骤 5：写正向用例 `main.cpp`

```cpp
#include "api.h"
#include "parse.h"

#include <exception>
#include <iostream>

int main() {
    try {
        RequestBudget budget = parse_budget("req-20261102-001", 3000, 4096);
        handle_request(budget.id, budget.timeout, budget.max_body);
    } catch (const std::exception& e) {
        std::cerr << "bad request: " << e.what() << '\n';
        return 1;
    }
}
```

### 步骤 6：写两个反向用例

`bad_swap.cpp` 交换超时和字节数的位置：

```cpp
#include "api.h"

int main() {
    RequestId id{"req-bad-001"};
    handle_request(id, Bytes{4096}, Milliseconds{3000});
}
```

`bad_raw.cpp` 直接传裸整数：

```cpp
#include "api.h"

int main() {
    RequestId id{"req-bad-002"};
    handle_request(id, 3000, 4096);
}
```

分别编译这两个文件，预期都失败：

```bash
g++ -std=c++17 -Wall -Wextra -Werror -c bad_swap.cpp -o /dev/null
g++ -std=c++17 -Wall -Wextra -Werror -c bad_raw.cpp -o /dev/null
```

第一步应看到 `Bytes` 不能转换成 `Milliseconds` 一类的错误；第二步应看到整数不能隐式转换成 `Milliseconds` 或 `Bytes`。编译失败在这里是**预期结果**，说明类型边界生效了。

### 步骤 7：编译并运行正向用例

```bash
g++ -std=c++17 -Wall -Wextra -Werror main.cpp api.cpp parse.cpp -o demo
./demo
```

预期输出：

```text
request_id=req-20261102-001 timeout_ms=3000 max_body_bytes=4096
```

### 步骤 8：验证运行时校验仍然必要

把 `main.cpp` 里的 `3000` 改成 `0`，重新运行。编译仍然通过，但 `parse_budget` 会抛出 `timeout out of range`，程序输出 `bad request` 并返回非零状态。这个实验说明：强类型解决了“单位混用”，但**数值是否在业务范围内**仍要靠运行时校验。

再做一次序列化检查：不要把整个 `RequestBudget` 用 `memcpy` 或 `reinterpret_cast` 直接发到网络。逐字段写出 `budget.id.str()`、`budget.timeout.count()`、`budget.max_body.count()`；读入时逐字段解析、校验，再重新构造三个类型。这样既不依赖对象布局，也能明确处理字节序、协议版本和非法值。

### 验收检查

- `bad_swap.cpp` 和 `bad_raw.cpp` 都编译失败；
- `main.cpp` 能正确输出请求预算；
- 能把 `3000` 改成 `0` 后观察运行时异常；
- 能用自己的话解释：`reinterpret_cast` 只解释地址/位模式，不构造目标类型对象。

## 五、自测题

**1. `Milliseconds` 的构造函数是 `explicit`，为什么 `handle_request(id, 3000, 4096)` 不能编译，但 `Milliseconds{-1}` 仍可能通过编译？这说明 `explicit` 保证了什么、没保证什么？**

<details><summary>答案</summary>
`explicit` 阻止裸整数隐式转换成 `Milliseconds`，所以函数调用和复制初始化失败；但直接初始化 `Milliseconds{-1}` 是显式构造，`explicit` 不会拦它。`explicit` 保证的是“转换必须写明”，不保证数值合法。负数超时、零字节、超范围值仍要在构造函数或边界解析中校验。
</details>

**2. `const Session& find(const RequestId&) const` 是否意味着多个线程可以安全地同时调用它和 `update()`？**

<details><summary>答案</summary>
不是。`const` 成员函数只是承诺通过该接口不修改对象，它不提供锁、原子性或内存屏障。如果另一个线程同时执行 `update()` 并写入共享数据，仍然会发生数据竞争。需要互斥量、原子变量或不可变快照等同步手段；另外，返回内部引用时还要注意解锁后的生命周期问题。
</details>

**3. 执行 `auto* p = reinterpret_cast<Milliseconds*>(&raw); p->count();`，为什么这不是把 `raw` 构造成 `Milliseconds`？正确的转换方式是什么？**

<details><summary>答案</summary>
`reinterpret_cast` 只把 `&raw` 这个地址按 `Milliseconds*` 解释，不调用构造函数，也不开始 `Milliseconds` 的对象生命周期。地址上本来没有 `Milliseconds` 对象，解引用并访问成员是未定义行为。正确做法是先按值转换或校验，再显式构造，例如检查范围后写 `Milliseconds{static_cast<std::int64_t>(raw)}`（`raw` 是 `uint64_t`，直接 `Milliseconds{raw}` 会因花括号禁止窄化而编译失败）；跨协议字节流应逐字段解析，而不是按位强转结构体。
</details>

## 六、推荐资源

- [类型系统、转换与接口设计：参考文档 / 原始资料](https://en.cppreference.com/w/cpp/language/implicit_conversion.html)（en.cppreference.com，英文文档，进阶，免费）：重点阅读显式转换与 `explicit`、`enum class` 与强类型包装，用于核对本节点实验的语义和边界。按节点的 sourceStudy 建议，沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。
