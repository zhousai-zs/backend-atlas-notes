# 第 24/208 课：算法、Lambda与函数对象

> 领域 B · 阶段 1 · 泛型与标准库 · 难度：基础 · 前置：b-stl（第 23 课）

**一句话**：标准算法把“怎么遍历”和“对每个元素做什么”拆开，Lambda 用写明了捕获方式的闭包来表达这个小动作。

## 1. 学习目标与为什么后端要懂

学完这节课你应该能：

- 讲清楚算法的**前置条件**，比如 `lower_bound` 要求区间已经有序，`sort` 要求比较器满足严格弱序；
- 独立完成：**用 `sort`、`lower_bound` 和 `transform` 实现日志分析，并测试重复值和空输入。**

为什么后端要懂：你要写**能组合的处理逻辑**，还要能**认出异步回调里的捕获风险**。公众号后台常见的场景有：把一批消息按时间排序后取某个时间窗口；把请求日志转换成耗时数组算 P99；把回调丢进线程池或者定时器，“稍后”再执行。最后这一种场景最容易出线上 core。

## 2. 从直觉到原理

### 2.1 算法前置条件：算法不检查，你来保证

直觉上，`std::lower_bound` 就是二分查找。二分能成立，靠的是区间**已经按同一个比较规则排好序**。算法本身不会验证这一点，前提不满足就是未定义行为，通常表现为结果悄悄出错，而不是崩溃。

```cpp
std::vector<int> v = {5, 1, 4, 2};
auto it = std::lower_bound(v.begin(), v.end(), 4);     // ❌ 没排序，结果无意义
std::sort(v.begin(), v.end());                         // {1,2,4,5}
it = std::lower_bound(v.begin(), v.end(), 4);          // ✅ 指向 4
```

常见的前置条件：

| 算法 | 前置条件 |
|---|---|
| `lower_bound` / `upper_bound` / `binary_search` / `equal_range` | 区间按**同一比较器**有序（准确说是按目标值分区） |
| `sort` / `stable_sort` | 随机访问迭代器，比较器满足严格弱序 |
| `unique` | 只去掉**相邻**的重复，通常要先 `sort` |
| `transform` 输出到 `out` | `out` 要有足够空间，或者用 `back_inserter` |
| `merge` / `set_intersection` | 两个输入都有序 |

还有两个细节：`lower_bound` 返回第一个 `>= x` 的位置，`upper_bound` 返回第一个 `> x` 的位置，两者之差就是重复值的个数。空区间是合法输入，返回的就是 `end()`。调试时可以加 `assert(std::is_sorted(...))` 把前提写成显式检查。

### 2.2 严格弱序与比较器

`sort` 要的不是随便一个返回 bool 的函数，而是**严格弱序**（strict weak ordering）：

1. **非自反**：`comp(a, a)` 必须是 `false`；
2. **非对称**：`comp(a, b)` 为真时，`comp(b, a)` 必须为假；
3. **传递**：`comp(a, b)` 和 `comp(b, c)` 为真，则 `comp(a, c)` 为真；
4. **“等价”也可传递**：两者谁都不小于谁就算等价，等价关系要能传递。

最经典的错误是用 `<=`：`comp(7, 7) == true`，直接违反第 1 条。`std::sort` 内部的分区循环依赖“遇到相等元素会停下来”，比较器一直返回 true 时可能**越界读写**，元素多、重复多的时候就是 core。

```cpp
auto bad  = [](int a, int b) { return a <= b; };   // ❌
auto good = [](int a, int b) { return a <  b; };   // ✅
```

多字段排序要用 `std::tie`，不要手写一串容易写错的 if：

```cpp
// 先按 ts 升序，ts 相同再按 uid 升序
std::sort(logs.begin(), logs.end(), [](const Log& a, const Log& b) {
    return std::tie(a.ts, a.uid) < std::tie(b.ts, b.uid);
});
```

另外几个要点：

- 浮点数里有 NaN 时，`<` 不满足严格弱序，要先过滤掉；
- 比较器在排序过程中必须**稳定不变**，不能依赖会被修改的外部状态或随机数；
- 相等元素需要保留原来的顺序（比如同一秒内的消息按到达顺序排）时，用 `stable_sort`；
- `lower_bound` 和 `sort` 要用**同一个排序规则**。

### 2.3 Lambda 是什么：编译器帮你生成的函数对象

