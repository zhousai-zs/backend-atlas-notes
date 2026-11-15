# 第 189/208 课：Agent 驱动测试与模糊测试

> 序号 189/208 · id `m-tests` · 领域 M · 阶段 3 · 难度 进阶 · 子类 研发效率
> 前置：[`m-coding`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/187.html)、[`j-sanitizer`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/147.html)、[`e-tsan`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/79.html)
> 节点摘要：Agent 可从协议契约生成边界样例与测试脚手架，fuzzer 在大量输入上探索内存与解析错误。生成测试很快，但 oracle 的正确性仍需工程师建立。

## 导读

第 187 课把协议当作测试合同，第 147、79 课说明 Sanitizer 如何把错误变成证据。本课的对象是一个长度前缀帧解析器：2 字节大端长度，后接恰好 N 字节负载。起始实现**故意不检查** `size >= 2+N` 就 `memcpy`。测试必须以**契约**为 oracle，并且缺陷必须被**编译后的驱动真正抓住**；加上长度检查后再干净复跑。

教学假设：该解析器是教学用编解码，不是某条生产协议。`is_live_model=false`。

此前一版只在系统 `PATH` 中查找 `clang++`，未盘点 cmvenv 里的 zig c++，因而把模糊测试写成未运行。本次修订先尝试 `zig c++ -fsanitize=fuzzer,address`；该链接在本机失败，因而改用真实编译的、带 ASan 的确定性变异驱动器，并明确写明**它不是 libFuzzer**。旧稿另行保留，文件名不变。

## 学习目标

1. 说明从契约到性质：测试应断言协议允许什么，而不是「与当前函数输出一致」。
2. 独立完成实践：保留 `LLVMFuzzerTestOneInput` 入口，保存可复现种子；用编译后的工具抓住越界读取，修复后再跑同一批输入。
3. 测试不以实现输出为唯一 oracle。若 libFuzzer 确实链不上，必须如实说明，不能用脚本冒充一次模糊测试成功。

## 为什么后端要懂

若测试根据当前（有 bug 的）实现来写，截断帧会被当成合法。Fuzzer 只会更高效地「证明」一个错误合同。oracle 必须来自规范。工具链则必须先盘点再下结论。

## 核心知识点

### 1. 契约是 oracle，实现不是

**直觉。** 解析成功当且仅当缓冲区里真的有 `2+N` 个字节。

**原理。** `contract_test.py`：`len(buf) < 2` 或 `len(buf) < 2+n` 则拒绝。语料 `corpus/truncated_len16.bin` 为 `00 10 41`（声明 16 字节，实际只有 1 字节负载）。契约拒绝它；错误实现却会 `memcpy` 16 字节。合法帧 `00 02 61 62` 被契约接受，负载为 `ab`。这两个判断都不来自「看当前 C++ 函数返回了什么就当标准」。

### 2. 先尝试 libFuzzer，链不上就用编译后的变异驱动，并写清楚

**直觉。** 入口函数可以先写好；能不能链上 libFuzzer 是另一回事，不能把失败说成成功。

**原理。** `fuzz_frame.cpp` 实现官方入口 `LLVMFuzzerTestOneInput`：若 `parse_frame` 返回成功，却发现 `size < 2+n`，则 `__builtin_trap()`。本次用 `tools/zig-c++`（clang 22.1.8）执行：

```
zig c++ -std=c++17 -fsanitize=fuzzer,address -I repo repo/fuzz_frame.cpp -o fuzz_frame
```

链接失败，`reports/libfuzzer-link.txt` 记录了 `undefined symbol: main`（未能链入 libFuzzer 运行时）。因此改用 `fuzz_driver.cpp`：这是一个**真实编译**的确定性变异驱动，调用同一个 `LLVMFuzzerTestOneInput`，对种子做位翻转、改写声明长度、截断等可复现变异，并链接 `libasan.so.8`。它对截断种子的运行退出码为 -6，ASan 报告 `heap-buffer-overflow`（`__asan_memcpy`）。这就是缺陷被抓住的证据。必须写明：**这不是 libFuzzer，也不是覆盖率引导的模糊测试。**

修复在 `parse_frame` 中加上 `size < 2+n` 则失败。同一驱动、同一种子和同一组变异在 ASan 下退出码为 0，日志不再有 overflow。种子仍保留在 `corpus/truncated_len16.bin`，作为回归输入。

## 常见误区

- 为通过当前实现而修改断言，或把实现输出当唯一 oracle。
- 只保存崩溃现场，不把最小化种子纳入仓库。
- 本机已有 zig c++ 与 ASan，却因系统 `PATH` 没有 `clang++` 就宣称无法编译。
- ` -fsanitize=fuzzer` 链接失败后，用纯 Python 随机字节冒充「已模糊测试」，或不说明替代驱动并非 libFuzzer。

## 设计权衡

Agent 可以很快写出边界样例和脚手架；oracle 和崩溃分类仍然要人来定。libFuzzer 能做覆盖率引导，本机链不上时，编译后的确定性变异加 ASan 仍然能复现和回归，只是探索能力更弱，不能改叫 libFuzzer。

## 动手实践

实践包在 `practice189/`，种子在 `corpus/truncated_len16.bin`。需要 `cmvenv` 中的 ziglang 以及系统 `libasan`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice189 "$tmp_dir/"
cd "$tmp_dir/practice189"
bash test.sh
```

本次修订实测：ASan 在截断种子上报告 `heap-buffer-overflow`；加上界限检查后，种子与变异战役均干净。libFuzzer 链接未成功，已写入未实测，并标明所用驱动不是 libFuzzer。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [LibFuzzer](https://llvm.org/docs/LibFuzzer.html)：HTTP 200，`Date` 2026-11-15 16:13:56 GMT（Asia/Shanghai 2026-11-16 00:13:56），`Last-Modified` 2026-11-15 08:35:50 GMT（Asia/Shanghai 2026-11-15 16:35:50）。文档定义 `LLVMFuzzerTestOneInput`、语料目录与 sanitizer 组合；本课按该入口编写了 harness，但本机未能链接 libFuzzer 运行时。

## 自测题

**1. 为什么不能把「当前 parse_frame 接受该种子」当作测试通过？**

答：当前实现会越界读取。本次契约拒绝 `00 10 41`，ASan 在错误实现上给出 `heap-buffer-overflow`。oracle 是协议。

**2. 种子文件的作用是什么？**

答：稳定复现，并在修复后做回归。本次路径为 `corpus/truncated_len16.bin`，内容十六进制 `001041`。

**3. libFuzzer 链接失败时，正确的做法是什么？**

答：记录失败日志，改用仍然真实编译的替代驱动，并明确写「不是 libFuzzer」。不能把未链接的入口或纯 Python 随机测试写成模糊测试已完成。

## 推荐资源

- [LLVM LibFuzzer](https://llvm.org/docs/LibFuzzer.html)（英文，文档，进阶，免费）：入口、语料与 sanitizer。
- 第 147 课、第 79 课：把内存错误和竞态变成可重复的动态证据。

## 本课小结

测试的正确性取决于 oracle 是否来自契约。缺陷要被编译后的工具抓住，修复后要用同一输入回归。链不上 libFuzzer 就必须如实说明替代方案。本课是 187–189 修订的第三课。
