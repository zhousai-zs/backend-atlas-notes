# 第 142/208 课：Git 工作流与变更管理

## 元信息

| 项 | 内容 |
|---|---|
| 序号 | 142/208 |
| 节点 ID | `j-git` |
| 英文名 | Git |
| 领域 / 阶段 | J / 第 0 阶段 |
| 子类 | 构建与变更管理 |
| 难度 | 入门 |
| 前置节点 | [`a-shell`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/5.html) |
| 标签 | `Git`、`Engineering` |
| 参考资料 | [Pro Git — Branching and Merging](https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging)（Pro Git，英文，入门，免费）：节点给出的唯一资源。节点标注 `verifiedAt` 为 2026-10-26；本课于 2026-11-14 用 curl 复核，返回 HTTP 200，无跳转，139702 字节，`<title>` 为 Git - Basic Branching and Merging，属 Pro Git 第 2 版 3.2 节。响应头 `Date` 为 2026-11-14 06:16:25 GMT（北京时间 14:16:25），`Last-Modified` 为 2026-11-12 04:34:47 GMT（北京时间 12:34:47）。 |
| 摘要 | Git 记录快照并用提交图表达变更历史；分支是提交引用，合并与变基改变历史组织方式。 |

> **边界说明（教学假设）**：本课的 `config.ini`、超时从 200 改到 500、同事在 main 上把同一行改成 300、最终取 400、坏提交把 `retry` 改成 100、`feature/shared` 的协作过程，都是教学实验里的设定，不对应腾讯微信公众号后台的真实配置、发布流程或线上事故。本课实验部分的所有哈希、提交数、退出码和输出片段都来自 `practice142/run.log` 的一次真实运行，环境是 git 2.47.3；Pro Git 的示例哈希和页面元数据是文档引用，已在「源码与文档阅读」中单独标明。

## 导读

这是 J 领域的第一课，学习位置从 I 领域的性能与容量转入工程实践。容量课关心「这台机器还能扛多少」，本课关心「这次改动是什么、由谁引入、怎样安全地回退」。两件事都需要可核对的证据，只是证据从压测数字换成了提交、分支和引用日志。

先看一个教学场景。假设公众号后台某服务用 `config.ini` 保存 `timeout_ms` 与 `retry`。你把超时从 200 调到 500，开了一个功能分支；同事同时在 main 上为线上问题把同一行改成 300。两条线要合到一起，Git 不会替你判断哪个值对，它只会把冲突标出来。另一次，某个把 `retry` 改成 100 的错误提交已经推送，后面还跟着一个文档提交，你要在不改写公开历史的前提下撤掉它。最后，`feature/shared` 已经推给同事，你要判断这次能不能变基。这些操作分别落在本课的三条主线上。前置 [`a-shell`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/5.html) 提供了命令、退出码、重定向和脚本执行的基础；本课把这些能力用于变更管理，下面的实验脚本固定作者和时间，在临时目录里建裸仓库和两个克隆，因此同一份脚本可以得到可复现的短哈希。本课三条主线如下。

1. **工作区/暂存区/提交**：文件先出现在工作区，`git add` 把选中的内容放进暂存区，`git commit` 才把它写成提交对象。暂存不是备份，提交才是历史里的一个节点。
2. **branch/merge/rebase**：分支是指向提交的引用，`HEAD` 指向当前分支。合并保留两条父线并生成合并提交；变基把提交重放到新基底上，产生新的哈希。
3. **revert 与 reset**：`revert` 追加一个反向提交，适合已经共享的历史；`reset` 移动分支指针，适合尚未共享的本地提交，`--hard` 还会丢弃工作区和暂存区的改动。

## 学习目标

- 能解释工作区、暂存区、提交三者的边界，说明 `git status`、`git diff`、`git add`、`git restore` 各自观察或改变哪一层。
- 独立完成节点实践：创建功能分支，制造并解决一次文本冲突，再 revert 一个错误提交；能从输出中读出冲突状态、父提交数和提交数变化。
- 能解释公开分支变基的协作风险：为什么哈希会变、普通推送为什么被拒、同事为什么会得到重复提交。
- 能用 `git log -S`、`git log -L` 和 `git blame` 找到某一行引入的提交，并知道合并提交默认不显示 diff 这个坑。

