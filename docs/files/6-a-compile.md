# 第 6/208 课：源码到可执行文件

> 节点：`a-compile`（Compilation Pipeline）· 域：A · 子类：工具与程序执行 · 难度：入门 · 前置：`a-shell`
>
> 一句话：C++ 源码经过**预处理、编译、汇编与链接**，形成包含机器代码及元数据的程序。

## 1. 学习目标与为什么后端要懂

学完本课你应该能：

1. 解释**翻译单元**与**头文件**是什么、各自起什么作用。
2. 独立完成：分别生成预处理文件、汇编和目标文件，并用 `nm` 定位未定义符号。
3. 说明头文件与库的作用（验收标准）。
4. 亲手修复一次**重复定义**和一次**未定义引用**（验收标准）。

**为什么后端要懂**：节点给出的理由是——区分**编译错误、链接错误和运行时错误**。微信后台大量服务是 C++ 写的，你提交代码后 CI 报错，第一件事就是判断错误出在哪一层：

| 错误类型 | 典型报错 | 发生在哪 | 第一反应 |
|---|---|---|---|
| 编译错误 | `error: 'foo' was not declared in this scope` | 编译单个 .cpp | 看语法、头文件有没有 include |
| 链接错误 | `undefined reference to 'foo()'` / `multiple definition of 'foo()'` | 把多个 .o 和库拼起来 | 看哪个 .o / 库提供了符号，链接参数对不对 |
| 运行时错误 | `Segmentation fault`、`error while loading shared libraries` | 程序跑起来后 | gdb、core 文件、`ldd` |

能一眼分清这三类，排查时间能省一大半。

## 2. 从直觉到原理

### 2.1 整体流水线（直觉）

把 `g++ main.cpp -o app` 这一条命令拆开，其实是四步：

```
main.cpp ──预处理(-E)──▶ main.i ──编译(-S)──▶ main.s ──汇编(-c)──▶ main.o ──链接──▶ app
                                                                 util.o ─┘   ▲
                                                                     libxxx.so/.a
```

```bash
g++ -E main.cpp -o main.i   # 只预处理：展开 #include、宏
g++ -S main.cpp -o main.s   # 到汇编为止
g++ -c main.cpp -o main.o   # 到目标文件为止（不链接）
g++ main.o util.o -o app    # 链接
g++ -save-temps main.cpp -o app   # 一次保留全部中间文件
```

### 2.2 核心要点一：翻译单元与头文件

**直觉**：编译器一次只看**一个** .cpp 文件——它根本不知道别的 .cpp 里写了什么。

**原理**：一个 .cpp 经过预处理（把所有 `#include` 的内容原样粘贴进来、展开宏）之后的结果，就叫**翻译单元**（translation unit）。编译器只对这个单元负责。

所以头文件的作用就是：**把"声明"复制到每个需要它的翻译单元里**，告诉编译器"这个函数存在，签名是这样，具体在哪你别管"。

```cpp
// msg.h —— 只放声明
#pragma once
#include <string>
std::string FormatMsg(int uin, const std::string& text);

// msg.cpp —— 放定义
#include "msg.h"
std::string FormatMsg(int uin, const std::string& text) {
    return std::to_string(uin) + ": " + text;
}

// main.cpp
#include <iostream>
#include "msg.h"
int main() { std::cout << FormatMsg(10001, "hello") << "\n"; }
```

看看预处理有多"暴力"：

```bash
g++ -E main.cpp -o main.i
wc -l main.cpp main.i        # 几行源码可能变成几万行
grep -n "FormatMsg" main.i   # msg.h 里的声明被原样贴了进来
```

`#pragma once`（或 `#ifndef` 守卫）防止同一个头文件在**一个**翻译单元里被贴两次；但它管不到**多个**翻译单元——这正是后面 ODR 问题的根源。

**头文件 vs 库**：头文件给编译器"声明"（我能这么调用），库（`.a` 静态库 / `.so` 动态库）给链接器"实现"（真正的机器码）。缺头文件 → 编译错误；缺库 → 链接错误。

### 2.3 核心要点二：目标文件、符号与链接

