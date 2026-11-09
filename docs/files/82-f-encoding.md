# 第 82/208 课：序列化成本与缓冲管理

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 协议与框架 |
| 前置 | f-protobuf、b-memory |
| 难度 | 进阶 |
| 领域 | F |
| 标签 | Serialization、F、C++ |
| 子分类 | 协议与框架 |
| 节点 ID | f-encoding |
| 英文名 | Serialization |

> 承接说明：前置节点 f-protobuf（Protobuf 模式与代码生成，第 81 课）讲了模式、字段编号、wire type、presence、`repeated` 与 `oneof`，以及新旧版本如何靠字段编号保持兼容。它回答的是“线格式长什么样、字段身份怎样固定”。前置节点 b-memory（内存管理，第 28 课）讲了 `new` 与 `malloc`、对齐、allocator 与 `pmr` 资源，以及 `monotonic_buffer_resource` 这类批量分配手段。本课把这两条线接起来：同一条消息，从内存对象变成线格式、再从线格式变回内存对象，中间到底花了多少 CPU、做了多少次分配、复制了几遍字节，以及缓冲区和消息上限应该怎样管理。本节点 summary 是：序列化把内存对象转换为线格式，成本包括遍历、编码、分配与复制，不能仅比较消息字节数。

---

## 导读

先看本节点的 summary，后面所有内容都围绕它展开：

> 序列化把内存对象转换为线格式，成本包括遍历、编码、分配与复制，不能仅比较消息字节数。

这句话至少包含三层意思。

第一层是**序列化是一个过程，不是一个字节数**。很多人比较两种协议时，只看“同一条消息谁更小”。字节数当然重要，但它只是结果之一。把一个对象变成线格式，需要遍历对象图、检查字段是否存在、把整数编成 varint、给长度分隔字段写长度、把字符串和字节数组搬进输出缓冲；反方向还要分配对象、分配字符串、维护容器、校验输入。每一件事都要花 CPU，也都可能触发内存分配。只看字节数，等于只看货物重量，不看打包、装卸和运输各花了多少人工。

第二层是**成本要分层归因**。一次 RPC 调用端到端的时间，至少可以拆成“业务处理、序列化、排队、系统调用、内核网络栈、对端处理、反序列化”这些部分。序列化内部还能继续拆成遍历、编码、分配和复制。节点为什么强调“解释 RPC 调用端 CPU 与分配热点”，就是因为线上常见的尾延迟并不总是网络慢，有时是调用端在序列化时做了大量小分配，或者在大 payload 上反复扩容、反复复制。把分配和网络分开统计，才知道该优化哪里。

第三层是**优化要付出代价**。varint 让常用小整数更省字节，但大整数和负数会更长，解码也要逐字节判断；Arena 减少逐对象分配，但对象生命周期被绑到 Arena 上，块内存可能长期保留；复用对象减少分配，但容易保留曾经的大容量，还可能引入共享状态。节点 pitfalls 和 tradeoffs 分别点出了两个最容易误判的地方：所谓零拷贝 API 不保证整条链路没有复制；复用对象减少分配，但易保留大容量并引入共享状态。

本课沿着三个节点要点推进：

- **varint 与长度字段**：线格式里最基础的两类编码，决定了小整数、大整数、字符串和嵌套消息各要花多少字节，也决定了编码器要不要为了写长度而多算一遍。
- **对象构造和 Arena**：反序列化不是“把字节读出来”这么简单，它要构造对象、分配字符串和容器；Arena 是一种把分配批量化、降低单次分配成本的手段，但它不是零复制。
- **复制次数与消息上限**：一条消息从对象到网卡可能经过多次复制；同时，任何来自网络的长度都必须有上限，反序列化必须能拒绝超限和损坏输入。

三个要点最终服务于本节点的 why：**解释 RPC 调用端 CPU 与分配热点。**

---

## 学习目标

### goals

- 能解释 varint 与长度字段。
- 能独立完成：对比不同字段分布的 Protobuf 编码解码，分开统计分配与网络耗时。

### acceptance

- 测量包含真实 payload 分布。
- 反序列化拒绝超限和损坏输入。

### 把验收翻译成可检查产物

“能解释”和“能独立完成”都太容易被“我看懂了”糊弄过去。把两条 acceptance 翻译成看得见、摸得着的东西：

- **“测量包含真实 payload 分布”**：你的实验不能只用一条手工构造的“平均消息”。仓库里要有一份可复现的分布说明，至少记录：字段长度或数值范围的分位数、`repeated` 元素个数的分布、可选字段实际出现的比例、嵌套深度、以及大 payload 在样本中的占比。测量结果要按分布分组，至少给出中位数和高分位数，而不是只给一个总数。真实分布往往有长尾：绝大多数消息很小，少数消息很大；如果只测平均值，长尾的分配和复制成本会被摊薄，优化方向就会错。
- **“反序列化拒绝超限和损坏输入”**：你要有可执行的测试，而不是口头承诺。至少覆盖两类：第一类是长度或总量超过应用上限，例如帧头声明 2 GiB、单个字符串长度超过剩余字节、嵌套层数过深；第二类是字节被截断或破坏，例如 varint 在中间断掉、字段长度前缀大于剩余负载、随机翻转若干位。测试要断言解析返回失败、进程不崩溃、不会因为长度计算溢出而越界读取，并记录失败时目标对象处于什么状态。

### 先修自检

- f-protobuf：能说清 tag 的组成、wire type 的取值、字段编号与线格式的关系；知道 `int32` 和 `sint32` 在线格式上不是同一种表示；知道 `repeated` 数值字段默认可能使用 packed 编码；知道解析器需要模式才能解释字节。
- b-memory：能说清 `new`/`malloc` 的差别、分配器和对齐的基本概念；知道 `std::pmr::monotonic_buffer_resource` 适合“一次性批量分配、统一释放”的场景；能解释为什么大量小分配比一次大分配更贵。
- C++ 工具链：能编译 C++17 程序，知道怎样链接 Protobuf 运行时；会用 `std::chrono::steady_clock` 做计时；知道 `std::string` 可以保存二进制字节，长度要用 `size()`。
- 测量常识：知道基准测试要先预热，要避免把编译优化掉的代码当成“零成本”，要区分单次耗时和吞吐；知道 `perf`、堆分析器或自定义分配计数可以帮助定位热点。

如果前两条里有任何一条说不清，先回去补。本课不会再从零讲 tag 和 wire type，也不会重新讲 `new` 和 `malloc` 的区别；它假设你已经知道“消息里有什么字段”和“内存分配大概怎么发生”，重点是把“从对象到字节、再从字节到对象”的成本拆开。

---

## 为什么后端要懂

本节点 why 的原话是：**解释 RPC 调用端 CPU 与分配热点。** 在真实后端里，这句话对应的不是“序列化库快不快”这种笼统问题，而是几类会直接影响容量规划、尾延迟和排障效率的具体问题。

### 场景一：QPS 很高，但单条消息很小

假设一个鉴权服务每秒处理 50 万次 RPC，每个请求只有几个字段：用户 ID、令牌、时间戳。单条消息可能只有几十字节，网络带宽看起来完全不是瓶颈。但每次调用都要构造请求对象、序列化、反序列化响应，还要在框架里分配缓冲区。如果每条消息有 20 次小分配，50 万 QPS 就是每秒一千万次分配。分配器再快，也会带来缓存不友好、锁竞争和尾延迟。这个时候，优化“消息再小 5 字节”可能毫无意义，减少每条消息的分配次数、复用缓冲区才有意义。

### 场景二：大小消息混合，平均值骗了你

线上流量通常不是单一分布。可能 95% 的请求小于 1 KiB，但有 0.1% 的请求带有几 MiB 的附件或批量数据。平均消息大小也许只有几十 KiB，但 p99 延迟由那 0.1% 决定。大 payload 会触发缓冲区扩容、额外的复制、更多的页分配，甚至逼近消息上限。如果实验只测平均值，你会得出“序列化很快”的结论，却在线上被长尾拖垮。节点 acceptance 要求“测量包含真实 payload 分布”，就是针对这个问题。

### 场景三：调用端 CPU 高，但业务代码看起来很轻

RPC 调用端的 CPU 热点经常不在业务逻辑里，而在“看不见”的地方：对象构造、字符串拷贝、输出缓冲区增长、日志里又序列化了一遍、TLS 或压缩层再复制一次。所谓“零拷贝 API”可能只消除了其中一次复制，应用层到框架、框架到内核的路径仍然有复制。节点 pitfalls 说“所谓零拷贝 API 不保证整条链路没有复制”，就是提醒你不要拿一个 API 的名字当端到端结论。

### 场景四：缓冲区复用带来的内存问题

为了减少分配，很多服务会复用请求/响应对象和缓冲区。短时间的大流量过去后，进程 RSS 却降不下来，因为复用的对象保留了曾经的大容量；如果对象被多个线程共享，还会引入数据竞争和状态污染。节点 tradeoffs 说“复用对象减少分配，但易保留大容量并引入共享状态”，这不是理论风险，而是容量规划和线上事故的常见来源。

---

## 从直觉到原理

### 2.1 先建立成本模型：一条消息到底花在哪里

把一次“对象到字节”的转换拆开，可以用下面这个粗略模型：

```text
总成本 ≈ 遍历对象图
       + 计算编码后长度
       + 写 tag / varint / 长度字段
       + 分配输出缓冲和临时缓冲
       + 把字符串和字节数组复制进输出
       + 框架层缓冲复制
       + 系统调用与内核网络栈
       + 对端反序列化的分配与复制
```

