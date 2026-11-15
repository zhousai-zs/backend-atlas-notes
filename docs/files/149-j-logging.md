# 第 149/208 课：结构化日志与敏感信息

> 序号 149/208 · id `j-logging` · 领域 J · 阶段 2 · 难度 基础 · 子类 可观测性
> 前置：[`j-gdb`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/148.html)
> 节点摘要：结构化日志以稳定字段记录事件，关联 request_id 并控制级别、采样和敏感数据暴露。

## 导读

上一课用匹配的二进制和 core 定位崩溃。服务在未崩溃时也需要可检索的事件记录：一次 KV 请求花了多久、成功还是失败、能否与某条追踪对应。日志既要足够稳定以便查询，又不能在磁盘故障时拖垮请求，更不能把密钥或完整用户数据写进文件。

教学假设：内容后台有一个教学用 KV 接口，请求处理完成后写入一条结构化日志。以下组件、字段和故障均为教学设计，不代表腾讯或微信内部实现。磁盘满由写入配额模拟，不是真实块设备耗尽。

## 学习目标

1. 解释字段 schema及适用边界
2. 独立完成实践：为 KV 请求记录延迟、状态与 trace_id，模拟磁盘满处理。

## 为什么后端要懂

节点给出的理由是「让故障可检索且不拖垮服务。」检索依赖稳定字段；服务不被拖垮依赖写入失败时的有界等待和丢弃，而不是在热路径上无限重试。

## 核心知识点

### 1. 字段 schema

**原理。** 结构化日志把一条事件拆成约定好的字段，而不是一行无法拆分的句子。OpenTelemetry Logs Data Model（文档标题 *Logs Data Model | OpenTelemetry*，状态 Stable）把一条日志记录定义为若干字段，其中包括 `Timestamp`（事件发生时间）、`ObservedTimestamp`（被采集系统观察到的时间）、`TraceId` / `SpanId` / `TraceFlags`、`SeverityText` / `SeverityNumber`、`Body`、`Resource`、`InstrumentationScope`、`Attributes` 和 `EventName`。规范说明 `Timestamp` 以源时钟计量，在源时间戳未知时可以缺省。

**直觉。** 查询「某条 trace 下状态为失败的请求」依赖字段名字稳定；每次更换键名或把延迟写进自由文本，都会让检索失效。

**实践对照。** 本课日志固定七个键：`ts`、`severity`、`event`、`trace_id`、`latency_ms`、`status`、`attrs`。`event` 取值 `kv.request`，对应规范里标识事件类别的 `EventName`；延迟和状态放在稳定顶层字段，便于过滤。这是教学 schema，不是完整的 OTel 导出记录：没有 `SeverityNumber`、`Resource`，时间戳是北京时间的 RFC 3339 文本，也不是规范中的 UNIX 纪元纳秒。

### 2. 异步写入与丢弃策略

**原理。** 请求线程只负责把记录放入有界队列，由后台线程落盘。队列满或写入返回错误（本课用配额触发 `ENOSPC`）时丢弃并计数，而不是让请求一直等到磁盘恢复。

**直觉。** 日志是观测手段。观测通道自己把延迟放大到秒级，会改变它本应观察的服务行为。

**实践对照。** 模拟磁盘满时，20 次 KV 请求全部返回成功；异步路径写入 1 条、丢弃 19 条（其中 19 次为写错误），耗时 0.0004 秒。对照的同步日志在同样的满盘条件下写入 0 条，触发 8 次阻塞，各睡眠 0.15 秒，总耗时 1.2018 秒。两次都是本机短时测量，只用于对照请求是否被日志路径拖住，不是生产吞吐或磁盘性能数据。

### 3. 脱敏与保留期

**原理。** 写入前删除或替换密钥类字段，截断用户标识；文件保留最近 N 条或按策略删除更早记录。规范把额外信息放在 `Attributes` 中，并不要求记录完整用户资料或密钥。

**直觉。** 日志一旦离开进程，访问范围往往大于原请求处理代码。多写细节有助于排障，也会扩大泄露面。

**实践对照。** 请求携带 `token=s3cret-token` 和 `user=alice@example.com`。落盘后 `token` 为 `[REDACTED]`，`user` 为 `alice@ex…`，文件中不出现明文令牌或完整邮箱。保留策略将 5 条示例记录裁为最近 2 条，结果见 `reports/retained.jsonl`。这是按条数保留的教学策略，不是天数或合规归档方案。

## 常见误区

1. **在热路径同步打印海量日志。** 节点原句即此。本课同步对照在磁盘满时把请求路径拉长到约 1.2 秒；异步丢弃则让请求在 0.001 秒量级返回。热路径上的无界同步写会把观测变成新的故障源。
2. **字段能写进去就算结构化。** 若每条请求使用不同键名，或把延迟、状态、令牌塞进一句自然语言，检索和脱敏都会失效。先固定 schema，再决定哪些键允许出现在 `attrs` 中。