```cpp
int threshold = 100;
auto slow = [threshold](const Log& l) { return l.latency_ms > threshold; };
```

编译器大致把它生成成下面这样的类：

```cpp
struct __Lambda {
    int threshold;                       // 值捕获：拷贝一份成员
    bool operator()(const Log& l) const { return l.latency_ms > threshold; }
};
```

所以 Lambda 就是一个**带状态的函数对象**（functor）。它可以被内联，通常比函数指针快；装进 `std::function` 时会做类型擦除，可能有堆分配。

### 2.4 捕获值、引用和 this

| 写法 | 含义 | 风险 |
|---|---|---|
| `[x]` | 拷贝 x | 大对象拷贝有开销 |
| `[&x]` | 引用 x | **x 销毁后再调用就是悬垂引用** |
| `[=]` / `[&]` | 隐式捕获所有用到的变量 | 看不出到底捕获了什么，不推荐用于异步场景 |
| `[this]` | 捕获指针 this | **不会延长对象寿命** |
| `[*this]`（C++17） | 拷贝整个对象 | 拷贝开销，改的是副本 |
| `[self = shared_from_this()]` | 持有 shared_ptr，保活 | 可能形成循环引用（第 22 课） |
| `[p = std::move(p)]` | 初始化捕获，可以移动进去 | 原变量已被移走 |

判断规则只有一条：**Lambda 会不会比它捕获的东西活得更久？**

- 同步调用，比如 `std::sort(..., [&](...){})`：调用期间局部变量一定还活着，用引用捕获安全，也最高效；
- 延迟执行，比如进线程池、定时器、RPC 回调：局部变量在函数返回时就销毁了，**必须值捕获或移动捕获**；对象成员要用 `shared_from_this()` 或 `weak_ptr` 来保活或判活。

```cpp
void Handler::OnRequest(Req req) {
    std::string openid = req.openid();
    pool.Submit([&openid] { Log(openid); });                // ❌ 返回后 openid 已销毁
    pool.Submit([openid] { Log(openid); });                 // ✅ 值捕获
    pool.Submit([this] { this->Reply(); });                 // ❌ Handler 可能已经析构
    pool.Submit([self = shared_from_this()] { self->Reply(); });   // ✅ 保活
    pool.Submit([w = weak_from_this()] { if (auto s = w.lock()) s->Reply(); });  // ✅ 已经销毁就跳过
}
```

注意 `[=]` 在成员函数里会**隐式捕获 this**（C++20 起已经弃用这种隐式行为）。它看起来像“全按值拷贝”，实际上访问的成员仍然走裸 this。

## 3. 常见误区

- **异步捕获 this 不会自动延长对象寿命。**这是本节点点名的坑：`[this]` 只是拷贝了一个指针。
- 用 `<=` 或 `>=` 写比较器，可能导致 sort 越界和 core。
- 对没排序的数据用 `lower_bound`，或者排序和查找用了不同的比较规则。
- 以为 `unique` 能去掉所有重复。它只合并相邻的重复，而且要配合 `erase` 才会真正缩小容器。
- `transform` 写到一个空 vector 的 `begin()`，结果越界。要先 `resize`，或者用 `back_inserter`。
- 用 `[&]` 一把梭，再把 Lambda 存进成员或者队列里。

## 4. 设计权衡

**算法减少了手工循环的错误，但要警惕它把多次扫描和拷贝藏起来。**

- 好处：用 `count_if`、`transform`、`lower_bound` 一行就把意图说清楚，不会写错下标边界，比手写循环更容易审查；
- 代价：一连串 `copy_if → transform → sort → unique`，每一步都是一次完整遍历，可能还有一次中间容器的拷贝。热路径上可以合成一次循环，或者先 `reserve`；
- 有些看起来轻的操作实际很贵：比较器按值接收 `Log`，每次比较都会拷贝 string，要写成 `const Log&`；`std::function` 也可能有堆分配；
- 想把 `sort` 换成 `partial_sort` / `nth_element` 时，比如只要 Top-K 或 P99：`nth_element` 平均 O(n)，比完整排序省很多。

## 5. 动手实践：日志分析

节点任务：**用 `sort`、`lower_bound` 和 `transform` 实现日志分析，测试重复值和空输入。**

配套代码在附件 `practice24.tar.gz` 里，解压后运行 `./test.sh`，需要 g++ 并支持 C++17。我已经在 Linux 上跑过，6 项全部通过。