这个模型不是精确公式，但它能帮你避免一个常见错误：把“序列化耗时”当成一个不可分割的黑盒。实际上，同一个库在不同消息形状下，热点可能完全不同：

- 字段很多但值很小：遍历和写 tag 占主导，分配相对少；
- 少量超大 `bytes`：复制和缓冲区扩容占主导；
- 很多短字符串：每个字符串一次分配，分配次数占主导；
- 大量小整数：varint 编码循环占主导，但字节数很省；
- 大量负数：`int32` 被符号扩展成 10 字节 varint，字节数和编码成本都上升；
- 嵌套很深：长度前缀需要自底向上计算，可能多一遍遍历，递归也有栈和校验成本。

所以本课不直接给你一个“Protobuf 每秒多少 MB”的数字，而是让你自己设计实验，把这些项分开测。不同机器、不同版本、不同消息分布下，数字会变；可迁移的是成本模型和测量方法。

### 2.2 varint：每字节 7 位数据，最高位表示继续

varint 是一种可变长无符号整数编码。每个字节的低 7 位放数据，最高位（MSB）是继续位：为 1 表示后面还有字节，为 0 表示这是最后一个字节。编码时从低位到高位，每次取 7 位；解码时反过来，把每段的 7 位放回对应位置。

一个最小的编码器可以写成这样：

```cpp
#include <cstdint>
#include <string>

void append_varint(std::uint64_t v, std::string* out) {
    while (v >= 0x80) {
        out->push_back(static_cast<char>((v & 0x7F) | 0x80));
        v >>= 7;
    }
    out->push_back(static_cast<char>(v));
}
```

对应的解码器必须检查截断和上限：

```cpp
#include <cstddef>
#include <cstdint>
#include <string_view>

bool read_varint(std::string_view in, std::size_t* pos, std::uint64_t* out) {
    std::uint64_t result = 0;
    int shift = 0;
    while (*pos < in.size()) {
        const auto byte = static_cast<std::uint8_t>(in[(*pos)++]);
        if (shift == 63 && (byte & 0x7E) != 0) {
            return false;  // 溢出：64 位放不下
        }
        result |= static_cast<std::uint64_t>(byte & 0x7F) << shift;
        if ((byte & 0x80) == 0) {
            *out = result;
            return true;
        }
        shift += 7;
        if (shift >= 64) {
            return false;  // 超过 10 字节，非法
        }
    }
    return false;  // 输入在 varint 中间结束
}
```

这段代码有两个教学点：

1. **varint 是自描述的**：读到最高位为 0 的字节就结束，不需要外部长度。但它也意味着解码器必须逐字节循环，不能像固定 32 位整数那样一次读 4 字节。
2. **解码必须防溢出和截断**：长度字段、字段编号、整数值都可能由不可信输入提供。上面的检查不是“防御性编程的装饰”，而是避免未定义行为和越界读取的必要条件。

常见值的编码如下：

| 值 | varint 字节（十六进制） | 字节数 |
| --- | --- | --- |
| 0 | `00` | 1 |
| 1 | `01` | 1 |
| 127 | `7F` | 1 |
| 128 | `80 01` | 2 |
| 300 | `AC 02` | 2 |
| 16383 | `FF 7F` | 2 |
| 16384 | `80 80 01` | 3 |
| 4294967295 | `FF FF FF FF 0F` | 5 |

可以看到，小于 128 的非负整数只占 1 字节，这是 varint 在“字段编号小、值也小”的 RPC 消息里非常省空间的原因。但“省空间”不是无条件的：值越大，字节数越多；如果值随机分布在一个很大的范围里，varint 可能接近甚至超过固定宽度编码。

### 2.3 负数、ZigZag 与字段类型选择

varint 本身编码的是无符号整数。Protobuf 的 `int32` 在遇到负数时，会先把值符号扩展成 64 位，再按 varint 编码，所以 `-1` 会变成 10 个字节的 `FF FF FF FF FF FF FF FF FF 01`。如果业务里负数很常见，这会让消息显著变大，编码和解码也更慢。

`sint32`/`sint64` 使用 ZigZag 变换，把有符号整数映射成无符号整数：

```text
0  -> 0
-1 -> 1
1  -> 2
-2 -> 3
2  -> 4
```

编码公式是 `(n << 1) ^ (n >> 31)`（32 位）或对应的 64 位版本。这样绝对值小的负数也能用较少的字节表示。选择字段类型时，不能只看“语义上是不是整数”，还要看实际取值分布：

- 绝大多数是非负小整数：普通 `int32`/`uint32` 都可以，varint 很省；
- 负数常见且绝对值不大：优先考虑 `sint32`/`sint64`；
- 值经常很大、分布均匀：固定宽度或 `fixed32`/`fixed64` 可能更可预测，虽然占的字节数不一定更少；
- 需要精确控制线格式大小时，先用手头版本的编码器实测，不要凭直觉。

这里也体现了本课的核心观点：**字节数只是成本的一部分。** 选择 `sint32` 可能让负数消息更小，但 ZigZag 变换本身也有 CPU 成本；选择 `int32` 可能让负数消息更大，但编码路径更直接。要用真实分布测，而不是只看单个样例。

### 2.4 长度字段：长度分隔字段怎样写，为什么有时要算两遍

Protobuf 的 `string`、`bytes`、嵌入消息和 packed `repeated` 字段使用 wire type 2，也就是“长度分隔”：线上排列是 tag、长度、负载。长度本身用 varint 编码。

例如字段编号 2、类型 `string`、值为 `"hi"`：

```text
tag    = (2 << 3) | 2 = 0x12
length = 2            = 0x02
payload= "hi"         = 0x68 0x69
完整字节 = 12 02 68 69
```

长度字段带来两个直接成本。

第一，**编码器必须知道负载长度**。字符串和 `bytes` 的长度可以直接从对象得到；嵌入消息的长度则要先知道整个子消息序列化后有多长。常见实现会在序列化前计算一次 `ByteSizeLong()`（或使用缓存的 size），再写父消息；如果实现选择“先把子消息写进临时缓冲区，再把长度和内容复制到父缓冲区”，就会多一次分配和一次复制。不同实现、不同版本、不同调用路径可能采用不同策略，这正是 sourceStudy 要核对的边界之一。

第二，**解码器必须校验长度**。长度是 varint，可能是一个恶意的大数；计算 `payload_end = pos + length` 时可能溢出；长度可能超过剩余字节，也可能超过应用允许的单字段上限。安全的解析顺序是：

```text
1. 读 tag，确认字段编号和 wire type；
2. 读长度 varint，检查是否溢出、是否在允许范围内；
3. 检查 length 是否小于等于剩余可读字节数；
4. 再决定是否分配、是否复制、是否跳过。
```

一个常见的错误是“先按长度分配，再检查剩余字节”。如果长度来自不可信输入，这样做会在检查之前就申请巨大的内存，轻则触发 OOM，重则被用来做拒绝服务攻击。正确顺序是先检查上限和剩余长度，再分配。

### 2.5 编码顺序与临时缓冲：长度前缀的连锁反应

假设你要编码一个嵌套消息：

```text
Parent {
  Child child = 1;
}
```

父消息里 `child` 的线格式是 `tag + length + child_payload`。编码器在写 `length` 之前必须知道 `child_payload` 的长度。常见做法有三类：

1. **先算大小，再写内容**：先递归调用 `ByteSizeLong()`，得到长度，分配足够大的输出缓冲，再递归写内容。好处是输出缓冲一次到位，坏处是对象图被遍历两遍（计算大小一遍、写内容一遍）。
2. **先写子消息到临时缓冲，再拼到父缓冲**：子消息序列化到临时 `std::string`，拿到长度后写父消息的 tag 和 length，再把临时缓冲追加进去。好处是逻辑简单，坏处是每个嵌套层级都可能多一次分配和一次复制。
3. **预留占位，再回填长度**：在可随机访问的输出缓冲里先跳过 length 的位置，写完子消息后回填。好处是少一次复制，坏处是输出缓冲必须支持回填，长度 varint 的字节数可能变化，回填时可能要移动数据。

这三种策略没有绝对优劣，取决于消息形状和实现。你要做的是：如果你的嵌套消息很多、层级很深，就用实验比较“计算大小”和“临时缓冲”两种路径的 CPU 与分配差异，而不是假设“库会自动做到最优”。

下面是一个简化的“临时缓冲”写法，用来观察复制次数：

```cpp
std::string encode_child_with_temp(const Child& child) {
    std::string child_wire;
    child_wire.reserve(child.ByteSizeLong());
    child.SerializeToString(&child_wire);

    std::string parent_wire;
    parent_wire.reserve(1 + 10 + child_wire.size());
    parent_wire.push_back(static_cast<char>(0x0A));  // field 1, wire type 2
    append_varint(child_wire.size(), &parent_wire);
    parent_wire.append(child_wire);                  // 这里发生一次复制
    return parent_wire;
}
```

如果改成“先算大小、一次分配、直接写父缓冲”，复制次数可能减少，但会多一次大小遍历。实验时要把这两种成本都记录下来，不能只数“函数调用次数”。

### 2.6 反序列化的对象构造成本

反序列化不是把字节映射成一块连续内存就结束了。对于 C++ 的 Protobuf 消息，解析器通常需要：

- 构造消息对象本身；
- 为每个 `string`/`bytes` 字段准备存储，可能发生一次或多次分配；
- 为 `repeated` 字段增长容器，可能触发扩容和元素复制；
- 为嵌套消息构造子对象；
- 为 `map` 字段插入节点，节点本身也要分配；
- 在解析失败时清理已经构造的部分。

