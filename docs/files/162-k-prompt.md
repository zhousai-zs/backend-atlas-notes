# 第 162/208 课：提示词与任务契约

> 序号 162/208 · id `k-prompt` · 领域 K · 阶段 2 · 难度 基础 · 子类 模型接口与输出
> 前置：[`k-api`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/161.html)
> 节点摘要：提示词表达目标、约束、输入来源和输出格式；它不能替代权限校验和程序验证。

## 导读

把提示写得更像专家，并不能保证模型遵守「无证据就不确定」。可维护的做法是：先把成功标准写成**程序可检查的契约**，再用固定测试集迭代。契约检查可以在本机完成；「这段提示是否真的改变了某个 LLM 的行为」必须在真实模型上复现，不能用一份按规则写死的 `rule_model()` 来证明。

教学假设：为故障摘要定义输入字段和输出契约，并用正常、缺失、冲突三条固定用例检查。以下提示词和规则执行器均为教学设计，不代表腾讯或微信内部提示，也不是对 Claude 或任何付费模型的实调。`is_live_model=false`，`used_paid_api=false`，`prompt_effect_on_llm_verified=false`，`contract_checker_verified=true`，`rule_model_is_not_prompt_evidence=true`。

## 学习目标

1. 解释角色与指令优先级及适用边界
2. 独立完成实践：为故障摘要定义输入字段和输出契约，用正常/缺失/冲突证据测试。

## 为什么后端要懂

节点给出的理由是「使模型任务可评估而非靠语气优化」。没有固定测试集，提示迭代只是在改文案；没有程序契约，模型的 JSON 看起来完整也可能引用了不存在的证据。

## 核心知识点

### 1. 角色与指令优先级

**直觉。** 系统契约应高于临时的用户补充。角色可以限制口吻，但不能覆盖「无证据必须不确定」这类硬条件。

**原理。** 2026-11-15 抓取节点给出的 Anthropic 提示工程概述时，URL 从 `https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview` 转到 `https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview`（408131 字节，Date 2026-11-15 22:58:57 CST）。当日页面在「Before prompt engineering」中要求：先有明确的成功标准，以及能对照这些标准做实证测试的方法，再去改第一稿提示；并指出并非所有失败都适合用提示工程解决。本课把成功标准写成 `validate_output()`，而不是「尽量像专家」。

教学提示写明：系统契约优先于任务约束，再高于用户附加说明；无证据或证据冲突时 `confidence` 必须是 `uncertain`。**这段提示是否被真实模型遵守，本机没有测量。** 本机只验证了：若输出违反契约，检查器会失败——即使有人在提示末尾追加「务必保证答案正确，一定要给出根因」。

### 2. few-shot

**直觉。** 少量示例可以示范输出格式，但示例不能推翻契约。若示例里出现「没有证据也要给根因」，测试集应判定失败。

**原理。** 提示里写了「few-shot 只演示格式，不覆盖上述契约」。这是一条**设计意图**，不是本机得到的模型行为数据。要验证 few-shot 有没有带偏某个 LLM，必须用 `live_eval.py` 或同等官方 SDK 调用，把同一批 `tests/cases.json` 交给模型。本机未跑。

### 3. 证据约束与固定测试集

**直觉。** 输出只能引用输入里出现过的 `evidence_ids`；没有证据时不能填写虚构编号，也不能把置信度写成 high。

**实践对照（契约层，已验证）。** `tests/cases.json` 是固定测试集，含 normal / missing / conflict。检查器断言：

- 缺失证据：`confidence` 必须是 `uncertain`，且 `evidence_ids` 为空；
- 冲突证据：即使引用了 E1、E2，`confidence` 仍须 `uncertain`；
- 人为构造的「一定是磁盘故障 + high + 空 evidence_ids」会被打上 `no_evidence_must_be_uncertain`。

`rule_model()` 是一个按同样规则生成「刚好能通过」输出的确定性函数。它出现在 `driver.py` 里，只是为了让三条用例有一份可运行的合法输出样本，并证明检查器在合法输出上不会误杀。**它的通过不能写成「提示词有效」或「LLM 遵守了角色优先级」。** 结果字段 `rule_model_is_not_prompt_evidence=true` 就是为了堵住这种循环自证。

