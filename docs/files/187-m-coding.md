# 第 187/208 课：Coding Agent 辅助 C++ 开发

> 序号 187/208 · id `m-coding` · 领域 M · 阶段 2 · 难度 基础 · 子类 研发效率
> 前置：[`l-pi`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/183.html)、[`j-unit`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/145.html)
> 节点摘要：将需求转为可验证任务，让 Agent 阅读工程约定、修改代码并运行构建测试，开发者审查关键语义和证据。自动化减少重复劳动，但不能省去关键设计审查。

## 导读

从本课起进入领域 M（Backend + AI）。第 183 课提供了可扩展的诊断宿主，第 145 课强调测试应钉住契约而不是当前实现的偶然输出。本课的任务是：给一个教学用 KV 存储增加 `EXISTS` 命令，要求补协议测试并说清它对键生命周期的影响。

教学假设：`kv_exists` 是为讲协议和代码审查构造的内存表，不是腾讯或微信的存储组件，也不与任何线上 KV 接口对应。实践中的「Agent」是脚本化改写，`is_live_model=false`，不能写成已经用真实编码模型改过仓库。

## 学习目标

1. 说明编码任务的边界：该改哪些文件、以什么协议为验收、哪些文件禁止动。
2. 独立完成实践：为 KV 增加 `exists`，解释它不创建键；协议测试必须反映这一生命周期。
3. 亲自复现文档中的构建命令，并检查无关文件未被修改。若本机没有编译器，必须如实记录，而不是用 Python 或 rustc 冒充 C++ 构建已通过。

## 为什么后端要懂

Coding Agent 最容易越界的不是「没写出函数」，而是改了不该改的文件、用实现细节当测试、或者在没有编译器的环境里宣称「构建已通过」。任务说明书和仓库约定要先于模型输出。

## 核心知识点

### 1. 先把需求写成协议，再允许改代码

**直觉。** `EXISTS` 是成员查询，不是「没有就插入」。

**原理。** `tests/protocol.md` 规定：`EXISTS <key>` 在键存在时返回 `1`，否则 `0`，且不得创建该键。起始树的 `handle_line` 把 `EXISTS` 落成 `ERR unknown command`，但 `tests/test_kv.cpp` 已经写好了对应断言——测试先于实现。脚本化 Agent 只改了 `include/kv.h`（声明 `bool exists() const`）和 `src/kv.cpp`（`const` 查找 `data_`，并在 `handle_line` 中路由 `EXISTS`）。生命周期说明写在 `reports/lifecycle.md`：只有 `SET` 创建或更新条目，`exists()` 是 const 查询，随后对缺失键的 `GET` 仍是 `MISSING`。

**边界。** 本课附带的 `tests/protocol_check.py` 只能检查协议格式和「EXISTS 不创建键」这一属性，**明确不是 C++ 编译器，也不能代替 `kv_test`**。它在本机通过，只说明契约本身被理解，不说明 `g++` 已经把 `test_kv.cpp` 跑绿。

### 2. 亲自复现构建命令，并且不碰无关文件

**直觉。** 「构建通过」必须是那条命令的退出码，不是叙述。

**原理。** README 中的命令是 `cmake -S . -B build`、`cmake --build build`、`ctest --test-dir build --output-on-failure`。实践驱动**实际调用**了这三条，并记录 `shutil.which`：本次机器上 `cmake`、`g++`、`clang++`、`c++`、`ctest` 均为 `None`，configure 以 `FileNotFoundError: cmake` 失败，后续 build/ctest 被标记为跳过。`cpp_build_succeeded=false`，`not_verified_on_this_machine` 写明端到端 C++ 构建与测试未在本机完成。有编译器的环境应按同一命令复现，而不是换成 rustc 或只跑 Python。

文件哈希对照：`docs/UNRELATED.md`（含 `UNRELATED_DOC_MARKER_DO_NOT_EDIT`）以及 README、CMakeLists、`main.cpp`、测试与协议文档均未修改；仅允许变更的实现文件是头文件和 `kv.cpp`。

## 常见误区

- 把生成代码直接当作已经通过审查的代码。
- 为让当前实现「通过」而改断言，或让 `EXISTS` 在缺失时插入键。
- 本机没有 `g++`/`cmake` 时，用 Python 协议检查或另一种语言的编译冒充 C++ 构建成功。
- 顺便改了 README、文档或其他无关文件却不声明。

## 设计权衡

自动化能减少机械编辑，但协议语义、生命周期和构建证据仍要人看。把测试先写进仓库，比事后根据 Agent 输出补断言更安全。

## 动手实践

实践包在 `practice187/`。起始仓库在 `repo/`，运行时复制到 `workspace/repo/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice187 "$tmp_dir/"
cd "$tmp_dir/practice187"
bash test.sh
```

本次：`exists` 已声明并实现，无关文件未改，Python 协议检查通过且标注「不是编译器」，构建命令已调用但因缺少工具链未成功。`is_live_model=false`。在有 CMake 与 C++ 编译器的机器上，应继续运行上述三条命令，并以它们的退出码为准。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）核对 [Pi SDK integration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)：raw 200，`Date` 2026-11-15 16:13:45 GMT（Asia/Shanghai 2026-11-16 00:13:45）。该文档讲的是会话与工具装配，并不规定本课的 EXISTS 协议；本课额外要求「亲自调用构建命令」和「无关文件不变」。本课实践没有再跑一遍 Pi 会话（见第 183 课），以避免把编码任务绑死在单一宿主上。

## 自测题

**1. 为什么 Python 协议检查通过，仍不能在汇报里写「C++ 构建已通过」？**

答：它只验证消息契约，不编译、不链接、也不跑 `kv_test`。本次 `cmake` 不存在，`cpp_build_succeeded=false`，未实测项已列出。

**2. EXISTS 对一个尚不存在的键返回 0 之后，GET 该键应得到什么？**

答：`MISSING`。EXISTS 不得创建键，否则生命周期被偷偷改变。

**3. 怎样证明无关文件没被改？**

答：对任务开始前的哈希或全文做对照。本次 `docs/UNRELATED.md` 的内容与哈希均不变，意外变更列表为空。

## 推荐资源

- [Pi SDK integration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)（英文，文档，进阶，免费）：任务宿主与工具边界，不能代替本仓库的构建命令。
- 本课 `repo/README.md` 与 `tests/protocol.md`：验收时实际执行的构建与协议合同。

## 本课小结

编码 Agent 的验收是：协议正确、无关文件不动、构建命令被真实调用。没有编译器就如实记录，绝不拿别的语言或协议脚本冒充。下一课是第 188 课 m-review（AI 代码审查与缺陷复核）。
