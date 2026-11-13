# 第 129/208 课：可复现微基准

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-benchmark` |
| 英文名 | Benchmark methodology |
| 领域 / 阶段 | I / 第 3 阶段 |
| 子类 | 测量与负载 |
| 难度 | 进阶 |
| 前置节点 | [`i-metrics`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/128.html)、[`b-syntax`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/17.html) |
| 标签 | `Benchmark methodology`、`Performance` |
| 参考资料 | [Google Benchmark — User Guide](https://github.com/google/benchmark/blob/main/docs/user_guide.md)（Google Benchmark 文档，英文，进阶，免费；2026-11-14 该地址返回 HTTP 200，无跳转，最终 URL 仍是 `https://github.com/google/benchmark/blob/main/docs/user_guide.md`，760100 字节，`Content-Type` 为 `text/html; charset=utf-8`。本次抓取响应头 `Date` 为 `Fri, 13 Nov 2026 17:34:31 GMT`（北京时间 2026-11-14 01:34:31）。`<title>` 与 `og:title` 为 benchmark/docs/user_guide.md at main · google/benchmark · GitHub，正文 Markdown 一级标题为 User Guide。同一文件的 raw 地址 `https://raw.githubusercontent.com/google/benchmark/main/docs/user_guide.md` 同样返回 HTTP 200，无跳转，62568 字节，`Content-Type` 为 `text/plain; charset=utf-8`，响应头 `Date` 为 `Fri, 13 Nov 2026 17:34:32 GMT`（北京时间 2026-11-14 01:34:32）。该文件最近一次提交为 `53aaafd9f2b0005f85c302711e40fd8e4ca60e38`，作者 Ritik Mishra，提交时间 `2026-10-30T10:52:06Z`（北京时间 2026-10-30 18:52:06）；GitHub API 的 `Last-Modified` 为 `Fri, 30 Oct 2026 10:52:06 GMT`。文档没有像书籍章节那样署单一作者，它属于 google/benchmark 项目。许可证为 Apache License 2.0（`LICENSE` 返回 HTTP 200，11358 字节）；`include/benchmark/benchmark.h` 文件头写 Copyright 2015 Google Inc.（HTTP 200，1116 字节）；仓库根目录没有 `NOTICE`（HTTP 404）。正文与本课直接相关的是 *Preventing Optimization*（`DoNotOptimize` 强制把表达式的**结果**留在内存或寄存器里，但并不阻止对表达式本身的优化；已知结果甚至可能整段替换，应先物化成局部 lvalue；`ClobberMemory` 强制把挂起的全局写做完，官方例子用 `v.data()` 的 lvalue 加上 `ClobberMemory`，避免 `vector::push_back` 被删掉；`ClobberMemory` 只在 GNU 或 MSVC 系编译器上可用）、*Statistics* 与 `--benchmark_repetitions`（默认只跑一次；重复后报告均值、中位数、标准差和变异系数，并不定义置信区间）、*Runtime and Reporting Considerations*（迭代次数按最小时间动态决定，预热默认 0 秒，报告前会打印机器信息）以及 *Disabling CPU Frequency Scaling*（若看到 CPU scaling is enabled，实时测量可能有噪声并带来额外开销）。该节指向跟随文档 [Reducing Variance](https://github.com/google/benchmark/blob/main/docs/reducing_variance.md)：2026-11-14 blob 地址返回 HTTP 200，无跳转，221945 字节，响应头 `Date` 为 `Fri, 13 Nov 2026 17:35:17 GMT`（北京时间 2026-11-14 01:35:17）；raw 地址返回 HTTP 200，9541 字节，`Date` 为 `Fri, 13 Nov 2026 17:35:15 GMT`（北京时间 2026-11-14 01:35:15），正文标题为 Reducing Variance。该页说明可用 `cpupower` 把调频策略改成 performance，并用 `taskset -c 0` 把任务亲和性钉在固定 CPU 上，同时列举睿频、上下文切换、超线程 / SMT、缓存和 NUMA 等方差来源） |
| 摘要 | 微基准需控制编译优化、预热、输入分布和测量误差，防止测到已被编译器删除的工作。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「一次插入 64 个教学标题」「教学 CPU 0 / 1」「教学编译器 `AtlasTeachCC 1.0`」都是**教学假设**，用来让插入成本、死代码删除、重复采样和运行环境噪声变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表 Google Benchmark 在某台机器上的实测纳秒。配套代码是 Python 3 标准库写的教学引擎 `BenchmarkEngine`，**不是 Redis，不是真实的基准测试产品，不是网络服务，不是 Google Benchmark 库，也不是宿主机上的 g++ / clang**。它不调用真实编译器，不生成目标文件，也不读性能计数器。`AtlasTeachCC` 只是引擎里的教学编译器标识；反汇编是教学 IR，不是 `objdump` 的输出。插入成本以教学虚拟周期计：一次写入 `1`、一次扩容拷贝 `1`。开启调频时使用的倍率表、未钉核时较慢核的 `1.15`，以及教学置信区间 `mean ± 1.96 * s / sqrt(n)`，都是本课的教学模型。User Guide **没有**定义置信区间，它报告的是均值、中位数、标准差和变异系数。本课所有数字都来自 `/workspace/atlas/lessons/practice129/results.json`，是**教学虚拟周期上的数字**，不是生产 RTT，也不是把文档里的墙钟微基准或 Android 方差例子原样当成这次实验的测量结果。

