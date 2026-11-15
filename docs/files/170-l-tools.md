# 第 170/208 课：Tool Calling 与执行器

> 序号 170/208 · id `l-tools` · 领域 L · 阶段 2 · 难度 基础 · 子类 Agent执行模型
> 前置：[`l-loop`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/169.html)、[`f-encoding`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/82.html)
> 节点摘要：Tool Calling 是模型提出结构化操作意图的接口，真正的执行器仍是宿主应用。

## 导读

模型输出「请调用 `get_metric`」只是一份意图。执行器必须完成 schema 校验、租户授权、时间窗口限制，再把结果绑回 `call_id`。工具列表是给模型看的菜单，不是授权书：同一个合法参数，换一个用户就可能越权。失败必须仍是失败，日志里不能出现令牌。

## 学习目标

1. 把 schema、授权和执行失败分成不同的错误类。
2. 独立完成实践：实现只读 `get_metric(service, start, end)`，用 pydantic / JSON Schema 限制字段，再由服务端校验服务归属和时间跨度；准备 10 条正常与 10 条异常输入；并行调用并用 `call_id` 对齐。

## 为什么后端要懂

若把模型输出直接当成后端 RPC，未知工具、跨租户服务和超大时间窗口都会变成真实查询。并行调用一旦按返回顺序而不是 `call_id` 装箱，就会张冠李戴。

## 核心知识点

### 1. Schema 是语法，授权是另一道门

**原理。** `schema.py` 里 `GetMetricArgs` 使用 pydantic 2.14.3：`extra='forbid'`，`start` / `end` 必须是带时区的时间。这只能挡住「多出来的字段」「类型错误」「无时区时间戳」。它看不见租户。

**执行器。** `executor.py` 的顺序是：未知工具 → schema → 租户是否存在 → 服务是否在该租户 ACL 内 → 时间范围是否反向或超过 7 天 → 再读指标。ACL 示例：alice 可读 checkout / search / payments，bob 只能读 search，`secret-internal` 虽然在指标表里存在，但没有任何租户被授权。

**10 + 10。** 10 条正常调用全部 `ok` 并返回 `value`。10 条异常全部 `ok=false` 且 payload 里没有 `value`，包括：未知工具 `drop_table`、额外字段夹带 `token`、无法解析的时间、`service` 类型错误、无时区时间戳、bob 读 checkout、alice 读 `secret-internal`、超过 7 天的窗口、反向时间、未知租户 carol。错误类分开：`protocol`（未知工具、参数不合法）与 `rejected`（越权、窗口、未认证）。验收要求的「越权、无效时间范围及未知工具均在服务端拒绝」全部命中。

### 2. 并行返回必须按 call_id 对齐

**原理。** 模型可以一次提出多个 tool call，完成顺序不必等于提交顺序。执行器为每次调用保留 `call_id`，`bind_results` 按 ID 装箱；缺 ID 或重复 ID 会直接失败。

**实践对照。** 一次提交 `call_aaa`（alice/checkout，成功）、`call_bbb`（bob/search，成功）、`call_ccc`（`drop_table`，失败）。完成顺序被故意反转为 ccc、bbb、aaa，绑定后 aaa / bbb 仍成功、ccc 仍是 `unknown_tool`。顺序变化没有把失败写进成功的那一格。

本课没有调用 OpenAI 的 function calling 接口（官方文档 2026-11-15 已抓取），并行场景由执行器自己复现。`is_live_model=false`。真实模型是否会稳定带上 `call_id`，需要你在 `chat.completions` / Responses 的 tool 调用路径上另测。

### 3. 失败就是失败，日志不得含令牌

失败结果保留可诊断的 `error` 码和简短 `message`，例如 `schema validation failed: ...` 只包含 loc 与 type，不回放可能含令牌的原始入参，也不附加内部堆栈。`value` 字段不会在失败对象上出现——这就是「不要把失败包装成成功」。

日志过滤器会把 `sk-live-example-not-used` 和 `tok_supersecret` 从消息和参数（包括 dict 头）里替换成 `[REDACTED]`。实践里故意把这两个值放进 `authorization` 与 `x-extra` 头，`reports/executor.log` 中只出现 `[REDACTED]`。验收：「日志不包含令牌或原始敏感数据」。

## 常见误区

- 以为模型能看见工具，就等于当前用户有权调用。
- 按返回顺序而不是 `call_id` 合并并行结果。
- 把业务拒绝（越权）和协议错误（缺字段）都包装成 `{ok: true, value: null}`。
- 为了方便排错，把 Authorization 头原样打进日志。

## 设计权衡

把「协议错误 / 业务拒绝 / 执行失败 / 结果不确定」分开，Agent 才能决定是改参数、换工具、停下来还是重试。混成一个 `false` 会让循环瞎试。只读工具仍然要做窗口和租户限制，否则会被用来扫整个历史。

## 动手实践

```bash
cd practice170
bash test.sh
```

接入真实模型时，把模型给出的 `tool_calls[].id` 当作本课的 `call_id` 交给执行器，再按 ID 把结果填回消息列表。不要让模型「看见工具列表」绕过 ACL。

## 源码与文档阅读

`schema.py`；`executor.py` 的门控、绑定与脱敏；`cases.json` 的 20 条输入；`reports/executor.log`。

## 自测题

**1. bob 用完全合法的参数查询 checkout，为什么必须失败？**

答案：参数通过 schema，但 checkout 不在 bob 的 ACL 里。这是 `unauthorized_service`，属于业务拒绝，不是 schema 问题。

**2. 并行三个调用乱序返回，怎样避免把失败派给成功的那一次？**

答案：每条结果带着提交时的 `call_id`，用 ID 而不是列表位置合并。本课在反转顺序后仍能把 `unknown_tool` 留在 `call_ccc`。

**3. 调用方把令牌放进了额外字段或请求头，日志里应看到什么？**

答案：不应看到原始令牌。本课在 schema 阶段拒绝额外字段，并在日志里把头中的秘密替换为 `[REDACTED]`。

## 推荐资源

| 资源 | 说明 |
| --- | --- |
| [OpenAI — Function calling](https://platform.openai.com/docs/guides/function-calling)（2026-11-15 同批抓取） | 模型侧的工具意图与 `call_id`。执行与鉴权仍在宿主。本课未调用实网 API。英文，基础，免费。 |

## 本课小结

Tool calling 的可靠部分在执行器：schema、租户、窗口、`call_id`、失败语义和日志脱敏。本课用 10+10 条用例和一次乱序并行把这些门跑通；模型如何选工具未在本机验证。
