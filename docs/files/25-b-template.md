# 第 25/208 课：模板与编译期泛型

> 领域 B · 阶段 2 · 基础 ｜ 子类：泛型与标准库 ｜ 前置：第 24 课 算法、Lambda 与函数对象

**一句话**：模板根据类型或常量参数生成具体代码，约束与实例化规则决定可用接口及错误呈现。

## 一、学习目标 & 为什么后端要懂

学完你应该能：
1. 解释函数模板与类模板是怎么"生成代码"的；
2. 独立完成：实现支持可比较类型的有界队列接口，为无效类型提供清晰编译错误。

验收标准（节点原文）：
- 能解释模板定义为何常放头文件；
- 能区分重载与特化。

**为什么后端要懂**：读懂容器、RPC 适配层和通用工具库。进了公众号后台，你会天天碰到 `std::vector<T>`、`std::unordered_map<K,V>`，还有各种"一个模板适配所有请求/响应类型"的 RPC 框架代码和公共库。看不懂模板，你就只能看懂调用方，看不懂实现，更看不懂那几百行的编译报错。

## 二、从直觉到原理

### 1. 函数与类模板：模板是"代码的模具"

直觉：模板本身不是代码，而是**生成代码的规则**。你写 `BoundedQueue<int>`，编译器才照着模具"印"出一份 int 版本的类，这个过程叫**实例化**。

```cpp
template <typename T>
T max_of(T a, T b) { return a < b ? b : a; }    // 函数模板

template <typename T>
class Box {                                      // 类模板
public:
    explicit Box(T v) : v_(std::move(v)) {}
    const T& get() const { return v_; }
private:
    T v_;
};

max_of(3, 7);              // 实例化 max_of<int>
Box<std::string> b("hi");  // 实例化 Box<std::string>
```

**为什么模板定义常放头文件？** 实例化发生在**使用处的编译单元**：编译 `a.cpp` 时编译器要看到模板的完整定义，才能为 `int` 生成代码。如果定义只写在 `queue.cpp`，`a.cpp` 只看到声明，就会在链接时报 `undefined reference`。所以模板通常整个写在 `.h` 里（替代办法是在 .cpp 里对已知类型做显式实例化，但就丢了通用性）。

可以用命令看实例化出了哪些符号：
```bash
g++ -std=c++20 -c main.cpp -o main.o && nm -C main.o | grep max_of
# 会看到 int max_of<int>(int, int) 之类的符号
```

**类模板的成员函数是按需实例化的**，这一点下面讲误区时会用到。

### 2. 推导、特化与依赖名

**模板实参推导**：调用 `max_of(3, 7)` 时编译器从实参推出 `T=int`。`max_of(3, 7.0)` 会推导冲突（int vs double）而失败，需要写 `max_of<double>(3, 7.0)`。C++17 起类模板也能推导：`std::vector v{1,2,3};`。

**特化**：给某个具体类型提供一份"定制版模具"。
```cpp
template <typename T> std::string kind(const T&) { return "generic"; }
template <> std::string kind<int>(const int&) { return "int-specialization"; } // 显式特化
std::string kind(const char*) { return "overload-cstr"; }                      // 普通重载

kind(1.5);   // generic
kind(42);    // int-specialization
kind("x");   // overload-cstr
```

**重载 vs 特化，怎么区分**：
- **重载**是多个独立的函数（或函数模板）名字相同，参与**重载决议**，编译器挑最匹配的那个；非模板函数在同等匹配时优先。
- **特化**不是新的候选者，它只是某个主模板在特定类型上的**替换实现**：先由重载决议选中主模板，再看有没有对应特化可用。
- 所以给函数"加特殊情况"时通常优先写重载；类模板没法重载，只能用特化（全特化/偏特化），比如 `std::hash<MyKey>`。

