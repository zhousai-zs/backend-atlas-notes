# 第 111/208 课：备份、校验与恢复演练

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-recovery` |
| 英文名 | Recovery Engineering |
| 领域 / 阶段 | G / 第 5 阶段 |
| 子类 | 数据生命周期 |
| 难度 | 专家 |
| 前置节点 | `g-rocksdb`、`g-replication` |
| 标签 | `Recovery Engineering`、`G`、`Systems` |
| 参考资料 | [备份、校验与恢复演练：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/How-to-backup-RocksDB)（github.com，文档；2026-11-13 该 URL 返回 301，跳转到 [meta/rocksdb wiki：How to backup RocksDB](https://github.com/meta/rocksdb/wiki/How-to-backup-RocksDB) 返回 200。同批核对 Checkpoints、Logger、RocksDB-Options-File，以及 `include/rocksdb/utilities/backup_engine.h`、`utilities/backup/backup_engine.cc`，均为 facebook 入口 301 后 200；raw.githubusercontent.com 上的头文件与实现直接 200。末尾带问号的 `wiki/How-to-backup-RocksDB?` 会跳到 wiki 首页，本课不引用） |
| 摘要 | 备份只有在可恢复且满足时间目标时才有价值，恢复流程应包含完整性校验和依赖项重建。 |

> **边界说明**：本课中的「内容后台」「文章标题」「阅读计数」「发布篇数」都是**教学假设**，用来让备份、校验、保留和恢复演练变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎，模块包括 `KVEngine` / `BackupEngine` / `BackupEngineOptions` / `DurableFS`（统称 `RecoveryEngine`），**不是 RocksDB，不是 Redis，也不是网络服务**。教学 SST 是带 CRC32 的 JSON 增量文件，不是 RocksDB 的 block-based SST；教学 MANIFEST、CURRENT 和 WAL 是简化的 JSON 与行日志，不是官方格式。它没有 Checkpoint 对象、没有 Env / FileSystem 抽象、没有 RateLimiter、没有 `backup_env`、没有多线程拷贝，也没有 blob 文件。`CreateNewBackup` / `VerifyBackup` / `RestoreDBFromBackup` / `PurgeOldBackups` 是对官方 API 的教学对应，不是对 `utilities/backup/backup_engine.cc` 的逐行移植。官方文档和头文件已于 **2026-11-13** 用 curl 核对（见下文）。文中的官方机制只写到核对过的页面和源码能够支撑的范围；教学实验里的字节数、耗时、标题和键数量都来自 `/workspace/atlas/lessons/practice111/results.json`，是这台教学机器上的实测，不是生产引擎的基准数字。

## 导读

### 一个直觉：备份任务绿了，不等于你能把数据带回来

内容后台的标题、计数、已发布篇数会落在本地 KV 里。运维面板上的备份任务如果显示成功，很容易让人以为「出事了可以重来」。节点把这件事说得很硬：备份只有在可恢复、并且满足时间目标时才有价值，恢复流程里还要做完整性校验和依赖项重建。任务成功只说明拷贝过程没有立刻报错，不说明文件没被静默写坏，也不说明你真的在隔离环境里打开过、核对过键的数量和业务不变量，更不说明恢复要花多久、会退回到哪一个时间点。

本课要盯住的不是「有没有做备份」，而是：这份备份是全量还是增量、保留策略删掉旧备份之后还能不能回到那个点、校验是只看大小还是连校验和一起看、恢复是在隔离目录里做的还是误把线上副本当成了备份。

### 接上前置

第 107 课把 RocksDB 当成嵌入式内核来用：写入走 WAL 和 memtable，刷盘形成 SST，snapshot 是进程内的读视图，**不会跨重启保存**。需要一份可重新打开的目录时，那是 Checkpoint，不是 snapshot。本课的备份建立在同一条线上：官方 wiki 写明，`BackupEngine::CreateNewBackup()` 是构建在 [checkpoint](https://github.com/facebook/rocksdb/wiki/Checkpoints) 之上的。

第 109 课讲复制、确认和故障切换。副本解决的是「另一台机器还能继续服务」，解决不了「这份写本身是错的」。误删除、错误的批量更新、有缺陷的发布脚本，都会顺着复制流到每一份副本上。节点把备份的为什么写成：抵御误删除、软件缺陷和跨副本传播的数据损坏。副本是可用性，独立备份是时间点。

### 一次备份走进 BackupEngine 之后

按 2026-11-13 核对的 [How to backup RocksDB](https://github.com/facebook/rocksdb/wiki/How-to-backup-RocksDB)，一次 `CreateNewBackup` 大致走这些步：

1. 如果 `flush_before_backup` 为真，先把 memtable 刷成 SST，这样拷进备份的 WAL 会少很多；该参数默认是 false。为假时，备份还会带上对应活 memtable 的 WAL。官方写明：无论这个参数取值，备份都应当与当时的数据库状态一致。
2. 禁用文件删除，避免拷贝过程中活文件被压实回收。
3. 取得 live files：table 文件、CURRENT、OPTIONS、MANIFEST；若未先 flush，再取活着的 WAL。
4. 把这些文件拷进备份目录。不可变且能够唯一标识的 table 文件，若备份目录里已经有了，就不再拷。自 6.12 起，官方用 file number 和 DB session ID 作为 SST 的唯一标识。CURRENT、MANIFEST、OPTIONS 以及 WAL 每次都进 `private/<id>/`，因为它们会变。
5. 重新启用文件删除。

备份目录是 BackupEngine 专用格式：`meta/<id>` 描述这一份备份有哪些文件；SST 在默认的增量模式下进 `shared_checksum/`，文件名带上原名、大小和校验和。之后可以用 `VerifyBackup` 检查，用 `RestoreDBFromBackup` 或 `RestoreDBFromLatestBackup` 恢复到指定的 DB 目录和 WAL 目录。官方恢复路径会给每个文件算校验和，不匹配就中止并返回 `Status::Corruption`。

### 教学场景（教学假设）

下面用「文章标题、阅读计数和已发布篇数」做例子。键形如 `article:001:title`、`article:001:views`，再加一个 `meta:published_count`。标题用 `Weekly digest` 和修订版 `Weekly digest (revised)`。业务不变量是：标题不能为空；有计数就必须有对应标题，且计数是非负整数；`published_count` 必须等于标题条数。这是教学假设，用来观察备份点、损坏检测和恢复是否真的回到了那个点，不代表任何线上内容系统的真实存储设计。

### 本课三条主线

1. **全量、增量与保留策略**：全量备份每份都独立拷齐文件；增量备份让不可变的 table 文件在多份备份之间共享，只拷新文件。保留策略决定能回到多远，也决定共享文件能不能被真正删掉。
2. **校验和与一致性备份**：一致的备份先冻结活文件集合再拷贝；校验既要看大小，也要能读数据算校验和。备份任务成功、只按大小核对通过，都还不够。
3. **RPO、RTO 和恢复演练**：在隔离环境里从备份恢复，必要时再重放备份点之后的 WAL，核对键数量、值和业务不变量，并记下实际恢复耗时和恢复点。线上副本不能替代独立备份。

## 学习目标

- 能解释全量、增量与保留策略。
- 能独立完成：在隔离环境从备份与 WAL 恢复 KV，校验键数量、值和业务不变量。

## 为什么后端要懂

公众号后台如果把「已发布标题」只信主从复制，一次错误的批量改写会同时出现在主库和副本上。那时候再去副本上找「上一份好数据」，找不到。独立备份是另一个时间点上的文件集合，而且必须能在隔离目录里打开、核对不变量。后端要能说清：这份备份覆盖到哪一次写入、恢复要花多久、校验没做会漏掉什么、删旧备份时会不会把仍被引用的共享文件留下。这些决定的是事故发生后你能不能把标题带回来，而不是备份任务是不是绿的。

## 核心知识点

### 1. 全量、增量与保留策略

全量备份对应 `BackupEngineOptions::share_table_files=false`：每一份备份都在 `private/<id>/` 里放齐自己的文件，互不共享。好处是删掉任意一份都不会影响别的备份；代价是第二份、第三份会把已经有的 SST 再拷一遍。

增量是官方的默认路径。2026-11-13 核对的 `backup_engine.h` 里，`share_table_files` 默认为 true，`share_files_with_checksum` 默认为 true。新备份只拷还没有的 table 文件，已有的按「原名 + 大小 + 校验和」共享。本课实验里，200 个标题做成第一份备份，逻辑大小和拷贝量都是 13051 字节；再写入 200 个标题后做第二份，逻辑大小变成 25965 字节，但实际只拷了 13069 字节，命中共享文件 1 次。同一份数据若走全量，第二份会拷 25965 字节。第三份只新增 50 个修订标题，拷贝量进一步降到 3986 字节，共享命中 2 次。第二份的拷贝量 13069 并不小于第一份的 13051，因为新 SST 本身和第一份差不多大。增量的意义是「不再拷已经在备份目录里的 SST」，不是「第二次拷贝量一定更小」。

保留用 `PurgeOldBackups(N)`：只留下最新的 N 份，也可以 `DeleteBackup(id)` 删指定一份。本课在三份增量备份之后调用 `PurgeOldBackups(2)`，ID 从 `[1, 2, 3]` 变成 `[2, 3]`。再去恢复 1，得到 `not_found: backup 1`；恢复 2 则回到 400 个 `Weekly digest`，指纹 `7d9b79ab92054eb049792cdb02d9134a1ba0971a558f96d15a74f44e512fc4c0` 与第二波写入后一致。这里还有一个容易算错的账：备份目录从 34095 字节降到 32831 字节，只少了 1264 字节，远小于第一份备份 13051 字节的逻辑大小。因为 `000002.sst` 仍被备份 2 和 3 引用，删掉备份 1 只能收回它的私有文件和 meta，收不回那份共享 SST。保留策略决定的是「还能恢复哪些 ID」，不是「删一份就按逻辑大小把空间吐出来」。

官方还提醒：`BackupEngine::Open()` 的耗时和已有备份数量成正比，远程文件系统上这个成本更明显，所以既要保留足够的时间点，也不要无限制堆下去。

### 2. 校验和与一致性备份

一致性首先来自「拷贝时文件集合是冻结的」。官方步骤里先 `Disable file deletions`，再 `Get live files` 并拷贝。教学里有一条对照路径：一边拷目录一边做 flush，再把新的 CURRENT / MANIFEST 覆盖过去、却没把新 SST 带走，打开时就会报找不到 `000005.sst`。这不是 BackupEngine，只是在展示「看起来像备份的目录拷贝」为什么不够。

校验要分两层。wiki 和头文件都写明：`VerifyBackup` 默认只检查文件是否存在、大小是否与记录一致，`verify_with_checksum` 默认为 false；带校验和的核对要读完全部数据。`BackupEngine::Open()` 本质上是对每一份备份做一次**不带校验和**的 `VerifyBackup`，用来把大小对不上的备份标成损坏。恢复则更严：官方对每个恢复文件计算校验和，不匹配就中止并返回 `Status::Corruption`。

本课把一份已经成功的备份里 `000002.sst` 的最后一字节异或掉，文件大小仍是 6495，CRC 从 1718804037 变成 1265643976。之后只按大小做 `VerifyBackup` 仍然是 OK，带校验和则返回 `corruption: checksum mismatch 000002.sst`。官方恢复路径同样中止。教学里那条「跳过校验和直接把文件拷回去」的路径，拷贝本身会成功，但打开教学 SST 时仍会因文件头里的 CRC 对不上而失败。备份任务返回成功这件事一直成立——这正是节点写的常见误区：备份任务成功不代表备份内容可用。

还有两个和「这份文件到底是不是同一份」有关的选项。`share_files_with_checksum=false` 已废弃：共享目录只按原文件名存放 SST，从非最新备份恢复后再写、再备份，即使只备份同一个 DB，也可能把更早那份备份里的同名文件覆盖掉。头文件的原文是 deprecated and potentially dangerous。本课按这个顺序做了一次：备份 1 和 2，从 1 恢复出分叉历史后再做备份 3，两份都去写 `shared/000005.sst`，再恢复备份 2 时得到 `corruption: size mismatch 000005.sst: 3644 != 3795`。改用默认的校验和命名后，备份 2 的第二份 SST 是 `shared_checksum/000005_3795_921b9daf.sst`，备份 3 是 `shared_checksum/000005_3644_652e929e.sst`，两份都还共享同一份未改动的 `shared_checksum/000002_3293_d106378a.sst`；恢复备份 2 仍是 100 个 `Weekly digest`。

`sync` 默认是 true：每个备份文件写入后 `fsync`，机器崩溃或重启后备份仍然一致。设为 false 会快一些，但较新的备份可能不一致；头文件还写明，此时只能保证创建新备份时不会改掉已经同步过的旧备份。教学用 `DurableFS` 模拟「可见字节 / 已 fsync 前缀」，不是内核页缓存的完整模型：`sync=true` 时崩溃后备份 1 和 2 都在；`sync=false` 时先把第一份当作已同步，再创建第二份后模拟掉电，重新 `Open` 只能看到备份 1，第二份的 meta 没有落稳，引擎看不到它，而不是把它标成 corrupt。

### 3. RPO、RTO 和恢复演练

RPO 是恢复点目标：出事之后，你最多能接受丢掉多远的数据。RTO 是恢复时间目标：最多能接受花多久把服务拉回来。它们是目标，不是某次备份命令的返回值。一次演练要记下的是**实际**恢复点和**实际**耗时，再拿去和目标比。

本课的演练是：先给 100 个 `Weekly digest` 做一份备份（201 个键，含 100 条计数和 `published_count=100`），然后把 `article:001:title` 改成 `Weekly digest (revised)`，再新增 20 篇文章，共 21 次逻辑写。活库此时是 120 个标题、241 个键。把备份点之后的 WAL 拷到隔离的归档目录——这是「备份与 WAL」，不是把线上副本再复制一份。接着清空活目录。

只把备份恢复到隔离目录时，回到 100 个 `Weekly digest`，指纹与备份点一致，RPO 是那 21 次逻辑写；这次恢复耗时见 `results.json` 的 `backup_only.restore_seconds`（本次为 0.001217 秒）。把归档 WAL 一起重放后，回到 120 个标题，`article:001` 是修订版，指纹与出事之前的活库一致，RPO 为 0，耗时 0.00171 秒。两条路径都在隔离目录里核对了键数量、标题值和 `published_count`。这些耗时是教学机器上的进程内拷贝，只能用来说明「演练必须记下数字」，不能当成生产 RTO。

为什么一定要隔离，而且不能用线上副本代替？另一组实验先给 80 个标题做独立备份，副本在故障前与主库指纹一致。随后一个教学用的逻辑错误把全部标题写成空字符串，并复制到副本上。副本此时 `invariants_ok=false`，80 条标题都为空。把副本当成恢复源，带不回 `Weekly digest`。从独立备份恢复到隔离目录，不变量通过，标题回到 `Weekly digest`，指纹与故障前的备份点一致。这就是验收里的第二句：不使用线上副本替代独立备份。

## 常见误区

- **备份任务成功，备份就可用。** 节点的 pitfalls 就是这句。本课里 `CreateNewBackup` 已经返回成功，再把 SST 的一个字节改掉，只按大小校验仍然通过，带校验和的校验与官方恢复都会失败。
- **只核对文件大小就够了。** `VerifyBackup` 默认正是只看大小；`Open()` 也只做这一层。大小不变的静默损坏要靠校验和，而且官方恢复路径才会强制做这一步。
- **删掉旧备份，就会按它的逻辑大小把空间全部收回。** 增量共享下，旧备份引用的 SST 只要还被更新的备份用着，就还会留在 `shared_checksum/` 里。本课删掉备份 1 之后只少了 1264 字节。
- **有副本就不需要独立备份。** 复制会把逻辑错误一起带走。本课里空标题同步到了副本，只有故障前的独立备份能在隔离目录里把不变量补回来。
- **`flush_before_backup=false` 的备份是不一致的。** 官方写明两种取值下备份都应与当时的 DB 状态一致，差别只是会不会多拷活着的 WAL。
- **有备份、却从未在隔离环境恢复过，也算知道自己的 RPO / RTO。** 目标不会自动变成实测。演练之前，你不知道恢复会回到哪个标题、要花多久、不变量过不过得了。

## 设计权衡

节点把权衡写成：更多备份降低风险，也增加容量、合规和恢复验证成本。多留几个时间点，误删或坏发布之后能选的恢复点就更多；但每一份都要占空间，增量共享还让「删掉一份」和「释放空间」不再相等，合规上保留多久、在哪存放、谁能取回，都要额外的流程。更关键的成本是验证：每多一份备份，就多一次「真正恢复并核对不变量」的工作，否则多留的只是还没演练过的目录。

全量和增量是空间与耦合的交换。全量占得多，删除干净；增量拷得少，备份之间共享文件，错误的命名规则或一次覆盖会伤到更早的备份。`share_files_with_checksum` 默认打开，正是为了避免按文件名共享带来的分叉历史问题；关掉它能省一点名字上的复杂度，但官方已经标成危险。

`sync=true` 换来的是崩溃后备份仍一致，代价是每次拷贝都要 `fsync`。`VerifyBackup` 带校验和更安全，但必须读完数据。把 RPO 收得很紧，就要更频繁的备份，或把备份点之后的 WAL 单独归档；这些都会增加容量和演练次数。目标定多紧，取决于丢 21 次标题修订你能不能接受，而不是取决于备份任务是不是经常成功。

## 动手实践：在隔离环境从备份与WAL恢复KV，校验键数量、值和业务不变量

本课不启动 RocksDB，也不连接真实数据库；下面的「BackupEngine」「共享 SST」「恢复」都由 `/workspace/atlas/lessons/practice111` 里的教学 `RecoveryEngine` 模拟。代码用 Python 3.13.5 标准库写成，不链接 RocksDB 或 Redis，不走网络。运行 `./test.sh` 会执行全部实验并写下 `results.json`。本次已跑通，输出 `ALL_CHECKS_PASSED`。下面的数字都来自这份结果，没有改写。

### 0. 文件结构

- `engine.py`：`Status`、`DurableFS`、`KVEngine`（memtable + 增量 SST + WAL）、`BackupEngine` / `BackupEngineOptions` / `BackupInfo`，以及 `check_invariants` / `naive_copy_db`。
- `app.py`：六组实验。全量与增量及保留、校验和与一致性拷贝、已废弃的按名共享、`sync` 与崩溃、RPO / RTO 演练、副本不能替代独立备份。
- `test_recovery.py` / `test.sh`：验收并写出 `results.json`。
- `results.json`：本课所有教学数字的来源。

### 1. 准备数据与目标操作

教学键是 `article:NNN:title`、`article:NNN:views` 和 `meta:published_count`。标题默认 `Weekly digest`，修订值是 `Weekly digest (revised)`。不变量见上文。200 篇文章对应 401 个键（200 条标题、200 条计数、1 个发布篇数）。实验要观察：增量少拷了多少、删旧备份后还能恢复哪一份、损坏后只按大小校验和带校验和的校验分别说什么、只恢复备份与备份加 WAL 会回到哪个标题、副本被逻辑错误污染之后独立备份还能不能在隔离目录里把不变量带回来。

### 2. 要比较的几条路径

1. 200 个标题 flush 后备份，再加 200 个 flush 后备份，再加 50 个修订标题后备份；同一组数据分别走增量（默认共享）和全量（`share_table_files=false`），最后 `PurgeOldBackups(2)`。
2. 100 个标题备份成功后，把备份里一份 SST 的一个字节翻掉，比较只按大小的 `VerifyBackup`、带校验和的 `VerifyBackup`、官方恢复和跳过校验和的天真恢复；再对照「拷贝中途 flush」的撕裂目录和一次一致的 `CreateNewBackup`。
3. 备份 1、备份 2，从备份 1 恢复后写入分叉标题再备份 3，比较 `share_files_with_checksum=false` 与默认的校验和命名，看备份 2 还能不能完好恢复。
4. `sync=true` 与 `sync=false` 各做两份备份，模拟掉电后重新 `Open`。
5. 100 个标题备份后做 21 次逻辑写，清空活目录；一条路径只恢复备份，一条路径恢复备份并重放隔离归档的 WAL。记录耗时、恢复点和不变量。
6. 80 个标题先做独立备份，再让一个写空标题的逻辑错误复制到副本上；比较「从副本恢复」和「在隔离目录从独立备份恢复」。

### 3. 运行并解读（真实输出）

在 `practice111` 目录执行 `./test.sh`，应看到 `ALL_CHECKS_PASSED`。本次摘要里和课文相关的数字如下（完整字段在 `results.json`）。

增量第二份拷贝 13069 字节，全量第二份拷贝 25965 字节，增量第二份逻辑大小也是 25965，共享命中 1；第三份增量只拷 3986 字节，共享命中 2。`PurgeOldBackups(2)` 之后留下 `[2, 3]`，目录减少 1264 字节。SST 被翻掉一个字节后，只按大小的校验仍通过，带校验和的校验与官方恢复都失败。只恢复备份：100 个 `Weekly digest`，RPO 21 次写，耗时 0.001217 秒。备份加 WAL：120 个标题，`Weekly digest (revised)`，RPO 0，耗时 0.00171 秒。副本不变量失败；隔离目录里从独立备份恢复成功，标题回到 `Weekly digest`，耗时 0.001735 秒。

### 4. 逐组对照真实数字

全量与增量（`full_incremental_retention`）：

| 备份 | 增量 logical / copied / shared_hit | 全量 copied |
|---|---|---|
| 1（200 个标题） | 13051 / 13051 / 0 | 13051 |
| 2（再加 200） | 25965 / 13069 / 1 | 25965 |
| 3（再加 50 个修订标题） | 29778 / 3986 / 2 | 29778 |

`PurgeOldBackups(2)` 之后 ID 为 `[2, 3]`，恢复 1 得到 `not_found: backup 1`，恢复 2 为 400 个 `Weekly digest`，指纹 `7d9b79ab92054eb049792cdb02d9134a1ba0971a558f96d15a74f44e512fc4c0`。目录从 34095 字节降到 32831，少 1264 字节。

校验与一致性（`checksum_and_consistency`）：备份任务成功；损坏前大小校验和带校验和的校验都是 OK。`000002.sst` 翻 1 字节后 size 仍为 6495，CRC 从 1718804037 变为 1265643976；只按大小仍为 OK，带校验和与官方恢复都是 `corruption: checksum mismatch 000002.sst`。天真恢复拷贝成功但打开失败；撕裂目录打开失败（缺 `000005.sst`）；一致备份恢复成功，100 个 `Weekly digest`，`restore_seconds=0.001367`。

废弃按名共享（`deprecated_shared_naming`）：`shared/000002.sst` 与 `shared/000005.sst` 被备份 2 和 3 共用，恢复 2 得到 `corruption: size mismatch 000005.sst: 3644 != 3795`。校验和命名下备份 2 为 `shared_checksum/000002_3293_d106378a.sst` 与 `shared_checksum/000005_3795_921b9daf.sst`，备份 3 的新 SST 是 `shared_checksum/000005_3644_652e929e.sst`，恢复 2 完好。

`sync` 与崩溃（`unsynced_backup_crash`）：`sync=true` 时崩溃后仍可见 `[1, 2]`；`sync=false` 时可见 `[1]`，较新的一份看不到。

RPO / RTO 与副本对照见下一节验收块，数字不再重复改写。

### 5. 对照验收标准

```
# 验收 1：记录实际恢复耗时与恢复点
# 备份点：100 个 Weekly digest，key_count=201，fingerprint=
#   d73821e9642790139d241f3dc705b6005dc13114b5e5c4cbe42783b9e351791d。
# 之后 21 次逻辑写，活库 120 个标题，article:001 为 Weekly digest (revised)，
#   fingerprint=4d1605d114d80101b5d6e66a5771769b762d5b1561de8b1fc1ec0594b89377e0。
# 只恢复备份（隔离目录 drill/restore_backup_only）：
#   title_count=100，restore_point_title=Weekly digest，
#   matches_backup_point=true，rpo_writes=21，
#   restore_seconds=0.001217，不变量通过。
# 恢复备份并重放归档 WAL（drill/restore_backup_and_wal）：
#   title_count=120，restore_point_title=Weekly digest (revised)，
#   matches_pre_disaster_live=true，rpo_writes=0，
#   restore_seconds=0.00171，不变量通过。
# 这是本次演练测到的恢复点和耗时，不是官方 SLA，也不是生产 RTO。

