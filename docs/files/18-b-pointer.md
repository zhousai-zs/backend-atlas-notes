# 第 18/208 课：指针、引用与借用边界

> 节点 id：`b-pointer` · 域：B · 子类：语法与类型 · 难度：基础 · 前置：`b-syntax`
>
> 一句话：指针表示地址且可以为空，引用绑定到对象；两者通常都不表达所有权，使用时必须同时考虑对象能活多久。

## 1. 学习目标与为什么后端要懂

### 1.1 学习目标

完成本课后，你应该能够：

1. **解释空指针与解引用。** 说清指针里存的是什么、`nullptr` 表示什么、解引用空指针为什么会是未定义行为（UB）；能写出“先检查、再使用”的代码，并能用 AddressSanitizer（ASan）读懂空指针访问或悬垂访问的报告。
2. **独立完成节点实践。** 为字节缓冲区实现带边界检查的访问接口，构造悬垂引用，并用 `g++ -fsanitize=address` 编译运行、定位问题。
3. **分清 `const` 的位置。** 看到 `const std::uint8_t*`、`std::uint8_t* const`、`const std::uint8_t* const` 时，能准确说出“指针本身能不能改”和“通过指针能不能改对象”。
4. **建立借用边界的意识。** 知道指针和引用通常只是“借用”，不负责让对象活着；接口一旦把借用交出去，就必须明确“谁拥有、活多久、何时失效”。

### 1.2 验收标准回扣

节点给出的验收标准有两条，本课会反复用到：

- **能区分指针本身 `const` 和指向 `const`。** 前者限制“指针变量能不能改指向”，后者限制“能不能通过指针修改对象”。这两件事互不相同，可以同时成立。
- **不会返回局部对象引用。** 局部对象在函数返回时生命周期结束，返回它的引用（或指针）会立刻变成悬垂引用；正确做法是按值返回、返回调用者拥有的对象，或把结果写入调用者提供的容器。

### 1.3 为什么微信公众号后台要懂

节点对“为什么”写得很直接：**阅读后台代码中的传参、缓冲区与非拥有视图。** 放进腾讯微信公众号后台的日常场景，就是下面这些具体问题：

- **消息体是“字节 + 长度”，不是以 `\0` 结尾的字符串。** 微信回调、客服消息、模板消息里可能包含 JSON/XML、图片、语音等二进制或混合内容；解析时通常拿到的是一段 `data` 加一个 `size`。用指针访问就必须自己检查下标，不能指望 `strlen` 或字符串结尾。
- **非拥有视图能减少拷贝。** 一个请求体可能被多个解析函数共享：验签、解 XML、取字段、写日志。如果每层都复制一遍，CPU 和内存都会浪费；用 `const std::uint8_t* + size` 或 `ByteView` 借用原缓冲区更高效。但这也意味着：**视图不能比原缓冲区活得更久**，异步任务、线程池、延迟发送尤其容易踩坑。
- **回调参数常以 `const T&` 传入。** `void on_message(const std::string& body)` 表示“我只读，不拷贝”。如果处理函数把 `body` 的引用存进成员变量或丢给异步线程，回调返回后这个引用就可能悬垂。后台代码里“看起来能跑、压测偶发乱码”的问题，很多都出在这里。
- **网络长度字段不可信。** 包头里的 `offset`、`length` 来自外部输入。用 `offset + length <= size` 做检查时，两个无符号数相加可能溢出；正确写法是先比较 `offset <= size`，再比较 `length <= size - offset`。这是缓冲区接口必须守住的边界。
- **线上故障定位成本高。** ASan 能在测试阶段直接给出“哪一行读了已释放/已出作用域的地址”，比线上靠 core dump 猜要便宜得多。把 `-fsanitize=address` 纳入单测和 CI，是后台 C++ 服务很划算的一道防线。

所以本课的目标不是背语法，而是建立一条工程习惯：**指针/引用只说明“怎么访问”，不说明“对象还活着”；所有权和生命周期要在接口设计里说清楚。**

## 2. 从直觉到原理：三个核心要点

### 2.1 空指针与解引用

#### 直觉：地址纸条可以为空

把指针想成一张写着“门牌号”的纸条：