节点验收「无证据时输出不确定」「提示迭代有固定测试集」：前半句在本机对**检查器**成立，对**模型输出**未验证；后半句成立（`fixed_test_set=tests/cases.json`）。

**真实模型路径（本机未执行，见 `live_eval.py`）。**

```bash
pip install 'openai==3.51.0'
export OPENAI_API_KEY=...          # 仅在你明确要实调时；test.sh 不会读它
python live_eval.py --model <你选择的、支持该接口的模型>
# 或指向你自己已经启动的本地 OpenAI 兼容服务：
python live_eval.py --base-url http://127.0.0.1:8000/v1 --model <local>
```

脚本把模型原文交给同一个 `validate_output()`，写入 `reports/live_eval.json`，并要求该文件里 `is_live_model=true` 才算提示效果证据。没有真实模型时不要伪造这份文件。

## 常见误区

- 写更强烈的措辞就认为能保证正确。本课用检查器抓住了「务必给出根因」仍输出 high 的情况；这仍然不是模型实验。
- 用自己写的固定回答证明 prompt / LLM 效果。
- 没有固定测试集就连续改提示，最后只记住了某一次偶然的好输出。
- 用提示替代权限校验（本课契约也不负责鉴权，见第 156 课）。

## 设计权衡

更长提示可明确边界，也增加成本和指令冲突。把能写成程序的约束（字段、证据编号、冲突时必须 uncertain）从提示里抽到检查器，提示可以更短，失败也可定位。真正只能由模型把握的风格与推理，才值得留在提示里，并在真实模型上回归。

## 动手实践

```bash
cd practice162
bash test.sh
# 阅读 contract.py、tests/cases.json、reports/prompt.txt
# 不要把 results.json 里 rule_model 的三组输出当成模型评测
# 有模型时再跑 live_eval.py
```

官方概述强调先定义成功标准和评测，再做提示迭代；本课用本地契约检查落实「先有评测」。页面其余关于 Claude 具体模型的调参建议，本课没有实调，不予沿用为实验结果。

## 源码与文档阅读

`contract.py` 的 `validate_output` 与被明确降权的 `rule_model`；`tests/cases.json`；`live_eval.py` 的官方 SDK 调用方式；`results.json` 中的 `verified_on_this_machine` 与 `not_verified_on_this_machine`。

## 自测题

**1. 为什么 `rule_model` 通过三条用例，仍不能写「提示工程有效」？**

答案：该函数按检查规则直接生成合法 JSON，没有经过任何语言模型。它只能说明检查器接受合法输出、拒绝非法输出。提示对模型有没有作用，要看 `live_eval.py` 或同等实调。

**2. 把「务必给出根因」追加到提示末尾，本机实验证明了什么、没证明什么？**

答案：证明了检查器仍会拒绝无证据的 high 置信输出（程序约束没有被措辞绕过）。没有证明真实模型会或不会被这句措辞带偏。

**3. 固定测试集应该和调提示用的例子分开吗？**

答案：应该。否则你会把测试集本身调进提示里，分数不再能反映新输入。本课的 `cases.json` 是检查器的回归集；若你开始实调模型，应再留一份未写入 few-shot 的保持集。本课未做模型侧的切分实验。

## 推荐资源

| 资源 | 字节 | 抓取时间 (CST) | 说明 |
| --- | --- | --- | --- |
| 节点原 URL [Anthropic prompt engineering overview](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview) → 最终 [platform.claude.com 同路径](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview) | 408131 | 2026-11-15 22:58:57 | 当天发生跳转，HTTP 最终 200。用于核对「先定义评测再改提示」。英文，基础，免费。页面其余模型调参建议未实调。 |

## 本课小结

提示词要服从可检查的任务契约，迭代要有固定测试集。本机验证了检查器与「更强硬的措辞不能绕过检查」；没有验证任何 LLM 是否遵守该提示。需要效果证据时，用 `live_eval.py` 在真实模型上跑同一批用例。
