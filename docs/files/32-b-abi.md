# 第 32/208 课：ABI、ODR与二进制兼容

> 领域 B（C++）· 阶段 4 · 难度：深入 · 子类：底层机制与工程边界
> 前置：b-template（模板与 Concepts）、a-compile（编译与链接）

**一句话**：ABI 约定二进制层面的调用方式、对象布局和符号形式；ODR（One Definition Rule，单一定义规则）约束跨翻译单元的定义必须一致。

## 一、学习目标与为什么要懂

学完本课你应该能：

1. 解释**符号修饰（name mangling）与调用约定**；
2. 独立完成：**构造两个编译选项不一致的共享库，用 nm/readelf 和符号版本定位差异**。

验收标准：

- 能区分**源代码兼容**与 **ABI 兼容**；
- 能制定**不跨边界传 STL 对象的 C 接口方案**。

为什么后台要懂：节点给的理由是「排查升级动态库后的崩溃和链接异常」。后台服务大量依赖 `.so`：公司公共组件、RPC 框架、存储 SDK、第三方加密库。典型现场是「源码一行没改，只是换了个库版本/换了编译机，服务一启动就 core」或者「链接时报 undefined reference，可函数明明在那」。这类问题看源码看不出来，只能到二进制层面去看。

## 二、从直觉到原理

### 2.1 源码兼容 ≠ ABI 兼容

- **源码兼容（API 兼容）**：老代码拿新头文件**重新编译**还能过、语义不变。
- **ABI 兼容**：老的**已编译好的**二进制，不重编，直接和新库一起跑也正确。

例：给结构体中间加一个字段。源码上调用方完全不用改，能编过——源码兼容；但老调用方按老的 `sizeof` 和偏移去访问，新库按新布局读——ABI 不兼容，结果是读错字段或踩内存。

ABI 主要管三件事：**符号叫什么、函数怎么调用、对象在内存里长什么样**。Linux x86-64 上 C 的部分由 System V AMD64 ABI 规定，C++ 部分由 GCC/Clang 共同遵循的 Itanium C++ ABI 规定。

### 2.2 要点一：符号修饰与调用约定

C++ 允许重载和命名空间，链接器却只认一个字符串名字，所以编译器把「命名空间 + 函数名 + 参数类型」编码进符号名：

```cpp
// f.cpp
int add(int a, int b) { return a + b; }
extern "C" int add_c(int a, int b) { return a + b; }
namespace wx { struct Msg; void send(const Msg*, long); }
void wx::send(const Msg*, long) {}
```

```bash
g++ -c f.cpp
nm f.o            # 原始修饰名
nm f.o | c++filt  # 还原可读名（nm -C 也可以）
```

实测输出：

```
T _Z3addii                    -> add(int, int)
T _ZN2wx4sendEPKNS_3MsgEl     -> wx::send(wx::Msg const*, long)
T add_c                       -> add_c（extern "C" 不修饰）
```

要点：

- **返回类型一般不进符号名**（普通函数），所以只改返回类型可能链接得上但运行错。
- **参数类型进符号名**，所以 `std::string` 的 ABI 一变，所有带 `std::string` 参数的函数符号都变（见实践）。
- `extern "C"` 关掉修饰，符号就是函数名——这是做跨语言、跨编译器边界接口的基础，但它只解决「名字」，不解决「布局」。

**调用约定**规定参数放哪些寄存器、谁清理栈、返回值怎么回来、哪些寄存器调用者要保存。x86-64 Linux 上前 6 个整数参数走 `rdi, rsi, rdx, rcx, r8, r9`，返回值走 `rax`；成员函数的 `this` 作为隐藏的第一个参数。可以自己看：

```bash
g++ -O1 -S f.cpp -o - | grep -A3 '_Z3addii:'
# 能看到 leal (%rdi,%rsi), %eax：a 在 edi，b 在 esi，结果放进 eax
```

还有一个容易忽略的点：有非平凡析构/拷贝的类对象按值传参时不走寄存器，而是通过隐藏指针传递。所以给一个原本平凡的类型加上自定义析构函数，函数签名没变，ABI 却变了。

### 2.3 要点二：对象布局与虚表

对象布局 = 成员顺序 + 对齐填充 + 隐藏成员（虚表指针）+ 基类子对象位置。

```cpp
struct Base { virtual ~Base() = default; virtual int handle() { return 1; } int id = 0; };
struct Derived : Base { int handle() override { return 2; } long seq = 0; };
```

