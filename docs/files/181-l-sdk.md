# 第 181/208 课：OpenAI Agents SDK 实践

> 序号 181/208 · id `l-sdk` · 领域 L · 阶段 3 · 难度 进阶 · 子类 框架源码与实践
> 前置：[`l-harness`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/174.html)、[`l-memory`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/176.html)
> 节点摘要：OpenAI Agents SDK 把 Agent、Runner、工具、会话和 trace 组织成可复用运行构件；它提供循环与观测，不替你定义业务成功条件，也不保证工具副作用幂等。

## 导读

第 174 课用手写 Harness 说明状态机和验收，第 176 课区分会话与任务状态。本课改用锁定版本的官方 SDK，做四件必须能在没有外部模型时验证的事：工具循环与工具错误、SQLiteSession 隔离、服务端授权不依赖 guardrail、以及本地脱敏 trace。

教学假设：一个后台笔记 Agent 通过工具写备注。这是为讲解 SDK 边界设计的场景，不代表腾讯或微信的内部客服助手。实践锁定 `openai-agents==0.24.0`，使用官方 `ScriptedModel`（不是旧教程里的 `FakeModel`），`is_live_model=false`。测试进程会移除默认的 BatchTraceProcessor，并删除环境中的 `OPENAI_API_KEY`（不读取、不打印其值），避免在有密钥时把测试 trace 上传。

## 学习目标

1. 说明 Agent 配置、Runner 循环、function tools、Session 与本地 context 的职责边界。
2. 独立完成实践：用 ScriptedModel 覆盖工具循环和工具错误，而不访问外部模型。
3. 用负例确认：不同 session 不会混用历史；guardrail 关闭时无权限请求仍被工具服务端拒绝；trace 能关联工具调用且敏感参数被省略。

## 为什么后端要懂

节点给出的理由是「用成熟运行构件减少重复代码，同时保留后台服务的权限、状态和验收边界」。直接抄未锁定版本的教程函数名，会在 0.24.0 上找不到 `FakeModel`；把 Session 当成事务库、把 guardrail 当成唯一授权，会在框架升级或关闭护栏之后出漏洞。

## 核心知识点

### 1. Agent、Runner 与最终输出

**直觉。** Agent 描述“用哪些工具、什么模型、什么指令”；Runner 负责循环，直到产生 `final_output` 或触发限制。

