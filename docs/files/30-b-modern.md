# 第 30/208 课：C++17/20/23工程选型

> 领域 B · 阶段 3 · 泛型与标准库 · 难度：进阶
> 前置：第 25 课（b-template 模板与 Concepts）、第 27 课（b-exception 异常与错误处理）

## 一、学习目标与为什么后端要懂

节点一句话：现代标准提供结构化绑定、variant、Concepts、ranges 和 expected 等能力，**采用时需要检查编译器与标准库支持**。

学完这一课你应该能：

1. 解释 C++17 的 `optional` 与 `variant` 分别解决什么问题。
2. 独立完成：为一个小库分别配置 C++17 和 C++23 构建，并记录一个新特性的替代实现。

验收标准（来自节点）：

- 能区分「语言特性」和「标准库可用性」。
- 采用新特性时有**可复现的编译器版本**。

为什么后端要懂：线上服务的代码要在公司统一的编译镜像、CI 和部署环境里构建，不是在你的笔记本上。选一个新特性，本质是在**工程收益、工具链和兼容性**之间做取舍。像公众号后台这种存量大、模块多的系统，一个库把 `-std` 提到 C++23，所有依赖它的服务都要跟着换编译器，这就是选型要慎重的原因。

## 二、从直觉到原理

### 2.1 C++17：optional 与 variant

**直觉**：以前表示「可能没有值」靠特殊值（-1、空字符串、空指针），表示「几种类型之一」靠 union 加一个手写的 type 字段。两种做法都容易漏判。

- `std::optional<T>`：要么有一个 T，要么什么都没有（`std::nullopt`），类型本身就写着「可能为空」。
- `std::variant<A, B, ...>`：类型安全的 union，同一时刻只持有其中一种，配合 `std::visit` 处理每种情况。
- 结构化绑定 `auto [k, v] = ...`：把 pair/struct 拆成有名字的变量。

后台场景：按 uid 查昵称可能查不到；一条消息可能是文本，也可能是图片。

```cpp
#include <optional>
#include <variant>
#include <string>
#include <map>
#include <cstdio>
std::optional<std::string> find_nick(const std::map<int,std::string>& m, int uid) {
  if (auto it = m.find(uid); it != m.end()) return it->second;   // C++17 if 初始化
  return std::nullopt;
}
struct Text { std::string content; };
struct Image { std::string media_id; int width; };
using Msg = std::variant<Text, Image>;
template <class... F> struct overloaded : F... { using F::operator()...; };
template <class... F> overloaded(F...) -> overloaded<F...>;
int main() {
  std::map<int,std::string> m{{1001,"alice"}};
  for (int uid : {1001, 1002}) {
    auto n = find_nick(m, uid);
    std::printf("%d -> %s\n", uid, n.value_or("<none>").c_str());
  }
  for (const auto& [uid, nick] : m) std::printf("binding %d=%s\n", uid, nick.c_str());
  Msg msgs[] = {Text{"hi"}, Image{"m_123", 640}};
  for (auto& msg : msgs)
    std::visit(overloaded{
      [](const Text& t){ std::printf("text: %s\n", t.content.c_str()); },
      [](const Image& i){ std::printf("image: %s w=%d\n", i.media_id.c_str(), i.width); }
    }, msg);
}
```

```bash
g++ -std=c++17 -Wall s17.cpp -o s17 && ./s17
# 1001 -> alice
# 1002 -> <none>
# binding 1001=alice
# text: hi
# image: m_123 w=640
```

**原理**：两者都是值语义，数据直接存在对象内部（不额外堆分配）。`variant` 加一种新类型后，如果 `visit` 漏写分支，编译就会报错，这比 `switch(type)` 漏 case 安全得多。注意 `optional` 只能说明「没有」，说不出「为什么没有」，这正是后面 `expected` 补上的。

### 2.2 C++20：Concepts 与 ranges

**Concepts**（第 25 课讲过）：给模板参数写约束，用错类型时报错直接说「约束不满足」，而不是几百行模板展开。

**ranges**：把「过滤、变换」写成管道，视图是惰性的，不产生中间容器。

```cpp
#include <concepts>
#include <ranges>
#include <vector>
#include <cstdio>
template <std::integral T>
T clamp_qps(T v, T lo, T hi) { return v < lo ? lo : (v > hi ? hi : v); }
int main() {
  std::vector<int> latency_ms{12, 250, 8, 900, 40, 1300};
  auto slow = latency_ms | std::views::filter([](int x){ return x > 200; })
                         | std::views::transform([](int x){ return x / 100; });
  for (int x : slow) std::printf("slow bucket %d\n", x);   // 2 9 13
  std::printf("clamp=%d\n", clamp_qps(5000, 1, 1000));     // 1000
  // clamp_qps(1.5, 1.0, 2.0); // 编译失败：double 不满足 std::integral
}
```

实测（g++ 14.2）：

- `-std=c++20` 正常运行；
- 放开最后一行，报 `constraints not satisfied ... integral<T> [with T = double]`；
- 同一文件用 `-std=c++17` 编译，报 `'std::integral' has not been declared`。

