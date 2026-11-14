# 第 140/208 课：性能回归与预算

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-regression` |
| 英文名 | Performance regression |
| 领域 / 阶段 | I / 第 5 阶段 |
| 子类 | 回归与容量 |
| 难度 | 专家 |
| 前置节点 | [`i-benchmark`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/129.html)、[`i-load`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/130.html) |
| 标签 | `Performance regression`、`Performance` |
| 参考资料 | [Google Benchmark — Comparing Benchmarks](https://github.com/google/benchmark/blob/main/docs/user_guide.md)（Google Benchmark 项目文档，英文，专家，免费）：节点给出的地址。2026-11-14 用 curl 核对，该地址返回 HTTP 200，无跳转，最终 URL 仍是该地址，786527 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Sat, 14 Nov 2026 02:19:02 GMT`（北京时间 2026-11-14 10:19:02）；`<title>` 为 benchmark/docs/user_guide.md at main · google/benchmark · GitHub。同一文件的 raw 地址返回 HTTP 200，无跳转，62568 字节，`Content-Type` 为 `text/plain; charset=utf-8`，`Date` 为 `Sat, 14 Nov 2026 02:19:04 GMT`（北京时间 2026-11-14 10:19:04）。最近一次提交为 `53aaafd9f2b0005f85c302711e40fd8e4ca60e38`，作者 Ritik Mishra，提交时间 `2026-10-30T10:52:06Z`（北京时间 2026-10-30 18:52:06）。正文一级标题为 User Guide；该文档没有像书籍那样署单一作者，属于 google/benchmark 项目；许可证为 Apache License 2.0（`LICENSE` raw 返回 HTTP 200，11358 字节）。User Guide 的 Result comparison 只写 “It is possible to compare the benchmarking results.” 并指向 docs/tools.md；它本身没有定义「噪声阈值」「性能预算」或「阻断门槛」。真正讲比较模式、相对变化和 Mann–Whitney U test 的是它指向的 tools.md（2026-11-14 raw 地址返回 HTTP 200，19213 字节；`compare.py` 默认 `--alpha` 为 0.05，`report.py` 的 `UTEST_MIN_REPETITIONS = 2`、`UTEST_OPTIMAL_REPETITIONS = 9`）。本课只把这一个地址列为推荐资源。 |
| 摘要 | 把吞吐、尾延迟、CPU/请求和内存/连接纳入可重复基线，避免局部优化牺牲整体目标。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest #N`」「固定数据集 200 个键」「固定轨迹 2000 次请求」「教学提交 `c0e1a01` / `c1e1a02` / `c2e1b03` / `c3e1c04`」都是**教学假设**，用来让数据集哈希、环境元数据、重复噪声、预算门槛和回归阻断变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `RegressionEngine`，**不是 Redis，不是 redis-py，不是真实 KV 服务，不是网络服务，不是 Google Benchmark 库，不是 compare.py，不是 scipy，也不是宿主机上的墙钟基准**。时间单位是教学虚拟微秒，CPU 是教学周期，内存是教学字节。教学主机名 `atlas-teach-ci-1`、内核 `Linux 6.8.0-teach`、隔离环境调频策略 `performance`、共享 CI 策略 `schedutil`，都是教学假设，不是真实机器，也不是真实 git 对象。U 检验是教学引擎对 9+9 样本做的精确枚举，不是 `scipy.stats.mannwhitneyu`。噪声用每轮重复的教学倍率表模拟，不是从 `/proc` 或 `cpufreq` 读到的。本课所有实验数字都来自 `/workspace/atlas/lessons/practice140/results.json`；教学场景里的服务成本、CPU 成本和倍率表是引擎中的教学参数，用来生成这些实验数字。全部是**教学虚拟时钟上的数字**，不是生产 RTT，也不是把文档示例里的 36 ns、14.42% 或 Android 一节的带宽数字当成这次实验的测量结果。

## 导读

### 上一课留下的缺口

上一课 [第 139 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/139.html) 把异步完成做成了可观察的吞吐，但没有回答「改完一轮代码，怎样判断性能是真的退了还是噪声，以及用哪些指标阻断提交」。那一课的 `UringEngine` 把重点放在队列深度、完成项、取消竞态和缓冲区寿命上，窗口里的吞吐是同一份代码在不同并行度下的结果。它没有跨提交的概念，也没有预算表。

节点把摘要写成：把吞吐、尾延迟、CPU/请求和内存/连接纳入可重复基线，避免局部优化牺牲整体目标。一次基准能说明「当前这版有多快」，回归基线要回答的是另一件事：下一版和上一版之间，哪些变化是噪声，哪些变化已经越过了必须阻断的线。

### 接上前置

前置 [`i-benchmark`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/129.html) 留下的结论是：微基准要先确认被测工作还在，再用重复采样和受控环境判断优化是否站得住；隔离环境提高了可比性，但仍要回到服务级验证。本课把「重复采样」和「环境元数据」从一次微基准扩展到多个提交：固定数据集、固定请求轨迹、每个提交跑 9 次，并把数据集哈希、提交号、内核和调频策略一起写进基线。

前置 [`i-load`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/130.html) 留下的结论是：尾延迟要贴近业务到达过程，只记录成功请求的延迟会让停顿和错误藏起来。本课把这条纪律落到回归报告上：p99 与错误率必须一起看，不能只报成功路径的 p99 和吞吐。

两条前置课分别解决了微基准复现和负载模型，本课把它们接到跨提交的回归基线与预算门槛上。Google Benchmark User Guide 的 Result comparison 本身只有一句话，并把具体比较交给它指向的 `docs/tools.md`；因此本课读文档时，会把 User Guide 的上下文、重复和运行注意事项，与 tools.md 里的 compare.py、相对变化和 U 检验一起对照。

### 教学场景（教学假设）

假设内容后台用教学 KV 保存文章标题，键形如 `article:01:title`，标题形如 `Weekly digest #N`。固定数据集有 `200` 个键，数据集哈希为 `0f48bbc37e78af58`。固定请求轨迹有 `2000` 次请求（`seed=42`）：`90%` 是 GET，`10%` 是 SET，`80%` 的流量落在最热的 `20` 个键上，热键请求共 `1600` 次，因此整条轨迹是 `get_count=1800`、`set_count=200`、`hot_count=1600`。基线 GET 热键是 `80` 教学微秒、GET 冷键是 `120` 教学微秒、SET 是 `200` 教学微秒；CPU 是 GET `50` 教学周期、SET `100` 教学周期；每条连接内存是 `4096` 教学字节；超时是 `2000` 教学微秒。每个提交都在同一条轨迹上跑 `9` 次重复。