**原理。** 2026-11-15 23:52:58（UTC+8）抓取的 [Running agents](https://openai.github.io/openai-agents-python/running_agents/)（Last-Modified 2026-11-10 16:04:28，UTC+8）描述了这一循环。实践用 `Runner.run_sync` 驱动，第一次模型步骤发出 `write_note`，工具返回后第二次步骤给出最终文本 `Saved the q3-forecast note.`。`model.assert_complete()` 确认脚本化步骤被全部消费，避免工作流提前结束却还以为测到了循环。

### 2. function tools 与工具错误

**直觉。** 工具是宿主函数；它抛错时，循环应能把错误交给下一步模型，而不是整个进程崩掉。

**原理。** 同日 [Testing](https://openai.github.io/openai-agents-python/testing/) 给出的官方配方是：先 `function_call(...)`，再 `assistant_message(...)`，中间跑的是真实工具管道。实践增加一个必失败工具 `boom`：`RuntimeError` 被 SDK 转成通用的 “An error occurred while running the tool.”，随后脚本化模型输出“The lookup tool failed; I will not invent a row.”。不要把这句脚本化回复写成模型自己的推理能力。同日 [Tools](https://openai.github.io/openai-agents-python/tools/) 文档（23:52:59，同一 Last-Modified）说明了 FunctionTool 的执行边界。

### 3. 本地 context 与模型输入分离

**直觉。** `Runner.run_sync(..., context={"user": "bob"})` 把本地对象交给工具，不一定也不应该把同一份对象序列化进 prompt。

**原理。** `write_note` 的第一个参数是 `RunContextWrapper[dict]`，从 `ctx.context["user"]` 读取身份。这与第 175 课的运行时/模型可见分离一致：授权发生在工具里，而不是靠把用户身份再写进说明文字。

### 4. SQLiteSession：同一 session 可恢复，不同 session 不混用

**直觉。** Session 存的是对话历史，不是业务事务。

**原理。** 2026-11-15 23:52:58（UTC+8）抓取的 [Sessions](https://openai.github.io/openai-agents-python/sessions/) 把 SQLiteSession 定义为对话历史存储，默认内存库，需要持久化时传入文件路径。实践使用同一 `sessions.db`、不同 `session_id`：Alice 先说 “my report is R-1”，关闭后再用同一 id 打开，继续问“what did I mention?”，历史里仍有 `R-1`（4 条 item）；Bob 的 session 没有 `R-1`（2 条 item）。这验证了隔离和重启恢复，并不表示 Session 能替代第 177 课的任务表。

### 5. guardrail 关了也不能绕过服务端授权

**直觉。** 护栏是额外检查，不是权限系统。

**原理。** Agent 的 `input_guardrails` 与 `output_guardrails` 保持空列表。Bob 调用 `write_note` 时，工具服务器检查 ACL 后返回 `ERROR: unauthorized`，`SERVER.notes` 里没有 Bob 的记录，只在 `denied` 里留下 `"bob"`。模型随后的“Write was rejected by the server.”仍是脚本化输出；真正的证据是服务器没有落盘。

### 6. 本地 trace：能关联工具，且敏感字段为 None

**直觉。** 要观测循环，但不把 `api_token` 送到任何外部处理器。

**原理。** 官方测试文档写明：配方里关闭 tracing，以免默认处理器在配置了 API 密钥时上传测试活动。本课的验收却要求“能关联工具调用、敏感参数关闭或脱敏”，因此用 `set_trace_processors([LocalProcessor])` **替换** 默认导出器，再以 `tracing_disabled=False`、`trace_include_sensitive_data=False` 跑一次。结果：出现 `type=function`、`name=write_note` 的 span，其 `_trace_id` 与本次 workflow 一致；官方行为把 function span 的 `input` / `output` 留在 `None`；整份 span 导出中没有 `TOKEN-ALICE-999`。这不是 OpenAI Traces 控制台的上传验证。

## 常见误区

把框架 guardrail 当成唯一权限系统。把 Session 当成所有后台任务的事务存储。沿用未锁定版本的旧教程函数签名（例如 `FakeModel`）。这三条是节点陷阱。实践分别用服务端 ACL、会话/任务分工，以及锁定 0.24.0 的 `ScriptedModel` 来对应。

## 设计权衡

SDK 集成和生态较好，但业务状态、存储一致性及模型迁移仍需设计。对简单固定流程，不必为了使用 SDK 而加入 handoff。本课故意不接真实模型和托管 Conversations，以免把脚本化步骤写成在线能力。

## 动手实践

实践包是 `practice181.tar.gz`，需要锁定的 `openai-agents==0.24.0`：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice181.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice181"
unset OPENAI_API_KEY
bash test.sh
```

本次机器环境为 CPython 3.13.5、`openai-agents==0.24.0`。独立复跑后：工具循环与工具错误都走完脚本化步骤；Alice 历史含 R-1、Bob 不含；Bob 未写入；function span 存在且令牌未出现在 trace；`openai_api_key_used=false`。未验证：真实 Responses / Chat Completions 的生成质量、Traces 控制台上传、托管 Conversations / Sessions。

## 源码与文档阅读

Running / Tools / Sessions / Testing 四页均在 2026-11-15 23:52 左右（UTC+8）抓取为 200，Last-Modified 均为 2026-11-10 16:04:28（UTC+8）。以 0.24.0 源码为准：测试入口是 `agents.testing.ScriptedModel`、`assistant_message`、`function_call`。若以后升级 SDK，应先重新阅读 Testing 页和 `agents.testing` 的导出名，不要假定旧别名还在。

## 自测题

**1. 为什么本课坚持用 ScriptedModel，而不是写一个固定返回字符串的“伪模型”来证明循环？**

答：ScriptedModel 是 0.24.0 官方测试边界，走真实的工具管道和规范化输出项。自写固定输出只能证明自己的桩，不能证明 SDK 循环。本次工具循环和工具错误都消费了官方步骤，并且 `is_live_model=false`。

**2. 关闭 input/output guardrail 之后，Bob 为什么仍然写不进去？**

答：授权在工具服务器里。本次 `SERVER.notes` 没有 Bob 的记录，`denied` 含 `"bob"`。guardrail 不是权限系统。

**3. 为什么既要关掉默认 trace 导出器，又要保留一次本地 function span？**

答：默认处理器可能在存在 API 密钥时上传测试数据；验收又要求能把工具调用关联到 trace，并省略敏感参数。本次用内存 LocalProcessor 替换导出器，`trace_include_sensitive_data=False` 时 input/output 为 None，令牌未出现。

## 推荐资源

- [Running agents](https://openai.github.io/openai-agents-python/running_agents/)：核对循环与执行配置。
- [Tools](https://openai.github.io/openai-agents-python/tools/)：核对 FunctionTool 与执行边界。
- [Sessions](https://openai.github.io/openai-agents-python/sessions/)：核对跨轮会话存储，以及它不是业务事务库。
- [Testing](https://openai.github.io/openai-agents-python/testing/)：核对 ScriptedModel 与“测试中关闭默认上传”的建议。

## 本课小结

锁定 `openai-agents==0.24.0` 后，可以用官方 ScriptedModel 验证循环、工具错误、会话隔离和服务端授权；本地 processor 能关联工具 span 并省略敏感字段，而不走默认上传。本课没有调用外部模型，也没有把 Session 当成任务事务。下一课是第 182 课 l-langgraph（LangGraph 状态图实践）。