- 纸条上写着有效地址，解引用 `*p` 就是“按地址去取东西”。
- 纸条上写着“没有地址”，就是空指针，用 `nullptr` 表示。
- 拿着一张空纸条去取东西，不是“取到 0”，而是**未定义行为**：可能立刻崩溃，也可能被优化成任意结果。

```cpp
#include <cstddef>
#include <cstdint>
#include <iostream>

const std::uint8_t* find_byte(const std::uint8_t* data, std::size_t size,
                              std::uint8_t key) {
    for (std::size_t i = 0; i < size; ++i) {
        if (data[i] == key) {
            return data + i;
        }
    }
    return nullptr;
}

int main() {
    const std::uint8_t payload[] = {0x01, 0x02, 0x03};
    const std::uint8_t* found = find_byte(payload, 3, 0x02);

    if (found != nullptr) {
        std::cout << "found: " << static_cast<int>(*found) << '\n';
    } else {
        std::cout << "not found\n";
    }
}
```

“查找可能失败”用返回值 `nullptr` 表达是常见设计；调用方必须在使用前检查。现代 C++ 也可以返回 `std::optional<std::uint8_t>` 或 `std::optional<std::size_t>`，把“可能没有”写进类型里，减少忘记检查的机会。

#### 原理：解引用空指针为什么危险

- `nullptr` 不指向任何对象，`*p` 没有合法对象可访问。
- C++ 标准不规定这时发生什么；操作系统可能因为访问 0 页触发段错误，编译器也可能假设“解引用过的指针一定非空”，从而删掉后面的判空逻辑。
- 所以**空指针检查必须发生在解引用之前**，而且不能靠“我先解引用一下看看”来探测。

一个常被忽略的边界：**指针非空，不代表对象还活着。** 指针只保存地址；对象被 `delete`、离开作用域、被容器扩容搬走后，旧地址仍然可能“非空”，但已经不能访问。这正是节点 pitfalls 强调的：

> 指针非空不保证仍指向存活对象。

用 ASan 编译一个空指针解引用的小程序：

```bash
g++ -std=c++17 -g -fsanitize=address -fno-omit-frame-pointer null_demo.cpp -o null_demo
./null_demo
```

典型报告会包含：

```text
ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000
The signal is caused by a READ memory access.
Hint: address points to the zero page.
```

报告里的 `0x000000000000` 就是空地址。读调用栈可以定位到具体函数；如果是悬垂对象，报告会换成 `heap-use-after-free` 或 `stack-use-after-scope`，地址不是 0，但同样无效。

#### 后台例子：回调里“可选字段”的查找

解析微信消息时，某个可选字段可能不存在：

```cpp
const std::uint8_t* field = find_field(body.data(), body.size(), key);
if (field == nullptr) {
    return handle_missing_field();
}
const std::uint8_t value = *field;
```

不要写成：

```cpp
const std::uint8_t* field = find_field(body.data(), body.size(), key);
const std::uint8_t value = *field;
if (field == nullptr) {
    return handle_missing_field();
}
```

后者既解引用了空指针，又让编译器有理由认为 `field` 永远非空。即使“目前没崩”，也是未定义行为。

### 2.2 引用绑定与 `const`

#### 直觉：引用是对象的别名

引用在定义时必须绑定到一个对象，之后一直是这个对象的别名：

```cpp
int code = 200;
int& ref = code;
ref = 500;
std::cout << code << '\n';
```

输出是 `500`，因为 `ref` 和 `code` 是同一个对象。引用有几个关键性质：

- **必须初始化**，不能先声明再绑定。
- **不能重新绑定**，`ref = other` 是给原对象赋值，不是让 `ref` 改指向 `other`。
- **没有合法的“空引用”**。通过解引用空指针来“制造引用”是 UB，不是一种可检查的状态。
- 引用本身通常不占独立对象空间（实现细节），但语义上它就是一个别名。

函数参数里的引用表达“借用调用者的对象”：

```cpp
void set_status(std::string& out, int code) {
    out = "status=" + std::to_string(code);
}

void log_message(const std::string& body) {
    std::cout << body.size() << '\n';
}
```

`std::string&` 是可写的借用，`const std::string&` 是只读借用。后者还能绑定临时对象，因此适合做只读参数，避免不必要的拷贝。

