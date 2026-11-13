# 第 131/208 课：perf 采样与符号

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-perf` |
| 英文名 | Linux perf |
| 领域 / 阶段 | I / 第 3 阶段 |
| 子类 | 剖析与诊断 |
| 难度 | 进阶 |
| 前置节点 | [`i-load`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/130.html)、[`c-thread`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/34.html) |
| 标签 | `Linux perf`、`Performance` |
| 参考资料 | [Linux perf — Tutorial](https://perf.wiki.kernel.org/index.php/Tutorial)（Linux perf 教程，英文，进阶，免费；2026-11-14 该地址本身返回 HTTP 301，无跟随时 162 字节，`Location` 为 `https://archive.kernel.org/oldwiki/perf.wiki.kernel.org/index.php/Tutorial.html`，响应头 `Date` 为 `Fri, 13 Nov 2026 17:48:01 GMT`（北京时间 2026-11-14 01:48:01）。归档地址再 301 到 `https://perfwiki.github.io/`（162 字节，`Date` 为 `Fri, 13 Nov 2026 17:48:04 GMT`，北京时间 01:48:04）。最终 200 页只有 73 字节，内容是把浏览器刷新到 `./main/`，`<title>` 没有正文标题，`Last-Modified` 为 `Sun, 11 Aug 2024 06:43:52 GMT`（北京时间 2024-08-11 14:43:52）。真正的首页是 [https://perfwiki.github.io/main/](https://perfwiki.github.io/main/)，HTTP 200，无再跳转，29276 字节，`Content-Type` 为 `text/html; charset=utf-8`，`Last-Modified` 为 `Wed, 26 Aug 2026 19:20:02 GMT`（北京时间 2026-08-27 03:20:02），响应头 `Date` 为 `Fri, 13 Nov 2026 17:48:25 GMT`（北京时间 01:48:25），`<title>` 为 perf: Linux profiling with performance counters。教程正文在 [https://perfwiki.github.io/main/tutorial/](https://perfwiki.github.io/main/tutorial/)，HTTP 200，无跳转，217182 字节，同一 `Last-Modified`，响应头 `Date` 为 `Fri, 13 Nov 2026 17:48:35 GMT`（北京时间 01:48:35），`<title>` 为 Introduction - perf: Linux profiling with performance counters，页内 `h1` 为 Linux kernel profiling with perf。大小写不同的 `/main/Tutorial/` 返回 404。同一教程的 Markdown 源在 `https://raw.githubusercontent.com/perfwiki/main/main/docs/tutorial.md`，HTTP 200，78747 字节，`Date` 为 `Fri, 13 Nov 2026 17:49:15 GMT`（北京时间 01:49:15）；该文件最近一次提交为 `36edda129a5154de69989f8d45e770d60ba90059`，作者 Ian Rogers，说明是 tutorial: Formatting fixes，提交时间 `2026-04-27T19:57:55Z`（北京时间 2026-04-28 03:57:55）。教程没有署单一原作者，示例写明取自一台 Ubuntu 11.04、kernel 2.6.38-8-generic 的 HP 6710b。正文与本课直接相关的是 *Commands*、*Counting with perf stat*、*Sampling with perf record*（默认按平均 1000 Hz 的频率采样，也可用 `-c` 固定周期；默认事件是 `cycles`；可用 `:u` / `:uk` 限制用户或内核；`perf record -g` 出现在火焰图例子里）以及 *Sample analysis with perf report*（`[.]` 表示用户、`[k]` 表示内核；被剥离符号的二进制会把符号列打成十六进制地址）。教程**没有**定义 frame pointer 与 DWARF 两种展开，也没有把「丢样」写成独立小节。本课跟随阅读了教程和首页都指向的 [Man pages](https://perfwiki.github.io/main/man-pages/)（HTTP 200，37052 字节，`Date` 为 `Fri, 13 Nov 2026 17:49:13 GMT`，北京时间 01:49:13，`<title>` 为 Man pages - perf: Linux profiling with performance counters）以及其中的 [perf-record(1)](https://man7.org/linux/man-pages/man1/perf-record.1.html)（HTTP 200，58884 字节，`Last-Modified` 为 `Sun, 08 Nov 2026 07:37:16 GMT`，北京时间 2026-11-08 15:37:16，响应头 `Date` 为 `Fri, 13 Nov 2026 17:49:38 GMT`，北京时间 01:49:38，`<title>` 为 perf-record(1) - Linux manual page，页脚为 perf 2026-01-13，HTML 渲染标注 2026-09-09、Michael Kerrisk）。该手册定义 `--call-graph` 的 `fp`（frame pointer）、`dwarf`（DWARF CFI）和 `lbr`，并写明若二进制用 `--fomit-frame-pointer` 构建，`fp` 会得到 bogus call graphs，应改用 `dwarf`；另在 `--threads` 一节提到要避免 data 目录里的 LOST events。同日还打开了 [perf-report(1)](https://man7.org/linux/man-pages/man1/perf-report.1.html)（52079 字节）和 [perf-stat(1)](https://man7.org/linux/man-pages/man1/perf-stat.1.html)（45970 字节），二者同为 HTTP 200、同一 `Last-Modified`） |
| 摘要 | perf 使用硬件与软件事件采样定位 CPU 消耗，调用栈质量依赖符号、栈展开策略和权限。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teach-kv`」「教学编译器 `AtlasTeachCC 1.0`」都是**教学假设**，用来让符号、栈展开、采样频率和丢样变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在某台机器上调用了宿主 `perf` 或 `g++`。配套代码是 Python 3 标准库写的教学引擎 `PerfEngine`，**不是 Redis，不是真实的 perf / 可观测性产品，不是网络服务，不是 Linux 的 `perf` 工具，也不是宿主机上的 g++ / clang**。它不调用宿主编译器，不读取硬件 PMU，也不写 `perf.data`。`AtlasTeachCC` 只是教学编译器标识；`-g` 与 `-fno-omit-frame-pointer` 是教学编译开关。采样窗口 `30.00` 是教学虚拟时钟上的三十秒，不是墙钟三十秒，也不是把教程里 Ubuntu 11.04 那组 `dd` / `sleep 5` 计数原样当成这次实验的结果。教程默认频率 1000 Hz、用户 / 内核修饰符和符号列的规则来自文档；frame pointer 与 DWARF 的对比来自跟随阅读的 `perf-record(1)`，不是教程正文的定义。丢样用教学环形缓冲区的排出速率 `2000` 样本 / 秒来模拟，是手册里 LOST events 的教学对照，不是某次真实 `perf record` 的丢失计数。本课所有数字都来自 `/workspace/atlas/lessons/practice131/results.json`，是**教学虚拟时钟上的数字**。

## 导读

### 上一课留下的缺口

上一课的 `LoadEngine` 解决的是：尾延迟取决于负载怎么打出去，闭环可能把一次停顿藏在 p99 之外。那一课的边界停在**到达过程会不会漏记延迟**，没有回答另一件事：若服务确实在消耗 CPU，时间花在用户态的哪一个函数、内核有没有占去一截，以及这份剖析本身可不可信。

节点把摘要写成：perf 使用硬件与软件事件采样定位 CPU 消耗，调用栈质量依赖符号、栈展开策略和权限。只看一个空调用栈，不能据此断言没有瓶颈。

### 接上前置

前置 [第 130 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/130.html) `i-load` 留下的结论是：测量要写清到达模型，成功请求的 p99 不能单独代表用户到来时的等待。本课把目光转到 CPU 上：在教学 KV 里循环读取 `Weekly digest`，用教学版的 `stat` / `record` / `report` 看用户态和内核态各占多少，热点是不是 `kv_get`。

另一则前置是 [第 34 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/34.html) `c-thread`。它讨论的是同一进程里的线程共享地址空间、却各自拥有执行栈。教程也写过可以把计数或采样附到一个内核可见的线程上。本课不再展开那一课的线程接口，只用到「栈是按线程展开的」：没有帧指针或 DWARF 信息时，展开会空掉或失真，这与线程仍然在跑、CPU 仍然在计数不是一回事。

教程把 `perf stat`、`perf record`、`perf report` 写成先计数、再采样、再按符号汇总的一条链；采样默认按 1000 Hz 的平均频率，也可以改频率或改成固定周期。调用栈怎么展开、空栈能不能当「没有热点」，教程本身没有写细，本课对照手册里的 `fp` / `dwarf` 说明，落到教学 KV 的 30 秒虚拟窗口上，而不是在本机执行 `perf` 或 `g++`。

### 教学场景（教学假设）

假设内容后台有一个教学 KV：请求打来，就按键 `article:01:title` 取出标题 `Weekly digest`。热点在用户态的 `kv_get`，它还会走到 `hash_mix`；把标题拷回请求方时会进入内核的 `copy_to_user`，收请求时会经过 `sys_recv`。二进制用教学编译器 `AtlasTeachCC 1.0` 构建，实践指定的参数是 `-g -fno-omit-frame-pointer`。剖析在教学虚拟时钟上连续跑 `30.00`，默认按 1000 Hz 采样，用 `fp` 展开调用栈。对照包括：去掉 `-g`、改用 `-fomit-frame-pointer`、改用 `dwarf` 展开，以及把频率改成 99 Hz 和 10000 Hz。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT，更不是宿主 `perf record sleep 30` 的墙钟结果。

### 本课三条主线

1. **perf stat / record / report**：`stat` 汇总计数，`record` 按溢出采样，`report` 按符号和特权级摊开。适用边界是：它回答「CPU 时间花在哪」，不代替上一课那种按到达过程计算的尾延迟；没有符号时，热点只会显示成地址。
2. **frame pointer 与 DWARF**：`fp` 依赖编译时保留帧指针；用 `-fomit-frame-pointer` 构建后再按 `fp` 展开，会得到空的或虚假的调用栈。`dwarf` 依赖 `-g` 一类调试 / CFI 信息，可以在没有帧指针时把栈找回。教程里的 `perf record -g` 是采样时打开调用栈，和编译器的 `-g` 不是同一个开关。
3. **采样频率和丢样**：更高频率能增加样本、让比例更稳，也会让教学环形缓冲区溢出并引入扰动。丢了多少样本、用户 / 内核占比有没有被扰动，要和热点一起看。

## 学习目标

- 能解释 `perf stat` / `record` / `report` 及适用边界：计数看总量和用户 / 内核占比，采样看热点；没有符号或调用栈为空时，不能把报告读成「没有瓶颈」。
- 独立完成节点实践：以 `-g -fno-omit-frame-pointer` 构建教学 KV，在虚拟时钟上采样 30 秒并核对热点是 `kv_get`，同时报告样本数、丢样以及用户 / 内核占比。
- 能说明为什么空调用栈不能用来断言没有瓶颈，以及提高采样频率时分辨率、丢样和扰动之间的权衡。

## 为什么后端要懂

节点对「为什么」写得很干脆：快速发现热点及内核开销。放到本课的教学内容后台：一次取出 `Weekly digest` 看起来只是用户态查询，教学统计里却有 `20%` 的周期花在内核。若符号被剥掉，`kv_get` 会变成 `0x401000`；若帧指针被省略却仍用 `fp` 展开，调用栈会是空的，报告热点变成 `[unknown]`，但用户态占比仍是 `80%`，样本仍有 `30000` 个。不会读符号和丢样，就无法判断这次剖析有没有资格评价优化是否有效。

## 核心知识点

### 1. perf stat / record / report

教程把常用命令写成一套和 git 类似的子命令。`perf stat` 在进程跑完后汇总计数：默认同时量用户态和内核态，可用 `cycles:u` 只看用户态，用 `cycles:uk` 显式两个都要。`perf record` 按事件溢出写下样本，默认事件是 `cycles`，默认按平均 1000 个样本 / 秒的频率采样，结果进 `perf.data`；`perf report` 再按命令、共享对象和符号列出开销，并用 `[.]`、`[k]` 标明特权级。教程提醒：`perf record` 结束时打印的样本数只是按字节数做的**估计**，准确数目要到 `perf report` 里数 `RECORD_SAMPLE`。本课教学引擎直接给出准确的教学计数，不模仿那条估计。

适用边界也在教程里写得很清楚：采样记录的是溢出后被打断时的指令指针，和计数器真正溢出的位置可能差几十条指令（skid）；`cycles` 在频率缩放和空闲时也不一定与墙钟成正比。它回答的是「这些样本落在哪个符号、哪个特权级」，不是上一课那种按预定到达时间计算的 p99。没有符号表时，教程说符号列会变成十六进制地址——本课在去掉 `-g` 之后，用户态热点从 `kv_get` 变成 `0x401000`，内核符号仍按教学 kallsyms 显示。

教学 `stat` 在 `30.00` 的窗口里给出任务时钟 `30.00`、利用率 `1.0`、用户态占比 `0.80`、内核态占比 `0.20`，用户周期 `24000000`，内核周期 `6000000`。同一窗口里按 1000 Hz 采样，意图样本 `30000`，全部记入，丢样 `0`。`report` 里 `kv_get` 自己占 `21000` 个样本（`0.70`，`[.]`），`copy_to_user` 占 `4500`（`0.15`，`[k]`），`hash_mix` 占 `3000`（`0.10`），`sys_recv` 占 `1500`（`0.05`）。用户态合计 `24000`，内核态 `6000`，与 `stat` 一致。实践要核对的热点就是 `kv_get`。

### 2. frame pointer 与 DWARF

教程的火焰图例子写过 `perf record -a -g -F 99 sleep 60`，这里的 `-g` 是 **perf 打开调用栈记录**，不是编译器生成调试信息的 `-g`。节点实践里的「以 `-g -fno-omit-frame-pointer` 构建 KV」，指的是**编译**开关。两者都要，但不要混成一个旗标。

教程正文没有定义 frame pointer 与 DWARF。跟随阅读的 `perf-record(1)` 写明：`-g` 为内核和用户都打开调用栈；`--call-graph` 默认用户态用 `fp`（frame pointer），也可以选 `dwarf`（DWARF CFI）或 `lbr`。若二进制用 `--fomit-frame-pointer` 构建，再用 `fp` 展开，会得到 bogus call graphs，这时应改用 `dwarf`（需要 perf 链到 libunwind 或 libdw）。

教学实验把这条写成三组对照。用 `-g -fno-omit-frame-pointer` 并以 `fp` 展开时，`kv_get` 的栈是 `main` → `handle_get` → `kv_get`，空栈数是 `0`。去掉帧指针后仍用 `fp`：`30000` 个样本的栈全部为空，热点显示成 `[unknown]`，但用户态占比仍是 `0.80`，样本一个也没少。这时若断言「没有瓶颈」，就是节点写出的误区。同样这份省略了帧指针、但带 `-g` 的二进制，改用 `dwarf` 展开，热点重新是 `kv_get`，空栈回到 `0`。没有 `-g` 时，`dwarf` 也同样全部空栈：CFI 与符号表一样，要靠教学 `-g` 提供。

### 3. 采样频率和丢样

教程把采样写成两种口径：固定事件周期（`-c`）和平均样本频率（`-F`）。工具默认是频率模式，目标 1000 Hz，内核会动态调整周期去贴近这个速率。手册里的 `-F` 还可以写成 `max`，并可能被 `kernel.perf_event_max_sample_rate` 节流；`-m` 设置 mmap 页数。教程没有专节讲解丢样。手册在讨论多线程写数据文件时提到要避免 LOST events。本课用教学环形缓冲区的排出速率 `2000` 样本 / 秒来对照：意图样本超过排出能力就会丢掉，并在溢出时加入 `5%` 的教学扰动，记在 `perf_overflow_handler` 上。这是教学模型，不是某次宿主 `perf` 的丢失日志。

三个频率都在同一段 `30.00` 窗口里比较。99 Hz：意图和记入都是 `2970`，丢样 `0`，`kv_get` 仍是 `0.70`，但 `copy_to_user` 变成 `0.1502`、`sys_recv` 变成 `0.0498`，比例开始出现取整误差。1000 Hz：`30000` 个样本全部记入，丢样 `0`，扰动 `0`，四个符号的占比正好是 `0.70` / `0.15` / `0.10` / `0.05`。10000 Hz：意图 `300000`，只记入 `60000`，丢掉 `240000`（`0.80`），扰动 `0.05`，用户态占比被挤到 `0.76`，`kv_get` 自己的占比降到 `0.665`（`39900` 个样本）。更高频率确实多留下了样本（`60000` 大于 `30000`），分辨率的潜力更大，但丢样和扰动也上来了，热点的份额已经不再是那组干净的 `0.70`。

## 常见误区

节点写出的误区是：空调用栈仍据此断言没有瓶颈。省略帧指针再用 `fp` 展开时，`30000` 个样本的栈全是空的，报告上的热点是 `[unknown]`。若只看调用栈，会误以为「什么函数都没采到，大概没有热点」。同一组数据里，用户态占比仍是 `0.80`，内核态仍是 `0.20`，`stat` 的用户周期仍是 `24000000`。换成 `dwarf`（并且保留 `-g`）之后，热点立刻回到 `kv_get` 的 `0.70`。空的是展开，不是工作。要先看样本数、丢样和用户 / 内核占比，再决定这张栈能不能用来下结论。

## 设计权衡

节点写出的权衡是：更高采样频率提升分辨率，也增加扰动。99 Hz 也能认出 `kv_get`，但次要符号的比例已经出现 `0.1502` 与 `0.0498` 这种取整偏差。1000 Hz 在本课的教学缓冲区里刚好不丢样，比例是整数。10000 Hz 留下了更多记入样本，却丢掉了 `80%` 的意图样本，并把 `5%` 的样本记到剖析器自己的处理函数上，`kv_get` 从 `0.70` 降到 `0.665`。分辨率不是只看频率数字，还要看实际记入了多少、丢了多少、热点有没有被扰动挪走。

## 动手实践：以 -g -fno-omit-frame-pointer 构建 KV，采样 30 秒并核对热点

配套代码在 [`/workspace/atlas/lessons/practice131/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice131.tar.gz)。`engine.py` 是 `PerfEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_perf.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，用 `-g -fno-omit-frame-pointer` 构建教学 KV，并在 `30.00` 的窗口里采样、核对热点；跑的是教学引擎，不是宿主 `perf`，也不是宿主 `g++`。

请先自己把教学编译器一行、`stat` 的用户 / 内核占比，以及 1000 Hz 下的样本数、丢样和 `report` 前几名记进一张表，确认编译器是 `AtlasTeachCC 1.0`、旗标是 `-g -fno-omit-frame-pointer`，用户态 `0.80`、内核态 `0.20`，意图和记入都是 `30000`、丢样 `0`，热点 `kv_get` 自己占 `21000`（`0.70`）。然后再核对另外三组：去掉 `-g` 后用户态热点变成 `0x401000`；省略帧指针再用 `fp` 时 `30000` 个栈全空，热点是 `[unknown]`，改用 `dwarf` 后回到 `kv_get`；99 Hz 记入 `2970` 且不丢样，10000 Hz 记入 `60000`、丢掉 `240000`，`kv_get` 降到 `0.665`。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_reports_samples_and_lost`、`acceptance_explains_user_kernel`、`practice_hotspot_checked`、`pitfall_empty_stacks_not_no_bottleneck`、`tradeoff_freq_vs_perturbation`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_perf_product`、`not_host_perf`、`not_host_gxx`、`cycles_are_teaching`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Linux perf — Tutorial 中与 Linux perf 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://perf.wiki.kernel.org/index.php/Tutorial](https://perf.wiki.kernel.org/index.php/Tutorial) 先 301 到 kernel.org 的旧 wiki 归档，再 301 到 `https://perfwiki.github.io/`，该页只有 73 字节的刷新标签。教程正文在跟随打开的 [https://perfwiki.github.io/main/tutorial/](https://perfwiki.github.io/main/tutorial/)，200、无跳转、217182 字节，`<title>` 为 Introduction - perf: Linux profiling with performance counters，`h1` 为 Linux kernel profiling with perf，`Last-Modified` 为 `Wed, 26 Aug 2026 19:20:02 GMT`（北京时间 2026-08-27 03:20:02）。Markdown 源 78747 字节，最近一次提交者为 Ian Rogers，时间为 2026-04-28 03:57:55（北京时间），说明是格式整理。教程示例取自 Ubuntu 11.04 上的一次实测，没有署单一原作者。教程和首页都链向 Man pages，本课据此打开了 `perf-record(1)` / `perf-report(1)` / `perf-stat(1)`，用来补教程未定义的展开方式。没有再把未列出的博客算进结论。

