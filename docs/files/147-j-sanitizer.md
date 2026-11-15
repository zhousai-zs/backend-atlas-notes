# 第 147/208 课：Sanitizer 动态检测

> 序号 147/208 · id `j-sanitizer` · 领域 J · 阶段 2 · 难度 基础 · 子类 测试与调试
> 前置：[`j-unit`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/145.html) 和 [`b-pointer`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/18.html)
> 节点摘要：ASan、UBSan、TSan 对不同类别的内存错误、未定义行为和数据竞争插桩检测，不能保证发现所有错误。

## 导读

上一课的集成与故障测试覆盖了重启和隔离，但请求得到正确响应，也不能证明执行途中没有内存错误、未定义行为或数据竞争。危险代码可能要等特定输入和线程交错出现才暴露。

教学假设：内容后台有一个 C++ 配置或会话缓存，持有条目、缓冲和共享计数。这里的组件和缺陷都是教学设计，不代表腾讯或微信内部实现。我们在真实编译出的程序里注入缺陷，保存检测报告，再用修复版本复跑。

## 学习目标

1. 解释AddressSanitizer及适用边界
2. 独立完成实践：分别注入 use-after-free、越界和数据竞争，保存报告并修复。
3. 分开构建检测器，用实际报告定位首次错误，并解释开销和漏报边界。

## 为什么后端要懂

节点给出的理由是「在上线前捕获难复现 C++ 缺陷」。缓存条目的生命周期、输入长度和并发访问经常跨越多个函数；普通功能断言只检查结果，错误访问有时不会立刻使结果变化。检测构建让已经执行到的危险操作更容易在测试时留下可定位的证据。

## 核心知识点

### 1. AddressSanitizer

**直觉。** 缓存返回的指针必须在对象仍活着时使用；数组的第一个非法位置也不能因为「只差一个字节」就被写入。ASan 检查插桩后的内存访问，并通过运行库跟踪分配与释放状态。

**原理。** ASan 使用影子内存标记可访问区域，在堆对象周围设置不可访问区；释放后的对象也会被标记。它能检查多类堆、栈和全局对象越界以及释放后使用，但只对被执行、可被它观察的操作提供证据。检测结果依赖构建、运行环境和执行路径。

本机用实践包包装器编译，下面是本次真实命令的主要参数；GNU 目标和额外运行库的原因见后文：

```bash
./tools/zig-c++ -target x86_64-linux-gnu -std=c++17 -g -O1 \
  -fno-omit-frame-pointer -pthread -fsanitize=address cache.cpp \
  /usr/lib/x86_64-linux-gnu/libasan.so.8 -o build/asan
./build/asan uaf bad
./build/asan bounds bad
```

`reports/asan-uaf.txt` 的第一组错误栈中，真正的访问发生在 `stale(bool)`，而不是后面运行库的终止位置：

```text
ERROR: AddressSanitizer: heap-use-after-free
READ of size 4
#0 ... in stale(bool) /workspace/atlas/lessons/practice147/cache.cpp:11
#1 ... in main /workspace/atlas/lessons/practice147/cache.cpp:37
```

省略号仅省去每次变化的地址和同一行上的其他字段。完整报告还给出释放栈和分配栈，帮助把失效引用追溯到对象生命周期。该坏版本退出码为 1；修复版先取值再释放，正常输出 `value=42`。

`reports/asan-bounds.txt` 报 `heap-buffer-overflow`，首帧为 `bounds(bool)` 的 `cache.cpp:16`：分配 8 字节后写下标 8。修复选择下标 7，坏版本退出码为 1，修复版输出 `value=120`。这是堆缓冲区越界；UBSan 也有部分数组边界检查，但本例的动态分配边界由 ASan 抓到，不能把两者视为同一组检查。

### 2. UndefinedBehaviorSanitizer

**直觉。** 访问的地址合法，也不代表算术表达式符合 C++ 规则。有符号整数溢出属于未定义行为；配置版本号或计数器在上限附近累加时需要边界处理。

**原理。** UBSan 在编译时为选定的未定义行为插入检查，例如有符号溢出、无效移位和部分越界。它不是内存生命周期检测器，检查集合也不是所有未定义行为的完整清单。这里用 `-fno-sanitize-recover=undefined` 要求命中后停止，而不是继续运行掩盖失败。

```bash
./tools/zig-c++ -target x86_64-linux-gnu -std=c++17 -g -O1 \
  -fno-omit-frame-pointer -pthread -fsanitize=undefined \
  -fno-sanitize-recover=undefined cache.cpp \
  /usr/lib/x86_64-linux-gnu/libubsan.so.1 -o build/ubsan
./build/ubsan overflow bad
```

本次 `reports/ubsan-overflow.txt` 来自 Zig 的 UBSan 运行时代码，格式是 panic：

```text
panic: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'
.../cache.cpp:24:14: ... in overflow (cache.cpp)
    return n + 1;
```

