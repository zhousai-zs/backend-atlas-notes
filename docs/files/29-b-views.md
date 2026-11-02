# 第 29/208 课：非拥有视图与边界安全

## 课程定位

| 项目 | 内容 |
|---|---|
| 节点 | `b-views` / Views and Spans |
| 所属领域 | B |
| 阶段 | 2 |
| 子类 | 生命周期与所有权 |
| 难度 | 基础 |
| 前置节点 | `b-lifetime`、`b-stl` |

节点摘要：`string_view` 和 `span` 表达一段非拥有数据；它们保留长度但不拥有底层存储。

节点动机：减少协议解析拷贝，同时避免传统裸指针长度分离。

---

## 1. 学习目标与为什么后端要懂

学完本课，你应当能够：

1. 解释 `std::string_view` 与零结尾之间的关系，不把“视图存在”误认为“字符串一定以 `'\0'` 结尾”。
2. 解释 `std::span` 为什么只能表示连续范围，以及动态长度和静态长度视图的区别。
3. 说清楚“借用寿命”和“底层存储失效”是两件事。
4. 独立完成：用 `std::span` 解析定长协议头，返回独立的值类型，并拒绝截断输入。
5. 识别临时 `std::string` 产生的悬垂 `string_view`，并知道什么时候应该复制成拥有所有权的类型。

为什么后端工程师要懂这些？因为协议解析、HTTP 请求处理、消息回调、日志切分这些高频路径，往往面对的是已经存在的一块缓冲区。如果每次都复制，会增加分配和内存带宽开销；如果只用裸指针加长度，又很容易出现长度传错、越界读、指针和长度不同步的问题。`string_view` 和 `span` 把“指针 + 长度”封装成一个类型，减少拷贝，同时让边界检查更自然。

但零拷贝不是免费午餐。视图不拥有数据，它只“借用”一段内存。视图对象还活着，不代表它指向的内存还活着。对即将进入腾讯微信公众号后台开发的工程师来说，这一点尤其重要：请求体 buffer 可能被框架复用，异步任务可能晚于请求处理函数执行，回调消息可能来自临时拼接的字符串。把 `string_view` 或 `span` 存进异步任务、缓存或成员变量之前，必须先确认底层数据的生命周期。

---

## 2. 从直觉到原理

### 2.1 `string_view` 与零结尾

**直觉。** `std::string` 像一栋自己拥有的房子，里面存着字符；`std::string_view` 像一张写着“从哪个地址开始、看多少个字符”的纸条。纸条本身不建房子，也不保证房子还在。

**原理。** `std::string_view` 是一段字符序列的非拥有视图，通常由“指向首字符的指针 + 长度”组成。它不分配内存，不拥有字符，也不负责释放。它的长度是权威信息，视图内部可以包含 `'\0'`，`size()` 仍然准确。

关键点是：`string_view` 不保证零结尾。

- 从字符串字面量构造时，字面量本身有结尾 `'\0'`，但视图的长度不包含这个结尾。
- 从 `const char*` 构造时，构造函数需要先找到结尾，因此输入必须是零结尾的 C 字符串。
- 从 `std::string`、指针加长度、`substr()` 等构造时，得到的视图不保证 `data()[size()] == '\0'`。
- 不能因为 `data()` 返回 `const char*`，就把它直接当成 C 字符串传给 `printf("%s")`、`strlen()` 或其他要求零结尾的 API。

```cpp
#include <string>
#include <string_view>

std::string_view dangling() {
    std::string s = "wechat";
    return std::string_view(s);
}

std::string owning() {
    std::string s = "wechat";
    return s;
}
```

`dangling()` 返回后，局部 `s` 已经销毁，返回的视图指向失效内存。`owning()` 返回的是拥有所有权的 `std::string`，值被移动或复制出来，生命周期由调用方管理。

如果确实需要把视图交给 C API，应该显式创建拥有所有权的字符串，并保证这个字符串在使用期间存活：

```cpp
#include <string>
#include <string_view>

void log_message(const char* text);

void write_log(std::string_view sv) {
    std::string tmp(sv);
    log_message(tmp.c_str());
}
```

在微信公众号后台场景里，解析 HTTP 头、URL 路径、JSON 字段名时，同步处理阶段可以直接使用 `string_view` 避免拷贝；一旦要把字段交给异步任务，就应该复制成 `std::string`。不要假设请求 buffer 会一直存在，也不要假设字段后面一定有 `'\0'`。