## 设计权衡

节点原句是「更详细日志便于诊断，也增加成本和隐私风险。」本课为每条请求记录操作、键名、截断后的用户标识、延迟和状态，能把一次 GET 与对应 trace 对齐；代价是磁盘与队列占用，以及用户字段即便截断仍可能结合其他数据识别个人。生产系统还要考虑采样、加密存放和访问控制，本课未实现这些。

## 动手实践

实践目录为 `practice149/`，需要 Python 3。本次使用 CPython 3.13.5。程序是教学用 Python，不要把它说成 C++ 编译或系统日志守护进程。

```bash
cd practice149
bash test.sh
cat results.json
cat reports/schema-sample.jsonl
cat reports/retained.jsonl
```

脚本先写两条成功的 KV 请求日志并检查 schema 与脱敏，再模拟磁盘满下的异步丢弃与同步阻塞对照，最后按保留策略裁剪示例文件。日志保存到 `run.log`，成功最后一行为 `ALL_CHECKS_PASSED`。时间戳、延迟和耗时依赖本机时钟与调度，读者应以自己生成的记录为准。

本次官方实践目录得到的结果如下。

`reports/schema-sample.jsonl` 中的两条记录（时间戳为北京时间）：

```json
{"ts":"2026-11-15T22:47:39.079+08:00","severity":"INFO","event":"kv.request","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","latency_ms":0.002,"status":"ok","attrs":{"op":"SET","key":"theme","user":"alice@ex…","token":"[REDACTED]"}}
{"ts":"2026-11-15T22:47:39.079+08:00","severity":"INFO","event":"kv.request","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","latency_ms":0.001,"status":"ok","attrs":{"op":"GET","key":"theme","user":"alice@ex…","token":"[REDACTED]"}}
```

`results.json` 中的对照数字：`async_written=1`，`async_dropped=19`，`async_errors=19`，`async_elapsed_s=0.0004`；`sync_written=0`，`sync_blocked_n=8`，`sync_elapsed_s=1.2018`；`retention_before=5`，`retention_keep=2`，`retention_after=2`。

两项验收是「日志路径故障不会无限阻塞请求」以及「无明文密钥和完整用户数据」。前者对应异步路径在磁盘满时仍于 0.0004 秒内返回全部 20 个请求；后者对应样本中的 `[REDACTED]` 与截断用户字段，以及脚本对明文令牌、完整邮箱的否定检查。队列长度、入队超时和配额都是教学参数，换一组数字会改变写入与丢弃的计数，但不能把「请求必须等到日志成功」当成通过条件。

## 源码与文档阅读

阅读 `logger.py` 中的 schema、脱敏和有界队列，以及 `service.py` 如何为 KV 请求填入 `latency_ms`、`status` 与 `trace_id`。对照 `reports/schema-sample.jsonl` 与 `results.json`。

OpenTelemetry Logs Data Model 给出日志记录的字段定义，例如 `TraceId` 为请求追踪 ID，`SeverityText` 为严重性文本（也称为日志级别），`Attributes` 为事件的附加信息。本实验用 JSON 行和教学键名落盘，没有实现该规范的导出协议、SeverityNumber 映射或 Resource。文档也不规定磁盘满时的丢弃算法，丢弃策略属于本课实现。

2026-11-15 在本机核对节点给出的官方 URL。表中时间已由 GMT 换算为北京时间。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） |
| --- | ---: | --- | --- |
| [OpenTelemetry — Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)：Logs Data Model \| OpenTelemetry | 94675 | 2026-11-15 22:43:37 | （响应未提供） |

最终 URL 与节点所列相同，HTTP 200，跳转 0 次。节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。文档是当天在线版本；实验使用本地 Python 教学记录器，文档中的字段类型与示例不保证与本课 JSON 行相同。

## 自测题

1. **为什么延迟和状态要放在固定字段里，而不是写进一句话？** 固定字段才能按状态或耗时范围检索；自由文本难以及稳定解析，也更容易把敏感信息藏进句子。
2. **磁盘满时请求还能成功，是否说明日志已经可靠落盘？** 不能。本课异步路径在故障时丢弃；需要另看丢弃计数和实际文件行数，不能把请求成功当成日志成功。
3. **截断邮箱后是否已经没有隐私风险？** 不一定。截断降低了直接泄露完整地址的可能，仍可能与其他字段结合识别用户；密钥则应直接删除或替换，而不是截断。

## 推荐资源

- [OpenTelemetry — Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)：用于核对字段 schema与异步写入与丢弃策略，将结论落实到本节点实验。英文，基础，免费。

## 本课小结

先约定稳定字段并写入延迟、状态和 trace_id，再在写入路径上使用有界等待与丢弃，避免日志故障阻塞请求；密钥删除、用户数据截断和保留策略限制暴露范围。下一课是第 150 课 j-metrics（Metrics 与指标基数）。
