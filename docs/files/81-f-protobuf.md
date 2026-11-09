# 第 81/208 课：Protobuf模式与代码生成

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 2 阶段 · 协议与框架 |
| 前置 | d-framing、b-stl |
| 难度 | 基础 |
| 领域 | F |
| 标签 | Protocol Buffers、F、C++ |
| 子分类 | 协议与框架 |
| 节点 ID | f-protobuf |
| 英文名 | Protocol Buffers |

> 承接说明：前置节点 d-framing（字节流分帧与协议解析，第 56 课）讲了字节流没有天然消息边界，需要长度前缀、分隔符或固定长度来切分，也讲了半包、粘包、增量解析和最大长度这些工程问题。前置节点 b-stl（STL 容器与迭代器，第 23 课）讲了 `std::string`、`std::vector`、`std::map` 等容器的基本用法与迭代器语义。本课往前走一步：当你已经能把一段字节可靠地切成一条消息，接下来要决定这段字节里每个字段怎么表示、字段的身份如何固定、以后怎样在不破坏旧客户端的前提下增删字段。Protobuf 用模式（schema）来回答这些问题，再由代码生成器把模式翻译成各语言的类型和编解码代码。本节点 summary 是：Protobuf 用模式定义跨语言消息，生成代码完成编码解码；字段编号与兼容规则比字段顺序重要。

---

## 导读

先看本节点的 summary，后面所有内容都围绕它展开：

> Protobuf 用模式定义跨语言消息，生成代码完成编码解码；字段编号与兼容规则比字段顺序重要。

这句话至少包含三层意思。

第一层是**模式即契约**。你写的 `.proto` 文件不是某个语言的结构体声明，而是一份跨语言的数据契约。C++ 服务、Python 脚本、Go 网关、Java 后台都可以从同一份模式生成各自的类型。线上真正传输的不是字段名，而是模式约定好的编号和编码方式。只要大家拿的是兼容的模式，就能互相读懂。

第二层是**字段编号才是身份**。在 JSON 里，字段名直接写在字节里，读的人靠名字找值；在 Protobuf 的二进制线格式里，字段名根本不出现，出现的是字段编号和 wire type。字段叫什么、在文件里排第几行，都不影响解码；字段编号是多少，才决定这段字节被解释成哪个字段。这也解释了为什么“字段顺序”不重要，而“编号不复用”极其重要。

第三层是**兼容是设计出来的，不是碰运气**。新增字段、删除字段、修改类型、调整 `repeated` 与 `optional`，每一种变更都有明确的兼容边界。模式演进不是“改完重新生成就行”，而是要先判断旧数据、旧客户端、旧服务会不会误读。节点 pitfalls 说的“不能复用已删除字段编号，也不应把序列化字节作为稳定规范化哈希”，正是这条线的两个典型反面。

本课沿着三个节点要点推进：

- **字段编号和 wire type**：线格式到底长什么样，一个字段怎样被编码成 tag、长度和值。
- **presence、repeated 和 oneof**：默认值、字段是否存在、重复字段和互斥字段在语义上有什么区别。
- **保留编号和代码生成版本**：删除字段时怎样留下“墓碑”，生成代码与运行时库的版本怎样配合。

三个要点最终服务于同一个目标，也就是本节点的 why：**为服务接口建立可演进的数据契约。**

---

## 学习目标

### goals

- 能解释字段编号和 wire type。
- 能独立完成：定义 KV 请求响应，生成 C++ 代码并测试旧新版本互读。

### acceptance

- 删除字段后保留编号。
- 能解释默认值与字段存在性区别。

### 把验收翻译成可检查产物

“能解释”和“能独立完成”都太容易被“我看懂了”糊弄过去。把两条 acceptance 翻译成看得见、摸得着的东西：

- **“删除字段后保留编号”**：你的仓库里要能看到一份真实的模式演进记录——旧版本有某个字段，新版本删掉了它，但在新版本里用 `reserved` 同时保留了它的编号和名字。你还要能演示：旧版本写出的、包含该字段的字节，被新版本读入时不会崩溃、不会把它误读成别的字段；并且新版本以后新增字段时不会再用这个编号。最好再加一条检查：如果有人试图把被保留的编号写回模式，`protoc` 会报错。
- **“能解释默认值与字段存在性区别”**：你不能只说“默认值是 0 或空字符串”。你要能说清：一个字段“没有出现在字节里”和“出现在字节里但值等于默认值”是两件事；proto3 的普通标量字段默认不区分这两者，`optional` 字段可以区分；消息字段、`oneof` 成员在存在性上又各有规则。并且用一个测试证明你的理解，例如写入一个显式设为 0 的 `optional int32`，读端 `has_xxx()` 返回真；而完全不设置时返回假。

### 先修自检

- d-framing：能说清为什么 TCP 字节流需要长度前缀或分隔符才能切出消息；能解释半包、粘包和“一次 read 不保证读到完整消息”；知道解析要按“先读长度、再读负载、设最大长度上限”的顺序做。
- b-stl：能熟练使用 `std::string` 保存二进制字节，知道 `std::string` 可以包含 `\0`，长度要用 `size()` 而不是 `strlen()`；能用 `std::vector` 管理重复字段；能读懂迭代器和范围 `for`。
- 工具链：知道怎样运行 `protoc`，知道 C++ 项目要链接 Protobuf 运行时库；如果还没有装过，实践一节会给出从零开始的命令和最小 CMake 配置。

如果前两条里有任何一条说不清，先回去补。本课不会重新讲 TCP 为什么粘包，也不会重新讲 STL 容器怎么遍历；它假设你已经能把一条完整消息拿在手里，重点是把“消息里面长什么样、以后怎么改”讲清楚。

---

## 为什么后端要懂

本节点 why 的原话是：**为服务接口建立可演进的数据契约。** 在真实后端里，这句话对应的不是“要不要用 protobuf”，而是几类会直接影响上线安全和排障效率的决策。

### 场景一：滚动升级时，新旧版本同时在线

假设一个 KV 服务有 200 个实例，你要发布新版本。发布不是原子的：一部分实例跑新代码，一部分还跑旧代码，负载均衡把请求随机打到两边。新版本想在请求里增加一个 `optional int32 ttl_ms` 字段，旧版本根本不认识这个字段。如果协议设计得对，旧版本会跳过不认识的字段，请求照常处理；如果设计得不对，比如新版本复用了旧版本删掉的某个编号，旧版本就会把 `ttl_ms` 当成另一个字段来解释，可能把“过期时间”读成了“用户 ID”。

Protobuf 的兼容规则就是为这种场景准备的。但规则只在“你遵守它”时才有效。字段编号一旦被复用，编译器不会知道你曾经用过它，线格式也不会自带“这是第几版”的标记，错误只会在旧数据或旧客户端那里悄悄发生。

### 场景二：跨语言链路

一个请求可能从 Go 网关进入，经过 C++ 存储服务，再落到 Python 离线任务。如果每个环节都手写 JSON 解析，字段名拼错、类型不一致、空值处理不同，都会变成联调时的反复确认。Protobuf 把类型和字段编号写进模式，各语言生成代码后，至少“字段叫什么、是什么类型、编号是多少”是同一份事实。代码生成不是魔法，它的价值在于把契约从口头约定变成编译期可见的类型。

### 场景三：把消息存进磁盘或消息队列

很多人只把 Protobuf 当成 RPC 的序列化格式，其实它同样适合做持久化格式和队列消息格式。问题在于：一旦字节写进磁盘，它就会活很久。你今天删掉的字段，半年后还可能出现在历史文件里；你今天觉得可以复用的编号，可能已经被几 TB 的旧数据占用。删除字段时留下 `reserved`，本质上是在为未来的自己保留解释权。

### 场景四：排障时看不懂二进制

节点 tradeoffs 说得很直接：二进制紧凑且跨语言，但人读与手工排障不如文本直观。线上抓到一段 Protobuf 字节，直接看是一串十六进制；要知道里面是什么，需要模式、需要 `protoc --decode`，或者需要能按模式解析的工具。这不是缺陷，而是取舍：你换来了更小的体积和更强的类型约束，代价是排障多一步。理解这一点，才不会在事故现场一边翻模式一边后悔。

### 场景五：把序列化字节当哈希

有些系统想给请求算一个“内容指纹”，用来去重、做缓存键或做幂等判断。如果直接对 Protobuf 序列化后的字节做哈希，会遇到一个隐蔽的问题：同样的逻辑消息，不保证每次序列化出的字节完全一样。字段顺序、`map` 的遍历顺序、未知字段、打包方式、甚至不同版本的运行时，都可能让字节不同。节点 pitfalls 明确说“不应把序列化字节作为稳定规范化哈希”，原因就在这里。需要稳定指纹时，应先按明确的规范化规则构造内容，再对内容做哈希，而不是对“某个实现碰巧生成的字节”做哈希。

