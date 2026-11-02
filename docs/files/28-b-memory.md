# 第 28/208 课：分配器、对齐与内存资源

> 领域 B · 阶段 3 · 难度：进阶 · 前置：b-lifetime（对象生命周期）、b-template（模板）

**一句话**：动态分配管理的是“原始存储”，对齐约束和对象构造是两件要分别满足的事，而内存资源（memory resource）让你可以定制分配的生命周期。

## 1. 学习目标与为什么后端要懂

学完这一课你应该能：

- 解释 `new` 和 `malloc` 的区别；
- 独立完成：对比普通分配和 `monotonic_buffer_resource` 处理批次请求时的分配次数；
- （验收）说清“释放存储”和“析构”不是一回事；
- （验收）保证内存池在异常路径上不留下悬垂对象。

**为什么后端要懂**：节点给的理由是“分析频繁分配、碎片和对象池带来的性能及安全影响”。放到公众号后台的场景里想一下：一个接入服务每秒处理大量请求，每个请求要解析出几十个字段，每个字段一个 `std::string`、一个 `vector`。每次 `new` 都要进分配器，可能抢锁，还会造成碎片。高峰时延迟毛刺很可能就来自这里。对象池和内存资源是常见的解法，但用错了就会出现悬垂指针和内存只涨不降。

## 2. 核心要点：从直觉到原理

### 2.1 new 与 malloc 的区别

**直觉**：`malloc` 只给你一块“生地”，`new` 是“拿地并盖好房子”。

**原理**：一个 `new T(args)` 表达式做两步：

1. 调 `operator new(sizeof(T))` 拿原始存储（默认实现通常也是向底层分配器要内存）；
2. 在这块存储上调 `T` 的构造函数。

`delete p` 反过来：先调析构函数，再调 `operator delete` 释放存储。

| | `malloc` / `free` | `new` / `delete` |
|---|---|---|
| 做什么 | 只分配、只释放字节 | 分配 + 构造 / 析构 + 释放 |
| 失败时 | 返回 `nullptr` | 抛 `std::bad_alloc`（`nothrow` 版本返回空） |
| 类型 | 返回 `void*` | 返回 `T*` |
| 能否定制 | 换 malloc 实现（如 jemalloc/tcmalloc） | 可重载 `operator new`，可按类重载 |

**关键结论：释放存储和析构是两回事。**

```cpp
#include <new>
#include <string>

alignas(std::string) unsigned char raw[sizeof(std::string)];
auto* s = new (raw) std::string("hello");  // placement new：只构造，不分配
s->~basic_string();                        // 只析构；存储是栈上的 raw，不需要也不能释放
// delete s;   // 错！这块内存不是 operator new 给的
```

**配对规则不能混用**：`malloc` 配 `free`，`new` 配 `delete`，`new[]` 配 `delete[]`，placement new 只配手动析构。混用就是未定义行为。

### 2.2 alignof 与过对齐

**直觉**：CPU 读数据时喜欢地址是某个数的倍数。`int` 通常要求 4 字节对齐，`double` 要求 8 字节。

**原理**：

- `alignof(T)` 返回类型的对齐要求；
- `alignas(N)` 可以把对齐要求调大；
- 对齐要求超过 `alignof(std::max_align_t)`（x86-64 上通常是 16）的类型叫**过对齐类型**；
- `malloc` 只保证 `max_align_t` 级别的对齐。C++17 起，`new` 一个过对齐类型会自动调用 `operator new(size, std::align_val_t)`，C 里则用 `aligned_alloc`。

后台常见用法是让每个线程的计数器独占一条缓存行，避免伪共享：

```cpp
struct alignas(64) Counter { long v; };   // alignof=64，sizeof 也被填充到 64

Counter* p = new Counter[4];              // C++17：地址自动按 64 对齐
void* q = std::aligned_alloc(64, 256);    // 大小必须是对齐的整数倍
```

在本机实测（g++ 14、x86-64）的输出：

```
alignof(int)=4 alignof(max_align_t)=16 alignof(Counter)=64 sizeof(Counter)=64
new Counter[] addr%64=0
aligned_alloc addr%64=0
```

