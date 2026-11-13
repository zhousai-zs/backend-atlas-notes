# 第 132/208 课：火焰图与差分分析

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-flame` |
| 英文名 | Flame graphs |
| 领域 / 阶段 | I / 第 3 阶段 |
| 子类 | 剖析与诊断 |
| 难度 | 进阶 |
| 前置节点 | [`i-perf`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/131.html) |
| 标签 | `Flame graphs`、`Performance` |
| 参考资料 | [Brendan Gregg — FlameGraph](https://github.com/brendangregg/FlameGraph)（GitHub 仓库，英文，进阶，免费；2026-11-14 打开 [https://github.com/brendangregg/FlameGraph](https://github.com/brendangregg/FlameGraph) 返回 HTTP 200，无跳转，349840 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Fri, 13 Nov 2026 17:54:58 GMT`（北京时间 2026-11-14 01:54:58），`<title>` / `og:title` 为 GitHub - brendangregg/FlameGraph: Stack trace visualizer，仓库描述为 Stack trace visualizer。默认分支是 `master`（`main` 上的 README 返回 404）。GitHub API 给出的创建时间为 `2011-12-16T02:20:53Z`（北京时间 2011-12-16 10:20:53），最近一次 push 为 `2024-10-20T21:48:45Z`（北京时间 2024-10-21 05:48:45），仓库 `updated_at` 为 `2026-11-13T15:31:30Z`（北京时间 2026-11-13 23:31:30）。API 的 `license` 字段为空，根目录没有名为 `LICENSE` 的文件（raw 地址返回 404、14 字节）；`flamegraph.pl` 头部采用 CDDL-1.0，并指向 `docs/cddl1.txt`，版权行写 Copyright 2016 Netflix, Inc.、Copyright 2011 Joyent, Inc.、Copyright 2011 Brendan Gregg；`difffolded.pl` 头部则是 Copyright (c) 2014 Brendan Gregg，按 GNU GPL v2 或后续版本授权，注释写明 28-Oct-2014 创建。README 原文在 [https://raw.githubusercontent.com/brendangregg/FlameGraph/master/README.md](https://raw.githubusercontent.com/brendangregg/FlameGraph/master/README.md)，HTTP 200，无跳转，9291 字节，`Content-Type` 为 `text/plain`，`Date` 为 `Fri, 13 Nov 2026 17:54:59 GMT`（北京时间 01:54:59），一级标题为 `# Flame Graphs visualize profiled code`。该文件最近一次提交为 `b8989aab9a0815d44014f993127390200ecd23c7`，作者 Ben Olson，说明是 minwidth now accepts a percentage of time with %，提交时间 `2023-10-17T20:31:51Z`（北京时间 2023-10-18 04:31:51）。README 把流程写成三步：Capture stacks、Fold stacks、`flamegraph.pl`；折叠格式是分号连接的栈帧加空格和计数；选项里有 `--flamechart`（按时间排序且不合并栈）和 `--negate`（对调差分色）。仓库首页字段是 [http://www.brendangregg.com/flamegraphs.html](http://www.brendangregg.com/flamegraphs.html)，2026-11-14 该 HTTP 地址返回 500、0 字节，`Date` 为 `Fri, 13 Nov 2026 17:55:47 GMT`（北京时间 01:55:47）；改用 [https://www.brendangregg.com/flamegraphs.html](https://www.brendangregg.com/flamegraphs.html) 返回 200，无再跳转，64420 字节，`Content-Type` 为 `text/html`，`Date` 为 `Fri, 13 Nov 2026 17:55:48 GMT`（北京时间 01:55:48），`<title>` 为 Flame Graphs。该页 *Summary* 写明横轴是栈样本总体、按字母排序，「不是时间流逝」，纵轴是从底部从零计的栈深度，框越宽表示该帧在栈里出现得越频繁，顶边是正在 on-CPU 的帧、下面是祖先。README 还指向 CPU 剖析页 [http://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html](http://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html)，同样是 HTTP 500、0 字节；HTTPS 地址 [https://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html](https://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html) 返回 200，52022 字节，`Date` 为 `Fri, 13 Nov 2026 17:55:50 GMT`（北京时间 01:55:50），`<title>` 为 CPU Flame Graphs。README 正文**没有**把 on-CPU 与 off-CPU、差分图写成独立小节。本课跟随阅读了首页点名的 [Off-CPU Flame Graphs](https://www.brendangregg.com/FlameGraphs/offcpuflamegraphs.html)（HTTP 200，38281 字节，`Date` 为 `Fri, 13 Nov 2026 17:56:17 GMT`，北京时间 01:56:17，`<title>` 为 Off-CPU Flame Graphs；站点根路径 `/offcpuflamegraphs.html` 返回 404）以及 [Differential Flame Graphs](https://www.brendangregg.com/blog/2014-11-09/differential-flame-graphs.html)（HTTP 200，27199 字节，`Date` 为 `Fri, 13 Nov 2026 17:56:35 GMT`，北京时间 01:56:35，`<title>` 为 Differential Flame Graphs，文内日期为 09 Nov 2014；对应 HTTP 地址返回 500）。`flamegraph.pl` 原文 39741 字节、`difffolded.pl` 原文 3640 字节、`docs/cddl1.txt` 18924 字节，均为 raw 地址 HTTP 200。仓库脚本与这些被点名的页面给出了折叠格式、横轴不是时间、on-CPU / off-CPU 以及红蓝差分的定义；本课没有把未打开的 ACM Queue / CACM 文章算进结论。） |
| 摘要 | 火焰图横向宽度表示采样累计权重，纵向表示调用关系，不是时间轴。解释图形前先确认采样事件、栈展开方式及符号是否足够完整。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teachkv`」都是**教学假设**，用来让短连接与长连接、syscall 与序列化、on-CPU 与 off-CPU 以及两版差分变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在某台机器上调用了宿主 `perf`、`g++` 或 Brendan Gregg 的 `flamegraph.pl` / `stackcollapse-*` / `difffolded.pl`。配套代码是 Python 3 标准库写的教学引擎 `FlameEngine`，**不是 Redis，不是真实的火焰图产品，不是网络服务，不是 Linux 的 `perf` 工具，也不是宿主机上的 Perl 脚本或 g++ / clang**。它不写 SVG 产品文件，不采集硬件 PMU，也不把某一段墙钟 `perf record -- sleep 60` 当成实验结果。README 示例里的 99 Hz、60 秒，以及首页、博客里的生产剖析图，都没有进入 `results.json`。采样按教学虚拟时钟上的 1000 Hz 进行；短连接跑完 `2000` 次请求的墙钟是 `30.00`，长连接是 `16.00`，都是虚拟时钟上的秒，不是生产 RTT，也不是本机等待了三十秒。折叠行格式、横轴按字母排序、差分图用第二份折叠文件决定宽度、`-n` 按总数归一化以及 `--negate` 对调红蓝，是对照官方仓库和被点名页面的教学实现。on-CPU 与 off-CPU 的定义来自跟随阅读的官方首页和 Off-CPU 页，不是 README 正文里的独立小节。本课所有数字都来自 `/workspace/atlas/lessons/practice132/results.json`，是**教学虚拟时钟上的数字**。

## 导读

### 上一课留下的缺口

上一课的 `PerfEngine` 解决的是：一次 30 秒的教学采样能不能信，取决于符号、栈展开和丢样。以 `-g -fno-omit-frame-pointer` 构建时，1000 Hz 下热点是 `kv_get`；省略帧指针再用 `fp`，`30000` 个栈会全部变成 `[unknown]`。那一课的边界停在**样本和调用栈是否完整**，没有回答另一件事：完整的栈怎样画成一张图，横轴是不是时间先后，短连接和长连接把时间花在 syscall 还是序列化上，以及优化前后两张图该怎样并排比较。

节点把摘要写成：火焰图横向宽度表示采样累计权重，纵向表示调用关系，不是时间轴。解释图形前先确认采样事件、栈展开方式及符号是否足够完整。只看见左边一截、右边一截，不能据此断言谁先执行。

### 接上前置

前置 [第 131 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/131.html) `i-perf` 留下的结论是：先看样本数、丢样、用户 / 内核占比和符号是否完整，再决定这张栈能不能用来下结论。本课假定展开和符号已经够用，把同一条取出 `Weekly digest` 的路径折成一行，再按官方火焰图的规则铺开：宽度是累计权重，高度是深度，左右顺序是字母，不是时间。

官方仓库的 README 把流程写成三步：先采集调用栈，再用 `stackcollapse-*` 折成一行，最后交给 `flamegraph.pl` 画 SVG。`flamegraph.pl` 的头部写明：「横轴上的顺序没有含义；数据既然是样本，事件的时间顺序并不知道。实际采用的顺序是按函数名字母排序。」首页 *Summary* 用另一句话重复了同一条边界：横轴是栈样本总体，按字母排序，不是时间流逝；纵轴从底部从零计算栈深度；框越宽，该帧在栈里出现得越频繁。本课用教学引擎复现折叠、字母布局和差分，而不是在本机执行这些 Perl 脚本。

### 教学场景（教学假设）

假设内容后台有一个教学 KV：请求打来，就按键 `article:01:title` 取出标题 `Weekly digest`。这一课比较两种压测方式。短连接每次请求都要 `accept`、收发再 `close`；长连接复用已经建立的连接，不再为每次请求付连接建立和拆除的 syscall。两边取出的标题相同，序列化路径也相同。剖析在教学虚拟时钟上按 1000 Hz 采样，on-CPU 与 off-CPU 分成两份事件，不画在同一张未标明事件的图里。比较两版时，两边都完成 `2000` 次请求，而不是只切同一段墙钟窗口。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT，更不是宿主 `flamegraph.pl` 或 `perf record` 的墙钟结果。

### 本课三条主线

1. **折叠调用栈**：把同一次采样里的根到叶帧写成 `frame1;frame2;frame3 count`，再按字母顺序合并相同的栈。适用边界是：它回答「哪些路径累计更宽、哪一层更深」，不把左右位置读成执行先后；符号或展开不完整时，图上只会留下一块 `[unknown]`，不能当作热点本身。
2. **on-CPU 与 off-CPU**：on-CPU 图的顶边是正在占用 CPU 的帧；off-CPU 图的宽度是阻塞时间，不是 CPU 消耗。短连接会多出 `wait_accept`，不能把这份宽度读进 on-CPU 成本里。
3. **差分图**：用两份折叠文件做红蓝对比，默认宽度取第二份（「之后」），颜色表示相对第一份发生了什么。适用边界是：必须保持相同负载再比较；只看相对宽度、或在负载已经对齐时再做总数归一化，都会把本来没变的序列化路径画成「变红」。

## 学习目标

- 能解释折叠调用栈及适用边界：一行一个栈加计数，合并后宽度是累计权重、高度是调用深度；横轴按字母排列，不是时间轴；解释图形前要确认采样事件、展开方式和符号是否完整。
- 独立完成节点实践：为短连接和长连接压测生成火焰图，对比 syscall 与序列化成本，并在相同请求次数下核对原始计数。
- 能说明宽栈和深栈的含义，以及为什么比较两版时要保持相同负载，不能只凭图上的相对宽度下结论。

## 为什么后端要懂

节点对「为什么」写得很干脆：把优化前后调用路径变化可视化。放到本课的教学内容后台：短连接取出 `Weekly digest` 时，on-CPU 样本里 syscall 占 `12000`（`0.60`），序列化 inclusive 占 `6000`（`0.30`）；改成长连接后，syscall 降到 `4000`（`0.3333`），序列化的原始计数仍是 `6000`，但在图上的宽度变成了 `0.50`。若只盯着那根变宽的 `serialize_title`，会以为序列化变贵了；并排看原始计数，才会看到真正减少的是 `sys_accept` 和 `sys_close`。差分图就是为这种前后对比准备的，但图仍然只指出方向，结论要落回计数。

## 核心知识点

### 1. 折叠调用栈

README 把第二步叫 Fold stacks：`stackcollapse-perf.pl` 一类工具把多行栈折成「分号分隔的帧 + 空格 + 计数」。教学引擎用同一格式。短连接 on-CPU 的折叠结果里有 `teachkv;accept_loop;sys_accept 4000`、`teachkv;handle_get;serialize_title 4000`、`teachkv;handle_get;serialize_title;json_write 2000` 等七行，合计 `20000` 个样本；长连接少了 accept / close 两行，合计 `12000`。

按字母布局之后，宽度是 inclusive 权重。短连接这张 on-CPU 图里，根帧 `teachkv` 占满 `1.0`；非根最宽的是 `handle_get`，inclusive `8000`、宽度 `0.4`、深度 `1`，这是**宽栈**——它出现在很多样本的调用路径上。最深的是 `json_write`，深度 `3`（从底部的 `teachkv` 起算为 0），inclusive `2000`、宽度 `0.1`，这是**深栈**——层数多，并不自动等于更热。`serialize_title` 自己独占 `4000`，加上孩子 `json_write` 后 inclusive 是 `6000`（`0.30`）；`sys_accept` 作为叶子宽 `0.20`。官方首页写的是：框越宽，该帧在栈里出现得越频繁；纵轴是调用深度，不是时间。

适用边界在解释图形之前。节点要求先确认采样事件、栈展开方式和符号是否足够完整。把符号关掉之后，同一份短连接 on-CPU 样本会折成单独一行 `[unknown] 20000`，图上只剩深度 `0` 的一整块宽条。这与上一课省略帧指针后的空栈是同一类问题：宽的是「无法命名的样本」，不是某个真正的热点函数。本课默认对照使用完整符号和 `fp` 展开，不再重复上一课的编译开关实验。

### 2. on-CPU 与 off-CPU

这两个名字以及「顶边是正在 on-CPU 的帧」这一句，写在仓库首页和 CPU / Off-CPU 页上，而不是 README 的独立小节。Off-CPU 页把 off-CPU 写成进程或线程不在 CPU 上运行的时间，包括在系统调用里阻塞，也包括非自愿切换和调度延迟。本课把两种事件分成两张图。

短连接在虚拟时钟上跑完 `2000` 次请求要 `30.00`：on-CPU 样本 `20000`，off-CPU 样本 `10000`。off-CPU 折叠是 `teachkv;accept_loop;wait_accept 6000` 和 `teachkv;conn_read;wait_recv 4000`，其中 `wait_accept` 宽 `0.60`，是这张 off-CPU 图上最醒目的塔。长连接跑完同样 `2000` 次只要 `16.00`：on-CPU `12000`，off-CPU 只剩 `wait_recv 4000`，不再为每次请求等待 `accept`。`wait_recv` 的原始计数两边都是 `4000`，并没有随长连接变少。

适用边界是：宽塔首先要问「这是哪一种采样事件」。`wait_accept` 的宽度是阻塞，不是 syscall 在 CPU 上的花费；`sys_accept` 的 `4000` 才是 on-CPU 里建立连接的成本。把两份事件混在一张未标明的图里，syscall 与等待会被加到同一根条上。

### 3. 差分图

`difffolded.pl` 的用法是 `difffolded.pl folded1 folded2 | flamegraph.pl`，输出三列：折叠栈、第一份计数、第二份计数。缺席的栈补零。`flamegraph.pl` 用第二份决定宽度，并用红 / 蓝表示增多 / 减少；颜色按该函数自己贡献的差额（exclusive），不包括孩子。官方博客把「之前」对「之后」写成两张图：`diff2.svg` 的宽度是之后的剖面，颜色表示已经发生的变化；把两份输入对调再加 `--negate`，得到 `diff1.svg`，宽度是之前的剖面，颜色表示即将发生的变化。完全消失的路径在只看 `diff2` 时没有格子可涂蓝，这是博客明确写过的问题。

本课把短连接当作之前、长连接当作之后，负载都是 `2000` 次取标题。未归一化的 `diff2` 里，`serialize_title` 两边都是 exclusive `4000`，颜色是灰；`sys_accept` 和 `sys_close` 从 `4000` 变成 `0`，在第二份宽度里消失，exclusive 差额是 `-4000`，按规则记为蓝。对调并做教学版 `--negate` 之后，`sys_accept` 重新出现在以短连接为宽度的图上，颜色仍是蓝，表示这条路径将会变少。这与官方「消失的帧要看 diff1」的处理是同一条边界。

`difffolded.pl -n` 会把第一份的计数按整数缩放到第二份的总数。官方博客说，它用来避免「一天里不同时刻、负载本身不同」时整张图全红或全蓝。本课的请求次数已经对齐，on-CPU 总数不同是因为短连接多做了 accept / close，不是因为两边负载不一致。若仍做 `-n`，`serialize_title` 会被从 `4000` 缩成 `2400` 再去和长连接的 `4000` 比，差额 `1600`、颜色变红；`json_write` 从 `2000` 缩成 `1200`，差额 `800`，同样变红。共享路径并没有变贵，是归一化把额外的 syscall 样本摊掉了。相同负载下应比较原始计数；`-n` 不能代替对齐负载。

## 常见误区

节点写出的误区是：把横轴当作执行先后顺序。短连接一次请求的真实顺序是 `accept_loop` → `conn_read` → `handle_get` → `conn_write` → `conn_close`。按字母布局后，`teachkv` 下的兄弟顺序变成 `accept_loop`、`conn_close`、`conn_read`、`conn_write`、`handle_get`。`conn_close` 的左边缘在 `x=4000`，`handle_get` 在 `x=12000`：后发生的关闭出现在序列化的左边。教学版的 flame chart（对照 README 的 `--flamechart`：按时间排序、不合并栈）会把叶子顺序恢复成 `sys_accept`、`sys_recv`、`parse_cmd`、`serialize_title`、`json_write`、`sys_send`、`sys_close`，那张图的横轴才是时间。官方首页也写过：火焰图按字母重排是为了尽量合并、看清整体；火焰图和火焰图时序图不是同一个东西，有的工具把后者误称为前者。看见左边的塔、右边的塔，不能读成「先跑左边、再跑右边」。

## 设计权衡

节点写出的权衡是：图形有助定位方向，仍需原始计数支持结论。长连接的 on-CPU 图上，`serialize_title` 的宽度从 `0.30` 变成 `0.50`，`handle_get` 从 `0.40` 变成 `0.6667`，看起来像序列化变重了；两边 inclusive 计数其实都是 `6000`，syscall 的 exclusive 计数才从 `12000` 降到 `4000`。差分图把减少的 accept / close 标成蓝，把没变的序列化标成灰，方向是对的，但若只保存一张图、不记下这两列原始计数，相对宽度仍然会骗人。

再看负载是否对齐。两边都跑完 `2000` 次时，序列化计数相同。若只因为长连接在 `16.00` 已经结束，就把短连接也切在同一段虚拟窗口里，短连接只完成 `1066` 次、另有 `1` 次未跑完，on-CPU 样本变成 `10665`，序列化 inclusive 只剩 `3199`，会误以为序列化变少了。这不是优化效果，是比较了不同的工作量。图可以指出该往哪看，结论要落回「相同负载下的原始计数」。

## 动手实践：为短连接和长连接压测生成火焰图，对比 syscall 与序列化成本

配套代码在 [`/workspace/atlas/lessons/practice132/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice132.tar.gz)。`engine.py` 是 `FlameEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_flame.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，为短连接和长连接压测生成火焰图，对比 syscall 与序列化成本；跑的是教学引擎，不是宿主 `perf`，也不是宿主 `flamegraph.pl`。

请先自己把两种连接在相同 `2000` 次请求下的 wall 时间、on-CPU / off-CPU 样本数、syscall 与序列化的原始计数和宽度记进一张表，确认短连接 on-CPU 合计 `20000`、长连接 `12000`，syscall exclusive 从 `12000` 降到 `4000`，序列化 inclusive 都是 `6000`，但宽度从 `0.30` 变成 `0.50`。然后再核对另外三组：off-CPU 里短连接多出 `wait_accept 6000`，`wait_recv` 两边都是 `4000`；未归一化的差分里序列化是灰色、`sys_accept` / `sys_close` 在之后的宽度里消失，错误地加上 `-n` 会让 `serialize_title` 变红、差额变成 `1600`；把短连接切到长连接结束的 `16.00` 时，只完成 `1066` 次，序列化计数变成 `3199`，不再是相同负载。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_explains_wide_and_deep`、`acceptance_same_load_comparison`、`pitfall_x_axis_not_time_order`、`tradeoff_picture_needs_raw_counts`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_flamegraph_product`、`not_host_flamegraph_pl`、`not_host_stackcollapse`、`not_host_difffolded_pl`、`not_host_perf`、`not_host_gxx`、`times_are_teaching_virtual_clock`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Brendan Gregg — FlameGraph 中与 Flame graphs 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://github.com/brendangregg/FlameGraph](https://github.com/brendangregg/FlameGraph) 返回 200、无跳转、349840 字节，`<title>` 为 GitHub - brendangregg/FlameGraph: Stack trace visualizer。默认分支是 `master`。仓库声明的主页是 `http://www.brendangregg.com/flamegraphs.html`，该 HTTP 地址当天返回 500；本课改用同一路径的 HTTPS，得到 64420 字节的 Flame Graphs 首页。CPU 页同样是 HTTP 500、HTTPS 200（52022 字节）。本课据此继续打开了首页点名的 Off-CPU Flame Graphs（38281 字节）和 Differential Flame Graphs（27199 字节，文内日期 09 Nov 2014），并阅读了 `README.md`（9291 字节）、`flamegraph.pl`（39741 字节）、`difffolded.pl`（3640 字节）和 `docs/cddl1.txt`。没有再把未打开的 ACM Queue / CACM 文章算进结论。