---
## 从直觉到原理

下面三个要点不是并列的语法点，而是一条理解线：先看字节层面字段怎样被标识（要点一），再看字段在语义层面怎样表达“有没有值”（要点二），最后看当模式发生变化时，怎样保护旧的编号和生成代码的版本边界（要点三）。走完这三步，你才能安全地修改一份正在被使用的模式。

### 要点一：字段编号和 wire type

#### 直觉：线格式里没有字段名

先做一个对比。假设有一条逻辑消息：

```text
key = "user:42"
value = "alice"
```

如果写成 JSON，字节里会出现 `"key"`、`"value"` 这些名字：

```json
{"key":"user:42","value":"alice"}
```

读的人即使没有模式，也能大致猜出每个值的含义。Protobuf 不这样做。它对每个字段只发送三样东西：

1. 一个 tag，里面同时包含**字段编号**和 **wire type**；
2. 如果 wire type 是长度分隔类型，再发送一个**长度**；
3. 字段的**值**（按 wire type 决定的编码）。

字段名不出现，字段在 `.proto` 文件里的行号也不出现。接收方拿到 tag 后，先取出编号，查自己手上的模式：“编号 1 对应哪个字段？类型是什么？”然后用对应的 wire type 去解码值。编号对不上，就跳过；编号被复用，就会解错。

所以第一句要记住的话是：**字段编号是线格式上的身份，字段名只是源码里的别名。**

#### tag 的位布局

Protobuf 的 tag 是一个 varint，它的低 3 位是 wire type，高位是字段编号：

```text
tag = (field_number << 3) | wire_type
```

举几个例子：

| 字段编号 | wire type | tag 十进制 | tag 十六进制 | 含义 |
| --- | --- | --- | --- | --- |
| 1 | 0 | 8 | `0x08` | 编号 1，varint |
| 2 | 2 | 18 | `0x12` | 编号 2，长度分隔 |
| 3 | 0 | 24 | `0x18` | 编号 3，varint |
| 16 | 0 | 128 | `0x80 0x01` | 编号 16，varint，两字节 |
| 100 | 2 | 802 | `0xA2 0x06` | 编号 100，长度分隔 |

注意最后两行：编号 1 到 15 的 tag 可以只用一个字节表示；编号 16 开始，tag 本身要用两个字节。这不是编码器的随意选择，而是 varint 的自然结果。字段编号选得小，tag 就短；编号选得大，每个字段的 tag 就多占字节。这也是为什么“1 到 15 留给最常用的字段”会成为一条经验规则。

#### wire type 的五种形态

wire type 决定“值怎么读”。常见的有：

| wire type | 名称 | 典型类型 | 值的形态 |
| --- | --- | --- | --- |
| 0 | Varint | `int32`、`int64`、`uint32`、`uint64`、`bool`、`enum`、`sint32`、`sint64` | 一个可变长整数 |
| 1 | 64-bit | `fixed64`、`sfixed64`、`double` | 固定 8 字节，小端 |
| 2 | Length-delimited | `string`、`bytes`、嵌入消息、打包的 `repeated` 标量 | 长度 varint + 若干字节 |
| 5 | 32-bit | `fixed32`、`sfixed32`、`float` | 固定 4 字节，小端 |

wire type 3 和 4 是早期的 group 编码，已经废弃，新代码不应使用。你在解析未知字段时如果遇到它们，应当按照规范跳过或报错，而不是猜。

关键点是：**wire type 是线格式层面的分类，不是完整的语言类型**。`int32` 和 `bool` 都用 wire type 0，`string` 和“嵌入的另一个消息”都用 wire type 2。接收方只有结合模式里的字段声明，才知道该把这段 varint 解释成有符号数还是布尔值，该把这段长度分隔的字节解释成 UTF-8 字符串还是另一个消息。

#### varint 到底怎么编

varint 的规则很简单：每个字节低 7 位放数据，最高位表示“后面还有没有字节”。如果最高位是 1，就继续读下一个字节；如果是 0，当前值结束。数据按小端顺序拼接，也就是先出现的 7 位是低位。

以十进制的 300 为例：

```text
300 = 0b1_0010_1100
拆成 7 位一组（从低到高）：
  低 7 位：010_1100 = 0x2C
  高位：  0000_0010 = 0x02
第一字节：0x2C | 0x80 = 0xAC   （还有后续）
第二字节：0x02                  （结束）
编码结果：AC 02
```

再以字段 1、值 300 为例，tag 是 `0x08`，后面紧跟 `AC 02`：

```text
08 AC 02
```

如果字段 1 的值是 1，就是：

```text
08 01
```

如果是字段 2 的字符串 `"alice"`，长度是 5，那么：

```text
12 05 61 6C 69 63 65
12          tag：字段 2，wire type 2（(2<<3)|2 = 0x12）
05          长度：5 字节
61..65      "alice" 的 UTF-8 字节（a l i c e）
```

这就是 Protobuf 线格式最核心的样子：**tag + 可选长度 + 值**。不需要字段名，不需要分隔符，不需要括号。解析器按 tag 循环读，直到字节用完。

#### 一个最小解码器

下面这段 C++ 代码不依赖 Protobuf 库，只按线格式手工解析一个“只有一个 string 字段”的消息。它的目的不是让你以后都手写解析，而是让你亲眼看到 tag、长度和值怎样在字节流里排列。

```cpp
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

struct Buffer {
    const uint8_t* data;
    size_t size;
    size_t pos = 0;

    bool eof() const { return pos >= size; }

    bool read_varint(uint64_t& out) {
        out = 0;
        int shift = 0;
        while (pos < size) {
            uint8_t b = data[pos++];
            out |= static_cast<uint64_t>(b & 0x7F) << shift;
            if ((b & 0x80) == 0) return true;
            shift += 7;
            if (shift >= 64) return false;   // 防止畸形输入无限循环
        }
        return false;
    }
};

int main() {
    // 手工构造：字段 2（string），值 "alice"
    const uint8_t bytes[] = {0x12, 0x05, 'a', 'l', 'i', 'c', 'e'};
    Buffer buf{bytes, sizeof(bytes)};

    while (!buf.eof()) {
        uint64_t tag = 0;
        if (!buf.read_varint(tag)) {
            std::puts("bad tag");
            return 1;
        }
        uint32_t field = static_cast<uint32_t>(tag >> 3);
        uint32_t wire  = static_cast<uint32_t>(tag & 0x07);

        if (wire == 2) {
            uint64_t len = 0;
            if (!buf.read_varint(len) || len > buf.size - buf.pos) {
                std::puts("truncated");
                return 1;
            }
            std::string value(reinterpret_cast<const char*>(buf.data + buf.pos),
                              static_cast<size_t>(len));
            buf.pos += static_cast<size_t>(len);
            std::printf("field=%u wire=2 value=%s\n", field, value.c_str());
        } else {
            // 本示例只处理 wire type 2；其他类型必须先按规范跳过。
            std::printf("skip field=%u wire=%u\n", field, wire);
            return 1;
        }
    }
    return 0;
}
```

输出是：

```text
field=2 wire=2 value=alice
```

请特别注意两个防御点：

- `len > buf.size - buf.pos` 必须先检查。长度是对方给的，不能直接拿来做指针偏移，否则一个恶意长度就能让你越界读。
- `shift >= 64` 必须判断。畸形 varint 可以一直设置最高位，如果不设上限，解析器会一直读下去。

这两点都属于“线格式解析的边界条件”，也是 sourceStudy 里要求你核对错误处理路径的原因。Protobuf 库替你做了这些检查，但你得知道它在防什么。

#### 为什么字段顺序不影响解码

因为解析器的循环是“读 tag、看编号、读值、继续”，而不是“按模式声明顺序依次读”。字段 5 的字节可以出现在字段 2 前面，解析器照样能把它们放到正确的字段里。发送端通常按编号顺序写，但这只是常见实现的习惯，不是协议要求。

这条性质带来两个推论：

1. **重排 `.proto` 里的字段声明不会改变线格式**。字段 1 写在字段 2 后面，生成的代码仍然用各自的编号编码。
2. **不能靠“顺序”做兼容性判断**。你把字段从第 3 行挪到第 8 行，旧客户端完全不受影响；但你把字段编号从 3 改成 8，旧客户端就会把它当成未知字段跳过，新客户端写出的值旧客户端读不到。真正要检查的是编号，不是行号。

#### 编号的可用范围