四个教学提交如下。`c0e1a01` 是基线；`c1e1a02` 是只改名的重构，成本与 `c0` 相同；`c2e1b03` 在每次 SET 上做一遍全表过期扫描，每个键额外 `2` 微秒和 `2` 周期，`200` 个键共增加 `400` 微秒和 `400` 周期；`c3e1c04` 用投机缓存把成功路径的 GET/SET 降到 `40` 微秒、`20` 周期，但每 `50` 个请求有 `1` 个会等到超时 `2000` 微秒后出错，错误率为 `0.02`，每条连接内存升到 `65536` 教学字节。

隔离环境的两组倍率是同一组百分数的两种排列，均值都是 `100`：A 为 `(99, 100, 101, 98, 102, 100, 99, 101, 100)`，B 为 `(101, 99, 100, 102, 98, 100, 101, 99, 100)`。共享 CI 的倍率更散：A 为 `(85, 90, 95, 100, 100, 105, 110, 115, 100)`，B 为 `(80, 88, 96, 104, 112, 120, 90, 100, 110)`。隔离环境元数据是 `hostname=atlas-teach-ci-1`、`cpus=8`、`governor=performance`、`kernel=Linux 6.8.0-teach`、`commit_id=c0e1a01`、`timeout_us=2000`；共享 CI 只把 `governor` 换成 `schedutil`，其余相同。这是教学版的 `--benchmark_context` / `AddCustomContext`：把数据集哈希、提交号、调频策略和轨迹口径写进基线，而不是只留一个 p99 数字。

### 本课三条主线

1. **噪声阈值**：同一提交在隔离环境跑两组 9 次，p99 均值都是 `200.0`，双侧精确 U 检验 `pvalue=1.0`，`alpha=0.05`，`statistically_different=false`，`reliable=true`。适用边界是：2 次重复虽然已经达到 `UTEST_MIN_REPETITIONS`，检验会跑，但 `reliable=false`；User Guide 和 tools.md 都没有给出一个固定的「±x% 即噪声」数字。本课把「先做足够重复，再用 U 检验和效应量一起看」当作教学阈值规则。
2. **代表性负载**：固定数据集和固定轨迹必须包含真实的 GET/SET 混合与热键分布。完整轨迹上 `c2` 的 p99 是 `600.0`，相对变化 `2.0`，U 检验 `pvalue=4.1e-05`；只打热键 GET 的 1454 次请求里，`c0` 与 `c2` 的 p99 都是 `80.0`，完全看不出 SET 扫描。适用边界是：只测一条热路径微基准，会把被改坏的路径从证据里删掉。
3. **性能预算与阻断门槛**：预算从隔离环境 `c0` 的 9 次均值算出：p99 `220.0`（`200.0` 的 1.10 倍）、吞吐 `9099.7619`（`10110.8465` 的 0.90 倍）、CPU/请求 `60.5`（`55.0` 的 1.10 倍）、错误率 `0.0`、内存 `8192`。任何一项越界就阻断。适用边界是：门槛建在 9 次重复的均值上，不是共享 CI 上单次运行的极窄硬阈值。

## 学习目标

- 能解释噪声阈值及适用边界：默认 `alpha=0.05` 且至少 9 次重复才谈得上有意义的 U 检验；2 次重复会跑检验但不可靠；User Guide 和 tools.md 没有定义固定的「±x% 即噪声」，要把重复次数、p 值和效应量放在一起看。
- 独立完成节点实践：为 KV 加入固定数据集基准，比较多个提交并保存环境元数据。本课用 200 个键、2000 次固定轨迹和 9 次重复，比较 `c0`、`c1`、`c2`、`c3`，并把数据集哈希、提交号、内核、调频策略和超时写进基线。
- 能建立并解释性能预算与阻断门槛：p99 `220.0`、吞吐 `9099.7619`、CPU/请求 `60.5`、错误率 `0.0`、内存 `8192`；能用这组预算挡住 `c2` 的 SET 扫描和 `c3` 的超时尾部，并在优化报告里同时给出 p99 与错误率。

## 为什么后端要懂

节点对「为什么」写得很干脆：让长期迭代保持性能证据。放到本课的教学内容后台：`c1` 只是改名，成本与 `c0` 相同，p99 均值仍是 `200.0`，U 检验 `pvalue=1.0`；如果没有跨提交基线，这种提交也可能被一次偶然的单次运行误判。`c2` 的改动看起来很小，但完整轨迹上的 p99 从 `200.0` 涨到 `600.0`，吞吐从 `10110.8465` 降到 `7199.507`，CPU/请求从 `55.0` 涨到 `95.0`；没有固定数据集和预算，这个退化很容易被「热键 GET 还是 `80.0`」掩盖。`c3` 更典型：成功路径 p99 从 `200.0` 降到 `40.0`，吞吐涨到 `12626.6923`，但 p99_all 变成 `2000.0`，错误率从 `0.0` 到 `0.02`，内存从 `4096` 涨到 `65536`。只报成功路径的数字，会把一次局部优化写成整体胜利。

长期迭代需要的不是一张「这版很快」的截图，而是一份能重复、能对照、能阻断的证据：同一个数据集哈希、同一条请求轨迹、同一种环境元数据、同一组预算指标。这样，性能讨论才不会被一次噪声或一个被删掉的负载路径带走。

## 核心知识点

### 1. 噪声阈值

同一提交 `c0` 在隔离环境跑两组任务 A、B，各 9 次。`p99_all` 均值都是 `200.0`，A 的取值是 `[198, 200, 202, 196, 204, 200, 198, 202, 200]`，B 的取值是 `[202, 198, 200, 204, 196, 200, 202, 198, 200]`，`stdev=2.4495`，`cv=0.012247`，`min=196`，`max=204`。吞吐均值都是 `10110.8465`，中位数 `10109.1791`，`stdev=137.745`，`cv=0.013623`，`min=9889.2405`，`max=10339.1232`。错误率均值 `0.0`，`cpu_per_req` 均值 `55.0`，`memory_per_conn` 均值 `4096.0`。按 `(new - old) / |old|` 计算，`rel_mean=0.0`；双侧精确 U 检验 `pvalue=1.0`，`alpha=0.05`，`statistically_different=false`，9 次重复所以 `reliable=true`。这是噪声，不是退化。

