# 第 151/208 课：Tracing 与上下文传播

> 序号 151/208 · id `j-tracing` · 领域 J · 阶段 3 · 难度 进阶 · 子类 可观测性
> 前置：[`j-metrics`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/150.html)
> 节点摘要：分布式追踪用 trace/span 关联跨服务操作；传播格式、采样和异步上下文管理决定链路完整性。

## 导读

上一课把多次请求压成计数和延迟分布。要回答「这一次 KV 读取慢在客户端、RPC 还是存储」，需要把三次操作挂到同一条追踪上。传播一旦在异步边界丢失，存储一侧就会变成另一条无关的根追踪。

教学假设：一次教学请求经过客户端、RPC 服务和存储三个跨度。以下跨度名称、标识和异步丢失均为教学设计，不代表腾讯或微信内部实现。本课在同一进程内用上下文对象模拟跨服务传播，没有访问外部追踪后端。

## 学习目标

1. 解释parent/child span及适用边界
2. 独立完成实践：串联客户端、RPC 服务和存储，故意丢失一次异步上下文并修复。

## 为什么后端要懂

节点给出的理由是「定位多跳 RPC 的延迟来源。」没有共同的 trace_id 和正确的父子关系，三个跨度无法画在同一条链上，延迟只能在各自动作内部解释。

## 核心知识点

### 1. parent/child span

**原理。** OpenTelemetry 的 Context propagation 文档说明：上下文保存发送方与接收方（或执行单元）用以关联信号的信息。当服务 A 调用服务 B 时，A 把 trace ID 和 span ID 放入上下文；B 用这些值创建属于同一条追踪的新跨度，并把 A 的跨度设为父跨度，从而跟踪跨服务的完整请求。

**直觉。** 一条追踪是整棵树，一个跨度是树上的节点。父标识指向上一层工作，而不是「时间上刚好在前面的任意函数」。

**实践对照。** 传入的父跨度是 `00f067aa0ba902b7`。本课记录的链为 `client.kv.get`（`772ef51403cbd305`）→ `rpc.kv.get`（`d7f8d255f5cd7994`）→ `storage.get`（`243778c61c4b41d5`），三者共享 `trace_id=4bf92f3577b34da6a3ce929d0e0e4736`。本地 span_id 由运行时生成，读者复跑时会变化；传入的 trace_id 与远程 parent 保持不变。父子关系只描述这次调用树，不能单独推出未记录的日志或指标。

### 2. W3C trace context

**原理。** OpenTelemetry 的默认传播器使用 W3C Trace Context 规定的头字段，并把标识嵌入 `traceparent`：`<version>-<trace-id>-<parent-id>-<trace-flags>`。W3C 规范写明 `traceparent` 以固定长度描述请求在追踪图中的位置，追踪工具即使主要依赖 `tracestate` 中的厂商数据，也必须正确设置 `traceparent`。当前版本 `00` 只定义一个 `sampled` 标志。

**直觉。** 对方不必实现同一套 SDK，只要能解析这四个字段，就能把新跨度挂到同一条追踪。

**实践对照。** 本课使用的入站头是 `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01`。RPC 跨度向外写出 `00-4bf92f3577b34da6a3ce929d0e0e4736-d7f8d255f5cd7994-01`，其中 parent-id 换成了当前 RPC 跨度。本课没有解析 `tracestate`，也没有实现采样器；标志 `01` 只是随头传递。

### 3. 采样与 baggage

**原理。** OpenTelemetry 写明 baggage 用于传播任意键值对，但在跨越服务边界时应避免放入敏感信息（如用户凭据、API 密钥或 PII），因为这些数据可能被记录或发往不可信的下游。W3C 同时指出：只记录部分请求会导致追踪断裂；记录全部出入站请求在负载下会过于昂贵。采样决定哪些追踪被记录，它不是「这条请求不存在」的证明。

**直觉。** baggage 会随调用继续往前走，密钥一旦进入 baggage，泄露面就不再限于当前进程。采样则是成本控制，不能替代审计日志。

**实践对照。** 入站 baggage 为 `region=sha,token=s3cret-token,env=lab`。实现丢弃 `token`，只保留 `region` 与 `env`；导出记录中不出现 `s3cret-token`。本课没有实现采样算法，也没有按比例丢弃跨度。

## 常见误区