**直觉**：每个 .o 是一块"拼图"，上面有凸起（我提供的函数）和凹槽（我需要别人提供的函数）。链接器负责把凹凸对上。

**原理**：目标文件（Linux 上是 ELF 格式）里有机器码，还有一张**符号表**：

```bash
g++ -c msg.cpp main.cpp
nm -C main.o | grep FormatMsg
#                  U FormatMsg[abi:cxx11](int, ...)   ← U = Undefined，我需要
nm -C msg.o | grep FormatMsg
# 0000000000000000 T FormatMsg[abi:cxx11](int, ...)   ← T = 在代码段定义，我提供
```

- `U`：本文件用到但没定义，等链接器去别处找。
- `T`：本文件在 text（代码）段里定义了。
- 链接器扫描所有 .o 和库：每个 `U` 必须恰好找到一个 `T` 来匹配。
  - 一个都找不到 → `undefined reference`
  - 找到多个强定义 → `multiple definition`

不加 `-C` 你会看到 `_Z9FormatMsgiRKNSt7__...` 这样的名字，这是 C++ 的**名字修饰**（name mangling），把参数类型编码进符号名，以支持重载。用 `c++filt` 还原：

```bash
nm msg.o | grep FormatMsg
echo _Z9FormatMsgi | c++filt
```

后台场景：你引入一个组内 SDK，头文件 include 好了，编译通过，链接报 `undefined reference to 'xxx::Client::Send(...)'`——说明**声明有了，实现没进链接**，去查构建配置（如 Makefile / Bazel / CMake）里是否依赖了对应的库，或者库的版本里根本没有这个签名（`nm -C libxxx.a | grep Send` 对比一下参数）。

运行时还能看动态库依赖：

```bash
ldd ./app                 # 列出依赖哪些 .so，"not found" 就是运行时找不到库
objdump -d main.o | less  # 反汇编看机器码
```

### 2.4 核心要点三：优化、调试信息和 ABI

**优化（`-O0/-O1/-O2/-O3`）**：编译器会内联函数、消除变量、重排指令。代码更快，但生成的机器码和源码不再一一对应。

```bash
g++ -O0 -S msg.cpp -o msg_O0.s
g++ -O2 -S msg.cpp -o msg_O2.s
wc -l msg_O0.s msg_O2.s   # 对比汇编行数和结构
```

**调试信息（`-g`）**：把"哪条机器指令对应哪行源码、变量叫什么"写进可执行文件（DWARF 段）。没有它，线上 core 文件里只能看到一堆地址。

```bash
g++ -O2 -g main.cpp msg.cpp -o app
objdump -h app | grep debug   # 能看到 .debug_info 等段
```

**ABI（应用二进制接口）**：规定函数怎么传参、类在内存里怎么排布、名字怎么修饰。只有 ABI 一致，不同编译产生的 .o / .so 才能链在一起。上面 `nm` 里出现的 `[abi:cxx11]` 就是 GCC 对 `std::string` 新旧 ABI 的区分——用新 ABI 编译的代码去链老 ABI 编的库，就会出现"明明有这个函数却 undefined reference"的怪事。

## 3. 常见误区与设计权衡

**误区（节点原文）**：把函数定义放进多处包含的头文件，可能违反 **ODR（一次定义规则）**。

```cpp
// util.h
#pragma once
int Add(int a, int b) { return a + b; }   // 定义写在了头文件里！
```

`a.cpp` 和 `b.cpp` 都 include 它 → 两个翻译单元各有一个 `T Add` → 链接时 `multiple definition of 'Add(int, int)'`。`#pragma once` 救不了你，因为它只管单个翻译单元。

修法三选一：
1. 头文件只留声明，定义挪到 `util.cpp`；
2. 加 `inline`：`inline int Add(...)`，允许多个翻译单元各有一份相同定义；
3. 类内定义的成员函数、模板默认就是这种 inline 语义。

**权衡（节点原文）**：优化可能改变调试体验，应保留**可定位**的构建。

- `-O0 -g`：好调试，但性能和线上不一样，有些 bug 复现不出来。
- `-O2`（无 `-g`）：快，但线上 core 了几乎没法定位。
- 常见折中：线上用 `-O2 -g` 构建，再把调试信息剥离单独保存（`objcopy --only-keep-debug`），出问题时配合 core 文件还原调用栈。用 gdb 时遇到 `<optimized out>` 的变量，是优化的正常代价，不是 bug。