两组任务的倍率表只是同一组百分数的不同排列，均值都是 `100`，但单次运行仍然会散开。任务 A 第一轮倍率 `99`，`total_us=195640`，吞吐 `10222.8583`，`p50_all=79`，`p99_all=198`；任务 B 第一轮倍率 `101`，`total_us=200040`，吞吐 `9998.0004`，`p50_all=81`，`p99_all=202`。只看这一对，`198` 对 `202` 看起来像退化；重复 9 次之后，两组均值都是 `200.0`，相对变化 `0.0`，U 检验也不显著。单次运行的差异来自这一轮的倍率，不来自代码。

若只用前 2 次重复比较 p99，A 的均值是 `199.0`（`[198, 200]`），B 的均值是 `200.0`（`[202, 198]`），`rel_mean=0.005025`，`pvalue=1.0`，但 `reliable=false`，`warning` 为 `WARNING: Results unreliable! 9+ repetitions are required for a meaningful U test (tools.md / report.py UTEST_OPTIMAL_REPETITIONS).`。2 次已经达到 `UTEST_MIN_REPETITIONS`，检验会跑；tools.md 的 WARNING 写的是 U 检验需要 LARGE（no less than 9）次重复，`tools/gbench/report.py` 的 `UTEST_OPTIMAL_REPETITIONS = 9` 把「最低合理次数」写成 9。2 次的结果可以展示检验怎么跑，但不能当成可靠结论。

User Guide 和 tools.md 都没有写出一个固定的「±x% 即噪声」数字。User Guide 在 Statistics 一节只说明默认每个基准跑一次，可用 `--benchmark_repetitions` 或 `Repetitions()` 重复，重复后报告均值、中位数、标准差和变异系数，它不定义置信区间。本课把「先做足够重复（本实验 9 次），再用 U 检验和效应量一起看」当作教学阈值规则；`alpha=0.05` 来自 tools.md 所描述的 `compare.py` 默认值，不来自 User Guide 正文。

读 compare.py 的颜色时也要小心。tools.md 的约定是：绿色表示 statistically different，可能是显著变好，也可能是显著变差，必须看实际数字；红色表示 statistically similar，性能没有显著变化。它还把「成功」写成 successfully finding a difference，把「失败」写成 failing to find a difference。tools.md 同时提醒：即使判断两次测量存在统计显著差异，也不必然意味着被测基准真的不同，反过来也一样。因此 p 值不能单独当成回归结论。

共享 CI 的噪声更大。同一提交在共享 CI 上跑两组 9 次，`p99_all` 均值仍都是 `200.0`，A 的取值是 `[170, 180, 190, 200, 200, 210, 220, 230, 200]`，`stdev=18.7083`，`cv=0.093541`；B 的取值是 `[160, 176, 192, 208, 224, 240, 180, 200, 220]`，`stdev=25.6905`，`cv=0.128452`。U 检验 `pvalue=1.0`，并不显著。吞吐均值因为 `1/time` 的非线性而不完全相同，分别是 `10189.2105` 和 `10262.9531`，`rel_mean=0.007237`，`pvalue=1.0`。`reducing_variance.md` 把调频、睿频、上下文切换、SMT、缓存效应、NUMA 等列为方差来源，并写：「Reducing sources of variance is OS and architecture dependent, which is one reason some companies maintain machines dedicated to performance testing.」共享 CI 与这句话里的专用性能机相反，单次运行的门槛更容易把噪声当成退化。

### 2. 代表性负载

代表性负载要求固定数据集和固定请求轨迹覆盖真实的混合。本课的数据集有 `200` 个键，哈希是 `0f48bbc37e78af58`；轨迹有 `2000` 次请求，`seed=42`，其中 `1800` 次 GET、`200` 次 SET，`1600` 次落在最热的 `20` 个键上。数据集哈希让「同一份数据」可以被核对，轨迹种子让「同一串请求」可以被复现；只留下一个 p99 数字，无法说明这次测的是哪份数据、哪种混合。

`c2` 在每次 SET 上做全表过期扫描，每个键额外 `2` 微秒和 `2` 周期，`200` 个键共增加 `400` 微秒和 `400` 周期。完整轨迹上，`c2` 的 `p99_all` 均值是 `600.0`，取值是 `[594, 600, 606, 588, 612, 600, 594, 606, 600]`，`stdev=7.3485`，`cv=0.012247`，`rel_mean=2.0`（+200%），U 检验 `pvalue=4.1e-05`，也就是 `0.000041`，精确枚举为 `2/C(18,9)=2/48620`，`statistically_different=true`。吞吐均值是 `7199.507`，中位数 `7198.3876`，`stdev=95.2361`，`cv=0.013228`，`min=7046.2232`，`max=7357.269`，`rel_mean=-0.287942`。`cpu_per_req` 均值 `95.0`，`rel_mean=0.727273`；错误率仍是 `0.0`，内存仍是 `4096.0`。第一轮倍率 `99`，`total_us=274840`，吞吐 `7276.9611`，`p50_all=79`，`p99_all=594`。`gate_c2` 未通过，失败项是 `p99_all`、`throughput_qps`、`cpu_per_req`。

完整轨迹里 SET 占 `10%`，高于 p99 所在的 1% 尾部，所以 p99 落在 SET 的 `600` 上；第一轮的 p50 仍是 GET 的 `79`。这也是为什么只看中位数或热路径会漏掉这次退化。

把负载改成「只打热键 GET」（`1454` 次请求，不含 SET）之后，`c0` 与 `c2` 的 `p99_all` 均值都是 `80.0`，取值都是 `[79, 80, 81, 78, 82, 80, 79, 81, 80]`，`rel_mean=0.0`，`pvalue=1.0`，完全看不出扫描回归。吞吐均值都是 `12502.6054`，`cpu_per_req` 都是 `50.0`。热键 GET 微基准没有错，它只是没有覆盖被改坏的路径。代表性负载的边界就在这里：固定数据集必须包含真实混合，而不是只测一条热路径。

### 3. 性能预算与阻断门槛