### 2.2 `span` 与连续范围

**直觉。** `std::span` 像一张“从某个地址开始，连续看 N 个元素”的窗口。它适合数组、`std::vector`、`std::array` 这类连续存储，不适合链表这种元素分散的结构。

**原理。** `std::span<T>` 是 C++20 引入的非拥有连续范围视图，通常也是“指针 + 长度”。`std::span<const T>` 表示只读视图。它有两种长度形式：

- `std::span<T>`：动态长度，运行时确定。
- `std::span<T, N>`：静态长度，长度是类型的一部分。

`span` 的前提是连续范围。`std::vector`、`std::array`、C 数组、`std::string` 的字符存储都是连续的；`std::list` 不是连续范围，不能直接构造 `span`；`std::deque` 的存储也不是单块连续内存，不能当作 `span`。

```cpp
#include <array>
#include <cstdint>
#include <span>
#include <vector>

void consume(std::span<const std::uint8_t> bytes);

void demo() {
    std::array<std::uint8_t, 4> fixed{1, 2, 3, 4};
    consume(fixed);

    std::vector<std::uint8_t> dynamic{1, 2, 3, 4};
    std::span<const std::uint8_t> view(dynamic);
    consume(view);
}
```

`span` 保留了长度，所以不再需要把“指针”和“长度”两个参数分开传。对于协议解析，这一点很重要：长度跟着视图走，接口更难出现“指针传了 A，长度传了 B”的错误。

但 `span` 不拥有数据，也不自动检查底层存储是否有效。它只负责表达“这里有一段连续内存”。边界检查仍然要由使用方完成，尤其是 `first()`、`subspan()`、下标访问这些操作都有前置条件。

对于定长协议头，可以在确认输入足够长之后，再收窄成静态长度视图：

```cpp
#include <cstdint>
#include <optional>
#include <span>

constexpr std::size_t kHeaderSize = 12;

std::optional<std::span<const std::uint8_t, kHeaderSize>>
take_header(std::span<const std::uint8_t> input) {
    if (input.size() < kHeaderSize) {
        return std::nullopt;
    }
    return input.first<kHeaderSize>();
}
```

注意顺序：先检查动态视图的长度，再调用 `first<kHeaderSize>()`。如果输入可能来自网络，不要直接用不可信长度构造静态长度视图。

### 2.3 借用寿命和失效

**直觉。** 视图是借来的钥匙。钥匙还在你手里，不代表房子还在，也不代表房子里的家具没有被搬走。

**原理。** 需要区分三个生命周期：

1. 视图对象本身的寿命：`string_view` 或 `span` 这个“指针 + 长度”结构什么时候销毁。
2. 底层存储的寿命：原来的 `std::string`、`std::vector`、数组或网络 buffer 什么时候销毁。
3. 底层存储的有效性：存储是否被重新分配、清空、覆盖或复用。

视图有效，必须同时满足：底层存储还活着，并且没有被会使视图失效的操作破坏。常见失效来源包括：

- 局部 `std::string` 或 `std::vector` 在函数返回后销毁。
- `std::vector` 扩容导致元素搬家，原来的 `span` 悬垂。
- `std::string` 的某些修改操作导致缓冲区重新分配。
- 容器执行 `clear()`、`erase()`、重新赋值等操作。
- 网络框架复用了请求 buffer，旧的视图仍然指向同一块地址，但内容已经变成下一个请求的数据。
- 异步任务捕获了视图，但原 buffer 在任务执行前已经被释放或复用。

```cpp
#include <cstdint>
#include <span>
#include <vector>

std::span<const std::uint8_t> view_of(const std::vector<std::uint8_t>& buf) {
    return std::span<const std::uint8_t>(buf);
}

void demo() {
    std::vector<std::uint8_t> buf{1, 2, 3};
    auto s = view_of(buf);
    buf.push_back(4);
    (void)s;
}
```

`buf.push_back(4)` 可能触发扩容。扩容后元素搬到新地址，原来的 `s` 仍然保存旧地址和旧长度，继续使用就是悬垂访问。

因此，视图最适合“短生命周期、同步使用”的场景：函数参数、一次解析过程、立即消费的只读窗口。跨线程、跨协程、跨请求缓存、长期保存时，优先使用拥有所有权的类型。

可以用三个问题做快速检查：

1. 长度够不够？访问前是否检查了 `size()`？
2. 底层存储还活着吗？视图会不会逃出原来的作用域？
3. 底层存储会不会被修改或复用？异步任务执行时，原 buffer 是否仍然有效？