即使消息最终被丢弃，这些构造和分配也已经发生。所以“反序列化耗时”不只是解码 varint 的时间，还包括对象生命周期管理的成本。对于高 QPS、小消息的场景，对象构造和分配往往是比解码本身更大的热点。

Protobuf 的 C++ API 提供了一些减少重复构造的手段：

```cpp
demo::Payload msg;
msg.Clear();  // 复用对象，通常保留内部容量
bool ok = msg.ParseFromArray(data, size);
```

`Clear()` 会把消息恢复到“没有字段被设置”的状态，但通常不会把已经分配的字符串容量、容器容量全部还给分配器。这带来两个后果：

- 好处：下一次解析同样形状的消息时，可以复用已有容量，减少分配；
- 坏处：如果曾经解析过一条很大的消息，对象会保留那块大容量，造成内存占用居高不下。

这正是节点 tradeoffs 的一部分。复用不是免费的，它把“分配成本”换成了“内存保留”和“状态管理成本”。

### 2.7 Arena：把逐对象分配变成批量分配

Arena 的基本思想是：不在每个对象上单独调用 `new`，而是从一块较大的内存区域里按顺序切出空间；对象不需要逐个 `delete`，统一由 Arena 释放或重置。它和 b-memory 里的 `monotonic_buffer_resource` 属于同一类思路：适合生命周期一致、批量创建、批量销毁的对象。

一个典型的 Protobuf Arena 用法如下（具体 API 以手头版本为准）：

```cpp
#include <google/protobuf/arena.h>

google::protobuf::Arena arena;
auto* req = google::protobuf::Arena::CreateMessage<demo::Payload>(&arena);
req->set_small(42);
req->set_text("hello");

// 在 arena 生命周期内，req 及其子对象不需要逐个 delete。
// 可以用下面的接口观察 arena 的用量：
const auto used = arena.SpaceUsed();
const auto allocated = arena.SpaceAllocated();
```

Arena 能减少什么：

- 减少每个消息对象、子对象、部分内部容器的单独分配次数；
- 把分配器的元数据开销和锁竞争摊薄；
- 在批量处理请求时，统一重置 Arena，比逐个销毁对象更简单。

Arena 不能保证什么：

- **不能保证没有复制**。字符串和 `bytes` 的负载仍然要从输入字节复制到对象里；序列化时仍然要复制到输出缓冲。
- **不能保证内存立刻归还**。Arena 通常会保留已经申请的块，`Reset()` 往往只是把游标回到起点；块可能继续被复用，也可能长期占用。
- **不能消除所有分配**。超过 Arena 块大小的大对象、某些容器、某些版本下的内部结构，仍可能走普通堆分配。
- **对象生命周期被绑定**。Arena 销毁时，从它创建的对象一起失效；不能把对象的裸指针带到 Arena 生命周期之外。

所以“用了 Arena”不等于“零分配”，更不等于“零复制”。它只是把分配策略从“逐对象”换成“批量”。测量时要同时记录分配次数、分配字节数、Arena 的 `SpaceAllocated` 和实际存活对象的 `SpaceUsed`，否则你会把“块申请了但没用完”误当成“消息占用”。

### 2.8 复制次数：从对象到网卡到底复制了几遍

一条消息从应用对象到对端，可能经过下面这些复制点：

```text
[对象字段] --编码--> [序列化输出缓冲] --框架--> [发送缓冲]
      --系统调用--> [内核 socket 缓冲] --DMA--> [网卡]
      --网络--> [对端内核缓冲] --read--> [对端应用缓冲]
      --解析--> [对端对象字段]
```

每一段箭头都可能是一次复制，也可能被某些技术消除：

- 编码阶段：对象字段到输出缓冲，通常至少一次复制；字符串和 `bytes` 尤其明显。
- 输出缓冲扩容：`std::string` 或 `std::vector` 增长时，旧内容要搬到新地址，这是一次额外复制；预留容量可以减少但不能消除。
- 框架层：RPC 框架可能把序列化结果再拼进帧头、再复制进发送队列。
- 系统调用：`send`/`write` 通常会把用户态缓冲复制到内核；某些零拷贝发送接口、`sendfile`、`io_uring` 的特定路径可以减少这次复制，但适用条件有限。
- 加密/压缩：TLS 和压缩库通常有自己的输入输出缓冲，每层都可能增加复制。
- 接收侧：内核到用户态一次复制，解析时再从输入缓冲复制到对象字段一次。
- 日志/审计：如果为了记录又序列化了一遍，成本会翻倍。

节点 pitfalls 的原话是：**所谓零拷贝 API 不保证整条链路没有复制。** 这句话的重点不是“零拷贝没用”，而是“要按链路核算”。一个 API 可能消除了“用户态到内核”的一次复制，但编码阶段和框架层仍然在复制；也可能只是把复制推迟到了另一个线程或另一个缓冲区。

测量复制成本时，不要只记录“消息字节数”。你可以维护两个计数器：

```text
wire_bytes        = 最终线上字节数
copied_bytes      = 应用代码显式 append / memcpy / assign 的字节数
```

如果 `copied_bytes` 是 `wire_bytes` 的 3 倍，说明每条消息平均被搬了三遍。这个放大系数往往比消息本身的大小更能解释 CPU 热点。具体怎么统计取决于你的代码和框架，可以从自己的缓冲类开始，把每次写入都记账，再逐步覆盖框架层。

### 2.9 消息上限：为什么“先分配再校验”是危险的

网络输入的长度字段是攻击面。一个 4 字节的帧头可以声明 4 GiB 的负载，一个 varint 长度可以声明一个巨大的字符串。如果解析器先按声明长度分配，再读取实际数据，攻击者只需要发一个很小的包，就能让服务申请大量内存。

消息上限至少要覆盖四个维度：

1. **总消息大小**：单条消息或单帧允许的最大字节数；
2. **单字段长度**：单个 `string`/`bytes` 允许的最大长度；
3. **容器元素个数**：`repeated` 或 `map` 允许的最大元素数；
4. **嵌套深度**：递归消息允许的最大层数，防止栈溢出和递归解析耗尽 CPU。

应用层通常还要有帧长度上限。一个安全的帧读取顺序是：

```text
1. 读固定长度的帧头；
2. 解析声明的长度；
3. 如果 length > kMaxFrame，直接拒绝并关闭连接；
4. 如果 length > 剩余可读字节，继续读或拒绝（取决于协议是否允许半包）；
5. 分配缓冲区时再次检查上限，避免整数溢出；
6. 解析消息时设置总字节上限和递归深度上限。
```

注意第 3 步和第 5 步不是重复。第 3 步是在读负载之前拒绝明显的超限帧；第 5 步是在真正分配时防止“length 计算溢出”或“检查之后长度又被改变”的问题。对于不可信输入，所有长度都要当作“声明”，不是事实。

Protobuf 运行时本身通常有解析上限和递归深度限制，但具体 API、默认值和版本行为会变化。不要把“库有默认限制”当成唯一防线；应用层应该根据自己的业务设置更小的上限，并在 sourceStudy 中记录手头版本的实际行为。

### 2.10 缓冲区管理：复用、扩容与所有权

缓冲区管理的目标不是“永远不分配”，而是“让分配次数、分配大小和生命周期都可预测”。常见策略有：

- **预留容量**：编码前用 `ByteSizeLong()` 或估算值 `reserve`，减少 `std::string` 扩容；但预留过大会浪费内存，估算过小仍会扩容。
- **复用缓冲**：每个线程或每个连接维护一个输出缓冲，处理完消息后 `clear()` 保留容量；要防止大消息把容量永久抬高。
- **分块/池化**：用固定大小的块或对象池管理高频小缓冲，降低分配器压力；要注意对齐、碎片和线程安全。
- **Arena/单调资源**：适合一次请求内批量创建、请求结束统一释放的对象图；不适合生命周期差异很大的对象。
- **读写分离**：接收缓冲和发送缓冲分开，避免一边读一边写导致扩容和数据移动；但会增加内存占用。
- **上限约束**：所有缓冲的增长都要有上限，超过上限时拒绝或降级，而不是无限扩容。

缓冲区复用和对象复用一样，有一个核心权衡：**减少分配，但保留容量并引入共享状态。** 如果缓冲区是线程共享的，还要加锁或做线程本地存储；如果复用的是消息对象，还要确保 `Clear()` 清掉了所有字段、没有残留的 `unknown fields`、没有指向旧缓冲的视图。

### 2.11 把成本拆开：实验应该记录哪些量

一个能解释 RPC 调用端热点的实验，至少应该记录下面这些量，并按消息分布分组：

| 指标 | 含义 | 为什么重要 |
| --- | --- | --- |
| `wire_bytes` | 编码后的字节数 | 影响网络和存储，但不是全部成本 |
| `encode_ns` | 单次编码耗时 | 反映遍历、varint、长度计算和复制 |
| `decode_ns` | 单次解码耗时 | 反映解析、对象构造和分配 |
| `alloc_count` | 每条消息的分配次数 | 高频小消息的主要热点之一 |
| `alloc_bytes` | 每条消息申请的字节数 | 反映内存压力和缓存占用 |
| `copied_bytes` | 显式复制的字节数 | 揭示复制放大系数 |
| `frame_rtt_ns` | 发送并接收一帧的往返时间 | 与序列化分开统计网络成本 |
| `p50/p95/p99` | 分位数 | 长尾比平均值更能反映线上体验 |

测量时要注意：

- **预热**：第一次调用通常包含页错误、分配器冷启动、分支预测未稳定，不能直接计入。
- **隔离**：编码、解码、网络分别计时；网络测试用预先编码好的字节，避免把编码时间算进网络。
- **防止优化**：把结果写入可观察的变量或校验和，避免编译器把整个计算删掉。
- **固定环境**：记录 CPU 型号、编译选项、Protobuf 版本、消息分布和测试日期；不同环境下的绝对数字不可直接比较。
- **多次重复**：至少多轮取中位数，同时保留分位数；不要只跑一次就下结论。