预算从隔离环境 `c0` 的 9 次均值算出，门槛也在 9 次重复的均值上判断。本课的预算是：`p99_all` 不超过 `220.0`，即基线均值 `200.0` 的 1.10 倍；`throughput_qps` 不低于 `9099.7619`，即 `10110.8465` 的 0.90 倍；`cpu_per_req` 不超过 `60.5`，即 `55.0` 的 1.10 倍；`error_rate` 不超过 `0.0`；`memory_per_conn` 不超过 `8192`。`evaluate_gate` 检查五项，任何一项不满足就整体不通过。

同一提交 `c0` 在隔离任务 B 和共享任务 B 上，gate 都通过。`c1` 只改名，成本与 `c0` 相同，gate 也通过。`c2` 失败在 `p99_all`、`throughput_qps`、`cpu_per_req`：`600.0` 超过 `220.0`，`7199.507` 低于 `9099.7619`，`95.0` 超过 `60.5`。`c3` 失败在 `p99_all`、`error_rate`、`memory_per_conn`：`2000.0` 超过 `220.0`，`0.02` 超过 `0.0`，`65536.0` 超过 `8192`。

`c3` 的数字说明为什么门槛不能只看成功路径。`p99_success` 均值是 `40.0`，取值含 `39`、`41`，`stdev=0.5`，`cv=0.0125`，`rel_mean=-0.8`，也就是快 80%。吞吐均值 `12626.6923`，`rel_mean=0.248826`；成功吞吐 `ok_throughput_qps` 均值 `12374.1585`，`rel_mean=0.22385`；`cpu_per_req` 均值 `20.6`，`rel_mean=-0.625455`。若只看成功路径的 p99 和吞吐，`optimistic_success_only` 为通过。但 `p99_all` 均值是 `2000.0`，每次都是 `2000`，因为超时进入尾部，`rel_mean=9.0`；错误率均值 `0.02`，来自 `40/2000`；内存 `65536.0`，`rel_mean=15.0`。第一轮倍率 `99`，`errors=40`，`total_us=158400`，吞吐 `12626.2626`，成功吞吐 `12373.7374`，`p50_all=40`，`p99_all=2000`，`p99_success=40`。`gate_c3` 未通过，失败项是 `p99_all`、`error_rate`、`memory_per_conn`。

错误率从 `0.0` 到 `0.02` 时，相对变化 `(new - old) / |old|` 因旧值为 0 在本引擎里记成 `0.0`。这个 `0.0` 不是「错误率没变」，只是公式在分母为 0 时的教学处理。绝对均值是 `0.0` 到 `0.02`，U 检验 `pvalue=4.1e-05`，显著不同。报告里应当写绝对值和 p 值，不能拿 `rel_mean=0.0` 去证明错误率没有变化。

预算把吞吐、尾延迟、CPU/请求、错误率和内存/连接放在同一张表里，正是为了避免局部优化牺牲整体目标。`c2` 的扫描提高了 SET 成本，`c3` 的缓存降低了成功路径成本却引入了超时尾部和错误，两者都只能被一张多指标的门槛挡住。

## 常见误区

节点写出的误区是：在共享 CI 上设极窄硬阈值。对 9 对单次运行套 0.5% 相对硬阈值，隔离环境误报 `7/9`。例如 `198` 对 `202` 的相对变化是 `0.020202`，最大的一对 `196` 对 `204` 是 `0.040816`，只有 `200` 对 `200` 的两对没有触发。共享 CI 误报 `9/9`，例如 `210` 对 `240` 是 `0.142857`，`220` 对 `180` 是 `-0.181818`，`200` 对 `224` 是 `0.12`。同一组单次运行改用 10% 阈值，隔离环境误报 `0/9`，共享 CI 仍有 `4/9`。共享 CI 两次任务的 p99 均值仍都是 `200.0`，U 检验 `pvalue=1.0`，并不显著。把 0.5% 的单次硬阈值放在共享 CI 上，保护看起来更严，实际是 9 次无意义的红灯。

与这个误区相邻的另一种做法，是只报成功路径的 p99 和吞吐。`c3` 的 `optimistic_success_only` 通过，成功路径 p99 是 `40.0`，成功吞吐是 `12374.1585`；但 `gate_c3` 因 `p99_all=2000.0`、`error_rate=0.02`、`memory_per_conn=65536` 失败。tools.md 还提醒，统计显著不等于被测基准真的不同，反之亦然；只看颜色或只看一个指标，都会把结论推过头。

## 设计权衡

严格门槛提高保护强度，也增加误报与维护成本。实验把这个权衡落成两组数字：0.5% 单次硬阈值在隔离环境就有 `7/9` 误报，维护成本是无意义的红灯；把门槛放到 9 次均值上的 10% p99 预算（`220.0`）时，同一代码过关，`c2` 的 `600.0` 和 `c3` 的超时尾部、`0.02` 错误率、`65536` 内存会被挡住。

更严的阈值不是没有代价。共享 CI 的 0.5% 阈值 `9/9` 误报，10% 单次阈值仍有 `4/9`；如果把这些单次结果直接接进阻断流程，团队会花时间处理噪声，而不是处理真实退化。反过来，预算太松会放走 `c2` 这种 p99 翻三倍的扫描。tools.md 提醒统计显著不等于真实差异，反之亦然，所以门槛要同时看 p 值和效应量，并且报告 p99 与错误率，而不是只看其中一个。

本课采用的折中是：用隔离或受控环境产生基线，用 9 次重复的均值和 U 检验判断差异是否稳定，用预算表把 p99、吞吐、CPU/请求、错误率和内存/连接一起拦住。这样严格性落在有证据的均值上，而不是落在共享 CI 的单次噪声上。

## 动手实践：为 KV 加入固定数据集基准，比较多个提交并保存环境元数据

实践按节点的表述，为 KV 加入固定数据集基准，比较多个提交并保存环境元数据。工作目录是 `practice140/`，配套代码是 `engine.py`、`app.py`、`test_regression.py`。在该目录执行：

```bash
cd /workspace/atlas/lessons/practice140
bash test.sh
```

成功时打印 `ALL_CHECKS_PASSED`，并写出 `results.json`。下面的实验数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。跑的是教学引擎 `RegressionEngine`，不是 Redis，不是真实 KV 服务，不是 Google Benchmark，也不是 compare.py 或 scipy。

### 核对一：固定数据集与环境元数据

