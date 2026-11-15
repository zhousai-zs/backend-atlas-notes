# 第 196/208 课：Agent 服务的 SLO 与追踪

> 序号 196/208 · id `m-observability` · 领域 M · 阶段 5 · 难度 专家 · 子类 工具基础设施
> 前置：[`m-ops`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/195.html)、[`j-tracing`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/151.html)
> 节点摘要：Agent 服务同时观测 HTTP/RPC、模型、工具与任务结果，成功率必须以实际任务验收而非 HTTP 200 定义。

## 导读

第 195 课产出 P10 诊断，第 151 课介绍追踪。本课为这类任务建立面板：任务成功率、工具错误率、时长和每成功任务成本。教学假设：轨迹来自 OpenAI Agents SDK 0.24.0 的官方 tracing API，但导出器是本地 `TracingProcessor`，不向 OpenAI 上传，也不读取 API 密钥。token 费用是脚本化数字，不是现场模型账单。`is_live_model=false`。

## 学习目标

1. 说明 task success SLI 及适用边界：HTTP 200 不是任务成功。
2. 独立完成实践：为 P10 建立任务成功率、工具错误率、总时长和每成功任务成本面板。
3. 验收：区分模型失败和工具失败；trace 去除敏感上下文。

## 为什么后端要懂

只监控模型调用成功率，会把「前端 200 但根因写错」算成健康。Agent 服务的用户体验是任务是否被验收，而不是某一跳的 HTTP 状态。

## 核心知识点

### 1. 任务成功率不是 HTTP 成功率

**直觉。** 门开着不代表活干完了。

**原理。** 实践跑 4 个离线任务，全部走了 HTTP 200：1 个任务验收通过，1 个工具与模型都正常但根因写错，1 个模型失败，1 个工具失败。于是 `http_200_rate=1.0`，而 `task_success_sli=0.25`。面板同时给出 `tool_error_rate=0.25`、`model_error_rate=0.25`，以及 `cost_per_successful_task_usd=0.0005`（仅按成功任务分摊脚本化费用）。如果只看模型 span 成功率，会漏掉工具失败和验收失败。

这些 span 来自官方 API：`trace()`、`generation_span()`、`function_span()`、`custom_span()`。`set_trace_processors([LocalProcessor()])` 替换了默认的托管导出，避免任何密钥或外网上传。

**边界。** 费用与 token 不是现场模型计量；也没有接 Grafana 或 OpenAI 托管 Trace。

### 2. 轨迹里的敏感字段必须去掉

**直觉。** 完整轨迹利于归因，也会把密钥和用户令牌写成日志。

**原理。** 模型输入里放了 `Bearer aabbccdd-secret`，工具输入里放了演示用的 `sk-demo-...`。本地 processor 在 `on_span_end` 导出时用正则替换为 `[REDACTED]`。复跑后 `reports/spans.json` 不再包含这两段明文，但保留 span 类型与错误信息，以便区分 model/tool 失败。

## 常见误区

- 只监控模型调用成功率或 HTTP 200。
- 把脚本化 token 费用写成真实账单。
- 默认导出到外部追踪后端却未检查是否发送了提示词或密钥。
- 用固定输出冒充模型质量。

## 设计权衡

完整轨迹便于归因，也增加数据治理成本。默认应记录结构化 span 与错误码，提示词和凭证默认脱敏或采样后进入更严格的存储。

## 动手实践

实践包在 `practice196/`。需要 cmvenv 中的 `openai-agents==0.24.0`。`test.sh` 会 `unset` API 密钥环境变量。

```bash
tmp_dir=$(mktemp -d)
cp -a practice196 "$tmp_dir/"
cd "$tmp_dir/practice196"
bash test.sh
```

本次实测：任务 SLI 0.25，HTTP 200 率 1.0；模型失败与工具失败各 1 次；敏感字段已脱敏。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [OpenAI Agents SDK — Tracing](https://openai.github.io/openai-agents-python/tracing/)：HTTP 200，URL 未重定向，`Date` 2026-11-15 16:41:27 GMT（Asia/Shanghai 2026-11-16 00:41:27），`Last-Modified` 2026-11-10 08:04:28 GMT（Asia/Shanghai 2026-11-10 16:04:28）。官方文档描述了 processor 与禁用导出；本次采用本地 processor，未调用托管导出。

## 自测题

**1. 四个任务都返回 HTTP 200，任务成功率为什么不是 100%？**

答：SLI 以任务验收为准。本次只有 1 个任务通过验收，SLI 为 0.25。

**2. 如何区分模型失败和工具失败？**

答：看 `generation` span 与 `function` span 的错误。本次各 1 次，分记为 model_error_rate 与 tool_error_rate。

**3. 为什么还要做脱敏？**

答：span 输入可能含令牌或密钥。本次明文已被替换为 `[REDACTED]`，否则轨迹不能离开本机。

## 推荐资源

- [OpenAI Agents SDK — Tracing](https://openai.github.io/openai-agents-python/tracing/)（英文，文档，专家，免费）：官方 span 与 processor。
- 第 151 课追踪：把轨迹当作服务的一部分来治理。

## 本课小结

Agent 服务的 SLO 要从任务验收算起，并分开模型、工具和 HTTP。领域 M 在本课结束。下一课是第 197 课 n-requirements（系统设计与需求澄清），进入领域 N。