## 为什么后端要懂

在教学假设的公众号后台场景里，配置改动经常和线上 hotfix 并行发生。你改的是超时，同事改的是同一行的另一个值；如果没有分支和提交图，最后只能靠聊天记录判断谁覆盖了谁。Git 把「谁在什么父提交上改了什么」记录成不可变对象，冲突时把两个版本都保留下来，解决后再写一个合并提交。这个合并提交的父提交数在本实验里是 2，不是装饰，它说明两条线都被接入了当前历史。线上问题需要回退时，区别更明显：错误提交 `3cb1666` 已经把 `retry` 改成 100，后面还有 `eb1d5ca` 的文档提交，而且两者都已推送。此时用 `reset` 改写 main 会让远端历史与同事的本地历史分叉；用 `revert` 则新增 `a9b6faa`，提交数从 6 变成 7，`retry` 回到 1，坏提交和反向提交都留在历史里，审计时能看到「问题是什么、何时被撤销」。如果错误提交只在本地、尚未推送，`reset` 才是一个干净的选择。排障还经常需要回答「这一行是谁引入的」：本实验里 `git log -S'timeout_ms=400'` 默认命中 0 条，因为 400 是在解决冲突的合并提交里引入的，而 `git log -S` 默认不看合并提交的 diff；加上 `--diff-merges=first-parent` 后能找到 `0d91107`，`git log -L1,1:config.ini` 和 `git blame -L1,1` 也指向它。

## 核心知识点

### 1. 工作区/暂存区/提交

工作区是你能直接编辑的目录；暂存区（index）是下一次提交的候选快照；提交是对象数据库里的一个节点，包含树、父提交、作者、提交者和说明。`HEAD` 指向当前分支的最新提交。三者的关系可以用一次新建文件看清。实验开始时 `config.ini` 有 3 行，执行 `git status --short` 先看到 `?? config.ini`，`git add config.ini` 后变成 `A  config.ini`，提交后状态为空，脚本打印 `(干净)`。`??` 表示未跟踪，`A` 表示已加入暂存区，空白表示工作区、暂存区和 `HEAD` 一致。

```text
?? config.ini
A  config.ini
(干净)
lines in HEAD: 3
diff(工作区 vs 暂存区):
 config.ini | 1 +
 1 file changed, 1 insertion(+)
```

```text
工作区 --git add--> 暂存区 --git commit--> * eb3e2ee init config
   ^                                        |
   +------------- git restore --------------+
```

提交之后脚本又向工作区追加了一行 `retry=2`，此时 `git diff --stat` 显示 `1 file changed, 1 insertion(+)`。这条 `git diff` 比较的是工作区与暂存区，不是工作区与 `HEAD`；要看暂存区与 `HEAD` 的差异，应使用 `git diff --cached`。实验随后执行 `git restore config.ini`，把工作区恢复到暂存区里的版本，追加的那一行消失。`git restore` 默认从暂存区取内容，所以它丢弃的是未暂存的修改；如果修改已经 `git add`，需要先 `git restore --staged` 再恢复工作区。暂存区是一个准备区，不是备份：`git add` 只把内容写入索引，索引仍然可以被下一次 `add`、`restore` 或 `reset` 覆盖，只有 `git commit` 才把快照写进提交图；实验第 6 步会把这一点演示得更直接，未提交的 `retry=9` 在 `reset --hard` 之后计数为 0。适用边界也要说清：`git diff` 适合看「还没暂存什么」，`git diff --cached` 适合看「这次提交会包含什么」，`git status --short` 适合看文件处于哪一列状态。把这三者混用，就会在提交前误判内容；先建立「三层快照」的模型，再记命令，比背选项更可靠。

### 2. branch/merge/rebase

分支是一个指向提交的引用，创建分支不会复制文件。实验在 `eb3e2ee init config` 之后切出 `feature/timeout`，在功能分支上把超时改成 500，提交为 `81bd3c1 feature: timeout 500`；回到 main 后把同一行改成 300，提交为 `9208223 hotfix: timeout 300`。两条分支都从 `eb3e2ee` 出发，改的是同一行，合并时 Git 无法自动选择。`git merge feature/timeout` 的退出码是 1，`git status --short` 显示 `UU config.ini`，文件里出现冲突标记：