先看数据集和轨迹口径：`n_keys=200`、`n_requests=2000`、`get_count=1800`、`set_count=200`、`hot_count=1600`、`trace_seed=42`、`dataset_hash=0f48bbc37e78af58`。隔离环境 `c0` 的元数据是 `hostname=atlas-teach-ci-1`、`cpus=8`、`governor=performance`、`kernel=Linux 6.8.0-teach`、`commit_id=c0e1a01`、`timeout_us=2000`；共享 CI 的 `governor=schedutil`，其余相同。`c2` 和 `c3` 的 `commit_id` 分别是 `c2e1b03` 和 `c3e1c04`。这些字段对应教学版的 `--benchmark_context` / `AddCustomContext`：把数据集哈希、提交号、调频策略和轨迹口径写进基线，而不是只留一个 p99 数字。

### 核对二：噪声与 2 次重复的边界

`c0` 的隔离任务 A、B 各 9 次。`p99_all` 均值都是 `200.0`，A 是 `[198, 200, 202, 196, 204, 200, 198, 202, 200]`，B 是 `[202, 198, 200, 204, 196, 200, 202, 198, 200]`，`stdev=2.4495`，`cv=0.012247`，`min=196`，`max=204`。`throughput_qps` 均值都是 `10110.8465`，中位数 `10109.1791`，`stdev=137.745`，`cv=0.013623`，`min=9889.2405`，`max=10339.1232`。`error_rate` 均值 `0.0`，`cpu_per_req` 均值 `55.0`，`memory_per_conn` 均值 `4096.0`。`rel_mean=0.0`，U 检验 `pvalue=1.0`，`alpha=0.05`，`statistically_different=false`，`reliable=true`。任务 A 第一轮倍率 `99`，`total_us=195640`，吞吐 `10222.8583`，`p50_all=79`，`p99_all=198`；任务 B 第一轮倍率 `101`，`total_us=200040`，吞吐 `9998.0004`，`p50_all=81`，`p99_all=202`。`c1` 的 p99 均值也是 `200.0`，`rel_mean=0.0`，`pvalue=1.0`，`statistically_different=false`，改名没有改变成本。

只取前 2 次重复时，A 的均值是 `199.0`（`[198, 200]`），B 的均值是 `200.0`（`[202, 198]`），`rel_mean=0.005025`，`pvalue=1.0`，但 `reliable=false`，`warning` 里写着 `WARNING: Results unreliable! 9+ repetitions are required for a meaningful U test (tools.md / report.py UTEST_OPTIMAL_REPETITIONS).`。2 次达到 `UTEST_MIN_REPETITIONS`，检验会跑，但不能当成可靠结论。

### 核对三：0.5% 与 10% 阈值、9 次均值预算

对 9 对单次运行套 0.5% 相对硬阈值，隔离环境误报 `7/9`：`198` 对 `202` 的相对变化是 `0.020202`，`196` 对 `204` 是 `0.040816`，只有 `200` 对 `200` 的两对没有触发。共享 CI 误报 `9/9`：`210` 对 `240` 是 `0.142857`，`220` 对 `180` 是 `-0.181818`，`200` 对 `224` 是 `0.12`。同一组单次运行改用 10% 阈值，隔离环境 `0/9`，共享 CI 仍有 `4/9`。共享 CI 的 p99 均值仍都是 `200.0`，U 检验 `pvalue=1.0`，并不显著；吞吐均值是 `10189.2105` 和 `10262.9531`，`rel_mean=0.007237`，`pvalue=1.0`。

把门槛建在 9 次重复的均值上，预算为：`p99_all=220.0`、`throughput_qps=9099.7619`、`cpu_per_req=60.5`、`error_rate=0.0`、`memory_per_conn=8192`。同一提交在隔离任务 B 和共享任务 B 上 gate 都通过；`c1` 也通过；`c2` 失败在 `p99_all`、`throughput_qps`、`cpu_per_req`；`c3` 失败在 `p99_all`、`error_rate`、`memory_per_conn`。

### 核对四：代表性负载

`c2` 在完整轨迹上 `p99_all` 均值 `600.0`，取值 `[594, 600, 606, 588, 612, 600, 594, 606, 600]`，`stdev=7.3485`，`cv=0.012247`，`rel_mean=2.0`（+200%），`pvalue=4.1e-05`（`0.000041`，精确枚举 `2/C(18,9)=2/48620`），`statistically_different=true`。吞吐均值 `7199.507`，中位数 `7198.3876`，`stdev=95.2361`，`cv=0.013228`，`min=7046.2232`，`max=7357.269`，`rel_mean=-0.287942`。`cpu_per_req` 均值 `95.0`，`rel_mean=0.727273`；错误率 `0.0`，内存 `4096.0`。第一轮倍率 `99`，`total_us=274840`，吞吐 `7276.9611`，`p50_all=79`，`p99_all=594`。

若只打热键 GET（`1454` 次请求，不含 SET），`c0` 与 `c2` 的 p99 均值都是 `80.0`，取值都是 `[79, 80, 81, 78, 82, 80, 79, 81, 80]`，`rel_mean=0.0`，`pvalue=1.0`，完全看不出扫描回归；吞吐均值都是 `12502.6054`，`cpu_per_req` 都是 `50.0`。完整轨迹的 `10%` SET 让 p99 落在 `600` 上，热键 GET 轨迹没有 SET，所以这条回归被负载本身藏掉了。

### 核对五：优化报告同时给 p99 与错误率

`c3` 的 `p99_success` 均值 `40.0`，取值含 `39`、`41`，`stdev=0.5`，`cv=0.0125`，`rel_mean=-0.8`（快 80%）。吞吐均值 `12626.6923`，`rel_mean=0.248826`；成功吞吐 `ok_throughput_qps` 均值 `12374.1585`，`rel_mean=0.22385`；`cpu_per_req` 均值 `20.6`，`rel_mean=-0.625455`。只看成功路径时，`optimistic_success_only` 通过。

