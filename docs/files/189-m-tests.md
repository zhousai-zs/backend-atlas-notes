# 第 189/208 课：Agent 驱动测试与模糊测试

> 序号 189/208 · id `m-tests` · 领域 M · 阶段 3 · 难度 进阶 · 子类 研发效率
> 前置：[`m-coding`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/187.html)、[`j-sanitizer`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/147.html)、[`e-tsan`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/79.html)
> 节点摘要：Agent 可从协议契约生成边界样例与测试脚手架，fuzzer 在大量输入上探索内存与解析错误。生成测试很快，但 oracle 的正确性仍需工程师建立。

## 导读

第 187 课把协议当作测试合同，第 147、79 课说明 Sanitizer 与竞态检测如何把错误变成证据。本课的对象是一个长度前缀帧解析器：2 字节大端长度，后接恰好 N 字节负载。起始实现**故意不检查** `size >= 2+N` 就 `memcpy`。脚本化 Agent 要写出 libFuzzer 入口，并用**契约**而不是当前实现的输出做 oracle。

教学假设：该解析器是教学用编解码，不是某条生产协议。本机没有 `clang++` 与 libFuzzer，不能声称已经跑过模糊测试。`is_live_model=false`。

## 学习目标

1. 说明从契约到性质：测试应断言协议允许什么，而不是「与当前函数输出一致」。
2. 独立完成实践：为解析器编写 `LLVMFuzzerTestOneInput`，保存可复现种子，并准备修复后的回归。
3. 测试不以实现输出为唯一 oracle；在能编译时运行 fuzzer，不能编译时如实记录。

## 为什么后端要懂

若测试根据当前（有 bug 的）实现来写，截断帧会被当成合法。Fuzzer 只会更高效地「证明」一个错误合同。oracle 必须来自规范。

## 核心知识点

### 1. 契约是 oracle，实现不是

**直觉。** 解析成功当且仅当缓冲区里真的有 `2+N` 个字节。

**原理。** `contract_test.py`：`len(buf) < 2` 或 `len(buf) < 2+n` 则拒绝。语料 `corpus/truncated_len16.bin` 为 `00 10 41`（声明 16 字节，实际只有 1 字节负载）。契约拒绝它；若按有 bug 的实现去「切片/拷贝」`2+n` 字节，则 `would_read=18 > available=3`。合法帧 `00 02 61 62` 被契约接受，负载为 `ab`。这两个结果都不来自 C++ 函数的返回值。

### 2. Harness、种子与（未跑到的）libFuzzer

**直觉。** 入口函数要把契约写成一旦被违反就 trap；种子要进版本库，修复后还得再跑。

**原理。** `fuzz_frame.cpp` 实现 `LLVMFuzzerTestOneInput`：若 `parse_frame` 返回成功，却发现 `size < 2+n` 或 `payload.size()!=n`，则 `__builtin_trap()`。这是用契约抓住实现。官方风格的构建命令是：

```text
clang++ -std=c++17 -fsanitize=fuzzer,address -I repo repo/fuzz_frame.cpp -o fuzz_frame
./fuzz_frame corpus -runs=100
```

驱动探测 `clang++` 并在存在时实际调用。本次 `which_clangxx=None`，`libfuzzer_ran=false`，未实测项写明：harness 与种子已提供，可在有 Clang 的机器上复现，本机没有把这次缺失写成一次模糊测试成功。

## 常见误区

- 为通过当前实现而修改断言，或把实现输出当唯一 oracle。
- 只保存崩溃现场，不把最小化种子纳入仓库。
- 没有 libFuzzer / clang++ 时，用 Python 循环随机字节冒充「已模糊测试」。

## 设计权衡

Agent 可以很快写出边界样例和脚手架；oracle 和崩溃分类仍然要人来定。Fuzzer 扩大覆盖，但不解释根因，也不取代契约测试。

## 动手实践

实践包在 `practice189/`，种子在 `corpus/truncated_len16.bin`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice189 "$tmp_dir/"
cd "$tmp_dir/practice189"
bash test.sh
```

本次：契约拒绝截断种子，错误实现会越界读取，harness 含官方入口并用契约 trap，种子已保存。libFuzzer 未在本机编译运行。`is_live_model=false`。修复 `parse_frame` 的长度检查后，同一种子应改为被实现拒绝且不再 trap，并作为回归保留。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [LibFuzzer](https://llvm.org/docs/LibFuzzer.html)：HTTP 200，`Date` 2026-11-15 16:13:56 GMT（Asia/Shanghai 2026-11-16 00:13:56），`Last-Modified` 2026-11-15 08:35:50 GMT（Asia/Shanghai 2026-11-15 16:35:50）。文档定义 `LLVMFuzzerTestOneInput`、语料目录与 sanitizer 组合；本课按该入口编写 harness，但未在本机执行。

## 自测题

**1. 为什么不能把「当前 parse_frame 接受该种子」当作测试通过？**

答：当前实现有越界读取的 bug。本次契约拒绝 `00 10 41`，而错误实现会读取 18 字节中并不存在的部分。oracle 是协议。

**2. 种子文件的作用是什么？**

答：稳定复现，并在修复后做回归。本次路径为 `corpus/truncated_len16.bin`，内容十六进制 `001041`。

**3. 没有 clang++ 时可以怎么写结论？**

答：写明未运行 libFuzzer，同时提供完整 harness、命令和种子，供有工具链的环境复现。不能改用无 sanitizer 的随机测试并声称模糊测试已完成。

## 推荐资源

- [LLVM LibFuzzer](https://llvm.org/docs/LibFuzzer.html)（英文，文档，进阶，免费）：入口、语料与 sanitizer。
- 第 147 课、第 79 课：把内存错误和竞态变成可重复的动态证据。

## 本课小结

测试的正确性取决于 oracle 是否来自契约。Harness 和种子可以先行提交；没有编译器就不要假装 fuzzer 已经跑过。下一课是第 190 课 m-knowledge（后台知识库与版本证据）。
