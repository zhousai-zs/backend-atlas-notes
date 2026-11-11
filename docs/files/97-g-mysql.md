# 第 97/208 课：MySQL访问与连接管理

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-mysql` |
| 英文名 | MySQL |
| 领域 / 阶段 | G / 第 2 阶段 |
| 子类 | 关系数据库 |
| 难度 | 基础 |
| 前置节点 | `a-db`、`d-pool` |
| 标签 | `MySQL`、`G`、`Systems` |
| 参考资料 | [MySQL 8.4 · SQL Prepared Statements](https://dev.mysql.com/doc/refman/8.4/en/sql-prepared-statements.html)（dev.mysql.com，文档） |
| 摘要 | MySQL服务端与客户端通过会话交互，连接、事务和字符集设置影响正确性及资源占用。 |

> **边界说明**：本课中出现的「内容后台」「用户服务」「编辑用户简介」等场景都是**教学假设**，用来让概念落地，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库加 sqlite3 写的教学模型，用来暴露会话、连接池、重连与绑定参数的语义；它不是 MySQL 服务器，本课出现的耗时数字只来自这次教学实验，不能当成生产 MySQL 的性能数据。MySQL 8.4 的具体行为以官方文档为准（本文已核对照的文档，访问日期 2026-11-11）。

## 导读

### 一个直觉：银行柜台窗口

想象一家银行，厅里有三个柜台窗口。你办业务时，窗口前的柜员会打开你的卷宗、改一笔账、切到某种表格语言、再请你签字提交。窗口和你之间形成的是一次**会话**：它记得「这笔账还没提交」「表格正在用哪种语言」，而不是一个忘了刚才发生过什么的无状态窗口。

现在把这三个窗口做成一个「窗口池」——办完就还回去，下一个人接着用，省得每次重新签到。这套做法会碰到四种麻烦：

- 有人递进来一张写成「姓名 = 张三' 或者 1=1」的查询单。如果柜员把这张纸条直接拼进指令，窗口会把全行客户的卷宗都翻出来（**SQL 注入**）；
- 柜员改完账还没请你签字，突然电话断了。再拨通时已经是另一个柜员、另一本空白卷宗，原来那笔未提交的账已经作废。如果系统还显示「继续刚才那笔业务，提交成功」，你以为改完了，库里其实没变（**静默重连、假成功**）；
- 上一个人把窗口的表格语言切成了另一种，还留下一笔没提交的「把普通客户升级成管理员」，人走了却不收拾。你一签字，别人的升级就被你提交了（**归还前不清理会话**）；
- 三个窗口都被人坐着办理「很长的一笔账」不走，第四个人只能在外面一直等（**长事务占满池**）。

MySQL 的客户端和连接池，做的就是这家银行的窗口管理。

### 接上前置

- **a-db**：SQL、事务、提交和回滚——这是本课「会话里那笔还没签字的账」的基础；
- **d-pool**：对象池的借还、容量上限、借不到就等待或失败——这是本课连接池的骨架。

前面单独看都学过：数据库能记账，池能复用资源。本课把它们组装成「带会话状态的 MySQL 连接」，然后看组装之后才出现的问题：连接不再是一根干净的管子，它上面还挂着事务、字符集、预处理语句和超时；复用它时，这些状态会跟着走。

### 一次查询与一次事务的生命周期

```
 调用方                          连接池                         会话 / 服务端
 ──────                          ────                          ────────────
 store.get_user_by_name(n)
        ──checkout──────────▶ ① 取出一条空闲连接
                              ② 若连接已死（wait_timeout / 断连）
                                 则换成一条全新会话
        ③ 绑定参数，执行
           SELECT ... WHERE name = ?  ──▶  ④ 在这条会话里执行
                                            （charset / autocommit / 事务
                                             / 预处理语句都属于这条会话）
        ◀─ 行集合 ──────────────────────── ⑤ 返回结果集（要读完）
        ──checkin───────────▶ ⑥ 归还前复位：ROLLBACK 残留事务，
                                 charset 回到默认，autocommit=1
                                 （语义上接近 mysql_reset_connection）