教学实验按以下方式使用它，并记下差异。

- **折叠调用栈**：README 给出三步流程和 `func;func;func count` 格式，`flamegraph.pl` 头部与首页 *Summary* 给出「横轴不是时间、按字母排序、宽度表示出现频率、纵轴是深度」。本课用教学折叠和字母布局复现这一点，短连接 on-CPU 得到 `20000` 个样本、七行折叠，宽栈 `handle_get` 为 `0.40`，深栈 `json_write` 深度为 `3`。本课没有调用宿主 `stackcollapse-*.pl` 或 `flamegraph.pl`，也不输出官方 SVG。
- **on-CPU 与 off-CPU**：这两个术语的定义在首页和 Off-CPU 页，不在 README 正文。本课按那两页把事件拆开，用教学虚拟时钟上的 on-CPU / off-CPU 样本对照，不实现 eBPF、调度跟踪或官方的 wakeup / chain graph。
- **差分图**：`difffolded.pl` 与 2014-11-09 的博客给出两列计数、默认用第二份决定宽度、红增蓝减、消失路径要看对调后的 `--negate` 图，以及 `-n` 用来对齐不同负载。本课用短 / 长连接作为前后两版，演示了灰色的序列化、消失的 accept / close，以及在负载已经对齐时误用 `-n` 会让序列化变红。教学差分不实现博客里提到的 elided flame graph、CPI flame graph 或第三方的三图对照。
- **符号与展开**：节点要求解释图形前先确认采样事件、展开和符号。Off-CPU 页也提到 `-fomit-frame-pointer` 会破坏基于帧指针的栈。本课只做了一个对照：去掉符号后折成 `[unknown] 20000`。展开失败的细节见上一课，不在本课重做宿主 `perf`。
- **时间含义**：README 和博客里的采集示例是墙钟命令（例如 `perf record -F 99 -a -g -- sleep 60`）。本课全部是教学虚拟时钟上的值，不是生产 RTT，也不是本机跑了 30 秒或 60 秒的 `perf record`。短连接的 `30.00` 是 `2000` 次请求跑完的虚拟时长，不是把官方 60 秒示例改写进结果。