**对齐要和构造分开满足**：自己手写内存池、用 placement new 时，给出的地址必须满足 `alignof(T)`，否则在某些平台上会崩溃，在 x86 上可能只是变慢，但同样是未定义行为。写缓冲区时用 `alignas(T) unsigned char buf[...]` 或 `std::align` 来对齐。

### 2.3 allocator 与 pmr 资源

**传统 allocator**：`std::vector<T, Alloc>` 里的分配器是**模板参数**。换一个分配器就是换一个类型，`vector<int, A>` 和 `vector<int, B>` 不能互相赋值，接口很难统一。

**C++17 的 pmr（polymorphic memory resource）**：分配策略变成一个运行时对象 `std::pmr::memory_resource*`。容器统一是 `std::pmr::vector<T>`，构造时传入资源指针就行。

常用的三种资源：

- `new_delete_resource()`：默认资源，走 `::operator new` / `delete`；
- `monotonic_buffer_resource`：只往前切内存，单个 `deallocate` 什么也不做，资源析构（或 `release()`）时才整块归还。缓冲用完后向**上游资源**要新块；
- `unsynchronized_pool_resource` / `synchronized_pool_resource`：按大小分档的池，能复用已释放的块（前者不带锁，后者线程安全）。

```cpp
#include <memory_resource>

alignas(std::max_align_t) char buf[64 * 1024];
{
    std::pmr::monotonic_buffer_resource pool(buf, sizeof(buf),
                                             std::pmr::null_memory_resource());
    std::pmr::vector<std::pmr::string> req(&pool);   // vector 和里面的 string 都从 pool 里切
    req.emplace_back("openid_oXyz1234567890abcdef");
}   // req 先析构（各个 string 的析构执行，但“释放”是空操作），然后 pool 析构
```

这里 `null_memory_resource()` 当上游的意思是：缓冲不够就直接抛 `bad_alloc`，绝不偷偷去堆上分配，测试时很有用。

**声明顺序很重要**：资源必须比使用它的容器活得更久。所以资源先声明、容器后声明，这样容器先析构、资源后析构。

## 3. 常见误区与设计权衡

**误区（节点原文）**：*placement new 不获取额外存储，释放规则不能混用。*

- 对 placement new 出来的指针调 `delete`：错，它的存储不是 `operator new` 给的；
- 以为 `monotonic_buffer_resource` 的 deallocate 会归还内存：不会，只有资源析构或 `release()` 时才归还；
- 以为“内存释放了，对象就析构了”：对 monotonic 资源直接 `release()`，不会帮你调对象的析构函数。如果对象自己持有其他资源（文件句柄、来自别处的内存），就会泄漏。应该先让容器析构，再释放资源；
- 容器比资源活得更久：资源一析构，容器里的指针就悬垂了。

**权衡（节点原文）**：*池化减少分配成本，也可能保留高峰内存并掩盖越界。*

- **保留高峰内存**：一个长期存活的 monotonic 资源只涨不降。如果整个进程共用一个，内存会停在历史峰值。所以 monotonic 适合“一个请求一个资源、请求结束整体丢弃”这种生命周期很明确的场景；
- **掩盖越界**：池里的对象挨得很近，越界写会悄悄改坏邻居，而不是落在 ASan 能抓到的保护区上。调试时可以临时切回默认资源再跑 ASan；
- 线程：`unsynchronized_*` 和 `monotonic_buffer_resource` 都不是线程安全的，跨线程共享时要换 `synchronized_pool_resource` 或每线程一个资源。

## 4. 动手实践：对比普通分配与 monotonic_buffer_resource 的分配次数

代码已经放在 `/workspace/atlas/practice28/`（`batch.cpp`、`align.cpp`、`test.sh`），我在本机跑通了。

**步骤 1：统计全局分配次数。** 重载全局 `operator new`，每调一次计数加一。注意也要重载带 `std::align_val_t` 的版本，因为 libstdc++ 的 `new_delete_resource` 走的是这个版本，漏了就会少算。

```cpp
static size_t g_new_calls = 0;
void* operator new(std::size_t n) {
    ++g_new_calls;
    if (void* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
void operator delete(void* p) noexcept { std::free(p); }
// 另外还有 operator new(size_t, align_val_t) 及对应的 delete，见 batch.cpp
```