```

正常路径只有这几步。本课真正要讲的是：第 ② 步发现死连接之后能不能假装还在原来的事务里；第 ⑥ 步如果省略，下一位调用方会继承哪些脏状态。

### 教学场景（教学假设）

内容后台的用户服务用一个很小的连接池做用户查询和更新。灰度期间出现了三件「接口都返回成功」的事：

1. 有人用名字查用户，却拿到了全部 5 个用户——查询条件是把用户输入直接拼进 SQL；
2. 编辑 alice 的简介时，事务中途连接被服务端断开。客户端静默重连后报告「提交成功」，但 alice 的 `bio` 仍是 `hello`；
3. 一个请求把会话切到 `latin1`，并在未提交的事务里把 bob 的 `role` 改成了 `admin`，然后把连接还回池。下一个请求随手 `COMMIT`，bob 真的变成了管理员。

实践里这三件事都复现了，而且全程可以没有语法错误。解析成功、提交返回成功，都不等于业务是对的。

### 本课三条主线

1. **预处理语句与参数**：用占位符绑定用户输入，语句结构不再被输入改写；
2. **连接池和会话状态**：连接是有状态的会话，借还和重连都要按「新会话 / 复位会话」来处理；
3. **字符集、超时与结果集**：`SET NAMES`、`wait_timeout` 和未读完的结果，都会让下一次使用这条连接时偏离预期。

三条主线对应三个很容易分开学、合在一起才出问题的点。只会写 `PREPARE` 但不处理池，脏会话仍会漏出去；只会建池但不理解重连，事务会在沉默中消失；只记得 `SET NAMES` 却不处理 `wait_timeout`，空闲连接会在池里变成另一条会话。下面按这个顺序展开。



## 学习目标

学完本课，你应该能够：

1. **能解释预处理语句与参数**（goals 原文）；
2. **能独立完成：实现带连接池的用户查询与更新，覆盖断连重连和SQL注入输入。**（goals 原文）；
3. 说清为什么 `' OR '1'='1` 能让拼接 SQL 返回全部用户，而绑定参数后它只是一个普通字符串；
4. 画出连接从 checkout 到 checkin 的生命周期，并指出归还前必须复位哪些会话状态；
5. 解释「重连是新会话」：原事务已被回滚，预处理语句、字符集和会话变量都不再属于你，不能静默续接；
6. 用一次回滚演练证明：池被长事务占满时，新的 checkout 会在超时后失败。

## 为什么后端要懂

节点的 why 原文是：「正确使用关系存储支持后台业务。」拆开看，这里的「正确」至少有两层。

**正确性。** 后台业务几乎都要读写用户、内容、权限这类关系数据。如果查询把用户输入拼进 SQL，一条恶意名字就能读走整张表；如果编辑简介的事务在断线后被客户端当成「还在原来那笔事务里」，调用方会以为提交成功，数据却丢了；如果连接还回池时还带着别人的未提交更新和错误字符集，下一个完全无关的请求就会提交一次提权，或者把多字节字符写坏。这些都不是「SQL 语法没过」，而是会话被用错了。

**资源占用。** 建立 MySQL 连接有握手、认证、会话初始化的成本，所以生产上普遍用连接池复用连接。但池里的每一条连接同时只能被一个请求占用：长事务、忘了归还、或者结果集没有读完，都会把池占满，让后面的请求排队或失败。超时（如 `wait_timeout`）还会在服务端悄悄丢掉空闲会话，池里那条连接其实已经死了。


把「单独测都对、组合起来才错」的情况列成表，入职后可以当排查清单用：

| 失败情况 | 谁先发现 | 若处理不当会怎样 | 本次实践中的对照 |
|---|---|---|---|
| 用户输入被拼进 SQL | 往往不是报错，而是多返回了行 | 越权读取或被改写 WHERE | naive 注入返回 5 行，错误数为 0 |
| 事务中途断连 | 下一次读写或 `COMMIT` | 假成功，或继续用已失效的预处理语句 | naive：`COMMIT` 成功但 bio 仍是 hello |
| 归还时留下事务 / charset | 下一个借用者的 `COMMIT` 或写入 | 错提交别人的更新、用错字符集 | naive：bob 被提成 admin |
| 空闲连接被 `wait_timeout` 踢掉 | 下一次 checkout 或第一条 SQL | 当成原会话继续用，脏设置被「继承」到新会话 | naive：重连后仍是 latin1，且 kept_id=True |
| 长事务不归还 | 其他请求的 checkout | 池被占满，接口超时 | 3 个占用者让第 4 次等待 200.175 ms 后失败 |
| 结果集未读完 | 同一条连接上的下一条语句 | 连接无法复用，或结果错位 | 本次未单独对照，见扩展练习 1 |

入职后你多半直接用公司现成的客户端和连接池，但出问题时要知道往哪查：

- 偶发「查一个用户却返回很多行」，先查是不是把输入拼进了 SQL；
- 「提交成功但数据没变」，先查中间有没有断连和静默重连；
- 「这个请求改了另一个请求的数据」或「字符集突然不对」，先查连接归还前有没有复位；
- 「突然大量获取连接超时」，先查是不是有长事务或泄漏的借用一直占着池。

## 核心知识点

### 1. 预处理语句与参数

MySQL 8.4 支持服务端预处理语句。文档给出的两个直接好处是：

- 同类语句反复执行时，减少每次都要重新解析的开销；
- **防止 SQL 注入**：参数里可以包含未转义的引号和分隔符，它们不再改变语句结构。

SQL 接口由三条语句组成（访问日期 2026-11-11，[MySQL 8.4 · SQL Prepared Statements](https://dev.mysql.com/doc/refman/8.4/en/sql-prepared-statements.html)）：

```sql
PREPARE stmt1 FROM 'SELECT SQRT(POW(?,2) + POW(?,2)) AS hypotenuse';
SET @a = 3;
SET @b = 4;
EXECUTE stmt1 USING @a, @b;
DEALLOCATE PREPARE stmt1;
```

`?` 是占位符。应用里更常见的是客户端 API 的绑定参数（走高效的 client/server binary protocol），语义一样：先确定语句结构，再单独传参数值。教学实践里，safe 路径的查询写成：

```python
conn.execute_bound(
    "SELECT id, name, role, bio FROM users WHERE name = ?",
    (name,),
)
```

naive 路径则是字符串拼接：

```python
conn.execute_concat(
    "SELECT id, name, role, bio FROM users WHERE name = '%s'",
    name,
)
```

当 `name` 是 `' OR '1'='1` 时，拼接后的语句变成：

```sql
SELECT id, name, role, bio FROM users WHERE name = '' OR '1'='1'
```

`OR '1'='1` 恒为真，WHERE 失去过滤作用，整张表都被选出来。实践中 naive 路径返回了 **5** 行：`alice,bob,cindy,dave,admin`。绑定参数的路径里，SQL 文本始终是 `WHERE name = ?`，参数是字面量 `' OR '1'='1`，库里没有这个名字，结果是 **0** 行。用正常名字 `alice` 查询时，两条路径都准确返回 **1** 行——所以 naive 不是「完全不能用」，它只是在特殊输入上悄悄改写了语义。

预处理语句属于**创建它的 session**。文档写明：如果会话结束时还没有 `DEALLOCATE PREPARE`，服务端会自动释放。它对该 session 是全局的：在存储过程里创建的语句，过程结束时不会自动释放。系统变量 `max_prepared_stmt_count` 可以限制同时存在的预处理语句，设为 0 等于禁止使用。另外，SQL 语法的预处理不支持在一条字符串里用分号写多条语句，也不能嵌套 `PREPARE`。这些边界以 8.4 文档为准，不要想当然。


再看一遍结构是怎么被改写的。naive 的模板是：

```sql
SELECT id, name, role, bio FROM users WHERE name = '<这里填入输入>'
```

输入里的单引号提前结束了字符串字面量，后面的 `OR '1'='1` 被解析成 SQL 布尔表达式。绑定参数不会走这一步：解析器先看到完整的 `WHERE name = ?`，再把参数当作值，而不是当作源代码。所以参数里即使有引号、分号、注释符，也不会多出新的子句。

有人会问：那我把输入里的引号换成 `''`，是不是就安全了？转义能挡住一部分直接断弦的注入，但不是完整答案。转义规则依赖当前字符集（这正好连到第三条主线）、也容易在「拼了两次」「漏掉某一处」「改用了另一种字面量写法」时失效。预处理语句把「语句」和「值」分成两条通道，从机制上就不再依赖每一次手工转义都写对。8.4 文档把「参数可以包含未转义的引号和分隔符」写成预处理语句的好处之一，依据就在这里。

教学代码里还有一个显式证据，不只依赖「结果行数看起来对」：

```python
c.execute_bound("SELECT id, name FROM users WHERE name = ?", (payload,))
assert payload not in c.last_sql
assert c.last_params == (payload,)
```

本次运行满足：`last_sql` 为 `SELECT id, name FROM users WHERE name = ?`，`payload_in_sql=False`，`bind_delta=1`。如果有人后来又在 safe 路径里偷偷改回拼接，这一组断言会先失败。

关于两种接口。文档区分了「应用里通过 C API / 连接器走 binary protocol」和「在 SQL 层写 `PREPARE`/`EXECUTE`/`DEALLOCATE PREPARE`」。后者不需要编程接口、适合在 mysql 客户端里试语义、排查和写复现用例，但效率不如前者。两者都是服务端预处理，都按 session 管理生命周期。教学实现对外暴露的是 `execute_bound(sql, params)`，内部也可以用 `PREPARE stmt FROM ...` 再 `EXECUTE`，语义对齐的是「先固定结构、再传值」，并不声称自己实现了 MySQL 的 binary protocol。