#### 原理：`const` 到底修饰谁

看指针声明时，从变量名往左读，`const` 修饰离它最近的那个部分：

| 声明 | 指针本身能否改指向 | 能否通过指针改对象 | 读法 |
|------|------------------|------------------|------|
| `std::uint8_t* p` | 能 | 能 | 指向可写字节的指针 |
| `const std::uint8_t* p` | 能 | 不能 | 指向 const 字节的指针（pointer to const） |
| `std::uint8_t* const p` | 不能 | 能 | const 指针，指向可写字节（const pointer） |
| `const std::uint8_t* const p` | 不能 | 不能 | 指向 const 字节的 const 指针 |

```cpp
std::uint8_t buffer[4] = {1, 2, 3, 4};

const std::uint8_t* p1 = buffer;
p1 = buffer + 1;
// *p1 = 9;

std::uint8_t* const p2 = buffer;
*p2 = 9;
// p2 = buffer + 1;

const std::uint8_t* const p3 = buffer;
// *p3 = 9;
// p3 = buffer + 1;
```

删掉注释后，后三种赋值会分别被编译器拒绝。注意 `p1` 可以改指向，但改不了 `*p1`；`p2` 正好相反。

引用版本的 `const` 更简单：

```cpp
std::uint8_t& writable = buffer[0];
const std::uint8_t& readable = buffer[1];
writable = 7;
// readable = 8;
```

`const std::uint8_t&` 是“引用到 const 对象”，不是“const 引用”。C++ 里没有 `T& const` 这种顶层 const 引用写法（会编译失败）；`const` 作用在“被引用的对象”上。

#### 生命周期：借用最怕“原对象先走”

引用本身不延长被引用对象的生命周期（除了一个局部 `const T&` 直接绑定临时对象的特例）。例如：

```cpp
const std::string& make_greeting() {
    std::string local = "hello";
    return local;
}
```

函数返回时 `local` 已销毁，返回的引用立刻悬垂。编译器通常会警告：

```text
warning: reference to local variable 'local' returned [-Wreturn-local-addr]
```

这个警告不能忽略，更不能靠 `-Wno-return-local-addr` 关掉。正确写法是按值返回：

```cpp
std::string make_greeting() {
    return std::string("hello");
}
```

或者由调用者提供存储：

```cpp
void make_greeting(std::string& out) {
    out = "hello";
}
```

同理，下面这种写法也会留下悬垂引用：

```cpp
void on_message(const std::string& body) {
    const std::string& saved = body;
    pending_.push_back(saved);
}
```

`body` 绑定的是调用者（或上层框架）拥有的对象；回调返回后，`pending_` 里的引用不保证还有效。跨回调、跨线程保存时，应该拷贝 `std::string`，或者用 `std::shared_ptr` 等明确的所有权类型。节点的 tradeoff 正好点在这里：

> 借用减少拷贝，但把生命周期约束交给调用者。

#### 后台例子：缓冲区接口的 const 重载

一个字节缓冲区通常提供两个 `at`：

```cpp
std::uint8_t& at(std::size_t index);
const std::uint8_t& at(std::size_t index) const;
```

- 非 const 对象调用第一个，返回可写引用。
- const 对象（或 const 引用）只能调用第二个，返回只读引用。
- 这样 `const ByteBuffer&` 的调用者在**编译期**就无法写缓冲区，而不是等到运行期才发现。

这也是本课实践里 `ByteBuffer` 的设计基础。

### 2.3 指针运算和数组边界

#### 直觉：指针加法是“跳过多少个元素”

数组在内存里连续存放。若 `p` 指向数组第 0 个元素，`p + i` 指向第 `i` 个元素（不是第 `i` 个字节，而是 `i * sizeof(T)` 个字节之后）：

```cpp
std::uint8_t payload[5] = {10, 20, 30, 40, 50};
std::uint8_t* p = payload;

std::cout << static_cast<int>(*(p + 2)) << '\n';
std::cout << static_cast<int>(p[4]) << '\n';
```

`*(p + 2)` 和 `p[2]` 等价。合法的指针运算有一个严格范围：

