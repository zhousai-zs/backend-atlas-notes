# 第 148/208 课：gdb 与 Core Dump

> 序号 148/208 · id `j-gdb` · 领域 J · 阶段 2 · 难度 基础 · 子类 测试与调试
> 前置：[`j-sanitizer`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/147.html) 和 [`b-lifetime`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/19.html)
> 节点摘要：通过断点、栈帧、寄存器和 core 分析崩溃现场；优化与符号完整性影响可观察变量。

## 导读

上一课的 Sanitizer 检测构建能抓住已执行路径中的一些内存错误，未覆盖的路径仍可能在服务运行时崩溃。排查现场需要保留对应的程序、符号和 core，再从线程栈、变量与指令寻找证据。

教学假设：内容后台有一个 C++ 条目处理组件，工作线程把选中的条目传给渲染函数。以下组件与缺陷均为教学设计，不代表腾讯或微信内部实现。程序故意把指针置空，再交给后续函数解引用。

## 学习目标

1. 解释backtrace/thread apply all及适用边界
2. 独立完成实践：制造空指针崩溃，用匹配二进制加载 core 并定位根因。

## 为什么后端要懂

节点给出的理由是「建立独立定位线上 C++ 崩溃的能力。」现场常常不能随意重启重现：线程停在哪里、传入了什么指针、使用的二进制是否就是发生故障的版本，都影响结论。调试记录是定位线索，还需要结合源码、测试和更早的写入验证因果关系。

## 核心知识点

### 1. backtrace/thread apply all

**直觉。** `bt` 从当前线程的当前帧向调用者回溯；`frame 1` 选中上一层，再用 `info locals` 检查该帧的局部变量。帧号是本次展开的结果，不能假定每次构建都相同。

**原理。** GDB 根据寄存器、栈和可用的展开信息恢复调用关系，并借助调试信息映射函数与源码。`thread apply all bt` 对每个可见线程执行回溯；它适合检查崩溃线程之外的等待或调用位置。线程栈是停下时的状态，不是事件历史；优化、缺失符号或损坏的栈也可能使帧不完整。

本次 `reports/live.txt` 中工作线程先停在无效读取，以下省去地址及运行库帧：

```text
Thread 2 "service-O0" received signal SIGSEGV, Segmentation fault.
#0  ... in render (item=0x0) at service.cpp:7
#1  ... in process () at service.cpp:14
#2  ... in worker () at service.cpp:16
```

同一报告中主线程的栈包含 `main () at service.cpp:27`，正在等待工作线程。切到崩溃线程的 `frame 1` 后，GDB 显示 `valid = {score = 42}` 与 `selected = 0x0`。查看源码第 13 行才知道 `selected = nullptr` 先覆盖了有效指针，第 14 行再传给 `render`；第 7 行是故障访问处，不是最早的错误赋值处。

这是人为放入的错误赋值，并非堆损坏的完整历史。真实内存破坏可能早于所有仍在栈上的帧；core 不记录每一次写入。应结合源码、输入、日志、watchpoint 或上一课的检测构建继续查证。

### 2. core 与 build-id

**直觉。** core 保存的是进程某一时刻可用的内存与线程状态；要用发生故障时的可执行文件、相应调试符号和共享库解释地址。文件名相同不证明内容相同。

本机软硬 `RLIMIT_CORE` 均为 0，不能依靠内核的崩溃路径自动落盘。实验让 GDB 运行程序并停在 SIGSEGV，再执行 `generate-core-file`，随后用同一个 `build/service-O0` 加载。这是调试器主动生成快照，与内核在进程终止时按 `core_pattern` 和限制生成 core 不同。生成时对一个内存页有读取警告，重新加载时也有寄存器扩展区大小警告；本次源码栈和变量仍可读取，见完整报告。

```gdb
run
thread apply all bt
frame 1
info locals
generate-core-file /tmp/crash.core
```

随后运行 `gdb -q build/service-O0 /tmp/crash.core`，执行 `bt` 和 `frame 1`。脚本实际使用临时目录，结束时删除 core，报告留在 `reports/`。core 可能含有请求内容、凭据或个人数据，应按敏感生产数据限制访问和保留时间。

**Build ID 核对。** `readelf -n build/service-O0` 输出的 ID 是 `680bceebd5d7e6fe3132110efed4875f69c5c9a4`。`extract_ids.py` 从 core 的 ELF64 小端 PT_LOAD 段中找出被保存的 ELF 头，对提取内容运行 `readelf -n`；本次可执行映射 `0x1000000` 的 ID 相同。core 的普通 note 列表本身不必直接有该 ID，且 core 可能不包含所需映射，因此提取失败应停止核对，不能猜测匹配。