这也是为什么重连和连接池复位必须处理预处理语句：新会话里旧的语句名已经不存在了。实践中，naive 在静默重连后仍拿着客户端缓存的 `get_alice` 去 `EXECUTE`，结果「还能跑」——它把旧语句文本在新会话上悄悄用了一次，把「服务端已释放」这件事掩盖了。safe 路径重连后旧句柄失效，再次执行得到 `PreparedGone`。

### 2. 连接池和会话状态

一条 MySQL 连接不是无状态的套接字。服务端为它保留一次会话，会话上至少可能包括：

- 当前事务（是否已 `BEGIN`、尚未 `COMMIT`/`ROLLBACK`）；
- `autocommit`；
- 字符集相关的会话变量（例如 `SET NAMES` 会隐式改到的那些）；
- 用户变量、预处理语句、临时表、表锁、`GET_LOCK()`、`LAST_INSERT_ID()` 等。

连接池复用的是这样一条会话，而不只是一个 TCP 连接。所以「借还」必须带上状态协议：

```
checkout：
  从空闲队列取出一条连接
  若已死（服务端已踢掉 / 本进程已 close）→ 换成全新会话，而不是继续用
  交给调用方独占

调用方使用期间：
  这条连接上的事务、charset、预处理语句都属于当前请求

checkin：
  若连接已死 → 丢弃，补一条新的进池
  若仍活着 → 复位后再入队
```

复位的语义，应对齐 MySQL 8.4 C API 的 `mysql_reset_connection()`（访问日期 2026-11-11）：在**不断开连接、不做重新认证**的前提下清理会话。文档列出的效果包括：回滚任何活动事务并复位 autocommit；释放表锁；关闭并删除 `TEMPORARY` 表；将会话系统变量重新初始化为对应的全局值（包括 `SET NAMES` 隐式设置的变量）；丢失用户变量；释放预处理语句；关闭 `HANDLER`；将 `LAST_INSERT_ID()` 置 0；释放 `GET_LOCK()` 锁。教学实现做了其中最容易测错的几项：回滚残留事务、charset 回到 `utf8mb4`、`autocommit` 回到 True、清空预处理语句登记。

实践里，借用者 A 执行 `SET NAMES latin1`，再 `BEGIN` 后把 bob 的 `role` 改成 `admin`，不提交就归还。下一位借用者 B：

- **naive**：B 拿到的就是这条脏会话，`charset=latin1`，`in_tx=True`。B 随手 `COMMIT`，bob 的 `role` 变成了 `admin`（`tx_leaked=True`）；
- **safe**：归还时已经 `ROLLBACK` 并把 charset 复位为 `utf8mb4`。B 看到的是干净会话，`in_tx=False`，bob 仍是 `user`。


用一次调用把状态变化画全。假设 checkout 得到 `conn_id=21` 的会话，随后：

```text
BEGIN                              in_tx=True, autocommit=False
UPDATE users SET bio=? WHERE name=?   （参数: 'in-tx-change', 'alice'）
           ┌─ 此时服务端踢掉会话 / 进程调用 kill()
           │    未提交的 UPDATE 被回滚
           │    服务端预处理语句释放
           │    这条会话的 thread_id 不再有效
           └─ 客户端如果没被通知，仍可能以为 in_tx=True
```

safe 的下一步是承认「21 号会话没了」，打开 22 号会话，并把 `TransactionAborted(old=21, new=22, was_in_tx=True)` 抛给调用方。naive 的下一步是打开一条新的服务端会话，却把客户端的 `in_tx`、charset、conn_id、预处理缓存原样留着，然后对一条并没有活动事务的新会话执行 `COMMIT`——在 MySQL 里，没有活动事务时 `COMMIT` 仍是成功的空操作。这就是假成功的机制，不是「COMMIT 实现写错了」。

教学池的关键片段如下（缩写，完整代码在 practice97 里）：

```python
class SafePool:
    def checkin(self, conn):
        if not conn.alive or conn.is_idle_timed_out():
            conn.kill()
            self._idle.put(self._new())  # 补一条全新会话
            return
        conn.reset_like_mysql_reset_connection()
        self._idle.put(conn)

    def checkout(self, timeout=None):
        conn = self._idle.get(timeout=timeout)  # 满了就 PoolTimeout
        if not conn.alive or conn.is_idle_timed_out():
            conn.kill()
            conn = self._new()
        return conn

class NaivePool:
    def checkin(self, conn):
        self._idle.put(conn)          # 原样还回去

    def checkout(self, timeout=None):
        conn = self._idle.get(timeout=timeout)
        if not conn.alive or conn.is_idle_timed_out():
            conn.silent_reconnect()   # 新会话，旧客户端状态
        return conn
```

`autocommit` 也值得单独说一句。MySQL 默认自动提交：一条非事务型语句结束，效果立刻持久化。`BEGIN`/`START TRANSACTION` 之后进入显式事务，必须 `COMMIT` 或 `ROLLBACK`（或会话结束时回滚）。连接池里最危险的组合是：A 关闭了 autocommit、开了事务、改了数据、把连接还回去；B 以为自己在 autocommit 下做了一次只读查询，最后却执行了一次 `COMMIT`，把 A 的修改持久化了。实验 3 就是这个组合的最小版本。

归还时为什么选择「复位但不断开」？因为断开再连要重新握手、重新认证，等于把池的主要收益吐回去。`mysql_reset_connection()` 的定位正是：在同一条物理连接上把会话擦干净。教学实现用同一条 sqlite 连接做 `ROLLBACK` 并清字段，来逼近这一语义；它没有重新做 MySQL 协议上的认证，这是刻意的简化。

注意：脏状态泄漏的是「下一次借用到**同一条物理连接**的人」。池里如果还有别的空闲连接，不一定是下一个请求立刻中招，但这只是时机问题，不是安全保证。

**重连是新会话，不是原会话的续集。** 8.4 文档 *Automatic Reconnection Control* 写得很清楚（访问日期 2026-11-11）：客户端库可以在发现连接断开时自动重连并再发一次语句，但自动重连已经 **deprecated**，`MYSQL_OPT_RECONNECT` 仍可用却会告警，未来会移除，而且默认是关闭的。若发生自动重连，应用**不会收到明确通知**——可以用重连前后 `mysql_thread_id()` 是否变化来检测。重连会在服务端重置连接相关状态，列表与 `mysql_reset_connection()` 高度相同，其中第一条就是：**回滚任何活动事务并复位 autocommit**，同时丢失会话变量、用户变量和预处理语句。

所以官方 pitfalls 不是吓唬人，而是文档里的行为：

> 重连可能丢失事务和会话设置，不能静默续接原事务。

实践里把这件事做成了对照。事务中把 alice 的 `bio` 改成 `in-tx-change`（不提交），然后模拟服务端踢掉会话：

