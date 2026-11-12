# 第 107/208 课：RocksDB集成与读写调优

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-rocksdb` |
| 英文名 | RocksDB |
| 领域 / 阶段 | G / 第 4 阶段 |
| 子类 | 存储引擎 |
| 难度 | 深入 |
| 前置节点 | `g-lsm`、`b-raii` |
| 标签 | `RocksDB`、`G`、`Systems` |
| 参考资料 | [RocksDB集成与读写调优：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/Basic-Operations)（github.com，文档；2026-11-12 该 URL 返回 301，跳转到 [meta/rocksdb wiki：Basic Operations](https://github.com/meta/rocksdb/wiki/Basic-Operations) 返回 200。同批核对 Column-Families、Write-Stalls、Block-Cache、Memory-usage-in-RocksDB、Iterator、Snapshot、Checkpoints、Setup-Options-and-Basic-Tuning，均为 facebook 入口 301 后 200；wiki 路径 `Snapshots` 返回 404，本课引用单数形式的 [Snapshot](https://github.com/facebook/rocksdb/wiki/Snapshot)） |
| 摘要 | RocksDB是嵌入式LSM存储引擎，应用负责服务层、资源预算和错误处理，不能把引擎当完整分布式数据库。 |

> **边界说明**：本课中出现的「内容后台」「文章标题」「阅读计数」等场景都是**教学假设**，用来让列族、批写、快照、迭代器和块缓存变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学 `RocksDBEngine`，**不是 RocksDB，不是 Redis，也不是一个对外提供网络协议的服务**。教学 SST 是**按 key 排好序的记录列表，再按固定条数切成块**，不是 RocksDB 的 block-based 约 4 KiB 压缩块；教学 block cache 只缓存这些未压缩的教学块。本课实现了简化的 WAL 恢复和 L0 刷盘/合并，但**没有** bloom filter、压缩、真实 MANIFEST 日志、Checkpoint、事务、Merge Operator、prefix seek、subcompaction 或写缓冲管理器。官方文档已于 **2026-11-12** 核对：节点 URL `https://github.com/facebook/rocksdb/wiki/Basic-Operations` 返回 301，跳转到 `https://github.com/meta/rocksdb/wiki/Basic-Operations` 并返回 200；同期打开的 Column-Families、Write-Stalls、Block-Cache、Memory-usage-in-RocksDB、Iterator、Snapshot、Checkpoints、Setup-Options-and-Basic-Tuning 同样 301 后 200。`https://github.com/facebook/rocksdb/wiki/Snapshots`（复数）返回 404，本课引用 `wiki/Snapshot`。文中的官方默认值、日志措辞和机制只写到核对过的页面能够支撑的范围；教学实验里的字节数、耗时和命中次数都来自 `practice107/results.json`，不是生产引擎的基准数字。

## 导读

### 一个直觉：换上嵌入式引擎，并不等于换上了一套完整数据库

内容后台如果要把标题和计数从第 104 课那种进程内哈希表，换成一套能重启恢复、能按范围扫描、能原子改多条记录的持久化内核，RocksDB 是常见的选择。它是嵌入式 LSM 存储引擎：进程打开一个目录，调用 `Put` / `Get` / `Write`，数据就落在这个目录里。节点把它概括为：RocksDB 是嵌入式 LSM 存储引擎，应用负责服务层、资源预算和错误处理，不能把引擎当完整分布式数据库。

这句里有三件容易被跳过的事。服务层——监听端口、鉴权、多副本、故障转移——都不是引擎的工作。资源预算——块缓存、memtable、后台刷盘和压实占用的内存与 IO——默认参数不一定适合这台机器和这条负载，必须由应用按预算来设。错误处理——几乎所有可能失败的调用都返回 `Status`，不检查就等于把 Incomplete、InvalidArgument 或 IO 错误吞掉。换内核，是把第 104 课的 KV 语义接到第 106 课的 LSM 上，并补上批写、快照、迭代器和重启恢复，而不是少写一层应用。

### 接上前置

第 106 课把 memtable、SST、压实和 write stall 讲清楚了，但那门课没有列族、没有快照、也没有块缓存，并且假设写入已经进了内存表。本课把这些集成接口接上：一个 DB 里可以有多个 Column Family，一次 `WriteBatch` 可以原子地改几个列族，读取可以用 snapshot 和 iterator 看到一个一致的时间点，读热数据走 block cache，后台跟不上时仍然会减速或停写。另一门前置 `b-raii` 讨论资源寿命：`GetSnapshot` / `ReleaseSnapshot`、迭代器的创建与销毁、`ColumnFamilyHandle` 必须在删除 DB 之前释放。本课的验收标准就是这两件事在集成时必须同时成立：所有 `Status` 都要检查；iterator 的寿命和 snapshot 的释放要正确。

### 一次写入走进 RocksDB 之后

一次「发布标题并记下阅读计数」的教学写入，会走这条路径：