- 可以形成指向数组元素或**尾后一位**（one-past-the-end）的指针，即 `payload + 5`。
- 可以解引用 `payload + 0` 到 `payload + 4`。
- **不能解引用 `payload + 5`**，尾后指针只能用于比较、作为区间右端点，不能读。
- 两个指针相减只在同一数组（或同一数组的尾后）内有定义；跨对象相减没有可移植意义。

#### 原理：为什么指针本身不携带长度

指针只有地址，没有“这个地址后面还有多少字节”的信息。下面这个函数单独看 `p`，无法知道它指向的数组有多长：

```cpp
void consume(const std::uint8_t* p) {
    // 不知道 p 后面有多少字节，不能安全地 p[0]、p[1] ...
}
```

所以后台接口更应该用“指针 + 长度”或“视图”表达：

```cpp
void consume(const std::uint8_t* data, std::size_t size) {
    for (std::size_t i = 0; i < size; ++i) {
        use(data[i]);
    }
}
```

对于不可信的长度字段，检查顺序很重要：

```cpp
bool in_range(std::size_t size, std::size_t offset, std::size_t length) {
    return offset <= size && length <= size - offset;
}
```

不要写：

```cpp
return offset + length <= size;
```

当 `offset` 和 `length` 很大时，`offset + length` 可能无符号溢出，回绕成一个很小的值，让非法区间通过检查。先减后比可以避免溢出：

- `offset <= size` 保证 `size - offset` 不会下溢；
- `length <= size - offset` 等价于 `offset + length <= size`，但没有加法溢出风险。

#### 边界检查的两种风格

1. **抛异常/返回错误**：适合公共接口、解析入口，越界是调用方错误，必须显式处理。

```cpp
std::uint8_t& ByteBuffer::at(std::size_t index) {
    if (index >= size_) {
        throw std::out_of_range("index out of range");
    }
    return data_[index];
}
```

2. **返回指针/布尔值**：适合热路径或“可能失败”的查找，调用方自行决定如何处理。

```cpp
const std::uint8_t* ByteBuffer::try_at(std::size_t index) const noexcept {
    if (index >= size_) {
        return nullptr;
    }
    return data_ + index;
}
```

无论哪种，**边界检查必须依据有效长度，而不是容量或分配大小**。缓冲区“分配了 4KB”不等于“有效数据有 4KB”；`std::vector` 的 `size()` 和 `capacity()` 也必须区分。

#### 后台例子：解析消息体里的字段

假设消息体格式是 `[2 字节长度][内容]`，需要取出内容：

```cpp
bool read_field(const std::uint8_t* body, std::size_t size,
                std::size_t& offset, ByteView& out) {
    if (offset > size || size - offset < 2) {
        return false;
    }
    const std::size_t length =
        (static_cast<std::size_t>(body[offset]) << 8) | body[offset + 1];
    offset += 2;

    if (length > size - offset) {
        return false;
    }
    out = ByteView(body + offset, length);
    offset += length;
    return true;
}
```

这里有三道边界：

- `offset` 是否还在缓冲区内；
- 长度字段本身是否读得下（至少 2 字节，且 `offset + 2` 不溢出）；
- 声明的长度是否超过剩余字节。

少任何一道，外部输入都可能让解析器越界读。用 ASan 跑一组畸形输入，比等线上报警快得多。

## 3. 常见误区与设计权衡

### 3.1 常见误区

1. **“指针非空，所以对象还活着。”**
   这是本节点最核心的 pitfall。指针只表示地址；`delete` 后、对象离开作用域后、`vector` 扩容后，旧地址可能仍然非空。有效性来自生命周期管理，不来自 `p != nullptr`。

2. **返回局部对象的引用或指针。**
   `return local;`（返回 `T&` 或 `T*`）在函数返回后立即悬垂。编译器通常给出 `-Wreturn-local-addr`，要把它当错误处理，而不是关掉警告。

3. **把 `const T*` 和 `T* const` 混为一谈。**
   前者是“指向只读对象的指针”，指针本身可以改指向；后者是“const 指针”，不能改指向，但可以通过它改对象。`const T* const` 才是两者都限制。

4. **把 `const T&` 参数存起来跨调用使用。**
   `void handle(const std::string& body)` 里的 `body` 只在调用期间有效。把它存进成员、队列、lambda 捕获或异步任务，回调结束后就可能悬垂。