这套方法比记住某个库的 benchmark 数字更有用，因为它能迁移到你的业务消息和你的机器上。

---

## 常见误区与设计权衡

### 误区一：消息越小，序列化一定越快

消息大小和 CPU 成本相关，但绝不是单调关系。反例很多：

- 一串随机大整数，用 varint 编码可能接近 5 字节，解码还要逐字节循环；固定宽度编码虽然字节数固定，但可能更容易用向量化或批量读取。
- 很多极小的消息，单条字节数很少，但每条都要构造对象、分配缓冲、走一次 RPC 框架；固定开销远大于负载本身。
- 大量短字符串会带来很多次小分配，字节数不大，分配次数却很高。
- 深层嵌套消息的长度前缀需要额外计算，消息可能不大，遍历和长度计算却不便宜。

所以比较协议或实现时，至少要同时看“字节数、编码时间、解码时间、分配次数、复制字节数”。只看其中一个，容易得出错误结论。

### 误区二：用了零拷贝 API，整条链路就没有复制

这是本节点 pitfalls 的正面展开：**所谓零拷贝 API 不保证整条链路没有复制。**

“零拷贝”通常指某一个特定环节消除了复制，例如发送时避免用户态到内核态的一次拷贝，或者解析时用视图引用输入缓冲而不是复制字符串。它不保证：

- 编码阶段没有复制；
- 框架层没有把消息拼进另一个缓冲；
- 压缩或加密层没有复制；
- 接收端解析时没有把字节复制进对象；
- 日志、审计、监控没有再次序列化。

判断是否真的减少了复制，要看数据从生产到消费的完整路径，并用 `copied_bytes` 之类的计数器验证。不要因为函数名里有 `zero_copy` 就认为端到端零复制。

### 误区三：Arena 等于零分配、零复制

Arena 减少的是“逐对象分配”的次数，不是所有分配，更不是复制。Arena 自己也要申请大块内存；字符串和 `bytes` 的负载仍然要复制；某些大对象可能绕过 Arena；不同版本的内部结构也可能有差异。用 Arena 时要测量：

- `SpaceUsed()`：实际被对象占用的字节；
- `SpaceAllocated()`：Arena 向系统申请的块大小；
- 分配次数：是否真的下降；
- 复制字节数：是否真的下降（通常不会因为 Arena 而下降）。

如果只看到“分配次数下降”，就宣称“序列化成本降低”，可能忽略了复制和内存保留。

### 误区四：复用对象一定更快

复用对象可以减少分配，但节点 tradeoffs 提醒：**复用对象减少分配，但易保留大容量并引入共享状态。**

具体风险包括：

- **容量保留**：一条大消息解析后，`std::string` 和容器保留大容量；之后即使只处理小消息，内存也不释放。
- **状态残留**：`Clear()` 是否清掉了所有字段、未知字段、缓存的大小、视图引用？如果清不干净，下一条消息可能读到上一条的残留。
- **共享状态**：对象被多个线程或多次异步回调共享时，需要同步；否则会出现数据竞争或“这条请求读到了那条请求的数据”。
- **生命周期**：如果对象里保存了指向输入缓冲的 `string_view` 或指针，复用缓冲后视图会悬空。

复用不是“把对象放进池子里”就结束了，它要求明确的所有权、清理协议和容量上限。对于容量可能突然变大的字段，可以在处理完后 `shrink_to_fit()` 或与空对象交换，把大容量还回去；也可以设置“超过阈值就不回收进池”的策略。

### 误区五：只测平均值，不看分布和尾延迟

平均值会把长尾摊平。一个服务 99% 的请求编码耗时 1 微秒，1% 的请求因为大 payload 耗时 10 毫秒，平均值可能仍然“好看”，但用户感受到的是那 1%。更糟的是，大 payload 可能触发分配器向操作系统申请新页，导致后续请求也变慢。

测量要按真实分布分组，并保留 p95、p99、p999。节点 acceptance 要求“测量包含真实 payload 分布”，不是让你构造一个漂亮的小消息来证明结论。

### 误区六：反序列化可以信任输入长度

不能。长度字段、元素个数、嵌套深度都来自网络，必须校验。常见的错误包括：

- 先按长度分配，再检查剩余字节；
- 用有符号整数接收无符号长度，导致负数或溢出；
- 只检查总长度，不检查单字段长度；
- 只检查一层，不限制递归深度；
- 解析失败后继续使用半成品对象。

这些错误可能导致 OOM、越界读、栈溢出或逻辑错误。安全做法是：所有长度先当“声明值”，检查上限和剩余字节后再使用；解析失败时丢弃对象或恢复到明确状态。

### 误区七：把序列化耗时和网络耗时混在一起

“RPC 慢了”不等于“序列化慢了”。如果只测端到端 RTT，你无法区分是编码、排队、系统调用、网络还是对端处理慢。正确的做法是分层计时：

- 在调用 `SerializeToString` 前后计时，得到编码时间；
- 在 `send`/`recv` 前后计时，得到传输时间；
- 在对端解析前后计时，得到解码时间；
- 如果框架支持，再记录排队和线程调度时间。

节点 practice 明确要求“分开统计分配与网络耗时”，就是为了避免把所有成本都归到“网络”或“序列化”一个篮子里。

### 误区八：缓存序列化结果没有代价

把对象序列化一次后缓存字节，确实可以避免重复编码，但会带来新的问题：对象修改后缓存何时失效？缓存键怎么定义？缓存本身占多少内存？并发读写怎么同步？如果缓存的是大 payload，可能比对象本身更占内存。缓存是优化手段，不是免费的午餐；只有在“同一对象被多次发送且修改不频繁”的场景才值得考虑。

### 权衡一：varint 的紧凑性与可预测性

varint 对小整数非常友好，但长度可变带来两个代价：

- **字节数不可预测**：同样类型的字段，不同取值可能占 1 到 10 字节；容量规划和缓冲预留更难。
- **解码需要循环**：逐字节判断继续位，分支多，难以像固定宽度那样批量处理。

如果消息里整数普遍很小，varint 的收益很大；如果整数普遍很大或负数很多，varint 可能既费字节又费 CPU。选择字段类型时要用真实分布测，不要只看协议默认值。

### 权衡二：长度前缀的便利性与额外计算

长度分隔字段让解析器可以先知道负载边界，便于跳过未知字段和并行处理；但编码器必须先得到长度，可能多一次遍历或一次临时缓冲。对于嵌套很深的文档，这个成本会累积。缓解手段包括缓存大小、一次分配、回填长度、减少不必要的嵌套；但每种手段都有自己的复杂度和边界。

### 权衡三：Arena 的分配效率与生命周期耦合

Arena 把分配批量化，适合“一个请求创建一批对象、请求结束统一释放”的模式。代价是：

- 对象不能单独释放，生命周期由 Arena 决定；
- 大块内存可能长期保留；
- 跨请求共享对象时要特别小心；
- 某些第三方库或容器可能不兼容 Arena 分配器。

如果对象生命周期差异很大，或者需要频繁单独释放，Arena 可能不是最佳选择。b-memory 里的 `monotonic_buffer_resource` 也有同样的取舍。

### 权衡四：复用对象的分配节省与内存保留

复用对象和缓冲可以显著减少分配次数，但会保留容量、增加状态管理复杂度，并可能引入共享状态。一个实用的折中是：

- 只复用“小的、常见的”对象或缓冲；
- 设置容量上限，超过上限的对象不进入池；
- 复用前执行明确的 `Clear()` 或重置协议；
- 线程本地复用优先于全局共享；
- 定期统计池的命中率、保留字节数和最大容量，避免池变成内存泄漏的遮羞布。

### 权衡五：消息上限的安全性与业务需求

上限设得太小，合法的大 payload 会被拒绝；设得太大，攻击面变成 OOM 和长耗时解析。合理做法是基于真实分布的 p99.9 或 p999，再留出业务增长空间；对不同接口设置不同上限；超限时返回明确错误，而不是静默截断。上限不是越宽松越“兼容”，而是要在安全和可用性之间做显式选择。

### 权衡六：缓冲复用的吞吐与内存占用

复用缓冲能提高吞吐，但每个连接、每个线程都保留一份缓冲会增加常驻内存。连接数很多时，固定缓冲的总量可能非常可观。可以按连接活跃度动态分配、设置空闲回收、使用共享池，或者在高并发下接受更多分配换取更低常驻内存。没有一种策略适合所有服务，关键是把“分配次数、常驻内存、尾延迟”三个指标一起看。

---

## 动手实践：对比不同字段分布的 Protobuf 编码解码，分开统计分配与网络耗时

本节点的 practice 是：**对比不同字段分布的 Protobuf 编码解码，分开统计分配与网络耗时。** 下面给出一个可复现的最小工程。它不追求做成通用 benchmark 框架，而是让你亲手把“消息形状、字节数、编码时间、解码时间、分配次数、复制字节数、传输时间”分开观察。

实践要回答四个问题：

1. 在小整数、大整数、负数、短字符串、大 `bytes`、深层嵌套这几种分布下，编码后的字节数差多少？
2. 编码和解码的 CPU 时间差多少？热点是 varint 循环、长度计算、对象构造还是复制？
3. 每条消息分配了多少次、多少字节？Arena 或对象复用能把分配降到什么程度，又保留了哪些容量？
4. 把编码后的字节通过本机 socket 发出去再收回来，传输时间与序列化时间各占多少？