1. 调用方构造一个 `WriteBatch`：向 `titles` 列族 `Put article:01:title`，向 `counters` 列族 `Put article:01:views`。两步是一批，不是两次互不相干的 `Put`。
2. `DB::Write` 先看有没有 write stall。如果某个列族的 immutable memtable 或 L0 已经碰到停写条件，而 `WriteOptions.no_slowdown` 为真，这次调用立刻返回 `Status::Incomplete()`，批里的键都不会被应用。
3. 通过检查后，整批写入共享的 WAL；`WriteOptions.sync=true` 时，写操作要等到日志进入持久存储才返回。然后同一批更新被应用到各列族自己的 memtable。列族共享 WAL，不共享 memtable 和 SST，所以标题和计数可以分开配置，又能在一次 Write 里原子提交。
4. memtable 满了会封存、刷成该列族的 L0 SST；L0 过多时后台压实。读一条标题时，先看该列族的 memtable，再按块去 SST 里找。块若在 block cache 里就是命中，否则从文件读入，并在 `ReadOptions.fill_cache` 为真时写入缓存。
5. `GetSnapshot` 记下当前序号。之后即使标题被改成修订版，带着这个 snapshot 的 `Get` 和 iterator 仍看到旧标题。快照是进程内对象，**不会跨重启保存**。需要一份可重新打开的目录副本时，那是 Checkpoint，不是 snapshot。

### 教学场景（教学假设）

下面用「文章标题和阅读计数」做例子。键形如 `article:01:title`、`article:01:views`，标题用 `Weekly digest` 和修订版 `Weekly digest (revised)`。标题放在 `titles` 列族，计数放在 `counters` 列族。这是教学假设，用来观察列族隔离、批写原子性、快照寿命、范围扫描和缓存淘汰，不代表任何线上内容系统的真实存储设计。

### 本课三条主线

1. **DB 与 Column Family**：一个目录就是一个 DB；每个键属于恰好一个列族，未指定时落到 `default`。列族可独立配置，共享 WAL，因此跨列族批写可以原子提交；读写打开时必须列出全部已有列族。
2. **WriteBatch、snapshot 与 iterator**：批写避免「先写新键、再删旧键」在中途崩溃后留下重复值；snapshot 提供点读和扫描的一致视图，但不是跨重启的备份；iterator 在创建时固定一个时间点，并且会钉住块，必须检查 `status()` 并及时释放。
3. **block cache 和 write stall**：块缓存按容量缓存未压缩块，默认大小往往不够，全表扫描若 `fill_cache=true` 会把热点挤出去；flush/compaction 跟不上时，触发条件按列族计算，但减速和停写作用在整个 DB 上。调优必须受内存和 IO 预算约束。

## 学习目标

- 能解释 DB 与 Column Family：它们如何分担 WAL 与 memtable/SST，读写打开为什么必须列出全部列族，跨列族原子写依赖什么。
- 能独立完成：把 KV 内核替换为教学 RocksDB 引擎，加入批写、范围扫描和重启恢复；并在此过程中检查每一处 `Status`，正确释放 iterator 和 snapshot。

## 为什么后端要懂

内容后台的标题、草稿状态和计数如果直接放进进程内哈希表，进程一退就没了，也无法原子地「改标题同时加计数」，更无法按 `article:01` 到 `article:03` 做范围扫描。把内核换成嵌入式 LSM，图的是成熟的持久化、有序扫描和批写，而不是少写服务层。若把 RocksDB 当成已经带了副本、监控和资源隔离的分布式数据库，出了 write stall 会误以为是「机器不够」，其实是某一个列族的 memtable 或 L0 触发了整库停写；若把 `GetSnapshot()` 当成备份，重启之后会发现那份只读视图已经不在了。这些都是应用必须自己负责的边界。

## 核心知识点

### 1. DB 与 Column Family

一个 RocksDB 数据库对应文件系统里的一个目录，打开时通常要检查返回的 `Status`。官方 Basic Operations 把 `Status` 放在很靠前的位置：大多数可能失败的调用都返回它，调用方要用 `ok()` 判断，并用 `ToString()` 打印原因。关闭也有三种做法，其中 `DB::Close()` 会返回 `Status`，只 `delete db` 可能把关闭时的错误丢掉。教学引擎用 `Close()` 在退出前把剩余 memtable 刷盘并同步 WAL。

Column Family 从 RocksDB 3.0 引入。每个键值对恰好属于一个列族；不指定时属于名为 `default` 的列族。官方文档列出几条对集成最要紧的性质：跨列族的原子写是支持的，可以一次 `Write({cf1,key1,value1},{cf2,key2,value2})`；可以拿到跨列族一致的视图；列族可以独立配置；可以在运行中创建和删除。实现上，列族**共享 WAL、不共享 memtable 和 SST 文件**。共享日志，所以一批更新要么一起进 WAL，要么一起不进；分开的 memtable 和 SST，所以标题列族和计数列族可以有不同的写入缓冲，也可以很快被删除。

因此也有一条调优约束：某一个列族如果迟迟不 flush，旧的 WAL 就不能删，因为里面还活着其他列族的数据。官方建议让所有列族都定期 flush，并关注 `max_total_wal_size`。本课教学引擎没有实现这条 WAL 滞留计数，但机制要记住。

打开方式也和单列族不同。读写模式的 `DB::Open` 必须列出当前存在的**全部**列族，否则返回 `Status::InvalidArgument()`。只读打开可以只打开子集。`ListColumnFamilies` 用来先查出名字。`ColumnFamilyHandle` 像打开的文件描述符，必须在删除 DB 指针之前全部释放；对已经 `DropColumnFamily` 的 handle，官方约定仍然可用，真正删数据要等到 handle 被销毁。