- **naive** 静默重连：客户端仍然认为 `in_tx=True`，接着 `COMMIT` 并报告成功；但新会话上这句 `COMMIT` 只是空提交，alice 的 `bio` 仍是 `hello`（`false_success=True`）。客户端缓存的预处理语句也能继续执行，好像什么都没丢；
- **safe** 把重连当作新会话：抛出 `TransactionAborted`，`in_tx=False`，会话 id 从 **21 变成 22**，charset 回到 `utf8mb4`，旧预处理句柄再执行会得到 `PreparedGone`。alice 的 `bio` 同样仍是 `hello`，但这一次是**符合预期的回滚**，调用方知道必须重新 `BEGIN` 并重做全部语句。

两种路径数据都没改上去，差别在于：naive 告诉你「提交成功」，safe 告诉你「原事务已经没了」。排障时，假成功比明确失败危险得多。

### 3. 字符集、超时与结果集

**字符集。** `SET NAMES charset_name` 会改动当前会话上与客户端、连接、结果相关的字符集变量（具体变量集合和默认值以 8.4 文档为准，这里不编造未核过的默认值）。错误的字符集不只是「日志看起来怪」：它可能造成写入截断、乱码、或同一列在不同连接上的比较结果不一致。连接池把这个问题放大了——A 改了 charset 却不还原，B 在不知情的情况下用错误的字符集读写。实践实验 3 已经用 `latin1` 泄漏给出了可观察的对照；实验 5 里，naive 在 `wait_timeout` 之后静默重连，**仍然把 `latin1` 带到了新会话**，这是「会话设置丢失却被客户端伪装成还在」的另一个侧面。

**超时。** 服务端会按会话上的 `wait_timeout`（交互式连接则是 `interactive_timeout`，以文档为准）踢掉空闲连接。连接池里一条看起来还在队列里的连接，其实可能已经被服务端关掉了。下一次使用必须按死连接处理：丢掉旧会话，建立新会话，并认为旧事务和旧设置都已不在。实践把全局 `wait_timeout` 设为 **0.12s**，连接归还后空闲 **0.25s**：

- **safe** 再 checkout 时发现会话已死，换成新会话：id 从 **1 变成 2**，charset 是默认的 `utf8mb4`；
- **naive** 静默重连：保留原来的客户端会话 id，charset 仍是归还前留下的 `latin1`。

**结果集。** 一条连接在结果还没读完（或多结果集还没消耗完）时，不能安全地开始下一条语句，更不能还回池。教学实现把结果一次性 `fetchall()`，是为了让主线实验聚焦在参数绑定、事务和 charset 上；生产客户端里，未读完的结果集、存储过程多结果集，都是归还前必须清掉的状态。文档还提到，SQL 预处理接口本身不支持在一条字符串里发送多条语句，应用如果打开了多语句，更要小心结果集边界。


对内容后台这种含中文的业务（教学假设），字符集出错的典型路径是：某次调试或某条历史代码执行了 `SET NAMES latin1`，连接被还回池后一直没被复位。后续请求用这条连接写入文章标题或用户简介时，多字节字符可能按错误的字符集编码、截断或在比较时对不上。教学实验没有模拟真实的编码转换（sqlite3 层按 Unicode 文本处理），它只把 `charset` 当作会话上必须复位的状态来测量泄漏。这一点要说清楚：实验 3 和实验 5 证明的是「状态会漏」，不是「latin1 在 sqlite 里真的截断了中文」。真实的截断和乱码，要在 MySQL 8.4 里用同样的 `SET NAMES` 自己验证。

超时和池的交互可以按时间线来看（本次实验的设置是 `wait_timeout=0.12s`，空闲 `0.25s`）：

```text
t=0.00  checkout，SET NAMES latin1，checkin
        safe：charset 被复位为 utf8mb4，wait_timeout 回到全局默认 0.12s
        naive：charset 仍是 latin1，wait_timeout 仍是 0.12s
t=0.12  服务端认为这条空闲会话已经到期，踢掉它
t=0.25  下一次 checkout
        safe：发现已死，新建会话（id 1→2），charset=utf8mb4
        naive：静默重连，保留客户端 id 和 latin1
```

生产上默认的 `wait_timeout` 通常远大于 0.12 秒（具体数值以 8.4 文档和你的实例配置为准，这里不写死一个未在本次实验里读到的默认值）。把它缩到 0.12 秒是为了让实验在一秒内跑完。机制不变：池里的空闲连接随时可能已经被服务端关掉，checkout 必须会处理死连接。

结果集为什么单独列出来？因为「语句执行完了」和「连接可以借给别人了」不是同一件事。典型的坑包括：

- 只取了第一行就走，剩下的行还在这条连接的协议缓冲区里，下一条语句被拒绝，或者更糟，和下一条语句的结果混在一起；
- 存储过程或多语句一次返回多个结果集，只读了第一个；
- 出错时跳过了读完或 `close` 结果集的路径，连接带着未完成的状态回到池里。

教学代码在 `execute()` 里对有描述符的游标一次性 `fetchall()`，是为了让前两条主线的实验稳定可重复。它把「结果集必须被消费」这个规则写进了实现，但没有单独做一组「故意不读完」的对照。扩展练习第 1 条就是留给你补的。

会话还有一些和结果、身份相关的值，例如 `LAST_INSERT_ID()`。自动重连和 `mysql_reset_connection()` 都会把它置 0。如果调用方在插入一行之后、读取插入 id 之前发生了复位或重连，读到的就不再是刚才那一行。教学实验没有单独测这一项，但归还和重连的原则是一样的：不要假设「这条连接上一次调用留下的会话值还在」。

和长度、报文相关的上限（例如 `max_allowed_packet`）同样属于「不可信输入要设上限」的意识，具体默认值以你使用的 8.4 文档为准，不要背二手数字。

### 三条主线的收束

- 预处理语句与参数，解决「用户输入会不会改写语句结构」；
- 连接池和会话状态，解决「这条连接现在属于谁、上面还挂着什么、还回去时怎么擦干净、断了之后算不算同一笔事务」；
- 字符集、超时与结果集，解决「会话的默认环境被谁改过、空闲会不会被踢、结果有没有消费完」。

## 常见误区

> 重连可能丢失事务和会话设置，不能静默续接原事务。

1. **把用户输入拼进 SQL。** `' OR '1'='1` 不是「奇怪的用户名」，它改写了 WHERE。实践中 naive 一次查询返回 5 行全表，safe 用 `WHERE name = ?` 绑定同一段输入，结果是 0 行；正常查询 `alice` 时两种路径都是 1 行。看起来「功能测过了」，只是没测恶意输入。
2. **断线后静默重连，并继续 COMMIT 原来的事务。** 服务端已经把旧事务回滚了。naive 的客户端仍把 `in_tx` 设为 True，`COMMIT` 报告成功，alice 的 `bio` 却还是 `hello`（假成功）。safe 抛出 `TransactionAborted`，会话 id 21→22，要求调用方重做整笔事务。
3. **以为预处理语句在重连后还在。** 文档写明预处理语句属于 session，会话结束即释放。naive 用客户端缓存把 `get_alice` 在新会话上重新用起来，`prepared_ok=True`，把丢失掩盖了；safe 再执行得到 `PreparedGone`。
4. **连接还回池时不清理事务和 charset。** 实践中 naive 的下一位借用者看到 `latin1` 和 `in_tx=True`，一次 `COMMIT` 就把 bob 提成了 `admin`。safe 归还前复位，bob 仍是 `user`。
5. **把连接池当成无状态对象池。** 复用的是会话。checkout 拿到的连接可能带着上次的事务、变量、预处理语句，也可能其实已经因 `wait_timeout` 死了。
6. **忽略空闲超时。** 实践中连接在池里只坐了 0.25s，短于或长于 `wait_timeout` 决定了它还是不是原来那条会话。naive 在超时后仍保留脏 charset，等于把「已经是新会话」这件事瞒过去了。