字段编号不是任意整数。按 Protobuf 规范：

- 合法范围是 1 到 2^29 - 1（536870911）；
- 其中 19000 到 19999 预留给 Protobuf 实现内部使用，用户不能使用；
- 编号 1 到 15 编码后 tag 只占一个字节，适合最常用、最小的字段；
- 编号越大，tag 的 varint 越长，但值本身的编码不受编号影响。

选择编号时的实用建议：

- 从 1 开始，按“最常用优先”分配；
- 给未来可能高频新增的字段留出低位编号，但不要为了“预留”把编号挖得七零八落；
- 删除字段时不要释放编号，见要点三；
- 不要用编号表达业务含义，例如“编号 100 表示用户 ID”。编号只是身份，业务含义应写在字段名和注释里。

#### 一张图记住要点一

```text
一条消息的字节流：
+--------+--------+--------+--------+--------+
|  tag   | value  |  tag   |  len   | value  | ...
+--------+--------+--------+--------+--------+
   |                  |
   |                  +-- wire type 2 才有长度
   +-- (field_number << 3) | wire_type

解码循环：
读 tag -> 取编号 -> 查模式 -> 按 wire type 读值 -> 读下一个 tag
字段名不参与线格式；字段编号才是身份。
```

---

### 要点二：presence、repeated 和 oneof

#### 直觉：三种“没有值”不是同一件事

在业务代码里，下面三种情况经常被混为一谈：

- 用户没有提交 `age` 字段；
- 用户提交了 `age`，值是 0；
- 用户提交了 `age`，值是空字符串或空列表。

在 JSON 里，前两种可以靠“键是否存在”区分。在 Protobuf 里，能不能区分取决于字段的**存在性（presence）**语义。如果分不清，做 PATCH 更新时就会出事：客户端只想改 `name`，没有传 `age`，服务端却把 `age` 当成 0 写回去，把用户的年龄清掉了。

所以第二句要记住的话是：**“值等于默认值”和“字段不存在”是两件事；Protobuf 不同字段形式对这两件事的区分能力不同。**

#### proto3 的默认值

Protobuf 的标量字段在没有显式设置时，读取会得到该类型的默认值：

| 类型 | 默认值 |
| --- | --- |
| 整数类型（`int32`、`int64`、`uint32`、`uint64`、`sint32`、`sint64`、`fixed32`、`fixed64`、`sfixed32`、`sfixed64`） | 0 |
| 浮点类型（`float`、`double`） | 0.0 |
| `bool` | `false` |
| `string` | 空字符串 |
| `bytes` | 空字节串 |
| `enum` | 第一个枚举值，编号必须为 0 |
| `repeated` 字段 | 空列表 |
| `map` 字段 | 空 map |
| 消息字段 | 未设置（C++ 里表现为没有 `has_` 或指针为空/默认实例） |

默认值本身不是“字段被设置了默认值”，而是“没有值时读出来的结果”。这个区别是理解 presence 的起点。

#### 隐式存在性与显式存在性

proto3 早期版本里，普通标量字段是**隐式存在性（implicit presence）**：

- 字段没有出现在字节里，读出来是默认值；
- 字段出现在字节里，值等于默认值，读出来也是默认值；
- 生成的 C++ 接口通常没有 `has_age()`，只有 `age()` 和 `set_age()`。

也就是说，**你无法区分“没设置”和“设置成 0”**。这对很多场景没问题：一个计数器为 0、一个开关为 false、一个字符串为空，语义上确实可以合并。但对“部分更新”“字段掩码”“需要知道调用方到底传没传”的场景，就会丢信息。

从 proto3 的较新版本开始，标量字段可以加 `optional` 关键字，获得**显式存在性（explicit presence）**：

```proto
message User {
  optional int32 age = 1;
  optional string nickname = 2;
}
```

生成 C++ 代码后，会有：

```cpp
bool has_age() const;
int32_t age() const;
void set_age(int32_t value);
void clear_age();
```

`has_age()` 为真，表示“字段在字节里出现过”；为假，表示“没出现过”。**注意：显式设置为 0 会让 `has_age()` 为真，而值仍然是 0。** 这就是 acceptance 要求你能解释的区别。

消息字段（message 类型）在 proto3 中天然具有显式存在性：你可以判断一个子消息是否被设置。`repeated` 字段和 `map` 字段没有 presence 概念，空列表和“没设置”不可区分。`oneof` 成员则通过“当前是哪个 case”表达存在性。

#### 一个对比表

| 字段形式 | 能否区分“未设置”和“设置为默认值” | C++ 典型接口 | 适用场景 |
| --- | --- | --- | --- |
| proto3 普通标量 `int32 age = 1;` | 不能 | `age()`、`set_age()` | 值本身有默认语义，0 和未设置等价 |
| proto3 `optional int32 age = 1;` | 能 | `has_age()`、`age()`、`clear_age()` | 部分更新、字段掩码、需要审计“是否传了” |
| 消息字段 `User user = 1;` | 能（消息整体是否设置） | `has_user()`、`user()`、`mutable_user()` | 嵌套结构、可选子对象 |
| `repeated string tags = 1;` | 不能区分空列表与未设置 | `tags_size()`、`add_tags()`、`tags()` | 多值字段，空即无 |
| `oneof` 成员 | 能（通过 case） | `payload_case()`、`has_x()` | 互斥字段，一次只能有一个 |
| `map<string,string> labels = 1;` | 不能区分空 map 与未设置 | `labels_size()`、`labels()` | 键值标签，空即无 |

这张表要能默写。很多兼容性事故不是因为 wire type 记错，而是因为把“隐式存在性”当成了“显式存在性”。

#### `repeated` 的编码与语义

`repeated` 表示同一字段可以出现多次。对数值类型（varint、fixed32、fixed64、float、double、bool、enum），proto3 默认使用**打包编码（packed）**：所有元素放在一个 wire type 2 的字段里，前面一个总长度，后面连续排列各个值，而不是每个元素都带一个 tag。

例如：

```proto
message Sample {
  repeated int32 nums = 1;
}
```

写入 `nums = [1, 2, 3]`，打包后的字节大致是：

```text
0A 03 01 02 03
^  ^  ^^^^^^^
|  |  三个 varint 值
|  +-- 总长度 3
+----- tag：字段 1，wire type 2
```

如果是非打包（每个元素一个 tag），则是：

```text
08 01 08 02 08 03
```

解析器对两种形式都要能接受：规范要求 repeated 标量字段的解析器同时支持 packed 和 unpacked。这一点对兼容性很重要——你改了 `packed` 选项，通常不影响能否读，但会影响字节长度和某些极端场景下的实现行为。

`repeated` 的语义要点：

- **顺序保留**：元素按写入顺序读回，不会自动排序；
- **空列表等于没设置**：无法区分“传了空数组”和“没传这个字段”；
- **不是集合**：允许重复值，去重是业务逻辑的事；
- **不要用 `repeated` 模拟 `optional`**：一个元素的列表和“字段不存在”仍然是两回事，但接口会变得别扭，且无法表达“显式空列表”。

如果业务确实需要区分“空列表”和“未设置”，通常要再加一个 `bool` 标志、使用 `optional` 包装消息，或者在设计上接受两者等价。不要指望 `repeated` 本身提供 presence。

#### `oneof`：互斥字段与“当前哪个”

`oneof` 把一组字段声明为互斥：同一时刻最多只有一个成员被设置。设置其中一个会自动清掉其他成员。

```proto
message Payload {
  oneof body {
    string text = 1;
    bytes blob = 2;
    int64 number = 3;
  }
}
```

生成 C++ 代码后，通常会有：

```cpp
enum BodyCase {
  kText = 1,
  kBlob = 2,
  kNumber = 3,
  BODY_NOT_SET = 0
};

BodyCase body_case() const;
bool has_text() const;
const std::string& text() const;
void set_text(const std::string& value);
void clear_body();
```

`body_case()` 告诉你当前是哪个成员。它的价值不只是省空间，更重要的是表达“这些字段在语义上互斥”。如果你用三个独立字段加注释“只能填一个”，编译器不会帮你检查，调用方也可能同时设置两个，最后哪一个是有效的取决于读取顺序。

`oneof` 的兼容性规则和普通字段类似：新增一个成员是安全的，旧客户端会把它当作未知字段；删除成员要保留编号；把一个已有字段移进 `oneof` 或移出 `oneof` 属于不兼容变更，因为存在性语义和生成代码的接口都变了。

一个常见误区是：**`oneof` 成员设置为默认值也算“设置了”。** 例如 `set_text("")` 之后，`body_case()` 是 `kText`，而不是 `BODY_NOT_SET`。这和 `optional` 的显式存在性是一致的：只要调用了 setter，就留下了“这个成员被选中”的事实。