**步骤 2：模拟一批请求。** 每个请求解析出 4 个长字段（超过短字符串优化 SSO 的长度，所以一定会堆分配），放进 `vector<string>`。跑 1000 个请求：

```cpp
for (int r = 0; r < batch; ++r) {
    std::vector<std::string> req;
    for (auto f : kFields) req.emplace_back(f);
}
```

**步骤 3：换成每个请求一个 monotonic 资源**，用栈上（静态）缓冲，上游设成 `null_memory_resource()`：

```cpp
for (int r = 0; r < batch; ++r) {
    std::pmr::monotonic_buffer_resource pool(buf, sizeof(buf), std::pmr::null_memory_resource());
    std::pmr::vector<std::pmr::string> req(&pool);
    for (auto f : kFields) req.emplace_back(f);
}
```

**步骤 4：对照组，缓冲不够时回退上游。** 只给 64 字节缓冲、上游用默认的 `new_delete_resource`，整个批次共用一个 pool。

**步骤 5：异常路径。** 在 pool 上构造一组 `Tracked` 对象，第 3 个构造时抛异常，检查存活对象数回到 0。

**步骤 6：运行** `bash /workspace/atlas/practice28/test.sh`，实测输出：

```
batch=1000  std::allocator new calls=7000
batch=1000  monotonic(stack buf, null upstream) new calls=0
batch=1000  monotonic(64B buf, new_delete upstream) new calls=19
caught: parse fail, alive=0
ALL_CHECKS_PASSED
```

**怎么解读**：

- 普通分配每个请求 7 次：4 个字符串，加上 vector 从 1→2→4 的 3 次扩容；
- monotonic 加栈上缓冲是 0 次，请求结束时整块丢弃；
- 64 字节缓冲的对照组只有 19 次上游分配。这是因为 monotonic 每次向上游要的块会按几何级数变大。代价是这 1000 个请求用过的内存在 pool 析构前一直不归还，这正是“保留高峰内存”；
- 异常路径上，`vector` 自己保证已构造的元素被析构，pool 再整体释放，没有悬垂对象。

**进阶（节点的 sourceStudy）**：阅读 libstdc++ 的 `memory_resource` 实现，追踪分配、上游回退和批次释放这三条路径。

## 5. 自测题

**题 1**：`auto* p = new (buf) Foo;` 之后，用 `delete p;` 释放对吗？应该怎么做？

<details><summary>答案</summary>

不对。placement new 不分配存储，`buf` 不是 `operator new` 给的，`delete` 会把它当堆内存释放，是未定义行为。正确做法是 `p->~Foo();` 只析构，`buf` 的存储按它本来的方式管理（栈上就随作用域结束，堆上就用对应的释放函数）。
</details>

**题 2**：在 x86-64 上，`malloc(sizeof(T))` 拿到的内存，能直接 placement new 一个 `struct alignas(64) T` 吗？

<details><summary>答案</summary>

不能保证。`malloc` 只保证 `alignof(std::max_align_t)`（通常是 16）的对齐，64 字节对齐不一定满足。应该用 `std::aligned_alloc(64, size)`（size 是 64 的倍数），或者直接 `new T`（C++17 会自动走对齐版本的 `operator new`）。
</details>

**题 3**：有人在进程启动时创建一个全局 `monotonic_buffer_resource`，所有请求都从它分配，上线后内存一直涨。为什么？怎么改？

<details><summary>答案</summary>

`monotonic_buffer_resource` 的 `deallocate` 是空操作，内存只在资源析构或 `release()` 时归还，全局资源永远不析构，所以只涨不降。改法：每个请求（或每个批次）一个 monotonic 资源，请求结束整体丢弃；如果确实需要长期复用、释放后能回收，改用 `unsynchronized_pool_resource`（单线程）或 `synchronized_pool_resource`（多线程）。
</details>

## 6. 推荐资源

- [cppreference：std::pmr::memory_resource](https://en.cppreference.com/w/cpp/memory/memory_resource.html)（英文文档，进阶）：节点建议重点读 new 与 malloc 的区别、alignof 与过对齐，用来核对本节实验的语义和边界。
