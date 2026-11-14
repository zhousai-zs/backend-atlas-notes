# 第 144/208 课：Bazel 与可复现依赖

> 序号 144/208 · id `j-bazel` · 领域 J · 阶段 2 · 难度 基础 · 子类 构建与变更管理
> 前置：[`j-cmake`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/143.html)
> 节点摘要：Bazel 用显式目标图、输入声明和缓存减少重复构建；可复现性还依赖工具链及外部依赖管理。

## 导读

上一课用 CMake 把 `kvutil`、`server` 和 `kvutil_test` 拆成三个 target，靠 PUBLIC / PRIVATE 控制头路径会不会传给消费者。这一课把同一组源码迁进 Bazel，回答三个更「大规模仓库」才会尖锐的问题：谁依赖谁必须写在 `BUILD` 里；没声明的文件能不能偷偷被编译器读到；以及缓存命中到底跟什么绑定——只开缓存，并不自动等于可复现。

教学场景还是那个后台配置解析库。微信公众号后台这类 C++ 服务往往在同一个仓库里被多个二进制和测试共用，CI 和开发机如果能安全地复用编译产物，等待时间会少很多；反过来，一次错误的缓存命中会把错误的 `.o` 发给所有人。

## 学习目标

1. 解释 `BUILD` 里的 target（`cc_library` / `cc_binary` / `cc_test`）表达了什么，以及适用边界。
2. 说清 sandbox 如何阻止未声明输入成为隐式依赖，以及 hermeticity 和「仅仅启用缓存」的差别。
3. 独立完成实践：把一个 C++ 库和测试迁入 Bazel，修改公共头观察重建范围；再用磁盘缓存验证「缓存命中与编译参数」的关系。

## 为什么后端要懂

节点给出的理由是「理解大型 C++ 仓库构建机制」。在后台研发里这具体表现为：仓库大了之后，你改一个公共头，应该只重编真正依赖它的目标，而不是全量再来一遍；CI 机器编过的产物，开发机要能放心复用，前提是动作的输入（源文件、头文件、命令行、工具链）一致。Bazel 把这些关系写成显式目标图，并用 sandbox 把「编译器顺手读到的文件」从隐式依赖里剔除。

## 核心知识点

### 1. BUILD target

**直觉。** 一个 target 就是图上的一个节点：它声明自己的源文件、对外暴露的头、以及对其他 target 的依赖。构建命令写的不是「编这个目录」，而是「编这个标签」，例如 `//server:server`。

**原理。** 工作区根目录放 `MODULE.bazel`，用来标识仓库并声明外部依赖；含有 `BUILD` 文件的目录是一个 package。本课实验的依赖图如下：

```text
//kvutil:kvutil     cc_library
  srcs: kv_parse.cpp, trim.h     # trim.h 是内部头，只给库自己用
  hdrs: kv.h                     # 公共头，消费者可以 include
  include_prefix = "kvutil"      # 于是 #include "kvutil/kv.h" 成立
  local_defines = KVUTIL_INTERNAL=1   # 只作用于本库，相当于 CMake 的 PRIVATE 宏
       ^                ^
       | deps           | deps
//server:server    //tests:kvutil_test
//misc:hello       # 故意不依赖 kvutil，用来对照重建范围
```

对应的库 target 是：

```starlark
load("@rules_cc//cc:defs.bzl", "cc_library")

cc_library(
    name = "kvutil",
    srcs = ["kv_parse.cpp", "trim.h"],
    hdrs = ["kv.h"],
    include_prefix = "kvutil",
    local_defines = ["KVUTIL_INTERNAL=1"],
    visibility = ["//visibility:public"],
)
```

实验在干净目录里执行 `bazel build //server:server //tests:kvutil_test //misc:hello`，结果为 `31 processes: 22 internal, 9 linux-sandbox`，耗时 20.931s（Critical Path 11.51s）。服务输出 `server: timeout_ms=400 retry=1`，传参 `"timeout_ms=300;retry=2"` 时输出 `server: timeout_ms=300 retry=2`；`bazel test //tests:kvutil_test` 为 `1 test passes`，测试程序打印 `kvutil_test: 4 assertions passed`。

