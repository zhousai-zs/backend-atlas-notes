# 第 139/208 课：io_uring 与异步完成

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-uring` |
| 英文名 | io_uring |
| 领域 / 阶段 | I / 第 4 阶段 |
| 子类 | 硬件与IO优化 |
| 难度 | 深入 |
| 前置节点 | [`i-zero-copy`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/138.html)、[`d-proactor`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/62.html) |
| 标签 | `io_uring`、`Performance` |
| 参考资料 | [Linux man-pages — io_uring(7)](https://man7.org/linux/man-pages/man7/io_uring.7.html)（该页 COLOPHON 写明它是 liburing 项目的手册页，英文，深入，免费；2026-11-14 打开该地址返回 HTTP 200，无跳转，44797 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Sat, 14 Nov 2026 00:20:29 GMT`（北京时间 2026-11-14 08:20:29），与响应头 `Date` 相同。`<title>` 为 io_uring(7) - Linux manual page，页内 `h1` 为 io_uring(7) — Linux manual page，页脚为 Linux、日期 July 26, 2020。HTML 渲染标注 2026-09-09、Michael Kerrisk。COLOPHON 记载该页于 2026-08-04 取自 [liburing 上游仓库](https://github.com/axboe/liburing)，当时仓库中最近一次提交日期为 2026-07-28。NAME 是 io_uring - Asynchronous I/O facility。正文把 io_uring 写成 Linux 特有的异步 I/O 接口：用用户态与内核共享的提交队列（SQ）和完成队列（CQ）通信，You place I/O requests you want to make on the SQ, while the kernel places the results of those operations on the CQ；基本模型里 The kernel places exactly one matching CQE in the CQ for every SQE you submit，但在设置 `IOSQE_CQE_SKIP_SUCCESS` 或多完成项的 multi-shot 操作时可以偏离这一规则。完成可以按任意顺序到达，要用 SQE 里传回的 `user_data` 对上请求。`res` 是等价系统调用的返回值，出错则为 `-errno`。`SQE pointer lifetimes & data stability` 一节写明：IORING_OP_READ / IORING_OP_WRITE 使用的缓冲区指针 must remain valid until completion。性能一节写 io_uring can be a zero-copy system，因为通信走共享环，where possible 避免在用户态和内核之间复制；But since the bulk of the communication in io_uring is via buffers shared… this huge performance overhead is completely avoided 指的是环上的通信，同时写明 Copying buffers to and from becomes necessary when system calls that transfer data between kernel and user space are involved。同日跟随打开了 SEE ALSO 中的 [io_uring_setup(2)](https://man7.org/linux/man-pages/man2/io_uring_setup.2.html)（200，无跳转，44172 字节，`Date`/`Last-Modified` 为 `Sat, 14 Nov 2026 00:20:31 GMT`，北京时间 08:20:31，页脚 January 29, 2019）、[io_uring_enter(2)](https://man7.org/linux/man-pages/man2/io_uring_enter.2.html)（200，90028 字节，`Date` 为 00:20:32 GMT，北京时间 08:20:32，页脚 January 22, 2019）以及 [io_uring_register(2)](https://man7.org/linux/man-pages/man2/io_uring_register.2.html)（200，58500 字节，`Date` 为 00:20:34 GMT，北京时间 08:20:34，页脚 January 17, 2019）。io_uring(7)「Pages that refer to this page」列出的 [io_uring_cancelation(7)](https://man7.org/linux/man-pages/man7/io_uring_cancelation.7.html)（注意拼写是 cancelation）返回 200，16500 字节，`Date`/`Last-Modified` 为 `Sat, 14 Nov 2026 00:21:09 GMT`（北京时间 08:21:09），页脚 January 18, 2025，同一套 COLOPHON（2026-08-04 取自 liburing，最近提交 2026-07-28，HTML 渲染 2026-09-09）。同日用 cancellation 拼写打开的地址返回 HTTP 404、196 字节，没有作为依据。） |
| 摘要 | io_uring 以提交队列和完成队列表达异步操作，具体操作支持与语义依赖内核版本及配置。 |

## 导读

### 上一课留下的缺口

上一课的 `ZeroCopyEngine` 已经说明：sendfile 把描述符之间的复制留在内核里，省掉的是用户态来回，不是整条链路零复制；小请求批量能抬吞吐，也可能拉长低负载等待。那一课的边界停在**复制次数和批大小**，没有回答另一件事：如果要把许多文章正文从磁盘里并行读出来，怎样用提交队列发出一批读、用完成队列收回结果，队列深度会怎样改变吞吐，以及完成之前能不能释放缓冲区、取消时会碰到什么竞态。

节点把摘要写成：io_uring 以提交队列和完成队列表达异步操作，具体操作支持与语义依赖内核版本及配置。文件读取流水线要的是完成通知，不是「就绪了再去 read」。

### 接上前置

前置 [第 138 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/138.html) `i-zero-copy` 留下的结论是：调用返回并不等于缓冲区或文件的所有权已经回到你手里。本课把这条纪律用到 io_uring 的读缓冲区上：IORING_OP_READ 的缓冲区必须一直活到对应的 CQE 出现。

前置 [第 62 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/62.html) `d-proactor` 已经区分过就绪通知和完成通知，并介绍了 SQ/CQ 的基本走法。本课不再重复「如何用 liburing 发一条 read」，而是把重点放在队列深度对文件读取流水线的影响，以及完成项、取消竞态、缓冲区和 fd 的生命周期。本课跑的是教学引擎，不是宿主 `io_uring` 或 liburing。

### 教学场景（教学假设）

假设内容后台的教学 KV `teachkv` 要从磁盘读出一批文章正文 `article-body.bin`（键为 `article:01:body`，标题为 `Weekly digest`），用来拼一份推送列表。每份正文 `256` KiB（`262144` 字节）。窗口都是 `30.00` 秒。同步路径每次用一次 `pread` 读完一份，不能与下一份重叠；io_uring 路径按队列深度 `1`、`4`、`8`、`16`、`32` 组成读取流水线，一次 `io_uring_enter` 提交一整波 SQE 并等待这波完成。每次系统调用在教学虚拟时钟上花费 `10000` 纳秒；每份正文的教学磁盘服务是 `200000` 纳秒，用户缓冲区复制每字节 `1` 纳秒，因此复制是 `262144` 纳秒，单份服务时间 `462144` 纳秒。教学设备的并发上限是 `8`（这是教学参数，不是宿主 SSD 的队列深度）：同一波里最多 `8` 份同时服务，深度超过 `8` 时要再排若干轮。全部时间都是教学虚拟时钟上的值，不是生产磁盘服务时间，也不是宿主 `io_uring` 的墙钟。

### 本课三条主线

1. **SQ/CQ**：把读请求写成 SQE 放到提交队列尾部，用 `io_uring_enter` 告诉内核去取；内核把结果写成 CQE 放到完成队列尾部。适用边界是：基本模型里一条 SQE 对应一条 CQE，但 `IOSQE_CQE_SKIP_SUCCESS` 和 multi-shot 可以偏离；完成顺序不必与提交顺序相同，要对 `user_data`；具体操作码和语义依赖内核版本及配置。
2. **完成项与取消**：CQE 的 `res` 是等价系统调用的返回值或 `-errno`，短读也是一种结果；取消会再产生一条取消操作自己的 CQE，和被取消请求的 CQE 一起出现，顺序不定。适用边界是：已经开始的磁盘 I/O 通常取消不掉，取消本身是竞态，必须同时处理成功、`-ENOENT` 和 `-EALREADY`。
3. **缓冲区和 fd 生命周期**：读缓冲区必须保留到完成；关闭应用侧的 fd 不会自动取消仍在途中的请求。适用边界是：异步接口提高了并行度，也把所有权从「系统调用返回」拉长到「看见 CQE（以及可能的通知项）」。

## 学习目标

- 能解释 SQ/CQ 及适用边界：共享环用来提交和收回完成项，完成可以乱序，基本的「一条 SQE 一条 CQE」会被跳过成功项或多完成项打破；操作支持和语义依赖内核版本及配置。
- 独立完成节点实践：用 liburing 实现文件读取流水线，与同步读取比较队列深度影响。本课跑的是教学引擎，不是宿主 liburing。
- 能做到完成前不释放缓冲区，并能处理部分完成与取消竞态。

## 为什么后端要懂

节点对「为什么」写得很干脆：比较就绪驱动与完成驱动的工程成本。放到本课的教学内容后台：同一批 `256` KiB 的 `article-body.bin`，在 `30.00` 秒的教学窗口里，同步 `pread` 读出 `63539` 份，吞吐 `555.21` MB/s，系统调用 `63539` 次，飞行中的缓冲区只有 `262144` 字节，但用户态复制仍等于全部载荷 `16656367616` 字节。io_uring 在队列深度 `1` 时，文件数和吞吐与同步路径相同；深度 `8` 时读出 `508312` 份，吞吐 `4441.7` MB/s，系统调用仍是 `63539` 次（每波一次 enter），飞行中的缓冲区变成 `2097152` 字节，用户态复制仍然等于载荷 `133250940928` 字节。深度再加到 `16` 和 `32`，吞吐只再到 `4489.3`、`4513.35` MB/s，飞行缓冲却变成 `4194304` 和 `8388608` 字节。完成驱动把磁盘服务重叠起来，换来的是必须盯住每一块尚未完成的缓冲、处理短读，以及在客户端断开时走取消协议——关闭 fd 并不会自动帮你收尾。不把「并行度」和「生命周期负担」放在一起看，就会在文件读取上一直同步打满窗口，或把队列深度加到设备并发上限以外，只多占内存、几乎不再提高吞吐。

## 核心知识点

### 1. SQ/CQ

io_uring(7) 的定义是 Linux 特有的异步 I/O 接口。它用用户态和内核共享的环当主通道：你把 I/O 请求放到 SQ，内核把结果放到 CQ。一次读取的基本步骤是：准备一条 opcode 为 `IORING_OP_READ`（或示例里的 `IORING_OP_READV`）的 SQE，填上 `fd`、缓冲区指针、长度和偏移，写到 SQ 的尾部，再调用 `io_uring_enter(2)`，让内核从 SQ 头部取走请求并开始处理。请求完成后，内核在 CQ 尾部放一条 CQE。你从 CQ 头部取走它，看 `res` 和 `user_data`。提交时你加在尾部、内核从头部取；完成时内核加在尾部、你从头部取。

手册写明：请求可以按任意顺序完成，飞行中的多条请求无法预先知道谁先执行完，所以取 CQE 时必须核对该项对应哪一次提交，最常见的办法是使用会原样传回的 `user_data`。基本模型是「你提交的每条 SQE，内核都放回恰好一条匹配的 CQE」，但配置可以偏离：`IOSQE_CQE_SKIP_SUCCESS` 会在成功时不放 CQE，multi-shot 则可能为一条 SQE 放出多条 CQE。内核老于 5.4 时，建环需要三次 `mmap(2)`，而不是示例里的两次。这些就是适用边界，也对应摘要里的那句：具体操作支持与语义依赖内核版本及配置。

本课把流水线写成「一波提交、一波等待」。一次 enter 的教学成本是 `10000` 纳秒，随后最多 `8` 份并行做完 `462144` 纳秒的服务。深度 `1`、`4`、`8` 时，一波时间都是 `472144` 纳秒，`30.00` 秒里各完成 `63539`、`254156`、`508312` 份，吞吐分别是 `555.21`、`2220.85`、`4441.7` MB/s。深度 `1` 与同步 `pread` 的文件数相同，因为两边都是「一次调用等一份服务」；深度加上去之后，同一声 enter 里重叠了多份磁盘服务，吞吐几乎按并发度上升，直到碰到教学并发上限 `8`。这些吞吐是教学成本算出来的，不是宿主 `io_uring` 的墙钟。io_uring(7) 还提到可以用 SQ polling 让内核线程自己取 SQE，从而避免 `io_uring_enter`；本课没有把 SQPOLL 做成第三条曲线。

### 2. 完成项与取消

CQE 本身很窄：`user_data`、`res`、`flags`。io_uring(7) 写 `res` 就是那次作为提交内容被执行的系统调用的返回值。读了 `1024` 字节就是 `1024`，失败就是 `-errno`。短读也是一种返回值，不能当成「已经把请求长度读满」。本课的部分完成实验按教学安排让一次请求 `262144` 字节的读返回 `131072`。若这条 SQE 还用 `IOSQE_IO_LINK`（io_uring_enter(2)，自 Linux 5.3 可用）链了后续操作，短读属于 unexpected result，会打断剩下的链，未开始的后续项以 `-ECANCELED`（`-125`）完成。这就是验收要求的「处理部分完成」。

取消是另一条要提交的请求。io_uring_enter(2) 里的 `IORING_OP_ASYNC_CANCEL`（自 5.5 可用）用目标请求的 `user_data` 去找人；io_uring_cancelation(7) 说取消会生成两条 CQE，顺序并不保证。本课用三条教学竞态把文档里的结果码落到结果上：

- 读已经完成再处理取消：取消项 `res` 为 `-ENOENT`（`-2`），原请求 `res` 为 `262144`。
- 请求仍可取消：取消项 `res` 为 `0`，原请求 `res` 为 `-ECANCELED`（`-125`）。
- 磁盘 I/O 已经开始：取消项 `res` 为 `-EALREADY`（`-114`）。io_uring_enter(2) 写的是 found and attempted canceled，请求 may or may not terminate，并且 disk IO requests cannot be canceled if already started；io_uring_cancelation(7) 把 `-EALREADY` 写成 found but already completing，NOTES 说已经交给硬件的操作（例如正在进行的磁盘 I/O）通常不能取消。本课这一例让原请求最终以 `262144` 成功完成，没有再编造另一种结局。

应用必须按 `user_data` 分辨「这是取消项自己的结果」还是「这是原请求的结果」，并且接受先看到哪一条都有可能。这就是「取消竞态」。

### 3. 缓冲区和 fd 生命周期

io_uring(7) 把指针寿命分成两类。像 `IORING_OP_TIMEOUT` 用的 `__kernel_timespec`，在非 SQPOLL、并且内核具备 `IORING_FEAT_SUBMIT_STABLE` 时，提交完成后就可以释放。但「在操作飞行期间被读或写的数据」不适用这一条：IORING_OP_READ / IORING_OP_WRITE 的缓冲区指针必须保持有效，直到完成。本课的违规实验在 CQE 出现之前就释放了读缓冲，并把 `violates_buffer_until_completion` 标为真；没有编造内核会往这块已经回收的内存里写下哪些字节。

fd 也有同样的拉长。io_uring_cancelation(7) 写得很清楚：关闭应用侧的 fd，并不会自动取消仍在途中、打在这个 fd 上的请求——环会为每个未完成请求自己持有一份文件引用。若你指望 `close` 之后pending 的 read 会自己冒出一条完成项，那不会发生，必须先显式取消。关掉整个 io_uring 实例时，未完成请求会被自动取消。发送路径上还有更长的寿命：io_uring_enter(2) 的 `IORING_OP_SEND_ZC` 可能先给出带 `IORING_CQE_F_MORE` 的完成项，再给出带 `IORING_CQE_F_NOTIF` 的通知项，在通知到来之前不得修改发送缓冲。本课的文件读取实验没有走这条零拷贝发送路径，只把它当作「完成项不等于所有权归还」的对照。

## 常见误区

节点写出的误区是：把所有操作都当作必然不阻塞或零复制。io_uring_enter(2) 对 `IOSQE_ASYNC`（自 5.6 可用）的说明是：默认会先尝试非阻塞地发出这条 SQE，失败后再按异步方式执行；并不是「提交以后发行路径绝不会停下」。io_uring(7) 说它 can be a zero-copy system，指的是共享环让双方少走系统调用式的缓冲复制，而且加了 where possible；一旦涉及在内核和用户空间之间传送数据的系统调用，复制仍然会发生。本课的 `IORING_OP_READ` 把数据读进用户缓冲，同步路径和深度 `8` 的路径都把 `user_copy_bytes` 记为与载荷相同：`16656367616` 与 `133250940928`。`IORING_OP_SEND_ZC` 自己也写：Zerocopy execution is not guaranteed and may fall back to copying。把「异步接口」读成「必然不阻塞、必然零复制」，就是忽略了这些适用边界。

## 设计权衡

节点写出的权衡是：异步接口提升并行度，也增加生命周期管理负担。教学窗口里，同步读取的吞吐是 `555.21` MB/s，飞行缓冲 `262144` 字节；深度 `8` 把吞吐做到 `4441.7` MB/s，飞行缓冲变成 `2097152` 字节。再把深度加到 `16` 和 `32`，并发仍然受到教学上限 `8` 的限制，一波里要跑 `2` 轮或 `4` 轮服务，吞吐只增加 `47.6` 和 `71.65` MB/s，飞行缓冲却到了 `4194304` 和 `8388608` 字节。多出来的并行度，要靠一直持有这些缓冲、处理短读和取消竞态、并在关 fd 之前显式取消来换。过了设备并发上限还加深度，生命周期负担继续涨，吞吐几乎不再涨。

## 动手实践：用 liburing 实现文件读取流水线，与同步读取比较队列深度影响

实践按节点的表述，用 liburing 实现文件读取流水线，与同步读取比较队列深度影响；跑的是教学引擎，不是宿主 `io_uring` 或 liburing。

工作目录是 `practice139/`。直接运行：

```bash
./test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。

请先自己根据 `262144` 字节的文件、系统调用 `10000` 纳秒、磁盘服务 `200000` 纳秒、每字节复制 `1` 纳秒，以及教学并发上限 `8`，算出 `30.00` 秒里同步路径和深度 `1`、`4`、`8`、`16`、`32` 各能读多少份、吞吐和飞行缓冲是多少，确认同步与深度 `1` 都是 `63539` 份、`555.21` MB/s、飞行缓冲 `262144` 字节；深度 `8` 是 `508312` 份、`4441.7` MB/s、飞行缓冲 `2097152` 字节；深度 `16` 是 `513760` 份、`4489.3` MB/s、飞行缓冲 `4194304` 字节；深度 `32` 是 `516512` 份、`4513.35` MB/s、飞行缓冲 `8388608` 字节。然后再核对另外三组：完成前释放缓冲时，`violates_buffer_until_completion` 为真，且没有给出内核写入的字节；短读 `131072` 会把链表上的后续项收成 `-125`；三条取消竞态的取消项 `res` 分别是 `-2`、`0`、`-114`。宿主记录里 `host_io_uring_invoked` 与 `host_liburing_invoked` 均为假；宿主内核是 `6.12.113+`，`io_uring_disabled` 的 sysctl 是 `0`，但本课没有调用宿主接口。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_buffer_held_until_cqe`、`acceptance_partial_and_cancel_race`、`pitfall_not_always_nonblock_or_zerocopy`、`tradeoff_parallelism_vs_lifetime`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_profiler_product`、`not_a_real_uring_product`、`not_host_io_uring`、`not_host_liburing`、`not_host_gxx`、`times_are_teaching_virtual_clock`、`max_parallel_is_teaching_parameter`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Linux man-pages — io_uring(7) 中与 io_uring 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://man7.org/linux/man-pages/man7/io_uring.7.html](https://man7.org/linux/man-pages/man7/io_uring.7.html) 返回 200、无跳转、44797 字节，页脚为 July 26, 2020，`<title>` 为 io_uring(7) - Linux manual page。COLOPHON 写明该页属于 liburing 项目，2026-08-04 取自上游仓库，当时最近提交为 2026-07-28。本课跟随打开了它点名的 io_uring_setup(2)（44172 字节，页脚 January 29, 2019）、io_uring_enter(2)（90028 字节，页脚 January 22, 2019）、io_uring_register(2)（58500 字节，页脚 January 17, 2019），以及「Pages that refer to this page」中的 io_uring_cancelation(7)（16500 字节，页脚 January 18, 2025）。用 cancellation 拼写打开的地址当天返回 404，没有当作依据。

教学实验按以下方式使用它，并记下差异。

- **SQ/CQ 与队列深度**：共享环、SQE/CQE、乱序完成、`user_data`、以及「基本模型一条 SQE 一条 CQE、可被配置偏离」都来自 io_uring(7)。本课的 `555.21` / `2220.85` / `4441.7` / `4489.3` / `4513.35` MB/s 是教学波次成本算出来的，不是一次宿主 `io_uring_enter`。并发上限 `8`、磁盘服务 `200000` 纳秒和复制单价是教学参数，文档没有给出这些数字。本课没有测量 SQPOLL，也没有复现 5.4 之前三次 `mmap` 的建环路径。
- **完成项与取消**：`res` 的含义来自 io_uring(7)；短读打断 `IOSQE_IO_LINK` 来自 io_uring_enter(2)；三条竞态的结果码和「顺序不保证」「本身是竞态」来自 io_uring_cancelation(7)，其中 `-EALREADY` 同时对照了 io_uring_enter(2)「可能终止也可能不终止、已开始的磁盘 I/O 不能取消」的表述。教学短读的 `131072` 是安排值，不是宿主短读。
- **寿命与误区**：读缓冲必须留到完成，来自 io_uring(7)；关 fd 不会自动取消，来自 io_uring_cancelation(7)；「先尝试非阻塞，失败再异步」来自 io_uring_enter(2) 的 `IOSQE_ASYNC`；「共享环可以少复制，但涉及用户态与内核之间传数据时仍会复制」来自 io_uring(7) 的性能一节，SEND_ZC「零拷贝执行不被保证」来自 io_uring_enter(2)。本课把文件读的 `user_copy_bytes` 记为与载荷相同，用来落实「不要把所有操作都当成必然零复制」。全部时间是教学虚拟时钟。违规释放缓冲时，本课不编造内核写入的字节。

## 自测题

**题 1.** SQ 和 CQ 各承担什么角色？为什么完成顺序不能当成提交顺序？在本课的教学窗口里，同步读取和队列深度 `1`、`8`、`32` 的吞吐、文件数和飞行缓冲各是多少？深度从 `8` 加到 `32` 为什么几乎不再提高吞吐？

<details>
<summary>答案</summary>

io_uring(7) 写明：你把 I/O 请求放到 SQ，内核把结果放到 CQ。你在 SQ 尾部加 SQE，内核从头部取走；内核在 CQ 尾部加 CQE，你从头部取走。基本模型里一条 SQE 对应一条 CQE，但跳过成功项或多完成项会偏离这一规则。

完成可以按任意顺序到达，飞行中的请求无法预先知道谁先结束，所以必须用原样传回的 `user_data` 把 CQE 对回那次提交。

`30.00` 秒的教学窗口里：同步读取和深度 `1` 都是 `63539` 份、吞吐 `555.21` MB/s、飞行缓冲 `262144` 字节；深度 `8` 是 `508312` 份、`4441.7` MB/s、飞行缓冲 `2097152` 字节；深度 `32` 是 `516512` 份、`4513.35` MB/s、飞行缓冲 `8388608` 字节。教学设备并发上限是 `8`，再加深队列只能在同一波里多排几轮服务，并多持有几份缓冲，吞吐只比深度 `8` 多 `71.65` MB/s。这些是教学虚拟时钟上的数字，不是宿主 `io_uring` 的测量。

</details>

**题 2.** 部分完成和取消竞态要处理哪些结果？短读为什么会把链接上的后续操作收成 `-ECANCELED`？三条教学竞态里，取消项和原请求的 `res` 分别是什么？关 fd 能不能代替取消？

<details>
<summary>答案</summary>

短读是合法的 `res`，只是小于请求长度。本课请求 `262144`、实际 `131072`。io_uring_enter(2) 写：`IOSQE_IO_LINK` 链路上，任何 unexpected result 都视为错误，短读也会打断剩余未开始的项，并以 `-ECANCELED`（`-125`）完成它们。

取消会多出一条取消操作自己的 CQE，和被取消请求的 CQE 一起出现，顺序不保证，而且本身是竞态。三条教学结果是：已完成再取消，取消项 `-2`（`-ENOENT`）、原请求 `262144`；仍可取消，取消项 `0`、原请求 `-125`；磁盘 I/O 已开始，取消项 `-114`（`-EALREADY`）、本课让原请求最终以 `262144` 成功完成。最后这一例同时对照 io_uring_enter(2)「可能终止也可能不终止、已开始的磁盘 I/O 不能取消」和 io_uring_cancelation(7) 的 NOTES，没有编造另一种结局。

关闭应用侧 fd 不会自动取消途中的请求，必须显式取消。关掉整个环才会自动取消未完成项。

</details>

**题 3.** 为什么不能在 CQE 出现之前释放读缓冲区？为什么不能把 io_uring 上的所有操作都当成必然不阻塞或零复制？本课文件读取路径有没有把用户态复制记成零？

<details>
<summary>答案</summary>

io_uring(7) 要求：IORING_OP_READ / IORING_OP_WRITE 使用的缓冲区指针必须保持有效，直到完成。提交返回、甚至 `io_uring_enter` 返回，都不等于这块缓冲的所有权已经回来。本课在 CQE 之前释放缓冲，只把 `violates_buffer_until_completion` 标为真，并写明 `kernel_bytes_not_invented`，没有编造内核写入的内容。

io_uring_enter(2) 的默认发行路径是先尝试非阻塞，失败后再异步执行，所以不能假设发行路径绝不停下。io_uring(7) 说它可以是 zero-copy system，指的是共享环上的通信 where possible 少复制；一旦系统调用要在内核和用户空间之间传数据，复制仍然会发生。本课的文件读把 `user_copy_bytes` 记为与载荷相同，同步路径是 `16656367616`，深度 `8` 是 `133250940928`，并没有记成零。`IORING_OP_SEND_ZC` 也写明零拷贝执行不被保证，可能退回复制。

</details>

## 推荐资源

- [Linux man-pages — io_uring(7)](https://man7.org/linux/man-pages/man7/io_uring.7.html)（liburing 项目手册页，2026-08-04 取自上游仓库，页脚 July 26, 2020，英文，深入，免费）：节点给出的官方出处。2026-11-14 返回 200，44797 字节，`<title>` 为 io_uring(7) - Linux manual page。用于核对 SQ/CQ、完成顺序与 `user_data`、读缓冲区必须留到完成，以及「共享环可以少复制，但用户态与内核传数据时仍会复制」。该页描述的是真实接口，本课的教学吞吐是对照，不是宿主采集。
- 跟随阅读：[io_uring_enter(2)](https://man7.org/linux/man-pages/man2/io_uring_enter.2.html)（同一来源，页脚 January 22, 2019；90028 字节）给出 `IORING_OP_ASYNC_CANCEL`、短读打断 `IOSQE_IO_LINK`、默认先尝试非阻塞，以及 SEND_ZC 的零拷贝不被保证；[io_uring_cancelation(7)](https://man7.org/linux/man-pages/man7/io_uring_cancelation.7.html)（页脚 January 18, 2025；16500 字节）给出两条 CQE、顺序不保证、关 fd 不会自动取消，以及已交给硬件的磁盘 I/O 通常不能取消。这些是文档记载，不是本课的宿主测量。

## 本课小结

- io_uring 用 SQ/CQ 表达异步操作：你提交 SQE，内核收回 CQE。完成可以乱序，操作支持和语义依赖内核版本及配置。
- 教学窗口里，同步读取和深度 `1` 的吞吐都是 `555.21` MB/s；深度 `8` 是 `4441.7` MB/s；深度 `32` 只再到 `4513.35` MB/s，飞行缓冲却从 `2097152` 增加到 `8388608` 字节。异步接口提升并行度，也增加生命周期管理负担。
- 完成前不能释放读缓冲。短读要按部分完成处理，链接上的后续项会以 `-ECANCELED` 收掉。取消是竞态：本课三条结果里，取消项 `res` 分别是 `-2`、`0`、`-114`，两条 CQE 的顺序不保证。关 fd 不能代替取消。
- 不要把所有操作都当成必然不阻塞或零复制。文件读路径的用户态复制仍等于载荷。
- 这些是教学虚拟时钟上的数字，不是生产磁盘服务时间，也不是宿主 `io_uring` / liburing 的输出。`UringEngine` 不是 Redis，也不是 Linux 的 io_uring 实现。并发上限 `8` 是教学参数。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「请求放到 SQ、结果放到 CQ」「缓冲区必须留到完成」「取消是竞态、顺序不保证」「先尝试非阻塞，失败再异步」「传数据时仍可能复制」这几段，不要把队列深度加到并发上限以外还指望吞吐按比例上涨，也不要在看见 CQE 之前回收缓冲。
