# 第 180/208 课：审批与人工介入

> 序号 180/208 · id `l-human` · 领域 L · 阶段 4 · 难度 深入 · 子类 权限与结果保障
> 前置：[`l-durable`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/177.html)、[`l-mcp-auth`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/173.html)
> 节点摘要：人工介入是任务状态机的一部分；批准必须绑定工具名、精确参数、目标资源和版本，并在执行前重新验证权限与前置条件。

## 导读

第 177 课解决进程重启后如何继续任务，第 173 课强调每次操作都要再授权。本课把两者接到高影响写操作上：先准备一份可审查的计划，等人批准，再执行；批准期间资源若已改变，或权限已被收回，旧批准必须作废。

教学假设：一个后台要把报告发布到指定渠道，发布前必须有人批准。这是为讲解批准绑定和幂等提交设计的场景，不代表腾讯或微信的内部审批流。实践把状态存在 SQLite，没有接入 Slack 或邮件通道，`is_live_pager_or_slack_approval=false`。

## 学习目标

1. 说明待审批、批准、拒绝、过期、执行后的状态迁移，以及批准必须绑定的字段。
2. 独立完成实践：进程重启后仍能找到待审批任务；重复批准不会再次提交。
3. 用负例确认：资源版本变化或权限撤销后旧批准失效；拒绝和过期都不会执行。

## 为什么后端要懂

节点给出的理由是「使高影响后台动作既能完成可审查准备，又能防止批准对象漂移和重复执行」。如果只在终端里读一句“用户同意”，或把模型转述当成审批，目标资源可能已经换成另一份，按钮却还是绿的。

## 核心知识点

### 1. 审批是服务端状态，不是浏览器上的一个按钮

**直觉。** 待审批任务必须能在进程重启后被再次找到。

**原理。** 实践把 `approvals` 表放在 SQLite。`prepare` 写入 pending 行后关闭连接，再用 `Store.reopen` 打开同一文件，pending 里仍有 `ap-restart`，随后批准并成功 `applied`。2026-11-15 23:52:59（UTC+8）抓取的 [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/) 与 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)（Agents 文档 Last-Modified 2026-11-10 16:04:28，LangGraph 为 2026-11-15 03:43:40，UTC+8）提供的是暂停/恢复机制；具体授权、版本校验和审计仍由服务端承担。本课没有跑这两套框架的审批 API，第 182 课才会用官方 `interrupt`。

### 2. 批准绑定工具、参数摘要、资源和版本

**直觉。** 批准的是“这一次、这几个参数、这一版资源”，不是“以后随便做什么都可以”。

**原理。** 每条审批记录保存 `tool`、`param_digest`（参数 JSON 的 SHA-256）、`resource_id` 和当时的 `resource_version`。本次正常用例绑定 `publish_report`、版本 1。若批准时传入被改过的参数（`channel` 从 `ops` 改成 `public`），得到 `param_binding_mismatch`，执行次数为 0。

### 3. 恢复时重新鉴权、重新核对前置条件

**直觉。** 昨天的批准，今天执行时还要再看一眼权限和资源是不是还是那一份。

**原理。** 在 pending 期间把资源从 v1 bump 到 v2，旧批准变为 `invalid`，原因 `resource_version_drift`。把 Alice 的权限改成 `revoked`，旧批准同样作废，原因 `permission_revoked`。两条路径的执行次数都是 0。

### 4. 重复批准必须幂等

**直觉。** 双击“同意”不能让远端提交两次。

**原理。** 第一次 `approve_and_apply` 插入唯一的 `request_id=exec-ap-ok`；第二次返回 `already_applied`，`resubmitted=false`，`execution_count` 仍为 1。

### 5. 拒绝和过期都不得执行

**直觉。** 只有仍有效的批准才能 apply。

**原理。** `reject` 后的状态是 `rejected`，再 apply 被拒绝，执行次数 0。`expires_at` 已经过去的记录在 apply 时被标为 `expired`，同样不执行。

### 6. 副作用必须放在批准之后，并且可重放安全

**直觉。** 若在等待之前就做了不可重放的写入，恢复节点时可能写两次。LangGraph 文档对 interrupt 也提出同一条规则，第 182 课会再验证。

**原理。** 本课把修改拆成 prepare / apply：prepare 只登记差异和绑定，apply 才产生 `executions` 行。拒绝、过期、漂移都不会走到插入。

## 常见误区

把模型转述的“用户同意”当成真实审批。恢复时跳过再次授权。在暂停前做不可重放的写，却假定恢复不会重复。这三条是节点陷阱。实践用服务端行、再次校验和 prepare/apply 分离来对应它们。

## 设计权衡

审批会增加等待成本，应集中在业务定义的高影响边界；过细的确认会使人失去审查能力。本课只覆盖发布这一类写操作，没有把每一次读日志都送去人工。

## 动手实践

实践包是 `practice180.tar.gz`：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice180.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice180"
bash test.sh
```

本次机器环境为 CPython 3.13.5。独立复跑后：绑定字段完整；重复批准不重提；版本漂移与权限撤销使旧批准无效；拒绝和过期不执行；reopen 后仍能批准 pending。未验证：Slack / 邮件审批通道，以及多区域的领导选举。

## 源码与文档阅读

同日抓取的 Agents HITL 与 LangGraph Interrupts 文档均为 200。它们说明如何暂停和恢复；本实践证明的是业务侧绑定、失效和幂等，而不是这两套 API 的完整产品能力。

## 自测题

**1. 为什么批准必须带上资源版本，而不是只记录“允许发布 report-1”？**

答：资源可能在等待期间被改掉。本次 bump 到 v2 后，旧批准的原因是 `resource_version_drift`，执行次数为 0。否则批准对象已经漂移。

**2. 进程重启后找不到待审批任务，会造成什么问题？**

答：审批会丢，或被重新准备成另一份绑定。本次关闭再打开同一 SQLite 文件，`ap-restart` 仍为 pending，随后成功 applied。

**3. 用户连点两次“批准”，为什么必须返回 already_applied 而不是再提交一次？**

答：提交是副作用。本次第二次调用 `resubmitted=false`，执行表仍只有一行。

## 推荐资源

- [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)：核对审批状态与序列化；本课未运行该 API。
- [Interrupts · 暂停与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)：对照暂停恢复及“暂停前副作用必须幂等”。

## 本课小结

人工介入是服务端状态机：批准绑定工具、参数摘要、资源和版本；执行前再查权限与版本；重复批准不重提，拒绝和过期不执行。本课用 SQLite 验证了这些规则，没有接入外部审批通道。下一课是第 181 课 l-sdk（OpenAI Agents SDK 实践）。
