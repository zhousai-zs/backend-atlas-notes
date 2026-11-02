# 第 13/208 课：关系模型与SQL入门

> 节点：`a-db` · 域：A · 子类：基础工程认知 · 难度：基础 · 前置：`a-sequence`
>
> 一句话：关系数据库用表和约束表达数据，SQL描述需要的结果，由优化器选择执行计划。

## 1. 学习目标与为什么后端要懂

### 1.1 学习目标

完成本课后，你应该能够：

1. **解释主键、外键和唯一性**：说清一行如何被唯一标识、表与表如何引用、业务唯一键与主键的差别。
2. **会写筛选、连接和聚合**：用 `WHERE`、`JOIN`、`GROUP BY` / 聚合函数从多表得到需要的结果集。
3. **坚持参数化查询，并正确处理 NULL**：绝不把用户输入拼进 SQL 字符串；知道 `NULL` 不是普通值，比较要用 `IS NULL` / `IS NOT NULL`。
4. **独立完成实践**：设计用户和订单表，编写连接查询，并测试重复主键约束（本课用 SQLite 落地，语义与 PostgreSQL 教程一致的部分可对照）。
5. 在微信公众号后台场景里，能把「粉丝/用户、消息与订单类业务、模板消息发送记录」想成带约束的表，而不是一堆 JSON 文件。

### 1.2 验收标准

节点给出的验收标准有两条，本课会反复回扣：

- **能避免字符串拼接 SQL。** 所有动态值走占位符（`?` / `$1` / `:name`）+ 绑定参数。拼接等于把注入面交给攻击者，也让类型/转义问题层出不穷。
- **能正确处理 NULL 比较。** SQL 三值逻辑下，`x = NULL` 的结果不是 TRUE（而是 UNKNOWN），筛不出「空」行；必须用 `IS NULL` / `IS NOT NULL`（或数据库提供的 `IS DISTINCT FROM` 等）。

### 1.3 为什么微信公众号后台要懂关系模型

节点对「为什么」写得很干脆：**避免把数据管理退化为手工文件读写。** 放到即将入职的腾讯微信公众号后台，几乎每天都会碰到「多实体、要关联、要约束」：

- **粉丝 / 用户表。** `openid`（或 unionid）必须唯一；主键往往是内部自增/雪花 id。没有 UNIQUE，同一粉丝会插出多行，积分、标签、会话状态全乱。
- **消息 / 订单类业务表。** 用户一次「付费阅读 / 会员购买 / 活动报名」会落一笔业务单据：必须挂到某个 `user_id`（外键），金额非负，状态机（created/paid/refunded）可查询。用文件追加 JSON，很难做「某用户近 30 天已支付订单」这类连接+筛选。
- **模板消息发送记录。** 每次下发记一行：`user_id`、`template_id`、`msgid`、成功/失败码、时间。要统计「某模板昨日失败率」→ 筛选 + 聚合；要联表看昵称 → JOIN。手工日志 grep 扛不住量和一致性。
- **约束就是线上保险丝。** 重复主键、孤儿订单（user_id 指向不存在的用户）应在写入时被数据库拒绝，而不是靠「记得先检查」的约定。

所以本课要建立的肌肉是：**先建模（表 + 约束），再用声明式 SQL 描述结果；把「怎么扫盘、怎么选索引」交给优化器（后续课再深挖），应用层专心参数绑定与业务语义。**

## 2. 从直觉到原理：三个核心要点

### 2.1 主键、外键和唯一性

#### 直觉：身份证、户口本上的「户主引用」、微信号不可重复

- **主键（PRIMARY KEY）**：表内唯一标识一行。像内部工号——稳定、不业务乱改。一表一主键（可以是单列或组合列）。
- **唯一约束（UNIQUE）**：业务上「不能重复」的列，但不一定是主键。公众号场景里 `openid` 几乎总要 UNIQUE：同一 openid 不应两行粉丝档案。
- **外键（FOREIGN KEY）**：本表某列的值必须在另一表主键（或 UNIQUE）里存在。订单的 `user_id` 引用 `users.id`——禁止「幽灵用户的订单」。

#### 原理：关系模型用「表 + 约束」表达数据

关系模型把世界拆成关系（表）：每行是一条元组，列有类型。**完整性**靠约束保证：