坏版本退出码为 134；修复分支改为上限内运算，输出 `value=2147483646`，退出码为 0。例子展示上限处理，实际业务应根据业务规则选择拒绝、扩大类型或安全的饱和处理。

漏报对照运行的是**同一个仍有溢出的坏分支**：`./build/asan overflow bad` 正常退出，`reports/asan-overflow-control.txt` 没有 ASan 错误。这只说明这次 ASan 配置没有报告这类算术错误，不说明溢出消失；UBSan 的报告和源码是对照证据。

### 3. ThreadSanitizer 分开构建

**直觉。** 两个线程都更新缓存计数，即使最终值偶尔正确，也可能存在未同步的冲突访问。数据竞争需要对访问和同步关系一起分析，内存地址仍然有效并不能排除它。

**原理。** TSan 插桩并记录线程的读写和同步事件，根据事件间的先行发生关系识别潜在竞争；报告会给出冲突访问及线程创建栈。测试只覆盖这次运行的路径和调度，未插桩代码、未执行路径等仍可能造成盲区。

脚本独立生成 `build/tsan`，不把 ASan 和 TSan 放在同一构建命令中。本机实际使用：

```bash
./tools/zig-c++ -target x86_64-linux-gnu -std=c++17 -g -O1 \
  -fno-omit-frame-pointer -pthread -fsanitize=thread cache.cpp \
  /usr/lib/x86_64-linux-gnu/libtsan.so.2 -o build/tsan
TSAN_OPTIONS=halt_on_error=1 ./build/tsan race bad
```

本次 `reports/tsan-race.txt` 中首次冲突访问与此前访问的关键帧均为 `increment(bool)`，`cache.cpp:31`；两个线程没有用锁保护 `++counter`。报告以 `WARNING: ThreadSanitizer: data race` 开头，坏版本退出码为 66。`race fixed` 用 `std::mutex` 保护每次更新，退出码为 0，输出 `counter=20000`，没有 TSan 警告。