教学实验把标题和计数拆进 `titles` 与 `counters`，再用一个 6 步的 `WriteBatch` 一次提交三篇文章的标题和初始计数。只在 `titles` 上做范围扫描，得到 3 条标题，计数键没有混进来。关闭后再打开，如果只传 `default`，状态是 `invalid_argument: must specify all existing column families, missing: titles,counters`。把三个列族都列出后，三篇标题和三条计数都能恢复，`ListColumnFamilies` 返回 `default`、`titles`、`counters`。这就是「把 KV 内核换成可恢复的引擎」的最小闭环：批写、按列族扫描、重启后还在。

### 2. WriteBatch、snapshot 与 iterator

Basic Operations 用「把 key1 的值移到 key2」说明批写：如果先 `Put(key2)` 再 `Delete(key1)`，进程在两步之间退出，同一个值可能同时出现在两个键下。`WriteBatch` 把一组编辑按顺序一次性提交。编辑的次序有意义：若 `key1` 和 `key2` 相同，必须先 Delete 再 Put，否则会把值删掉。批写也可以用来加快大量更新，但本课先抓住原子性。

教学实验复现了这条边界。先把 `article:01:title` 写成 `Weekly digest`。天真路径只把同样的值 `Put` 到 `article:01:moved` 并同步，然后在删除旧键之前模拟掉电：恢复后两个键都还在，值为 `Weekly digest`，出现重复。改成一个 `WriteBatch`：先 Delete 旧键，再 Put 新键，并且 `sync=true`，再掉电：旧键是 `not_found`，新键仍是 `Weekly digest`，移动是原子的。同一批若 `sync=false` 后立刻掉电，整批丢失，旧标题还在，新键不存在——原子性保护的是「不会只应用一半」，并不自动提供耐久性。若只是杀掉进程、不掉电，操作系统的页缓存还在，未同步的整批仍然能恢复，并且仍然是原子的。这和第 105 课的 WAL 边界一致：进程崩溃与机器掉电不是一回事。

Snapshot 提供整库状态的一致只读视图。`ReadOptions.snapshot` 非空时，点读和迭代都按这个版本读；为空时，读操作使用当前状态的隐式快照。官方 Snapshot 页第一句就是：A snapshot captures a point-in-time view of the DB at the time it's created. **Snapshots do not persist across DB restarts.** 调用方必须 `GetSnapshot()`，用完后 `ReleaseSnapshot()`，否则引擎要为这份视图继续保留旧版本，快照很多时甚至会拖慢 flush/compaction，严重时诱发 write stall。Checkpoint 才是把当前 DB 复制到另一个目录（同盘可用硬链接），可以再打开，那是备份/副本路径，不是 `GetSnapshot()`。

教学实验里，先写入 `Weekly digest` 并取得序号为 1 的快照，再把标题改成 `Weekly digest (revised)`。当前读取到修订版，带着快照的 `Get` 和 iterator 仍读到 `Weekly digest`。此时 `ReleaseSnapshot`：再用这个句柄去做 `Get`，返回 `invalid_argument: snapshot already released`；但**释放前已经创建的 iterator 仍然有效**，还能读到旧标题——iterator 在创建时钉住了那份视图。把这个 iterator 关掉后，再对已释放快照 `NewIterator`，得到同样的 `invalid_argument`。当前读取仍是修订版。另一次实验在快照仍然活着时模拟掉电：崩溃前快照还能看到 `Weekly digest`，进程里有 1 个快照；恢复后快照数为 0，读到的是 `Weekly digest (revised)`，旧的 snapshot 句柄不能再用。这就是本课的误区：快照不是跨重启的永久备份。

Iterator 按比较器给出的全序做范围扫描，创建时固定一个一致时间点。`ReadOptions.snapshot` 为空时，这份一致视图是创建瞬间的隐式快照，不能事后再变成显式 snapshot。错误处理有一条硬约定：`Valid()` 为真时，`status()` 一定是 OK，循环里可以先用 `key()` / `value()`；`Valid()` 为假时，可能是扫到了尽头（此时 `status()` 仍是 OK），也可能是 IO 或校验错误，**必须检查 `status()`**。教学实验扫描 16 个标题到尽头：`Valid()` 为假，`status()` 为 `OK`。用下界 `article:05:title`、上界 `article:09:title`，扫描结果正好是 05 到 08 四篇，上界是左闭右开。

Iterator 自身占用的内存通常不大，但它会阻止资源释放：创建时钉住当时的 memtable 和 SST，当前位置的 data block 也会留在内存里（在 block cache 中钉住，或没有 cache 时留在堆上）。官方建议 iterator 保持短命。教学实验在 `SeekToFirst` 之后，钉住的块占用是 96 字节；迭代结束并 `close()` 之后，钉住占用回到 0，未关闭的 iterator 计数从 1 变为 0。只创建不释放，块就一直占着预算。

### 3. block cache 和 write stall

