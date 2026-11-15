# 第 188/208 课：AI 代码审查与缺陷复核

> 序号 188/208 · id `m-review` · 领域 M · 阶段 3 · 难度 进阶 · 子类 研发效率
> 前置：[`m-coding`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/187.html)、[`j-sanitizer`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/147.html)
> 节点摘要：模型可提出潜在生命周期、并发和异常安全问题，但缺陷必须通过最小复现、规则或人工判断确认。流畅的解释不能代替可触发的证据。

## 导读

第 187 课强调生成代码仍要审查，第 147 课用 Sanitizer 把未定义行为变成可复现证据。本课给审查器一份含**悬空引用**和**未同步计数器**的 diff，要求每条结论带代码位置，并且缺陷必须被**编译后的工具或测试真正抓住**；修复后再干净复跑。

教学假设：`buffer.h` / `use.cpp` 是为暴露缺陷构造的教学片段，不是某条业务链路的生产代码。审查器是脚本化规则，`is_live_model=false`。

此前一版只在系统 `PATH` 中查找 `clang-tidy`，未盘点 cmvenv 与第 147 课已经用过的 zig c++ / 系统 `libasan`、`libtsan`，因而把静态分析和 sanitizer 写成未运行。本次修订先清点工具，再用同一编译器链接 `libasan.so.8` 与 `libtsan.so.2`，并安装、运行真实的 clang-tidy 22.1.8。旧稿另行保留，文件名不变。

## 学习目标

1. 说明审查的上下文范围：不仅看 diff 新增行，还要看调用点如何触发。
2. 独立完成实践：用编译后的 ASan 抓住悬空引用，用 TSan 抓住数据竞争；修复后两次运行都必须干净。
3. 尽力运行真实 clang-tidy，并如实记录它是否（以及是否没有）单独报告这两个缺陷。并发检查不能因为「模型没意见」而跳过。

## 为什么后端要懂

模型擅长跨文件提示「这里可能有问题」，但漏报和误报都常见。没有文件:行和可触发的复现，审查意见就只是文风。静态分析值得跑，但动态 sanitizer 往往更能钉住生命周期和竞态；两者都要在本机真实调用，缺工具时先找已经装过的版本。

## 核心知识点

### 1. 缺陷必须被编译后的工具抓住

**直觉。** 先复现，再谈修复。

**原理。** `temporary_token()` 返回 `Slice{token.c_str(), token.size()}`，`token` 是函数内的 `std::string`，返回后对象销毁；`use.cpp` 随即 `std::string(s.ptr, s.len)`。用 `tools/zig-c++`（clang 22.1.8）加上 `-fsanitize=address`，并链接系统 `/usr/lib/x86_64-linux-gnu/libasan.so.8`（这与第 147 课相同），运行结果为退出码 1，日志为 `ERROR: AddressSanitizer: stack-use-after-scope`，访问发生在 `use.cpp` 的 `main` 读取那块已经结束的栈上字符串。

`bump()` 对非原子的 `shared_counter++` 没有互斥，`use.cpp` 用两个 `std::thread` 同时调用。同样的编译器加上 `-fsanitize=thread` 并链接 `libtsan.so.2`，运行退出码 66，日志为 `WARNING: ThreadSanitizer: data race`，摘要指向 `buffer.h` 的 `bump()`。并发项没有因为「模型没有意见」而被跳过。

修复把 `temporary_token()` 改为返回拥有所有权的 `std::string`，并把计数器改成 `std::atomic<int>`。再用 ASan/TSan 编译运行：退出码 0，输出 `live-token` 与 `2`，日志中不再有 sanitizer 报错。

### 2. 真实 clang-tidy 要跑到，但不要替它编造成果

**直觉。** 静态分析是补充；没报出来不能假装报出来。

**原理。** 本次从 PyPI 安装 `clang-tidy==22.1.8`（[ssciwr/clang-tidy-wheel](https://github.com/ssciwr/clang-tidy-wheel)，Apache-2.0，打包的是 LLVM 官方二进制，版本与 zig c++ 的 clang 22.1.8 一致），二进制在 `cmvenv/bin/clang-tidy`，`--version` 为 `LLVM version 22.1.8`。审查用 CMake 生成的 `compile_commands.json` 作为编译数据库来运行。clang-tidy **确实执行了**，但这一次并没有单独给出能对应「悬空 Slice」或「数据竞争」的诊断；抓住这两个缺陷的是 ASan 与 TSan。`results.json` 把这一点写在 `not_verified_on_this_machine` 里，而不是改写 tidy 的输出。

官方 clang-tidy 文档说明检查器与配置，不保证某两个缺陷一定被默认检查集命中。验收顺序是：编译后的 sanitizer 必须抓住缺陷；tidy 能补充就补充，不能就如实记录。

## 常见误区

- 用流畅解释代替可触发证据。
- 模型没提到并发，审查就跳过数据竞争。
- 本机其实已有编译器与 sanitizer 运行库，却只在系统 `PATH` 里找 `g++`/`clang-tidy`，然后写成「无法运行」。
- 把「应当能被 tidy 检出」写成「tidy 已经检出」。

## 设计权衡

模型适合提出跨文件的生命周期和并发假设；sanitizer 把假设变成退出码和日志；clang-tidy 更可重复，但对这类未定义行为并不总是敏感。三者一起用，并且以实际运行记录为准。

## 动手实践

实践包在 `practice188/`。`repo/change.diff` 与完整源文件一并提供。需要 `cmvenv` 中的 cmake、ninja、ziglang、clang-tidy 22.1.8，以及系统 `libasan`/`libtsan`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice188 "$tmp_dir/"
cd "$tmp_dir/practice188"
bash test.sh
```

本次修订实测：ASan 报告 `stack-use-after-scope`，TSan 报告 `data race`，修复后两次运行干净；clang-tidy 22.1.8 已运行，但不是抓住这两处缺陷的工具。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Clang-Tidy](https://clang.llvm.org/extra/clang-tidy/)：HTTP 200，`Date` 2026-11-15 16:13:55 GMT（Asia/Shanghai 2026-11-16 00:13:55），`Last-Modified` 2026-10-14 22:34:19 GMT（Asia/Shanghai 2026-10-15 06:34:19）。第 147 课说明了 zig c++ 链接系统 sanitizer 运行库的做法，本次沿用。

## 自测题

**1. 只评论 `return Slice{token.c_str(), ...}` 而不运行 ASan，漏掉了什么？**

答：可触发证据。本次 ASan 在 `main` 读取 `s.ptr` 时给出 `stack-use-after-scope`，这才是缺陷被抓住的记录。

**2. 如果 clang-tidy 没有提到数据竞争，能否认为没有竞态？**

答：不能。本次 tidy 22.1.8 已运行但未单独报出该问题；TSan 在 `bump()` 上报告了 `data race`。并发检查不能缺席。

**3. 修复后怎样才算通过？**

答：同一套 ASan/TSan 命令再次运行，退出码为 0，日志中不再出现 sanitizer 错误。本次固定实现输出 `live-token` 与计数 `2`。

## 推荐资源

- [Clang-Tidy](https://clang.llvm.org/extra/clang-tidy/)（英文，文档，进阶，免费）：检查器与配置；是否命中以实际运行日志为准。
- 第 147 课 Sanitizer：同一套 zig c++ 与系统 sanitizer 运行库。

## 本课小结

审查意见要能被编译后的工具复现；并发检查不能缺席。clang-tidy 值得安装和运行，但不要替它编造成果。下一课是第 189 课 m-tests（Agent 驱动测试与模糊测试）。