但 `p99_all` 均值 `2000.0`，每次都是 `2000`，`rel_mean=9.0`；错误率均值 `0.02`，来自 `40/2000`；内存 `65536.0`，`rel_mean=15.0`。第一轮倍率 `99`，`errors=40`，`total_us=158400`，吞吐 `12626.2626`，成功吞吐 `12373.7374`，`p50_all=40`，`p99_all=2000`，`p99_success=40`。`gate_c3` 未通过，失败项是 `p99_all`、`error_rate`、`memory_per_conn`。错误率的 `rel_mean` 在本引擎里因旧值为 0 而记成 `0.0`，但绝对均值从 `0.0` 到 `0.02`，U 检验 `pvalue=4.1e-05`，显著不同；报告不能把这个 `0.0` 当成「错误率没变」。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_distinguish_noise_from_regression`、`acceptance_report_p99_and_error_rate`、`practice_fixed_dataset_and_metadata`、`pitfall_narrow_threshold_on_shared_ci`、`tradeoff_strict_gate_vs_false_positive`、`representative_load_catches_set_regression`、`two_rep_utest_unreliable`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_benchmark_product`、`not_google_benchmark`、`not_compare_py`、`not_scipy`、`cycles_are_teaching`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Google Benchmark — Comparing Benchmarks 中与 Performance regression 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://github.com/google/benchmark/blob/main/docs/user_guide.md](https://github.com/google/benchmark/blob/main/docs/user_guide.md) 返回 200，无跳转，最终 URL 仍是该地址，786527 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Sat, 14 Nov 2026 02:19:02 GMT`（北京时间 2026-11-14 10:19:02）。`<title>` 为 benchmark/docs/user_guide.md at main · google/benchmark · GitHub，正文一级标题为 User Guide。同一文件的 raw 地址返回 200，无跳转，62568 字节，`text/plain; charset=utf-8`，`Date` 为 `Sat, 14 Nov 2026 02:19:04 GMT`（北京时间 2026-11-14 10:19:04）。最近一次提交为 `53aaafd9f2b0005f85c302711e40fd8e4ca60e38`，作者 Ritik Mishra，提交时间 `2026-10-30T10:52:06Z`（北京时间 2026-10-30 18:52:06）。项目许可证为 Apache License 2.0（`LICENSE` raw 返回 HTTP 200，11358 字节）。
- User Guide 的 Result comparison 只写 “It is possible to compare the benchmarking results.” 并指向 Additional Tooling Documentation，即 [docs/tools.md](https://github.com/google/benchmark/blob/main/docs/tools.md)。User Guide 本身没有定义「噪声阈值」「性能预算」或「阻断门槛」。Extra Context 一节说明默认会在结果前打印运行该基准的 CPU 信息，可用 `--benchmark_context` 或 `benchmark::AddCustomContext("foo", "bar")` 追加上下文；同一 key 再写第二个值会失败并报错。Statistics 一节说明默认每个基准只跑一次，可用 `--benchmark_repetitions` 或 `Repetitions()` 重复，之后报告均值、中位数、标准差和变异系数；User Guide 不定义置信区间。Runtime and Reporting Considerations 说明迭代次数按最小时间动态决定，预热默认 0 秒，报告前会打印机器信息。Disabling CPU Frequency Scaling 指向 `reducing_variance.md`。
- 跟随阅读的 [tools.md raw](https://raw.githubusercontent.com/google/benchmark/main/docs/tools.md) 返回 200，无跳转，19213 字节，`Content-Type` 为 `text/plain; charset=utf-8`，`Date` 为 `Sat, 14 Nov 2026 02:19:38 GMT`（北京时间 2026-11-14 10:19:38）。对应的 blob 地址返回 200，无跳转，282964 字节，`Date` 为 `Sat, 14 Nov 2026 02:19:39 GMT`（北京时间 2026-11-14 10:19:39），`<title>` 为 benchmark/docs/tools.md at main · google/benchmark · GitHub。该文件最近一次提交为 `b5aade18104abd60ba61e54b3b7affaa95c44a26`，作者 देवांश वार्ष्णेय，提交时间 `2023-07-09T16:25:34Z`（北京时间 2023-07-10 00:25:34），说明为 Update tools.md for missing color meaning issue #1491 (#1624)；GitHub API 对该提交的 `Last-Modified` 为 `Sun, 09 Jul 2023 16:25:34 GMT`。正文一级标题为 Benchmark Tools。
- tools.md 写明 `compare.py` 用三种模式比较基准结果：两个结果文件或可执行文件；同一二进制的两个 filter；两个二进制上的两个 filter。同名才比较，名字对不上就省略。Time 与 CPU 列的相对变化按 `(new - old) / |old|` 计算。它把性能测量写成 “Performance measurements are an art, and performance comparisons are doubly so. Results are often noisy… just by visual inspection, it is not at all apparent if two measurements are actually showing a performance change or not.”，因此 `compare.py` 使用 Mann–Whitney U test，原假设是 there’s no difference in performance。若 p-value 小于选定的显著性水平 alpha，拒绝原假设，认为基准显著不同；若 p-value ≥ alpha，不拒绝原假设，把两次基准当作相似。颜色约定是：绿色表示 statistically different，可能是显著变好也可能是显著变差，必须看实际数字；红色表示 statistically similar，性能没有显著变化。它还把 “Success means successfully finding a difference. Failure means failing to find a difference.” 写进说明。U test 一节有 WARNING：requires LARGE (no less than 9) number of repetitions to be meaningful。同日阅读的 `tools/compare.py` 与 `tools/gbench/report.py` 给出默认 `--alpha` 为 0.05，`UTEST_MIN_REPETITIONS = 2`，`UTEST_OPTIMAL_REPETITIONS = 9`（Lowest reasonable number, More is better.）。这些数字不在 `user_guide.md` 正文里，来自 `compare.py` / `report.py`。`compare.py` 依赖 scipy；本课教学引擎不用 scipy，也不是 `compare.py`。
- 跟随阅读的 [reducing_variance.md raw](https://raw.githubusercontent.com/google/benchmark/main/docs/reducing_variance.md) 返回 200，9541 字节，`Date` 为 `Sat, 14 Nov 2026 02:19:38 GMT`（北京时间 2026-11-14 10:19:38）。blob 地址返回 200，228189 字节，`Date` 为 `Sat, 14 Nov 2026 02:21:15 GMT`（北京时间 2026-11-14 10:21:15），`<title>` 为 benchmark/docs/reducing_variance.md at main · google/benchmark · GitHub。最近一次提交为 `32ae3f43f7fd2300401a2e6ad7f2176841250211`，作者 Manu Nicholas Jacob，提交时间 `2026-11-02T09:30:48Z`（北京时间 2026-11-02 17:30:48），说明为 [doc] Add an Android section to reducing_variance (#2356)。该页列出调频、睿频、上下文切换、SMT、缓存效应、NUMA 等方差来源，并写下这句关于专用性能机的说明。本课的「共享 CI」对照的就是这句话：共享跑步机与专用性能机相反，在上面设极窄硬阈值更容易误报。本课没有把该页 Android 例子里的带宽或百分比数字当作实验数字。

教学实验按以下方式使用这些材料，并记下差异。

- **结果比较与 U 检验**：User Guide 的 Result comparison 只有一句并指向 tools.md；比较模式、相对变化、Mann–Whitney U test、`alpha=0.05` 和 9 次重复的警告都来自 tools.md 及 `compare.py` / `report.py`。本课的 `pvalue=1.0`、`pvalue=4.1e-05` 是对 9+9 样本做精确枚举的教学结果，不是 `scipy.stats.mannwhitneyu`，也不是 `compare.py` 的输出。`statistically_different` 和颜色语义按 tools.md 的解释用于教学报告；`reliable` 对应 tools.md 的 9 次重复警告和 `report.py` 的常量。
- **上下文与重复**：`--benchmark_context` / `AddCustomContext` 对应本课的 `environment_c0_isolated` 和 `environment_c0_shared`：数据集哈希、提交号、内核、调频策略、超时和轨迹口径都被保存。User Guide 的重复统计报告均值、中位数、标准差和变异系数，不定义置信区间；本课用 9 次重复和 U 检验，预算表是教学模型，不是 User Guide 的正式接口。
- **方差来源**：`reducing_variance.md` 的调频、睿频、上下文切换、SMT、缓存和 NUMA 列表，用来解释共享 CI 的 p99 为什么从 `stdev=18.7083` 到 `25.6905` 地散开。本课不执行 `cpupower` 或 `taskset`，也不复现该页的 Android 数据；教学倍率表只是让「同一组均值、不同排列」的噪声可核对。
- **时间含义**：Google Benchmark 测量真实 CPU 时间或墙钟时间；本课全部是教学虚拟微秒、教学周期和教学字节。`c2` 的 `600.0`、`c3` 的 `2000.0`、吞吐 `10110.8465` 或 `7199.507` 都不是生产 RTT，也不是某台宿主机上的基准输出。

## 自测题

**题 1.** 怎样区分统计噪声与真实退化？请用 `c0` 的隔离任务 A/B 与 `c2` 的数字说明，并解释为什么 2 次重复的 U 检验不能作为可靠结论。

<details>
<summary>答案</summary>

`c0` 的隔离任务 A 和 B 是同一提交、同一轨迹、同一数据集，只是倍率表排列不同。9 次重复后，两组的 `p99_all` 均值都是 `200.0`，A 是 `[198, 200, 202, 196, 204, 200, 198, 202, 200]`，B 是 `[202, 198, 200, 204, 196, 200, 202, 198, 200]`，`stdev=2.4495`，`cv=0.012247`，`rel_mean=0.0`，U 检验 `pvalue=1.0`，`alpha=0.05`，`statistically_different=false`，`reliable=true`。任务 A 第一轮 p99 是 `198`，任务 B 第一轮是 `202`，单看这一对像是差异；重复之后差异消失，这是噪声。

`c2` 在完整轨迹上 p99 均值是 `600.0`，取值 `[594, 600, 606, 588, 612, 600, 594, 606, 600]`，`stdev=7.3485`，`cv=0.012247`，`rel_mean=2.0`（+200%），U 检验 `pvalue=4.1e-05`（`0.000041`，精确枚举 `2/C(18,9)=2/48620`），`statistically_different=true`。吞吐从 `10110.8465` 降到 `7199.507`，CPU/请求从 `55.0` 升到 `95.0`，效应量和 p 值都指向真实退化。这里的变异系数与 `c0` 相同，都是 `0.012247`，说明这不是「更吵」，而是均值真的移到了 `600`。

2 次重复时，A 均值 `199.0`（`[198, 200]`），B 均值 `200.0`（`[202, 198]`），`rel_mean=0.005025`，`pvalue=1.0`，但 `reliable=false`，warning 写明 `9+ repetitions are required for a meaningful U test`。2 次达到 `UTEST_MIN_REPETITIONS`，检验会跑；tools.md 要求 LARGE（no less than 9）次重复，`report.py` 的 `UTEST_OPTIMAL_REPETITIONS = 9` 也说明 9 才是最低合理次数。因此 2 次的结果只能说明检验怎么跑，不能当作可靠结论。

</details>

**题 2.** `c3` 的成功路径看起来更快，为什么 `gate_c3` 仍然失败？优化报告为什么必须同时给 p99 与错误率？

<details>
<summary>答案</summary>

`c3` 的 `p99_success` 均值是 `40.0`，取值含 `39`、`41`，`stdev=0.5`，`cv=0.0125`，`rel_mean=-0.8`，成功路径快 80%。吞吐均值 `12626.6923`，`rel_mean=0.248826`；成功吞吐 `ok_throughput_qps` 均值 `12374.1585`，`rel_mean=0.22385`；`cpu_per_req` 均值 `20.6`，`rel_mean=-0.625455`。若只看这些数，`optimistic_success_only` 为通过。

但 `c3` 每 `50` 个请求有 `1` 个等到超时 `2000` 微秒后出错。`p99_all` 均值是 `2000.0`，每次都是 `2000`，`rel_mean=9.0`；错误率均值 `0.02`，来自 `40/2000`；内存 `65536.0`，`rel_mean=15.0`。第一轮倍率 `99`，`errors=40`，`total_us=158400`，吞吐 `12626.2626`，成功吞吐 `12373.7374`，`p50_all=40`，`p99_all=2000`，`p99_success=40`。`gate_c3` 失败在 `p99_all`、`error_rate`、`memory_per_conn`：`2000.0` 超过 `220.0`，`0.02` 超过 `0.0`，`65536.0` 超过 `8192`。

错误率从 `0.0` 到 `0.02` 时，相对变化 `(new - old) / |old|` 因旧值为 0 在本引擎里记成 `0.0`；这个 `0.0` 不是「错误率没变」。绝对均值是 `0.0` 到 `0.02`，U 检验 `pvalue=4.1e-05`，显著不同。只报成功路径的 p99 和吞吐，会把超时尾部、请求失败和连接内存上涨都藏起来；优化报告必须同时给 p99 与错误率，才能看出这次「局部优化」牺牲了整体目标。

</details>

**题 3.** 热键 GET 负载为什么看不出 `c2` 的 SET 扫描退化？完整轨迹用什么数字暴露它？

<details>
<summary>答案</summary>

热键 GET 负载只保留 `1454` 次 GET，不含 SET。`c2` 的扫描只在 SET 上发生，每次 SET 对 `200` 个键各加 `2` 微秒和 `2` 周期，共增加 `400` 微秒和 `400` 周期。SET 被从轨迹里删掉后，`c0` 与 `c2` 的 `p99_all` 均值都是 `80.0`，取值都是 `[79, 80, 81, 78, 82, 80, 79, 81, 80]`，`rel_mean=0.0`，U 检验 `pvalue=1.0`，完全看不出回归；吞吐均值都是 `12502.6054`，`cpu_per_req` 都是 `50.0`。

完整轨迹有 `2000` 次请求，其中 `1800` 次 GET、`200` 次 SET，SET 占 `10%`，高于 p99 所在的 1% 尾部。`c2` 的 p99 均值因此变成 `600.0`，取值 `[594, 600, 606, 588, 612, 600, 594, 606, 600]`，`rel_mean=2.0`（+200%），`pvalue=4.1e-05`（`0.000041`，精确枚举 `2/C(18,9)=2/48620`），`statistically_different=true`；吞吐降到 `7199.507`，`rel_mean=-0.287942`；CPU/请求升到 `95.0`，`rel_mean=0.727273`。第一轮倍率 `99`，`total_us=274840`，吞吐 `7276.9611`，`p50_all=79`，`p99_all=594`。固定数据集和固定轨迹必须包含真实混合，只测一条热路径微基准会把被改坏的 SET 路径从证据里删掉。

</details>

## 推荐资源

- [Google Benchmark — Comparing Benchmarks](https://github.com/google/benchmark/blob/main/docs/user_guide.md)（Google Benchmark 项目文档，英文，专家，免费）：节点给出的官方出处，也是本课唯一列出的推荐资源。2026-11-14 用 curl 核对，该地址返回 HTTP 200，无跳转，最终 URL 仍是该地址，786527 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Sat, 14 Nov 2026 02:19:02 GMT`（北京时间 2026-11-14 10:19:02）；`<title>` 为 benchmark/docs/user_guide.md at main · google/benchmark · GitHub，正文一级标题为 User Guide。同一文件的 raw 地址返回 HTTP 200，无跳转，62568 字节，`text/plain; charset=utf-8`，`Date` 为 `Sat, 14 Nov 2026 02:19:04 GMT`（北京时间 2026-11-14 10:19:04）。最近一次提交为 `53aaafd9f2b0005f85c302711e40fd8e4ca60e38`，作者 Ritik Mishra，提交时间 `2026-10-30T10:52:06Z`（北京时间 2026-10-30 18:52:06）。许可证为 Apache License 2.0（`LICENSE` raw 返回 HTTP 200，11358 字节）。User Guide 的 Result comparison 只写 “It is possible to compare the benchmarking results.” 并指向 Additional Tooling Documentation；真正讲比较模式、相对变化和 Mann–Whitney U test 的是它指向的 `docs/tools.md`（2026-11-14 raw 地址返回 HTTP 200，19213 字节；`compare.py` 默认 `--alpha` 为 0.05，`report.py` 的 `UTEST_MIN_REPETITIONS = 2`、`UTEST_OPTIMAL_REPETITIONS = 9`）。本课用这份文档核对环境上下文、重复统计和调频噪声，把结论落到固定数据集、9 次重复和预算门槛的教学实验上；教学引擎不是 Google Benchmark，不是 compare.py，也不是 scipy。

## 本课小结

- 跨提交回归需要可重复基线：固定数据集 `200` 个键、哈希 `0f48bbc37e78af58`、固定轨迹 `2000` 次请求（`1800` GET、`200` SET、`1600` 次热键）、每个提交 `9` 次重复，并保存 `hostname=atlas-teach-ci-1`、`cpus=8`、`governor=performance`、`kernel=Linux 6.8.0-teach`、`commit_id` 和 `timeout_us=2000` 等环境元数据。
- 噪声与真实退化要分开：`c0` 隔离任务 A/B 的 p99 均值都是 `200.0`，`rel_mean=0.0`，U 检验 `pvalue=1.0`，`reliable=true`；只取 2 次重复时 A 均值 `199.0`、B 均值 `200.0`，`rel_mean=0.005025`，`pvalue=1.0` 但 `reliable=false`。`c2` 的 p99 均值 `600.0`，`rel_mean=2.0`，`pvalue=4.1e-05`（`0.000041`，精确枚举 `2/C(18,9)=2/48620`），才是真退化。
- 代表性负载要包含真实混合：完整轨迹能看出 `c2` 的 SET 扫描；只打热键 GET 的 `1454` 次请求里，`c0` 与 `c2` 的 p99 都是 `80.0`，`rel_mean=0.0`，`pvalue=1.0`，吞吐都是 `12502.6054`，CPU/请求都是 `50.0`。
- 预算建在 9 次均值上：p99 `220.0`、吞吐 `9099.7619`、CPU/请求 `60.5`、错误率 `0.0`、内存 `8192`。`c2` 失败在 `p99_all`、`throughput_qps`、`cpu_per_req`；`c3` 失败在 `p99_all`、`error_rate`、`memory_per_conn`，但它的 `optimistic_success_only` 通过。
- 优化报告必须同时给 p99 与错误率：`c3` 的成功路径 p99 是 `40.0`，`rel_mean=-0.8`，成功吞吐 `12374.1585`，但 `p99_all=2000.0`、错误率 `0.02`（`40/2000`）、内存 `65536.0`。错误率的 `rel_mean` 因旧值为 0 记成 `0.0`，不能当成「没变」。
- 不要在共享 CI 上对单次运行设极窄硬阈值：0.5% 阈值在隔离环境误报 `7/9`，在共享 CI 误报 `9/9`；10% 阈值在隔离环境 `0/9`，共享 CI 仍有 `4/9`。共享 CI 的 p99 均值仍都是 `200.0`，U 检验 `pvalue=1.0`，并不显著。严格门槛提高保护强度，也增加误报与维护成本，门槛应同时看 p 值和效应量。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是 Google Benchmark、compare.py 或 scipy 的测量。`RegressionEngine` 不是 Redis，不是真实 KV 服务，也不是网络服务；教学主机和提交号都是假设，不是真实机器或真实 git 对象。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「Result comparison 指向 tools.md」「U test 需要 no less than 9 次重复」「统计显著不等于真实差异」以及「共享机器与专用性能机相反」这几段，不要把一次噪声当成退化，也不要只报成功路径的 p99 和吞吐就把一次回归放过去。