## 导读

### 上一课留下的缺口

上一课的 `EchoMetricsEngine` 解决的是：在稳定窗口里记录吞吐、端到端百分位和排队，并提醒不要只用一个 QPS、也不要把平均值当成尾部体验。那一课的边界停在**服务被怎么量**，没有回答另一件更靠前的问题：若要评价一个 C++ 组件的优化是否真实有效，这次测量有没有测到本应存在的工作，数字能不能复现，一次快几个百分点够不够下结论。

一次并发扫描可以说明「吞吐何时封顶」，但不能代替一次可复现的微基准。节点把摘要写成：微基准需控制编译优化、预热、输入分布和测量误差，防止测到已被编译器删除的工作。

### 接上前置

前置 [第 128 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/128.html) `i-metrics` 留下的结论是：同一组测量要写清口径和条件，分布比单个平均数更接近用户体验。本课把这条谨慎用到更小的尺度上：先确认被测的插入循环还在，再用重复采样看差异稳不稳，最后看 CPU 频率和亲和性会怎样把同一次插入测成不同的数。

另一则前置是 [第 17 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/17.html) `b-syntax`。它讨论的是类型、表达式与控制流，本课只用到其中最直接的一点：向容器里插入元素，就是一组循环和表达式。优化器完全有可能在结果未被观察时删掉这组工作。本课不再展开那一课里的语法练习，只把「表达式算了没有、结果有没有留下来」接到 `DoNotOptimize` 的适用边界上。

User Guide 把防止优化、重复运行后的统计，以及 CPU 调频警告写成微基准的方法；跟随文档 *Reducing Variance* 再补充钉核、调频策略和其他方差来源。本课用教学向量的预留与未预留插入，把这几句话落到一组可复现的虚拟周期上，而不是复现 Google Benchmark 的二进制或某台机器的墙钟。

### 教学场景（教学假设）

假设内容后台在汇总周报标题：文章键是 `article:01:title`，标题是 `Weekly digest`。一次汇总要把 `64` 个教学标题插入一个教学向量。对比的是「先 `reserve(64)` 再插入」和「不预留、按倍增扩容插入」。测量在教学编译器 `AtlasTeachCC 1.0`、默认参数 `-O2` 下进行；先丢弃 `1` 次预热，再记录 `9` 次重复，每次循环 `100` 次，报告的是每次构建的虚拟周期。隔离环境关闭教学调频并把亲和性钉在教学 CPU 0；对照环境打开调频，或允许迁移到较慢的教学 CPU 1。所有周期都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT。

### 本课三条主线

1. **DoNotOptimize**：它强制把表达式的结果留在内存或寄存器里，常常要配合 `ClobberMemory`，才能避免插入被优化掉。它并不禁止对表达式本身做优化；已知结果可能整段消失。适用边界是：先物化成局部 lvalue，再检查反汇编确认被测代码还在。
2. **重复采样与置信区间**：默认只跑一次不够。重复之后应看均值、中位数、标准差和变异系数。本课另给一条教学置信区间，用来判断「快几个百分点」是否站得住；这条区间不是 User Guide 里的统计量。
3. **CPU 频率与亲和性**：调频打开或未钉核时，同一次插入的重复结果会散开。隔离环境提高可比性，但仍只是微基准，不能代替服务级验证。

