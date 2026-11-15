# 第 187/208 课：Coding Agent 辅助 C++ 开发

> 序号 187/208 · id `m-coding` · 领域 M · 阶段 2 · 难度 基础 · 子类 研发效率
> 前置：[`l-pi`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/183.html)、[`j-unit`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/145.html)
> 节点摘要：将需求转为可验证任务，让 Agent 阅读工程约定、修改代码并运行构建测试，开发者审查关键语义和证据。自动化减少重复劳动，但不能省去关键设计审查。

## 导读

从本课起进入领域 M（Backend + AI）。第 183 课提供了可扩展的诊断宿主，第 145 课强调测试应钉住契约而不是当前实现的偶然输出。本课的任务是：给一个教学用 KV 存储增加 `EXISTS` 命令，要求补协议测试并说清它对键生命周期的影响，并且**必须真实编译、链接并跑 ctest**。

教学假设：`kv_exists` 是为讲协议和代码审查构造的内存表，不是腾讯或微信的存储组件。实践中的「Agent」是脚本化改写，`is_live_model=false`，不能写成已经用真实编码模型改过仓库。

本课曾有一版只调用了系统 `PATH` 里的 `cmake`/`g++`，在没有先盘点 `/workspace/atlas/cmvenv` 的情况下把构建写成「未成功」。第 143–152 课已经使用同一套工具链完成过真实 C++ 构建。本次修订在开工前先清点：`cmake` 4.4.4、`ninja` 1.13.2、`ziglang` 0.17.1（`zig c++` 报告为 clang 22.1.8），然后用它们完成配置、编译和 ctest。旧稿另行保留，本页与实践包文件名不变。

## 学习目标

1. 说明编码任务的边界：该改哪些文件、以什么协议为验收、哪些文件禁止动。
2. 独立完成实践：为 KV 增加 `exists`，解释它不创建键；协议测试必须反映这一生命周期。
3. 亲自复现文档中的构建命令，并检查无关文件未被修改。构建是否通过以 cmake/ctest 的退出码为准，不能用 Python 协议脚本或另一种语言的编译器冒充。

## 为什么后端要懂

Coding Agent 最容易越界的不是「没写出函数」，而是改了不该改的文件、用实现细节当测试、或者在本机其实已有工具链时宣称「构建无法运行」。开工前要先盘点已安装的编译器和 CMake，包括虚拟环境里的包。

## 核心知识点

### 1. 先把需求写成协议，再允许改代码

**直觉。** `EXISTS` 是成员查询，不是「没有就插入」。

**原理。** `tests/protocol.md` 规定：`EXISTS <key>` 在键存在时返回 `1`，否则 `0`，且不得创建该键。起始树的 `handle_line` 把 `EXISTS` 落成 `ERR unknown command`，但 `tests/test_kv.cpp` 已经写好了对应断言——测试先于实现。脚本化 Agent 只改了 `include/kv.h`（声明 `bool exists() const`）和 `src/kv.cpp`（`const` 查找 `data_`，并在 `handle_line` 中路由 `EXISTS`）。生命周期说明写在 `reports/lifecycle.md`：只有 `SET` 创建或更新条目，随后对缺失键的 `GET` 仍是 `MISSING`。

**边界。** `tests/protocol_check.py` 只能检查协议格式和「EXISTS 不创建键」这一属性，**明确不是 C++ 编译器，也不能代替 `kv_test`**。它只是补充，真正的验收是下面的 ctest。

### 2. 亲自复现构建命令，并且不碰无关文件

**直觉。** 「构建通过」必须是那条命令的退出码，不是叙述。

**原理。** 本课把 `PATH` 指向 `cmvenv/bin` 和实践包内的 `tools/zig-c++`，再执行：

```
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build
ctest --test-dir build --output-on-failure
```

CMake 识别到的 CXX 是 Clang 22.1.8。对**未打补丁**的起始树，编译成功，但 ctest 失败（退出码 8），输出 `FAIL exists present got=[ERR unknown command] want=[1]` 以及 `exists absent` 同样失败。打上 `exists()` 之后再次配置并构建，ctest 报告 `100% tests passed`。`docs/UNRELATED.md` 的全文与哈希均未改动，允许变更的文件只有头文件和 `kv.cpp`。

这次用的不是系统 `g++`。工具链来自与第 143–152 课相同的 cmvenv：CMake 4.4.4（Kitware）、Ninja 1.13.2、ziglang 0.17.1。`results.json` 里写明了路径和版本，并把「系统 g++/clang++」列为未使用的编译器，而不是把构建本身写成未实测。

## 常见误区

- 把生成代码直接当作已经通过审查的代码。
- 为让当前实现「通过」而改断言，或让 `EXISTS` 在缺失时插入键。
- 只在系统 `PATH` 里找 `g++`，不盘点已经装在虚拟环境里的 CMake/Ninja/zig，然后宣称无法构建。
- 用 Python 协议检查冒充 C++ 测试已通过。
- 顺便改了 README、文档或其他无关文件却不声明。

## 设计权衡

自动化能减少机械编辑，但协议语义、生命周期和构建证据仍要人看。把测试先写进仓库，比事后根据 Agent 输出补断言更安全。选用 zig c++ 是为了在没有系统 g++ 时仍然使用真实的 Clang 前端完成编译，而不是换一种语言交差。

## 动手实践

实践包在 `practice187/`。起始仓库在 `repo/`，运行时复制到 `workspace/repo/`。需要已安装的 `cmvenv`（cmake、ninja、ziglang）。

```bash
tmp_dir=$(mktemp -d)
cp -a practice187 "$tmp_dir/"
cd "$tmp_dir/practice187"
bash test.sh
```

本次修订实测：起始树 ctest 失败，补上 `exists()` 后 ctest 通过，无关文件未改。`is_live_model=false`。Python 协议检查仍然通过，且继续标注「不是编译器」。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）核对 [Pi SDK integration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)：raw 200，`Date` 2026-11-15 16:13:45 GMT（Asia/Shanghai 2026-11-16 00:13:45）。该文档讲的是会话与工具装配，并不规定本课的 EXISTS 协议；本课额外要求「亲自调用构建命令」和「无关文件不变」。本课实践没有再跑一遍 Pi 会话（见第 183 课）。

## 自测题

**1. 为什么 Python 协议检查通过，仍不能单独写成「C++ 测试已通过」？**

答：它只验证消息契约，不编译、不链接、也不跑 `kv_test`。本次验收以起始失败、修补后 `100% tests passed` 的 ctest 退出码为准。

**2. EXISTS 对一个尚不存在的键返回 0 之后，GET 该键应得到什么？**

答：`MISSING`。EXISTS 不得创建键，否则生命周期被偷偷改变。

**3. 系统 `PATH` 里没有 `g++`，是否就可以写「无法复现构建」？**

答：不行。应先盘点已有工具。本次 `cmvenv` 中已有 cmake 4.4.4 与 zig c++（clang 22.1.8），构建和 ctest 都实际跑过。

## 推荐资源

- [Pi SDK integration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)（英文，文档，进阶，免费）：任务宿主与工具边界，不能代替本仓库的构建命令。
- 本课 `repo/README.md` 与 `tests/protocol.md`：验收时实际执行的构建与协议合同。
- 第 143 课 j-cmake：同一套 cmake/ninja/zig 工具链的用法。

## 本课小结

编码 Agent 的验收是：协议正确、无关文件不动、构建命令被真实调用且退出码符合预期。开工前要先清点本机已有的 CMake 与编译器。下一课是第 188 课 m-review（AI 代码审查与缺陷复核）。