教学实验按以下方式使用它，并记下差异。

- **stat / record / report**：教程给出了命令职责、默认 1000 Hz、用户 / 内核修饰符、`[.]` / `[k]` 以及「没有符号表就打印地址」。本课用教学计数复现这条链，并在 `30.00` 窗口里得到用户 `0.80`、内核 `0.20`、热点 `kv_get` 的 `0.70`。教程里 `dd`、`noploop`、`sleep 5` 的墙钟计数和估计样本数没有进入 `results.json`。教学引擎报告的是准确样本数，不使用教程所说的按文件字节数估计。
- **编译器 `-g` 与 `perf record -g`**：节点实践的 `-g -fno-omit-frame-pointer` 是构建教学 KV 的编译开关。教程火焰图例子里的 `-g` 是 `perf record` 打开调用栈。本课两者都模拟了，并在课文里分开写，避免把它们当成同一个旗标。本课没有调用宿主 `g++`，也没有生成真实 ELF。
- **frame pointer 与 DWARF**：这两个名字及其「省略帧指针会使 fp 失真、应改用 dwarf」的适用边界，写在 `perf-record(1)` 里，不在教程正文。本课按手册做对照，并用空栈和 `[unknown]` 作为教学上的 bogus call graph，不实现 libdw、LBR 或内核 ORC 展开。
- **采样频率和丢样**：频率与周期的定义来自教程；LOST events 只在手册的 `--threads` / mmap 相关讨论里出现。教学丢样按排出速率 `2000` 样本 / 秒计算，10000 Hz 下丢掉 `0.80`、并加入 `5%` 扰动。这些比例是教学参数，不是手册或教程里的实测丢失率。教程提到的 skid、频率缩放、`perf_event_paranoid` 权限，本课没有逐项做成实验。
- **时间含义**：教程示例是墙钟命令。本课全部是教学虚拟时钟上的值，不是生产 RTT，也不是本机跑了 30 秒 `perf record`。