这些误区有一个共同结构：客户端和调用方看到的「成功」，和服务器会话里实际还在的状态，已经不是同一回事。注入时，语句在服务器上的结构已经变了，客户端却还以为自己在按名字查一个人；假成功提交时，服务器上的事务早就没了，客户端却还握着 `in_tx=True`；脏连接归还时，服务器上那笔未提交的更新还在，下一个调用方却以为自己从一张干净的桌子开始。排查时不要只看错误率——这三组对照里，naive 路径都可以做到「没有抛异常」。

再补一个和重连相关的操作层面提醒。8.4 文档说，可以用重连前后的 `mysql_thread_id()` 是否变化来检测自动重连。如果你必须在遗留代码里面对仍开启着的自动重连，至少要在关键事务前后核对这个 id，发现变化就按「整笔事务失败」处理，而不是继续 `COMMIT`。长期做法仍是关掉自动重连（它已 deprecated），在应用层显式处理断开、建立新会话、按幂等规则决定是否重试。

## 设计权衡

> 连接池减少握手，也需防止长事务占满池。

建立一条 MySQL 连接要做网络握手、认证和会话初始化，比在已经鉴权的会话上再跑一条 SQL 贵得多。连接池用「先建好、借还复用」把握手成本摊掉，这是它存在的理由。

代价是：池的容量是硬上限。每一条被借走、却迟迟不还的连接，都是一个不能再给别人用的窗口。最常见的占满原因就是**长事务**——`BEGIN` 之后做很多业务计算、调别人的接口、甚至等用户，连接一直被握在手里。实践里池大小为 **3**，三个借用者都 `BEGIN` 后不归还，第四次 `checkout` 在 **200.175 ms** 后超时失败；三个都归还之后，再 checkout 只需 **0.006 ms** 就拿到了连接。数字来自教学池的 200ms 等待上限，用来证明「占满是可观察的」，不是 MySQL 服务端自己的排队延迟。

工程上常见的制衡手段（具体名称以你用的客户端和框架为准）包括：

- 限制池大小与等待时间，让占满尽快变成明确错误，而不是无限排队；
- 限制单次借用时间和连接最长存活时间，避免一条连接无限期被占，也避免把脏会话留得太久；
- 归还时做复位（`mysql_reset_connection()` 这类路径）。复位有 CPU 和往返成本，但远小于一次脏会话造成的错账；
- 把「打开事务」的临界区缩到真正需要原子写的那几条 SQL，而不是包住整个请求。

池不是越大越好：池大会增加服务端会话占用（内存、文件描述符、并发事务）。先设上限，再用占满率和等待时间来调，而不是先开一个「应该够用」的很大的池。

把「减少握手」说得更具体一点。每次新建会话，服务端都要分配连接线程（或等价的会话对象）、做认证、初始化一组会话变量。如果每个 HTTP 请求都新建并关掉一条 MySQL 连接，数据库的连接建立速率会先于 SQL 本身成为瓶颈，TIME_WAIT 和认证日志也会跟着涨。池把「建连」从请求路径里拿出去，请求路径上只剩 checkout（通常是取一条已存在的会话）和 SQL。

但这笔收益有一个隐形前提：调用方占用连接的时间必须短。占用时间 = 事务里所有 SQL 的执行时间 + 你在握着连接时做的任何非 SQL 工作。如果在事务中间调用下游 RPC、读远程文件、甚至等锁，连接就从「在工作」变成了「被拿来当会话级的锁」。池大小是 3 的时候，3 个这样的请求就能让整个用户服务不再能访问数据库——实验 4 用 200ms 的等待上限把这件事变成了明确的 `PoolTimeout`。如果把等待改成无限期，对外表现就会变成「接口偶尔卡死」，更难定位。

所以调池的顺序通常是：先缩短占用（事务尽量短、不要在持连时做远程调用），再考虑要不要加大池。只加大池，会把问题从「应用获取连接超时」变成「数据库会话和并发事务变多」，服务端先撑不住。

复位成本也可以量化地理解，不必编造生产数字：实验 3 的 safe 路径在归还时多做了一次 `ROLLBACK` 和若干字段重置，但避免了一次错误的 `admin` 提升。一次复位的代价是有限的本地操作；一次脏提交的代价是数据修回、审计和可能的安全事故。权衡几乎总是偏向归还前复位。



## 动手实践：实现带连接池的用户查询与更新

**要求：** 实现带连接池的用户查询与更新，覆盖断连重连和 SQL 注入输入。查询走绑定参数；连接归还前事务和会话状态可控。

环境是 Python 3 标准库加 sqlite3，用教学会话模型模拟 MySQL 的会话、预处理语句、连接池和重连语义。不要把下面的毫秒数当成生产 MySQL 的基准。

```bash
tar xzf practice97.tar.gz && cd practice97
./test.sh        # 约 1 秒，最后一行是 ALL_CHECKS_PASSED
```

本次运行的完整输出见实验表。本次 `./test.sh` 以 `ALL_CHECKS_PASSED` 结束，退出码 0。

### 0. 文件结构

```
practice97/
  engine.py        # 教学会话：绑定/拼接、事务、charset、预处理、断连与重连
  pool.py          # SafePool / NaivePool
  app.py           # UserStore：get_user_by_name / update_user_bio
  test_mysql.py    # 5 组对照实验，并写入 results.json
  test.sh          # 启动入口
  results.json     # 本次运行的数字
```

教学表 `users` 预置 5 行（教学假设）：

| id | name  | role  | bio         |
|---:|-------|-------|-------------|
| 1  | alice | user  | hello       |
| 2  | bob   | user  | hobby:chess |
| 3  | cindy | user  | hobby:music |
| 4  | dave  | user  | hobby:coding|
| 5  | admin | admin | root bio    |

### 步骤 1：两条执行路径（engine.py / app.py）

safe 的查询和更新必须走绑定参数，SQL 文本里是 `?`，用户输入只出现在参数里。naive 用 `'%s' % name` 直接拼接。`UserStore.get_user_by_name()` 按模式选择其中一条路径。验收时除了看注入结果，还断言：safe 最近一次 SQL 不含 payload 原文，`params` 等于 `["' OR '1'='1"]`，绑定次数加 1。


`UserStore` 本身很短，差别全在「参数怎么进 SQL」：