曾对组合选项做过一次工具链探测：编译竟返回 0，运行释放后访问时输出的是 TSan 的 `heap-use-after-free` 报告，退出码 66；这一现象不证明两个检测器同时有效。[GCC 插桩选项文档](https://gcc.gnu.org/onlinedocs/gcc/Instrumentation-Options.html)明确禁止在 GCC 中组合 ASan 与 TSan；这是 GCC 的规则，本机没有使用 GCC 编译。探测原始文本保存在 `gen147/combined-run.txt`，正式实践始终使用独立构建。

## 常见误区

1. **把无报告当作程序无错误的证明。** 本次 ASan 跑有符号溢出无报告，UBSan 却在同一坏分支报错。无报告也可能是路径没执行或代码没有被正确插桩；要结合针对性测试和适合该缺陷的检测器。
2. **只看最后的崩溃位置。** ASan 的 `#0 stale(bool)` 是非法读取处；释放栈和分配栈分别说明指针如何失效和对象从哪里来。运行库里的 abort 帧不能替代这些线索。
3. **竞争测试得到正确计数就认为安全。** 最终结果受调度影响；TSan 的两组冲突访问栈说明缺少同步，即使某次数字看起来正确也需要修复。

## 设计权衡

节点原句是「检测构建更慢更耗内存，适合测试而非直接比较生产性能。」本次对同一修复版程序分别用普通和 ASan 构建，以 Python `time.perf_counter()` 测量子进程从启动到退出的墙钟时间，工作负载和输出相同：普通构建 **0.010460 秒**，ASan **0.022284 秒**。原始数值在 `results.json` 和 `reports/timing.json` 中。

这是各一次短运行，包含启动和调度，优化、机器负载及检测运行库都会影响结果；不能外推成生产服务的性能倍数。本次没有测量内存开销。检测构建适合放进测试流水线；容量与性能决策需要另行使用代表生产的构建和负载测量。

## 动手实践

实践包在 `practice147/`。需要 Python 3、支持相应检测器的 C++ 编译器和线程库；本机回退到 Zig 时还使用现有 GNU Sanitizer 运行库。脚本优先寻找系统 `g++` 或 `clang++`，均不存在时使用包内 `tools/zig-c++`；包装器也可使用当前 Python 环境的 `ziglang`，不以本机虚拟环境路径作为唯一途径。

```bash
cd practice147
bash test.sh
cat results.json
cat reports/asan-uaf.txt
cat reports/asan-bounds.txt
cat reports/ubsan-overflow.txt
cat reports/tsan-race.txt
```

1. **构建并注入。** `cache.cpp` 的 `bad` 分支分别访问已释放对象、写堆数组外、让有符号数溢出、无锁更新共享计数。脚本要求前三项的相应诊断和非零退出；TSan 成功运行时也要求竞争报告和非零退出。
2. **保存报告并修复。** 每项的完整标准输出和错误输出保存在 `reports/`。对应 `fixed` 分支分别调整生命周期、下标、算术和同步；脚本以同一检测器复跑，要求退出 0 且没有相应错误。TSan 若编译或运行环境不可用，脚本保留真实错误并将状态写入 `results.json`，不能把它当成竞争已检测或已验证修复。
3. **验收首次错误栈。** 打开 `asan-uaf.txt` 第一组 `READ` 后的 `#0`，定位 `stale(bool)` 的第 11 行；比较后续释放栈与分配栈。再检查越界首帧第 16 行和竞争首帧第 31 行，注意它们是不同报告中的不同操作。
4. **验收开销与漏报。** 比较 `reports/timing.json` 的同负载实测值；再查看 `asan-overflow-control.txt` 和 `ubsan-overflow.txt`。前者无报告但仍执行坏分支，后者指出溢出。重新运行时地址、线程顺序和耗时会变化，应以新生成的文件为准。

脚本最后输出 `ALL_CHECKS_PASSED`。这是脚本检查通过的标志；若 TSan 环境不可用，具体状态仍须看 `results.json`。复制到干净临时目录并删除旧构建与报告后，本次复跑也输出了该标志。

## 源码与文档阅读

2026-11-15 北京时间亲自抓取了三篇 Clang 文档；响应最终 URL 均与下列链接相同，均为 HTTP 200、无跳转。记录保存在 `gen147/` 的 `.curl`、`.hdr` 和 `.html` 文件中。节点 `verifiedAt` 是 2026-10-26，与本次核对日期不同。

| 页面标题 | HTML 字节数 | 响应 Date（北京时间） | Last-Modified（北京时间） |
| --- | ---: | --- | --- |
| AddressSanitizer - Clang | 78964 | 2026-11-15 22:26:58 | 2026-11-04 02:35:24 |
| ThreadSanitizer - Clang | 74354 | 2026-11-15 22:26:59 | 2026-11-04 02:35:24 |
| UndefinedBehaviorSanitizer - Clang | 84972 | 2026-11-15 22:26:59 | 2026-11-04 02:35:24 |

ASan 文档列出越界、释放后使用等可检测类别，示例用编译选项启用插桩，并建议调试信息和保留帧指针改善堆栈；它也说明检测开销及使用限制。TSan 文档描述数据竞争和编译运行方法；UBSan 文档列出有符号溢出等检查以及恢复、终止策略。这些定义解释了本实验为何需要不同检测器，而不是用一次干净运行证明全部正确。

与文档示例的差异：本机没有系统 g++ 或 clang++，实际是 ziglang 0.17.1 的 `zig c++`，前端报告 clang 22.1.8。版本信息显示宿主目标 `x86_64-unknown-linux5.10.0-gnu2.41.0`，默认 `-dumpmachine` 是 `x86_64-unknown-linux-musl`；本次**实际构建**显式指定 `x86_64-linux-gnu`。默认目标的 ASan 链接缺少运行库符号，本次 GNU 构建显式链接机器已有运行库，UBSan 报告则由 Zig 自带运行时代码产生。文档示例不使用这个包装器及运行库组合；本例还增加了缓存场景、互斥修复和错误检测器对照。

## 自测题

**1. `asan-uaf.txt` 的首次错误栈应先看哪里？**

答：看 `READ` 后 `#0` 的 `stale(bool)`、`cache.cpp:11`，它是非法读取；再看释放栈和分配栈，解释对象为何失效。不要只看运行库终止位置。

**2. ASan 对溢出坏分支无报告，能否删除 UBSan 检查？**

答：不能。本次 `overflow bad` 的 ASan 对照无报告，同一分支在 UBSan 下报告有符号溢出。检测器的职责和执行覆盖不同，无报告不证明没有缺陷。

**3. 为什么 TSan 修复版需要互斥锁，即使坏版某次计数也正确？**

答：坏版两个线程的更新没有同步，TSan 报告两次冲突写入。偶然得到正确计数不改变竞争；锁为更新建立同步关系，修复版仍要在 TSan 下复跑。

## 推荐资源

- [Clang — AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html)：节点主资源，英文、基础、免费；用于核对 AddressSanitizer 与 UndefinedBehaviorSanitizer，并将结论落实到实验。
- [Clang — ThreadSanitizer](https://clang.llvm.org/docs/ThreadSanitizer.html)：节点资源，英文、进阶、免费；用于对比 TSan 的数据竞争检测与 ASan 内存检测。
- 跟随阅读 [Clang — UndefinedBehaviorSanitizer](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html)：核对检查类别及恢复和终止选项。

## 本课小结

ASan 捕获本次释放后访问与堆越界，UBSan 捕获有符号溢出，独立构建的 TSan 捕获共享计数的数据竞争。保存完整报告、从首次错误栈回到源码并复跑修复，才能把检测结果变成可验证的改动；无报告仍有覆盖和检测类别的边界。下一课是第 148 课 j-gdb（gdb 与 Core Dump）。