## 自测题

**题 1.** `perf stat`、`perf record`、`perf report` 各自回答什么问题？在本课的 30 秒窗口里，怎样用样本数、丢样和用户 / 内核占比核对热点？适用边界在哪里？

<details>
<summary>答案</summary>

`stat` 汇总计数，适合看总量和用户 / 内核占比。`record` 按事件溢出记下样本，默认事件是 `cycles`，默认目标频率 1000 Hz。`report` 把样本按符号和特权级摊开，`[.]` 是用户态，`[k]` 是内核态。

教学实验以 `-g -fno-omit-frame-pointer` 构建 `teach-kv`，在虚拟时钟上采 `30.00`。`stat` 给出用户态 `0.80`、内核态 `0.20`（周期分别是 `24000000` 和 `6000000`）。`record` / `report` 在 1000 Hz 下意图和记入都是 `30000`，丢样 `0`，`kv_get` 自己占 `21000`（`0.70`，`[.]`），`copy_to_user` 占 `4500`（`0.15`，`[k]`）。用户态合计 `24000`，与 `stat` 的 `0.80` 一致，热点就是 `kv_get`。验收要求把样本数、丢样和这份占比一起报告。

适用边界是：这组数字回答「CPU 花在哪一个符号、哪一个特权级」，不能代替上一课按到达过程计算的 p99。教程还提醒，样本里的指令指针可能带着 skid，`cycles` 也不一定与墙钟成正比。没有符号时，用户态热点会变成 `0x401000`，就不能再按函数名去对热点。