1. **把 trace 当作无损记录所有请求的审计日志。** 节点原句即此。W3C 已指出全量记录在负载下成本过高，部分记录又会留下断裂的树。需要完整审计时应使用受控的业务日志或专门流水，而不是假设追踪系统保存了每一次调用。
2. **异步任务会自动继承当前跨度。** 本课故意在工作函数里清空上下文后启动存储跨度，得到新的 `trace_id=f26f90207e28a8bcd6d1128d60534dbe`，父跨度为空，与原来的 `27a1318e3d13699421237a56f5386073` 无法关联。修复后子跨度与父跨度共享 `5849c4b65d88f24498df5beb1fabc3d3`，父标识为 `4b764bca9a788c8d`。这些标识同样会在复跑时改变，判断标准是「是否共享 trace_id 且 parent 指向父跨度」。

## 设计权衡

节点原句是「高采样便于诊断，增加传输与存储成本。」提高采样能看到更多完整链路，也让每个服务多发送、多存储跨度。本课始终记录演示请求，没有给出生产采样率；选择采样率时要同时接受漏掉部分请求和增加观测成本这两面。

## 动手实践

实践目录为 `practice151/`，需要 Python 3。本次使用 CPython 3.13.5。程序是教学用上下文与跨度记录器，不要把它说成已经接入的生产追踪系统或 C++ 编译产物。

```bash
cd practice151
bash test.sh
cat results.json
cat reports/trace.json
```

脚本先解析入站 `traceparent` 与 baggage，串联客户端、RPC 和存储，写一条带 `trace_id` 的日志，再演示异步上下文丢失与修复。日志保存到 `run.log`，成功最后一行为 `ALL_CHECKS_PASSED`。除固定的入站标识外，本地 span_id 与演示用异步追踪 ID 每次运行会不同，读者应以自己生成的 `reports/trace.json` 为准。

本次官方实践目录的链见上文 `reports/trace.json`。对应日志为 `event=kv.request`，`trace_id=4bf92f3577b34da6a3ce929d0e0e4736`，`span_id=d7f8d255f5cd7994`，`status=ok`。

两项验收是「日志可关联 trace_id」以及「敏感数据不放 baggage」。前者对应日志中的 `trace_id` 与 RPC 跨度相同；后者对应 `token` 被丢弃，且导出数据中没有明文令牌。

## 源码与文档阅读

阅读 `trace.py` 中的 `traceparent` 解析、baggage 过滤和异步绑定，对照 `reports/trace.json` 的父子关系。OpenTelemetry Context propagation 用 Frontend 调用 Product Catalog 的例子说明 `traceparent` 的四个字段；W3C Trace Context 则规定头名称、字段宽度和 `sampled` 标志。本实验只实现 `traceparent` 的解析与生成，没有实现 `tracestate`、语言 SDK 的自动插桩或远程导出。文档中的服务名与示例 ID 与本课教学头也不相同。

2026-11-15 在本机核对节点给出的两条官方 URL。表中时间已由 GMT 换算为北京时间。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） |
| --- | ---: | --- | --- |
| [OpenTelemetry — Context propagation](https://opentelemetry.io/docs/concepts/context-propagation/)：Context propagation \| OpenTelemetry | 415605 | 2026-11-15 22:43:37 | （响应未提供） |
| [W3C — Trace Context](https://www.w3.org/TR/trace-context/)：Trace Context | 100612 | 2026-11-15 22:43:37 | 2021-11-23 21:50:04 |

两条最终 URL 均与节点所列相同，HTTP 200，跳转 0 次。节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。W3C 页面的 Last-Modified 属于该推荐规范文本；实验是本地教学实现，规范示例中的标识不保证与本课记录相同。

## 自测题

1. **父跨度 ID 和 trace ID 有何不同？** trace ID 标识整条追踪，父跨度 ID 标识树中的上一层节点。共享 trace ID 但父标识为空，通常表示出现了新的根跨度。
2. **日志里有 trace_id 是否证明链路完整？** 只能证明这条日志能按 ID 检索。若异步存储跨度用了另一个 trace_id，日志仍然对不上那一段工作。
3. **为什么令牌不能放进 baggage？** baggage 会继续向下游传播，OpenTelemetry 明确提示凭据、API 密钥和 PII 可能被记录或发往不可信服务。

## 推荐资源

- [OpenTelemetry — Context propagation](https://opentelemetry.io/docs/concepts/context-propagation/)：用于核对parent/child span与W3C trace context，将结论落实到本节点实验。英文，进阶，免费。
- [W3C — Trace Context](https://www.w3.org/TR/trace-context/)：核对 `traceparent` 格式与跨服务传播约束。英文，进阶，免费。

## 本课小结

用 W3C `traceparent` 把客户端、RPC 和存储挂到同一条追踪，并让日志带上同一个 trace_id；异步边界必须显式传递上下文，否则会出现无法关联的根跨度。敏感字段不要写入 baggage。下一课是第 152 课 j-ci（CI 与供应链检查）。