实测（g++ x86-64）：`sizeof(Base)=16`（8 字节 vptr + 4 字节 id + 4 字节填充），`sizeof(Derived)=24`，`id` 偏移是 8——因为偏移 0 被 vptr 占了。

虚函数调用在二进制里是「取 vptr → 按**槽位下标**取函数地址 → 调用」。调用方编译时就把槽位下标写死了。所以下面这些改动源码兼容、ABI 不兼容：

- 在已有虚函数**中间**插入新虚函数 → 后续虚函数槽位整体后移，老调用方调到错误函数；
- 给类加成员、改成员顺序、改成员类型 → `sizeof` 和偏移变化；
- 给原本没有虚函数的类加第一个虚函数 → 多出 vptr，所有成员偏移 +8；
- 改继承关系。

查看虚表和 typeinfo 符号：

```bash
nm -C libxxx.so | grep -E 'vtable|typeinfo'
# _ZTV = vtable，_ZTI = typeinfo，_ZTS = typeinfo name
g++ -fdump-lang-class -c layout.cpp   # GCC 输出类布局和 vtable 详情到 *.class 文件
```

### 2.4 要点三：ODR、内联与标准库 ABI

**ODR**：同一个实体在整个程序里只能有一个定义；`inline` 函数、类、模板可以在多个翻译单元各有一份定义，但**每一份必须逐 token 相同、含义相同**。违反了通常是未定义行为，编译器和链接器**不保证报错**。

演示：

```cpp
// a.cpp
inline int limit() { return 100; }
int from_a() { return limit(); }
// b.cpp
inline int limit() { return 200; }   // 违反 ODR
int from_b() { return limit(); }
```

实测：

```bash
g++ -O0 a.cpp b.cpp m.cpp -o odr && ./odr      # a=100 b=100
g++ -O0 b.cpp a.cpp m.cpp -o odr2 && ./odr2    # a=200 b=200   只换了链接顺序
g++ -O2 -flto a.cpp b.cpp m.cpp -o odr3 && ./odr3  # a=100 b=200  被内联后又不同了
```

没有任何报错，结果却随链接顺序和优化级别变化。原因是 inline 函数生成弱符号，链接器随便留一份；被内联的调用点则用的是自己那一份。后台真实场景：两个组件各自 vendoring 了不同版本的同名 header-only 库，或者同一个头文件在不同 `.so` 里用不同的宏（例如 `-DNDEBUG`、`-DUSE_FAST_PATH`）编译，让结构体或内联函数在不同地方长得不一样。

**标准库 ABI**：`std::string`、`std::list` 等的内部布局本身就是 ABI 的一部分。GCC 5 起为了满足 C++11 要求，换了 `std::string` 实现，用宏 `_GLIBCXX_USE_CXX11_ABI` 切换新旧两套，新版的符号带 `std::__cxx11::`。一个库用 `=0` 编、另一个用 `=1` 编，带 `std::string` 参数的接口就对不上（实践第 1 步会亲眼看到）。此外，`_GLIBCXX_DEBUG` 会改变容器布局，不同 `-std=` 或不同版本的 libstdc++ 也可能影响模板实例。结论是：**STL 对象不要跨你控制不了编译选项的二进制边界传递**。

### 2.5 符号版本与 SONAME

动态库用两种机制管理兼容性：

- **SONAME**（如 `libfoo.so.1`）：主版本号，ABI 不兼容时升主版本，老程序继续链接 `.so.1`；
- **符号版本**（version script）：每个符号带版本标签，如 `cfg_timeout@@CFG_1.0`。glibc、libstdc++ 都用这个机制（`GLIBC_2.2.5`、`GLIBCXX_3.4.21`），程序运行时会检查需要的版本是否存在——这就是线上常见的 `version 'GLIBCXX_3.4.29' not found` 报错来源。

```bash
readelf -d libx.so | grep -E 'SONAME|NEEDED'   # 看 SONAME 和依赖
readelf --dyn-syms -W libx.so                  # 看导出符号及版本
readelf -V app                                  # 看需要哪些版本
objdump -T libx.so                              # 另一种看动态符号的方式
```

## 三、常见误区与设计权衡

**误区（节点原文）：`extern "C"` 不自动保证任意结构布局跨平台相同。**
`extern "C"` 只是让符号名不修饰、按 C 调用约定调用。结构体布局仍取决于对齐规则、打包选项（`-fpack-struct`、`#pragma pack`）、`long` 的宽度（Linux 64 位是 8 字节，Windows 64 位是 4 字节）、`bool`/`enum` 的大小。实践第 2 步就会演示：符号完全一样、链接一切正常，但库用 `-fpack-struct=1` 编译，`sizeof` 从 16 变成 9，读出来的 `timeout_ms` 是垃圾值。