## 自测题

**题 1.** 折叠调用栈之后，宽栈和深栈分别表示什么？在本课的短连接 on-CPU 图里，怎样读 `handle_get`、`serialize_title` 和 `json_write`？解释这张图前还要确认什么？适用边界在哪里？

<details>
<summary>答案</summary>

折叠格式是一行一个根到叶的栈，后面跟计数。合并之后，横向宽度是该帧的采样累计权重（inclusive），纵向是调用深度，从底部的根帧以 0 起算。宽栈表示这条路径出现在更多样本里，深栈只表示层数多，不自动等于更热。

短连接 on-CPU 合计 `20000`。非根最宽的是 `handle_get`，inclusive `8000`、宽度 `0.4`、深度 `1`。`serialize_title` inclusive `6000`（自己 `4000`，加上 `json_write` `2000`），宽度 `0.30`。最深的是 `json_write`，深度 `3`，宽度只有 `0.1`。所以 `handle_get` 更宽，`json_write` 更深但不是最热的叶子；`sys_accept` 作为叶子宽 `0.20`，比 `json_write` 更宽。

解释之前要确认采样事件（这张是 on-CPU，不是 off-CPU）、栈展开方式和符号是否完整。同一组请求去掉符号后，图上只剩 `[unknown] 20000`，不能把这一整块当成热点函数。适用边界是：这张图回答「哪些路径累计更重、哪一层更深」，不能把左右位置读成执行顺序，也不能代替下一张差分里对两版计数的比较。

