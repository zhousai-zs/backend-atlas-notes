# 第 163/208 课：结构化输出与校验

> 序号 163/208 · id `k-structured` · 领域 K · 阶段 2 · 难度 基础 · 子类 模型接口与输出
> 前置：[`k-api`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/161.html)、[`k-prompt`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/162.html)
> 节点摘要：受 schema 约束的生成有助于得到可解析结构，业务合法性仍需校验，拒答和中断需单独处理。

## 导读

把模型输出接进工单、回滚或自动修复之前，先问两件不同的事：它是不是一张合法的表，以及表上的内容是不是真的。JSON Schema 只回答前者。OpenAI Structured Outputs 进一步约束的是「已完成、且非拒答」的输出符合 schema，并不替你处理拒答、截断、API 错误，以及 schema 表达不了的业务规则。

## 学习目标

1. 解释 JSON Schema 及适用边界。
2. 独立完成实践：用 schema 输出诊断结论，再用程序检查证据 ID 是否真实存在。

## 为什么后端要懂

格式通过只说明解析器没有摔倒。若把「字段齐全、枚举合法」当成「可以自动关单」，一条编造的证据 ID 就会驱动下游。拒答和 `finish_reason=length` 更危险：它们往往仍能吐出半截 JSON，绝不能当成成功对象执行。

## 核心知识点

### 1. JSON Schema 约束的是形状

**直觉。** Schema 是合同的语法部分：必填字段、类型、枚举、数组形状。它不认识你的证据库，也不知道「status=ok 时必须有证据」这类跨字段业务规则，除非你把规则也写成 schema（即便如此，证据是否真实存在仍然在 schema 之外）。

**原理与代码。** 实践使用 `jsonschema` 4.26.0 与 pydantic 2.14.3 描述诊断对象：`incident_id`、`status`（`ok` / `needs_review` / `refused`）、`conclusion`、`evidence_ids`、`confidence`、`open_questions`。`schema.py` 里的 JSON Schema 与 pydantic 模型是同一份合同的两种写法；`validate.py` 先做格式校验，再查 `fixtures/evidence.json` 里的真实 ID。

**边界。** 合法 JSON 不等于事实正确——这是本课验收原话。空的 `evidence_ids` 配上 `status=ok`，形状可以完全合法，门控仍必须拦住。

### 2. 格式校验与语义校验要拆开

**直觉。** 格式说「这是一个证据 ID 列表」；语义说「这些 ID 此刻存在，且能支撑结论」。第二步是普通的后端查找，不是再写一段提示词。

**实践对照。** 用例 `schema_ok_fake_evidence` 的 JSON 通过 schema（`E999` 类型和字段都对），但证据表里没有 `E999`。`TicketingDownstream` 只在门控全部通过后调用，整个批次里下游只被调用 1 次，就是那条真实证据齐全的 `good_complete`。假证据、缺字段、截断、错误枚举都不会创建工单。

**反例。** 若先调下游再校验，或把「schema 通过」写成 `accepted=true`，假证据就会变成一次真实副作用。本课把下游做成带计数器的对象，断言 `downstream_invocations==1`，避免这种顺序错误被测试放行。

### 3. 拒答与不完整输出不是「差不多的成功」

**直觉。** 模型可以拒绝回答，也可以在长度上限处被切断。这两种都不是一份可消费的诊断对象。

**官方口径（当日抓取）。** 2026-11-15 抓取 OpenAI Structured Outputs（`https://platform.openai.com/docs/guides/structured-outputs`，Date 2026-11-15 23:12:32 Asia/Shanghai，Last-Modified 2026-11-14 15:14:36 Asia/Shanghai）。文档写明：Structured Outputs 约束的是已完成且非拒答的输出符合 JSON Schema；应用仍须自行处理拒答、不完整响应、API 错误，以及 schema 未表达的业务规则。本课按这句话实现门控，而不是假定 `parse()` 一定返回对象。

**实践对照。** `sdk_contract.py` 用官方 `openai` 3.51.0 的 `chat.completions.parse`，经 `httpx.MockTransport` 注入三种响应，不发网络、不读密钥：

- 合法完成：解析出 `Diagnosis` 对象，再送入同一套门控，通过后才允许下游；
- 官方风格拒答：`parsed is None`，不得调用下游；
- 形状合法但证据为假：SDK 仍然能 parse（这只说明形状），门控拒绝。

另两组固定夹具覆盖截断 JSON 与 `finish_reason=length`：`evaluate_case` 的结果是「不完整」，下游计数不变。

**未在本机验证。** 真实托管模型是否会遵守 strict structured outputs，以及 `responses.parse` / `chat.completions.parse` 的付费调用，均未执行。用户路径（不要在本课脚本里读密钥）：安装 `openai==3.51.0`，自行设置 `OPENAI_API_KEY`，对受支持的模型调用 `client.chat.completions.parse(..., response_format=Diagnosis)` 或 `client.responses.parse(..., text_format=Diagnosis)`，再把原始结果（含拒答和 `finish_reason`）送进 `validate.evaluate_case`。MockTransport 的解析成功不能当作模型守约的证据。

## 常见误区

- 把 schema 通过等同任务正确。
- 拒答或截断时仍把半截 JSON 送进写路径。
- 在提示词里「要求输出 JSON」，却不做程序校验。
- 用自家写死的固定输出证明「模型会遵守 schema」。

## 设计权衡

严格 schema 简化消费，也可能限制开放探索。诊断这类要进工单的任务适合严格合同；头脑风暴或尚未定形的分类则不必先锁死枚举。无论松紧，业务规则和证据存在性都要留在代码里。

## 动手实践

```bash
cd practice163
bash test.sh
# 不读取、不发送 API 密钥；官方 SDK 只走 MockTransport
```

依赖：`openai` 3.51.0、`pydantic` 2.14.3、`jsonschema` 4.26.0（`/workspace/atlas/cmvenv`）。

## 源码与文档阅读

`schema.py` 的 JSON Schema 与 pydantic 模型；`validate.py` 的两段校验与 `TicketingDownstream`；`sdk_contract.py` 的 `chat.completions.parse` + MockTransport；`fixtures/cases.json` 与 `fixtures/evidence.json`。

## 自测题

**1. 一条 JSON 通过了 schema，为什么还不能创建工单？**

答案：schema 只检查形状。证据 ID 可能不存在，或 `status=ok` 却没有证据。本课的假证据用例会通过 schema、被门控拒绝，下游调用次数仍为 1。

**2. Official `parse()` 返回了对象，是否等于任务成功？**

答案：不等于。`parse()` 保证的是完成且非拒答时符合 schema。假证据仍然能被 parse，必须再做业务校验。拒答时 `parsed is None`，不能当对象用。

**3. 遇到 `finish_reason=length` 或半截 JSON，正确做法是什么？**

答案：视为不完整，不执行下游。需要的话按业务重试或改走人工，而不是补全括号后当成功。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | Last-Modified (Asia/Shanghai) | 说明 |
| --- | --- | --- | --- |
| [OpenAI — Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs) | 2026-11-15 23:12:32 | 2026-11-14 15:14:36 | 当日口径：约束已完成、非拒答输出；拒答、截断、API 错误与业务规则仍由应用处理。英文，基础，免费。 |

## 本课小结

结构化输出让程序「拿得到一张表」，不能让程序「相信表上的话」。本课用 jsonschema / pydantic 做格式检查，用证据库做语义检查，用官方 SDK 的 MockTransport 演示 `parse` 与拒答，并把下游调用限制在唯一一条真正通过门控的用例。托管模型是否守约，需要你在真实模型路径上复现，本机未测。
