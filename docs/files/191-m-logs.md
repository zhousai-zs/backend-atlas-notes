# 第 191/208 课：Agent 日志查询工具

> 序号 191/208 · id `m-logs` · 领域 M · 阶段 3 · 难度 进阶 · 子类 知识与诊断
> 前置：[`l-tools`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/170.html)、[`j-logging`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/149.html)、[`f-context`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/92.html)
> 节点摘要：将日志检索限制为结构化条件、时间范围与结果上限，让 Agent 按证据逐步缩小故障范围。每条结论可回查日志；工具不得返回完整敏感请求体；不能让模型直接拼接不受控的 SQL 或 shell。

## 导读

第 170 课把工具当成有边界的接口，第 149 课要求日志结构化并处理敏感字段，第 92 课强调请求上下文与链路。本课提供 `query_logs(service, start, end, error_code)`，并强制带上租户；拒绝跨租户和过宽时间窗。

教学假设：日志里的租户、请求号和 token 形态字段都是构造数据，不来自真实流量。没有接入 OpenTelemetry 收集器。`is_live_model=false`。

## 学习目标

1. 说明查询应是绑定字段的结构化接口，而不是开放 DSL 或字符串拼接。
2. 独立完成实践：实现 `query_logs`，拒绝跨租户和无限范围。
3. 每条结论能回到具体日志（本次用 `request_id`）；工具不返回完整敏感请求体。

## 为什么后端要懂

把「帮我查一下日志」交给 Agent 之后，最危险的不是查不到，而是查到了别人的租户、或把 `Authorization` 原样送回上下文。严格的查询模板会限制探索，但换来可追踪和可限流。

## 核心知识点

### 1. 结构化参数，拒绝跨租户与无界范围

**直觉。** 租户、服务、时间、错误码是字段，不是拿去拼 SQL 的字符串。

**原理。** 工具把 `tenant`、`service`、`start`、`end`、`error_code` 当作绑定条件，在已解析的记录上过滤，从不把参数拼进 shell 或 SQL。`tenant` 或 `service` 为空直接拒绝。时间窗超过 3600 秒返回 `unbounded or too-wide range`（本次 14:00–16:05 被拒）。`tenant=acme, service=kv, error_code=EXISTS_UNIMPL` 在 5 分钟窗内命中 `R-41`、`R-43` 两条；其他租户的 `R-99` 与其他服务的 `R-50` 都不会出现。把 `service` 设成 `kv; drop table logs` 时，它只是一个不存在的服务名，结果 0 行，没有执行副作用。

### 2. 脱敏、上限，以及结论必须能回查

**直觉。** 工具输出会进模型上下文，等于再开一个日志副本。

**原理。** `Authorization: Bearer ...` 与 `sk-...` 被替换；完整请求体不会返回，只留不超过 40 字且带 `[BODY_REDACTED]` 的预览。结果条数有上限。诊断写：「kv 在该窗内有 2 条 EXISTS_UNIMPL」，证据列表就是 `R-41`、`R-43`，并且每条都能在工具输出里找到。推断部分单独写明：这不能单独证明修复已经上线。

OpenTelemetry 日志数据模型把 body、属性与资源分开，并提醒敏感内容。本课落实的是「不要把完整请求体交给 Agent」，而不是宣称已经对接某个 collector。

## 常见误区

- 让模型拼接不受控的 SQL、Lucene 或 shell。
- 只在 UI 打码，工具 JSON 里仍有 token。
- 结论只说「日志里有很多错误」，却给不出可回查的 request_id 或时间点。
- 用超大时间窗「先倒出再筛」，把限流和租户隔离留到事后。

## 设计权衡

严格模板更安全、更好限流，复杂探索能力受限。若确需临时探索，应走受审计的只读出口，而不是给 Agent 一根开放管道。

## 动手实践

实践包在 `practice191/`，语料为 `data/logs.jsonl`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice191 "$tmp_dir/"
cd "$tmp_dir/practice191"
bash test.sh
```

本次：2 条合法命中，跨租户与其他服务未泄漏，过宽范围被拒，密钥未出现在 `results.json` 或 `run.log`，结论引用了 request_id。`is_live_model=false`。未实测生产日志后端或 OTel collector。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)：HTTP 200，`Date` 2026-11-15 16:13:56 GMT（Asia/Shanghai 2026-11-16 00:13:56）。用它核对「结构化字段、资源/属性与 body 分离」；本课的 JSONL 是教学用的简化记录，不是该规范的完整实现。

## 自测题

**1. 为什么「把 service 写成带分号的 SQL」不应导致工具报数据库错误？**

答：参数是字面量服务名。本次该查询返回 0 行且 `ok=true`，没有执行删除。

**2. 命中记录里还可以看到 Authorization 头吗？**

答：不可以。本次预览被打码且截断为 `[BODY_REDACTED]`，`sk-live-AAA` 等值不出现在结果和日志里。

**3. 怎样才算「结论可回查」？**

答：每条判断都指向工具返回的 `request_id`（或同等稳定标识），并且这些标识确实存在于当次查询结果。

## 推荐资源

- [OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)（英文，规范，进阶，免费）：字段分层与敏感数据提醒。
- 第 149 课、第 170 课：结构化日志与工具边界。

## 本课小结

日志工具要结构化、限范围、限租户，并且先脱敏再交给模型。结论必须能指回具体记录。下一课是第 192 课 m-metrics（指标诊断与异常归因）。
