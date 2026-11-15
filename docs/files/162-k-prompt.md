# 第 162/208 课：提示词与任务契约

> 序号 162/208 · id `k-prompt` · 领域 K · 阶段 2 · 难度 基础 · 子类 模型接口与输出
> 前置：[`k-api`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/161.html)
> 节点摘要：提示词用于表达目标、约束、输入来源和输出格式；它不能替代权限校验和程序验证。

## 导读

上一课把模型调用变成可重试、可取消的客户端。调用之前还要约定任务：输入有哪些字段、输出必须长什么样、没有证据或证据冲突时能不能编造根因。这些约定如果只靠更强硬的语气，而没有固定测试集，就无法验收。

教学假设：为故障摘要定义输入字段和输出契约，并用正常、缺失、冲突三条固定用例检查。以下提示词和规则执行器均为教学设计，不代表腾讯或微信内部提示，也不是对 Claude 或任何付费模型的实调。`is_live_model=false`，`used_paid_api=false`。

## 学习目标

1. 解释角色与指令优先级及适用边界
2. 独立完成实践：为故障摘要定义输入字段和输出契约，用正常/缺失/冲突证据测试。

## 为什么后端要懂

节点给出的理由是「使模型任务可评估而非靠语气优化。」没有固定测试集，就无法知道改提示是真的更稳，还是只在一两句示例上更顺口。

## 核心知识点

### 1. 角色与指令优先级

**原理。** 2026-11-15 抓取节点给出的 Anthropic 提示工程概述时，URL 从 `https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview` 转到 `https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview`。当日页面在「Before prompt engineering」中要求：先有明确的成功标准，以及能对照这些标准做实证测试的方法，再去改第一稿提示；并指出并非所有失败都适合用提示工程解决。本课把成功标准写成可检查的契约，而不是「尽量像专家」。

**直觉。** 系统契约应高于临时的用户补充。角色可以限制口吻，但不能覆盖「无证据必须不确定」这类硬条件。

**实践对照。** 教学提示写明：系统契约优先于任务约束，再高于用户附加说明；无证据或证据冲突时 `confidence` 必须是 `uncertain`。规则执行器只用来跑契约，不是实调模型。

### 2. few-shot

**原理。** 少量示例可以示范输出格式，但示例不能推翻契约。若示例里出现「没有证据也要给根因」，测试集应判定失败。

**实践对照。** 提示明确 few-shot 只演示格式。固定测试集不含「跟着示例编造」的通过条件。

### 3. 证据约束

**原理。** 输出只能引用输入里出现过的 `evidence_ids`；没有证据时不能填写虚构编号，也不能把置信度写成 high。

**实践对照。** 正常用例引用 E1、E2，置信度为 high；缺失用例置信度为 uncertain，且 `evidence_ids` 为空；冲突用例尽管引用了 E1、E2，置信度仍为 uncertain，并在 `open_questions` 中写出冲突双方。另用一条「一定是磁盘故障 / high」的答案去打缺失用例，契约捕获 `no_evidence_must_be_uncertain`。

## 常见误区

1. **写更强烈的措辞就认为能保证正确。** 节点原句即此。附加「务必保证答案正确」并不能让那条编造根因的输出通过测试集。
2. **改提示后只看一两个手工例子。** 没有固定测试集，就无法防止回归。本课三条用例写在 `tests/cases.json`，并标记 `fixed=true`。

## 设计权衡

节点原句是「更长提示可明确边界，也增加成本和指令冲突。」把契约写进提示能减少无证据瞎编，但词元会增加，且多段指令可能互相打架。本课提示约 244 个字符，真正的约束仍由程序校验执行，而不是指望模型自己永远遵守。

## 动手实践

实践目录为 `practice162/`，需要 Python 3。本次为 CPython 3.13.5。不要把规则执行器说成已接入的 Claude 或其他 LLM。

```bash
cd practice162
bash test.sh
cat results.json
cat tests/cases.json
cat reports/prompt.txt
```

成功最后一行为 `ALL_CHECKS_PASSED`。提示迭代时必须继续跑同一份 `tests/cases.json`。

两项验收是「无证据时输出不确定」以及「提示迭代有固定测试集」。前者对应缺失用例；后者对应 `fixed_test_set` 与三条全部通过，以及更强硬措辞不能绕过校验。

## 源码与文档阅读

阅读 `contract.py`、`tests/cases.json` 和 `reports/prompt.txt`。官方概述强调先定义成功标准和评测，再做提示迭代；本课用本地契约检查落实这一点。页面其余关于 Claude 具体模型的调参建议，本课没有实调，不予沿用为实验结果。

2026-11-15 在本机核对节点 URL。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） | 备注 |
| --- | ---: | --- | --- | --- |
| 节点原 URL [Anthropic prompt engineering overview](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview) → 最终 [platform.claude.com 同路径](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview) | 408131 | 2026-11-15 22:58:57 | （响应未提供） | 当天发生跳转，HTTP 最终 200 |

节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。文档路径相对节点记录已改变。

## 自测题

1. **没有证据时为什么必须 uncertain？** 否则就是编造根因。本课缺失用例若输出 high，会被固定测试集拒绝。
2. **为什么 few-shot 不能覆盖契约？** 示例只示范格式；若示例与「无证据不得编造」冲突，仍以契约和测试为准。
3. **把提示写得更强硬能否代替测试集？** 不能。本课附加「务必保证正确」后，编造输出仍然失败。

## 推荐资源

- [Anthropic — Prompt engineering overview](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview)（节点原地址会跳转到此）：用于核对角色与指令优先级及先定义评测再改提示的做法，将结论落实到本节点实验。英文，基础，免费。

## 本课小结

先把输入字段、输出契约和失败条件写成可执行的测试，再迭代提示；无证据或证据冲突必须输出不确定，更强硬的措辞不能代替这套评测。下一课是第 163 课 k-structured（结构化输出与校验）。
