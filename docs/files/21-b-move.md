# 第 21/208 课：拷贝、移动与值类别

> 领域 B · 阶段 1 · 子类：生命周期与所有权 · 难度：基础 · 前置：b-raii（RAII 与资源所有权）

## 一、学习目标与为什么后端要懂

**一句话**：移动语义允许从"可转移的来源"接管资源；`std::move` 只做值类别转换，真正拷贝还是移动由重载决议决定。

**学完你应该能**：
1. 解释左值、右值与引用折叠；
2. 独立完成：实现可移动的字节缓冲区，并统计 `vector` 扩容中拷贝与移动的次数。

**为什么后端要懂**：减少不必要的复制，并正确处理容器扩容和资源转移。公众号后台里一条消息的包体、一个请求对象，常常要从解包层传到业务队列、再传到发送层。每一步如果是深拷贝，几 KB 的 buffer 就要重新分配内存再 memcpy 一次。QPS 一高，这些开销就会出现在火焰图上。用移动来交接，只是交换一个指针。

## 二、从直觉到原理

### 1. 左值、右值与引用折叠

**直觉**：左值是有名字、能取地址的东西，比如 `msg`；右值是临时的、马上要消失的东西，比如 `make_msg()` 的返回值、`a + b`。临时对象反正要销毁，把它的资源"偷"过来不会伤害任何人，这就是移动的出发点。

**原理**：
- `T&` 只绑定左值，`const T&` 什么都能绑，`T&&` 只绑定右值。
- `std::move(x)` 并不移动任何东西，它就是 `static_cast<T&&>(x)`，把 x 标记成"可以被偷"。真正搬资源的是之后被选中的**移动构造或移动赋值函数**。
- **有名字的右值引用本身是左值**：`void f(Buf&& b)` 函数体里的 `b` 是左值，要继续转交得再写 `std::move(b)`。
- **引用折叠**：在模板或 `auto&&` 里会出现"引用的引用"，规则是只要有一个 `&` 结果就是 `&`，只有 `&& &&` 才折叠成 `&&`。`template<class T> void f(T&& x)` 因此成为"转发引用"：传左值时 `T = U&`，`x` 是 `U&`；传右值时 `T = U`，`x` 是 `U&&`。配合 `std::forward<T>(x)` 能原样保留值类别。

```cpp
template <class T>
void enqueue(T&& req) {                  // 转发引用
    queue.push_back(std::forward<T>(req)); // 左值进来就拷贝，右值进来就移动
}
Request r = parse(pkt);
enqueue(r);             // 拷贝，r 之后还能用
enqueue(std::move(r));  // 移动，r 之后只能析构或重新赋值
```

### 2. 五法则与零法则

**五法则**：只要你手写了析构、拷贝构造、拷贝赋值、移动构造、移动赋值中的任意一个，通常就意味着你在直接管理资源，五个都要想清楚。只写析构的话，编译器不会再生成移动操作，"移动"会悄悄退化成拷贝。

**零法则**：最好一个都不写。成员用 `std::string`、`std::vector`、`std::unique_ptr`（上一课的 `UniqueFd` 也算）这类标准资源类组合起来，编译器生成的五个函数自动就是对的。

```cpp
struct Message {               // 零法则：什么特殊成员都不写
    std::string openid;
    std::vector<char> body;
    std::unique_ptr<Ctx> ctx;  // 有它就自动变成"只能移动"
};
```

### 3. noexcept 移动和拷贝消除

**为什么 noexcept 很关键**：`vector` 扩容要把旧元素搬到新内存，同时要保证强异常安全（失败了原 vector 不变）。如果移动构造可能抛异常，搬到一半失败就回不去了，所以 `vector` 会通过 `std::move_if_noexcept` 选择**拷贝**。只有移动构造标了 `noexcept`，扩容才会走移动。本课实践在 box 上实测的结果是：push 100 个元素，有 noexcept 时 copy=0、move=127；去掉 noexcept 后 copy=127、move=0。

**拷贝消除**：C++17 起，`return Buf(64);` 这种返回纯右值的写法**保证**不发生拷贝或移动，对象直接构造在调用方。`Buf b; ...; return b;` 这种具名返回（NRVO）编译器通常也会消除；即使没消除，也会先尝试按右值移动返回。

## 三、常见误区与设计权衡

