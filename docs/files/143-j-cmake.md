# 第 143/208 课：CMake 目标式构建

> 序号 143/208 · id `j-cmake` · 领域 J · 阶段 1 · 难度 基础 · 子类 构建与变更管理
> 前置：[`j-git`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/142.html)、[`a-compile`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/6.html)
> 节点摘要：以 target 表达源文件、依赖和使用要求，避免全局 include 与链接参数污染。

## 导读

上一课讲 Git 时留下一个问题：构建产物为什么不该进仓库。这一课给出答案的另一半——如果构建目录可以随时删掉、在任何机器上从源码重新生成，那它就没有入库的必要。CMake 的目标式写法正是为了让"从干净目录重新构建"这件事稳定可复现。

教学假设场景：你所在的后台组有一个公共的配置解析库 `kvutil`，被一个 C++ 后台服务 `server` 和它的单元测试 `kvutil_test` 同时使用。新同事拉下代码，在自己的开发机上执行两条命令就应该能编过，并且服务的 CMakeLists 里不需要手工写一行 `-I../kvutil/include`。

## 学习目标

1. 解释 `target_include_directories` 做了什么，以及它的适用边界（什么头路径该挂在哪个 target 上）。
2. 说清 PUBLIC / PRIVATE / INTERFACE 三个作用域关键字如何决定属性向消费者传播。
3. 独立完成实践：把工具库、服务与测试拆成三个 target，在干净的 out-of-source 目录构建，并输出 `compile_commands.json`。

## 为什么后端要懂

节点给出的理由是"让 C++ 工程跨机器可复现"。在后台开发里这意味着三件事：CI 机器、同事的开发机和发布构建机得到同样的编译命令；一个库的内部头文件和内部宏不会悄悄泄漏给几十个依赖它的服务；IDE 和 clangd 能通过 `compile_commands.json` 拿到每个文件真实的编译参数，跳转和补全才准确。旧式写法用 `include_directories()`、`add_definitions()` 这类目录级全局命令，所有 target 共享一套参数，项目一大就分不清某个 `-I` 是谁需要的。

## 核心知识点

### 1. target_include_directories

**直觉。** 头文件搜索路径不是"整个工程的设置"，而是"某个 target 编译时需要、或者用它的人编译时需要"的属性。把它挂在 target 上，路径就跟着 target 走。

**原理。** CMake 的每个 target 有两组属性：自己编译时用的（如 `INCLUDE_DIRECTORIES`），以及向链接它的 target 提供的使用要求（如 `INTERFACE_INCLUDE_DIRECTORIES`）。`target_include_directories` 根据作用域关键字把路径写进其中一组或两组。本课实验的库是这样声明的：

```cmake
add_library(kvutil STATIC src/kvutil/kv_parse.cpp)
target_include_directories(kvutil
  PUBLIC  ${CMAKE_CURRENT_SOURCE_DIR}/include
  PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src/kvutil)
target_compile_definitions(kvutil PRIVATE KVUTIL_INTERNAL=1)
```

`include/kvutil/kv.h` 是对外接口（声明 `kvutil::parse`），`src/kvutil/trim.h` 是库内部工具。服务只写一行 `target_link_libraries(server PRIVATE kvutil project_warnings)`，不写任何 include 路径。实验生成的 `compile_commands.json` 证明了效果：

```text
条目数= 3
src/kvutil/kv_parse.cpp: includes=['-I<src>/include', '-I<src>/src/kvutil'] flags=['-DKVUTIL_INTERNAL=1', '-Wall', '-Wextra']
src/server/main.cpp: includes=['-I<src>/include'] flags=['-Wall', '-Wextra']
tests/kvutil_test.cpp: includes=['-I<src>/include'] flags=['-Wall', '-Wextra']
```

库自己拿到两条路径和内部宏；服务和测试只拿到公共头路径。

**适用边界。** 对外头文件所在目录用 PUBLIC（或 INTERFACE），实现细节目录用 PRIVATE。不要用它去指向另一个库的头目录——那应当通过 `target_link_libraries` 链接那个库，由它自己声明的使用要求带过来。另外，`target_include_directories` 只解决"头文件在哪里找"，不解决链接；链接关系由 `target_link_libraries` 表达，两者合在一起才是完整的依赖。

