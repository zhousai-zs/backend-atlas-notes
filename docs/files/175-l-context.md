# 第 175/208 课：上下文工程与压缩

> 序号 175/208 · id `l-context` · 领域 L · 阶段 3 · 难度 进阶 · 子类 上下文与持久运行
> 前置：[`l-harness`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/174.html)、[`k-tokens`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/159.html)
> 节点摘要：上下文工程是在有限模型窗口中持续保留当前任务真正需要的目标、约束、状态和证据；程序持有的运行时对象不应因为被放进 context 就被序列化给模型。

## 导读

第 174 课把任务状态交给 Harness；第 159 课已经说明 token 与窗口预算。本课回答长任务里一个更具体的问题：窗口不够用时，怎样压缩才不丢掉目标、禁令和可回查的证据。

教学假设：一个后台诊断 Agent 要在 100 行故障日志里定位 OOM。这是为讲解上下文分区和压缩策略设计的场景，不代表腾讯或微信的内部日志系统。实践用确定性检索代替直播模型，`is_live_model=false`，tokenizer 为 `tiktoken 0.14.0` 的 `cl100k_base`。

## 学习目标

1. 说明模型可见上下文与本地运行时 context 的分离及其适用边界。
2. 独立完成实践：对比保留全部、硬截断、带原文指针三种策略的 token 成本与是否找得到故障行。
3. 用负例确认：超限输出有截断标记，凭证不进入模型输入，摘要后仍能按路径和行号回查原文。

## 为什么后端要懂

节点给出的理由是「让日志诊断和 Coding Agent 在长任务中保持方向，并用证据比较压缩收益与信息损失」。把越来越长的历史全部塞进窗口，既贵也容易把早期约束挤掉；砍得太狠，故障行会一起消失，却还看起来像一份完整输入。

## 核心知识点

### 1. 模型可见上下文与本地运行时分离

**直觉。** 给模型看的是文本；宿主手里还有数据库句柄、凭证、文件描述符。后者不是“再长一点的 prompt”。