## 4. 动手实践（节点实践：分别生成预处理文件、汇编和目标文件，用 nm 定位未定义符号）

```bash
mkdir -p ~/lab/compile && cd ~/lab/compile
```

**步骤 1：准备代码**——把 2.2 里的 `msg.h`、`msg.cpp`、`main.cpp` 写进去。

**步骤 2：逐阶段生成中间产物**

```bash
g++ -E main.cpp -o main.i && wc -l main.i
g++ -S main.cpp -o main.s && grep -n FormatMsg main.s
g++ -c main.cpp -o main.o && file main.o
```

**步骤 3：制造并定位未定义引用**

```bash
g++ main.o -o app          # 故意不带 msg.o
# → undefined reference to `FormatMsg[abi:cxx11](int, ...)'
nm -C main.o | grep ' U '  # 找到 U 的 FormatMsg
```

修复：`g++ -c msg.cpp && g++ main.o msg.o -o app && ./app`，输出 `10001: hello`。

**步骤 4：制造并修复重复定义**

```bash
cat > util.h <<'X'
#pragma once
int Add(int a, int b) { return a + b; }
X
printf '#include "util.h"\nint F(){return Add(1,2);}\n' > a.cpp
printf '#include "util.h"\nint F();\nint main(){return F()+Add(3,4)==10?0:1;}\n' > b.cpp
g++ a.cpp b.cpp -o dup     # → multiple definition of `Add(int, int)'
g++ -c a.cpp b.cpp && nm -C a.o b.o | grep Add   # 两个 T
```

修复：把 `int Add` 改成 `inline int Add`，重新 `g++ a.cpp b.cpp -o dup && ./dup; echo $?`，输出 `0`。

**步骤 5：对比优化与调试信息**

```bash
g++ -O0 -g main.cpp msg.cpp -o app_dbg
g++ -O2    main.cpp msg.cpp -o app_rel
ls -l app_dbg app_rel; objdump -h app_dbg | grep -c debug; objdump -h app_rel | grep -c debug
ldd app_rel
```

**对照验收**：
- [ ] 能用自己的话说明头文件与库的作用（声明 vs 实现，编译期 vs 链接期）
- [ ] 完成步骤 3、4，各修复一次未定义引用和重复定义

## 5. 自测题

**Q1**：`#include` 了第三方头文件，编译通过，链接报 `undefined reference to 'Foo::Bar()'`。最可能缺什么？怎么确认？

> **答**：缺的是实现（库或对应的 .o 没参与链接，或库里签名不一致）。用 `nm -C 你的.o | grep Bar` 看到 `U`，再用 `nm -C libfoo.a | grep Bar` 看库里有没有 `T` 且签名一致；有的话检查链接参数（`-L` / `-l` 和顺序）。

**Q2**：头文件已经加了 `#pragma once`，为什么还会出现 `multiple definition`？

> **答**：`#pragma once` 只保证同一个翻译单元里不重复包含。多个 .cpp 各自包含该头文件，每个翻译单元都会生成一份非 inline 的定义，链接时出现多个强符号，违反 ODR。应把定义移到 .cpp 或标记 `inline`。

**Q3**：线上服务 core 了，gdb 里调用栈全是 `??`，变量显示 `<optimized out>`。分别是什么原因，构建上该怎么改？

> **答**：`??` 通常是没有调试信息（没加 `-g` 或被剥离且没保留符号文件）；`<optimized out>` 是 `-O2` 等优化把变量消掉了，属于正常代价。构建上用 `-O2 -g`，并单独保留调试信息文件，保证线上版本可定位。

## 6. 推荐资源

- [GCC 文档：Overall Options](https://gcc.gnu.org/onlinedocs/gcc/Overall-Options.html)（gcc.gnu.org，英文，入门，免费）——重点读翻译单元与头文件、目标文件/符号与链接相关部分，用来核对本节实验里 `-E`/`-S`/`-c` 等选项的语义和边界。

> 源码研读建议（节点原文）：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。