---

## 3. 常见误区与设计权衡

### 3.1 常见误区

**误区一：`string_view` 一定以零结尾。**  
不对。`string_view` 只保证长度，不保证 `data()[size()]` 是 `'\0'`。需要 C 字符串时，显式构造 `std::string`。

**误区二：视图对象活着，底层内存就有效。**  
不对。视图只是指针和长度。底层字符串、容器或 buffer 销毁后，视图仍然可能“看起来正常”，但访问它是未定义行为。

**误区三：临时 `std::string` 可以安全地转成 `string_view`。**  
不对。临时对象在完整表达式结束后销毁，视图随即悬垂。

```cpp
#include <string>
#include <string_view>

void use(std::string_view sv);

void bad() {
    std::string_view sv = std::string("wechat");
    use(sv);
}
```

`std::string("wechat")` 产生的临时对象在语句结束后销毁，`sv` 指向失效内存。

**误区四：`span` 可以指向任意容器。**  
不对。`span` 要求连续范围。链表、哈希表、`std::deque` 等不能直接当作连续字节块使用。

**误区五：`span` 会自动做边界检查。**  
不对。`span` 让长度和指针绑定在一起，但 `first()`、`subspan()`、下标访问仍然有前置条件。网络输入必须先检查长度。

**误区六：解析完协议头后，把 payload 的 `span` 直接丢进异步队列。**  
这很危险。异步任务执行时，原始网络 buffer 可能已经被复用或释放。要么复制 payload，要么确保 buffer 的所有权被转移到任务中。

### 3.2 设计权衡

零拷贝借用要求更严格的寿命管理，复制可能更简单可靠。实际设计中可以这样取舍：

- **函数参数优先用视图。** 例如 `void handle(std::string_view name)`、`void parse(std::span<const std::uint8_t> packet)`，调用方可以传 `std::string`、字面量、数组或 vector，不强制复制。
- **返回值优先用拥有所有权的类型。** 如果函数内部创建了数据，返回 `std::string` 或 `std::vector`，不要返回指向局部对象的视图。
- **API 边界写清楚寿命契约。** 如果确实要返回视图，必须在接口名、注释或文档中说明它借用的是哪个对象、能活多久。
- **同步热路径用视图，异步路径用拷贝。** 公众号后台处理回调时，同步解析可以直接借用请求 buffer；投递到线程池、协程或消息队列时，把需要的数据复制成独立对象。
- **小数据复制往往更划算。** 几十字节的字段复制成本很低，能换来更简单的生命周期和更少的悬垂风险。
- **定长头可以用静态长度，但入口仍接收动态长度。** 外部输入先检查 `size()`，确认足够后再收窄为 `std::span<const std::uint8_t, N>`。

---

## 4. 动手实践：用 `span` 解析定长协议头

### 4.1 实践目标与验收标准

目标：用 `std::span` 解析一个 12 字节的定长协议头，返回独立的 `Header` 值，并对截断输入返回失败。

验收标准：

- 输入长度小于 12 字节时，解析函数返回 `std::nullopt`，不读取越界内存。
- 解析结果 `Header` 是独立值，不包含指向输入 buffer 的 `span` 或 `string_view`。
- 不假设输入以零结尾。
- 能说明原始 buffer 被修改、扩容或释放后，为什么不能继续使用旧视图。
- 能识别临时 `std::string` 产生的悬垂 `string_view`。

### 4.2 协议头布局

本实践使用一个大端序的 12 字节协议头：

| 偏移 | 长度 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 2 | `magic` | 固定值 `0x5758`，即字符 `WX` |
| 2 | 1 | `version` | 协议版本 |
| 3 | 1 | `type` | 消息类型 |
| 4 | 4 | `body_len` | 消息体长度，大端序 |
| 8 | 4 | `seq` | 序列号，大端序 |

总长度：`2 + 1 + 1 + 4 + 4 = 12` 字节。

### 4.3 可执行步骤

**步骤 1：定义常量与结果类型。**  
定义 `kHeaderSize = 12`、`kMagic = 0x5758`，以及只包含普通整数成员的 `Header` 结构体。不要让 `Header` 持有 `std::span` 或 `std::string_view`。

**步骤 2：写大端序读取函数。**  
按字节移位读取 16 位和 32 位整数。不要直接用 `reinterpret_cast` 把字节指针转成整数指针，避免对齐和严格别名问题。