Block cache 缓存的是未压缩块。官方默认会建一个 32 MB 的 LRU 缓存（8.2 版之前是 8 MB），文档写明这只适合「不怎么依赖读性能、希望引擎内存足迹较小」的应用。缓存对象可以在同一进程的多个 DB、多个列族之间共享，用来卡住总容量；默认情况下，每个列族往往各自拥有一份缓存，内存会按实例叠加。操作系统的页缓存会再缓存压缩后的文件块，形成两级。索引和 filter 默认不计入你给 block cache 设的那笔预算，这是「明明设了 10 GB，进程却用了 15 GB」的常见来源之一。Setup Options 建议把 block cache 大约设成总内存预算的三分之一，剩下留给操作系统页缓存，避免把预算绷得太紧。容量按分片均分，分片之间不共享额度；当有迭代器把块钉住、钉住总量超过容量时，默认允许缓存暂时超卖，`strict_capacity_limit=true` 则会拒绝新插入并让这次读失败。

全量扫描会污染缓存。Basic Operations 给出的办法是给这个 iterator 设 `ReadOptions.fill_cache=false`，避免把工作集里的热块挤走。

教学实验把 40 个标题刷成 8 个 SST、16 个块（每块 4 条记录；教学 `write_buffer_size=120`，约 5 条刷一次，所以每个 SST 常是 4+1 两条块）。缓存容量 400 字节，大约只能留下 4 个块（占用 384 字节）。第一次按 key 顺序点读：16 次缺失、24 次命中，耗时 0.01624 秒——每个块的第一键是缺失，同一块里后续键是命中。第二次同样的顺序扫描，仍然是 16 次缺失、24 次命中、0.01624 秒：缓存只能留下扫描末端的 4 个块，顺序扫会一边读一边把前面刚装入的块淘汰掉。这不是「第二次就应该全命中」，而是容量远小于工作集时的顺序扫描抖动。

若只保留前 8 个热键，第二次点读是 8 次命中、0 次缺失。接着用 `fill_cache=true` 的 iterator 做一次全表扫描，淘汰 28 次；再读这 8 个热键，变成 5 次命中、3 次缺失，耗时 0.00305 秒。同样的热键预热之后，用 `fill_cache=false` 再扫全表：淘汰 0 次，缓存占用仍是 216 字节；再读热键，是 8 次命中、0 次缺失，耗时 0.00008 秒。全表扫描要不要填缓存，直接决定热点还在不在。

Write stall 的机制第 106 课已经核对过：immutable memtable 过多、L0 文件数触达 slowdown/stop、待压实字节触达 soft/hard 限制。触发条件按列族计算，但 stall 作用于整个 DB；`no_slowdown=true` 时不阻塞，立刻返回 `Incomplete`。本课核对的 Write-Stalls 页面仍然没有给出 slowdown/stop 的官方默认数字，实验里的 8/12 只是教学阈值。

80 次写入、教学 `write_buffer_size=200`、`max_write_buffer_number=3`、压实带宽 800 字节/秒。快写间隔 0.001 秒：stop 4 次，停写 1.17 秒，写入 p99 为 0.2935 秒，本次没有走到 L0 slowdown，日志原因是 `stopping writes because we have 3 immutable memtables (waiting for flush), max_write_buffer_number is set to 3`，immutable 峰值 3。慢写间隔 0.15 秒：stop 和 stall 都是 0，p99 就是 0.15 秒的写入间隔，后台跟得上。另一次对照把 `auto_compact` 关掉，只往 `counters` 写到停写压力（15 次写入后出现上面这条原因），然后对 `titles` 发一条 `no_slowdown` 的写入：返回 `Incomplete`，标题键仍是 `not_found`——计数列族触发的停写，挡住了标题列族。随后一条会阻塞的标题写入，在后台 flush 减压后成功，值为 `Weekly digest (revised)`。这就是「按列族触发、按整个 DB 停写」。

## 常见误区

快照不是跨重启的永久备份。`GetSnapshot()` 在进程里登记一个序号，让引擎暂时不要丢掉这个序号仍能看见的版本。教学实验在快照还活着时掉电，恢复后快照数从 1 变成 0，读到的是修订后的标题，旧句柄不能再用。若需要一份重启后还能打开的副本，应当走 Checkpoint 或备份，而不是把 snapshot 句柄存起来。

另外两件也常一起搞错。一件是把两次成功的 `Put` 当成原子提交：天真路径在删除旧键之前崩溃，旧键和新键会同时持有 `Weekly digest`。另一件是打开 DB 时只写 `default`：只要曾经创建过其他列族，读写打开就会得到 `InvalidArgument`。只读打开才允许打开子集。

## 设计权衡

节点的权衡是：默认参数不一定适合机器和负载，调优需受内存和 IO 预算约束。Setup Options 把这件事说得很直接：多数选项可以先保持默认，但与资源上限有关的项——列族 `write_buffer_size`（官方默认 64 MB，并且要按最坏情况预留约两倍内存）、block cache 大小（建议约占预算的三分之一）、压缩、以及是否为点查打开 bloom——每项都在花内存、CPU 或 IO。把 cache 设太小，顺序扫描和点查会反复缺失；设太大，又和 memtable、索引、filter、iterator 钉住的块抢同一台机器的内存。为每个列族各建一份默认缓存，总占用会叠加；让它们共用一个 `Cache` 对象，才能把进程级上限卡住。