```python
class UserStore:
    def get_user_by_name(self, name: str):
        conn = self.pool.checkout()
        try:
            if self.mode == "safe":
                return conn.execute_bound(
                    "SELECT id, name, role, bio FROM users WHERE name = ?",
                    (name,),
                )
            return conn.execute_concat(
                "SELECT id, name, role, bio FROM users WHERE name = '%s'",
                name,
            )
        finally:
            self.pool.checkin(conn)
```

请注意 `finally` 里一定会 `checkin`。若在 `return` 之前漏掉归还，实验 4 那种占满会在正常流量里出现。safe 路径即使查询抛错，也必须能走进复位逻辑，否则一次失败的请求就会留下脏会话。

### 步骤 2：连接池的归还和重连（pool.py）

- **SafePool.checkin**：若连接已死就丢弃并补新的；否则调用复位（回滚残留事务、charset=`utf8mb4`、autocommit=True、清空预处理语句），再入队。checkout 时若发现 `wait_timeout` 已到，同样换成新会话。
- **NaivePool.checkin**：原样归还，脏事务和脏 charset 留给下一个人。checkout 时若连接已死，走 `silent_reconnect()`：新开一条服务端会话，但客户端仍保留原来的 `in_tx`、charset、会话 id 和预处理语句缓存。


### 步骤 2.1：断连 API 在教学模型里怎么写

`TeachingConnection.kill()` 只拆服务端：关闭底层 sqlite 连接（未提交事务随之回滚），清空服务端预处理登记，把 `alive=False`。它故意不改客户端仍能看到的 `in_tx`、`charset` 和 `client_prepared`——因为真实的客户端库在 TCP 被对端关掉时，也不是立刻把你的「我在事务里」这个本地标志清掉。差别出在发现死连接之后：

```python
def silent_reconnect(self):   # naive
    # 记下客户端仍相信的状态，打开新的服务端会话，再写回去
    old_in_tx, old_charset = self.in_tx, self.charset
    cached = dict(self.client_prepared)
    self.kill()
    self._open_sqlite()
    self.in_tx, self.charset = old_in_tx, old_charset
    self.client_prepared = cached
    self.prepared = {}        # 服务端其实是空的

def safe_reconnect(self):     # safe
    old_id, was_in_tx = self.conn_id, self.in_tx
    self.kill()
    self._open_sqlite()
    self.charset = DEFAULT_CHARSET
    self.autocommit = True
    self.in_tx = False
    self.prepared.clear()
    self.client_prepared.clear()
    # 由调用方改写 conn_id，并抛出 TransactionAborted(old_id, new_id, was_in_tx)
```

实验 2 就是对这两段逻辑的直接测量。读代码时请对照 `test_mysql.py` 里的断言，不要只看函数名。

### 步骤 2.2：如何读 results.json

`./test.sh` 除了在终端打印对照表，也会把同一组数字写进 `results.json`。教案引用的毫秒数都以本次运行的这份文件为准。再次执行时，注入和事务的行数、布尔值应当稳定；毫秒数会随机器负载轻微波动。如果某次运行注入不再是 5 对 0、或 safe 归还后 bob 变成了 admin，那是实现被改坏了，而不是「正常抖动」。

### 步骤 3：运行并解读 5 组对照（真实输出）

**实验 1：SQL 注入**

输入 `' OR '1'='1` 调用 `get_user_by_name`：

| path | rows | names | 正常查 alice | 本次耗时 |
|---|---:|---|---:|---:|
| naive | 5 | alice,bob,cindy,dave,admin | 1 | 0.621 ms |
| safe | 0 | （无） | 1 | 0.088 ms |

safe 的绑定证据：`sql='SELECT id, name FROM users WHERE name = ?'`，`params=["' OR '1'='1"]`，`payload_in_sql=False`，`bind_delta=1`。

**实验 2：事务中途断连再重连**

在事务里把 alice 的 `bio` 改成 `in-tx-change`，然后杀掉服务端会话：

| path | 重连后 in_tx | COMMIT | 之后的 bio | 假成功 | 旧预处理还能用 |
|---|---|---|---|---|---|
| naive | True | 报告成功 | hello | True | True |
| safe | False | 中止（TransactionAborted） | hello | False | False（PreparedGone） |

safe 的会话 id 从 21 变为 22。两边数据都没改上去，但只有 safe 把「原事务已经丢了」告诉了调用方。

把实验 2 的时间顺序再展开一次，避免把「数据没改上去」误读成两种路径行为相同：

```text
初始：alice.bio = hello
BEGIN
UPDATE bio='in-tx-change'          -- 仅在当前会话可见，未持久化
kill()                             -- 服务端回滚，磁盘上仍是 hello

naive.silent_reconnect()
  客户端 in_tx 仍为 True
  COMMIT                           -- 新会话上空提交，返回成功
  读取 alice.bio                   -- hello
  结论：调用方记录「提交成功」，数据未变（假成功）

safe.safe_reconnect()
  抛出 TransactionAborted（21→22）
  in_tx=False
  读取 alice.bio                   -- hello
  结论：调用方知道要重试整笔事务（真失败）
```

排障口诀：断连之后如果看到「成功」却对不上数据，先假设发生过静默重连，而不是先怀疑 UPDATE 语句写错。



**实验 3：归还前的事务和会话状态**

借用者 A：`SET NAMES latin1` + `BEGIN` + 把 bob 的 role 改为 `admin`，不提交就归还。借用者 B 取出同一条物理连接并 `COMMIT`。本实验把池大小设为 1，保证 B 就是这条连接的下一位借用者。

| path | B 的 charset | B 的 in_tx | bob 的 role | 事务泄漏 |
|---|---|---|---|---|
| naive | latin1 | True | admin | True |
| safe | utf8mb4 | False | user | False |

这同时覆盖了两条验收标准里的第二条：「连接归还前事务和会话状态可控」。

**实验 4：长事务占满池**

池大小 3，三个借用者都 `BEGIN` 后不归还。第四次 checkout（等待上限 200ms）超时失败，等待 **200.175 ms**；三人归还后再 checkout，**0.006 ms** 就取到了。`fourth_timed_out=True`。这组数字用来支撑权衡里「池会减少握手，也会被长事务占满」。

**实验 5：`wait_timeout` 空闲断连**

全局 `wait_timeout=0.12s`，归还后空闲 `0.25s` 再 checkout：

- safe：新会话，id 1→2，charset=`utf8mb4`；
- naive：静默重连，`kept_id=True`，charset 仍是 `latin1`。

### 步骤 4：对照验收标准

- **查询使用绑定参数：** 实验 1 中 safe 对注入输入返回 0 行，SQL 文本是 `?` 占位，payload 只出现在参数里，`bind_delta=1`。
- **连接归还前事务和会话状态可控：** 实验 3 中 safe 归还后，下一位借用者看到 `utf8mb4`、`in_tx=False`，bob 仍为 `user`；naive 则把 `latin1` 和未提交的提权一并泄漏。实验 2 和实验 5 进一步证明：重连和空闲超时之后，safe 不会假装还在原会话里。