**原理**：Concepts 是**语言特性**（编译器前端要支持 `requires`/约束语法），`<concepts>`、`<ranges>` 是**标准库**（由 libstdc++/libc++ 提供）。两者是分开实现、分开发版的，这就是验收标准里「区分语言特性和标准库可用性」的意思。另外，ranges 视图是借用底层容器的（回忆第 29 课的非拥有视图），容器先销毁的话视图会悬垂。

### 2.3 C++23：expected 与支持矩阵

`std::expected<T, E>`：要么是成功值 T，要么是错误 E。它就是「带原因的 optional」，适合第 27 课讲的「预期内失败」（配置非法、参数解析失败），不必抛异常。

```cpp
#include <expected>
#include <string>
#include <cstdio>
enum class Err { Empty, NotNumber, OutOfRange };
std::expected<int, Err> parse_timeout(const std::string& s) {
  if (s.empty()) return std::unexpected(Err::Empty);
  int v = 0;
  for (char c : s) {
    if (c < '0' || c > '9') return std::unexpected(Err::NotNumber);
    v = v * 10 + (c - '0');
    if (v > 60000) return std::unexpected(Err::OutOfRange);
  }
  return v;
}
int main() {
  for (std::string s : {"3000", "", "3s", "99999"}) {
    auto r = parse_timeout(s);
    if (r) std::printf("'%s' ok %d\n", s.c_str(), *r);
    else   std::printf("'%s' err %d\n", s.c_str(), static_cast<int>(r.error()));
  }
  auto doubled = parse_timeout("1500").transform([](int v){ return v * 2; });
  std::printf("doubled=%d\n", doubled.value_or(-1));   // 3000
}
```

`-std=c++23` 下输出 `3000 ok / err 0 / err 1 / err 2 / doubled=3000`；换成 `-std=c++20` 则报 `'expected' in namespace 'std' does not name a template type`。

**支持矩阵**：标准发布了，不等于你的编译器已经实现。判断方法有三层：