教学实验用同一组 60 个标题、间隔 0.001 秒的写入对照两种预算。偏紧的一组：`write_buffer_size=120`、压实带宽 400、缓存 150 字节，结果是 stall 6 次、stop 14 次、停写 8.9 秒、stall 1.2 秒、写入 p99 为 0.6 秒；随后两遍点读命中 86、缺失 34、耗时 0.03486 秒，淘汰 33 次，缓存用了 120 / 150。放宽预算的一组：`write_buffer_size=800`、带宽 4000、缓存 4096 字节，stall 和 stop 都是 0，写入 p99 为 0，两遍点读命中 105、缺失 15、耗时 0.01605 秒，淘汰 0 次，缓存用了 2400 / 4096。这些数字只说明「预算会改变前台停写和读缺失」，不是生产调参建议。另一组对照：两个列族共用 300 字节缓存，占用 263，淘汰 8 次；拆成两份各 300、合计 600 的私有缓存，占用 467，淘汰 5 次。共享更省总量，私有更不容易互相挤占，选择必须回到进程的内存预算，而不是「默认就好」。

把某个 slowdown/stop 阈值改得极大，可以暂时看不到 stall，但空间放大和读放大会在后台恶化——Write-Stalls 页把这写成引入减速机制的原因。调参是在前台尾延迟和后台债务之间做预算，不是把触发条件关死。

## 动手实践：把KV内核替换为RocksDB，加入批写、范围扫描和重启恢复

配套代码在 `/workspace/atlas/lessons/practice107`，用 Python 3.13.5 标准库写成，不链接 RocksDB，也不走网络。运行 `./test.sh` 会执行全部实验并写下 `results.json`。本次已跑通，输出 `ALL_CHECKS_PASSED`。下面的数字都来自这份结果，不要改写。

### 0. 文件结构

- `engine.py`：教学 `RocksDBEngine`。`Status`、`WriteBatch`、`GetSnapshot` / `ReleaseSnapshot`、`NewIterator`、列族、教学 block cache、write stall、基于 Durable WAL 的重启恢复。
- `app.py`：八组对照实验。
- `test_rocksdb.py` / `test.sh`：验收并写出 `results.json`。

### 1. 准备数据与目标操作

教学键是 `article:NN:title` 和 `article:NN:views`，标题用 `Weekly digest` / `Weekly digest (revised)`。列族为 `default`、`titles`、`counters`。目标操作是：一次批写提交标题和计数，按列族做范围扫描，关闭后按全部列族重新打开并恢复；对比天真分步写和 `WriteBatch` 在掉电后的结果；观察 snapshot 与 iterator 的寿命，以及重启后快照消失；测量 block cache 的命中与 `fill_cache` 的副作用；用快写/慢写和跨列族 `no_slowdown` 观察 write stall；最后对照偏紧预算和放宽预算。

### 2. 要比较的几条路径

1. 跨列族批写 + 范围扫描 + 重启恢复，以及打开时漏列列族。
2. 天真分步移动键，对照 `sync=true` 的批写、`sync=false` 后掉电、`sync=false` 后只杀进程。
3. 快照读写、释放后的句柄、释放前已创建的 iterator，以及「快照仍活着时崩溃再恢复」。
4. iterator 扫到尽头时的 `status()`，以及钉住占用在 `close()` 前后的变化。
5. 40 键顺序点读、热键点读、`fill_cache=true/false` 的全表扫描对热键的影响。
6. 快写与慢写的停写时间，以及计数列族触发压力时标题列族的 `Incomplete`。
7. 偏紧预算与放宽预算，以及共享缓存与私有缓存的容量占用。

### 3. 运行并解读（真实输出）

```text
ALL_CHECKS_PASSED
SUMMARY batch_ops=6 titles_scan=3 open_missing_invalid=1 recovered=3 naive_dup=1 sync_atomic=1 unsync_lost=1 kill_atomic=1 snap_v1='Weekly digest' live_v2='Weekly digest (revised)' iter_after_release=1 snap_lost=1 snap_after=0 reopen_v2=1 pinned_during=96 pinned_after_close=0 iter_end_ok=1 range=4 cache_blocks=16 first_miss=16 first_hit=24 hot2_hit=8 fill_evict=28 hot_after_fill_miss=3 nofill_evict=0 hot_after_nofill_hit=8 fast_stop=4 fast_stop_s=1.17 fast_p99=0.2935 slow_stop=0 slow_p99=0.15 cross_incomplete=1 tight_stop=14 tight_stall=6 tight_p99=0.6 budget_stop=0 budget_miss=15 tight_miss=34 shared_cap=300 private_cap=600 status_all=1
```

### 4. 逐组对照真实数字