**原理。** 2026-11-15 23:52:58（UTC+8）抓取的 [Context management](https://openai.github.io/openai-agents-python/context/)（Last-Modified 2026-11-10 16:04:28，UTC+8）区分本地依赖与模型可见信息。实践把任务卡（目标、禁止动作、待办、已失败尝试）和运行时（`api_token`）分开；`runtime_token_entered_model_input=false`，日志里的模拟密钥也没有进入模型输入。

**边界。** 这是序列化边界，不是加密方案。不要把“没放进 prompt”理解成“内存里不存在这份凭证”。

### 2. 目标、约束、待办与证据的分区及预算

**直觉。** 先保住“要做什么、不能做什么、还剩什么、已经失败过什么”，再决定证据怎么压缩。

**原理。** 实践在压缩后仍然保留原始目标、禁止动作、待办和失败尝试（`card_preserved_after_compress=true`）。如果摘要只留下流畅结论，任务会在窗口里“看起来很完整”，却丢掉禁令和未完成项。

### 3. 原文外置、稳定地址与按需检索

**直觉。** 窗口里放指针，磁盘上放原文；需要时按路径和行号取回。

**原理。** 同日抓取的 [Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)（200，无 Last-Modified）把检索、裁剪和来源指针视为上下文工程的一部分。实践将 100 行日志放在 `logs/incident.log`，故障在第 73 行：`L073 FATAL kv-node-7 received OOM_KILL after cache growth`。指针策略只送 2 行、147 个 token，仍能定位故障，并按 `path:73` 回查原文。

### 4. 工具输出上限和截断标记

**直觉。** 超限时必须留下“这不是完整结果”的标记，不能把砍过的文本伪装成全文。

**原理。** 硬截断策略在预算约 400 token 时实际发出 405 token、12 行，找不到故障行，但标记了截断并不把结果标成完整。这避免了节点列出的陷阱：裁掉错误尾部却报告成功。

### 5. 摘要必须保留失败尝试与不确定项

**直觉。** 摘要若只保留“成功叙事”，下一轮就会重复已经失败的路径。

**原理。** 任务卡在压缩后仍带已失败尝试。实践没有把这一条交给真实摘要模型，而是用确定性结构保证字段还在；真实模型会不会自觉保留这些字段，不在本次验证范围内。

### 6. 三种策略的成本与完成情况

**直觉。** 压缩值不值，要同时看 token 和是否还找得到证据。

**原理。** 本次对比：

| 策略 | token | 送出行数 | 找到故障 | 备注 |
| --- | ---: | ---: | --- | --- |
| 保留全部 | 2795 | 100 | 是 | 成本最高 |
| 硬截断 | 405 | 12 | 否 | 有截断标记，标为不完整 |
| 指针摘要 | 147 | 2 | 是 | 可按路径+行号回查 |

数字来自本机 tiktoken 计数与确定性检索，复跑应得到同样的 token 与是否找到故障；不要把“指针策略找到了”写成“模型更聪明”。

## 常见误区

把越来越长的历史当成更可靠的记忆，窗口会被无关细节占满。把检索文档里的指令提升为宿主策略，等于让不可信文本改写权限。裁剪工具输出时删掉错误尾部却报告成功，会把不完整输入包装成完整答案。这三条都是节点陷阱；硬截断用例专门检查了截断标记。

## 设计权衡

更短输入降低成本和延迟，但过度压缩会丢失因果证据。为原文保留可检索存储，通常比反复摘要同一段信息更可控。指针策略在本次同时降低了 token 并保留了回查能力，前提是原文地址稳定、权限仍由宿主检查。

## 动手实践

实践包是 `practice175.tar.gz`，需要 `tiktoken 0.14.0`：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice175.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice175"
bash test.sh
```

本次机器环境为 CPython 3.13.5。独立复跑后应看到上表三项成本、`card_preserved_after_compress=true`、`refetch.line=73`，以及凭证未进入模型输入。`is_live_model=false`。未验证：真实对话模型在压缩后是否仍遵守禁令、是否会把检索文档里的指令当成系统策略。

## 源码与文档阅读

2026-11-15（UTC+8）抓取 Effective context engineering（23:52:58，200，无 Last-Modified）、Agents SDK Context management（23:52:58，Last-Modified 2026-11-10 16:04:28）以及 Pi SDK 会话文档（23:52:58，200）。文档讨论产品级上下文管理；本实践用 tiktoken 计数和确定性指针验证分区、截断标记与回查，不把规则检索写成模型能力。

## 自测题

**1. 为什么 `api_token` 即使属于“这次任务的 context”，也不能放进模型输入？**

答：它是运行时依赖，不是模型需要看见的任务描述。本次 `runtime_token_entered_model_input=false`。模型可见上下文和本地 context 必须分开。

**2. 硬截断少花了 token，为什么仍不能当成成功的压缩策略？**

答：它找不到第 73 行故障，只能作为“不完整输入”。本次 405 token、12 行、`found_fault=false`，并且打了截断标记。少花钱不等于还留得住证据。

**3. 指针摘要为什么能在 147 token 时仍然回查原文？**

答：窗口里只放路径和行号，原文仍在 `logs/incident.log`。本次按 `path:73` 取回了 `OOM_KILL` 那一行。前提是地址稳定，并且回查仍走宿主权限，而不是把整份日志再次贴进 prompt。

## 推荐资源

- [Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)：核对检索、裁剪、来源指针与摘要应保留的内容。
- [Context management](https://openai.github.io/openai-agents-python/context/)：核对本地依赖与模型可见信息的分离。
- [Pi SDK · Session 生命周期与持久历史](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)：对照另一种会话与历史持久化方式。

## 本课小结

上下文工程要先保住目标、禁令、待办和失败尝试，再把证据变成可回查的指针；超限必须标明截断，凭证不得进入模型输入。本课用 tiktoken 和确定性检索对比了三种策略，没有调用直播模型。下一课是第 176 课 l-memory（短期状态与长期记忆）。