| 约束 | 防止什么 | 公众号例子 |
|------|----------|------------|
| PRIMARY KEY | 两行同一身份 | `users.id` 不能重复 |
| UNIQUE | 业务键冲突 | `users.openid` 不能重复 |
| FOREIGN KEY | 引用悬空 | `orders.user_id` 必须对应真实用户 |
| CHECK / NOT NULL | 非法取值、必填缺失 | 金额 ≥ 0；状态非空 |

SQL 是**声明式**的：你写「要什么结果」，优化器选扫描/索引/连接顺序（执行计划）。入门阶段先保证约束与语义正确，再谈性能。

#### 例子：用户与订单（SQLite / 通用 SQL 形态）

```sql
CREATE TABLE users (
    id       INTEGER PRIMARY KEY,
    openid   TEXT    NOT NULL UNIQUE,
    nickname TEXT
);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    status       TEXT    NOT NULL DEFAULT 'created',
    note         TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

> SQLite 默认不强制外键，连接后需 `PRAGMA foreign_keys = ON`。PostgreSQL 默认强制。对照官方教程时注意引擎差异，但「主键 / 唯一 / 外键」概念一致。

#### 微信公众号例子

- 粉丝关注回调：按 `openid` upsert——依赖 UNIQUE，冲突则更新昵称/关注时间，而不是再插一行。
- 模板消息发送记录表 `tpl_send_log(user_id, …)`：外键保证统计时 JOIN 用户表不会大量悬空。

---

### 2.2 筛选、连接和聚合

#### 直觉：先过滤、再拼表、再汇总

- **筛选 `WHERE`**：只要满足条件的行（状态=已支付、时间窗口内）。
- **连接 `JOIN`**：把「用户行」和「订单行」按 `user_id` 拼成更宽的结果。
- **聚合 `COUNT` / `SUM` / `GROUP BY`**：从多行收成「每用户一单汇总」。

#### 原理：连接是笛卡尔积上的谓词；聚合按组折叠

- `INNER JOIN`：两边都匹配才保留（只有下过单的用户×其订单）。
- `LEFT JOIN`：左表全保留，右表没有则右列填 NULL（「所有粉丝及其订单，含零单粉丝」）。
- `GROUP BY` 后，SELECT 里非聚合列必须是分组键（或依赖引擎的宽松规则；写规范 SQL 更安全）。

优化器可能选嵌套循环、哈希连接等——你声明结果形状，不必手写嵌套循环扫文件。

#### 短 SQL

```sql
-- 已支付订单 + 用户 openid
SELECT u.openid, o.id, o.amount_cents
FROM users AS u
INNER JOIN orders AS o ON o.user_id = u.id
WHERE o.status = 'paid';

-- 每用户订单数与金额合计（含零单用户则用 LEFT JOIN）
SELECT u.openid,
       COUNT(o.id) AS order_cnt,
       COALESCE(SUM(o.amount_cents), 0) AS total_cents