1. 一次 `WriteBatch` 写 6 步（3 个标题 + 3 个计数）。`titles` 范围扫描 3 条，扫描结果里没有 `:views` 键；`counters` 上能单独扫到三条计数。漏列列族打开得到 `InvalidArgument`。按三个列族重新打开后，三篇标题全部恢复，分别为 `Weekly digest`、`Product update`、`Community notes`。
2. 天真移动键在掉电后重复：旧键和新键都是 `Weekly digest`。同步批写原子成功；未同步批写掉电后整批丢失；只杀进程则整批仍在且仍原子。
3. 快照读到 `Weekly digest`，当前读到 `Weekly digest (revised)`。释放后新的 `Get` / `NewIterator` 都是 `invalid_argument: snapshot already released`，但释放前那个 iterator 仍可读到旧标题。崩溃时丢失 1 个活快照，恢复后快照数为 0，读到修订版。
4. 16 个标题扫到尽头：`Valid()` 为假，`status()` 为 `OK`。`SeekToFirst` 后钉住 96 字节，`close()` 后为 0。`[article:05:title, article:09:title)` 扫到 4 个键。
5. 40 个标题、8 个 SST、16 个块、缓存 400 字节。两遍顺序点读都是缺失 16、命中 24、0.01624 秒。热键第二遍 8 次命中。`fill_cache=true` 的全表扫描淘汰 28 次，之后热键缺失 3 次、耗时 0.00305 秒；`fill_cache=false` 淘汰 0 次，热键 8 次命中、耗时 0.00008 秒。
6. 快写 stop 4 次、停写 1.17 秒、p99 0.2935 秒；慢写 stop 0、p99 0.15 秒。计数列族先把停写压力堆起来之后，标题列族的 `no_slowdown` 写入返回 `Incomplete` 且键不存在，阻塞写入随后成功。
7. 偏紧预算 stop 14、stall 6、p99 0.6 秒、两遍点读缺失 34、0.03486 秒；放宽预算 stop/stall 为 0、缺失 15、0.01605 秒。共享缓存容量 300、占用 263；两份私有缓存容量合计 600、占用 467。

### 5. 对照验收标准

```text
# 验收 1：所有 Status 均检查
# Get 空键是 not_found（ok() 为假）；未知列族、漏列列族打开、已释放快照
# 都是 invalid_argument；iterator 扫到尽头 Valid=false 且 status 为 OK。
# no_slowdown 在停写压力下返回 Incomplete，键未被写入。
# status_all=1，每一处非 OK 都被检测，而不是忽略返回值。

# 验收 2：iterator 寿命和 snapshot 释放正确
# ReleaseSnapshot 之后，新的 Get/NewIterator 得到 invalid_argument；
# 释放前已创建的 iterator 仍可读到 Weekly digest，close 后钉住占用从 96 回到 0。
# 活快照在掉电时丢失（snap_lost=1），恢复后 snap_after=0，读到修订版。
# 快照不是跨重启的永久备份。
```

### 6. 自己动手的扩展练习

- 给 `titles` 和 `counters` 设不同的 `write_buffer_size`，看是不是仍然一个列族就能让整个 DB 返回 `Incomplete`。
- 在仍有活快照时强制压实，观察旧标题会不会被丢掉（生产引擎的 CompactionIterator 会为快照保留可见版本；本课教学压实没有实现这条，不要把引擎里「压掉了」直接当成 RocksDB 行为）。
- 把 block cache 改成严格容量限制，再拿长寿命 iterator 钉住超过容量的块，看随后的点读是失败还是超卖。

### 7. 和生产引擎的对应关系（不要记错边界）

- 教学 `Status` 的 `ok` / `not_found` / `incomplete` / `invalid_argument` 对应 RocksDB 的 `Status`；`NotFound` 时 `ok()` 为假，要用 `IsNotFound()` 区分。
- 教学列族共享一条 Durable WAL、各有 memtable 和 SST，对应官方「共享 WAL、不共享 memtable 和 table files」。没有实现 `max_total_wal_size` 和因过期列族而强制 flush。
- 教学 snapshot 用一个整数序号，崩溃后不会进入 MANIFEST；这与「Snapshots do not persist across DB restarts」一致。本课没有实现 Checkpoint。
- 教学块是固定条数的记录切片，缓存容量以字节计，默认 32 MB 是官方值，不是本课实验用的 400 字节。
- 教学 write stall 的 8/12 是阈值设定，不是官方默认。跨列族停写和 `Incomplete` 的语义按 Write-Stalls 页核对。

## 源码与文档阅读

按节点的 `sourceStudy`：从公开 Basic Operations 示例追踪 DBImpl，先核对 `Status` 与 iterator / snapshot 的寿命约定。2026-11-12 实际打开的页面如下。facebook 入口均为 301，跳转到 `meta/rocksdb` 后返回 200；下面保留节点和文档里给出的 facebook URL。