```text
Auto-merging config.ini
CONFLICT (content): Merge conflict in config.ini
Automatic merge failed; fix conflicts and then commit the result.
merge exit=1
UU config.ini
--- 冲突标记 ---
<<<<<<< HEAD
timeout_ms=300
=======
timeout_ms=500
>>>>>>> feature/timeout
retry=1
feature_x=off
```

`UU` 表示两边都修改了同一个文件，尚未解决。`<<<<<<< HEAD` 到 `=======` 是当前分支（main）的内容，`=======` 到 `>>>>>>> feature/timeout` 是待合并分支的内容。实验手工把整段改成 `timeout_ms=400`，然后 `git add config.ini` 标记冲突已解决，再 `git commit`。这个提交的父提交数是 2，短哈希为 `0d91107`，说明它同时接入了 main 的 hotfix 和功能分支：

```text
合并提交父提交数=2
*   0d91107 merge feature/timeout: settle on 400
|\  
| * 81bd3c1 feature: timeout 500
* | 9208223 hotfix: timeout 300
|/  
* eb3e2ee init config
```

合并保留了两条父线，也保留了「哪次改动来自哪个分支」的拓扑。它不会把 `81bd3c1` 或 `9208223` 改写成别的哈希，只是在它们之上新增一个快照。解决冲突时的 `git add` 不是普通暂存，它告诉 Git 这个路径的冲突已经处理完；忘记这一步就提交，Git 会继续认为合并未完成。变基的处理方式不同。`git rebase main` 会把当前分支上的提交逐个重放到 main 的新基底上，提交的父提交变了，提交对象的内容也变了，因此哈希会变。实验里 `feature/shared` 已经推送，提交 `shared: a` 的短哈希是 `73c9115`；同事 `peer` 基于这个旧提交又提交了 `peer: b`。作者在 dev 里 rebase 到 main 后，同样内容的 `shared: a` 变成 `56b6136`。普通 `git push` 退出码为 1，远端拒绝 `non-fast-forward`；改用 `git push --force-with-lease` 才推上去。随后同事本地与远端分叉为 `2	3`，执行 `git pull`（merge）后，历史里 `shared: a` 出现 2 次：

```text
rebase 前 shared: a = 73c9115, 之后 = 56b6136（内容相同，哈希不同）
普通 push exit=1
 ! [rejected]        feature/shared -> feature/shared (non-fast-forward)
force-with-lease 推送完成
同事本地与远端分叉: 2	3  (左=只在本地, 右=只在远端)
同事 pull(merge) 后历史里 'shared: a' 出现次数=2
```

`--force-with-lease` 比 `--force` 多一层检查：它要求远端引用仍是你预期的位置，避免覆盖别人刚推上来的提交。但它不能消除已经发生的分叉。同事的本地历史仍有旧 `73c9115`，远端已经是新 `56b6136`，拉取合并后两条内容相同的提交都留在图里，所以计数是 2。结论是：只对尚未共享的本地分支变基；已经推送、别人基于它工作的分支，不要改写。合并与变基的取舍在这里很具体：合并提交保留协作拓扑，代价是图会有分叉；变基得到更线性的本地历史，代价是哈希变化和共享分支上的重复。实验既展示了 `0d91107` 的双父提交，也展示了 `73c9115` 到 `56b6136` 的改写，两个结果不能只用「历史更干净」来概括。

### 3. revert 与 reset

`revert` 的做法是新增一个提交，把指定提交的改动反向应用。实验先制造坏提交 `3cb1666 tune: retry 100 (bad)`，把 `retry` 改成 100；随后提交 `eb1d5ca docs: notes`，两者都已推送。此时 `git revert --no-edit 3cb1666` 生成 `a9b6faa Revert "tune: retry 100 (bad)"`，提交数从 6 变成 7，`retry` 回到 1。坏提交没有被删除，反向提交也没有抹掉它，历史里同时保留原因和补救：

```text
revert 前提交数=6 后=7
retry=1
a9b6faa Revert "tune: retry 100 (bad)"
eb1d5ca docs: notes
3cb1666 tune: retry 100 (bad)
```

```text
* a9b6faa Revert "tune: retry 100 (bad)"   <- 新增反向提交
* eb1d5ca docs: notes
* 3cb1666 tune: retry 100 (bad)             <- 坏提交仍在
```