**误区 1：return 局部变量时滥用 `std::move` 可能妨碍 NRVO。**
```cpp
Buf make() { Buf b(64); fill(b); return std::move(b); } // ✗ 返回类型变成 Buf&&，NRVO 失效，强制多一次移动
Buf make() { Buf b(64); fill(b); return b; }            // ✓ 交给 NRVO
```
GCC/Clang 的 `-Wpessimizing-move` 会对第一种写法报警。

**误区 2：对 const 对象 move 可能发生拷贝。** `std::move(const_obj)` 得到 `const T&&`，它绑不到 `T&&` 的移动构造上，只能匹配 `const T&` 的拷贝构造。编译不报错，但其实一直在拷贝。实践中会实测验证。

**误区 3：认为移动后的对象"没了"。** 移动后的对象处于"有效但未指定"的状态，之后**一定还会被析构**，所以移动构造必须把源对象置成可安全析构的状态（比如指针置空），否则会双重释放。它也可以被重新赋值后继续使用。

**误区 4：移动构造没标 `noexcept`。** 后果见上一节：放进 vector 后扩容全是拷贝。

**设计权衡**：优先组合标准资源类（零法则），只有直接管理资源（裸内存、fd、句柄）时才手写特殊成员，并且五个一起设计。

## 四、动手实践：可移动字节缓冲区 + 扩容计数

代码在附带的 `practice21/`（buffer.cpp、test.sh）中，已在 box 上跑通。

1. **实现 `ByteBuffer`**：持有 `char* data_` 和 `size_t size_`；拷贝构造深拷贝并让 `copy` 计数加 1；移动构造用 `std::exchange(o.data_, nullptr)` 接管资源并让 `move` 计数加 1；赋值用 copy-and-swap 写法；析构 `delete[] data_`。
2. **用宏控制 noexcept**：`ByteBuffer(ByteBuffer&& o) MOVE_NOEXCEPT`，默认定义为 `noexcept`，编译时加 `-DMOVE_NOEXCEPT=` 就能去掉。
3. **统计扩容**：往 `std::vector<ByteBuffer>` 里 `emplace_back` 100 次，打印拷贝和移动次数。
4. **验证 const move**：`const ByteBuffer c(32); ByteBuffer d = std::move(c);` 应该是 copy=1、move=0。
5. **验证移动后状态**：`b = std::move(a)` 之后 `a` 为空、size 为 0，重新赋值后可以继续使用，析构时不会双重释放（用 `-fsanitize=address` 检查）。
6. **运行**：`bash practice21/test.sh`，两个版本各编译一次，断言全部通过后输出 `ALL_TESTS_PASSED`。

实测输出：
```
== noexcept 移动 ==
vector_grow copy=0 move=127
const_move copy=1 move=0
moved_from empty=1 size=0 b.size=8
== 去掉 noexcept ==
vector_grow copy=127 move=0
```

**延伸（节点的源码研究建议）**：用编译器生成汇编（`g++ -S -O1`），结合构造日志，研究 vector 在 noexcept 条件变化时选择移动还是拷贝的路径，重点看 `std::__uninitialized_move_if_noexcept_a`。

**验收标准**：
- 能解释为什么对 const 对象 move 可能发生拷贝；
- 能说明为什么移动后的对象仍须可以析构。

## 五、自测题

**1.** `void push(Buf&& b) { q.push_back(b); }` 里，`push_back` 调用的是拷贝还是移动？为什么？
> **答**：拷贝。`b` 有名字，是左值，虽然它的类型是右值引用。要移动得写 `q.push_back(std::move(b))`。

**2.** 一个类只手写了析构函数来释放裸指针，把它放进 `vector` 扩容时会发生什么？
> **答**：手写析构会抑制编译器隐式生成移动操作，所以扩容走的是隐式拷贝构造。而隐式拷贝是浅拷贝指针，最终会双重释放。这正是五法则要求把五个特殊成员一起设计的原因，或者改用 `unique_ptr`，遵循零法则。

**3.** `Buf make() { Buf b; return std::move(b); }` 有什么问题？怎么改？
> **答**：`std::move` 让返回表达式变成 `Buf&&`，不再符合 NRVO 的条件，强制多一次移动构造。直接写 `return b;`：编译器会优先做 NRVO，做不了也会自动按右值移动。

## 六、推荐资源

- [cppreference：Move constructors](https://en.cppreference.com/w/cpp/language/move_constructor.html)（英文文档，基础，免费）：重点读左值、右值与引用折叠，以及五法则与零法则，用来核对本节点实验的语义和边界。