其他常见误区：

- 「能链接成功就是 ABI 兼容」——布局问题链接器根本检查不到；
- 「我只是加了个字段/加了个虚函数，没删东西」——加也会破坏 ABI；
- 「ODR 违反编译器会报错」——大多数情况下静默。

**权衡（节点原文）：Pimpl 或 C 接口降低 ABI 暴露，可能增加间接访问和封装成本。**

| 方案 | 做法 | 好处 | 代价 |
|---|---|---|---|
| C 接口 | `extern "C"` 函数 + 不透明句柄 + 只用定宽整数/指针+长度 | 跨编译器、跨语言最稳 | 失去 RAII/异常/重载，要写包装层 |
| Pimpl | 公开类只有一个指向实现的指针，成员都在 `.cpp` 里 | 加字段不改公开布局 | 多一次堆分配和指针间接访问 |
| 一起编译 | monorepo 统一工具链和编译选项、从源码构建 | 可以随意用 STL/模板 | 要求所有依赖同步构建，升级成本高 |

一个不跨边界传 STL 的 C 接口方案示例：

```c
// cfg_api.h —— 对外只暴露这些
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct cfg_handle cfg_handle;            /* 不透明句柄，布局不暴露 */
cfg_handle* cfg_open(const char* path, size_t path_len);
int64_t     cfg_get_i64(const cfg_handle*, const char* key, size_t key_len, int64_t dflt);
/* 输出字符串：调用方提供缓冲区，返回需要的长度 */
size_t      cfg_get_str(const cfg_handle*, const char* key, size_t key_len, char* buf, size_t cap);
void        cfg_close(cfg_handle*);
#ifdef __cplusplus
}
#endif
```

要点：字符串用「指针 + 长度」，不用 `std::string`；内存谁分配谁释放（`cfg_open`/`cfg_close` 成对）；异常不能穿过 C 边界，在实现里 catch 后转错误码；使用方可以再包一层只在自己编译单元内生效的 C++ RAII 包装。

## 四、动手实践：两个编译选项不一致的共享库

目标（节点原文）：构造两个编译选项不一致的共享库，使用 nm/readelf 和符号版本定位差异。

**步骤 1：标准库 ABI 不一致。**
同一份 `greet(const std::string&)`，分别用 `-D_GLIBCXX_USE_CXX11_ABI=1` 和 `=0` 编成两个 `.so`：

```bash
g++ -std=c++17 -fPIC -shared -D_GLIBCXX_USE_CXX11_ABI=1 greet.cpp -o libgreet_new.so
g++ -std=c++17 -fPIC -shared -D_GLIBCXX_USE_CXX11_ABI=0 greet.cpp -o libgreet_old.so
nm -D --defined-only libgreet_new.so | grep greet | c++filt
nm -D --defined-only libgreet_old.so | grep greet | c++filt
```

实测：新版符号是 `_Z5greetRKNSt7__cxx1112basic_string...`（`std::__cxx11::basic_string`），旧版是 `_Z5greetRKSs`。用新 ABI 编的调用方去链接旧库，报 `undefined reference to greet(std::__cxx11::basic_string<...> const&)`。**排查口诀：报错里看到 `__cxx11`，先查两边的 `_GLIBCXX_USE_CXX11_ABI`。**

**步骤 2：布局不一致（`extern "C"` 也救不了）。**
`struct Cfg { char flag; int64_t timeout_ms; };` 配 `extern "C"` 函数，一个库正常编，一个库加 `-fpack-struct=1`。

实测：

```
正常库：caller sizeof=16 lib sizeof=16 timeout=3000
打包库：caller sizeof=16 lib sizeof=9  timeout=-5188146770730811392
```

再 `diff` 两个库的 `nm -D` 输出：符号**完全相同**。结论是 nm 只能查名字层面的差异，布局差异要靠导出 `sizeof`/版本号的自检函数、`-fdump-lang-class`、或者调试信息（`pahole`、`abidiff` 这类工具）。

**步骤 3：符号版本与 SONAME。**

```bash
cat > v1.map <<'M'
CFG_1.0 { global: cfg_timeout; cfg_size; local: *; };
M
g++ -fPIC -shared cfg.cpp -Wl,--version-script=v1.map -Wl,-soname,libcfgv.so.1 -o libcfgv.so.1
readelf -d libcfgv.so.1 | grep SONAME            # Library soname: [libcfgv.so.1]
readelf --dyn-syms -W libcfgv.so.1 | grep cfg_   # cfg_timeout@@CFG_1.0
readelf -V libgreet_new.so | grep -oE '(GLIBCXX|CXXABI|GLIBC)_[0-9.]+' | sort -u
```