这正是已推送分支应优先用 `revert` 的原因。它只向前追加，不改变已有提交的哈希，其他克隆拉取时不会被迫重建历史。代价是历史会多一条记录；如果后续还要重新引入同一改动，需要理解这是一次新的变更，而不是把旧提交恢复回来。`reset` 移动的是分支指针。`git reset --hard HEAD~1` 让当前分支退到上一个提交，同时把暂存区和工作区都重置到该提交。实验先提交了 `wip tmp`，短哈希 `e809840`，再执行 `reset --hard HEAD~1`：`HEAD` 回到 `a9b6faa`，`tmp.txt` 不存在；但 `git reflog` 仍记得 `e809840 commit: wip tmp`。这说明只要提交曾经进入对象数据库，本地引用日志通常还能把它找回来。另一次实验把 `retry=9` 写进工作区后直接 `reset --hard`，没有先提交，检查结果为 0，说明未提交的修改不在提交图里，不能依赖 reflog 恢复：

```text
reset 后 HEAD=a9b6faa Revert "tune: retry 100 (bad)"; tmp.txt 存在? no
reflog 仍记得: e809840 commit: wip tmp
未提交的修改被 reset --hard 丢弃后无处可找回:
  retry=9 还在? 0
```

`reset` 还有 `--soft` 和默认的 `--mixed` 形式，它们对暂存区和工作区的处理不同；本实验只覆盖 `--hard`。教学上要区分「移动引用」和「丢弃内容」：`reset --hard` 两者都做，所以它适合清理尚未共享的本地提交，不适合已经推送的公共分支。`reflog` 也是本地引用日志，不是远端备份，它不能替代提交，也不能保证在清理后仍然存在。变更追溯常和撤销一起出现。实验在第 5 步用三种方式找 `timeout_ms=400` 的来源：`git log -S'timeout_ms=400' --oneline` 默认命中 0 条；加上 `--diff-merges=first-parent` 后找到 `0d91107`；`git log -L1,1:config.ini` 的最近一条也是 `0d91107`；`git blame -L1,1` 输出 `0d911076`，作者 `lab`，时间 `2026-11-14 10:05:00 +0800`。默认 0 条不是因为没有这行，而是因为这一行是在合并提交里引入的，`git log -S` 默认不展示合并提交的 diff。这个坑在冲突解决后的排查里很常见：

```text
git log -S'timeout_ms=400' --oneline（默认不看合并提交的 diff）:
  命中 0 条
加 --diff-merges=first-parent:
0d91107 merge feature/timeout: settle on 400
diff --git a/config.ini b/config.ini
index 54ecee7..84f93ff 100644
--- a/config.ini
+++ b/config.ini
@@ -1,3 +1,3 @@
-timeout_ms=300
+timeout_ms=400
 retry=1
 feature_x=off
git log -L1,1:config.ini 最近一条:
0d91107 merge feature/timeout: settle on 400
git blame -L1,1:
0d911076 (lab 2026-11-14 10:05:00 +0800 1) timeout_ms=400
```

## 常见误区

节点写出的误区是：把未提交变更当作已备份。实验里 `retry=9` 只写在工作区，`reset --hard` 之后检查计数为 0；它没有提交对象、没有 reflog 记录，不能像 `e809840` 那样被引用日志找回。暂存区也不是备份：`git add` 之后的内容仍然可以被覆盖，只有提交才进入历史。相邻的误区有四个：把冲突标记当成内容损坏，`<<<<<<<`、`=======`、`>>>>>>>` 是 Git 留下的两个版本，手工选择后 `git add` 即可继续；把变基当成「移动原有提交」，`73c9115` 变成 `56b6136` 说明变基产生的是新提交；把 `revert` 当成「删除坏提交」，实验里坏提交 `3cb1666` 仍在，新增的是 `a9b6faa`；把 `--force-with-lease` 当成共享分支的安全通行证，它只检查远端引用位置，不能阻止同事基于旧提交产生重复。还有一个偏工具性的坑：只跑 `git log -S` 就下结论；默认命中 0 条时，先想想目标行是不是在合并提交里引入的，加上 `--diff-merges=first-parent`，或用 `-L`、`blame` 对照，才不会把「没搜到」误判成「没有来源」。