把验收标准直接映射到 `test_mysql.py` 里的断言，便于你改代码后回归：

```python
# 验收 1：查询使用绑定参数（实验 1）
assert naive_rows == 5 and safe_rows == 0
assert naive_alice == 1 and safe_alice == 1
assert bind_count_delta == 1
assert payload not in last_sql
assert last_params == [payload]

# 验收 2：归还前事务和会话状态可控（实验 3）
assert naive_b_charset == "latin1" and naive_bob_role == "admin"
assert safe_b_charset == "utf8mb4" and safe_b_in_tx is False
assert safe_bob_role == "user"

# 配套：重连不得静默续接（实验 2）
assert naive_false_success is True          # COMMIT 成功，bio 仍是 hello
assert safe_aborted is True and safe_id_changed is True
assert safe_prepared_err == "PreparedGone"
```

这三组断言同时失败或同时被删掉，通常意味着有人把对照实验「改简单了」。改练习代码时可以加功能，但不要为了让 naive 看起来也正确而删掉这些断言。

运行时间方面，本次 `./test.sh` 大约 1 秒（主要花在两段 0.25s 的 `wait_timeout` 空闲等待，以及实验 4 的 200ms checkout 超时上）。若你把 `wait_timeout` 或池等待上限改得更大，总时间会按你改的值增加。没有网络，也不需要本机安装 MySQL。

### 步骤 5：自己动手的扩展练习

这些是练习，不是本次已经跑过的数字，不要把设想的结果写进总结。

1. 给 safe 的 `checkin` 补上「未读结果集必须清掉」的路径：故意留一个未 `fetch` 完的查询，再借给下一个人，观察 naive 与 safe 的差异。
2. 把实验 2 改成「断连后自动重试整笔事务」：safe 捕获 `TransactionAborted`，重新 `BEGIN` 并重做 `UPDATE`，最后再 `COMMIT`，确认 alice 的 `bio` 变为 `in-tx-change`。naive 的假成功在这一步会显得更危险——它以为已经提交，根本不会重试。
3. 把池大小改回 3，再做实验 3：脏连接在队列末尾，不一定下一个请求立刻中招。加一个循环，直到再次借到那条物理连接，确认泄漏仍然发生。这能帮你理解「偶发错账」。
4. 对照 8.4 文档，把复位列表里教学实现还没做的项（临时表、用户变量、`LAST_INSERT_ID()`）补一条最小演示。


### 步骤 6：和生产客户端的对应关系（不要记错边界）

教学模型和你之后在公司里看到的客户端，可以按职责对齐，而不能按类名对齐：

| 教学模型 | 生产里通常由谁做 | 你要对齐的语义 |
|---|---|---|
| `execute_bound` / `PREPARE`+`EXECUTE` | 官方连接器或框架的预处理/参数绑定 API | 用户输入走参数通道，不进 SQL 文本 |
| `SafePool.checkin` 复位 | 连接池在归还时调用 `mysql_reset_connection()` 或等价重置 | 下一位借用者看不到上一位的事务和会话变量 |
| `TransactionAborted` | 关闭自动重连后，应用自己处理断开并决定是否重试 | 新会话 ≠ 原事务；只有幂等的整笔事务才能重试 |
| `silent_reconnect` | 已 deprecated 的自动重连（`MYSQL_OPT_RECONNECT`） | 可能无通知、丢失事务和会话设置 |
| `PoolTimeout` | 池的最大等待时间 / 获取连接超时 | 长事务占满时要快速失败，而不是无限等 |
| `wait_timeout` 检测 | 服务端踢掉空闲会话 + 客户端发现连接已死 | checkout 或第一次用时必须按新会话处理 |

表里的生产 API 名称，只包含本课已在 8.4 文档中核对过的部分。框架各自的池配置项名称以该框架文档为准，不要把教学类名抄进生产代码。

## 源码与文档阅读

> sourceStudy 原文：「沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。」