## 学习目标

- 能解释 `DoNotOptimize` 及适用边界：它留住的是表达式的结果，不是表达式里的全部工作；对向量插入还需要让缓冲区逃逸并 `ClobberMemory`；优化后要核对教学反汇编里是否还有 `push_back` / `store`。
- 独立完成节点实践：用教学引擎对照 Google Benchmark 的方法，对比 vector 预留容量前后的插入成本，并输出教学编译器版本与参数。
- 能说明为什么单次运行快几个百分点不能宣布优化，以及隔离环境提高可比性之后，为什么还要做服务级验证。

## 为什么后端要懂

节点对「为什么」写得很干脆：评价 C++ 组件优化是否真实有效。放到本课的教学内容后台：若编译器已经把插入循环删掉，测到的「很快」并不属于预留容量；若只跑一次、又赶上调频或迁核，快 `5%` 也可能是噪声。把编译器参数、反汇编和重复采样写下来，才能判断这次优化是不是真的有效。即便微基准干净，也还要把结论放回服务级的吞吐和延迟里看。

## 核心知识点

### 1. DoNotOptimize

User Guide 的 *Preventing Optimization* 写明：`DoNotOptimize(<expr>)` 强制把 `<expr>` 的**结果**存进内存或寄存器；在 GNU 系编译器上，它还像一道对全局内存的读写屏障。它**并不**阻止对 `<expr>` 本身的优化。若结果已经已知，表达式甚至会被整段去掉，例如 `DoNotOptimize(foo(0))` 可以变成 `DoNotOptimize(42)`。文档因此给出一条适用边界：先把结果物化成局部变量，再把这个 lvalue 传进去。官方的 `vector::push_back` 例子还用 `DoNotOptimize(data)` 让 `v.data()` 逃逸，再用 `ClobberMemory()` 强迫这次写入真正发生；并注明 `ClobberMemory()` 只在 GNU 或 MSVC 系编译器上可用。

教学引擎在 `-O2` 下模拟这一边界。若插入结果未被观察、也没有逃逸和 clobber，循环按死代码删除处理，每次构建的成本是 `0`，教学 IR 里只剩下 `ret`，看不到 `push_back` 或 `store`。加上 lvalue 形式的 `DoNotOptimize` 和 `ClobberMemory` 之后，未预留插入的成本回到 `127`，IR 里重新出现 `grow`、`push_back` 和 `store`。同样在 `-O2` 下为预留插入加上逃逸后，成本是 `64`，IR 里有 `reserve v, 64` 和 `store`，没有 `grow`。`-O0` 即使不加逃逸，未预留插入仍是 `127`，工作还在。若把「结果已知」的表达式直接交给 `DoNotOptimize`，教学 IR 会变成 `const 42` / `DoNotOptimize 42`，成本是 `0`：这就是文档说的「并不阻止对表达式本身的优化」。

编译器版本与参数要和反汇编一起输出。本课写出的是 `AtlasTeachCC 1.0`、默认 `-O2`，并标明这是教学编译器，不是宿主机 g++。验收看的就是这两件事：版本和参数被记下来，被测代码在教学 IR 里确实存在。

### 2. 重复采样与置信区间

User Guide 写得很直白：默认每个基准只跑一次，而基准常常有噪声，单次结果未必代表整体行为，所以可以按 `--benchmark_repetitions` 或 `Repetitions` 重复运行。重复之后报告的是均值、中位数、标准差和变异系数。文档**没有**定义置信区间，也没有给出 `1.96 * s / sqrt(n)` 这种公式。本课的教学区间是在重复采样之后另外计算的，公式为 `mean ± 1.96 * s / sqrt(n)`，`ci_is_teaching` 为真；不要把它写成 Google Benchmark 的正式输出。

文档里预热默认是 `0` 秒，迭代次数会按最小时间动态增加，最多到 `1e9`。本课为了让数字可核对，固定先丢弃 `1` 次预热，再测 `9` 次，每次循环 `100` 次，报告每次构建的虚拟周期。这是教学选择，不是把动态迭代规则搬进引擎。