## 设计权衡

节点写出的权衡是：线性历史易读，合并提交保留协作拓扑。实验给出两组可对照的事实。合并方案里，`0d91107` 有两个父提交，`git log --graph` 能看出 `81bd3c1` 和 `9208223` 分别从哪条线来；代价是图有分叉，而且 `git log -S` 默认看不到合并提交的 diff，排查时要加参数。变基方案把功能分支接到 main 后面，提交更线性；代价是哈希变化，`73c9115` 变成 `56b6136`，共享分支上的同事拉取后出现 2 条 `shared: a`。撤销策略也有类似取舍：`revert` 保留完整因果链，适合公开历史，但会多出反向提交；`reset` 让本地历史更短、更干净，却会改写引用，并且 `--hard` 会丢弃未提交内容。选择标准不是「哪种历史更漂亮」，而是「这段历史是否已经共享、是否需要审计、其他人是否已经基于它工作」。实验里已推送的坏提交用 `revert`，本地 `wip` 用 `reset --hard`，两种做法针对的是不同边界。

## 动手实践：分支、冲突、revert 与变基风险

工作目录是 `practice142/`。执行：

```bash
cd /workspace/atlas/lessons/practice142
bash test.sh
```

脚本在临时目录建裸仓库 `origin.git` 和两个克隆 `dev`、`peer`，固定作者与时间，所以短哈希可复现。成功时打印 `ALL_CHECKS_PASSED` 并写出 `results.json`。下面的数字和哈希全部来自这次 git 2.47.3 的真实运行。

### 核对一：三个区、冲突与双父合并

新建 `config.ini` 后状态是 `?? config.ini`，`git add` 后是 `A  config.ini`，提交后状态干净，`HEAD` 里的文件有 3 行。随后只改工作区，`git diff --stat` 显示 `1 file changed, 1 insertion(+)`，`git restore config.ini` 丢弃这次修改。验收时要能说出：`??` 是未跟踪，`A` 是已进暂存区，`git diff` 比较工作区与暂存区，提交才是历史节点。main 把 `timeout_ms` 改成 300，`feature/timeout` 改成 500，合并退出码为 1，状态为 `UU config.ini`，冲突标记里 HEAD 侧是 300、功能分支侧是 500。手工取 400，`git add` 后提交，合并提交父提交数为 2，短哈希 `0d91107`；`git log --graph --oneline --all` 显示 `0d91107`、`81bd3c1`、`9208223`、`eb3e2ee` 的拓扑。这个实验同时覆盖了节点的实践要求「创建功能分支，制造并解决一次文本冲突」。

### 核对二：revert、追溯与 reset

坏提交 `3cb1666` 把 `retry` 改成 100，之后还有已推送的 `eb1d5ca docs: notes`。`git revert --no-edit 3cb1666` 后，提交数从 6 变成 7，`retry` 回到 1，新增 `a9b6faa`，坏提交仍在历史里；这里要能解释为什么不用 `reset` 改写已推送的 main，以及 `revert` 的代价是多一条反向提交。`git log -S'timeout_ms=400' --oneline` 默认命中 0 条；加 `--diff-merges=first-parent` 后找到 `0d91107`，并显示 `-timeout_ms=300` 到 `+timeout_ms=400` 的 diff。`git log -L1,1:config.ini` 的最近一条是 `0d91107`，`git blame -L1,1` 输出 `0d911076`，作者 `lab`，时间 `2026-11-14 10:05:00 +0800`；验收「用 log 找到某行引入的提交」就落在这里，关键在合并提交默认不显示 diff。`reset --hard HEAD~1` 丢掉本地 `wip tmp` 提交后，`tmp.txt` 不存在，`reflog` 仍记得 `e809840`；未提交的 `retry=9` 被同一条命令丢弃后，检查计数为 0。前者是提交，后者不是，这个对比直接对应节点误区「把未提交变更当作已备份」。

### 核对三：公开分支变基的协作风险