5. **尾后指针解引用。**
   `data + size` 可以形成、可以用于比较和表示区间右端，但 `*(data + size)` 是越界。循环条件应写 `i < size`，而不是 `i <= size`。

6. **用 `offset + length <= size` 检查外部长度。**
   无符号加法可能溢出。应写 `offset <= size && length <= size - offset`。

7. **把 `sizeof(pointer)` 当数组长度。**
   指针参数退化为指针后，`sizeof(p)` 是指针大小（常见 8 字节），不是数组字节数。数组长度要用单独参数、`std::size`（对真正的数组）或容器 `size()`。

8. **未初始化指针。**
   `std::uint8_t* p;` 的值不确定，解引用是 UB。定义时就初始化为 `nullptr`，或直接绑定到有效对象。

9. **混淆 `size()` 与 `capacity()`。**
   `vector` 的容量之外、`size()` 之内才是有效元素；用容量做边界检查会读到未构造区域。

### 3.2 设计权衡

| 选择 | 好处 | 代价 |
|------|------|------|
| 借用（指针 / 引用 / 视图） | 避免拷贝，适合大消息体、多层解析 | 生命周期约束转移给调用者；跨异步/线程时容易悬垂 |
| 按值返回 / 拷贝 | 所有权清晰，生命周期简单 | 内存分配与拷贝成本；大对象可能昂贵 |
| `std::string_view` | 只读字符串视图，接口简洁 | 不保证以 `\0` 结尾；不能用于二进制中的任意 NUL；仍有生命周期问题 |
| 指针 + 长度 / 字节视图 | 适合二进制、可含 NUL、零拷贝解析 | 需要手动做边界检查；接口略啰嗦 |
| `at()` 抛异常 | 错误无法被静默忽略 | 异常路径有开销；热路径可能不适合 |
| `operator[]` / 裸指针 | 快，无检查 | 越界是 UB；必须靠不变量或外部校验保证 |
| `unique_ptr` / `shared_ptr` 表达所有权 | 释放责任明确，减少泄漏 | 有间接层与原子引用计数成本；不是所有场景都需要 |
| `const` 正确性 | 编译期阻止误写，接口意图清晰 | 需要维护 const/非 const 重载；旧代码改造有成本 |

一个实用的取舍原则：

- **入口处做检查，内部靠不变量。** 解析网络包、读配置、接收外部长度时，用带检查的接口；进入核心逻辑后，如果类型和前置条件已经保证范围，可以用更轻的访问方式。
- **所有权用类型表达。** 需要拥有就 `std::vector`、`std::string`、`std::unique_ptr`；只是临时借用就用指针/引用/视图，并在接口文档和命名里写清“有效期”。
- **跨边界不借。** 跨线程、跨回调、进队列、进缓存的数据，要么拷贝，要么转移所有权；不要把栈上或回调参数的引用带出去。

## 4. 动手实践：边界检查缓冲区 + ASan 定位悬垂引用

实践目录已经准备好：

```text
practice18/
├── byte_buffer.hpp
├── bounds_demo.cpp
├── dangling_ref.cpp
├── test.sh
└── README.md
```

### 步骤 0：进入目录并确认工具链

```bash
cd /workspace/atlas/lessons/practice18
g++ --version
```

需要支持 C++17 和 `-fsanitize=address` 的 GCC（本课在 GCC 14 上验证）。

### 步骤 1：设计接口

`ByteBuffer` 拥有数据，`ByteView` 只借用数据：

- `ByteBuffer(std::size_t size)`：分配指定字节数，初始为 0。
- `ByteBuffer(std::initializer_list<std::uint8_t>)`：方便测试。
- `size()` / `empty()`：返回有效长度。
- `at(std::size_t)`：带边界检查，越界抛 `std::out_of_range`。
- `at(std::size_t) const`：只读重载，返回 `const std::uint8_t&`。
- `data()` / `data() const`：返回裸指针，供底层库或视图使用。
- `ByteView(const std::uint8_t*, std::size_t)`：非拥有视图。
- `ByteView(const ByteBuffer&)`：从缓冲区构造视图，但不延长缓冲区寿命。