</details>

**题 2.** 为什么比较短连接和长连接时要保持相同负载？只看序列化在图上变宽，或把两份剖面切到同一段 `16.00` 窗口，分别会得到什么结论？官方的 `-n` 在这里能不能代替对齐负载？

<details>
<summary>答案</summary>

节点的验收要求是：保持相同负载比较两版。本课的相同负载是两边都完成 `2000` 次取出 `Weekly digest` 的请求。这时 syscall exclusive 从 `12000` 降到 `4000`，序列化 inclusive 都是 `6000`。长连接图上序列化宽度从 `0.30` 变成 `0.50`，只是因为分母从 `20000` 变成了 `12000`，不是序列化变贵。

若按长连接结束的 `16.00` 去切短连接，短连接只完成 `1066` 次、另有 `1` 次未跑完，序列化 inclusive 变成 `3199`，会误以为序列化减少了。那是工作量不同，不是同一负载。

`difffolded.pl -n` 把第一份总数缩放到第二份，官方用来处理「不同时刻负载本身不同」。本课请求次数已经对齐，总数不同是因为短连接多做了 accept / close。再做 `-n` 会把 `serialize_title` 从 `4000` 缩成 `2400`，差额 `1600`、颜色变红，共享路径被画成增长。所以 `-n` 不能代替对齐负载；图可以指出方向，结论要落回原始计数。