`feature/shared` 已推送，`shared: a` 原哈希 `73c9115`；同事 `peer` 基于它提交 `peer: b`。作者 rebase 到 main 后，同内容提交变成 `56b6136`。普通 push 退出码 1，被 `non-fast-forward` 拒绝；`--force-with-lease` 推送成功。同事本地与远端分叉为 `2	3`，`git pull`（merge）后历史里 `shared: a` 出现 2 次。`results.json` 里对应的检查 `conflict_happened`、`merge_commit_has_2_parents`、`revert_adds_commit`、`revert_restores_retry`、`log_finds_line`、`rebase_changes_hash`、`plain_push_rejected`、`peer_gets_duplicate` 均为 `true`，脚本最后打印 `ALL_CHECKS_PASSED`。

## 源码与文档阅读

按节点的 `sourceStudy`，阅读 Pro Git — Branching and Merging 中与 Git 相关的定义、示例或实现，并记录与教学实验的差异。2026-11-14 实际打开 [https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging](https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging)，返回 HTTP 200，无跳转，139702 字节，`<title>` 为 Git - Basic Branching and Merging，属 Pro Git 第 2 版 3.2 节。响应头 `Date` 为 2026-11-14 06:16:25 GMT（北京时间 14:16:25），`Last-Modified` 为 2026-11-12 04:34:47 GMT（北京时间 12:34:47）。该页用 `iss53` 和 `hotfix` 两个分支举例。先 fast-forward 合并 hotfix，示例输出写 `Updating f42c576..3a0874c Fast-forward`；再三方合并 `iss53`，输出写 `Merge made by the 'recursive' strategy.`。冲突示例写 `CONFLICT (content): Merge conflict in index.html`，并说明可以用 `git status` 查看 `Unmerged paths`、用 `git add` 标记解决、用 `git mergetool` 图形化解决、用 `git branch -d` 删除分支。这些是书中的分支与合并示例。与教学实验的差异要如实记录。本实验的 git 2.47.3 默认合并策略是 ort，而不是书中示例输出写的 recursive；这里只说书中输出写的是 recursive，不展开 ort 的实现细节。该页不涉及 rebase、revert、reset，本课用实验补上这三者，并且把冲突文件从书中的 `index.html` 换成 `config.ini`。书中示例的 `f42c576`、`3a0874c` 不是本实验的哈希，不能混用；本实验的哈希是 `eb3e2ee`、`81bd3c1`、`9208223`、`0d91107`、`3cb1666`、`eb1d5ca`、`a9b6faa`、`e809840`、`73c9115`、`56b6136`。

## 自测题

**题 1.** 工作区、暂存区、提交各保存什么？为什么「未提交变更」不能算备份？请用实验里的状态和数字说明。
<details>
<summary>答案</summary>

工作区是可直接编辑的文件，暂存区是下一次提交的候选快照，提交是对象数据库里的历史节点。实验里新建文件先显示 `?? config.ini`，`git add` 后显示 `A  config.ini`，提交后状态干净，`HEAD` 里的文件有 3 行；只改工作区时 `git diff --stat` 显示 `1 file changed, 1 insertion(+)`，`git restore` 后修改消失。暂存只是准备区，`git add` 的内容仍可被覆盖，只有提交才进入历史。未提交变更不能算备份，因为 `reset --hard` 会直接丢弃它。实验里 `retry=9` 只写在工作区，重置后检查计数为 0，既没有提交对象，也没有 `reflog` 记录。相对的，已提交的 `wip tmp` 被 `reset --hard HEAD~1` 丢掉后，`reflog` 仍记得 `e809840`。要备份就提交，或者至少推到远端；暂存和本地工作区都不是备份。

</details>

**题 2.** 复述从功能分支到合并提交的过程。为什么 `git log -S'timeout_ms=400'` 默认命中 0 条？怎样找到引入它的提交？
<details>
<summary>答案</summary>

main 的 hotfix 把 `timeout_ms` 改成 300，提交 `9208223`；`feature/timeout` 把同一行改成 500，提交 `81bd3c1`；两者都从 `eb3e2ee` 出发。合并退出码为 1，状态 `UU config.ini`，冲突标记 HEAD 侧是 300、功能分支侧是 500。手工取 400 后 `git add` 标记解决，再提交，得到父提交数为 2 的 `0d91107`，图里可以看到两条父线。默认 0 条是因为这一行是在解决冲突的合并提交里引入的，而 `git log -S` 默认不展示合并提交的 diff。加 `--diff-merges=first-parent` 后能找到 `0d91107`，并显示 `-timeout_ms=300` 到 `+timeout_ms=400`；`git log -L1,1:config.ini` 的最近一条也是 `0d91107`，`git blame -L1,1` 输出 `0d911076`。所以「没搜到」不等于「没有来源」，要针对合并提交换一种查法。