**适用边界。** `hdrs` 声明的是「依赖我的人可以正当使用的头」，`srcs` 里的头只应给本库实现用。改公共头 `kv.h`（只加了一行注释）后再次构建，执行日志里重跑的编译动作是：

```text
CppCompile //kvutil:kvutil     # kv_parse.cpp 包含了 kv.h
CppCompile //server:server     # main.cpp 包含了 kv.h
CppCompile //tests:kvutil_test # 测试同样包含了 kv.h
```

摘要为 `5 processes: 16 action cache hit, 2 internal, 3 linux-sandbox`，耗时 0.810s。`//misc:hello` 没有出现在 `CppCompile` 列表里——它不依赖 `kvutil`，公共头的变更进不了它的动作输入。作为对照，只改内部头 `trim.h` 时，重跑的只有 `Compiling kvutil/kv_parse.cpp`：server 和测试并不 `#include "trim.h"`，所以它们的编译动作不会因这次修改而失效。

另外，这次只改了注释，三个编译动作重跑之后，链接被记为 action cache hit：新生成的 `.o` 与旧的内容相同，链接动作的输入哈希没变。可见「重编范围」先看输入是否变化，再看产物内容是否真的变了。

### 2. sandbox 与 hermeticity

**直觉。** sandbox 给每个动作准备一个只包含**已声明输入**的工作目录，编译器看不到名单之外的文件。hermeticity 则更进一步：同样的源码和配置，不该因为主机上装了什么库、哪个版本的编译器而得到不同产物。

**原理。** 官方 Sandboxing 文档的表述是：Bazel 的文件系统沙箱在一个只含已知输入的工作目录里跑进程，这样工具就看不见它们不该访问的源文件（除非它们知道绝对路径）。本次实验实际用的是 `linux-sandbox`（Linux 命名空间）。反例非常直接：在 `kv_parse.cpp` 里 `#include "hidden.h"`，并把 `hidden.h` 放在同一目录，但**不**写入 `srcs` / `hdrs`。构建失败：

```text
kvutil/kv_parse.cpp:3:10: fatal error: 'hidden.h' file not found
    3 | #include "hidden.h"
undeclared build exit=1
```

文件明明在包目录里，sandbox 却没有把它挂进动作的工作目录，所以不能成为隐式依赖。把 `"hidden.h"` 加进 `srcs` 之后，同一次 `bazel build //kvutil:kvutil` 转为 `Build completed successfully`。这就是节点验收条件「未声明输入不能成为隐式依赖」的实验含义：少写一项输入，不会「碰巧编过」，而是立刻暴露。

和上一课 CMake 的 PRIVATE 头路径相比，还有一个需要记清楚的差别。`#include "trim.h"`（短名字）在 server 里会失败，报 `fatal error: 'trim.h' file not found`，因为消费者并没有得到库内部目录。但默认 C++ 编译命令里带有 `-iquote .`（工作区根），所以 `#include "kvutil/trim.h"` 仍然可能编过。`hdrs` / `srcs` 首先约束的是依赖声明和重建范围；若要在搜索路径上彻底挡住内部头，需要额外启用 `layering_check` 一类检查，或不要把内部头放在 `-iquote .` 能直接点到的位置。

Hermeticity 官方文档把隔离和「输入同一性」并列为两件事：工具应被当作源码来管理，输入集合要用明确的哈希识别。本课实验的编译器是工作区外的 `zig c++`（本机没有 g++），这本身就是一处非密封点——下一小节的缓存会把它的后果讲清楚。

### 3. 远程缓存命中

**直觉。** Bazel 把构建拆成动作，每个动作有声明的输入、输出名、命令行和环境变量。这些一起构成动作的哈希。哈希相同，就可以复用别人或上次的产物。

**原理。** 远程缓存里有两块数据：动作缓存（动作哈希 → 结果元数据）和内容寻址存储（CAS，按内容哈希存输出文件）。官方文档把本地目录做成的 `--disk_cache` 也按远程缓存来用，适合同一台机器上切换分支或多个 checkout。本课用它在单机上复现「命中 / 未命中」，不必先搭 nginx 或 bazel-remote。

实验步骤和结果：