</details>

**题 2.** 为什么编译时的 `-g`、`-fno-omit-frame-pointer` 和 `perf record -g` 不是一回事？省略帧指针之后，为什么空调用栈不能用来断言没有瓶颈？

<details>
<summary>答案</summary>

节点实践里的 `-g` 是给教学二进制留下符号和 DWARF CFI，`-fno-omit-frame-pointer` 是留下帧指针。教程例子里的 `perf record -g` 是采样时把调用栈记下来，默认用户态按 `fp` 展开。手册写明：若构建时用了 `--fomit-frame-pointer`，再按 `fp` 展开会得到虚假的调用栈，应改用 `dwarf`。

教学对照里，保留帧指针并用 `fp` 时，`kv_get` 的栈是 `main` → `handle_get` → `kv_get`。改成 `-fomit-frame-pointer` 仍用 `fp`：`30000` 个栈全部为空，热点显示 `[unknown]`，但用户态占比仍是 `0.80`，记入样本仍是 `30000`。CPU 工作还在，只是展开失败了。同一份二进制改用 `dwarf`（并且仍有 `-g`），热点回到 `kv_get`，空栈为 `0`。没有 `-g` 时，`dwarf` 也会全部空栈。所以空调用栈不能用来断言没有瓶颈，要先看样本、丢样和用户 / 内核占比，再换一种展开或补上符号。