### 第一步：建立目录

```text
enc_bench/
├── CMakeLists.txt
├── proto/
│   └── enc.proto
├── bench/
│   ├── alloc_counter.h
│   ├── bench_main.cc
│   ├── net_bench.cc
│   └── reject_test.cc
└── build/
```

`alloc_counter.h` 放分配计数和计时工具；`bench_main.cc` 做编码/解码对比；`net_bench.cc` 单独测传输；`reject_test.cc` 放超限和损坏输入测试。把网络测试和序列化测试拆开，是为了避免计时互相污染。

### 第二步：定义覆盖不同分布的消息

`proto/enc.proto`：

```proto
syntax = "proto3";

package demo;

message Item {
  int32 id = 1;
  string name = 2;
  bytes blob = 3;
}

message Payload {
  int32 small = 1;
  int32 large = 2;
  int32 negative = 3;
  sint32 zigzag = 4;
  string text = 5;
  bytes blob = 6;
  repeated int32 nums = 7;
  repeated Item items = 8;
  Payload child = 9;
  repeated sint32 zznums = 10;
}
```

同一个 `Payload` 可以通过不同填法模拟不同分布。这样对比的是“字段分布”而不是“不同模式”，更接近同一个接口在真实流量里的变化。注意 `int32` 的负数字段和 `sint32` 的 ZigZag 字段故意放在一起，方便你观察同一批负数在两种类型下的字节数差异。

### 第三步：生成 C++ 代码并配置构建

```bash
protoc --version
protoc --proto_path=proto --cpp_out=build proto/enc.proto
```

`CMakeLists.txt` 的一种写法：

```cmake
cmake_minimum_required(VERSION 3.16)
project(enc_bench CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Protobuf REQUIRED)
protobuf_generate_cpp(PROTO_SRCS PROTO_HDRS proto/enc.proto)

add_executable(bench_main bench/bench_main.cc ${PROTO_SRCS} ${PROTO_HDRS})
target_include_directories(bench_main PRIVATE ${CMAKE_CURRENT_BINARY_DIR} ${Protobuf_INCLUDE_DIRS})
target_link_libraries(bench_main PRIVATE ${Protobuf_LIBRARIES})

add_executable(net_bench bench/net_bench.cc ${PROTO_SRCS} ${PROTO_HDRS})
target_include_directories(net_bench PRIVATE ${CMAKE_CURRENT_BINARY_DIR} ${Protobuf_INCLUDE_DIRS})
target_link_libraries(net_bench PRIVATE ${Protobuf_LIBRARIES})

add_executable(reject_test bench/reject_test.cc ${PROTO_SRCS} ${PROTO_HDRS})
target_include_directories(reject_test PRIVATE ${CMAKE_CURRENT_BINARY_DIR} ${Protobuf_INCLUDE_DIRS})
target_link_libraries(reject_test PRIVATE ${Protobuf_LIBRARIES})
```

如果本机使用包管理器安装的 Protobuf，`find_package(Protobuf)` 一般能工作；如果你的环境只提供 CMake config 模式，把 `find_package` 换成对应的 config 包即可。构建系统、`protoc` 和运行时库的版本要记录在实验报告里。

### 第四步：准备分配计数与计时工具

`bench/alloc_counter.h`：

```cpp
#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <new>

struct AllocStats {
    std::uint64_t count = 0;
    std::uint64_t bytes = 0;
};

inline thread_local AllocStats t_alloc;

inline void* counted_alloc(std::size_t n) {
    void* p = std::malloc(n == 0 ? 1 : n);
    if (p == nullptr) {
        throw std::bad_alloc();
    }
    t_alloc.count += 1;
    t_alloc.bytes += n;
    return p;
}

inline void counted_free(void* p) {
    std::free(p);
}

void* operator new(std::size_t n) { return counted_alloc(n); }
void* operator new[](std::size_t n) { return counted_alloc(n); }
void operator delete(void* p) noexcept { counted_free(p); }
void operator delete[](void* p) noexcept { counted_free(p); }
void operator delete(void* p, std::size_t) noexcept { counted_free(p); }
void operator delete[](void* p, std::size_t) noexcept { counted_free(p); }

struct AllocScope {
    AllocStats before = t_alloc;
    AllocStats delta() const {
        return AllocStats{t_alloc.count - before.count,
                          t_alloc.bytes - before.bytes};
    }
};

using Clock = std::chrono::steady_clock;

template <class F>
double measure_ns(F&& fn, int iters) {
    const auto begin = Clock::now();
    for (int i = 0; i < iters; ++i) {
        fn();
    }
    const auto end = Clock::now();
    return std::chrono::duration<double, std::nano>(end - begin).count() /
           static_cast<double>(iters);
}
```

这段代码有几个必须说明的限制：

- 全局替换 `operator new` 会影响整个进程，包括标准库和 Protobuf 内部；它适合做实验，不适合直接搬到生产。
- `thread_local` 计数只统计当前线程，所以网络测试里的服务线程不会把它的分配混进主线程。
- 计数只反映“走到这个分配器的内存”，如果某个库使用自己的池或 `mmap`，不会被统计到。因此要把“分配次数”与 Arena 的 `SpaceUsed`/`SpaceAllocated`、以及平台堆分析工具互相印证。
- 用 `std::malloc` 实现 `operator new` 时没有处理过对齐分配；实验里可以接受，生产代码要用 `aligned_alloc` 或分配器接口正确处理对齐。
- 不要在计时循环里做字符串格式化、日志输出或 `std::cout`，否则测到的是 I/O 不是序列化。
- 如果同一个可执行文件有多个翻译单元包含这个头文件，要把全局 `operator new` 的定义放到单独的 `.cc` 中，头文件里只保留工具函数，避免重复定义。

### 第五步：生成不同字段分布

`bench/bench_main.cc` 的核心生成函数：

```cpp
#include "enc.pb.h"

#include <cstdint>
#include <random>
#include <string>

demo::Payload make_payload(const std::string& dist, std::uint32_t seed) {
    std::mt19937 rng(seed);
    demo::Payload p;

    if (dist == "tiny") {
        p.set_small(7 + static_cast<int>(rng() % 100));
        p.set_text("abcdefghijklmnop");
        p.add_nums(1);
        p.add_nums(2);
    } else if (dist == "many_small_ints") {
        for (int i = 0; i < 1000; ++i) {
            p.add_nums(1 + static_cast<int>(rng() % 127));
        }
    } else if (dist == "random_large_ints") {
        for (int i = 0; i < 1000; ++i) {
            p.add_nums(static_cast<int>(rng() & 0x7FFFFFFFu));
        }
    } else if (dist == "negative_int32") {
        for (int i = 0; i < 1000; ++i) {
            p.add_nums(-1 - static_cast<int>(rng() % 1000));
        }
    } else if (dist == "negative_sint32") {
        for (int i = 0; i < 1000; ++i) {
            p.add_zznums(-1 - static_cast<int>(rng() % 1000));
        }
    } else if (dist == "short_strings") {
        for (int i = 0; i < 200; ++i) {
            auto* item = p.add_items();
            item->set_id(i);
            item->set_name(std::string(8 + rng() % 24, 'x'));
        }
    } else if (dist == "large_blob") {
        p.set_blob(std::string(1024 * 1024, 'b'));
    } else if (dist == "deep_nesting") {
        demo::Payload* cur = &p;
        for (int depth = 0; depth < 16; ++depth) {
            cur->set_small(depth);
            cur = cur->mutable_child();
        }
    } else if (dist == "mixed") {
        p.set_small(42);
        p.set_large(123456789);
        p.set_negative(-7);
        p.set_zigzag(-7);
        p.set_text(std::string(64, 't'));
        p.set_blob(std::string(4096, 'b'));
        for (int i = 0; i < 32; ++i) {
            p.add_nums(i * 3);
        }
        for (int i = 0; i < 8; ++i) {
            auto* item = p.add_items();
            item->set_id(i);
            item->set_name(std::string(16, 'n'));
        }
    }
    return p;
}
```

这些分布不是“标准答案”，而是一组有代表性的形状。真实实验应该把 `make_payload` 换成从生产样本或脱敏抓包中恢复的分布；如果做不到，至少要让字段长度、数值范围、`repeated` 个数和嵌套深度的分位数与线上一致。节点 acceptance 要求“测量包含真实 payload 分布”，这一点不能靠这几个写死的例子替代。

### 第六步：分开测编码、解码、分配和复用

在同一个 `bench_main.cc` 里加入下面的测量逻辑：

```cpp
#include "alloc_counter.h"

#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

static volatile std::uint64_t g_sink = 0;

std::string encode(const demo::Payload& p) {
    std::string out;
    out.reserve(static_cast<std::size_t>(p.ByteSizeLong()));
    if (!p.SerializeToString(&out)) {
        throw std::runtime_error("serialize failed");
    }
    return out;
}

void run_one(const std::string& dist) {
    constexpr int kIters = 1000;
    const auto payload = make_payload(dist, 12345);
    const auto wire = encode(payload);

    // 预热：让页、分配器和分支预测进入稳定状态。
    for (int i = 0; i < 10; ++i) {
        g_sink += encode(payload).size();
        demo::Payload tmp;
        g_sink += tmp.ParseFromArray(wire.data(), static_cast<int>(wire.size()));
    }

    AllocScope encode_alloc;
    const double encode_ns = measure_ns([&] {
        g_sink += encode(payload).size();
    }, kIters);
    const AllocStats enc_stats = encode_alloc.delta();

    AllocScope decode_alloc;
    const double decode_fresh_ns = measure_ns([&] {
        demo::Payload tmp;
        g_sink += tmp.ParseFromArray(wire.data(), static_cast<int>(wire.size()));
    }, kIters);
    const AllocStats dec_stats = decode_alloc.delta();

    demo::Payload reused;
    const double decode_reuse_ns = measure_ns([&] {
        reused.Clear();
        g_sink += reused.ParseFromArray(wire.data(), static_cast<int>(wire.size()));
    }, kIters);

    std::printf(
        "%-18s wire=%-9zu enc_ns=%-10.1f dec_new_ns=%-10.1f "
        "dec_reuse_ns=%-10.1f enc_alloc=(%llu,%llu) dec_alloc=(%llu,%llu)\n",
        dist.c_str(), wire.size(), encode_ns, decode_fresh_ns, decode_reuse_ns,
        static_cast<unsigned long long>(enc_stats.count),
        static_cast<unsigned long long>(enc_stats.bytes),
        static_cast<unsigned long long>(dec_stats.count),
        static_cast<unsigned long long>(dec_stats.bytes));
}
```