1. `bazel build ... --disk_cache=<dir>` 写入缓存：`31 processes: 22 internal, 9 linux-sandbox`，耗时 3.655s。
2. `bazel clean` 清掉输出树，但保留 disk cache；再以相同命令构建：`31 processes: 9 disk cache hit, 22 internal`，耗时 0.525s，Critical Path 仅 0.04s。九个原先走 linux-sandbox 的动作全部命中。
3. 加上 `--copt=-DKVUTIL_LESSON=144`（源码并没有使用这个宏）：Bazel 提示 `Build option --copt has changed, discarding analysis cache`，摘要为 `5 processes: 26 action cache hit, 1 internal, 4 linux-sandbox`。四个编译动作（`kv_parse.cpp`、`main.cpp`、`kvutil_test.cpp`、`hello.cpp`）因为命令行变了而缓存未命中，必须重跑。
4. 再次 `clean`，仍带同一个 `--copt`：`31 processes: 9 disk cache hit, 22 internal`，耗时 0.436s。新参数下的结果已经被写入缓存，可以再次命中。

这就是验收条件「解释缓存命中与编译参数关系」：编译参数进入动作哈希，参数变了即使源码一字未改，编译动作也不会命中旧缓存。第 3 步里链接仍可能是 action cache hit，因为这个宏并未改变目标文件内容——命令行变的是编译动作，链接动作看的是 `.o` 的内容哈希。

## 常见误区

1. **认为启用缓存就自动得到可复现构建（节点 pitfalls）。** 缓存只保证「哈希相同则复用产物」。若哈希没覆盖到真正影响产物的东西，命中就是错的。官方 Remote Caching 的 Known issues 写得很直白：Bazel 默认不跟踪工作区外的工具；如果动作使用 `/usr/bin/` 下的编译器，两台机器上编译器不同、路径却相同，就会算出同一个动作哈希、错误地共享缓存。本课的 `zig c++` 同样是工作区外的绝对路径工具，只是把路径写进了命令行：路径相同而二进制不同时，仍然会误命中。可复现性还要靠密封工具链、锁定的外部依赖（本课用 `MODULE.bazel.lock` 钉住 `rules_cc` 0.2.28），而不是「打开了 `--disk_cache`」本身。
2. **以为目录里有头文件，编译器就一定能看见。** 没有写入 `srcs` / `hdrs` 的 `hidden.h` 会被 sandbox 挡住，表现为 `file not found`，而不是静默的隐式依赖。
3. **把 `hdrs` / `srcs` 完全等同于 CMake 的 PUBLIC / PRIVATE 搜索路径。** 重建范围和短名字 `#include "trim.h"` 的行为相近，但 `-iquote .` 仍可能让 `#include "kvutil/trim.h"` 编过。
4. **用缓存命中率反推「这次没做任何工作」。** 官方说明远程缓存还会存 stdout/stderr，不能靠终端输出判断是否命中；要以摘要里的 `disk cache hit` / `linux-sandbox` 为准。

## 设计权衡

节点给出的权衡是：声明式构建增加配置工作，换来规模化增量效率。

- **收益。** 实验里改一行公共头注释，只重编三个相关翻译单元，无关的 `//misc:hello` 不动；`hidden.h` 未声明时立刻失败，不会把隐式依赖藏进增量构建；disk cache 在 `clean` 之后把 Critical Path 从数秒降到 0.04s 量级。仓库越大、CI 与开发机共享缓存时，这个收益越明显。
- **代价。** 每个库都要写 `BUILD`，公共头和内部头要分开声明，外部依赖要进 `MODULE.bazel`。多包仓库还要处理 `visibility`（官方 C++ Tutorial 第三阶段默认 target 只对同一 `BUILD` 文件内可见）。工具链、sandbox 可写目录、缓存路径也需要配置——本课为了在没有 g++ 的机器上跑起来，就额外写了编译器包装脚本和 `--sandbox_writable_path`。
- **判断方法。** 仓库还小、工具链简单时，上一课的 CMake 目标式写法往往更轻；一旦出现「改一个头全量重编」「CI 产物不敢给开发机用」「有人靠隐式包含碰巧编过」，声明式目标图和显式输入就开始值得投入。

## 动手实践

节点实践：把一个 C++ 库和测试迁入 Bazel，修改公共头观察重建范围。实践包是 `practice144.tar.gz`，解压后运行：