### 2. PUBLIC / PRIVATE / INTERFACE

**直觉。** 关键字回答的是"这个属性给谁用"：

| 关键字 | 拥有者自己用 | 链接它的消费者用 |
|---|---|---|
| PRIVATE | 是 | 否 |
| INTERFACE | 否 | 是 |
| PUBLIC | 是 | 是 |

**原理。** 依赖传播沿着 `target_link_libraries` 的边进行。本实验的依赖图如下：

```text
          project_warnings (INTERFACE 库，无源文件)
           INTERFACE: -Wall -Wextra
          /        |          \
   PRIVATE      PRIVATE      PRIVATE
        |          |            |
     kvutil ---> server      kvutil_test
       |  PUBLIC  include/  ---> server, kvutil_test 都拿到
       |  PRIVATE src/kvutil/, KVUTIL_INTERNAL=1  ---> 只给 kvutil
```

`project_warnings` 是一个纯 INTERFACE 库：`add_library(project_warnings INTERFACE)` 加 `target_compile_options(project_warnings INTERFACE -Wall -Wextra)`。它不编译任何东西，只携带使用要求，谁链接它谁就得到这两个警告选项——上面三个条目都有 `-Wall -Wextra` 就是这么来的。

实验还做了两个反例来证明边界是被强制执行的，而不是约定：

- 把库的公共 include 改成 PRIVATE（`14:  PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include`），服务立刻编不过：`main.cpp:2:10: fatal error: 'kvutil/kv.h' file not found`，`build_bad exit=1`。
- 让服务去 include 库的内部头 `trim.h`：`main.cpp:3:10: fatal error: 'trim.h' file not found`，`build_leak exit=1`。

第二个反例是目标式构建的价值所在：内部头天然对外不可见，消费者想偷用都编不过，库作者以后改 `trim.h` 不会误伤别人。

**传播的链式效应。** 如果 A 用 PUBLIC 链接 B，A 的消费者也会拿到 B 的使用要求；如果是 PRIVATE，就到 A 为止。经验规则：库的公共头文件里 #include 了某个依赖的头，就用 PUBLIC 链接那个依赖；只在 .cpp 里用，就用 PRIVATE。

### 3. out-of-source build

**直觉。** 源码树只放人写的东西，构建产物全部放在另一个目录。删掉构建目录等于"清理"，不会误删源码。

**原理。** `cmake -S <源码目录> -B <构建目录>` 把 CMakeCache.txt、CMakeFiles/、build.ninja、目标文件和可执行文件都写进构建目录。实验在一个全新临时目录中执行：

```bash
cmake -S src -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build
ctest --test-dir build --output-on-failure
```

构建输出共 6 步：

```text
[1/6] Building CXX object CMakeFiles/kvutil.dir/src/kvutil/kv_parse.cpp.o
[2/6] Linking CXX static library libkvutil.a
[3/6] Building CXX object CMakeFiles/kvutil_test.dir/tests/kvutil_test.cpp.o
[4/6] Building CXX object CMakeFiles/server.dir/src/server/main.cpp.o
[5/6] Linking CXX executable kvutil_test
[6/6] Linking CXX executable server
```

检查项"源码树未被污染"通过：`src/` 下没有 CMakeCache.txt 和 CMakeFiles。同一份源码可以并存多个构建目录（如 `build-debug`、`build-asan`），互不干扰。

`compile_commands.json` 由 `set(CMAKE_EXPORT_COMPILE_COMMANDS ON)` 打开，也生成在构建目录里（Makefile 和 Ninja 生成器支持）。clangd 等工具通常通过软链接或配置指向它，而不是把它复制进源码树。

增量构建也是构建目录的职责：实验最后只改了 `src/server/main.cpp`，再次构建只执行了 `rebuilt steps=2`（重新编译 main.cpp 和重新链接 server），库和测试没有重编。

## 常见误区