#### presence 在 KV 请求里的意义

把上面的概念放进本课的实践场景。假设 KV 服务有一个更新请求：

```proto
message PutRequest {
  string key = 1;
  string value = 2;

  // 0 表示不设置过期时间；但如果调用方想显式把 TTL 设为 0（永不过期），
  // 普通标量无法表达“我确实传了 0”。
  optional int64 ttl_ms = 3;

  // 标签：整体替换语义，空列表表示清空所有标签。
  repeated string tags = 4;

  // 负载只允许一种形态。
  oneof payload {
    bytes raw = 5;
    string text = 6;
  }
}
```

这段模式里每个字段的 presence 语义都不同：

- `key`、`value` 是普通标量，空字符串就是默认值，无法区分“没传”和“传了空串”。对必填字段来说，这通常可接受，但服务端要用业务校验拒绝空 key。
- `ttl_ms` 用 `optional`，可以区分“没传”（沿用旧 TTL）和“传了 0”（永不过期）。这是部分更新的典型需求。
- `tags` 是 `repeated`，空列表和没传无法区分；如果更新语义是“整体替换”，那么空列表就表示清空，这需要在文档里写清楚。如果更新语义是“增量追加”，则无法用空列表表达“清空”，需要额外的操作类型。
- `payload` 是 `oneof`，保证 `raw` 和 `text` 不会同时存在；服务端可以按 `payload_case()` 分支处理，而不是猜“两个都填了该用哪个”。

这就是“模式即契约”的具体样子：字段形式的选择本身就在表达业务语义。选错了形式，后面写多少注释都补不回来。

---
### 要点三：保留编号和代码生成版本

#### 直觉：删除字段不是“把那一行删掉”

初学者删除字段时最容易做的事，是把 `.proto` 里的那一行直接删掉，然后重新生成代码。编辑器不会报错，编译器也不会报错，服务照常启动。问题会晚几个月出现：某个旧客户端仍然发送那个编号，新服务把它当成另一个字段；或者历史数据里还带着那个编号，新服务按新语义解析，得到看似合法但完全错误的值。

Protobuf 的线格式里没有字段名，也没有“这个编号属于哪个字段”的元数据。接收方只能拿自己的模式去对照。你删掉字段，等于告诉新代码“这个编号我不认识”，这本身没问题：未知字段会被跳过或保留。真正危险的是**你把编号分配给了新字段**。这时旧字节里的编号会被新代码当成新字段来解释，而且因为 wire type 可能碰巧兼容，错误不会以“解析失败”的形式暴露，而是变成静默的数据污染。

所以删除字段的正确动作是两步：

1. 从消息里删掉字段声明；
2. 用 `reserved` 把这个编号和字段名标记为“永远不要再用”。

#### `reserved` 的写法

```proto
message User {
  reserved 5, 7, 11 to 13;
  reserved "old_nickname", "legacy_id";

  string id = 1;
  string name = 2;
}
```

`reserved` 可以保留编号、编号区间和字段名。建议编号和名字都保留：

- 保留编号，防止未来有人复用线格式上的身份；
- 保留名字，防止有人用同一个名字重新引入字段，导致代码、日志和文档产生歧义；
- 区间写法 `11 to 13` 适合批量删除的连续编号；
- 不要用 `reserved` 来“预占”一堆没用过的编号。它是墓碑，不是规划工具。真正需要预留时，应在设计文档里说明，而不是把编号锁死。

`protoc` 会检查冲突：如果新模式里既有 `reserved 5`，又有 `int32 x = 5;`，编译会失败。这正是我们想要的——把错误从运行期提前到生成期。

#### 一个复用编号的事故模型

假设 v1 有：

```proto
message Record {
  string key = 1;
  string user_id = 2;   // 编号 2，wire type 2
}
```

v2 删除了 `user_id`，但没有保留编号，并且新增了：

```proto
message Record {
  string key = 1;
  int32 ttl_ms = 2;     // 复用了编号 2，wire type 0
}
```

现在 v1 写出的字节是：

```text
0A 07 75 73 65 72 3A 34 32    // key = "user:42"
12 05 61 6C 69 63 65          // 编号 2，wire type 2，长度 5，"alice"
```

v2 读到第二个字段时，看到编号 2，按自己的模式期望 wire type 0 的 `int32`。但字节里是 wire type 2。规范的解析器会把 wire type 不匹配视为未知字段或解析错误，具体行为取决于实现和版本；如果 wire type 恰好也兼容（例如旧字段是 `int32`，新字段是 `bool`，都用 wire type 0），那么 `alice` 这段字节根本不会被当成字符串，而是被读成一个整数，结果是一个看似合法但毫无意义的值。更糟的情况是旧字段是 `string`，新字段是 `bytes`，两者都是 wire type 2，解析器会原样接受这段字节，业务层拿到一堆“看起来像用户 ID 的二进制”。

这类事故的可怕之处在于：**没有崩溃，没有异常，只有错的数据。** 而 `reserved` 能把这种错误挡在编译期。

#### 模式演进的安全等级

把常见变更按兼容性分成三档。这里的“兼容”主要指线格式兼容，也就是旧代码能否读新数据、新代码能否读旧数据；API 兼容是另一回事，字段改名会让源码编译失败，但通常不影响线格式。

| 变更 | 线格式兼容性 | 说明 |
| --- | --- | --- |
| 新增字段并使用新编号 | 安全 | 旧代码把它当未知字段跳过或保留；新代码读旧数据时得到默认值/未设置 |
| 删除字段并 `reserved` 编号与名字 | 安全 | 旧数据里的该字段成为未知字段；新代码不会误读 |
| 删除字段但不保留编号 | 危险 | 未来可能被复用，造成静默误读 |
| 字段改名 | 线格式安全，API 不兼容 | 编号不变则字节不变；源码里的 getter/setter 名字会变 |
| 字段重排 | 线格式安全 | 线格式按编号，不按声明顺序 |
| 修改字段编号 | 不兼容 | 等于删掉旧字段再加一个新字段，旧数据读不到 |
| `int32` 与 `int64` 互换 | 通常线格式兼容 | 都用 varint；但截断/符号扩展要小心，负数编码行为不同 |
| `int32` 与 `sint32` 互换 | 不兼容 | 负数编码方式不同（zigzag 与补码） |
| `string` 与 `bytes` 互换 | 线格式通常兼容 | 都是长度分隔；但 `string` 要求 UTF-8，非法 UTF-8 可能被拒绝 |
| `fixed32` 与 `sfixed32` 互换 | 线格式兼容 | 都是 4 字节；符号解释不同 |
| `fixed32` 与 `int32` 互换 | 不兼容 | wire type 不同（5 与 0） |
| 在 `oneof` 中新增成员 | 安全 | 旧代码视为未知字段；新代码能识别新 case |
| 把普通字段移入/移出 `oneof` | 不兼容 | 存在性语义和生成接口改变 |
| `repeated` 标量在 packed 与 unpacked 间切换 | 通常兼容 | 规范要求解析器同时接受两种形式；但字节布局变化 |
| 修改枚举值编号 | 危险 | 旧代码可能把新编号当未知枚举值；删除枚举值也要 `reserved` |

这张表不是让你背下来应付考试，而是让你在改模式时先问三个问题：

1. 旧代码读到新字节，会不会把未知字段误当成已知字段？
2. 新代码读到旧字节，会不会把缺失字段误当成“显式设置为默认值”？
3. 有没有任何编号或枚举值被回收再利用？

只要第三问的答案是“有”，就停下来，改成 `reserved`。

#### 枚举的保留

枚举也有编号复用问题：

```proto
enum Status {
  STATUS_UNSPECIFIED = 0;
  STATUS_ACTIVE = 1;
  STATUS_DELETED = 2;
  reserved 3, 4;
  reserved "STATUS_LEGACY";
}
```

枚举的第一个值必须是 0，这是 proto3 的要求，目的是让默认值与“未设置”有一个明确的哨兵值。删除枚举值时同样要保留编号，否则以后新增的值可能和旧数据里的编号冲突。枚举的未知值在 proto3 中会被保留为原始数字（具体行为取决于语言和实现），业务代码要有兜底分支，不能假设只有声明过的值。

#### 代码生成：`protoc` 做了什么

`.proto` 文件本身不能直接参与 C++ 编译。你需要用 `protoc` 把它翻译成 `.pb.h` 和 `.pb.cc`：

```bash
protoc --proto_path=proto \
       --cpp_out=generated \
       proto/kv.proto
```

这条命令会生成：

