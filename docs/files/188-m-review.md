# 第 188/208 课：AI 代码审查与缺陷复核

> 序号 188/208 · id `m-review` · 领域 M · 阶段 3 · 难度 进阶 · 子类 研发效率
> 前置：[`m-coding`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/187.html)、[`j-sanitizer`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/147.html)
> 节点摘要：模型可提出潜在生命周期、并发和异常安全问题，但缺陷必须通过最小复现、规则或人工判断确认。流畅的解释不能代替可触发的证据。

## 导读

第 187 课强调生成代码仍要审查，第 147 课用 Sanitizer 把未定义行为变成可复现证据。本课给审查器一份含**悬空引用**和**未同步计数器**的 diff，要求每条结论带代码位置、触发条件和最小测试，并且不能因为「模型这次没提并发」就跳过并发检查。

教学假设：`buffer.h` / `use.cpp` 是为暴露缺陷构造的教学片段，不是某条业务链路的生产代码。审查器是脚本化规则，`is_live_model=false`。本机没有 `clang-tidy`，不能声称已经跑过它。

## 学习目标

1. 说明审查的上下文范围：不仅看 diff 新增行，还要看调用点如何触发。
2. 独立完成实践：为悬空引用给出位置、触发条件和最小测试；并发问题同样必须有代码证据。
3. 每条结论都有代码证据；探测并记录 `clang-tidy` 是否真的可运行。

## 为什么后端要懂

模型擅长跨文件提示「这里可能有问题」，但漏报和误报都常见。没有文件:行、触发路径和最小复现，审查意见就只是文风。静态分析更稳定，却不是每台机器都装得上，缺工具时要说缺，而不是用叙述补上。

## 核心知识点

### 1. 每条意见都要能指回代码

**直觉。** 先引用，再解释。

**原理。** `temporary_token()` 返回 `Slice{token.c_str(), token.size()}`，而 `token` 是函数内的 `std::string`，函数返回后对象销毁。`use.cpp` 随即 `std::string(s.ptr, s.len)`，这是触发点。本次记录 `dangling-slice`，`buffer.h` 中对应行的原文作为 evidence，并给出最小测试：在 ASan/UBSan 下跨过该函数后再读 `s.ptr`。

`bump()` 对非原子的 `shared_counter++` 没有互斥；`use.cpp` 用两个 `std::thread` 同时调用。即便有的模型在这次 diff 里「没有意见」，审查清单仍必须包含并发项：`concurrency_not_skipped=true`。最小测试是在 TSan 下跑这两个线程。本课没有在本机运行 ASan/TSan（与第 147 课的工具链限制一致），所以这些是**应当执行的复现步骤**，不是已经跑绿的报告。

### 2. clang-tidy 是补充，不是可以假装的步骤

**直觉。** 静态分析能稳定指出一批问题，但要真正调用二进制。

**原理。** 驱动使用 `shutil.which("clang-tidy")`，若存在则以 `clang-tidy use.cpp -- -std=c++17 -I.` 调用。本次结果 `which=None`，`clang_tidy_ran=false`，`not_verified_on_this_machine` 写明：发现来自代码证据，不是 tidy 报告。官方 clang-tidy 文档在 2026-11-16 仍然有效，不能因为本机缺失就改写成「已用 tidy 确认」。

## 常见误区

- 用流畅解释代替可触发证据，或不给出最小测试。
- 模型没提到并发，审查就跳过数据竞争。
- 本机没有 clang-tidy / Sanitizer 时，把「应当能检出」写成「已经检出」。

## 设计权衡

模型适合提出跨文件的生命周期和并发假设；clang-tidy 与 Sanitizer 更可重复。两者都需要代码位置。缺少工具时，保留假设和复现命令，比伪造一次「全部通过」更有用。

## 动手实践

实践包在 `practice188/`。`repo/change.diff` 与完整源文件一并提供。

```bash
tmp_dir=$(mktemp -d)
cp -a practice188 "$tmp_dir/"
cd "$tmp_dir/practice188"
bash test.sh
```

本次两条发现均有 file:line 与触发点，并发项未被跳过；clang-tidy 未安装，已列入未实测。`is_live_model=false`。若你的机器有 clang-tidy 或 `-fsanitize=address,thread`，应按最小测试补跑，并以那些日志为准。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Clang-Tidy](https://clang.llvm.org/extra/clang-tidy/)：HTTP 200，`Date` 2026-11-15 16:13:55 GMT（Asia/Shanghai 2026-11-16 00:13:55），`Last-Modified` 2026-10-14 22:34:19 GMT（Asia/Shanghai 2026-10-15 06:34:19）。该页说明检查器与配置，不保证本课两个缺陷一定被某个默认检查器命中；本课的验收首先是代码证据，其次才是 tidy 输出。

## 自测题

**1. 只评论 `return Slice{token.c_str(), ...}` 而不看 `use.cpp`，漏掉了什么？**

答：触发条件。悬空指针在返回时形成，在 `std::string(s.ptr, s.len)` 处被使用。本次两条 evidence 都包含调用点。

**2. 如果脚本化审查器或模型没有输出并发意见，能否认为没有竞态？**

答：不能。清单必须覆盖共享可变状态。本次即使不依赖「模型意见」，也对 `shared_counter++` 和双线程调用给出了证据。

**3. 没有 clang-tidy 时正确的表述是什么？**

答：记录二进制不存在、未运行，审查结论来自代码证据。不要写「tidy 已确认」。

## 推荐资源

- [Clang-Tidy](https://clang.llvm.org/extra/clang-tidy/)（英文，文档，进阶，免费）：检查器与配置；本机是否跑通以实际命令为准。
- 第 147 课 Sanitizer：把生命周期和竞态变成可复现的动态证据。

## 本课小结

审查意见要带位置、触发和最小复现；并发检查不能缺席。静态分析值得跑，但跑不到就要写未实测。下一课是第 189 课 m-tests（Agent 驱动测试与模糊测试）。
