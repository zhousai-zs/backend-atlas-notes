# 第 169/208 课：Agent 循环与 Workflow

> 序号 169/208 · id `l-loop` · 领域 L · 阶段 2 · 难度 基础 · 子类 Agent执行模型
> 前置：[`k-structured`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/163.html)
> 节点摘要：Agent 与 Workflow 的关键差异在于谁选择下一步动作：固定流程由代码限定分支，Agent 循环允许模型根据观察选择工具、继续调查或结束任务。

## 导读

Workflow 是代码写死的路径：检索、阅读、回答或拒答，没有「再查一轮」的自由。Agent 循环把「下一步做什么」交给策略——在生产里那通常是模型，在本课里是脚本化策略，节点允许先这样做。脚本策略不是模型智能，本课每一份结果都写了 `scripted_policy_is_llm=false`。模型说「完成」也只是候选，不能代替业务验收。

## 学习目标

1. 分清「谁选择下一步」以及四种必须覆盖的终态。
2. 独立完成实践：先实现文档检索 → 带出处回答的固定 Workflow，再加入允许继续检索的 Agent 版本；用脚本模型输出固定动作，注入空结果、异常和无限检索，并做固定动作回放。

## 为什么后端要懂

把本可写死的认证、写入、发布包进一个不可预测的循环，会让失败难以复盘。加入 Agent 之前，要先有可控基线，才能证明动态规划到底改善了什么（成功率、步数、调用次数），而不是只多了一层「看起来更智能」。

## 核心知识点

### 1. 谁选择下一步

**原理。** `harness.py` 里循环的输入是问题，动作集合是 `retrieve` / `read` / `answer` / `abstain` / `cancel`，观察是工具返回或错误，停止条件是终态或步数 / 调用预算。`workflow_policy` 把阶段计数写死：第 1 步检索，第 2 步阅读 top-1 或拒答，第 3 步根据是否读到正文回答或拒答，不会因为「还想再搜」而加步。`agent_good` 则根据当前 `hits` / `returned` 决定是检索、继续读下一段，还是结束——这就是 Agent 侧「谁来选下一步」的最小模型，只是决策函数是脚本。

**实践对照。** 同一三问（kv-node-7 为何被杀、就绪探针会否重启、经理手机号）上，两条路径的终态都是 success（前两问 answered 并引用原文，第三问 abstain）。Agent 脚本在有多段命中时会继续 `read`，平均调用次数高于固定 Workflow。这是成本差异，不是「Agent 更聪明」。

### 2. 四种终态，以及步数上限

验收要求至少覆盖成功、预算耗尽、工具失败、取消：

- **success**：Workflow 在 Q1 引用 `D01-P01` 回答；空检索则 success + `abstain`（拒答也是成功终态，不是失败）。
- **budget_exhausted**：`agent_infinite` 每步都 `retrieve`，`max_steps=5` 时在第 5 步停下，`stop_reason=max_steps`；把 `budget_calls` 设为 1 时，好策略在第二次工具调用前耗尽，`stop_reason=budget_calls`。模型（或脚本）不断要求调用，必须在配置的步数或预算内停住。
- **tool_failure**：检索后端 `mode=error` 抛异常；另一例是尚未 `read` 就引用 `D01-P01`，`citation_ok` 判定 `cited_not_returned`，终态同样是 tool_failure——因为「回答」没有通过业务验收。
- **cancel**：策略在检索后发出 `cancel`，终态为 `cancel`。

### 3. 引用可回查，固定动作可以回放

回答只能引用已经 `read` 返回的段落，不能引用「我猜在库里」的 ID。回放把成功路径上记录的动作序列原样再喂给新的 `Loop`，本机复现了相同终态、相同引用、相同动作和相同状态迁移。这是日志的价值：出了问题可以按动作重放，而不是只能看一段无法复现的「模型当时是这么想的」。

## 常见误区

- 把脚本策略或演示用的固定动作称为模型智能。
- 模型说「完成」就当作业务成功，跳过引用检查。
- 没有步数上限，让「再查一次」变成无限循环。
- 引用从未返回的资料。

## 设计权衡

Workflow 可预测、易回放、成本稳定，但遇到需要额外一跳检索的问题不会自己加步。Agent 循环更灵活，也把停止条件和验收变成了必须显式编码的部分。适合把确定性步骤（鉴权、写入、发布）留在代码里，把不确定的证据搜索放进受限循环。

## 动手实践

```bash
cd practice169
bash test.sh
```

若接入真实模型：让模型只输出本课的动作 JSON，仍走同一个 `Loop` 和引用检查。不要把模型的「我完成了」当成终态。本课未调用真实模型。

## 源码与文档阅读

`harness.py` 的工具、循环、策略与引用检查；`corpus/docs.json`；`driver.py` 的四终态、回放和成本对照。

## 自测题

**1. Agent 和 Workflow 最关键的差别是什么？**

答案：谁选择下一步。Workflow 由代码限定分支；Agent 循环根据观察从动作集合里选（本课用脚本代替模型）。

**2. 为什么「引用了正确的段落 ID」仍可能是 tool_failure？**

答案：如果该段还没有通过 `read` 返回，就是 `cited_not_returned`。引用必须可回查到本轮观察，而不是「ID 在语料里存在」。

**3. 回放成功说明了什么？**

答案：在工具确定、策略确定时，状态迁移可以被日志复现。它不说明模型在新问题上会做对。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | 说明 |
| --- | --- | --- |
| [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents) | 2026-11-15 同批 | 工作流是预置代码路径，Agent 由模型选择下一步。与本课定义一致。本课未调用 Anthropic API。 |
| [LangGraph — Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents) | 2026-11-15 同批 | 同一区分。本课未部署 LangGraph。 |

## 本课小结

先把循环的输入、动作、观察、停止条件和终态写清楚，再用固定 Workflow 做基线。Agent 版本只是换了「谁来选下一步」。本课用脚本策略覆盖了四种终态、步数上限、引用检查和回放；真实模型的动作选择未测。