</details>

**题 3.** 为什么不能把火焰图的横轴当作执行先后顺序？短连接里 `conn_close` 和 `handle_get` 的左右位置说明了什么？这和 `--flamechart` 有什么不同？

<details>
<summary>答案</summary>

`flamegraph.pl` 的头部写明横轴顺序没有含义，实际按函数名字母排序；首页 *Summary* 也写横轴不是时间流逝。短连接一次请求的真实顺序是先 `accept_loop`，最后才 `conn_close`。按字母布局后，兄弟顺序是 `accept_loop`、`conn_close`、`conn_read`、`conn_write`、`handle_get`。`conn_close` 的左边缘在 `x=4000`，`handle_get` 在 `x=12000`，后发生的关闭画在序列化左边。

README 里的 `--flamechart` 才按时间排序并且不合并栈。教学对照里，这张时序图的叶子顺序是 `sys_accept`、`sys_recv`、`parse_cmd`、`serialize_title`、`json_write`、`sys_send`、`sys_close`，横轴这时才表示时间。官方也提醒过：有的工具把火焰图时序图误称为火焰图。看火焰图时，不要把左右读成先后。

</details>

## 推荐资源

- [Brendan Gregg — FlameGraph](https://github.com/brendangregg/FlameGraph)（GitHub 仓库，英文，进阶，免费）：节点给出的官方出处。2026-11-14 返回 200，349840 字节，`<title>` 为 GitHub - brendangregg/FlameGraph: Stack trace visualizer，默认分支 `master`。用于核对折叠格式、三步流程、`--flamechart` 与 `--negate`。仓库根目录没有 `LICENSE` 文件；`flamegraph.pl` 为 CDDL-1.0，`difffolded.pl` 为 GPL v2。本课的教学引擎、虚拟时钟上的 1000 Hz 和短 / 长连接计数，都是对照，不是在宿主上执行这些 Perl 脚本，也不是把 README 里 99 Hz、60 秒的示例写进 `results.json`。
- [Flame Graphs](https://www.brendangregg.com/flamegraphs.html)（仓库主页，英文，进阶，免费）：README 标明的 Main Website。2026-11-14 HTTP 地址返回 500；HTTPS 返回 200，64420 字节，`<title>` 为 Flame Graphs。该页定义横轴不是时间、宽度是出现频率、顶边是 on-CPU。本课按这一页理解宽栈 / 深栈，并跟随打开了它点名的 [CPU Flame Graphs](https://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html)、[Off-CPU Flame Graphs](https://www.brendangregg.com/FlameGraphs/offcpuflamegraphs.html)（38281 字节）和 [Differential Flame Graphs](https://www.brendangregg.com/blog/2014-11-09/differential-flame-graphs.html)（27199 字节，09 Nov 2014）。on-CPU / off-CPU 与红蓝差分的定义以这些被点名的页面为准，而不是 README 正文里并不存在的独立小节。

## 本课小结

- 火焰图的横向宽度是采样累计权重，纵向是调用关系，横轴不是时间轴。解释图形前要确认采样事件、栈展开和符号是否完整。
- 短连接与长连接在相同的 `2000` 次请求下：短连接虚拟时长 `30.00`、on-CPU `20000`，长连接 `16.00`、on-CPU `12000`。syscall exclusive 从 `12000` 降到 `4000`，序列化 inclusive 都是 `6000`，但宽度从 `0.30` 变成 `0.50`。宽栈以 `handle_get` 的 `0.40` 为例，深栈以 `json_write` 的深度 `3` 为例。
- on-CPU 与 off-CPU 是不同事件。短连接 off-CPU 多出 `wait_accept 6000`；`wait_recv` 两边都是 `4000`。不要把阻塞宽度读成 CPU 成本。
- 差分图默认用之后的剖面决定宽度。未归一化时序列化是灰色，accept / close 在之后的图里消失；对调并取反色才能看见它们变少。负载对齐后再做 `-n`，会让序列化误变成红色差额 `1600`。切到同一段 `16.00` 窗口则只完成 `1066` 次，序列化变成 `3199`，那不是相同负载。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 `perf` / `flamegraph.pl` 或官方示例里的墙钟计数。`FlameEngine` 不是 Redis，也不是真实的火焰图产品。on-CPU / off-CPU 的定义来自跟随阅读的官方页面，不是 README 正文。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「横轴不是时间」「宽度取第二份折叠文件」以及「比较前先对齐负载」这几段，不要把左右位置当成执行顺序，也不要把相对宽度当成原始成本。