```text
generated/kv.pb.h
generated/kv.pb.cc
```

生成的头文件里通常包含：

- 每个 message 对应的 C++ 类；
- 字段的 getter/setter、`has_`、`clear_`、`mutable_`；
- `repeated` 字段的 `add_`、`_size()`、下标访问；
- `oneof` 的 case 枚举和 `_case()`；
- 序列化接口 `SerializeToString`、`SerializeToArray`；
- 解析接口 `ParseFromString`、`ParseFromArray`；
- 描述符（descriptor）相关代码，供反射和调试使用。

生成代码不是“可选依赖”。你的程序必须链接 Protobuf 运行时库（常见的是 `libprotobuf`），因为生成类里的大量逻辑——varint 编解码、未知字段处理、消息反射——都在运行时库里。

#### 版本：生成器、运行时、语言绑定

Protobuf 的版本问题经常被低估。一次构建至少涉及三个东西：

1. **`protoc` 生成器版本**：把 `.proto` 翻译成 C++ 的编译器；
2. **Protobuf 运行时库版本**：你的程序链接的 `libprotobuf`；
3. **语言绑定/构建集成版本**：CMake 的 `find_package(Protobuf)`、包管理器里的 `protobuf` 包、或其他构建插件。

三者不一致时，可能出现：

- 生成代码使用了运行时库里不存在的新 API，链接失败；
- 生成代码和运行时对某个 wire type 或未知字段的处理假设不同；
- 不同机器上生成的 `.pb.cc` 不一致，导致构建缓存失效或行为差异；
- 重新生成后接口变了，但旧的目标文件没有重新编译，出现难以解释的符号错误。

实用做法：

- **固定 `protoc` 版本**。在 CI、开发机和发布镜像里用同一个版本；把版本号写进构建脚本或工具链文件。
- **固定运行时版本**。用包管理器锁定版本，或者把 Protobuf 作为子模块/第三方依赖固定 commit。
- **让生成步骤成为构建的一部分**，而不是“某个人手动跑一下然后把生成文件提交上去”。如果必须提交生成文件，要在 CI 里校验“重新生成后没有差异”。
- **在生成文件头里记录版本**。`protoc` 生成的代码通常带有版本宏和注释，排障时先看这个。
- **升级时一起升级**。不要只升级 `protoc` 不升级运行时，也不要反过来；升级后跑一遍兼容性测试和旧数据读取测试。

一个最小但可复现的生成命令，应该把输入、输出和版本都写清楚：

```bash
#!/usr/bin/env bash
set -euo pipefail

PROTOC_VERSION="$(protoc --version)"
echo "using ${PROTOC_VERSION}"

rm -rf generated
mkdir -p generated

protoc --proto_path=proto \
       --cpp_out=generated \
       proto/kv.proto

# 记录版本，供构建审计和排障使用
printf '%s\n' "${PROTOC_VERSION}" > generated/PROTOC_VERSION
```

#### 生成代码与手写代码的边界

生成代码应该被视为“构建产物”，而不是“可以随手改的源码”。理由很实际：

- 下次生成会覆盖你的修改；
- 生成代码的结构和命名不是稳定 API，升级 `protoc` 可能变化；
- 把业务逻辑塞进生成文件，会让模式变更和业务变更耦合在一起。

正确做法是：生成代码只负责数据承载和编解码；业务逻辑写在你自己的类、函数或适配层里。需要给生成类型加行为时，用自由函数、包装类型或转换函数，而不是改 `.pb.h`。

#### 包名与命名空间

`.proto` 里的 `package` 会影响生成 C++ 的命名空间：

```proto
syntax = "proto3";

package atlas.kv.v1;

message GetRequest {
  string key = 1;
}
```

生成的 C++ 类型通常是 `atlas::kv::v1::GetRequest`。包名建议带版本（`v1`），这样未来做不兼容的大改时，可以让 `v1` 和 `v2` 的消息并存，而不是在同一个包里做危险的原地修改。版本化包名不是万能的，但它给了你一条清晰的迁移路径。

---

## 常见误区与设计权衡

### 误区一：删除字段就是把行删掉

这是本课最想纠正的误区。删除字段必须配合 `reserved`，并且要同时保留编号和名字。只删行不保留，等于把编号放回可分配池，未来任何人（包括三个月后的你）都可能踩中。

### 误区二：把序列化字节当稳定哈希

节点 pitfalls 明确说：**不应把序列化字节作为稳定规范化哈希。**

原因可以列成一串：

- 字段的写入顺序不保证固定，不同实现或不同版本可能不同；
- `map` 的遍历顺序不保证固定，同样的逻辑 map 可能序列化成不同字节；
- 未知字段的存在会影响字节；
- `repeated` 标量的 packed 与 unpacked 表示可能不同；
- 不同版本的运行时可能改变某些边界情况的编码；
- 浮点数的 NaN 表示、负零等也可能带来差异。

如果你需要“内容指纹”，正确方向是：定义一套明确的规范化规则（哪些字段参与、按什么顺序、如何编码），然后对规范化后的内容做哈希。不要对“某个实现这次生成的字节”做哈希，更不要把这种哈希写进跨版本的持久化键。

### 误区三：默认值就是“字段存在且等于默认值”

默认值是“没有值时读出来的结果”，不是“字段被显式设置”。普通标量字段无法区分这两者；`optional`、消息字段和 `oneof` 可以表达存在性。做部分更新、字段掩码、审计“调用方到底传没传”时，必须选对字段形式。

### 误区四：字段顺序和字段名会影响线格式

不会。线格式只看编号和 wire type。字段名只影响源码 API 和可读性；字段顺序只影响 `.proto` 文件的阅读体验。重命名和重排通常是线格式安全的（但重命名会破坏源码兼容）。真正危险的是改编号和复用编号。

### 误区五：Protobuf 是加密的，或者自带压缩

不是。Protobuf 只做结构化编码，不提供机密性和完整性保护，也不做通用压缩。它通过 varint、省略字段名、紧凑的 tag 获得较小的体积，但这不等于压缩。需要保密就上 TLS 或应用层加密；需要压缩就单独做压缩，并注意压缩层和消息边界的配合。

### 误区六：未知字段一定会被安全保留

不同版本、不同语言、不同解析路径对未知字段的处理不完全一样。较新的 proto3 实现通常会保留未知字段并在重新序列化时写回，但这不是你可以随意依赖的业务保证。跨版本传输时，最稳妥的做法是：不要指望未知字段“自动无损”，而是在契约里明确哪些字段会被保留、哪些会被丢弃，并为关键字段设计显式的前向兼容方案。

### 误区七：`repeated` 能表达“空列表”和“未设置”的区别

不能。`repeated` 字段的空列表和未设置在读端通常无法区分。需要区分时，要么加一个显式的布尔字段，要么把列表包进一个消息并用 `optional` 表达“这个消息是否出现”，要么在业务上接受两者等价。不要用“列表长度是否为 0”来推断调用方意图。

### 误区八：改完 `.proto` 重新生成就万事大吉

代码生成只保证“新代码和模式一致”，不保证“新代码和旧数据、旧客户端一致”。每次改模式都要做兼容性判断，并且用旧版本写出的真实字节做回归测试。生成成功不等于兼容。

### 权衡：二进制紧凑跨语言，但人读与排障不如文本

节点 tradeoffs 的原话是：**二进制紧凑且跨语言，但人读与手工排障不如文本直观。**

这条权衡要在设计时正面接受，而不是假装不存在：

- **紧凑**：字段名不传输，varint 压缩小整数，常用字段 tag 只占一字节；在带宽和存储敏感的场景很有价值。
- **跨语言**：一份模式生成多语言类型，减少“字段名拼错、类型不一致”的联调成本。
- **人读差**：抓包看到的是十六进制；没有模式就无法解释；字段编号和类型要靠 `.proto` 对照。
- **手工排障成本**：需要 `protoc --decode`、需要描述符、需要能把字节和模式对应起来的工具。

缓解手段：

- 在日志里同时打印结构化字段和必要的原始长度，而不是只打印原始字节；
- 为关键消息保留 `protoc --decode` 的排障命令；
- 用 `protoc --decode_raw` 做“没有模式时的粗略查看”，但要明白它只能猜结构，不能保证语义；
- 对持久化数据，保存模式版本或描述符，避免“数据还在、模式丢了”；
- 对文本友好的场景（配置、调试接口），不要强行用二进制，JSON/YAML 可能更合适。

---

## 动手实践：定义 KV 请求响应，生成 C++ 代码并测试旧新版本互读

本节点的 practice 是：**定义 KV 请求响应，生成 C++ 代码并测试旧新版本互读。** 下面给出从零可跑的一套最小工程。重点有三个：