**依赖名**：模板里依赖 `T` 的名字，编译器在看到具体 `T` 之前不知道它是类型还是值，需要你用 `typename` 说明：
```cpp
template <typename C>
typename C::value_type first_or(const C& c, typename C::value_type d) {
    return c.empty() ? d : *c.begin();
}
```
不写 `typename`，`C::value_type` 会被当成一个值，导致编译错误。同理，调用依赖类型的成员模板时要写 `obj.template foo<int>()`。

### 3. SFINAE 与 Concept 约束

问题：模板对 `T` 是"默认什么都能接"，传错类型时错误会在模板深处爆出来。约束就是**把要求写在接口上**。

**SFINAE**（替换失败不是错误）：推导时如果替换出非法类型，这个候选只是被丢掉，而不算报错。标准库用 `enable_if` 利用这一点：
```cpp
template <typename T, std::enable_if_t<std::is_integral_v<T>, int> = 0>
T twice(T x) { return x * 2; }
twice(21);    // OK
// twice(1.5);  // 没有匹配函数：double 的候选被"静默丢弃"
```
缺点：写法晦涩，报错信息是"no matching function"，看不出到底缺什么。

**Concept（C++20）**：把要求写成有名字的谓词，报错直接指出哪条约束不满足。
```cpp
template <typename T>
concept Comparable = requires(const T& a, const T& b) {
    { a < b } -> std::convertible_to<bool>;
};

template <typename T> requires Comparable<T>
class BoundedQueue { /* ... */ };
```
传入没有 `operator<` 的类型时，g++ 会给出 `constraints not satisfied` 和 `required for the satisfaction of 'Comparable<T>' [with T = Msg]`，一眼能看出原因（本课实践里实测过）。

**源码阅读建议**（节点给出的方向）：阅读标准库 `enable_if`、`void_t` 和 concepts 的实现，区分哪些是**语言规则**（SFINAE、requires 子句），哪些是**库技巧**（`enable_if` 只是一个在条件为假时不定义 `type` 的普通结构体模板）。在 box 上可以直接看：
```bash
grep -n "struct enable_if" /usr/include/c++/14/type_traits
grep -n "concept totally_ordered" /usr/include/c++/14/concepts
```

## 三、常见误区与设计权衡

**误区（节点原文）：模板代码未被实例化时可能未触发所有错误。**
类模板的成员函数只有被调用才实例化。下面这个无约束队列，`Q<Msg>` 照样能编译，直到某天有人调了 `max()` 才在 `std::max_element` 深处报一长串错误：
```cpp
template <typename T> struct Q {
    std::deque<T> q;
    void push(T v) { q.push_back(v); }
    T max() const { return *std::max_element(q.begin(), q.end()); }
};
struct Msg { int id; };
Q<Msg> q; q.push({1});   // 能编译！max() 从未实例化
```
后果：模板库自己的单测如果没覆盖每个成员函数、每类类型，bug 会潜伏到下游业务才暴露。对策是写 concept 约束，再加上 `static_assert` 和对各成员都调用一遍的测试。

**权衡（节点原文）：泛型减少重复，过度元编程会增加编译时间与维护难度。**
- 好处：一份 `BoundedQueue<T>` 同时服务消息、任务、openid 字符串，没有类型擦除带来的运行时开销。
- 代价：每种 T 都生成一份代码（编译慢、二进制变大）；层层模板元编程让同事看不懂、报错难读。大型后台项目里编译时间本身就是成本。
- 经验：能用普通函数/重载解决就不要上模板；需要泛型时优先用 concept 写清约束，而不是叠 SFINAE 技巧。

## 四、动手实践：可比较类型的有界队列 + 清晰编译错误

目标（节点原文）：实现支持可比较类型的有界队列接口，为无效类型提供清晰编译错误。

1. **建目录**：`mkdir -p ~/practice25 && cd ~/practice25`
2. **写约束**：在 `bounded_queue.h` 里定义 `concept Comparable`（要求 `a < b` 可转为 bool）。
3. **写类模板**：`template <typename T> requires Comparable<T> && std::movable<T> class BoundedQueue`，内部用 `std::deque<T>`，提供：
   - `bool push(T v)`：满了返回 false，不阻塞；
   - `std::optional<T> pop()`：空时返回 `nullopt`；
   - `std::optional<T> max() const`：用 `std::max_element`，空时返回 `nullopt`；
   - `size()`、`capacity()`。