FROM users AS u
LEFT JOIN orders AS o ON o.user_id = u.id
GROUP BY u.id, u.openid;
```

#### 短代码（Python sqlite3，参数化）

```python
cur.execute(
    """
    SELECT u.openid, o.id, o.amount_cents
    FROM users u
    INNER JOIN orders o ON o.user_id = u.id
    WHERE o.status = ?
    """,
    ("paid",),
)
```

#### 微信公众号例子

- 「某 template_id 昨天失败多少条」→ `WHERE` + `COUNT`。
- 「失败记录带出粉丝昵称」→ `tpl_send_log` JOIN `users`。
- 「有下发记录但昵称仍空」→ LEFT JOIN 后 `nickname IS NULL`（见下一节）。

---

### 2.3 参数化查询与 NULL

#### 直觉：占位符是安全带；NULL 是「未知」不是「空字符串」

- **参数化**：SQL 文本固定，值另传。数据库驱动负责转义/绑定，切断注入。
- **NULL**：表示缺失/未知。`nickname` 没同步到、订单 `note` 未填，都是 NULL——不是 `''`。

#### 原理：三值逻辑与注入面

比较运算遇到 NULL → 结果为 UNKNOWN。`WHERE` 只保留 TRUE 行，故：

```sql
WHERE nickname = NULL      -- 几乎永远筛不到（结果是 UNKNOWN）
WHERE nickname IS NULL     -- 正确
WHERE nickname IS NOT NULL -- 正确
```

字符串拼接：

```python
# 危险：用户输入可改写语句结构
sql = f"SELECT * FROM users WHERE openid = '{user_input}'"
# 若 input 为  ox' OR '1'='1  → 条件被掏空
```

正确：

```python
cur.execute("SELECT * FROM users WHERE openid = ?", (user_input,))
```

#### 微信公众号例子

- 回调里的 `openid`、活动表单里的备注：一律参数绑定。
- 查「尚未同步昵称的粉丝」：`WHERE nickname IS NULL`，不要写 `= ''` 除非你真的存了空串且语义就是空串。
- 验收回扣：**避免拼接** + **正确处理 NULL 比较**。

## 3. 常见误区与设计权衡

### 3.1 误区（pitfalls）

节点原文：**SQL的NULL不是普通值，等号不能替代IS NULL。**

展开几条后端高频踩坑：

1. **用 `= NULL` / `!= NULL` 判断空。** 永远不对（在标准 SQL 语义下）。改用 `IS NULL` / `IS NOT NULL`。
2. **把 NULL 和空字符串混用。** 业务要约定：未填到底是 NULL 还是 `''`；查询与唯一约束行为不同（多行 NULL 在 UNIQUE 上的处理因引擎而异，需查文档）。
3. **拼接 SQL「图省事」。** 日志里拼好再 execute，等于放弃驱动绑定；微信回调字段一旦被污染就是注入。
4. **以为应用层 check 就够，不建约束。** 并发双请求都能「先查再插」通过——只有 UNIQUE/PK 能在存储引擎层卡死重复。

### 3.2 设计权衡（tradeoffs）

节点原文：**规范化降低更新异常，查询性能需结合索引与访问模式。**

- **规范化（少冗余）**：用户昵称只存 users 表；订单不拷贝一份 nickname。改昵称只改一行，避免「订单里旧昵称、用户表新昵称」不一致（更新异常）。
- **代价**：展示订单列表常要 JOIN；高 QPS 列表页可能觉得「多一次连接贵」。
- **实践权衡**：先按规范建模 + 在 `user_id` / 常用筛选列上建索引；若只读报表极热，再考虑受控冗余或缓存，而不是一上来复制字段、放弃外键。
- **访问模式决定索引**：按 openid 点查 → UNIQUE/索引在 openid；按 `created_at` 扫发送日志 → 时间列索引或分区（后续课）。没有「万能索引」，只有「匹配查询形状的索引」。

## 4. 动手实践

对应节点 practice：**设计用户和订单表，编写连接查询并测试重复主键约束。** 并覆盖验收：参数化、NULL。

### 步骤 1：进入目录并运行脚本

```bash
cd /workspace/atlas/lessons/practice13
python3 users_orders.py
```

脚本使用标准库 `sqlite3`，会：

1. 创建 `users` / `orders`（PK、UNIQUE openid、FK、`PRAGMA foreign_keys=ON`）
2. 参数化插入 3 用户 + 4 订单
3. `INNER JOIN` 查 `status='paid'`；`LEFT JOIN` 聚合每用户金额
4. 故意插重复 `users.id`、重复 `openid`、无效 `user_id` → 均应 `IntegrityError`
5. 对比 `= NULL` vs `IS NULL`
6. 打印危险拼接示例（**不执行**）与安全参数化查询

### 步骤 2：对照预期输出（摘要）

本环境实测（Asia/Shanghai）：

- paid JOIN：小明两单 `(101, 990)`、`(102, 1990)`
- 聚合：`ox_fan_001` → 2 单 / 2980 分；`ox_fan_002` → 1 / 500；`ox_fan_003` → 1 / 0
- 重复主键 / UNIQUE / 外键：三次预期失败
- `nickname IS NULL` → 1 行（`ox_fan_003`）；`= NULL` → 0 行

### 步骤 3：自己改一问（可选）

在脚本同连接里追加（或开 `sqlite3 practice13.db`）：

```sql
SELECT status, COUNT(*), SUM(amount_cents)
FROM orders
GROUP BY status;
```

想一想：若把 `SUM` 换成对 `note` 的某种聚合，NULL 笔记如何影响结果？

### 步骤 4：验收自查

- [ ] 能口头解释：主键 vs UNIQUE(openid) vs 外键各自防什么  
- [ ] 能写（或读懂）users⋈orders 的 JOIN，并加 WHERE / GROUP BY  
- [ ] 重复主键插入失败，且你知道这是约束而非「运气」  
- [ ] **从不**把 openid/备注拼进 SQL 字符串  
- [ ] 查空昵称用 `IS NULL`，不用 `=`

### 步骤 5：代码位置

- 脚本：`/workspace/atlas/lessons/practice13/users_orders.py`
- 说明：`/workspace/atlas/lessons/practice13/README.md`
- 运行后生成：`practice13.db`（可删，脚本每次重建）

## 5. 自测题（3 道，附答案）

### 题 1：主键、外键与唯一性

**题：** 粉丝表已有 `id INTEGER PRIMARY KEY`，又把 `openid` 建成 `UNIQUE`。有人说「有主键就够了，openid 不必 UNIQUE」。订单表 `user_id` 不建外键，「插入前先 SELECT 用户」即可。请反驳或限定，并各举一个公众号后台会炸的场景。

**答案：**

- 主键标识「内部一行」；`openid` 是**业务自然键**。微信回调只带 openid：没有 UNIQUE，并发关注/重复同步会插出两行粉丝，积分与标签分裂。  
- 仅应用层先查再插，挡不住并发与漏检；外键让「订单引用不存在用户」在提交时失败。模板消息日志若大量悬空 `user_id`，JOIN 昵称与对账都会残缺。  
- 回扣 goals：**能解释主键外键和唯一性**。

### 题 2：参数化（验收）

**题：** 活动页把用户输入的备注写进查询：  
`sql = "SELECT * FROM orders WHERE note = '" + user_note + "'"`  
指出风险，并写出 Python `sqlite3` 下的安全写法。

**答案：**

- 风险：注入（如 `x' OR '1'='1`）、引号/编码错误、审计困难。  
- 安全写法：`cur.execute("SELECT * FROM orders WHERE note = ?", (user_note,))`  
- 回扣验收：**能避免字符串拼接 SQL**。

### 题 3：NULL 比较（验收 + pitfalls）

**题：** 要列出尚未同步昵称的粉丝。同事写了 `WHERE nickname = NULL`，结果永远为空；另一同事写 `WHERE nickname = ''`，也漏了真正的 NULL 行。正确怎么写？为什么 `=` 不行？

**答案：**

- 正确：`WHERE nickname IS NULL`（若业务还把空串当「未填」，需 `WHERE nickname IS NULL OR nickname = ''`，并最好统一存储约定）。  
- `=` 在三值逻辑下对 NULL 得 UNKNOWN，`WHERE` 丢弃非 TRUE 行——这正是节点 pitfalls：**SQL的NULL不是普通值，等号不能替代IS NULL。**  
- 回扣验收：**能正确处理NULL比较**。

## 6. 推荐资源

只使用节点给出的资源：

1. **关系模型与SQL入门：参考文档 / 原始资料**
   - URL：https://www.postgresql.org/docs/current/tutorial-sql.html
   - 来源：www.postgresql.org · 英文 · 文档 · 基础 · 免费
   - 阅读建议：重点阅读主键/外键/唯一性，以及筛选、连接和聚合章节；用本课 `users`/`orders` 实验对照 PostgreSQL 教程中的语义与边界（注意 SQLite 与 PG 在外键默认开关、部分类型上的差异，以教程与本机验证为准）。

## 7. 小结与前后置关系

### 7.1 本课要点回顾

1. **表 + 约束**：主键标识行，UNIQUE 护业务键，外键护引用；避免退化成手工文件。  
2. **筛选 / 连接 / 聚合**：声明要的结果，JOIN 拼实体，GROUP BY 做统计。  
3. **参数化与 NULL**：占位符绑参；空值用 `IS NULL`，不用等号。  
4. **误区与权衡**：NULL 三值逻辑；规范化减更新异常，性能靠索引与访问模式。  
5. **实践**：users/orders、JOIN、重复主键失败、NULL 与参数化演示。

### 7.2 验收回扣

- **避免拼接 SQL：** 自测题 2 + 实践步骤 1/4/6。  
- **正确处理 NULL：** 自测题 3 + 实践步骤 5。

### 7.3 前后置

- **前置：** `a-sequence`（有序与集合直觉，利于理解「行的集合」与结果集）。  
- **本节点：** `a-db`（关系模型与 SQL 入门）。  
- **后续：** 事务与隔离、索引与执行计划、ORM 与迁移，都会默认你具备本课的约束观与参数化习惯。

### 7.4 一句话带走

**用表和约束表达真相，用参数化 SQL 描述结果；NULL 用 IS NULL，openid 用 UNIQUE，订单用外键挂到真实用户。**