**步骤 3：实现解析函数。**  
函数签名接收 `std::span<const std::uint8_t>`，返回 `std::optional<Header>`。先判断 `input.size() < kHeaderSize`，不足则返回 `std::nullopt`。确认长度足够后，再用 `input.first<kHeaderSize>()` 收窄为静态长度视图。

**步骤 4：解析字段并校验。**  
依次读取 `magic`、`version`、`type`、`body_len`、`seq`。校验 `magic` 是否符合协议；不符合时返回 `std::nullopt`。所有字段写入独立的 `Header` 值。

**步骤 5：校验消息体长度。**  
如果调用方还要处理消息体，检查 `body_len <= input.size() - kHeaderSize`。注意先确认 `input.size() >= kHeaderSize`，再做减法，避免无符号下溢。

**步骤 6：处理返回结果。**  
调用方检查 `optional` 是否有值。需要消息体时，用 `input.subspan(kHeaderSize)` 取得剩余范围，并且只在当前同步处理过程中使用；如果要异步处理，复制成 `std::vector<std::uint8_t>` 或 `std::string`。

**步骤 7：补充测试。**  
至少覆盖：完整 12 字节头、0 到 11 字节的截断输入、错误 `magic`、`body_len` 超过实际剩余长度。截断输入必须全部被拒绝。

### 4.4 参考实现

```cpp
#include <array>
#include <cstdint>
#include <optional>
#include <span>

constexpr std::size_t kHeaderSize = 12;
constexpr std::uint16_t kMagic = 0x5758;

struct Header {
    std::uint16_t magic;
    std::uint8_t version;
    std::uint8_t type;
    std::uint32_t body_len;
    std::uint32_t seq;
};

std::uint16_t read_u16_be(const std::uint8_t* p) {
    return static_cast<std::uint16_t>(
        (static_cast<std::uint16_t>(p[0]) << 8) |
        static_cast<std::uint16_t>(p[1]));
}

std::uint32_t read_u32_be(const std::uint8_t* p) {
    return (static_cast<std::uint32_t>(p[0]) << 24) |
           (static_cast<std::uint32_t>(p[1]) << 16) |
           (static_cast<std::uint32_t>(p[2]) << 8) |
           static_cast<std::uint32_t>(p[3]);
}

std::optional<Header> parse_header(std::span<const std::uint8_t> input) {
    if (input.size() < kHeaderSize) {
        return std::nullopt;
    }

    auto raw = input.first<kHeaderSize>();
    Header header{};
    header.magic = read_u16_be(raw.data());
    header.version = raw[2];
    header.type = raw[3];
    header.body_len = read_u32_be(raw.data() + 4);
    header.seq = read_u32_be(raw.data() + 8);

    if (header.magic != kMagic) {
        return std::nullopt;
    }

    return header;
}
```

这段实现有几个关键点：

- 输入是动态长度 `std::span`，边界检查在入口完成。
- `first<kHeaderSize>()` 只在长度检查之后调用，满足前置条件。
- 返回的是 `Header` 值，不把外部 buffer 的视图带出函数。
- 所有多字节字段按大端序读取，不依赖主机字节序。
- 不假设输入以零结尾，因为解析只依赖长度和偏移。

### 4.5 测试与验收代码

```cpp
#include <array>
#include <cassert>
#include <cstdint>
#include <span>

void test_parse_header() {
    std::array<std::uint8_t, 12> full{
        0x57, 0x58, 0x01, 0x02,
        0x00, 0x00, 0x00, 0x10,
        0x00, 0x00, 0x00, 0x2A
    };

    auto header = parse_header(full);
    assert(header.has_value());
    assert(header->magic == 0x5758);
    assert(header->version == 1);
    assert(header->type == 2);
    assert(header->body_len == 16);
    assert(header->seq == 42);

    for (std::size_t n = 0; n < 12; ++n) {
        std::span<const std::uint8_t> truncated(full.data(), n);
        assert(!parse_header(truncated).has_value());
    }

    std::array<std::uint8_t, 12> wrong_magic{};
    assert(!parse_header(wrong_magic).has_value());
}
```

这段测试覆盖了完整头、全部截断长度和错误 `magic`。其中“全部截断长度”正是边界安全的核心：0 字节、1 字节、……、11 字节都不能被当成合法头读取。

如果还要处理消息体，可以增加一个长度校验函数：