关键点：`at` 的 const 重载让 `const ByteBuffer&` 只能读；`ByteView` 的 `at` 返回 `const std::uint8_t&`，因为视图不应修改原缓冲区。

### 步骤 2：实现边界检查

核心逻辑：

```cpp
void check(std::size_t index) const {
    if (index >= data_.size()) {
        throw std::out_of_range("ByteBuffer::at index out of range");
    }
}

std::uint8_t& at(std::size_t index) {
    check(index);
    return data_[index];
}

const std::uint8_t& at(std::size_t index) const {
    check(index);
    return data_[index];
}
```

注意 `index >= size` 而不是 `index > size`：下标从 0 开始，最后一个合法下标是 `size - 1`。

### 步骤 3：写测试

`bounds_demo.cpp` 覆盖：

- 正常读写首字节、末字节；
- `at(size)`、`at(100)` 越界抛异常；
- 通过越界引用写入也被拒绝；
- const 缓冲区只能读；
- `const std::uint8_t*` 可以改指向但不能改对象；
- `std::uint8_t* const` 不能改指向但可以改对象；
- `ByteView` 能读、越界会抛异常。

编译运行：

```bash
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -g -O1 \
    bounds_demo.cpp -o bounds_demo
./bounds_demo
```

预期输出：

```text
[PASS] size is 4
[PASS] at(0) reads first byte
...
[PASS] view rejects out-of-range read
all tests passed
```

### 步骤 4：构造悬垂引用

`dangling_ref.cpp` 提供三个场景：

1. `heap`：引用绑定到 `new[]` 的字节，`delete[]` 后继续读。

```cpp
std::uint8_t* payload = new std::uint8_t[4]{0x01, 0x02, 0x03, 0x04};
std::uint8_t& second = payload[1];
delete[] payload;
return static_cast<int>(second);
```

2. `stack`：指针指向块作用域数组元素，离开作用域后解引用。

```cpp
std::uint8_t* second = nullptr;
{
    std::uint8_t frame[4] = {0x0A, 0x0B, 0x0C, 0x0D};
    second = &frame[1];
}
return static_cast<int>(*second);
```

3. `return`：函数返回局部 `std::string` 的引用。

```cpp
const std::string& local_greeting() {
    std::string local = "hello from a local string";
    return local;
}
```

### 步骤 5：用 ASan 编译运行

```bash
g++ -std=c++17 -Wall -Wextra -g -O0 \
    -fsanitize=address -fno-omit-frame-pointer \
    dangling_ref.cpp -o dangling_ref

./dangling_ref heap
./dangling_ref stack
./dangling_ref return
```

编译时你应看到：

```text
warning: reference to local variable 'local' returned [-Wreturn-local-addr]
```

这是编译器在免费帮你抓第 3 个场景。运行 `heap` 时，ASan 典型报告：

```text
ERROR: AddressSanitizer: heap-use-after-free on address ...
READ of size 1 at ...
    #0 ... heap_reference_case ... dangling_ref.cpp:16
    #1 ... main ... dangling_ref.cpp:34

freed by thread T0 here:
    #0 ... operator delete[](void*)
    #1 ... heap_reference_case ... dangling_ref.cpp:15
```

先看错误类型，再看 `READ` 的调用栈（谁在越界读），最后看 `freed by`（谁提前释放了对象）。运行 `stack` 时会得到：

```text
ERROR: AddressSanitizer: stack-use-after-scope on address ...
```

报告会指出该地址位于哪个栈帧、哪个变量（例如 `frame`），以及越界读发生在哪一行。

`return` 场景在 GCC 的 ASan 下不保证被拦截：可能直接段错误，也可能读到垃圾值。原因是 use-after-return 的检测依赖编译器和运行库支持；**不能把“ASan 没报”当成代码正确**。这类问题主要靠编译警告和代码评审拦截。

> 实践提示：悬垂 demo 使用 `-O0`，是为了让“释放后读取”的操作保持原样。若开启优化，编译器可能基于 UB 把读取重排或删除，ASan 就未必在同一位置报错。真实项目通常用 `-O1 -g -fsanitize=address` 平衡速度与可读性，但排查特定问题时可以降到 `-O0`。