</details>

**题 3.** 已推送的坏提交该用 revert 还是 reset？公开分支变基有什么协作风险？请用实验数字说明。
<details>
<summary>答案</summary>

已推送的坏提交用 `revert`。实验里 `3cb1666` 把 `retry` 改成 100，后面还有已推送的 `eb1d5ca docs: notes`；`git revert --no-edit 3cb1666` 后提交数从 6 变成 7，`retry` 回到 1，新增 `a9b6faa`，坏提交仍在。这样不会改写别人已经拉取的历史。`reset --hard` 会移动分支指针并丢弃内容；实验里它丢掉了本地 `wip tmp`，`reflog` 还能找到 `e809840`，但未提交的 `retry=9` 计数为 0，找不回。所以本地未共享的提交可以 reset，公开历史应 revert。公开分支变基的风险来自哈希改写。`feature/shared` 已推送，`shared: a` 从 `73c9115` 变成 `56b6136`，内容相同但哈希不同；普通 push 退出码 1，被 `non-fast-forward` 拒绝，`--force-with-lease` 才能推上去。同事 `peer` 已基于旧提交工作，本地与远端分叉为 `2	3`，`git pull`（merge）后历史里 `shared: a` 出现 2 次。结论是只对尚未共享的本地分支变基；已经推送、别人基于它工作的分支，应保留原历史，用合并和 revert 处理。

</details>

## 推荐资源

- [Pro Git — Branching and Merging](https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging)（Pro Git，英文，入门，免费）：节点给出的唯一推荐资源。2026-11-14 用 curl 核对，返回 HTTP 200，无跳转，139702 字节，`<title>` 为 Git - Basic Branching and Merging，属 Pro Git 第 2 版 3.2 节；响应头 `Date` 为 2026-11-14 06:16:25 GMT（北京时间 14:16:25），`Last-Modified` 为 2026-11-12 04:34:47 GMT（北京时间 12:34:47）。该页用 `iss53` 和 `hotfix` 讲 fast-forward、三方合并、冲突标记、`git status` 的 `Unmerged paths`、`git add` 和 `git mergetool`，适合对照本课的冲突与合并提交。需要记住：该页不涉及 rebase、revert、reset，这三者由本课实验补充；书中的 `f42c576`、`3a0874c` 和 `index.html` 是书中示例，不是本实验的哈希和文件。

## 本课小结

- 工作区是编辑区，暂存区是下一次提交的候选快照，提交才是历史节点。实验里 `?? config.ini` 经 `git add` 变成 `A  config.ini`，提交后干净，`HEAD` 有 3 行；只改工作区时 `git diff` 显示 1 行插入，`git restore` 丢弃它。
- 分支是指向提交的引用。main 的 `9208223` 与 `feature/timeout` 的 `81bd3c1` 修改同一行，合并退出码为 1，状态 `UU`，手工取 400 后得到父提交数为 2 的 `0d91107`；合并保留两条父线，变基则会把 `73c9115` 改写成 `56b6136`。
- `revert` 追加反向提交，适合已推送历史：坏提交 `3cb1666` 之后有 `eb1d5ca`，revert 后提交数从 6 变成 7，`retry` 回到 1，新增 `a9b6faa`。`reset --hard` 适合本地清理：它丢掉 `wip tmp` 后 `reflog` 仍记得 `e809840`，但未提交的 `retry=9` 计数为 0。
- 公开分支变基会让普通 push 被 `non-fast-forward` 拒绝，`--force-with-lease` 只能完成推送，不能消除同事的分叉；实验里同事本地与远端分叉 `2	3`，拉取合并后 `shared: a` 出现 2 次。只对尚未共享的本地分支变基。
- 找某行来源时，`git log -S'timeout_ms=400'` 默认命中 0 条，因为该行在合并提交里引入；加 `--diff-merges=first-parent` 找到 `0d91107`，`git log -L1,1:config.ini` 和 `git blame -L1,1` 也指向它。全部实验数字来自 `practice142/run.log` 的 git 2.47.3 运行，不是生产仓库数据。