# 验收 2：不使用线上副本替代独立备份
# 故障前主库与副本指纹同为
#   32f79b77d09aeace7cc3500c4b97f94bcbf80df6fc32d4a27727f637a1dff9f8，80 个 Weekly digest。
# 空标题的逻辑错误被复制后：replica_invariants_ok=false，empty_titles=true，
#   broken_count=80。用副本当恢复源，带不回标题。
# 在隔离目录从独立备份恢复：invariants_ok=true，sample_title=Weekly digest，
#   matches_pre_bug_backup=true，not_using_online_replica=true，
#   restore_seconds=0.001735。
# replica_cannot_replace_independent_backup=true。
```

### 6. 自己动手的扩展练习

- 把 `PurgeOldBackups(2)` 改成保留 1 份，看备份 2 是否还能恢复，以及 `000002.sst` 会不会因此被回收。
- 在损坏 SST 之后，先做一次不带校验和的 `VerifyBackup` 再 `Open` 一个新的 `BackupEngine`，看 `Open` 会不会把这份大小仍然正确的备份标成 corrupt。
- 给 `CreateNewBackup` 加上 `flush_before_backup=false`，让备份包含活 WAL，恢复时不再另归档一份 WAL，比较恢复点是否仍能到修订版标题。

### 7. 和生产引擎的对应关系（不要记错边界）

教学 `CreateNewBackup` 会禁用删除、拷 live files、在增量模式下按大小和 CRC 共享 SST，对应官方步骤和 `shared_checksum` 命名，但没有硬链接、没有 file number 与 DB session ID 那套 6.12 唯一标识、也没有多线程和限速。教学 `VerifyBackup` 区分大小和校验和，教学恢复默认核校验和，这与官方一致；「跳过校验和的恢复」只是对照，不是官方选项。`DurableFS` 只模拟未 `fsync` 的字节在掉电后丢失。教学 WAL 归档是把备份点之后的日志文件拷到另一目录再重放，不是 RocksDB 的 WAL 运输或备份增量命令。业务不变量是应用层检查，不是引擎提供的约束。

## 源码与文档阅读

按节点的 `sourceStudy`：沿 RocksDB BackupEngine 的官方示例核对创建、校验、保留与恢复流程。2026-11-13 实际打开的页面如下。facebook 入口均为 301，跳转到 `meta/rocksdb` 后返回 200；下面保留节点和文档里给出的 facebook URL。当日最新正式发布是 v11.13.2（2026-11-04）；main 上 `version.h` 写的是 11.15.0，注释说明这是计划中的下一个版本号，不要把它当成已发布版本。

- [How to backup RocksDB](https://github.com/facebook/rocksdb/wiki/How-to-backup-RocksDB)（节点给出的入口）：`CreateNewBackup`、`GetBackupInfo`、`VerifyBackup`、`RestoreDBFromBackup` / `RestoreDBFromLatestBackup`、目录结构、增量共享、`PurgeOldBackups`、`CreateNewBackup` 建立在 checkpoint 上的那五步，以及 `flush_before_backup` 与 `sync` 的含义。用它核对本课「只按大小校验不够」和「恢复必须对校验和」。注意 `wiki/How-to-backup-RocksDB?` 当日会跳到 wiki 首页，不要引用。
- [Checkpoints](https://github.com/facebook/rocksdb/wiki/Checkpoints)：同一文件系统上 SST 用硬链接，否则拷贝；MANIFEST 和 CURRENT 拷贝；多列族时还要拷贝覆盖该时间窗的 WAL。用它区分第 107 课的 snapshot 句柄和可重新打开的目录副本，也用来理解备份为什么先禁用删除再取 live files。
- [include/rocksdb/utilities/backup_engine.h](https://github.com/facebook/rocksdb/blob/main/include/rocksdb/utilities/backup_engine.h)：`BackupEngineOptions` 的默认值（`share_table_files`、`share_files_with_checksum`、`sync`、`backup_log_files`、`flush_before_backup`），`VerifyBackup` 两个层次的注释，以及 `PurgeOldBackups` / `DeleteBackup`。`share_files_with_checksum=false` 被标明 deprecated and potentially dangerous，对照本课分叉历史那组。
- [utilities/backup/backup_engine.cc](https://github.com/facebook/rocksdb/blob/main/utilities/backup/backup_engine.cc)：实现与 meta 文件格式。需要看实现时从这里读，不要把教学引擎当成这份源码的缩写。
- 备份 wiki 还指向 [Logger](https://github.com/facebook/rocksdb/wiki/Logger) 和 [RocksDB Options File](https://github.com/facebook/rocksdb/wiki/RocksDB-Options-File)。2026-11-13 这两个入口均为 301 后 200，本课只记录它们存在，不展开未在正文里核对过的细节。

## 自测题

**1.** 增量备份的第二份拷了 13069 字节，第一份是 13051 字节。这能说明增量没生效吗？为什么 `PurgeOldBackups(2)` 之后，备份目录只少了 1264 字节？

<details>
<summary>答案</summary>

不能说明增量失败。第二份的逻辑大小是 25965 字节，实际只拷了 13069，共享命中 1 次，而且明显少于全量第二份的 25965 字节。拷贝量和第一份接近，是因为新 SST 本身和第一份差不多大。增量的含义是不再拷已经在备份目录里的 `000002.sst`。删掉备份 1 之后，这份 SST 仍被备份 2 和 3 引用，所以只能收回备份 1 的私有文件和 meta，空间下降 1264 字节，远小于 13051。

</details>

**2.** 备份任务返回成功，接着只按文件大小做 `VerifyBackup` 也是 OK。能否据此宣布这份备份可以用来恢复？

<details>
<summary>答案</summary>

不能。本课在成功备份之后只改了 SST 的一个字节，大小仍是 6495，只按大小的校验仍然通过，带校验和的 `VerifyBackup` 和官方 `RestoreDBFromBackup` 都返回 `corruption: checksum mismatch 000002.sst`。节点的误区就是：备份任务成功不代表备份内容可用。可用性要靠校验和，并且要在隔离目录里真正恢复、核对应有的键和值。

</details>

**3.** 主库上一个脚本把已发布标题全部写空，副本已经跟上。这时应该从哪里恢复？只恢复备份，和恢复备份并重放备份点之后的 WAL，恢复点有什么差别？

<details>
<summary>答案</summary>

不能从副本恢复。逻辑错误会被复制，本课里副本 80 条标题为空，不变量失败。应该在隔离目录里用故障前的独立备份。只恢复备份会回到备份点：100 个 `Weekly digest`，RPO 为之后的 21 次逻辑写；把归档 WAL 一并重放，可以回到出事之前的活库：120 个标题，`article:001` 为 `Weekly digest (revised)`，RPO 为 0。两种做法都要记下实际耗时和恢复点，并且核对 `published_count` 与标题条数是否一致。

</details>

## 推荐资源

- [备份、校验与恢复演练：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/How-to-backup-RocksDB)（github.com，英文文档，专家，免费）。重点阅读全量、增量与保留策略与校验和与一致性备份，用于核对本节点实验的语义和边界。2026-11-13 该入口 301 到 [meta/rocksdb 的同一页](https://github.com/meta/rocksdb/wiki/How-to-backup-RocksDB) 后返回 200；同一次核对建议打开 [Checkpoints](https://github.com/facebook/rocksdb/wiki/Checkpoints) 和 [backup_engine.h](https://github.com/facebook/rocksdb/blob/main/include/rocksdb/utilities/backup_engine.h)。注意 `wiki/How-to-backup-RocksDB?` 当日会跳到 wiki 首页，应使用不带问号的路径。

## 本课小结

- 备份的价值在可恢复，以及能不能满足时间目标。全量每份都独立拷齐；增量按默认的 `share_table_files` 与 `share_files_with_checksum` 共享不可变 SST，本课第二份只拷 13069 / 25965 字节。保留策略决定还能恢复哪些 ID：`PurgeOldBackups(2)` 之后备份 1 不可恢复，备份 2 仍是 400 个 `Weekly digest`，但共享 SST 不会因为删掉备份 1 就按 13051 字节全部释放。
- 一致的备份要冻结活文件再拷贝。`VerifyBackup` 默认只看大小，`Open()` 也是如此；官方恢复才会强制核对校验和。备份任务成功之后翻掉一个字节，大小校验仍然通过，带校验和的校验和恢复都会失败。按文件名共享是已废弃且危险的路径，分叉历史会把更早的备份改坏。
- 演练必须在隔离环境里做，并记下恢复耗时和恢复点。本课只恢复备份时 RPO 为 21 次写、标题回到 `Weekly digest`；备份加 WAL 时 RPO 为 0、标题为修订版。线上副本会被逻辑错误一起污染，不能替代独立备份。
- 学完后可以用三句话自检：全量和增量分别怎样占用空间，删旧备份时为什么共享文件还在；为什么备份任务成功和只按大小校验都不够，官方恢复为什么要算校验和；为什么副本不是备份，只恢复备份和备份加 WAL 会回到哪个点、要记哪些数字。三句都能答上来，并且能指出 `practice111/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验字段，再回到官方文档里关于「`share_table_files` / `share_files_with_checksum`」「`VerifyBackup` 默认只看大小」「恢复时校验和不匹配返回 `Status::Corruption`」「`PurgeOldBackups`」「`CreateNewBackup` 建立在 checkpoint 上」的那几句，不要只把「要做备份」这句话背下来。