```cpp
bool body_len_is_valid(const Header& header,
                       std::span<const std::uint8_t> input) {
    if (input.size() < kHeaderSize) {
        return false;
    }
    return header.body_len <= input.size() - kHeaderSize;
}
```

先检查头长度，再做无符号减法，避免 `input.size() - kHeaderSize` 下溢。

### 4.6 后台场景加练

把上面的解析逻辑放到一个模拟请求处理函数中：

1. 从 `std::vector<std::uint8_t>` 读取完整请求包。
2. 用 `std::span<const std::uint8_t>` 调用 `parse_header`。
3. 如果解析失败，记录“协议头截断或 magic 错误”，直接返回错误。
4. 如果 `body_len` 大于剩余字节数，返回“消息体不完整”，不要继续解析。
5. 如果消息体需要交给异步任务，复制成拥有所有权的 `std::vector<std::uint8_t>`，不要只传 `span`。

这个练习的重点不是协议本身，而是建立习惯：入口用视图减少拷贝，出口用拥有所有权的值保证安全。

### 4.7 用 ASan 亲眼看到悬垂视图

课里的两个失效场景可以直接跑出来（g++ 13，`-std=c++20 -fsanitize=address,undefined`）：

```bash
# dangling.cpp: std::string_view v = make_name();  // 临时 string 本行结束即销毁
# invalidate.cpp: std::span<int> s(ids); ids.push_back(4); s[0];
g++ -std=c++20 -g -fsanitize=address,undefined dangling.cpp -o dangling && ./dangling
g++ -std=c++20 -g -fsanitize=address,undefined invalidate.cpp -o invalidate && ./invalidate
```

两者都会报 `heap-use-after-free`。不加 ASan 时程序往往“正常”输出，这正是视图悬垂最危险的地方。

另一个小实验能看到 `string_view` 不以零结尾：对 `"GET /api/user?id=42 HTTP/1.1"` 取 `substr(4, 15)` 得到 `/api/user?id=42`，用 `printf("%.*s", (int)v.size(), v.data())` 打印正确，而 `printf("%s", v.data())` 会一路打印到原字符串末尾，输出 `/api/user?id=42 HTTP/1.1`。

---

## 5. 自测题

### 题目 1

下面代码有什么问题？应该如何修改？

```cpp
#include <string>
#include <string_view>

void use(std::string_view sv);

void bad() {
    std::string_view sv = std::string("wechat");
    use(sv);
}
```

### 题目 2

已知 `std::string_view sv = "abc";`，能否直接把 `sv.data()` 传给要求零结尾字符串的 C API？为什么？

### 题目 3

下面代码中，`s` 在 `push_back` 之后还可以继续使用吗？为什么？

```cpp
#include <cstdint>
#include <span>
#include <vector>

void demo() {
    std::vector<std::uint8_t> buf{1, 2, 3};
    std::span<const std::uint8_t> s(buf);
    buf.push_back(4);
}
```

### 答案

**答案 1。**  
有问题。`std::string("wechat")` 是临时对象，在完整表达式结束后销毁，`sv` 随即悬垂。修改方式是让拥有所有权的字符串活到使用结束，或者直接使用 `std::string`：

```cpp
void good() {
    std::string owner = "wechat";
    use(owner);
}
```

**答案 2。**  
不能仅凭 `string_view` 保证。字符串字面量本身有结尾 `'\0'`，但 `string_view` 的契约不保证 `data()[size()]` 是 `'\0'`，尤其是 `substr()`、指针加长度构造、来自非零结尾 buffer 的视图。应显式构造 `std::string`，或确认源数据确实零结尾。

**答案 3。**  
不能假设可以继续使用。`push_back` 可能触发 `std::vector` 扩容，元素搬到新地址，原来的 `span` 指向旧内存，继续访问是未定义行为。应在修改容器后重新获取 `span`，或者提前预留足够容量，并且不要跨异步边界保存这个视图。

---

## 6. 推荐资源

- [非拥有视图与边界安全：参考文档 / 原始资料](https://en.cppreference.com/w/cpp/container/span.html)  
  来源：`en.cppreference.com`；语言：英文；类型：文档；难度：进阶；免费。  
  阅读重点：`string_view` 与零结尾、`span` 与连续范围，用于核对本节点实验的语义和边界。

核对资料时，建议沿“定义、示例、错误处理路径”三部分阅读，并记录适用版本与边界条件。特别是区分 C++17 的 `string_view` 与 C++20 的 `span`，以及 `first()`、`subspan()` 等操作的前置条件。