```bash
cd practice144
bash test.sh
```

环境说明：本次运行的机器上没有 g++，因此实验用 Bazelisk 1.30.0 拉起的 Bazel 9.3.2（构建时间 2026-11-04 22:18:38 UTC，北京时间 2026-11-05 06:18:38），以及虚拟环境里 ziglang 0.17.1 提供的 `zig c++`（clang 22.1.8）。脚本会优先使用系统里的 g++ 或 clang++；只有两者都不存在时才回退到 `tools/zig-c++`。`tools/bazel` 是 Bazelisk，第一次运行会按 `USE_BAZEL_VERSION=9.3.2` 下载对应的 Bazel。

步骤与本次结果：

1. **读项目结构。** `proj/` 下有 `MODULE.bazel`（`bazel_dep(name = "rules_cc", version = "0.2.28")`）、`kvutil/`、`server/`、`tests/`、`misc/hello`。先在纸上画出上一节的目标图，再对照 `BUILD` 文件。
2. **首次构建。** 脚本把项目复制到全新临时目录后执行 `bazel build //server:server //tests:kvutil_test //misc:hello`，`31 processes: 22 internal, 9 linux-sandbox`，20.931s。服务与 `hello-unrelated` 输出见上。
3. **跑测试。** `bazel test //tests:kvutil_test` 结果为 `Executed 1 out of 1 test: 1 test passes`。
4. **改公共头观察重建范围（验收：重建范围跟声明的依赖走）。** 给 `kv.h` 追加一行注释后重编，三个依赖该头的 `CppCompile` 重跑，`//misc:hello` 不重编。
5. **未声明输入（验收：未声明输入不能成为隐式依赖）。** `hidden.h` 不进 `BUILD` 时编译失败，报 `hidden.h file not found`；写入 `srcs` 后通过。
6. **磁盘缓存与编译参数（验收：解释缓存命中与编译参数关系）。** `clean` 后 9 次 disk cache hit（0.525s）；加 `--copt=-DKVUTIL_LESSON=144` 后 4 个编译动作走 linux-sandbox；再 `clean` 并以相同 copt 构建，再次 9 次 disk cache hit（0.436s）。

最后一行应为 `ALL_CHECKS_PASSED`，并生成 `results.json`。延伸练习：给 `//kvutil:kvutil` 加上 `visibility = ["//server:__pkg__"]`，看测试还能不能依赖它；或打开 `--features=layering_check`，再试 `#include "kvutil/trim.h"`。

## 源码与文档阅读

节点建议阅读 Bazel — C++ Tutorial。2026-11-14 北京时间核对：