1. **把生成目录提交到仓库（节点 pitfalls）。** CMakeCache.txt 里写死了本机的编译器绝对路径和缓存变量，换一台机器就是错的；目标文件和可执行文件体积大且每次构建都变。实验里 `.gitignore` 只有一行 `build*/`，把整个构建目录复制进源码树后 `git status --porcelain` 输出为空，仓库只跟踪 7 个文件。
2. **用全局 `include_directories()` 图省事。** 所有 target 都拿到所有路径，内部头的保护就消失了，上面第二个反例会"意外地"编过。
3. **公共头路径写成 PRIVATE。** 库自己能编，消费者编不过，表现为"在库目录里好好的，一被别人用就找不到头文件"，正是反例一。
4. **在源码目录直接 `cmake .`。** 生成物和源码混在一起，清理时容易误删，也无法并存多种构建配置。
5. **把 `compile_commands.json` 当成源码提交。** 它里面是本机绝对路径（实验输出中被替换成了 `<src>` 才便于展示），属于构建产物。

## 设计权衡

节点给出的权衡是：封装目标提升复用，但需明确依赖传播边界。

- **收益。** 实验中 `server` 和 `kvutil_test` 各自只写一行链接就拿到正确的头路径和警告选项；库作者通过 PRIVATE 把 `trim.h` 和 `KVUTIL_INTERNAL` 留在内部，反例二证明消费者无法依赖它们。
- **代价。** 每个属性都要想清楚作用域。写成 PUBLIC 最"省事"，但会把实现细节传播给所有下游，扩大重编范围并制造隐式依赖；写成 PRIVATE 过度，则像反例一那样让消费者编不过。INTERFACE 库（如 `project_warnings`）便于统一策略，但它的选项会作用于所有链接它的 target，加一个 `-Werror` 可能让全工程同时报错。
- **判断方法。** 问一句"消费者的编译命令里需要这一项吗"：需要就 PUBLIC/INTERFACE，不需要就 PRIVATE。

## 动手实践

节点实践：把工具库、服务与测试拆成三个 target，输出 compile_commands.json。实践包是 `practice143.tar.gz`，解压后运行：

```bash
cd practice143
bash test.sh
```

环境说明：本次运行的机器上没有 g++，因此实验用 pip 装在虚拟环境里的 cmake 4.4.4、ninja 1.13.2 和 ziglang 0.17.1 提供的 `zig c++`（clang 前端）作编译器，通过 `CXX` 环境变量指定。在你自己的机器上用系统的 g++ 或 clang++ 即可，把 `test.sh` 开头的 `PATH`/`CXX` 两行改掉，其余命令不变。

步骤与本次结果：

1. **读项目结构。** `proj/` 下有 `CMakeLists.txt`、对外头 `include/kvutil/kv.h`、实现 `src/kvutil/kv_parse.cpp`、内部头 `src/kvutil/trim.h`、服务 `src/server/main.cpp`、测试 `tests/kvutil_test.cpp`、`.gitignore`。先在纸上画出 target 依赖图，再对照上文的图。
2. **干净目录构建（验收 1：干净目录能构建）。** 脚本把 `proj` 复制到全新临时目录后执行 configure 和 build，6 步完成。服务输出 `server: timeout_ms=400 retry=1`，传参 `"timeout_ms=300;retry=2"` 时输出 `server: timeout_ms=300 retry=2`。
3. **跑测试 target。** `ctest` 结果为 `100% tests passed out of 1`，测试程序打印 4 条断言通过。
4. **检查 compile_commands.json（验收 2：库消费者无需手工补充公共头路径）。** 共 3 个条目；`main.cpp` 与 `kvutil_test.cpp` 只有 `-I<src>/include`，没有 `src/kvutil` 和 `KVUTIL_INTERNAL`。
5. **做两个反例。** PRIVATE 化公共头路径与消费者引用内部头，两次构建都以 exit=1 失败，报错见上文。
6. **验证生成目录不入库。** `.gitignore` 生效，`git status --porcelain` 为空。
7. **增量构建。** 改一个服务源文件，只重建 2 步。

最后一行应为 `ALL_CHECKS_PASSED`，并生成 `results.json`。延伸练习：把 `kvutil` 改成 `SHARED`，观察 `compile_commands.json` 有何变化；或者把三个 target 拆进 `add_subdirectory` 子目录。

## 源码与文档阅读

节点建议阅读 CMake Tutorial。2026-11-14 北京时间 16:15 核对：索引页返回 200，标题为 "CMake Tutorial — CMake 4.5.0 Documentation"，目录从 Step 0 到 Step 11。与本课最相关的是 Step 1 Getting Started with CMake（Building a Library、Linking Together Libraries and Executables 等练习）、Step 4 In-Depth CMake Target Commands（Exercise 3 - Include and Link Directories）、Step 5 In-Depth CMake Library Concepts（Exercise 2 - Interface Libraries）和 Step 8 Testing and CTest。