在隔离环境里，未预留插入 9 次都是 `127.0`，预留后都是 `64.0`，标准差和变异系数都是 `0.0`，教学区间退化成一个点。打开教学调频后，同一段预留插入的 9 个样本是 `64.0`、`60.8`、`67.2`、`58.88`、`69.12`、`62.08`、`65.92`、`56.96`、`71.04`，均值和中位数仍是 `64.0`，标准差 `4.7356`，变异系数 `0.074`，教学区间是 `[60.9061, 67.0939]`。把一段真实成本完全相同的「微调版」并排来看：它的第一次是 `60.8`，对比基准的第一次 `64.0`，看起来快了 `0.05`，也就是 `5%`。但 9 次之后两条均值都是 `64.0`，区间完全重合。单次快几个百分点，不够宣布优化。

### 3. CPU 频率与亲和性

User Guide 在 *Disabling CPU Frequency Scaling* 里给出警告：若 CPU scaling is enabled，基准的实时测量可能有噪声，并会带来额外开销；建议在跑基准时关闭调频，并考虑其他稳定手段。具体做法写在跟随文档 *Reducing Variance*：用 `cpupower` 把 governor 设为 performance，用 `taskset -c 0` 把任务钉在固定 CPU 上，同时还列举了睿频、迁核、超线程 / SMT、缓存和 NUMA。本课不调用这些系统命令，只用教学开关对应「调频开或关」「亲和性钉或不钉」。

隔离时（调频关、钉在教学 CPU 0），预留插入的变异系数是 `0.0`，9 次都是 `64.0`。只打开调频、仍钉核时，变异系数升到 `0.074`，均值仍是 `64.0`。只取消钉核、调频仍关时，第 3、6、9 次落到较慢的教学 CPU 1，周期变成 `73.6`，均值被抬到 `67.2`，中位数仍是 `64.0`，变异系数 `0.0714`，教学区间是 `[64.064, 70.336]`。两项同时打开时，变异系数再到 `0.1242`，均值 `67.3387`，区间 `[61.8737, 72.8036]`。隔离提高了可比性：同一段工作不再因为频率和迁核而被测成不同的数。即便在最吵的环境里，未预留插入的教学区间是 `[122.7807, 144.4696]`，与预留的 `[61.8737, 72.8036]` 仍然分开，说明 `64` 与 `127` 的差异不是调频噪声能抹平的。可是这仍然只是插入微基准，不能代替上一课那种服务级的吞吐和延迟验证。

## 常见误区

节点写出的误区是：单次运行快几个百分点就宣布优化。教学对照里，两段预留插入的真实成本都是 `64`，只是调频表的排列不同。第一次分别是 `64.0` 和 `60.8`，相对差异恰好 `0.05`。若只把这两次拿来公布「微调快了 5%」，结论是错的。9 次重复之后，均值都是 `64.0`，标准差都是 `4.7356`，教学区间都是 `[60.9061, 67.0939]`，彼此重合。要宣布优化，至少得看到重复采样后的统计，而不能只看一次。User Guide 也提醒：默认只报告一次，而基准常常有噪声。

## 设计权衡

节点写出的权衡是：隔离环境提高可比性，但仍需服务级验证。关掉调频并钉核之后，预留与未预留的均值是干净的 `64.0` 和 `127.0`，变异系数为 `0.0`，教学区间没有宽度，两组数字可以直接比。打开调频或取消钉核，变异系数分别升到 `0.074` 和 `0.0714`，两者一起打开则是 `0.1242`，同一次插入会被测成一条带子。隔离让微基准更可比，也更不像线上那台还要处理排队和尾部延迟的服务。教学引擎把 `tradeoff_isolation_still_needs_service_validation` 置为真，并不是因为它又去跑了一遍上一课的 Echo，而是要提醒：向量预留少花的那 `63.0` 个虚拟周期，不能直接写成内容后台的 QPS 或 p99 已经变好。

## 动手实践：用 Google Benchmark 对比 vector 预留容量前后的插入成本

