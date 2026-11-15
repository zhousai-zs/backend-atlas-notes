# 第 200/208 课：技术决策与 ADR

> 序号 200/208 · id `n-adr` · 领域 N · 阶段 4 · 难度 深入 · 子类 架构决策
> 前置：[`n-requirements`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/197.html)
> 节点摘要：ADR 记录上下文、备选方案、选择与后果，使后来者能理解为什么当时这样设计。

## 导读

第 197 课要求组件能回到需求。决策本身也要留下「当时为什么这样选」。本课按 [adr.github.io](https://adr.github.io/) 的结构，为 Reactor 线程模型写一条 ADR，比较每连接线程、有界线程池和协程，并用一次真实测量支撑关键假设。教学假设：测量在 Python TCP 回显服务上进行，**不是** C++ epoll/coroutine 的生产基准。`is_live_model=false`。

## 学习目标

1. 说明可逆与不可逆决策及适用边界，以及何时复审。
2. 独立完成实践：对 Reactor 线程模型写 ADR，比较每连接线程、线程池和协程。
3. 验收：包含被否决方案的原因，并用数据说明关键假设。

## 为什么后端要懂

只留下结论的审批表，几个月后无人能判断前提是否已变。ADR 把上下文、备选和后果写下来，复审才有起点。

## 核心知识点

### 1. ADR 不是结论清单

**直觉。** 后来者要的是「当时看到了什么」。

**原理。** [Architectural Decision Records](https://adr.github.io/) 收集多种模板，共同部分通常包括状态、上下文、决策、后果与备选。本课校验器要求这五段都非空，并且正文出现测量数据。一份只有 `## Decision / Use coroutines` 的稿件会被判失败（缺 status/context/consequences/alternatives，也没有测量）。合格 ADR 还写了复审触发条件：并发目标变化 10 倍、尾延迟不达标、或实现语言变化。语言或负载变了，这条决策应按可逆决策重新测量，而不是当作不可更改的教条。

### 2. 被否决方案也要有原因和数据

**直觉。** 「我们选了线程池」不够，还要说另外两个为什么不作为默认。

**原理。** 实践在约 80 路并发、每模型 300 个短连接下实测三次。三次均 300/300 成功。按 ok 数与 qps 选出当次胜者（常见是 thread_pool），并在 Alternatives 中写入三组实测数字。本次正式树运行的胜者与 qps 见 `reports/bench.json` 和 ADR 正文。必须写明：这是 Python 运行时的教学测量，不能把 qps 绝对值搬到 C++ Reactor。

## 常见误区

- 把 ADR 写成仅有结论的审批表。
- 不记录被否决方案，或只用形容词而不放数据。
- 把一次 Python 测量写成已经验证过的 C++ 生产模型。

## 设计权衡

文档需足够简洁才能保持更新，又要保留关键证据。本课把测量表直接嵌进 Alternatives，避免证据和结论分家。

## 动手实践

实践包在 `practice200/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice200 "$tmp_dir/"
cd "$tmp_dir/practice200"
bash test.sh
```

本次实测：三种模型都跑完；结论型 ADR 被拒；完整 ADR 被接受并包含 qps 数据。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [ADR — Architectural Decision Records](https://adr.github.io/)：HTTP 200，`Date` 2026-11-15 16:41:27 GMT（Asia/Shanghai 2026-11-16 00:41:27），`Last-Modified` 2026-11-02 06:20:34 GMT（Asia/Shanghai 2026-11-02 14:20:34）。该站是模板索引，并不规定必须用某一家格式；本课采用常见的 Status/Context/Decision/Consequences/Alternatives 五段。

## 自测题

**1. 只有一句「使用协程」的 ADR 缺了什么？**

答：状态、上下文、后果、备选和数据。本次这样的稿件无法通过校验。

**2. 关键假设「每连接线程更贵」如何证明？**

答：用同一负载下的实测。三次模型的 qps 都写入 ADR；通常有界池高于每连接线程，但以当次 `reports/bench.json` 为准。

**3. 这条 ADR 什么时候必须复审？**

答：并发目标数量级变化、尾延迟不达标、或实现语言从本次的教学运行时换成 C++ 等。那是决策可逆的边界。

## 推荐资源

- [ADR — Architectural Decision Records](https://adr.github.io/)（英文，文档，深入，免费）：模板与社区索引。

## 本课小结

ADR 要把被否决方案和当时的数据留下，并写清复审条件。下一课是第 201 课 n-evolution（架构演进与迁移策略）。