Step 1 页面中值得记下的几点：

- 文档把头文件定义为使用要求（usage requirement）而不是构建要求，这正是 PUBLIC 头路径的概念基础。
- 三个作用域的定义：PRIVATE 属性只对拥有它的 target 可用；INTERFACE 属性只对链接它的 target 可用，拥有者自己用不到，header-only 库就是 INTERFACE 属性集合的例子；PUBLIC 不是独立的一类，等于 PRIVATE 加 INTERFACE。

与本课实验的差异：

1. Tutorial 的 Step 1 用 `target_sources(MyLibrary PRIVATE ... PUBLIC FILE_SET myHeaders TYPE HEADERS BASE_DIRS include FILES ...)` 描述公共头文件，由 FILE_SET 的 BASE_DIRS 推出头搜索路径；本课实验用更常见的 `target_include_directories(... PUBLIC include)`。两种方式对消费者的效果相同，FILE_SET 还把头文件本身登记为 target 的一部分，便于安装。
2. 文档版本是 4.5.0，实验用 cmake 4.4.4。
3. 实验额外用了一个 INTERFACE 库承载警告选项，并用反例验证了作用域边界，Tutorial 在 Step 5 才系统讲 Interface Libraries。

## 自测题

**1. 库 `kvutil` 的公共头 `kv.h` 里 `#include <map>`，实现文件用了一个只在 .cpp 里出现的第三方库 `fmt`。`kvutil` 链接 `fmt` 应该用 PUBLIC 还是 PRIVATE？**

答：PRIVATE。消费者只 include `kv.h`，而 `kv.h` 并不包含 `fmt` 的头，所以消费者编译时不需要 `fmt` 的头路径。若某天 `kv.h` 的接口里出现了 `fmt` 的类型，就要改成 PUBLIC。（静态库的链接依赖本身仍会被 CMake 传递到最终可执行文件，这与编译期使用要求是两回事。）

**2. 实验的 `compile_commands.json` 里，为什么 `kvutil_test.cpp` 有 `-Wall -Wextra` 却没有 `-DKVUTIL_INTERNAL=1`？**

答：`-Wall -Wextra` 来自 `project_warnings` 的 INTERFACE 编译选项，测试 target 直接链接了它；`KVUTIL_INTERNAL=1` 是 `kvutil` 用 PRIVATE 声明的宏，只作用于 `kvutil` 自己，不随链接传给测试。

**3. 同事说"我把 build/ 提交了，这样别人不用再编译"。用本课内容说明问题。**

答：构建目录里的 CMakeCache.txt 和 `compile_commands.json` 含本机绝对路径和本机编译器选择，换机器即失效；目标文件随每次构建变化，会污染 diff 和仓库体积。正确做法是 out-of-source 构建加 `.gitignore` 忽略 `build*/`，让任何人用 `cmake -S . -B build && cmake --build build` 从干净目录复现，实验里这两步加测试全部通过。

## 推荐资源

- [CMake — Tutorial](https://cmake.org/cmake/help/latest/guide/tutorial/index.html)（CMake 官方文档，英文，入门，免费）：节点推荐理由是用于核对 target_include_directories 与 PUBLIC/PRIVATE/INTERFACE，将结论落实到本节点实验。
- 文中核对的 Step 1 页面：[Getting Started with CMake](https://cmake.org/cmake/help/latest/guide/tutorial/Getting%20Started%20with%20CMake.html)，三个作用域关键字的定义在该页介绍 target_link_libraries 的部分。

## 本课小结

目标式构建把"谁需要什么"写在 target 上：`target_include_directories` 声明头路径，PUBLIC/PRIVATE/INTERFACE 决定它是否沿 `target_link_libraries` 传给消费者，out-of-source 构建让产物与源码分离、可随时重建。本课实验用三个 target 加一个 INTERFACE 库，在干净目录 6 步构建通过，消费者自动拿到公共头路径，两个反例证明内部头和错误作用域会在编译期暴露。下一课是第 144 课 j-bazel（Bazel 与可复现依赖）。