</details>

**题 3.** 把采样频率从 99 Hz 加到 10000 Hz，分辨率、丢样和扰动会怎样变？请用本课的记入样本数和 `kv_get` 占比说明。

<details>
<summary>答案</summary>

教程默认按 1000 Hz 采样，也可以用 `-F` 改频率。本课在同一段 `30.00` 窗口里比较三档。99 Hz 记入 `2970`，不丢样，`kv_get` 仍是 `0.70`，但 `copy_to_user` 是 `0.1502`、`sys_recv` 是 `0.0498`，次要份额已经有取整误差。1000 Hz 记入 `30000`，丢样 `0`，四个符号的占比正好是 `0.70` / `0.15` / `0.10` / `0.05`。10000 Hz 意图 `300000`，只记下 `60000`，丢掉 `240000`（`0.80`），另有 `5%` 的教学扰动记到 `perf_overflow_handler` 上，用户态降到 `0.76`，`kv_get` 降到 `0.665`。

更高频率确实多留下了样本，也更有希望把小份额测稳，但本课的教学缓冲区在 10000 Hz 已经跟不上，丢样和扰动会把热点份额挪走。分辨率要看实际记入的样本和是否被扰动，不能只看频率数字。这些丢样比例是教学模型，不是教程示例里的丢失率。