`-O2` 对照构建的 ID 是 `446436600a639875b128828f46afa8056e091149`，不在本次 core 的已提取 ID 中。用它加载同一个 core 的真实输出包含：

```text
warning: core file may not match specified executable file.
#0  ... in __libc_csu_init (...) at .../elf-init-2.33.c:86
```

此处省略地址和参数；完整记录在 `reports/mismatch.txt`。与匹配构建的 `render` 对照可见错误符号化的风险。Build ID 是识别构建的有力线索，不单独证明每个共享库和外置调试文件都匹配；生产排查还要核对各模块及其符号来源。

### 3. 优化变量和汇编

**直觉。** 源码变量不一定有固定的内存位置，优化还可能消除计算、内联函数或改变执行顺序。断点对应的机器指令可能落在相邻源码行。

脚本用同一源码分别以 `-g -O0` 和 `-g -O2` 构建，在 `optimized` 函数对应位置打印 `transient`、寄存器并反汇编。`-O0` 下 `print transient` 为 `$1 = 35`；计算及保存操作在 `reports/opt-O0.txt`：

```text
imul   $0x7,-0x4(%rbp),%eax
mov    %eax,-0x8(%rbp)
```

`-O2` 的断点请求第 19 行，实际停在第 20 行；`print transient` 仍得到 `$1 = 35`，本次并没有显示 `<optimized out>`。`info registers` 显示 `rdi 0x5 5`；反汇编中该函数没有乘法或保存 `transient` 的指令，返回值使用：

```text
lea    0x1(%rdi),%eax
```

这里可由输入 5 和源码表达式 `input * 7` 推得 35，GDB 也能显示该值；它不是证据表明有一个独立的栈槽。调试信息可能提供重建方式，也可能在其他位置显示 `<optimized out>`。寄存器和指令仅解释本次构建及停点，不应照搬到不同架构或编译选项。

## 常见误区

1. **只看栈顶函数就认定根因。** 本例 `render` 执行了空指针读取，`process` 的更早赋值让指针变空；栈顶是现场，检查调用者、数据来源和先前写入后才能提出根因。
2. **有源码行就认定符号匹配。** 不匹配构建也能显示一行看似可信的运行库源码，本次还产生了错误的 `__libc_csu_init` 栈帧。先核对构建与符号，再解释栈。
3. **把所有线程的栈当作完整执行历史。** `thread apply all bt` 只显示快照中可恢复的线程调用链；已经返回的调用和过去的写操作需要其他证据。

## 设计权衡

节点原句是「低优化易调试，生产优化更贴近真实行为。」本次 `-O0` 保存局部变量并显示乘法，`-O2` 消除了该计算，但 GDB 仍可显示重建值；不能据此断言所有优化变量都会消失。低优化构建便于理解控制流，却可能改变时序和布局；生产版本的优化构建更有代表性，也可能让变量和帧更难观察。排查时保留精确的生产二进制及匹配符号，再用低优化或检测构建辅助复现。

## 动手实践

实践目录为 `practice148/`，需要 Linux、Python 3、`readelf`、支持线程及 build-id 的 C++ 工具链和允许调试子进程的 GDB。脚本先找系统 `g++`、`clang++`，否则使用 `tools/zig-c++`；该包装器可使用安装了 `ziglang` 的 Python 环境。本次 Zig 目标为 `x86_64-linux-gnu`，不是默认 musl 目标。脚本先查 PATH 中的 GDB，再查 `ATLAS_GDB` 和本机包装器；都不存在时明确失败。

```bash
cd practice148
bash test.sh
cat results.json
cat reports/live.txt
cat reports/core.txt
cat reports/core-build-ids.txt
cat reports/mismatch.txt
cat reports/opt-O0.txt
cat reports/opt-O2.txt
```

脚本构建两份带 `-g`、SHA-1 build-id 的二进制，关闭本例的编译器未定义行为检查，以观察实际空指针 SIGSEGV。单独直接运行坏程序的退出码为 139，作为预期故障检查；GDB 停住后主动生成 core，再使用同一低优化二进制加载。日志保存到 `run.log`，成功最后一行为 `ALL_CHECKS_PASSED`。地址、ID、线程号和优化效果依赖构建及环境，读者应以自己生成的记录为准。

若要亲手操作，保留脚本构建的二进制后，在实践目录启动交互式 GDB：

```bash
gdb -q build/service-O0
```

在 GDB 提示符下执行以下命令；`run` 遇到信号后会停住。
`info threads` 帮助辨认当前线程，随后查看所有线程的调用栈：

```gdb
set pagination off
run
info threads
thread apply all bt
frame 1
info locals
list
```