1. 模式里同时包含 v1 和 v2 的演进痕迹（新增字段、删除字段、`reserved`）；
2. 用 v1 写出的字节让 v2 读，用 v2 写出的字节让 v1 读；
3. 用测试证明“默认值不等于存在性”，并证明“删除字段后编号被保留”。

### 第一步：建立目录

```text
kv_proto/
├── CMakeLists.txt
├── proto/
│   └── kv.proto
├── v1/
│   └── main.cc
├── v2/
│   └── main.cc
└── generated/          # protoc 输出，构建时生成
```

为了演示“旧新版本互读”，我们把 v1 和 v2 的模式分别保存成两份文件。真实项目里通常是同一份模式配 Git 历史，这里拆开只是为了让你能在同一台机器上同时编译两个版本。

### 第二步：定义 v1 模式

`proto/kv_v1.proto`：

```proto
syntax = "proto3";

package atlas.kv.v1;

message PutRequest {
  string key = 1;
  string value = 2;

  // v1 的旧字段，v2 会删除它，用来演示 reserved。
  string legacy_session = 3;
}

message PutResponse {
  bool ok = 1;
  string error = 2;
}

message GetRequest {
  string key = 1;
}

message GetResponse {
  bool found = 1;
  string value = 2;
}
```

### 第三步：定义 v2 模式

`proto/kv_v2.proto`：

```proto
syntax = "proto3";

package atlas.kv.v1;   // 包名保持 v1，表示仍在线格式兼容范围内演进

message PutRequest {
  string key = 1;
  string value = 2;

  // 删除 legacy_session 后，编号和名字都必须保留。
  reserved 3;
  reserved "legacy_session";

  // 显式存在性：区分“没传 TTL”和“显式传了 0”。
  optional int64 ttl_ms = 4;

  // 标签：空列表与未设置在 repeated 上不可区分。
  repeated string tags = 5;

  // 负载互斥：raw 和 text 只能有一个。
  oneof payload {
    bytes raw = 6;
    string text = 7;
  }
}

message PutResponse {
  bool ok = 1;
  string error = 2;
}

message GetRequest {
  string key = 1;
}

message GetResponse {
  bool found = 1;
  string value = 2;
}
```

注意这里故意让两个文件使用同一个 `package atlas.kv.v1`，但文件名不同。真实项目里不要把同一包的两份不同定义同时链接进一个二进制，否则会出现类型重复定义或描述符冲突。本实践是分开编译两个可执行文件，各自只包含自己那份模式。

### 第四步：生成 C++ 代码

```bash
# 版本固定与记录
protoc --version

protoc --proto_path=proto --cpp_out=generated proto/kv_v1.proto
protoc --proto_path=proto --cpp_out=generated proto/kv_v2.proto

ls -l generated
```

生成结果：

```text
kv_v1.pb.h
kv_v1.pb.cc
kv_v2.pb.h
kv_v2.pb.cc
```

### 第五步：v1 程序——写旧格式

`v1/main.cc`：

```cpp
#include "kv_v1.pb.h"

#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: kv_v1 <output.bin>\n";
        return 2;
    }

    atlas::kv::v1::PutRequest req;
    req.set_key("user:42");
    req.set_value("alice");
    req.set_legacy_session("session-from-v1");

    std::string bytes;
    if (!req.SerializeToString(&bytes)) {
        std::cerr << "serialize failed\n";
        return 1;
    }

    std::ofstream out(argv[1], std::ios::binary);
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    if (!out) {
        std::cerr << "write failed\n";
        return 1;
    }
    std::cout << "v1 wrote " << bytes.size() << " bytes\n";

    // 顺手打印十六进制，方便和线格式对照。
    for (unsigned char c : bytes) {
        std::printf("%02X ", c);
    }
    std::printf("\n");
    return 0;
}
```

### 第六步：v2 程序——读旧格式、写新格式

`v2/main.cc`：

```cpp
#include "kv_v2.pb.h"

#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>

static std::string read_file(const char* path) {
    std::ifstream in(path, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(in),
                       std::istreambuf_iterator<char>());
}

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: kv_v2 <input.bin> <output.bin>\n";
        return 2;
    }

    // ---- 场景 A：v2 读 v1 写出的字节 ----
    const std::string old_bytes = read_file(argv[1]);
    atlas::kv::v1::PutRequest from_v1;
    if (!from_v1.ParseFromString(old_bytes)) {
        std::cerr << "v2 failed to parse v1 bytes\n";
        return 1;
    }

    assert(from_v1.key() == "user:42");
    assert(from_v1.value() == "alice");

    // v1 的 legacy_session 在 v2 里已删除并 reserved。
    // v2 的模式里没有这个字段，所以它只能作为未知字段存在，业务代码读不到。
    assert(!from_v1.has_ttl_ms());   // 旧数据里没有 ttl_ms
    assert(from_v1.tags_size() == 0);
    assert(from_v1.payload_case() == atlas::kv::v1::PutRequest::PAYLOAD_NOT_SET);

    std::cout << "v2 read v1: key=" << from_v1.key()
              << " value=" << from_v1.value()
              << " has_ttl=" << from_v1.has_ttl_ms()
              << " tags=" << from_v1.tags_size() << "\n";

    // ---- 场景 B：v2 写新字节，包含显式 0 的 optional ----
    atlas::kv::v1::PutRequest out;
    out.set_key("user:42");
    out.set_value("alice");
    out.set_ttl_ms(0);               // 显式设置为 0
    out.add_tags("beta");
    out.add_tags("canary");
    out.set_text("hello");

    assert(out.has_ttl_ms());        // 关键：设置过就是 true
    assert(out.ttl_ms() == 0);
    assert(out.payload_case() == atlas::kv::v1::PutRequest::kText);

    std::string new_bytes;
    if (!out.SerializeToString(&new_bytes)) {
        std::cerr << "v2 serialize failed\n";
        return 1;
    }

    std::ofstream f(argv[2], std::ios::binary);
    f.write(new_bytes.data(), static_cast<std::streamsize>(new_bytes.size()));
    if (!f) {
        std::cerr << "v2 write failed\n";
        return 1;
    }

    std::cout << "v2 wrote " << new_bytes.size() << " bytes\n";
    return 0;
}
```

这段代码里用 `atlas::kv::v1::PutRequest` 同时承载 v1 和 v2 的字节，是因为两个文件用了同一个包名和消息名。编译时每个可执行文件只包含自己那份生成的 `.pb.cc`，所以类型名一致但定义不冲突。真实项目里不要让两份定义进入同一个链接单元。

### 第七步：v1 读 v2 字节的程序

再写一个 `v1/read_new.cc`，用 v1 的类型去解析 v2 写出的字节：

```cpp
#include "kv_v1.pb.h"

#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: v1_read_new <input.bin>\n";
        return 2;
    }

    std::ifstream in(argv[1], std::ios::binary);
    std::string bytes(std::istreambuf_iterator<char>(in),
                      std::istreambuf_iterator<char>());

    atlas::kv::v1::PutRequest req;
    if (!req.ParseFromString(bytes)) {
        std::cerr << "v1 failed to parse v2 bytes\n";
        return 1;
    }

    // v1 认识的字段仍然正确。
    assert(req.key() == "user:42");
    assert(req.value() == "alice");

    // v1 不认识的字段（ttl_ms、tags、payload）被当作未知字段跳过。
    // v1 的 legacy_session 没有被 v2 写入，所以是空字符串。
    assert(req.legacy_session().empty());

    std::cout << "v1 read v2: key=" << req.key()
              << " value=" << req.value()
              << " legacy_session='" << req.legacy_session() << "'\n";
    return 0;
}
```

### 第八步：CMake 构建

`CMakeLists.txt`：

```cmake
cmake_minimum_required(VERSION 3.16)
project(kv_proto_demo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Protobuf REQUIRED)

# 生成 v1
protobuf_generate_cpp(V1_SRCS V1_HDRS proto/kv_v1.proto)
add_executable(kv_v1 v1/main.cc ${V1_SRCS} ${V1_HDRS})
target_include_directories(kv_v1 PRIVATE
    ${CMAKE_CURRENT_BINARY_DIR}
    ${Protobuf_INCLUDE_DIRS})
target_link_libraries(kv_v1 PRIVATE ${Protobuf_LIBRARIES})

# 生成 v2
protobuf_generate_cpp(V2_SRCS V2_HDRS proto/kv_v2.proto)
add_executable(kv_v2 v2/main.cc ${V2_SRCS} ${V2_HDRS})
target_include_directories(kv_v2 PRIVATE
    ${CMAKE_CURRENT_BINARY_DIR}
    ${Protobuf_INCLUDE_DIRS})
target_link_libraries(kv_v2 PRIVATE ${Protobuf_LIBRARIES})

# v1 读新字节
add_executable(v1_read_new v1/read_new.cc ${V1_SRCS} ${V1_HDRS})
target_include_directories(v1_read_new PRIVATE
    ${CMAKE_CURRENT_BINARY_DIR}
    ${Protobuf_INCLUDE_DIRS})
target_link_libraries(v1_read_new PRIVATE ${Protobuf_LIBRARIES})
```