实测能看到 `cfg_timeout@@CFG_1.0`，以及 C++ 库依赖 `GLIBCXX_3.4.21`、`CXXABI_1.3`。`local: *` 还顺带把不想导出的符号隐藏掉了，缩小了 ABI 面。

**步骤 4：记录 ABI 边界**（对应节点的 sourceStudy）。
找一个你会用到的共享库，用 `nm -DC --defined-only` 列出导出符号，统计有多少带 `std::`，用 `nm -C | grep vtable` 看导出了哪些虚表，写下：哪些接口传了 STL 对象、哪些类的布局暴露给了调用方——这些都是升级时的风险点。

完整脚本（一键跑完上面三步并自动断言，最后打印 `ALL_CHECKS_PASSED`，已在 g++ 14 上实测通过）见文末附录。

## 五、自测题

**1. 某公共库 v2 只在 `class Session` 的中间加了一个 `std::string trace_id` 成员，头文件其他地方不变。业务方不重编、直接替换 `.so` 上线后 core 了。这算源码兼容还是 ABI 兼容问题？为什么链接/加载时没报错？**

<details><summary>答案</summary>
源码兼容（重编能过），但 ABI 不兼容。老业务代码编译时按旧的 sizeof 分配 Session、按旧偏移访问成员，新库按新布局读写，导致错位和越界。符号名只包含函数名和参数类型，不包含类的布局，所以动态链接器找得到所有符号，检查不出问题。修复：重编所有使用方，或者升 SONAME 主版本；长期改用 Pimpl/不透明句柄。
</details>

**2. 链接报错 `undefined reference to foo(std::__cxx11::basic_string<char...> const&)`，`nm -DC libfoo.so | grep foo` 能看到 `foo(std::basic_string<char...> const&)`。原因是什么、怎么定位？**

<details><summary>答案</summary>
双方的 `_GLIBCXX_USE_CXX11_ABI` 不一致：调用方用新 ABI（=1，符号带 __cxx11），库用旧 ABI（=0）编译。用 `nm -D` 对比修饰名就能确认。解决办法是统一宏值重编其中一方；根本办法是对外接口不传 std::string，改成 `const char* + size_t`。
</details>

**3. 同事说：「我们的插件接口全是 `extern "C"`，所以绝对 ABI 安全。」接口里有 `struct Req { bool urgent; long deadline; };` 按值传递，宿主和插件分别由不同团队编译。指出至少两个风险。**

<details><summary>答案</summary>
（1）extern "C" 只保证符号不修饰和 C 调用约定，不保证布局：任一方用了 `-fpack-struct` 或 `#pragma pack`，偏移就变了（本课实践里 sizeof 16 和 9 的情况）；（2）`long` 宽度依平台而定（Linux 64 位是 8，Windows 64 位是 4），跨平台要用 `int64_t`；`bool` 的大小也由实现决定。还有一个风险：结构体以后加字段，按值传递的老调用方直接错。更稳的做法是用定宽类型、显式填充或在结构体首字段放 `size`/版本号，或者改为不透明句柄加访问函数。
</details>

## 六、推荐资源