- [Basic Operations](https://github.com/facebook/rocksdb/wiki/Basic-Operations)（节点给出的入口）：打开与关闭、`Status`、`WriteBatch` 的原子更新、同步/非同步写、Iteration、Snapshots、Cache 与 `fill_cache`。用它核对「先 Delete 再 Put」的批写次序，以及 snapshot 用完必须 `ReleaseSnapshot`。
- [Column Families](https://github.com/facebook/rocksdb/wiki/Column-Families)：每个键属于一个列族、跨列族原子写、读写打开必须列出全部列族、handle 的寿命、共享 WAL 和不共享 memtable/SST。对照本课漏列列族时的 `InvalidArgument`，以及标题扫描不混入计数。
- [Snapshot](https://github.com/facebook/rocksdb/wiki/Snapshot)：第一句写明快照不跨重启；`GetSnapshot` / `ReleaseSnapshot`；compaction 会感知活快照。注意复数路径 `wiki/Snapshots` 在 2026-11-12 返回 404，不要引用。对照本课 `snap_lost=1`、恢复后读到修订版。
- [Iterator](https://github.com/facebook/rocksdb/wiki/Iterator)：`Valid()` 与 `status()` 的约定、隐式快照、iterator 钉住 memtable / SST / 当前 block、应当短命。对照本课扫到尽头时 `status=OK`，以及 `pinned_during=96` 在 `close()` 后归零。
- [Block Cache](https://github.com/facebook/rocksdb/wiki/Block-Cache) 与 [Memory usage in RocksDB](https://github.com/facebook/rocksdb/wiki/Memory-usage-in-RocksDB)：默认 32 MB LRU（8.2 前为 8 MB）、可共享、索引和 filter 默认不计在这笔预算里、iterator 钉住的块。对照本课 `fill_cache` 两套热键数字。
- [Write Stalls](https://github.com/facebook/rocksdb/wiki/Write-Stalls)：三个原因、按列族触发但作用于整个 DB、`no_slowdown` 返回 `Incomplete`。该页没有给出 slowdown/stop 的官方默认数字。
- [Setup Options and Basic Tuning](https://github.com/facebook/rocksdb/wiki/Setup-Options-and-Basic-Tuning)：`write_buffer_size` 默认 64 MB、block cache 建议约占预算三分之一、同一进程应共享同一个 Cache 对象。对照本课偏紧预算和放宽预算。
- [Checkpoints](https://github.com/facebook/rocksdb/wiki/Checkpoints)：目录级一致快照，可再打开。用它区分 snapshot 句柄和可持久的备份，不要把两者叫成同一个东西。

源码阅读按「先看调用约定，再看写入与缓存」两条线走。不要编造行号或未核对的函数名。

- [examples/column_families_example.cc](https://github.com/facebook/rocksdb/blob/main/examples/column_families_example.cc)：`CreateColumnFamily`、跨列族 `WriteBatch`、`Get`、`DropColumnFamily`。先按这个示例把 handle 和 `Status` 检查走一遍。
- [include/rocksdb/db.h](https://github.com/facebook/rocksdb/blob/main/include/rocksdb/db.h)：`DB::Open` 的多列族重载、`GetSnapshot`、`ReleaseSnapshot`、`NewIterator`、`Write`、`Close`。核对哪些调用返回 `Status`，哪些资源要由调用方释放。
- [db/db_impl/db_impl_write.cc](https://github.com/facebook/rocksdb/blob/main/db/db_impl/db_impl_write.cc)：前台 `Write` / `WriteImpl` 与 stall 处理的交点；结合第 106 课已经核对过的 `DelayWrite` 和 `no_slowdown`。
- [db/write_batch.cc](https://github.com/facebook/rocksdb/blob/main/db/write_batch.cc)：批里的编辑如何按顺序编码，帮助理解「先 Delete 再 Put」和跨列族原子提交。
- [cache/lru_cache.cc](https://github.com/facebook/rocksdb/blob/main/cache/lru_cache.cc) 与 [db/db_iter.cc](https://github.com/facebook/rocksdb/blob/main/db/db_iter.cc)：缓存分片与插入，以及 iterator 如何持有一致视图。阅读时关注块被钉住时容量为什么可能被突破。

阅读时记下三件事：你打开页面的日期和最终 URL；`Status`、snapshot、iterator 各自该由谁释放；快照页有没有把「不跨重启」写在开头。不要把本课教学引擎的函数名、缓存字节数或 8/12 阈值当成 RocksDB 的实现细节。

## 自测题

**1.** 读写打开一个已经有 `titles` 和 `counters` 的 DB 时，为什么只传 `default` 会失败？列族之间共享什么、不共享什么？这和「一次批写同时改标题和计数」有什么关系？

**答案：** 读写模式的 `DB::Open` 必须列出全部已有列族，否则返回 `InvalidArgument`。教学实验里这条状态是 `invalid_argument: must specify all existing column families, missing: titles,counters`。列族共享 WAL，不共享 memtable 和 SST，所以标题和计数可以独立配置、独立刷盘，又能在一个 `WriteBatch` 里原子提交。正是共享的那条 WAL，让跨列族的一批编辑要么一起持久化，要么一起不落地。只读打开才允许只打开子集。

**2.** 为什么说快照不是跨重启的永久备份？释放 snapshot 之后，已经创建的 iterator 为什么还能读到旧标题？如果需要一份重启后还能打开的副本，应该用什么？

**答案：** 官方 Snapshot 页写明 snapshots do not persist across DB restarts。教学实验在活快照下掉电，崩溃前还能读到 `Weekly digest`，恢复后快照数从 1 变成 0，只能读到 `Weekly digest (revised)`，旧句柄返回 `invalid_argument`。`ReleaseSnapshot` 之后，新的 `Get` 和 `NewIterator` 也不能再用这个句柄；但释放前已经创建的 iterator 在创建时钉住了那份一致视图（官方说 iterator 会钉住当时的 memtable、SST 和当前 block），所以仍能读到旧标题，直到它被 `close()`。需要可重新打开的目录副本时，应使用 Checkpoint 或备份，而不是 snapshot 句柄。

**3.** 40 个标题的两遍顺序点读为什么都是 16 次缺失、24 次命中？`fill_cache=true` 的全表扫描之后，热键为什么会重新缺失？同一条扫描把 `fill_cache` 关掉后，数字如何变化？偏紧预算和放宽预算的停写差异说明了什么？

**答案：** 40 个键刷成 8 个 SST、16 个块，教学缓存只有 400 字节，大约放下 4 个块（384 字节）。顺序点读时，每个块的第一键缺失、块内其余键命中，所以是 16 和 24；第二遍仍从最前面开始扫，末端那 4 个块会被前面的缺失淘汰掉，于是数字不变，耗时都是 0.01624 秒。热键第二遍可以 8 次全命中；但 `fill_cache=true` 的全表扫描会把这 8 个键挤出缓存（淘汰 28 次），之后热键变成缺失 3 次、耗时 0.00305 秒。`fill_cache=false` 时淘汰 0 次，热键仍是 8 次命中、耗时 0.00008 秒。偏紧预算下 stop 14、stall 6、p99 0.6 秒、两遍点读缺失 34；放宽预算后停写为 0、缺失 15。默认参数不必适合这台机器和这条负载，缓存和写入缓冲都要放进内存与 IO 预算里看，而不是只看 QPS。

## 推荐资源

- [RocksDB集成与读写调优：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/Basic-Operations)（github.com，英文文档，深入，免费）。重点阅读 DB 与 Column Family 与 WriteBatch、snapshot 与 iterator，用于核对本节点实验的语义和边界。2026-11-12 该入口 301 到 [meta/rocksdb 的同一页](https://github.com/meta/rocksdb/wiki/Basic-Operations) 后返回 200；同一次核对建议打开 Column-Families、Snapshot、Iterator、Block-Cache、Memory-usage-in-RocksDB、Write-Stalls、Setup-Options-and-Basic-Tuning 和 Checkpoints。注意 `wiki/Snapshots` 当日 404，应使用 `wiki/Snapshot`。

## 本课小结

- RocksDB 是嵌入式 LSM 存储引擎，应用要自己承担服务层、资源预算和 `Status` 检查，不能把它当成完整的分布式数据库。教学引擎是 Python 3 标准库的 `RocksDBEngine`，不是 RocksDB 本身；教学 SST 按固定条数切块，没有 bloom、压缩、Checkpoint、事务或真实 MANIFEST 日志。
- 列族共享 WAL、不共享 memtable 和 SST。读写打开必须列出全部列族，教学里漏列会得到 `invalid_argument: must specify all existing column families, missing: titles,counters`。一次 6 步批写提交三篇标题和计数，范围扫描只在 `titles` 上看到 3 条，重启后三篇标题都能恢复。
- `WriteBatch` 避免分步移动键在崩溃后留下重复值：天真路径掉电后两个键都是 `Weekly digest`；`sync=true` 的批写原子成功；`sync=false` 后掉电则整批丢失，只杀进程则整批仍在。快照能让当前标题已是 `Weekly digest (revised)` 时仍读到 `Weekly digest`，但活快照在掉电后丢失（1 变 0），不是跨重启的备份。释放快照后新的读失败，释放前的 iterator 仍有效；iterator 扫到尽头要检查 `status()`（本课为 `OK`），`close()` 后钉住占用从 96 回到 0。
- 教学缓存 400 字节装不下 16 个块时，两遍顺序点读都是缺失 16、命中 24、0.01624 秒。`fill_cache=true` 的全表扫描淘汰 28 次，热键随后缺失 3 次、0.00305 秒；`fill_cache=false` 淘汰 0 次，热键 8 次命中、0.00008 秒。官方默认 block cache 是 32 MB（8.2 前 8 MB），建议约占内存预算三分之一，并且多列族应共享同一个 Cache 对象。
- 快写停写 4 次、1.17 秒、p99 0.2935 秒，原因是 3 个 immutable memtable；慢写没有停写，p99 为 0.15 秒。计数列族触发的压力会让标题列族的 `no_slowdown` 写入返回 `Incomplete`。偏紧预算 stop 14、stall 6、p99 0.6 秒、缺失 34；放宽后停写为 0、缺失 15。默认参数不必适合当前机器和负载，调优要受内存和 IO 预算约束。

学完后可以用三句话自检：列族共享的是 WAL 还是 SST，读写打开为什么不能只写 `default`；快照和 Checkpoint 差在哪里，释放 snapshot 之后 iterator 为什么可能仍然有效；顺序扫描在缓存装不下时为什么第二遍仍会缺失，以及为什么某一个列族的停写会让另一个列族收到 `Incomplete`。三句都能答上来，并且能指出 `practice107/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验表，再回到官方文档里关于 `Status`、Column Family 打开约定、Snapshots do not persist across DB restarts、`Valid()`/`status()`，以及 write stall 按列族触发、作用于整个 DB 的那几句，不要只把「换上 RocksDB 就有持久化」这句话背下来。

写作笔记时顺便记下三件事：你核对文档的日期和实际打开的页面（包括 `wiki/Snapshot` 而不是 404 的 `wiki/Snapshots`），这次实验里每一处 `Status` 是否都检查了，以及快照在重启之后究竟还能不能用。这三件事能把本课的集成边界带进后面的存储引擎课，而不是只留下一句「RocksDB 很快」。