- [C++ Tutorial](https://bazel.build/tutorials/cpp) 返回 308，转到 `https://bazel.build/start/cpp`（200）。HTML 页 Date 为 2026-11-14 10:17:20 GMT（北京时间 18:17:20），Last-Modified 为 2026-11-14 09:17:27 GMT（北京时间 17:17:27）；Markdown 镜像 13731 字节，标题为 "Bazel Tutorial: Build a C++ Project"。
- 跟随阅读的 [Hermeticity](https://bazel.build/concepts/hermeticity) 转到 `https://bazel.build/basics/hermeticity`（200），Date 与 Last-Modified 均为 2026-11-14 10:18:55 GMT（北京时间 18:18:55），Markdown 5867 字节。
- [Remote Caching](https://bazel.build/remote/caching) 200，Date 2026-11-14 10:17:48 GMT（北京时间 18:17:48），Last-Modified 2026-11-14 07:39:21 GMT（北京时间 15:39:21），Markdown 16220 字节。
- [Sandboxing](https://bazel.build/docs/sandboxing) 200，Date 2026-11-14 10:18:55 GMT（北京时间 18:18:55），Last-Modified 2026-11-14 08:37:01 GMT（北京时间 16:37:01），Markdown 11450 字节。

Tutorial 中与本课直接相关的定义：workspace 由根目录 `MODULE.bazel` 标识；含 `BUILD` 的目录是 package；`BUILD` 里每条规则的一次实例是 target，指向源文件和其他 target。第二阶段用 `cc_library` + `cc_binary(deps = [...])` 演示增量：改 `hello-greet.cc` 只重编那个文件。第三阶段引入多 package 和 `visibility`。

与本课实验的差异：

1. Tutorial 示例在较新文档里仍可写空的 `MODULE.bazel` 并直接用内置规则；本实验使用 Bazel 9.3.2，必须 `bazel_dep(name = "rules_cc", version = "0.2.28")`，并在 `BUILD` 里 `load("@rules_cc//cc:defs.bzl", ...)`，否则会报 `unknown repo 'rules_cc'`。
2. Tutorial 的练习到「多 package 的 hello-world」为止，没有覆盖 sandbox 未声明输入，也没有用 `--disk_cache` 演示缓存与 `--copt` 的关系；这些是本节点的要点，实验额外补上。
3. Tutorial 假定系统已安装 Bazel 和常用 C++ 工具链；本实验在没有 g++ 的机器上用 Bazelisk 1.30.0 固定 9.3.2，并用 `zig c++` 作为 clang 前端。这使构建能跑通，但也提醒：主机编译器仍在工作区之外，不能把它当成已经密封。
4. 节点资源链接仍是 `https://bazel.build/tutorials/cpp`，当前站点会 308 到 `/start/cpp`，内容是同一篇教程。

## 自测题

**1. 为什么改 `kv.h` 会重编 `//server:server`，而改 `trim.h` 不会？`//misc:hello` 呢？**

答：`server/main.cpp` `#include "kvutil/kv.h"`，`kv.h` 在库的 `hdrs` 里，是 server 编译动作的输入；`trim.h` 只被 `kv_parse.cpp` 包含，列在库的 `srcs` 里，不进入 server 的编译输入。`//misc:hello` 与 `kvutil` 没有 `deps` 边，两个头的变更都不会让它重编。实验里改 `kv.h` 出现三个 `CppCompile`，其中没有 hello。

**2. 同事说「我开了 `--disk_cache`，所以咱们两台机器共享缓存一定是安全的」。用本课内容说明问题。**

答：缓存只按动作哈希复用产物。官方文档指出 Bazel 默认不跟踪工作区外的编译器，路径同为 `/usr/bin/...` 但版本不同的两台机器会算出相同哈希、错误命中。本课用 `--copt` 证明编译参数一旦变化，编译动作就不会命中旧缓存；反过来，若参数和路径都相同、唯独编译器二进制不同，哈希却可能不变。要可复现，还需要密封工具链和锁定的外部依赖，而不是只启用缓存。

**3. 把 `hidden.h` 放进 `kvutil/` 目录却不写进 `BUILD`，为什么不是「编译器自己会找到」？**

答：linux-sandbox 给编译动作准备的工作目录只挂已声明输入。未写入 `srcs` / `hdrs` 的 `hidden.h` 对编译器不可见，实验报 `fatal error: 'hidden.h' file not found`，exit=1。写进 `srcs` 之后同一目标才能编过。这正是为了防止未声明文件变成隐式依赖，否则改了它 Bazel 还以为构建是最新的。

## 推荐资源

- [Bazel — C++ Tutorial](https://bazel.build/tutorials/cpp)（Bazel 官方文档，英文，入门，免费；当前 308 至 `/start/cpp`）：节点推荐理由是用于核对 BUILD target 与 sandbox 与 hermeticity，将结论落实到本节点实验。
- 文中跟随阅读：[Hermeticity](https://bazel.build/basics/hermeticity)、[Remote Caching](https://bazel.build/remote/caching)（含 `--disk_cache` 与「不跟踪工作区外工具」这一已知问题）、[Sandboxing](https://bazel.build/docs/sandboxing)。

## 本课小结

Bazel 用 `BUILD` target 把源文件、公共头和依赖画成显式图：改 `kv.h` 只重编真正包含它的目标，未声明的 `hidden.h` 在 sandbox 里无法变成隐式依赖。缓存命中绑定的是动作哈希，编译参数一变，编译动作就不会复用旧产物；但启用缓存本身并不等于可复现，还要把工具链和外部依赖纳入输入。本课实验用 Bazel 9.3.2 完成了库与测试的迁移，三次验收都在真实构建里得到了对应的日志。下一课是第 145 课 j-unit（单元测试与边界设计）。