- [ABI、ODR与二进制兼容：参考文档 / 原始资料（cppreference: Definitions and ODR）](https://en.cppreference.com/w/cpp/language/definition.html)，英文文档，难度深入，免费。节点建议重点读与符号修饰、调用约定、对象布局和虚表相关的部分，用来核对本节实验的语义和边界。

---

## 附录：practice32/test.sh

```bash
#!/usr/bin/env bash
# 第32课实践：两个编译选项不一致的共享库，用 nm/readelf/c++filt 定位 ABI 差异
set -euo pipefail
cd "$(dirname "$0")"; rm -rf build; mkdir build; cd build

# ---- 1. 同一份源码，两种 _GLIBCXX_USE_CXX11_ABI 编出两个共享库 ----
cat > greet.h <<'H'
#pragma once
#include <string>
std::string greet(const std::string& name);
H
cat > greet.cpp <<'C'
#include "greet.h"
std::string greet(const std::string& name) { return "hello, " + name; }
C
g++ -std=c++17 -fPIC -shared -D_GLIBCXX_USE_CXX11_ABI=1 greet.cpp -o libgreet_new.so
g++ -std=c++17 -fPIC -shared -D_GLIBCXX_USE_CXX11_ABI=0 greet.cpp -o libgreet_old.so
echo "== 导出符号（修饰名）=="
nm -D --defined-only libgreet_new.so | grep greet
nm -D --defined-only libgreet_old.so | grep greet
echo "== c++filt 还原 =="
nm -D --defined-only libgreet_new.so | grep greet | awk '{print $3}' | c++filt
nm -D --defined-only libgreet_old.so | grep greet | awk '{print $3}' | c++filt

# 用新 ABI 编调用方，链接旧 ABI 库 -> 链接期找不到符号
cat > main.cpp <<'C'
#include "greet.h"
#include <iostream>
int main() { std::cout << greet("wechat") << "\n"; }
C
g++ -std=c++17 -D_GLIBCXX_USE_CXX11_ABI=1 main.cpp -L. -lgreet_new -Wl,-rpath,'$ORIGIN' -o app_ok
./app_ok | tee out_ok.txt
grep -q "hello, wechat" out_ok.txt
if g++ -std=c++17 -D_GLIBCXX_USE_CXX11_ABI=1 main.cpp -L. -lgreet_old -o app_bad 2> link_err.txt; then
  echo "FAIL: 预期链接失败"; exit 1
fi
echo "== 新 ABI 调用方链接旧 ABI 库：链接失败（符合预期）=="
grep -m1 "undefined reference" link_err.txt

# ---- 2. 结构布局被编译选项改变：extern "C" 也救不了 ----
cat > cfg.h <<'H'
#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
struct Cfg { char flag; int64_t timeout_ms; };
int64_t cfg_timeout(const struct Cfg* c);
unsigned cfg_size(void);
#ifdef __cplusplus
}
#endif
H
cat > cfg.cpp <<'C'
#include "cfg.h"
extern "C" int64_t cfg_timeout(const Cfg* c) { return c->timeout_ms; }
extern "C" unsigned cfg_size(void) { return sizeof(Cfg); }
C
g++ -fPIC -shared cfg.cpp -o libcfg_normal.so
g++ -fPIC -shared -fpack-struct=1 cfg.cpp -o libcfg_packed.so
cat > use_cfg.cpp <<'C'
#include "cfg.h"
#include <cstdio>
int main() {
  Cfg c{}; c.flag = 1; c.timeout_ms = 3000;
  std::printf("caller sizeof=%zu lib sizeof=%u timeout=%lld\n",
              sizeof(Cfg), cfg_size(), (long long)cfg_timeout(&c));
}
C
g++ use_cfg.cpp -L. -lcfg_normal -Wl,-rpath,'$ORIGIN' -o cfg_ok
g++ use_cfg.cpp -L. -lcfg_packed -Wl,-rpath,'$ORIGIN' -o cfg_bad 2>/dev/null
echo "== 布局一致 =="; ./cfg_ok | tee cfg_ok.txt
echo "== 库用 -fpack-struct=1：链接成功但读错字段 =="; ./cfg_bad | tee cfg_bad.txt
grep -q "caller sizeof=16 lib sizeof=16 timeout=3000" cfg_ok.txt
grep -q "lib sizeof=9" cfg_bad.txt
! grep -q "timeout=3000" cfg_bad.txt
echo "== nm 两边符号完全一样，看不出差异 =="
diff <(nm -D --defined-only libcfg_normal.so | awk '{print $3}' | grep cfg_) \
     <(nm -D --defined-only libcfg_packed.so | awk '{print $3}' | grep cfg_) && echo "符号相同"

# ---- 3. 符号版本：给 C 接口打版本号 ----
cat > v1.map <<'M'
CFG_1.0 { global: cfg_timeout; cfg_size; local: *; };
M
g++ -fPIC -shared cfg.cpp -Wl,--version-script=v1.map -Wl,-soname,libcfgv.so.1 -o libcfgv.so.1
echo "== readelf 查看 SONAME 与符号版本 =="
readelf -d libcfgv.so.1 | grep SONAME
readelf --dyn-syms -W libcfgv.so.1 | grep cfg_
readelf --dyn-syms -W libcfgv.so.1 | grep -q "cfg_timeout@@CFG_1.0"
echo "== 依赖的 glibc/libstdc++ 版本（新 ABI 库）=="
readelf -V libgreet_new.so | grep -oE "(GLIBCXX|CXXABI|GLIBC)_[0-9.]+" | sort -u | tail -5
echo ALL_CHECKS_PASSED
```