入口必须是本课唯一指定的资源：[MySQL 8.4 · SQL Prepared Statements](https://dev.mysql.com/doc/refman/8.4/en/sql-prepared-statements.html)。版本记为 **MySQL 8.4**，访问日期记为 **2026-11-11**。下面只给「带着什么问题去读」，具体段落以你打开的那一版文档为准。

**1. 定义与示例（对照：教学代码的 `prepare` / `execute_bound` / `PREPARE ... EXECUTE`）**

- `PREPARE`、`EXECUTE ... USING`、`DEALLOCATE PREPARE` 各自做什么？占位符 `?` 出现在语句的哪些位置（包括文档提到的 `LIMIT`）？
- 预处理语句与 session 的关系：会话结束时未释放会怎样？在存储过程里创建的语句，过程结束时会不会被自动释放？
- `max_prepared_stmt_count` 为 0 时意味着什么？SQL 语法的预处理为什么不能嵌套、不能在一条字符串里写多条语句？
- 记录待验证：你自己在 8.4 环境里跑一遍文档中的 hypotenuse 示例，把结果记下来。教学实践用 sqlite3 模拟语义，不替代这一步。

**2. 错误处理与生命周期（对照：实验 2 的 `PreparedGone`、实验 5 的新会话）**

- 文档如何描述「语句属于创建它的 session」？如果会话已经没了，再 `EXECUTE` 旧名字，你预期看到什么错误（以你实际跑到的为准，不要提前背一个未核对的错误码）？
- 应用接口（C API / 连接器）和 SQL 接口是两条路径：文档强调它们并不等价。记下这条边界，避免在教案或代码里把 `PREPARE` 语句再拿去给 `mysql_stmt_prepare()` 用（文档写了这是不允许的）。

**3. 需要另外对照、且本课已核对的两份 8.4 文档（访问日期同样是 2026-11-11）**

连接池和重连不在 Prepared Statements 这一页里写完。本课已核过的两份是：

- [Automatic Reconnection Control](https://dev.mysql.com/doc/c-api/8.4/en/c-api-auto-reconnect.html)：自动重连已 deprecated、默认关闭、重连无明确通知、状态重置列表（含回滚活动事务）；
- [mysql_reset_connection()](https://dev.mysql.com/doc/c-api/8.4/en/mysql-reset-connection.html)：不断开连接的会话复位，适合归还连接池时使用。

阅读时请自己打开原文，核对这些结论有没有随小版本变化，并记下你看到的版本号。

**阅读笔记模板**

| 条款 | 文档位置（版本 / 访问日期） | 机制要点 | 教学模型对应 | 差异 / 边界 |
|---|---|---|---|---|
| 占位符与防注入 | 8.4 Prepared Statements / 2026-11-11 | 参数不改变语句结构 | `execute_bound` / 实验 1 | 教学层用 sqlite3 占位符，不是 MySQL binary protocol |
| 语句的 session 生命周期 | 同上 | 会话结束自动释放 | 实验 2 的 `PreparedGone` | naive 的客户端缓存会掩盖这一点 |
| 自动重连会丢事务 | 8.4 Auto-reconnect / 2026-11-11 | 回滚活动事务，无明确通知 | `silent_reconnect` vs `TransactionAborted` | 教学模型主动抛错；真实自动重连可能完全沉默 |
| 归还前复位 | 8.4 mysql_reset_connection() / 2026-11-11 | 回滚 + 复位会话变量 | `SafePool.checkin` | 教学实现只覆盖事务、charset、autocommit、预处理语句 |


阅读时建议按「定义 → 示例 → 边界 → 错误路径」的顺序做笔记，而不是只收藏链接。一个可操作的最小流程是：

1. 把文档中的 hypotenuse 示例原样抄进 8.4 的 mysql 客户端（如果你有的话）跑通，记下 `PREPARE`/`EXECUTE`/`DEALLOCATE` 三步各自的输出；
2. 故意 `EXECUTE` 一个从未 `PREPARE` 的名字，或先 `DEALLOCATE` 再 `EXECUTE`，把报错原文记下来；
3. 在一个会话里 `PREPARE`，换一个会话再 `EXECUTE` 同一个名字，确认它是 session 级而不是实例级；
4. 回头看 practice97 的实验 1 和实验 2，问自己：教学模型的哪一步对应文档的哪一条，哪一步是简化（例如用 sqlite3 而不是 binary protocol）。

没有 8.4 实例也不要跳过第 4 步。sourceStudy 要求「沿资源中的定义、示例与错误处理路径核对实现」，「核对」的意思是你能指出实现和文档一致的地方，以及实现故意没覆盖的地方，而不是把文档再复述一遍。

记录：文档版本、访问日期、你实际打开的 URL、你亲自跑过的示例。边界：教学实验只模拟语义，不代表 MySQL 服务端的实现方式、错误码和性能。

## 自测题

**题 1**：为什么 `' OR '1'='1` 作为用户名传给拼接 SQL 会返回全部用户，而传给绑定参数的 `WHERE name = ?` 却一行都没有？怎样证明查询真的使用了绑定参数？

<details><summary>答案</summary>

- 拼接后的语句是 `WHERE name = '' OR '1'='1'`，后半段恒真，过滤条件被掏空。实践中 naive 返回 5 行：alice、bob、cindy、dave、admin。
- 绑定参数时，语句结构固定为 `WHERE name = ?`，`' OR '1'='1` 只是要匹配的字符串。表里没有这个名字，所以是 0 行。同一套数据上查 `alice`，两条路径都返回 1 行，说明差异来自输入被如何对待，而不是查询函数本身坏了。
- 证明绑定：看执行日志或计数器。本次实验里 safe 的 SQL 文本是 `SELECT id, name FROM users WHERE name = ?`，`params=["' OR '1'='1"]`，`payload_in_sql=False`，`bind_delta=1`。只看「没有报错」不够，必须看到「输入没有进入 SQL 文本」。
</details>

**题 2**：事务中已经 `UPDATE` 了 alice 的 bio，连接在 `COMMIT` 前被服务端断开。naive 静默重连后再 `COMMIT`，会怎样？safe 应该怎样处理？为什么不能续接原事务？

<details><summary>答案</summary>

- naive：客户端仍认为 `in_tx=True`，`COMMIT` 报告成功，但原事务在断连时已经回滚，alice 的 bio 仍是 `hello`（`false_success=True`）。预处理语句的客户端缓存也还在，可能继续「成功」执行，把会话丢失掩盖掉。
- safe：把重连当作新会话，抛出 `TransactionAborted`，`in_tx=False`，会话 id 从 21 变为 22，旧预处理句柄失效（`PreparedGone`）。bio 同样是 `hello`，但这是预期中的回滚。调用方必须重新 `BEGIN` 并重做全部语句。
- 原因写在 8.4 自动重连文档里：重连会回滚任何活动事务并复位 autocommit，会话变量和预处理语句一并丢失，而且自动重连还可能不通知应用。不能静默续接原事务。
</details>

**题 3**：借用者 A 执行了 `SET NAMES latin1`，并在未提交的事务里把 bob 改成 admin，然后把连接还回池。B 取到这条连接并 `COMMIT`。naive 和 safe 分别会发生什么？这和「长事务占满池」有什么关系？

<details><summary>答案</summary>

- naive：B 看到 `charset=latin1`、`in_tx=True`，它的 `COMMIT` 把 A 的更新提交了，bob 的 role 变成 `admin`。一次完全无关的请求完成了提权。
- safe：归还前已经 `ROLLBACK` 并把 charset 复位为 `utf8mb4`，`in_tx=False`，bob 仍是 `user`。这就是验收标准「连接归还前事务和会话状态可控」。
- 关系：归还不复位会把错误写进数据；归还不及时（长事务一直占着连接）会把正确的请求挡在池外。实践中池大小为 3 时，三个未归还的事务让第四次 checkout 等待 200.175 ms 后失败；归还后只需 0.006 ms。两个问题都来自「连接是带状态的、且数量有限」。
</details>

## 推荐资源

- [MySQL 8.4 · SQL Prepared Statements](https://dev.mysql.com/doc/refman/8.4/en/sql-prepared-statements.html)，来源 dev.mysql.com｜文档｜英文｜进阶｜免费。阅读重点：「重点阅读预处理语句与参数与连接池和会话状态，用于核对本节点实验的语义和边界。」建议结合 practice97 逐项核对，并记下 8.4 版本与访问日期。

## 本课小结

- MySQL 服务端与客户端通过会话交互，连接、事务和字符集设置影响正确性及资源占用。
- **预处理语句与参数**：用 `?` 占位并把值单独绑定，语句结构不会被输入改写。实践中同一段 `' OR '1'='1`，拼接路径泄漏 5 行，绑定路径返回 0 行。
- **连接池和会话状态**：复用的是会话。归还前要回滚残留事务、复位 charset 和 autocommit（语义接近 `mysql_reset_connection()`）。重连是新会话，不能静默续接原事务——否则会出现「COMMIT 成功但数据没变」的假成功。
- **字符集、超时与结果集**：`SET NAMES` 会改变会话环境；`wait_timeout` 会在服务端踢掉空闲连接；结果集要消费完才能安全复用连接。
- 验收：查询使用绑定参数；连接归还前事务和会话状态可控。实践中 safe 路径两项都成立，naive 路径在注入、假成功提交、脏连接提权上全部暴露。
- 误区：重连可能丢失事务和会话设置，不能静默续接原事务。
- 权衡：连接池减少握手，也需防止长事务占满池（实践中 3 条连接被占时，第 4 次 checkout 等待 200.175 ms 后失败）。

学完后可以用三句话自检：查询有没有把输入和语句结构分开；还回池的连接是否像新会话一样干净；断连之后有没有把「新会话」误当成「原事务的继续」。三句都能答上来，并且能指出 practice97 里对应的那组数字，这一课就算过关。

如果有一句答不上来，先回到对应的实验表，再回到文档里那条定义，不要只把结论背下来。

- 边界：业务场景为教学假设；配套代码是教学模型；MySQL 细节以 8.4 官方文档为准。