运行后你会得到类似下面这张表（表头是固定的，数字要由你的机器和分布填出来）：

| 分布 | wire 字节 | 编码 ns | 新对象解码 ns | 复用对象解码 ns | 编码分配次数/字节 | 解码分配次数/字节 |
| --- | --- | --- | --- | --- | --- | --- |
| tiny | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| many_small_ints | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| random_large_ints | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| negative_int32 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| negative_sint32 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| short_strings | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| large_blob | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| deep_nesting | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| mixed | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |

不要只比较 `wire` 列。重点观察：

- `negative_int32` 与 `negative_sint32` 的字节数差异，是否和 varint 符号扩展的预期一致？
- `many_small_ints` 的字节数很小，但解码时间是否被大量 varint 循环拉高？
- `short_strings` 的分配次数是否远高于字节数相近的其他分布？
- `large_blob` 的编码时间是否主要由复制和缓冲增长主导？
- `deep_nesting` 的长度计算和递归开销是否明显？
- `dec_reuse_ns` 是否比 `dec_new_ns` 低？低多少？复用对象的容量保留是否会让内存上涨？

### 第七步：加入 Arena 对比

在支持 Arena 的版本上，可以再加一个解码变体：

```cpp
#include <google/protobuf/arena.h>

void run_arena_one(const std::string& dist) {
    constexpr int kIters = 1000;
    const auto payload = make_payload(dist, 12345);
    const auto wire = encode(payload);

    google::protobuf::Arena arena;
    auto* msg = google::protobuf::Arena::CreateMessage<demo::Payload>(&arena);

    const double arena_decode_ns = measure_ns([&] {
        arena.Reset();
        msg = google::protobuf::Arena::CreateMessage<demo::Payload>(&arena);
        g_sink += msg->ParseFromArray(wire.data(), static_cast<int>(wire.size()));
    }, kIters);

    std::printf("%-18s arena_decode_ns=%.1f used=%llu allocated=%llu\n",
                dist.c_str(), arena_decode_ns,
                static_cast<unsigned long long>(arena.SpaceUsed()),
                static_cast<unsigned long long>(arena.SpaceAllocated()));
}
```

这里特意在每轮 `Reset()` 后重新创建消息，是因为 Arena 重置后旧对象失效，不能继续使用。真实代码通常按请求或按批次管理 Arena，而不是每轮都创建和重置。你需要观察：

- Arena 解码是否减少了分配次数？
- `SpaceAllocated` 是否明显大于 `SpaceUsed`？
- 大 `blob` 分布下，Arena 是否真的避免了所有分配？
- 如果重复解析不同大小的消息，Arena 的块是否被复用，常驻内存是否上升？

如果手头的 Protobuf 版本 API 与上面不同，不要硬套；以 `sourceStudy` 的方式记录版本和实际接口，并保证实验语义一致。

### 第八步：把传输时间单独测出来

`bench/net_bench.cc` 可以先使用本机 socket 对作为可重复的传输基线。它测的是“系统调用 + 本机内核转发”，不是真实网络 RTT，但足以把传输时间与编码时间分开：

```cpp
#include "enc.pb.h"
#include "alloc_counter.h"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <string>
#include <thread>

bool write_all(int fd, const void* data, std::size_t n) {
    const char* p = static_cast<const char*>(data);
    while (n > 0) {
        const ssize_t k = ::send(fd, p, n, 0);
        if (k <= 0) {
            return false;
        }
        p += k;
        n -= static_cast<std::size_t>(k);
    }
    return true;
}

bool read_all(int fd, void* data, std::size_t n) {
    char* p = static_cast<char*>(data);
    while (n > 0) {
        const ssize_t k = ::recv(fd, p, n, 0);
        if (k <= 0) {
            return false;
        }
        p += k;
        n -= static_cast<std::size_t>(k);
    }
    return true;
}

void echo_loop(int fd, int rounds) {
    constexpr std::uint32_t kMaxEchoFrame = 8 * 1024 * 1024;
    for (int i = 0; i < rounds; ++i) {
        std::uint32_t be = 0;
        if (!read_all(fd, &be, sizeof(be))) {
            return;
        }
        const std::uint32_t len = ntohl(be);
        if (len > kMaxEchoFrame) {
            return;  // 实验接收端也要有上限，不能按对端声明无限分配
        }
        std::string body(len, '\0');
        if (!read_all(fd, body.data(), body.size())) {
            return;
        }
        const std::uint32_t out_len = htonl(len);
        if (!write_all(fd, &out_len, sizeof(out_len))) {
            return;
        }
        if (!write_all(fd, body.data(), body.size())) {
            return;
        }
    }
}

double measure_net_rtt(int fd, const std::string& wire, int iters) {
    const std::uint32_t be = htonl(static_cast<std::uint32_t>(wire.size()));
    const auto begin = Clock::now();
    for (int i = 0; i < iters; ++i) {
        if (!write_all(fd, &be, sizeof(be)) ||
            !write_all(fd, wire.data(), wire.size())) {
            return -1.0;
        }
        std::uint32_t reply_len = 0;
        if (!read_all(fd, &reply_len, sizeof(reply_len))) {
            return -1.0;
        }
        const std::uint32_t n = ntohl(reply_len);
        std::string reply(n, '\0');
        if (!read_all(fd, reply.data(), reply.size())) {
            return -1.0;
        }
    }
    const auto end = Clock::now();
    return std::chrono::duration<double, std::nano>(end - begin).count() /
           static_cast<double>(iters);
}
```

上面的 `make_payload` 和 `encode` 与前面的实验共用；实际工程应把它们抽到共享头文件中，避免两个可执行文件重复代码。这里保留在同一段说明里，是为了让传输计时和编码计时的边界看得更清楚。

连接建立和线程启动的代码可以写成：

```cpp
int sv[2];
if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) {
    return 1;
}

const auto wire = encode(make_payload("mixed", 7));
constexpr int kIters = 2000;

std::thread server(echo_loop, sv[1], kIters);
const double rtt_ns = measure_net_rtt(sv[0], wire, kIters);
server.join();
::close(sv[0]);
::close(sv[1]);

std::printf("wire=%zu net_rtt_ns=%.1f\n", wire.size(), rtt_ns);
```

这里要特别强调：`net_rtt_ns` 不包含编码时间，因为 `wire` 是提前编码好的；它包含系统调用、本机内核转发、线程调度和内存拷贝。把同一段代码改成 TCP loopback 后，你会看到另一组数字；再换成真实网络，才包含链路 RTT。不要把本机 socket 的数字当成生产网络延迟。

### 第九步：反序列化拒绝超限和损坏输入

`bench/reject_test.cc` 里先定义应用上限和安全的解析入口：

```cpp
#include "enc.pb.h"

#include <arpa/inet.h>
#include <climits>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>

constexpr std::size_t kMaxMessage = 4 * 1024 * 1024;   // 4 MiB
constexpr std::uint32_t kMaxFrame = 8 * 1024 * 1024;   // 8 MiB

bool safe_parse(const std::string& wire, demo::Payload* out) {
    if (wire.size() > kMaxMessage) {
        return false;
    }
    if (wire.size() > static_cast<std::size_t>(INT_MAX)) {
        return false;
    }

    demo::Payload tmp;
    if (!tmp.ParseFromArray(wire.data(), static_cast<int>(wire.size()))) {
        return false;
    }
    out->Swap(&tmp);  // 只有完整解析成功才替换目标对象
    return true;
}
```

注意 `safe_parse` 先检查大小，再解析到临时对象，最后才 `Swap`。这样做避免“解析失败但目标对象已经被改了一半”的问题。不同版本的 `ParseFromArray` 对失败状态的处理可能不同，但应用层至少可以保证自己的入口不暴露半成品。

下面是几组必须有的测试用例：