</details>

## 推荐资源

- [Linux perf — Tutorial](https://perfwiki.github.io/main/tutorial/)（Linux perf 教程现行正文，英文，进阶，免费）：用于核对 `perf stat` / `record` / `report`、默认 1000 Hz 以及用户 / 内核符号列，并将结论落到本节点的教学 KV 采样实验。节点给出的旧地址 [perf.wiki.kernel.org/index.php/Tutorial](https://perf.wiki.kernel.org/index.php/Tutorial) 在 2026-11-14 经两次 301 到达 `https://perfwiki.github.io/` 的 73 字节刷新页；教程正文 217182 字节，`<title>` 为 Introduction - perf: Linux profiling with performance counters，`h1` 为 Linux kernel profiling with perf，`Last-Modified` 为 `Wed, 26 Aug 2026 19:20:02 GMT`。教程定义计数、采样和报告，并不定义 frame pointer 与 DWARF。本课的教学编译器、虚拟时钟上的 30 秒、教学丢样模型，都是对照，不是该页 Ubuntu 11.04 示例的墙钟计数。
- [perf-record(1)](https://man7.org/linux/man-pages/man1/perf-record.1.html)（手册页，英文，进阶，免费）：教程「Man pages」索引指向它。2026-11-14 返回 200，58884 字节，`<title>` 为 perf-record(1) - Linux manual page，`Last-Modified` 为 `Sun, 08 Nov 2026 07:37:16 GMT`，页脚为 perf 2026-01-13。该页定义 `--call-graph` 的 `fp` / `dwarf` / `lbr`，并警告 `--fomit-frame-pointer` 会使 `fp` 失真。本课按这条边界做展开对照，不实现 LBR 或真实 libdw。

## 本课小结

- perf 用计数和采样定位 CPU 消耗，调用栈质量依赖符号、展开策略和权限。不要在空调用栈上断言没有瓶颈。
- 以 `-g -fno-omit-frame-pointer` 构建教学 KV，在虚拟时钟上采 `30.00`、1000 Hz：意图和记入都是 `30000`，丢样 `0`，用户态 `0.80`，内核态 `0.20`，热点 `kv_get` 自己占 `0.70`。验收要同时报告样本数、丢样和这份占比。
- 编译器的 `-g` 和 `perf record -g` 不是同一个旗标。省略帧指针后再用 `fp`，`30000` 个栈全空，热点变成 `[unknown]`，但用户态仍是 `0.80`；改用 `dwarf`（并保留 `-g`）后热点回到 `kv_get`。
- 更高频率能提高分辨率，也增加丢样和扰动。99 Hz 记入 `2970` 且开始有取整误差；10000 Hz 记入 `60000`、丢掉 `0.80`，`kv_get` 降到 `0.665`。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 `perf` 或教程里 Ubuntu 11.04 的墙钟计数。`PerfEngine` 不是 Redis，也不是 Linux `perf` 或宿主机 g++。frame pointer 与 DWARF 的定义来自跟随阅读的手册，不是教程正文。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「先 record 再 report」「没有符号表就只剩地址」以及手册里「省略帧指针时 fp 会得到虚假调用栈」这几段，不要把空栈当成没有热点，也不要把频率数字当成实际记入的分辨率。