### 步骤 6：修复

- 堆上的引用：不要让引用活过 `delete`；如果确实需要共享，使用 `std::shared_ptr` 或明确的所有权容器。
- 栈上的指针：扩大对象作用域，或把值拷贝出来；不要返回指向局部数组元素的指针。
- 返回局部引用：改为按值返回：

```cpp
std::string local_greeting() {
    return std::string("hello from a local string");
}
```

- 非拥有视图：保证拥有者活得更久；跨异步边界时复制数据或转移所有权。

### 步骤 7：一键验证

```bash
cd /workspace/atlas/lessons/practice18
./test.sh
```

脚本会：

1. 用 `-Wall -Wextra -Wpedantic -Werror` 编译并运行 `bounds_demo`；
2. 用 ASan 编译 `dangling_ref`；
3. 分别运行 `heap` 和 `stack`，确认进程非 0 退出且报告包含 `heap-use-after-free` / `stack-use-after-scope`。

看到 `all practice18 checks passed` 即完成。

## 5. 自测题

### 题 1：区分 `const` 的位置

下面三个声明分别表示什么？哪些可以改变指针指向？哪些可以通过指针修改 `std::uint8_t` 对象？

```cpp
const std::uint8_t* a = buffer;
std::uint8_t* const b = buffer;
const std::uint8_t* const c = buffer;
```

**答案：**

- `a` 是“指向 const 字节的指针”（pointer to const）：可以改指向（`a = buffer + 1` 合法），不能写 `*a`。
- `b` 是“const 指针”（const pointer）：不能改指向（`b = buffer + 1` 非法），可以写 `*b`。
- `c` 是“指向 const 字节的 const 指针”：既不能改指向，也不能写 `*c`。

记忆方法：从变量名往左读，`const` 修饰离它最近的部分；`*` 左边的 `const` 管对象，`*` 右边的 `const` 管指针本身。

### 题 2：返回局部引用

下面的函数有什么问题？编译器会给出什么警告？应该怎么改？

```cpp
const std::string& build_response() {
    std::string body = "{\"errcode\":0}";
    return body;
}
```

**答案：**

`body` 是局部对象，函数返回时被销毁，返回的引用立即悬垂。调用方使用它属于未定义行为：可能读到垃圾、崩溃，或者在优化后表现“正常”。

GCC 通常给出：

```text
warning: reference to local variable 'body' returned [-Wreturn-local-addr]
```

正确做法是按值返回（依赖 RVO/移动）：

```cpp
std::string build_response() {
    return std::string("{\"errcode\":0}");
}
```

或者让调用者提供输出参数：

```cpp
void build_response(std::string& out) {
    out = "{\"errcode\":0}";
}
```

不要把警告当成“已知问题”关掉；这类接口在后台代码里一旦跨线程或延迟调用，就很难排查。

### 题 3：安全地检查偏移与长度

`size` 是缓冲区有效长度，`offset` 和 `length` 来自网络包头。下面这个检查为什么不够安全？应怎么写？

```cpp
bool ok = (offset + length <= size);
```

**答案：**

`offset`、`length`、`size` 都是无符号类型时，`offset + length` 可能溢出并回绕。例如 `offset = SIZE_MAX`、`length = 2`，相加得到 `1`，可能错误地通过 `1 <= size` 的检查，随后 `body + offset` 就越界了。

安全写法是先保证 `offset` 不越界，再用减法比较：

```cpp
bool ok = (offset <= size) && (length <= size - offset);
```

这样 `size - offset` 不会下溢，并且等价于 `offset + length <= size`。随后再形成 `body + offset`，并只在 `length` 范围内访问；注意 `body + size` 是合法的尾后指针，但不能解引用。

## 6. 推荐资源

- **指针、引用与借用边界：参考文档 / 原始资料**
  - 链接：<https://en.cppreference.com/w/cpp/language/pointer.html>
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：基础
  - 说明：重点阅读空指针与解引用、引用绑定与 `const`，用于核对本节点实验的语义和边界。读的时候把示例中的指针声明按“从变量名往左读”的方法拆一遍，再对照本课的 `ByteBuffer::at` 与 `dangling_ref.cpp`，确认自己对 const 位置和生命周期边界的理解一致。