构建与运行：

```bash
cmake -S . -B build
cmake --build build -j

./build/kv_v1 v1.bin
./build/kv_v2 v1.bin v2.bin
./build/v1_read_new v2.bin
```

预期输出类似：

```text
v1 wrote 41 bytes
...
v2 read v1: key=user:42 value=alice has_ttl=0 tags=0
v2 wrote 39 bytes
v1 read v2: key=user:42 value=alice legacy_session=''
```

具体字节数会随字段和实现略有差异。**不要断言字节总数或十六进制完全等于某个固定值**，因为序列化顺序、未知字段和运行时版本都可能影响结果。要断言的是语义：字段值、`has_`、`_case()`、数量。

### 第九步：测试“默认值不等于存在性”

单独写一个测试，覆盖三种情况：

```cpp
#include "kv_v2.pb.h"

#include <cassert>
#include <string>

int main() {
    using atlas::kv::v1::PutRequest;

    {
        PutRequest r;
        assert(!r.has_ttl_ms());
        assert(r.ttl_ms() == 0);        // 读出来是默认值
    }

    {
        PutRequest r;
        r.set_ttl_ms(0);                // 显式设置为默认值
        assert(r.has_ttl_ms());         // 存在性为真
        assert(r.ttl_ms() == 0);
    }

    {
        PutRequest r;
        r.set_ttl_ms(0);
        r.clear_ttl_ms();
        assert(!r.has_ttl_ms());        // clear 后存在性为假
        assert(r.ttl_ms() == 0);
    }

    {
        PutRequest r;
        std::string bytes;
        r.SerializeToString(&bytes);

        PutRequest parsed;
        assert(parsed.ParseFromString(bytes));
        assert(!parsed.has_ttl_ms());   // 没设置就不会出现在字节里
    }

    {
        PutRequest r;
        r.set_ttl_ms(0);
        std::string bytes;
        r.SerializeToString(&bytes);

        PutRequest parsed;
        assert(parsed.ParseFromString(bytes));
        assert(parsed.has_ttl_ms());    // 显式 0 会保留存在性
        assert(parsed.ttl_ms() == 0);
    }

    return 0;
}
```

这个测试就是 acceptance 第二条的证据。它明确区分了四个状态：

1. 未设置：`has=false`，读取为 0；
2. 显式设置为 0：`has=true`，读取为 0；
3. 设置后清除：`has=false`，读取为 0；
4. 序列化后解析：显式 0 的存在性仍然保留。

### 第十步：测试“删除字段后保留编号”

先验证正常情况：v1 写出的 `legacy_session` 不会让 v2 解析失败，v2 读到的 `key` 和 `value` 正确。再把下面这段故意写错的内容加进 `kv_v2.proto`：

```proto
message PutRequest {
  string key = 1;
  string value = 2;

  reserved 3;
  reserved "legacy_session";

  int32 reused = 3;   // 故意与 reserved 冲突
}
```

然后运行：

```bash
protoc --proto_path=proto --cpp_out=generated proto/kv_v2.proto
```

预期 `protoc` 报错，类似：

```text
kv_v2.proto: Field "reused" uses reserved number 3.
```

不同版本的报错文案可能略有差异，但一定不会生成成功。这个失败就是证据：`reserved` 把“复用编号”从运行期事故变成了生成期错误。验证完记得把错误字段删掉。

再做一个“没有 reserved 就会出事”的对照实验：在临时副本里去掉 `reserved`，把 `legacy_session` 的编号 3 分配给 `int32 reused = 3;`，用 v1 写出一条包含 `legacy_session` 的字节，再用这个错误的 v2 去解析。你会看到两种可能：

- wire type 不匹配（string 是 2，int32 是 0），解析器把它当未知字段或报错；
- 如果换成一个同样使用 wire type 2 的 `bytes reused = 3;`，解析器会安静地接受这段字节，业务层拿到一个错误的“reused”值。

这个对照实验比任何说教都更能说明为什么不能复用编号。

### 第十一步：验收清单

做完上面的步骤，逐条打勾：

- [ ] `.proto` 里有 v1 和 v2 两个版本，v2 明确删除了一个字段。
- [ ] 删除的字段在 v2 里同时用 `reserved` 保留了编号和名字。
- [ ] 故意复用编号时，`protoc` 报错。
- [ ] v1 写出的字节能被 v2 成功解析，`key`、`value` 正确。
- [ ] v2 写出的字节能被 v1 成功解析，v1 认识的字段正确，不认识的字段被跳过。
- [ ] 有测试证明“未设置”和“显式设置为 0”在 `optional` 字段上可区分。
- [ ] 有测试证明 `repeated` 空列表和未设置不可区分（或明确记录这一限制）。
- [ ] 有测试证明 `oneof` 的 `_case()` 能正确反映当前成员，设置为默认值也算选中。
- [ ] 构建脚本固定并记录了 `protoc` 版本。
- [ ] 没有把序列化字节的长度或内容当作稳定哈希或固定断言。

### 第十二步：把实验变成回归

真实项目要把上面的手工实验变成 CI：保存旧版本真实写出的字节作为黄金样本，每次改模式后用新代码解析并断言关键字段，再用旧解析器读新字节；同时检查所有已删除字段都被 `reserved` 覆盖、生成器与运行时版本在允许范围内。不要把“当前代码重新生成”的字节当样本，那只能证明新代码自洽，不能证明旧数据可读。

---

## 延伸阅读：sourceStudy

本节点 sourceStudy 的要求是：**沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。**

这条要求可以落实为三件事：

### 动作一：定位定义并对照示例

打开资源页面后，先找到三组定义，不要从第一段顺读到尾：

1. **字段编号与 wire type**：tag 的位布局、varint 规则、wire type 的取值；
2. **presence**：隐式/显式存在性、`optional`、消息字段、`oneof` 怎样表达“有没有值”；
3. **`repeated` 与 `oneof`**：打包编码、顺序、空列表、互斥成员的设置与清除。

每读到一条定义，回到本课对应段落复述并举一个反例；然后对照示例确认：编号为什么这样分配、tag/长度/值怎样排列、`optional` 在未设置与显式默认值时接口返回什么、`oneof` 设置一个成员后另一个怎样被清除。若示例与手头 `protoc` 行为不一致，以实际版本为准并记录差异。

### 动作二：走错误处理路径并记录版本

不要只看“正确输入会怎样”，还要看“错误输入会怎样”：截断的 varint、长度前缀超过剩余字节、长度超过实现上限、未知 wire type、非法 UTF-8、未知枚举值，以及解析失败时消息对象处于什么状态、部分字段是否已被修改。这些答案不同语言和版本可能不同，把“资源怎么说”和“运行时实际怎么做”分开记录。

记录版本与边界时，建议维护一张小表：

| 项目 | 记录内容 |
| --- | --- |
| `protoc --version` | 实际生成器版本 |
| Protobuf 运行时版本 | `libprotobuf` 或包管理器版本 |
| 语言与标准/验证日期 | 例如 C++17，以及实际测试日期 |
| 已确认行为 | tag 布局、varint、presence、packed、未知字段 |
| 未确认/存疑 | 资源没写清、或与实现不一致的点 |
| 边界条件 | 最大长度、递归深度、非法 UTF-8、未知枚举 |

这份记录不是形式主义。Protobuf 的行为在版本之间有过变化，尤其是 proto3 的 presence 和未知字段处理。把版本和边界写下来，下次升级或排障时就不用重新猜。

---

## 自测题

先别看答案。能对着三个要点和 practice 讲清楚，再对照答案检查。

### 题 1：wire type 与 tag

**问题**：一条消息里有一个字段编号 3、类型 `int32`，值为 1；还有一个字段编号 2、类型 `string`，值为 `"hi"`。请回答：

1. 这两个字段的 wire type 分别是什么？
2. 它们的 tag 分别是多少（十进制和十六进制）？
3. 按“先字段 3、后字段 2”的顺序，写出完整字节。
4. 如果把字段声明顺序改成“先 string 后 int32”，字节会变吗？为什么？

**答案**：