```cpp
bool must_reject(const std::string& wire) {
    demo::Payload p;
    return !safe_parse(wire, &p);
}

int run_reject_tests() {
    const auto good = [] {
        demo::Payload p;
        p.set_small(1);
        p.set_text("hello");
        p.add_nums(7);
        std::string out;
        p.SerializeToString(&out);
        return out;
    }();

    int failures = 0;

    // 1) 正常输入必须能解析，作为对照。
    {
        demo::Payload p;
        if (!safe_parse(good, &p)) {
            ++failures;
        }
    }

    // 2) 截断：去掉最后一个字节，varint 或长度字段可能不完整。
    if (!must_reject(good.substr(0, good.size() - 1))) {
        ++failures;
    }

    // 3) 声明的长度远大于剩余字节：在负载前追加一个超大长度。
    {
        std::string bad = good;
        bad.push_back(static_cast<char>(0x12));  // field 2, wire type 2
        std::uint64_t huge = 0x7FFFFFFF;
        while (huge >= 0x80) {
            bad.push_back(static_cast<char>((huge & 0x7F) | 0x80));
            huge >>= 7;
        }
        bad.push_back(static_cast<char>(huge));
        if (!must_reject(bad)) {
            ++failures;
        }
    }

    // 4) 总长度超限：不进入解析器，直接在应用层拒绝。
    {
        std::string oversized(kMaxMessage + 1, 'x');
        if (!must_reject(oversized)) {
            ++failures;
        }
    }

    // 5) 帧头超限：读负载之前就拒绝。
    {
        std::uint32_t be = htonl(kMaxFrame + 1);
        std::string header(reinterpret_cast<const char*>(&be), sizeof(be));
        std::uint32_t declared = 0;
        std::memcpy(&declared, header.data(), sizeof(declared));
        declared = ntohl(declared);
        if (declared <= kMaxFrame) {
            ++failures;
        }
    }

    return failures;
}
```

这些测试覆盖了 acceptance 的两条要求：总量超限、单字段长度超限、帧长度超限，以及截断和损坏输入。真实项目里还应该加入：随机位翻转、嵌套深度超限、`repeated` 元素个数超限、空输入、只有 tag 没有值、非法 wire type、以及解析失败后目标对象是否保持可用。测试的目标不是“猜中某个库的错误码”，而是“应用层始终拒绝不安全输入，并且不崩溃、不越界、不泄漏”。

### 第十步：观察结果并写成结论

把 `bench_main`、`net_bench`、`reject_test` 的结果整理成一张实验记录。建议至少写清楚：

1. 消息分布：每种分布的字段长度、数值范围、`repeated` 个数、嵌套深度和样本比例；
2. 字节数：`wire` 大小，以及 `negative_int32` 与 `negative_sint32` 的对比；
3. CPU：编码、新对象解码、复用对象解码、Arena 解码的 p50/p95/p99；
4. 分配：每条消息的分配次数和分配字节数；
5. 复制：如果有自己的缓冲类，记录 `copied_bytes` 与 `wire_bytes` 的比值；
6. 传输：本机 socket RTT、TCP loopback RTT（如果做了），以及它们与编码/解码时间的比例；
7. 安全：超限、截断、损坏输入全部被拒绝的测试结果；
8. 版本：编译器、编译选项、Protobuf 版本、操作系统、CPU 型号和测试日期。

最后回答本节点的核心问题：**在这个服务的真实分布下，调用端 CPU 主要花在遍历、编码、分配还是复制？网络时间占多少？** 如果答案是“分配次数最多”，就优先考虑 Arena、对象复用和缓冲池；如果答案是“复制字节数被放大”，就检查框架层、日志层和压缩/加密层；如果答案是“负数字段字节数过大”，就检查 `int32`/`sint32` 的选择；如果答案是“大 payload 的 p99 很差”，就检查缓冲扩容和消息上限。

### 验收对照表

| 验收项 | 你的仓库里应该有什么 |
| --- | --- |
| 测量包含真实 payload 分布 | 一份分布说明和对应样本生成逻辑；按分布分组的结果；p50/p95/p99；不是只测一条“平均消息” |
| 反序列化拒绝超限 | 总量、单字段、帧长度、嵌套深度的上限检查；超限时返回失败，且在分配前拒绝 |
| 反序列化拒绝损坏输入 | 截断、超大长度、随机翻转等测试；解析失败不崩溃；失败不污染目标对象 |
| 分开统计分配与网络 | 编码/解码时间、分配次数/字节、传输 RTT 分开记录，不混成一个端到端数字 |
| 能解释 varint 与长度字段 | 能画出 tag + length + payload 的排列，能解释负数为什么可能占 10 字节，能解释长度前缀为什么要求先算大小 |
| 能解释 Arena 与复用 | 能说明 Arena 减少了什么、没有减少什么；能说明复用对象为什么可能保留大容量和共享状态 |

---

## 延伸阅读：sourceStudy

本节点 sourceStudy 的要求是：**沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。**

这条要求可以落实为四个动作。

### 动作一：定位 varint 与长度字段的定义和示例

打开资源页面后，先找到与 varint、长度分隔字段相关的定义，不要从第一段顺读到尾。重点核对：

- 每个字节的低 7 位和最高位分别表示什么；超过 1 字节的值怎样排列；
- 负数和 `sint32`/`sint64` 的 ZigZag 映射规则；
- 长度分隔字段的 `tag + length + payload` 排列；
- 字段编号、wire type 和 tag 的关系；
- 示例中的字节序列与你自己手算的结果是否一致。

每读到一条定义，回到本课对应段落复述，并举一个反例。例如：为什么 `int32` 的 `-1` 不是 1 个字节，而 `sint32` 的 `-1` 是 1 个字节；为什么长度字段本身也用 varint，而不是固定 4 字节。

### 动作二：核对对象构造与 Arena 的语义

资源推荐理由要求重点阅读“varint 与长度字段”和“对象构造和 Arena”。编码指南通常重点讲线格式，对象构造和 Arena 更多出现在 C++ API、Arena 分配器和生成代码的文档或头文件中。读的时候要分开记录：

- 消息对象、字符串、`repeated` 元素、嵌套消息分别在什么时候构造；
- `Clear()`、`ParseFromArray`、`MergeFromString`、`SerializeToString` 对已有对象做了什么；
- Arena 的创建、`Reset`、销毁分别影响哪些对象；
- `SpaceUsed` 和 `SpaceAllocated` 的语义差异；
- 哪些对象或容器可能不走 Arena。

不要把“资源页面上没写”当成“实现不会做”。以手头版本的头文件、生成代码和实际测试为准，并把差异记下来。

### 动作三：走错误处理路径

不要只看“正确输入会怎样”，还要看“错误输入会怎样”。至少核对下面这些路径：

- varint 在中间被截断；
- varint 超过 10 字节或发生 64 位溢出；
- 长度字段大于剩余字节；
- 长度字段大于实现或应用上限；
- 嵌套消息递归过深；
- 非法 wire type；
- 解析失败时消息对象的状态；
- 超限时是在分配前拒绝，还是已经分配后才失败。

把“资源怎么说”“运行时实际怎么做”“你的应用层应该怎么防”分成三列记录。这样才能把文档知识变成可执行的防护。

### 动作四：记录版本和边界

建议维护一张小表：

| 项目 | 记录内容 |
| --- | --- |
| `protoc --version` | 实际使用的生成器版本 |
| Protobuf 运行时版本 | `libprotobuf` 或包管理器版本 |
| 编译器与标准 | 例如 C++17，以及实际编译选项 |
| 操作系统与 CPU | 影响分配和计时结果 |
| 已确认的 varint 行为 | 负数、ZigZag、截断、溢出 |
| 已确认的长度字段行为 | 上限、剩余字节检查、嵌套深度 |
| 已确认的 Arena 行为 | 创建、Reset、销毁、SpaceUsed/SpaceAllocated |
| 未确认/存疑 | 文档没写清或与实现不一致的点 |
| 实验日期 | 便于以后复现 |

这份记录不是形式主义。Protobuf 的 API 和边界行为会随版本变化，尤其是解析上限、Arena 接口和未知字段处理。把版本、边界和实际测试结果写下来，下次升级或排障时就不用重新猜。

---

## 自测题

先别看答案。能对着三个要点、practice 和 acceptance 讲清楚，再对照答案检查。

### 题 1：varint、负数和长度字段

**问题**：一条 proto3 消息里有四个字段：

```proto
message M {
  int32 a = 1;    // 值 300
  string b = 2;   // 值 "hi"
  int32 c = 3;    // 值 -1
  sint32 d = 4;   // 值 -1
}
```

请回答：

1. 四个字段的 tag 分别是多少（十六进制）？
2. 按字段编号 1、2、3、4 的顺序，写出完整线格式字节。
3. 为什么 `c = -1` 和 `d = -1` 的字节数差这么多？
4. 如果 `b` 的值不是 `"hi"`，而是一个 1 MiB 的字符串，编码器在写 `b` 的长度和内容时需要注意什么？

**答案**：

1. 字段 1 的 tag = `(1 << 3) | 0 = 0x08`；字段 2 的 tag = `(2 << 3) | 2 = 0x12`；字段 3 的 tag = `(3 << 3) | 0 = 0x18`；字段 4 的 tag = `(4 << 3) | 0 = 0x20`。
2. 字段 1：`08 AC 02`；字段 2：`12 02 68 69`；字段 3：`18 FF FF FF FF FF FF FF FF FF 01`；字段 4：`20 01`。完整字节为 `08 AC 02 12 02 68 69 18 FF FF FF FF FF FF FF FF FF 01 20 01`。
3. `int32` 的负数会先符号扩展成 64 位，再按 varint 编码，所以 `-1` 占 10 字节；`sint32` 先用 ZigZag 把 `-1` 映射成 `1`，再用 varint 编码，所以只占 1 字节。负数常见时，`sint32` 通常更省字节，但会多一次 ZigZag 变换。
4. 编码器要先得到字符串长度，把长度写成 varint，再复制内容；长度字段本身可能占多个字节。解码端不能直接相信这个长度：要先检查它是否超过应用上限、是否小于等于剩余可读字节，再决定是否分配。对于 1 MiB 这样的负载，还要考虑复制次数、输出缓冲扩容和消息总上限。

### 题 2：成本核算

**问题**：假设一个服务每秒处理 200000 次 RPC，每条消息线上 180 字节，每条消息平均发生 24 次内存分配，应用层和框架层合计复制的字节数约为线上字节数的 2.5 倍。请回答：