4. **正向测试**（main.cpp）：容量 3 放 5/9/1，第 4 个 push 返回 false；`max()==9`；`pop()==5`；`BoundedQueue<std::string>` 能按字典序取最大；空队列 `pop/max` 都是空；`static_assert(Comparable<int> && !Comparable<Msg>)`。
5. **负向测试**（bad.cpp）：`struct Msg { int id; };` 后写 `BoundedQueue<Msg> q(4);`，确认**编译失败**且错误里出现 `Comparable`。
6. **对照实验**（lazy.cpp）：写一个无约束版本，确认不调 `max()` 时能编译、调用后在 STL 深处报错，体会"未实例化不报错"。
7. **编译运行**：
   ```bash
   g++ -std=c++20 -Wall -Wextra -fsanitize=address,undefined -g main.cpp -o main && ./main
   g++ -std=c++20 -c bad.cpp -o /dev/null   # 预期失败，看报错是否清晰
   ```

参考实现已在我的环境里跑通，`test.sh` 实测输出：
```
ALL_TESTS_PASSED
BAD_TYPE_REJECTED_WITH_CONCEPT_MESSAGE
bad.cpp:3:30: note: constraints not satisfied
bounded_queue.h:10:9:   required for the satisfaction of 'Comparable<T>' [with T = Msg]
UNCONSTRAINED_COMPILES_UNTIL_MAX_USED
CALL_MAX_FAILS_DEEP_IN_STL
```
建议你先自己写，再对照附件中的参考代码。

## 五、自测题

**1. 把类模板 `BoundedQueue<T>` 的成员函数定义挪到 `bounded_queue.cpp`，只在头文件留声明，`main.cpp` 用 `BoundedQueue<int>`，会发生什么？为什么？**
<details><summary>答案</summary>
通常编译通过、链接报 undefined reference。模板在使用处的编译单元实例化：编译 main.cpp 时看不到成员定义，无法生成 int 版本；而编译 bounded_queue.cpp 时又没人用 int 实例化它。解决：定义放头文件，或在 .cpp 里显式实例化 `template class BoundedQueue<int>;`（只对列出的类型有效）。
</details>

**2. 有 `template<typename T> void f(T)`、它的显式特化 `template<> void f<int>(int)`，以及普通函数 `void f(int)`。调用 `f(1)` 用哪个？重载和特化的本质区别是什么？**
<details><summary>答案</summary>
调用普通函数 `void f(int)`。重载决议只在"普通函数和主模板"之间选，同等匹配下非模板优先；特化不参与重载决议，只在主模板被选中后才替换实现。所以重载是新增候选，特化是给已有模板换某个类型的实现。
</details>

**3. 一个无约束的类模板 `Q<T>` 里有用到 `<` 的 `max()`，团队用 `Q<Msg>`（Msg 没有 `operator<`）上线了半年都没报错，某天新代码调了 `q.max()` 才编译失败。解释原因，并说出两种让问题更早暴露的办法。**
<details><summary>答案</summary>
类模板成员函数按需实例化，`max()` 之前从未被调用，所以对 Msg 来说从没被生成，错误也没触发（"未实例化时可能未触发所有错误"）。办法：① 用 concept/requires 约束 T（如 Comparable），在 `Q<Msg>` 实例化类时就报清晰错误；② 在模板库测试里对每个成员函数都调用，并用 `static_assert` 检查关键类型满足约束。
</details>

## 六、推荐资源

- [cppreference：Templates](https://en.cppreference.com/w/cpp/language/templates.html)（英文文档，进阶，免费）：重点读函数与类模板，以及推导、特化与依赖名，用来核对本节实验的语义和边界。