1. 查 [cppreference 编译器支持表](https://en.cppreference.com/w/cpp/compiler_support.html)，按特性看各编译器从哪个版本开始支持；
2. 在自己的工具链上用**特性测试宏**实测（`<version>` 头里的 `__cpp_lib_*` 是库特性，`__cpp_*` 是语言特性）；
3. 把结论（编译器版本、`-std`、宏的值）写进构建说明，保证可复现。

```cpp
#include <version>
#include <cstdio>
int main(){
#ifdef __cpp_lib_expected
  printf("expected %ld\n", (long)__cpp_lib_expected);
#endif
#ifdef __cpp_lib_ranges
  printf("ranges %ld\n", (long)__cpp_lib_ranges);
#endif
#ifdef __cpp_concepts
  printf("concepts %ld\n", (long)__cpp_concepts);
#endif
  printf("cplusplus %ld\n", (long)__cplusplus);
}
```

在本机 g++ 14.2.0 上实测：

| -std | `__cplusplus` | `__cpp_concepts` | `__cpp_lib_ranges` | `__cpp_lib_expected` |
|---|---|---|---|---|
| c++17 | 201703 | 无 | 无 | 无 |
| c++20 | 202002 | 202002 | 202110 | 无 |
| c++23 | 202302 | 202002 | 202302 | 202211 |

可以看到同一个编译器、同一份 ranges，在不同 `-std` 下宏的值也不同。这张表只代表这一个工具链，换了编译器或版本就要重新测。

## 三、常见误区与设计权衡

**误区（节点原话）：标准发布不意味着所有工具链完整实现。**

- 「C++23 已经发布，我就能用 `std::expected`」：要看编译器和标准库版本，还要看线上构建镜像用的是哪一版。
- 「编译器认识 `-std=c++23` 就等于全部支持」：开关被接受，只代表编译器知道这个标准名，具体特性仍要逐个查表或用宏检测。
- 「语言支持了，库就支持了」：Concepts 语法和 `<concepts>`/`<ranges>` 头是两回事，用 clang 搭配不同版本的 libstdc++ 时尤其容易对不上。
- 「本机能编过就行」：没有记录编译器版本，别人和 CI 就复现不了。

**权衡（节点原话）：提高最低标准可简化代码，也可能提高部署工具链成本。**

| 选择 | 好处 | 代价 |
|---|---|---|
| 最低标准停在 C++17 | 工具链要求低，老服务、老镜像都能用 | 错误处理要自己写 Result 类型，模板报错难读 |
| 提到 C++20 | Concepts 让接口约束清楚，ranges 少写循环 | 需要较新的编译器，依赖方也要跟着升级 |
| 提到 C++23 | `expected` 等新库直接可用 | 工具链成本最高，要确认所有构建环境都已支持 |
| 用宏做条件编译兼容多标准 | 新环境用新特性，老环境走替代实现 | 维护两套路径，两套都要测 |

经验做法：库对外接口尽量保守，内部用特性测试宏做可选增强；业务服务跟随团队统一的编译镜像，不要自己单独抬高标准。

## 四、动手实践：为小库分别配置 C++17 和 C++23 构建

目标：做一个解析端口号的小库，C++23 下用 `std::expected`，C++17 下用 `std::variant` 写替代实现，两种构建都通过同一组测试，并记录编译器版本。

**步骤 1：记录工具链**

```bash
g++ --version | head -1      # 本机：g++ (Debian 14.2.0-19) 14.2.0
```

**步骤 2：写带特性检测的 Result 头文件 `result.hpp`**

```cpp
#pragma once
#include <string>
#include <string_view>
#include <version>
#if defined(__cpp_lib_expected) && __cpp_lib_expected >= 202211L
  #include <expected>
  #define RESULT_IMPL "std::expected"
  template <class T> using Result = std::expected<T, std::string>;
  inline auto make_err(std::string m) { return std::unexpected(std::move(m)); }
  template <class T> bool ok(const Result<T>& r) { return r.has_value(); }
  template <class T> const T& value(const Result<T>& r) { return *r; }
  template <class T> const std::string& error(const Result<T>& r) { return r.error(); }
#else
  #include <variant>
  #define RESULT_IMPL "std::variant fallback"
  struct Err { std::string msg; };
  template <class T> using Result = std::variant<T, Err>;
  inline Err make_err(std::string m) { return Err{std::move(m)}; }
  template <class T> bool ok(const Result<T>& r) { return r.index() == 0; }
  template <class T> const T& value(const Result<T>& r) { return std::get<0>(r); }
  template <class T> const std::string& error(const Result<T>& r) { return std::get<1>(r).msg; }
#endif

inline Result<int> parse_port(std::string_view s) {
  if (s.empty()) return make_err("empty");
  int v = 0;
  for (char c : s) {
    if (c < '0' || c > '9') return make_err("not a number: " + std::string(s));
    v = v * 10 + (c - '0');
    if (v > 65535) return make_err("out of range: " + std::string(s));
  }
  if (v == 0) return make_err("port 0 not allowed");
  return v;
}
```

要点：调用方只用 `ok/value/error` 三个函数，不关心底下是 `expected` 还是 `variant`。

**步骤 3：写测试 `main.cpp`**，用例 `"8080" "" "80a" "70000" "0" "443"`，期望只有 8080 和 443 成功，最后打印 `ALL_CHECKS_PASSED`。

**步骤 4：两种标准分别构建运行**

```bash
for s in c++17 c++23; do
  g++ -std=$s -Wall -Wextra -fsanitize=address,undefined main.cpp -o port_$s && ./port_$s
done
```

实测输出：C++17 打印 `impl=std::variant fallback`，C++23 打印 `impl=std::expected`，两边结果一致，都是 `ALL_CHECKS_PASSED`。

**步骤 5：做反例**，在 `-std=c++17` 下直接写 `std::expected`，确认编译失败（`'expected' in namespace 'std' does not name a template type`）；同一文件用 `-std=c++23` 能编过。

**步骤 6：写一段记录**（这就是节点要求的「记录一个新特性的替代实现」）：

> 特性：std::expected（`__cpp_lib_expected` = 202211，g++ 14.2.0 -std=c++23 可用）。替代实现：C++17 下用 `std::variant<T, Err>`，对外接口 ok/value/error 不变。差异：替代版没有 `transform`/`and_then` 这类链式操作，也没有 `unexpected` 包装，需要自己约定错误放在第二个分支。

完整代码和一键脚本见附件 `practice30/test.sh`。

## 五、自测题

**1. `-std=c++23` 能被编译器接受，是否说明 `std::expected` 一定能用？怎么确认？**

不能。开关被接受只说明编译器认识这个标准名，具体库特性还取决于标准库版本。确认方法：查 cppreference 编译器支持表，并在自己的工具链上检查 `<version>` 里的 `__cpp_lib_expected` 是否定义，再把编译器版本写进构建记录。

**2. 查用户昵称「可能查不到」和解析配置「可能失败且要说明原因」，分别选 optional、variant 还是 expected？**

查不到昵称是正常情况，不需要原因，用 `optional<string>`。解析配置失败要告诉调用方原因（为空、不是数字、越界），C++23 用 `expected<T, E>`；只有 C++17 时，可以用 `variant<T, Err>` 做替代实现。`variant` 本身更适合「几种合法形态之一」，比如文本消息和图片消息。

**3. 一个公共库想把最低标准从 C++17 提到 C++23，需要考虑什么？**

收益是代码更简单（直接用 `expected`、Concepts 等）。代价是所有依赖这个库的服务都要换到支持 C++23 的编译器和标准库，部署工具链成本变高。比较稳妥的做法是：先确认各构建环境的实际支持情况；或者对外接口保持 C++17，内部用特性测试宏在新环境启用新特性，老环境走替代实现，两条路径都放进 CI 测试。

## 六、推荐资源

- [C++ 编译器支持表（cppreference）](https://en.cppreference.com/w/cpp/compiler_support.html)：英文文档，进阶，免费。重点阅读 C++17 的 optional 与 variant、C++20 的 Concepts 与 ranges 部分，用来核对本课实验的语义和边界。

源码研读建议（来自节点）：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。