确认帧 1 是 `process` 后再解释局部变量。
不同编译器或优化设置可能改变帧号，应先读 `bt` 再选择帧。
当前帧的源码可以用 `list` 查看，不能仅凭 `selected = 0x0` 推断它何时被写入。

在仍然停住的进程上生成 core，然后退出 GDB：

```gdb
generate-core-file /tmp/practice148-manual.core
quit
```

再次加载同一二进制和该 core，检查离线栈：

```bash
gdb -q build/service-O0 /tmp/practice148-manual.core
# 在 GDB 中执行 bt、frame 1、info locals，然后 quit
rm -f /tmp/practice148-manual.core
```

这里手动生成的文件与脚本的临时 core 分开；调试结束应及时删除。
生产排查应依照现场的数据管理要求处理 core，而不是将其放进公开实践包。

两项验收是「确认符号和二进制匹配」以及从 core 或现场定位到故障位置和可能的根因。分别对应：`elf-o0.txt` 与 `core-build-ids.txt` 核对执行文件映射的 ID，`mismatch.txt` 展示错误构建对照；`core.txt` 的栈和 `frame 1` 局部变量配合 `service.cpp` 的赋值，区分故障访问与更早错误写入。脚本的提取辅助程序只支持 ELF64 小端 core，且要求 core 保存了相关 ELF 头；不满足时应使用适合目标的工具，不应视为通过。

## 源码与文档阅读

阅读 `service.cpp` 的 `process` 与 `render`，对照 `reports/live.txt` 的两条线程栈。GDB 手册的 Backtrace 描述当前线程调用栈，Threads 描述对各线程执行命令；本实验补充了源码中的较早赋值，不能从快照单独推知历史。Core File Generation 介绍调试器生成 core，Files 介绍加载程序与符号；`core(5)` 则说明内核 core 的生成条件和命名规则。

2026-11-15 本机亲自尝试重新抓取节点两条 URL，均因 DNS 解析失败（curl 退出码 6、无 HTTP 状态）；以下核对本目录当天既有的 `.html`、`.hdr`、`.curl`。表中时间均已由 GMT 换算为北京时间；最终 URL 均为表中链接，HTTP 200、跳转 0 次。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） |
| --- | ---: | --- | --- |
| [GDB — User Manual](https://sourceware.org/gdb/current/onlinedocs/gdb.html/)：Top (Debugging with GDB) | 113541 | 2026-11-15 22:35:09 | 2026-11-15 10:07:32 |
| [core(5)](https://man7.org/linux/man-pages/man5/core.5.html)：core(5) - Linux manual page | 30553 | 2026-11-15 22:35:11 | 2026-11-14 08:33:03 |
| [Backtrace](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Backtrace.html)：Backtrace (Debugging with GDB) | 15637 | 2026-11-15 22:35:11 | 2026-11-15 10:07:33 |
| [Threads](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Threads.html)：Threads (Debugging with GDB) | 29490 | 2026-11-15 22:35:12 | 2026-11-15 10:07:33 |
| [Core File Generation](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Core-File-Generation.html)：Core File Generation (Debugging with GDB) | 8079 | 2026-11-15 22:35:13 | 2026-11-15 10:07:33 |
| [Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html)：Files (Debugging with GDB) | 41439 | 2026-11-15 22:35:13 | 2026-11-15 10:07:34 |

节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。手册是当前在线版本，实验实际使用 GNU gdb (Debian 16.3-1) 16.3 包装器、ziglang 0.17.1 的 `zig c++`（clang 22.1.8 前端）和 GNU 目标；手册示例不保证与本机的库、地址、线程号或 core 生成路径相同。

## 自测题

1. **`bt` 与 `thread apply all bt` 有何不同？** 前者通常查看当前线程，后者对每个可见线程回溯；两者都不是执行历史。
2. **文件名一样就能用它分析 core 吗？** 不能。核对 core 中可用的映射与可执行文件的 Build ID，并核对相应调试符号和共享库；缺少证据时继续查找构建产物。
3. **`-O2` 下还能打印 35，是否证明变量在栈上？** 不能。本次汇编没有保存该变量的计算，值可能由调试信息和输入重建；观察寄存器、指令及对应源码位置。

## 推荐资源

- [GDB — User Manual](https://sourceware.org/gdb/current/onlinedocs/gdb.html/)：用于核对backtrace/thread apply all与core 与 build-id，将结论落实到本节点实验。英文，基础，免费。
- [Linux man-pages — core(5)](https://man7.org/linux/man-pages/man5/core.5.html)：了解 core 生成限制与文件规则。英文，进阶，免费。

## 本课小结

崩溃定位从匹配程序与符号开始，使用线程栈定位故障访问，再追溯指针来源；优化构建需要结合寄存器与汇编解释变量。下一课是第 149 课 j-logging（结构化日志与敏感信息）。
