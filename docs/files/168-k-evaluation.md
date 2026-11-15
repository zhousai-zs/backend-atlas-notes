# 第 168/208 课：LLM 评测与校准

> 序号 168/208 · id `k-evaluation` · 领域 K · 阶段 4 · 难度 深入 · 子类 服务化与评测
> 前置：[`k-rag`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/164.html)、[`k-cost`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/167.html)
> 节点摘要：评测需定义真实任务分布、可复验标准和失败分类；模型裁判需要校准而非无条件信任。

## 导读

没有 gold set，就只剩演示。有了 gold set，还要把调提示用的子集和最终测试子集分开，并把失败案例留下来，而不是只报一个平均分。尤其不要让同一个模型自己宣布「我做对了」——那是本课官方误区，也是本课特意抓住的反例。

## 学习目标

1. 解释 gold set 及适用边界。
2. 独立完成实践：收集 30 个故障问答，分别评价事实、证据、拒答和格式。

## 为什么后端要懂

上线前若只用「看起来不错」和模型自报成功，回归时你将没有可复验的失败分类。评测基础设施本身也会过期：今天（2026-11-15）再去 OpenAI Evals 平台开新项目，已经走不通。

## 核心知识点

### 1. Gold set、切分，以及「只报平均分」不够

**直觉。** Gold set 是一组带标准的题目：可回答的要写清必须出现的事实和必须引用的段落；不可回答的要写清允许的拒答理由。它描述的是你关心的任务分布，不是模型自己出的考题。

**实践对照。** `gold/qas.json` 含 30 题（G01–G30）：18 条可回答、12 条不可回答。切分是 tune 12 / test 18，ID 集合不相交。本课 driver **不会**根据 test 行去改阈值，避免把测试集泄漏进「调参」。指标按全部夹具、tune、test 分开写。

**失败要落盘。** 验收要求「保存错误案例而非只平均分」。`reports/error_cases.json` 留下了 G02、G03、G05、G09、G11、G20、G22 等失败，每条带 `fact` / `evidence` / `refusal` / `format` 四个维度的对错和错误码。平均分没法告诉你「拒答被写成事实」和「JSON 都没成形」是不是同一类问题。

### 2. 四个维度的规则检查

`graders.py` 按这个顺序看一条候选：

- **格式**：能解析为对象；`status` 只能是 `answered` 或 `abstain`；回答必须有非空 `answer` 和至少一条引用；拒答必须有 `reason`。
- **事实**：可回答题目的答案须包含 gold 事实；拒答时不得再附带一段捏造答案。
- **证据**：回答必须引用至少一条 gold 出处，且不得夹带其他文档 ID。
- **拒答**：不可回答的题必须 abstain，理由只能是 `not_retrieved` 或 `insufficient_evidence`；`fact_absent` 不合格；把不可回答的题直接答出来也不合格。

夹具里的失败都被抓住了：非 JSON、空答案、把 `E_CONN_RESET` 说成磁盘满、用无关的 `D20-P01` 当出处、对「2019-01-01 账单库是否故障」给出确定回答、把缺少的 Wi-Fi 密码标成 `fact_absent`。夹具里的正确回答与正确拒答都通过。这些候选是手写夹具，不是现场模型输出，`candidates_are_fixtures=true`，`is_live_model=false`。

### 3. 自裁判无效，且不要在即将关闭的平台上开新项目

**自裁判。** `self_judge_accepts` 只看候选自己写的 `self_grade=pass`。G09 把编码说成 `r50k_base`（gold 是 `cl100k_base`），同时自称 pass：事实维度失败，但自裁判放行。`self_judge_invalid.used_as_official_grade=false`。官方误区「让同一个模型自己宣布全部完成」在这里被写成一条可复跑的反例。自动裁判可以做粗筛，必须用 gold 或人工校准，不能代替官方分数。

**OpenAI Evals 平台（当日抓取）。** `https://platform.openai.com/docs/guides/evals`（Date 2026-11-15 23:12:34 Asia/Shanghai，Last-Modified 2026-11-14 17:17:20 Asia/Shanghai）：平台已于 **2026-10-31** 对现有用户变为只读，并计划于 **2026-11-30** 关闭。今天是 2026-11-15，只读已经开始，关闭还有 15 天。页面建议转向 Datasets。本课 `start_new_work_here=false`，`is_live_openai_evals=false`，评测走本地 gold set。不要把新的回归建在这个即将关闭的平台上。

## 常见误区

- 让同一个模型自己宣布全部完成。
- 用测试集调提示或改阈值，再宣布「测试集分数」。
- 只保留平均分，不保存错误案例。
- 在 2026-11-15 之后还往 OpenAI Evals 平台上开新项目。

## 设计权衡

人工评审更准但贵；规则检查可复验、可回归，但覆盖不了「措辞不同却事实正确」的情况；模型裁判可扩展，但有偏差，必须校准。本课把规则检查当作可复跑的基线，并把生成式评分列为未验证。

## 动手实践

```bash
cd practice168
bash test.sh
```

若要评真实模型：让模型按同一 JSON 合同答题，把输出交给 `graders.grade`，不要用它的 `self_grade` 当官方分数。不要在 Evals 平台新建任务。

## 源码与文档阅读

`gold/qas.json`；`graders.py` 的四个维度；`candidates.py` 的正反夹具；`reports/error_cases.json`。

## 自测题

**1. 为什么调优集和测试集必须分开？**

答案：在测试集上改提示或阈值，分数就不再是对「没见过的题」的估计。本课 tune / test 的 ID 不相交，driver 也不根据 test 行改规则。

**2. 模型在答案里写了 `self_grade: pass`，能当通过吗？**

答案：不能。G09 自称 pass，但把编码写成 `r50k_base`，事实检查失败。自裁判不得作为官方指标。

**3. 今天还应该在 OpenAI Evals 平台上启动新评测吗？**

答案：不应该。该平台 2026-10-31 已只读，2026-11-30 将关闭。应使用 Datasets 或像本课这样的本地 gold set。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | Last-Modified (Asia/Shanghai) | 说明 |
| --- | --- | --- | --- |
| [OpenAI — Evals](https://platform.openai.com/docs/guides/evals) | 2026-11-15 23:12:34 | 2026-11-14 17:17:20 | 只读与关闭时间表；建议转向 Datasets。不要在此开新项目。英文，深入，免费。 |

## 本课小结

评测是带切分、带失败分类、可复跑的合同，不是一次演示或一次自我打分。本课用 30 条 gold、四个规则维度和落盘的错误案例把这件事做实；真实模型分数与已只读的 Evals 平台均未调用。