1. 每秒大约发生多少次分配？每秒复制的字节数是多少？
2. 如果只看“消息只有 180 字节”，为什么会得出错误结论？
3. 你应该优先测哪些指标，才能判断热点在分配、复制还是网络？
4. 如果实验发现编码时间只占总 RTT 的 5%，但分配次数很高，应该优先优化什么？

**答案**：

1. 分配次数约为 `200000 × 24 = 4,800,000` 次/秒；复制字节数约为 `200000 × 180 × 2.5 = 90,000,000` 字节/秒，也就是约 90 MB/s。这里的“复制”包括应用显式复制和框架层可统计的复制，具体边界取决于你的计数器。
2. 180 字节只是线上负载大小，不代表 CPU 成本。高频小消息的主要成本可能是对象构造、分配器元数据、缓冲管理和框架固定开销；90 MB/s 的复制量也说明数据被搬了多遍。只比较消息字节数，会忽略这些与消息大小不成比例的成本。
3. 至少分开测：编码时间、解码时间、每条消息的分配次数和分配字节数、显式复制的字节数、系统调用/传输时间，以及 p50/p95/p99。还要按真实消息分布分组，避免平均值掩盖长尾。
4. 如果编码只占 5% 而分配次数很高，优先减少分配：复用对象/缓冲、使用 Arena 或对象池、减少临时字符串、避免每条消息都新建容器。但要注意节点 tradeoffs：复用会保留大容量并可能引入共享状态，所以同时要测内存保留和并发安全。

### 题 3：Arena、复用与内存保留

**问题**：一个服务为了减少分配，复用一个 `Payload` 对象解析请求，并在每条请求后调用 `Clear()`。上线后发现：平时内存稳定在 50 MiB，经历一次大 payload 高峰后 RSS 涨到 300 MiB，之后即使流量恢复，RSS 也降不下来。请回答：

1. 结合 `Clear()` 和容器容量的行为，解释 RSS 为什么降不下来。
2. 如果改用 Arena，`Reset()` 之后内存会立刻还给操作系统吗？为什么？
3. 设计一个既能减少分配、又不会无限保留大容量的复用策略。
4. 复用对象还可能带来哪些正确性问题？

**答案**：

1. `Clear()` 通常只把消息恢复到“没有字段被设置”的状态，不一定释放 `std::string`、`repeated` 容器等已经分配的容量。大 payload 让这些容器扩容到很大；之后即使只处理小消息，容量仍然保留，分配器也不一定把页还给操作系统，所以 RSS 居高不下。
2. 不一定。Arena 的 `Reset()` 通常只是把分配游标回到起点，已经申请的块往往会保留以便复用；`SpaceAllocated` 可能仍然很大。是否归还、何时归还取决于 Arena 实现和版本，必须实测，不能假设。
3. 可以组合使用：设置单对象/单缓冲的容量上限，超过上限的对象不回收进池；定期对超大容量执行 `shrink_to_fit()` 或与空对象交换；使用线程本地池降低共享；按请求或批次使用 Arena，并对 Arena 块大小设上限；监控池的命中率、保留字节数和最大值，发现异常时主动收缩。关键是把“复用”与“无上限保留”分开。
4. 可能的问题包括：`Clear()` 没有清干净导致上一条请求的字段残留；对象被多个线程共享导致数据竞争；异步回调持有对象时，下一条请求已经复用了它；对象里保存了指向旧输入缓冲的 `string_view` 或指针，复用后悬空；未知字段或缓存的大小没有重置。复用必须有明确的所有权、清理协议和并发模型。

### 题 4：消息上限、损坏输入与零拷贝

**问题**：

1. 帧头声明长度为 `0xFFFFFFFF`，应用在读取负载前应该做哪些检查？为什么不能先 `resize` 再读？
2. 解析器读到长度字段 1000，但当前只剩 10 个字节，应该怎样处理？
3. 有同事说“我们用了零拷贝发送 API，所以序列化链路没有复制”。请列出至少四个可能仍然发生复制的位置。
4. 解析失败后，直接把半成品消息返回给业务层，可能有什么问题？

**答案**：

1. 先检查声明长度是否超过应用上限，再检查是否可能造成整数溢出，最后才决定是否读取和分配。不能先 `resize` 再读，因为攻击者可以用一个很小的包声明巨大的长度，迫使服务在真正收到数据前就申请大量内存，造成 OOM 或拒绝服务。
2. 应该拒绝这条消息或当前字段，返回解析失败；不能用“长度为 1000”去读 10 个字节，也不能越界访问。安全的解析器会检查 `length <= remaining`，并在长度前缀本身溢出或超过上限时尽早失败。
3. 可能的复制点包括：对象字段编码到序列化输出缓冲；输出缓冲扩容时的旧内容搬迁；框架把序列化结果拼进发送帧或发送队列；系统调用把用户态数据复制到内核（取决于 API）；TLS/压缩层的输入输出缓冲；接收侧内核到用户态、以及解析时从输入缓冲复制到对象字段；日志或审计再次序列化。零拷贝 API 通常只消除其中一个环节。
4. 半成品对象可能包含部分已解析字段、未初始化的状态或不一致的容器，业务层可能误以为请求完整；如果对象里还有指向已释放/已复用缓冲的视图，还可能越界或读到别的请求数据。正确做法是解析到临时对象，成功后再交换；失败时丢弃并返回明确错误。

---

## 推荐资源

- **序列化成本与缓冲管理：参考文档 / 原始资料**
  - 链接：https://protobuf.dev/programming-guides/encoding/
  - 来源：protobuf.dev
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：按节点建议，重点阅读 varint 与长度字段、对象构造和 Arena，用于核对本节点实验的语义和边界。阅读时抓住：varint 的 7 位数据加继续位布局、负数和 ZigZag、长度分隔字段的 tag/length/payload、长度前缀与嵌套消息的编码顺序；同时结合手头版本的 C++ API 和头文件核对对象构造、`Clear`/解析行为、Arena 的创建/重置/销毁，以及超限、截断、递归深度等错误处理边界。资源页面没有展开的 C++ 基准测试、分配计数和本机传输实验，以本课实践为准。

---

## 本课小结

1. **序列化是一个过程。** 它包含遍历对象图、计算长度、编写 tag/varint、分配缓冲和复制字节；成本不能压缩成一个“消息字节数”。这是本节点 summary 的核心。
2. **varint 用 7 位数据加 1 位继续位。** 小非负整数很省字节；大整数和负数可能变长；`int32` 的负数会符号扩展成 10 字节，`sint32` 用 ZigZag 可以把小负数压回 1 到 2 字节。
3. **长度字段是 varint，长度分隔字段是 tag + length + payload。** 编码器必须先知道负载长度，可能多一次遍历或一次临时缓冲；解码器必须检查长度是否溢出、是否超过上限、是否小于等于剩余字节。
4. **反序列化要构造对象。** 字符串、`repeated` 容器、嵌套消息和 `map` 节点都可能分配；高频小消息的热点常常是分配和对象生命周期，而不只是解码循环。
5. **Arena 把逐对象分配变成批量分配。** 它减少分配次数，但自己也要申请块，不能消除负载复制，也不能保证 `Reset()` 立刻把内存还给操作系统；对象生命周期被绑定到 Arena。
6. **复制要按链路核算。** 对象到输出缓冲、缓冲扩容、框架拼帧、系统调用、TLS/压缩、接收侧解析、日志都可能复制；`copied_bytes / wire_bytes` 比消息大小更能揭示热点。
7. **零拷贝 API 不是端到端零复制。** 这是节点 pitfalls。一个环节消除了复制，不代表整条链路没有复制；要按完整数据路径验证。
8. **消息上限至少覆盖总量、单字段、元素个数和嵌套深度。** 长度来自不可信输入，必须先检查上限和剩余字节，再分配；不能先按声明长度 `resize`。
9. **解析失败不要暴露半成品。** 先解析到临时对象，成功后再交换；失败时返回明确错误，避免业务层读到部分字段或悬空视图。
10. **复用对象减少分配，但易保留大容量并引入共享状态。** 这是节点 tradeoffs。复用要有容量上限、明确的重置协议、线程本地优先和监控；否则会把分配问题换成内存问题。
11. **测量必须包含真实 payload 分布。** 这是 acceptance 的第一条。按字段长度、数值范围、`repeated` 个数、嵌套深度和样本比例分组，保留 p50/p95/p99，不要只用平均值或一条手工消息。
12. **反序列化必须拒绝超限和损坏输入。** 这是 acceptance 的第二条。测试要覆盖截断、超大长度、帧头超限、随机损坏和递归过深，并断言不崩溃、不越界、不污染目标对象。
13. **分开统计分配与网络耗时。** 这是 practice 的核心。编码、解码、分配、复制和传输分别计时；网络测试用预先编码的字节，传输数字要和序列化数字分表记录。
14. **本课服务于“解释 RPC 调用端 CPU 与分配热点”。** 先建立成本模型，再用真实分布测量，最后根据结果选择 varint 类型、长度编码策略、Arena、对象复用或缓冲管理；不要凭消息大小或 API 名字下结论。

本课的主线可以压缩成一句话：**用 varint 和长度字段理解线格式的字节成本，用对象构造和 Arena 理解反序列化的分配成本，用复制次数和消息上限理解端到端的安全与 CPU 成本；最后用真实分布把编码、解码、分配和传输分开测出来。** 下一步可以继续沿着这个方向深入：怎样用分配器剖析工具定位具体调用栈，怎样为消息上限设计可配置的策略，以及怎样在框架层减少缓冲复制而不引入共享状态。