1. `int32` 是 varint，wire type 0；`string` 是长度分隔，wire type 2。
2. 字段 3 的 tag = `(3 << 3) | 0 = 24 = 0x18`；字段 2 的 tag = `(2 << 3) | 2 = 18 = 0x12`。
3. 字段 3 的值为 1，编码为 `18 01`；字段 2 的值为 `"hi"`，长度 2，编码为 `12 02 68 69`。完整字节：`18 01 12 02 68 69`。
4. 不会。线格式按字段编号编码，不按声明顺序。解析器读到 tag 后按编号查找字段，所以声明顺序改变不影响字节；但如果把字段编号从 3 改成别的值，字节就会变，旧代码也会把它当未知字段。

### 题 2：默认值与存在性

**问题**：一个 proto3 消息里有三个字段：

```proto
message M {
  int32 a = 1;
  optional int32 b = 2;
  repeated int32 c = 3;
}
```

请回答：

1. 完全不设置任何字段时，`a()`、`has_b()`、`b()`、`c_size()` 分别是什么？
2. 设置 `a = 0`、`b = 0` 后，能否区分“设置过 0”和“没设置”？分别说明。
3. 序列化后解析，`b` 的存在性是否保留？为什么？
4. 如果业务需要区分“调用方传了空列表”和“调用方没传 `c`”，应该怎样设计？

**答案**：

1. `a()` 返回 0；`has_b()` 返回 false；`b()` 返回 0；`c_size()` 返回 0。
2. `a` 不能区分：普通标量是隐式存在性，设置为 0 和未设置在读端都表现为 0，生成接口也没有 `has_a()`。`b` 能区分：`optional` 提供显式存在性，设置 `b = 0` 后 `has_b()` 为 true，未设置时为 false。
3. 保留。只要显式设置了 `b`，即使值是 0，序列化时也会记录该字段（或至少记录其存在性），解析后 `has_b()` 仍为 true。具体字节表示取决于实现，但语义上存在性被保留。
4. `repeated` 本身无法区分空列表和未设置。可以增加一个 `bool has_c = 4;` 作为显式标志，或者把列表包进一个消息并用 `optional` 表达“这个消息是否出现”，或者在契约中明确“空列表等于清空/等于未设置”，让两者在业务上等价。不要用 `c_size() == 0` 去猜调用方意图。

### 题 3：演进、保留编号与哈希

**问题**：你的团队维护一份已经上线两年的模式。现在要删除字段 `string legacy_session = 3;`，并新增 `int64 ttl_ms = 4;`。请回答：

1. 删除 `legacy_session` 时，`.proto` 里必须做什么？为什么不能只删掉那一行？
2. 如果把编号 3 直接分配给 `int64 ttl_ms = 3;`，可能出现什么后果？请分“wire type 兼容”和“wire type 不兼容”两种情况说明。
3. 有同事建议用 `SerializeToString()` 得到的字节做 SHA-256，作为请求去重键。这个方案有什么问题？应该怎样改？
4. 升级 `protoc` 时要注意什么？为什么“生成成功”不等于“兼容”？

**答案**：

1. 必须用 `reserved 3;` 和 `reserved "legacy_session";` 同时保留编号和名字。只删行会把编号放回可分配池，未来任何人新增字段时都可能复用编号，导致旧数据或旧客户端被静默误读。
2. 如果新字段与旧字段 wire type 兼容（例如把 `string` 换成 `bytes`，都用 wire type 2），旧字节会被新代码当作合法的新字段接受，业务层拿到错误的值，且不会报错。如果 wire type 不兼容（例如 `string` 是 2，`int64` 是 0），解析器可能把该字段当未知字段跳过或报解析错误，具体取决于实现；无论哪种，旧客户端发来的数据都无法被正确解释，行为不可依赖。
3. 问题在于同样逻辑内容不保证序列化成相同字节：字段顺序、`map` 遍历顺序、未知字段、packed 表示、运行时版本都可能造成差异。应该先定义明确的规范化规则（参与字段、顺序、编码方式），对规范化后的内容做哈希；或者使用业务键加版本号，而不是对序列化字节直接做哈希。
4. 要同时固定 `protoc`、运行时库和语言绑定的版本，升级后跑旧数据读取、新旧互读和黄金样本回归。生成成功只证明“新代码和模式一致”，不证明“新代码和旧数据、旧客户端一致”；兼容性要靠模式演进规则和回归测试保证。

---

## 推荐资源

- **Protobuf模式与代码生成：参考文档 / 原始资料**
  - 链接：https://protobuf.dev/programming-guides/proto3/
  - 来源：protobuf.dev
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：按节点建议，重点阅读字段编号和 wire type 与 presence、`repeated` 和 `oneof`，用于核对本节点实验的语义和边界。阅读时抓住：tag 的位布局和 varint 编码；proto3 的隐式存在性与 `optional` 的显式存在性有什么区别；`repeated` 的打包编码和空列表语义；`oneof` 的设置、清除和 `case` 判断；删除字段时 `reserved` 的写法；以及默认值、未知字段和错误处理在不同版本下的边界。把文档里的约束映射回本课的 KV 模式：哪些字段该用普通标量，哪些该用 `optional`，哪些该用 `oneof`，删除字段时编号和名字是否都保留。页面没有展开的 C++ 生成接口、构建集成和新旧版本互读实验，以本课讲解与实践为准。

---

## 本课小结

1. **模式即契约。** `.proto` 不是某个语言的结构体，而是跨语言的数据约定；生成代码把约定变成类型和编解码逻辑。这是本节点 summary 的第一层。
2. **字段编号是线格式上的身份。** 字段名不传输，声明顺序不影响解码；tag = `(field_number << 3) | wire_type`。编号选得小，tag 更短；编号 1 到 15 适合常用字段。
3. **wire type 决定值怎么读。** 0 是 varint，1 是 64 位，2 是长度分隔，5 是 32 位；`int32` 和 `bool` 共用 wire type 0，`string` 和嵌入消息共用 wire type 2，必须结合模式才能确定语义。
4. **varint 是可变长整数。** 每字节低 7 位是数据，最高位表示是否继续；解析时必须检查截断和长度上限，不能信任对方给的长度。
5. **默认值不等于存在性。** proto3 普通标量是隐式存在性，无法区分“没设置”和“设置为默认值”；`optional` 提供显式存在性；消息字段、`oneof` 也能表达存在性。
6. **`repeated` 不提供 presence。** 空列表和未设置通常不可区分，元素顺序保留，数值类型默认 packed；需要区分时用显式标志或包装消息。
7. **`oneof` 表达互斥。** 设置一个成员会清除其他成员，`_case()` 反映当前选择；设置为默认值也算选中。不要把互斥字段写成多个独立字段加注释。
8. **删除字段必须留墓碑。** 用 `reserved` 同时保留编号和名字，防止未来复用造成静默误读。复用编号可能不报错，只产生错数据，这是最危险的兼容性事故。
9. **演进要看双向。** 旧代码读新字节、新代码读旧字节都要检查；新增字段通常安全，改编号不兼容，`int32`/`sint32` 互换不兼容，普通字段与 `oneof` 成员互转不兼容。
10. **代码生成有版本边界。** `protoc`、运行时库和构建集成要一起固定、一起升级；生成代码是构建产物，不要手改；构建脚本要记录版本。
11. **不要把序列化字节当稳定哈希。** 字段顺序、`map` 顺序、未知字段、packed 表示和运行时版本都可能让同样内容产生不同字节。这是节点 pitfalls。
12. **二进制紧凑跨语言，但人读和排障不如文本。** 这是节点 tradeoffs。用 `protoc --decode`、日志、描述符和模式版本来缓解，不要假装这个代价不存在。
13. **本课实践的主线**：定义 KV 请求响应，生成 C++ 代码，用 v1 写、v2 读，再用 v2 写、v1 读；用测试证明删除字段后编号被保留，用测试证明默认值与存在性不是一回事。
14. **为什么后端要懂：为服务接口建立可演进的数据契约。** 滚动升级、跨语言链路、持久化数据和排障都依赖这份契约。把编号当身份、把存在性写清楚、把删除字段的编号保留下来，契约才能随时间安全演进。

本课的主线可以压缩成一句话：**用模式固定字段的身份，用编号和 wire type 理解线格式，用 presence 表达“有没有值”，用 `reserved` 和版本固定保护未来的兼容性；最后用同一份 KV 模式做一次新旧版本互读，把理解变成可执行的测试。** 下一步可以继续沿着这个方向深入：怎样为大规模模式做兼容性检查和 CI 门禁，怎样用描述符做通用的消息检查工具，以及怎样在持久化场景里管理模式版本与历史数据的长期兼容。