1. **定义数据**：`struct Log { long ts; int latency_ms; std::string uid; };`
2. **排序**：用 `std::stable_sort` 加比较器 `a.ts < b.ts`，再用 `assert(std::is_sorted(...))` 验证前提。
3. **窗口查询**：两次 `lower_bound` 求 `[from, to)` 内的条数，异构比较器写成 `[](const Log& l, long t){ return l.ts < t; }`。
4. **转换**：用 `transform` 加 `back_inserter` 取出耗时数组，提前 `reserve`。
5. **测边界**：空输入返回 0；3 条相同 ts 的计数正好是 3；窗口在数据范围外时返回 0；`stable_sort` 保持相等元素原来的顺序。对应 `log_analyze.cpp`，用 ASan 和 UBSan 编译后会输出 `ALL_TESTS_PASSED`。
6. **故意写错比较器**：`bad_comparator.cpp` 用 `<=` 排 64 个相同元素。用 `g++ -D_GLIBCXX_DEBUG` 编译后运行，会报 `comparison doesn't meet irreflexive requirements, assert(!(a < a))` 并中止。
7. **捕获实验**：`capture.cpp` 模拟“函数返回后才执行”的任务队列，用 `-fsanitize=address` 编译后跑四种模式：
   - `./capture value`：值捕获正常输出；
   - `./capture ref`：引用捕获局部 string，ASan 报 **heap-use-after-free**（string 的堆缓冲区已经释放）；
   - `./capture this`：捕获裸 this，对象销毁后 ASan 报 **heap-use-after-free**；
   - `./capture self`：捕获 `shared_from_this()` 后正常输出 `u42`。

**验收标准**（来自节点）：

- [ ] 能解释错误比较器为什么破坏 sort 的前提；
- [ ] 延迟执行的 Lambda 不捕获已经销毁的局部变量的引用。

延伸练习：给 `Log` 加 P99 统计，对比 `sort` 和 `nth_element` 在 100 万条数据上的耗时。

## 6. 自测题

**Q1.** 比较器写成 `return a.ts <= b.ts;`，为什么可能让 `std::sort` 崩溃，而不只是排得不对？

<details><summary>答案</summary>
它违反了严格弱序的非自反性：`comp(a, a)` 为 true。libstdc++ 的 introsort 在分区时依赖“遇到不小于 pivot 的元素就停下”来避免越界检查，所有相等元素都返回 true 时，扫描指针会一直越过区间边界，导致越界读写。这属于未定义行为，常见表现就是 core。正确写法是 `<`，调试时可以用 `-D_GLIBCXX_DEBUG` 检测。
</details>

**Q2.** 下面代码有什么问题？怎么改？
```cpp
void Session::Start() {
    timer_.After(5s, [this] { SendHeartbeat(); });
}
```

<details><summary>答案</summary>
5 秒后 Session 可能已经析构，`[this]` 只是一个裸指针，不会延长对象寿命，回调里访问成员就是 use-after-free。改法有两种：需要保活时用 `[self = shared_from_this()]`（要求对象由 shared_ptr 管理）；不想延长寿命时用 `[w = weak_from_this()] { if (auto s = w.lock()) s->SendHeartbeat(); }`。也可以在析构时取消定时器，但要处理回调已经在执行的竞争。
</details>

**Q3.** 已排序的 `vector<int> v = {1, 3, 3, 3, 7}`，怎么一次求出 3 出现的次数？空 vector 时结果是多少？

<details><summary>答案</summary>
`auto [lo, hi] = std::equal_range(v.begin(), v.end(), 3); auto n = hi - lo;`，等价于 `upper_bound - lower_bound`，结果是 3，复杂度 O(log n)。空 vector 时 lo 和 hi 都是 `end()`，结果是 0，不需要特判。前提是 v 按 `<` 有序。
</details>

## 7. 推荐资源

- [cppreference：Algorithms library](https://en.cppreference.com/w/cpp/algorithm.html)（英文 · 文档 · 基础 · 免费）：重点读“算法前置条件”和“捕获值、引用和 this”两部分，用来核对本节实验的语义和边界。
- 读源码的方法（来自节点）：沿着资源里的定义、示例和错误处理路径核对实现，并记录适用的版本和边界条件。比如 Lambda 的 `[=]` 隐式捕获 this 在 C++20 起被弃用。