配套代码在 [`/workspace/atlas/lessons/practice129/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice129.tar.gz)。`engine.py` 是 `BenchmarkEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_benchmark.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟周期上的值。实践按节点的表述对照 Google Benchmark 的方法来做，跑的是教学引擎，不是 Google Benchmark 库，也不是宿主机编译器。

请先自己把「预留前 / 预留后」在隔离环境里的均值、标准差和教学区间记进一张表，确认未预留是 `127.0`、预留后是 `64.0`、差值 `63.0`、比值 `1.9844`，两组教学反汇编里都有 `push_back` / `store`，预留后不再出现 `grow`。然后再核对另外三组：`-O2` 下不加逃逸时成本是 `0`、教学 IR 只剩 `ret`；调频噪声里单次 `64.0` 对 `60.8` 看起来快 `5%`，9 次之后区间重合；隔离、只调频、只取消钉核、两者都打开时，变异系数分别是 `0.0`、`0.074`、`0.0714`、`0.1242`。

插入成本按倍增扩容累计：写入一次计 `1` 个虚拟周期，扩容时把已有元素拷贝一次也计 `1`。`64` 次未预留插入会拷贝 `0+1+2+4+8+16+32 = 63` 次，总成本 `127`；先 `reserve(64)` 则没有拷贝，总成本 `64`。教学置信区间采用 `mean ± 1.96 * s / sqrt(n)`，这是写在引擎里的教学取法，不是 User Guide 规定的算法。预热丢弃 `1` 次、测量 `9` 次、每次 `100` 轮，也是教学选择；文档默认预热是 `0`，迭代次数按最小时间动态决定。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_compiler_version_emitted`、`acceptance_disassembly_shows_work`、`practice_compares_reserve`、`pitfall_single_run_not_enough`、`tradeoff_isolation_still_needs_service_validation`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_benchmark_product`、`not_google_benchmark`、`not_host_gxx`、`cycles_are_teaching`。编译器一行是 `AtlasTeachCC` / `1.0` / `-O2`，并写明 `compiler_is_teaching`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Google Benchmark — User Guide 中与 Benchmark methodology 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://github.com/google/benchmark/blob/main/docs/user_guide.md](https://github.com/google/benchmark/blob/main/docs/user_guide.md) 返回 200，无跳转，760100 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Fri, 13 Nov 2026 17:34:31 GMT`（北京时间 2026-11-14 01:34:31）。`<title>` 为 benchmark/docs/user_guide.md at main · google/benchmark · GitHub，正文标题是 User Guide。同一内容的 raw 文件 62568 字节，同样是 200、无跳转。最近一次触及该文件的提交是 `53aaafd9f2b0005f85c302711e40fd8e4ca60e38`，作者 Ritik Mishra，时间为 2026-10-30 18:52:06（北京时间）。项目许可证为 Apache 2.0；`NOTICE` 返回 404。User Guide 在 *Disabling CPU Frequency Scaling* 和 Benchmarking Tips 里指向跟随文档 [reducing_variance.md](https://github.com/google/benchmark/blob/main/docs/reducing_variance.md)，该页 2026-11-14 返回 200，raw 长度为 9541 字节，标题为 Reducing Variance。本课实际阅读了这两份文档，没有再把 *Additional Tooling Documentation*（`tools.md`）或 LLVM Benchmarking tips 算进结论。

教学实验按以下方式使用它，并记下差异。

- **DoNotOptimize 与 ClobberMemory**：指南给出了定义、适用边界和 `vector::push_back` 示例。本课用教学 IR 对照「无逃逸则循环消失、加上 lvalue 逃逸和 clobber 则 `store` 还在」，并用 `DoNotOptimize(42)` 对照「结果已知时表达式本身可以被替换」。本课没有编译真实的 `benchmark::DoNotOptimize` 实现，也没有声称教学 IR 等价于某一版头文件里的内联汇编。`ClobberMemory` 在真实库里只用于 GNU 或 MSVC 系编译器；教学引擎在模型里始终提供这个开关。
- **重复采样与统计**：指南在重复后报告均值、中位数、标准差和变异系数，默认重复次数是 1，预热默认 0 秒，迭代次数按最小时间动态选择。本课固定 1 次预热、9 次测量、100 次循环，并额外给出教学置信区间；区间公式不是指南规定的输出。指南里的自定义统计、只报告聚合、复杂度拟合，本课都没有做。
- **CPU 频率与亲和性**：指南给出调频警告，并把具体做法放到 *Reducing Variance*：`cpupower frequency-set --governor performance`、`taskset -c 0`，以及睿频、迁核、SMT、缓存、NUMA。本课只用教学倍率和两颗教学 CPU 来对照「关调频并钉核 / 开调频 / 不钉核」，不执行这些系统命令，也不复现该页 Android 一节里 32% 带宽差这类墙钟数字。较慢核的 `1.15` 和调频表 `(100, 95, 105, 92, 108, 97, 103, 89, 111)` 是为了让均值和 `5%` 单次差异可核对而选定的教学参数。
- **时间含义**：Google Benchmark 测量的是真实的 CPU 时间或墙钟时间。本课全部是教学虚拟周期，不是生产 RTT，也不是某次 `BM_memcpy` 示例里的纳秒。报告前的机器前言、perf 计数器、内存管理器和多线程基准，教学引擎都没有做，它们的数字不会进入 `results.json`。
- **编译器与反汇编**：验收要求输出编译器版本与参数，并检查反汇编确认被测代码存在。本课输出的是教学编译器 `AtlasTeachCC 1.0` 和教学 IR，明确标注不是宿主机 g++，也不是真实目标文件的反汇编。

## 自测题

**题 1.** `DoNotOptimize` 的适用边界是什么？怎样用编译器版本、参数和反汇编确认这次测到的是插入工作，而不是已被删除的循环？

<details>
<summary>答案</summary>

`DoNotOptimize` 强制把表达式的结果留在内存或寄存器里，并不保证表达式本身不被优化。指南里的反例是 `DoNotOptimize(foo(0))` 变成 `DoNotOptimize(42)`。正确做法是先把结果放进局部 lvalue，再传进去。对向量插入，还要让缓冲区逃逸，并用 `ClobberMemory` 强迫写入发生，这与指南中 `v.data()` 加 `push_back` 的例子一致。

教学实验把这条边界写成三组对照。`AtlasTeachCC 1.0`、`-O2`、不加逃逸也不 clobber 时，未预留插入的成本是 `0`，教学 IR 只有 `ret`，`work_present` 为假：这时若去对比预留，测到的是空工作，不是插入。同样在 `-O2` 下加上 lvalue 形式的 `DoNotOptimize` 和 `ClobberMemory` 后，未预留成本回到 `127`，IR 里有 `grow`、`push_back` 和 `store`；预留后成本是 `64`，IR 里有 `reserve v, 64` 和 `store`，没有 `grow`。`-O0` 即使不加逃逸，成本仍是 `127`，工作还在。把已知结果直接交给 `DoNotOptimize` 时，IR 变成 `const 42`，成本再次是 `0`。

验收因此要求两件事一起看：结果里要写出编译器标识、版本和参数（本课是 `AtlasTeachCC`、`1.0`、`-O2`，并标明这是教学编译器）；被保留下来的那两组插入，教学反汇编里必须还能看到 `push_back` / `store`。只看一个「很快」的数字，不够证明优化有效。

</details>

**题 2.** 为什么单次运行快几个百分点，不能宣布这次优化有效？重复采样之后应该看哪些数？本课的置信区间和 User Guide 里的统计有什么不同？

<details>
<summary>答案</summary>

在打开教学调频的环境里，两段真实成本同为 `64` 的预留插入，第一次分别是 `64.0` 和 `60.8`，相对差异是 `0.05`。只看这一对数字，会得出「微调快了 5%」。把重复加到 9 次之后，两组样本只是同一张倍率表的不同排列，均值都是 `64.0`，中位数都是 `64.0`，标准差都是 `4.7356`，变异系数都是 `0.074`，教学区间都是 `[60.9061, 67.0939]`，完全重合。那 5% 来自一次噪声，不是优化。

User Guide 在重复后报告均值、中位数、标准差和变异系数，并提醒默认只跑一次往往不够。它没有定义置信区间。本课额外使用 `mean ± 1.96 * s / sqrt(n)` 作为教学区间，用来判断两条带子有没有分开；`ci_is_teaching` 为真，不要把它写成指南的正式接口。隔离环境里标准差是 `0.0`，区间退化成点估计 `64.0` 和 `127.0`，这时差异来自预留本身，而不是一次偶然的快。

</details>

**题 3.** CPU 频率和亲和性怎样影响可比性？为什么隔离环境仍然不能代替服务级验证？请用预留前后的成本说明。

<details>
<summary>答案</summary>

隔离时关闭教学调频、并把任务钉在教学 CPU 0，预留插入 9 次都是 `64.0`，变异系数 `0.0`。只打开调频后，样本在 `56.96` 到 `71.04` 之间散开，变异系数 `0.074`，均值仍是 `64.0`。只取消钉核时，三次落到较慢的教学 CPU 1，周期是 `73.6`，均值被抬到 `67.2`，变异系数 `0.0714`。两项一起打开，变异系数再到 `0.1242`，均值 `67.3387`，教学区间是 `[61.8737, 72.8036]`。指南把这类现象写成：调频打开时实时测量可能有噪声；跟随文档则建议用 performance 策略和 `taskset` 钉核。本课的倍率和 `1.15` 是教学对照，不是把 `cpupower` 或某次墙钟测量搬过来。

隔离之后，预留与未预留的对比变得干净：`64.0` 对 `127.0`，差 `63.0`，比值 `1.9844`。即便在最吵的环境里，两组教学区间 `[61.8737, 72.8036]` 与 `[122.7807, 144.4696]` 仍然不重合，说明预留少掉的那次扩容拷贝不是噪声。可是这些数字量的是「构建一个 64 元教学向量要多少虚拟周期」，不是内容后台在真实并发下的 QPS 或 p99。隔离提高了微基准的可比性，仍要把结论放回服务级验证里看。这与节点写的权衡一致。

</details>

## 推荐资源

- [Google Benchmark — User Guide](https://github.com/google/benchmark/blob/main/docs/user_guide.md)（Google Benchmark 文档，英文，进阶，免费）：用于核对 `DoNotOptimize`、重复采样与统计、预热和调频警告，并将结论落到本节点的预留对照实验。2026-11-14 该地址返回 200，无跳转，760100 字节，`<title>` 为 benchmark/docs/user_guide.md at main · google/benchmark · GitHub，正文标题为 User Guide，响应头 `Date` 为 `Fri, 13 Nov 2026 17:34:31 GMT`。同一文件 raw 长度为 62568 字节。文档定义 `DoNotOptimize` / `ClobberMemory` 的适用边界，并在重复后报告均值、中位数、标准差和变异系数；并不定义置信区间。本课的教学区间、固定预热与迭代次数、教学编译器和教学 IR，都是对照，不是该库的正式接口。
- [Google Benchmark — Reducing Variance](https://github.com/google/benchmark/blob/main/docs/reducing_variance.md)（同一项目的跟随文档，英文，进阶，免费）：User Guide 在关闭 CPU 调频一节指向它。2026-11-14 raw 地址返回 200，9541 字节，标题为 Reducing Variance。该页给出 `cpupower` 与 `taskset -c 0` 等降低方差的做法。本课只吸收「频率和亲和性会影响可比性」这一结论，不执行这些命令，也不把页内的墙钟例子写入 `results.json`。

## 本课小结

- 微基准要控制编译优化、预热、输入和测量误差，避免测到已被编译器删除的工作。不要用一次看起来很快的数字评价优化是否有效。
- `DoNotOptimize` 留住的是表达式的结果，不是表达式里的全部工作。`-O2` 下不加逃逸时，未预留插入的成本是 `0`，教学 IR 只剩 `ret`；加上 lvalue 和 `ClobberMemory` 后，成本回到 `127`，`push_back` / `store` 还在。结果已知时，`DoNotOptimize(42)` 仍可能测不到原来的工作。
- 先 `reserve(64)` 再插入，隔离环境下均值是 `64.0`；不预留是 `127.0`，差 `63.0`，比值 `1.9844`。验收要同时输出教学编译器 `AtlasTeachCC 1.0`、`-O2`，并检查反汇编里被测代码还在。
- 默认只跑一次不够。调频噪声里，同成本的两段预留插入第一次相差 `5%`（`64.0` 对 `60.8`），9 次之后均值都是 `64.0`，教学区间 `[60.9061, 67.0939]` 重合。User Guide 报告均值、中位数、标准差和变异系数，并不定义置信区间；本课的区间是教学取法。
- 关闭调频并钉核，变异系数是 `0.0`；只开调频是 `0.074`，只取消钉核是 `0.0714`，两者一起是 `0.1242`。隔离提高可比性，仍需服务级验证：这些虚拟周期不能代替上一课的 QPS 与 p99。
- 这些是教学虚拟周期上的数字，不是生产 RTT，也不是 Google Benchmark 的墙钟测量。`BenchmarkEngine` 不是 Redis，也不是 Google Benchmark 库或宿主机 g++。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「`DoNotOptimize` 并不阻止对表达式本身的优化」「单次结果未必代表整体行为」以及「CPU scaling is enabled 时实时测量可能有噪声」这几段，不要把一次快几个百分点当成优化已经成立，也不要把隔离环境里的微基准直接写成服务已经变快。
