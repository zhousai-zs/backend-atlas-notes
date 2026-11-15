# 第 204/208 课：跨团队接口与协作

> 序号 204/208 · id `n-collaboration` · 领域 N · 阶段 5 · 难度 专家 · 子类 组织与技术影响
> 前置：[`n-risk`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/203.html)、[`h-evolution`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/127.html)
> 节点摘要：跨团队协作需要明确服务边界、接口契约、变更责任和故障升级路径，而非仅约定调用地址。

## 导读

第 203 课把依赖写进风险清单，第 193 课已经强制过网关 deadline。本课为 Agent 工具网关与存储团队写出接口契约和超时责任矩阵，并且**必须有可执行的兼容测试**。教学假设：这是一份 v1 教学契约与内存存储桩，不是两支真实团队的值班安排。`is_live_model=false`。

## 学习目标

1. 说明 API 契约、版本与 ownership 的边界。
2. 独立完成实践：为网关与存储写接口契约和超时责任矩阵。
3. 验收：错误码与重试语义一致；存在可执行的兼容测试。双方不得各自无限重试。

## 为什么后端要懂

只约定一个 URL，错误码和重试会在两边各写一套。[Engagement Model](https://sre.google/workbook/engagement-model/) 强调协作要有清晰的接口与责任，而不是默认对方会「合理重试」。

## 核心知识点

### 1. 超时和错误码要有主人，并且测试跑得起来

**直觉。** 契约如果只能被人读、不能被测试跑，过几天就会分叉。

**原理。** 契约规定：存储拥有服务端 deadline（100ms），网关不得抬高；网关拥有重试预算。`UNAVAILABLE` 只允许网关再试一次（共两次）；`DEADLINE_EXCEEDED` 与 `NOT_FOUND` 不得重试。可执行测试覆盖了这三条：一次短暂 UNAVAILABLE 后存储调用次数为 2 且最终成功；睡眠超过 cap 时返回 `DEADLINE_EXCEEDED` 且只调用 1 次；缺失键不重试。错误码集合与契约表一致。

### 2. 双边重试会被放大，必须被测试抓住

**直觉。** 两边各「再试几次」会变成乘法。

**原理。** 对照实现里存储内部重试 3 次、网关再重试 3 次，在持续 UNAVAILABLE 下存储调用次数为 16，远高于契约路径的 2 次。兼容测试把这种放大判定为违约，而不是「更尽力」。统一规范提高协作效率，但允许有书面理由的例外；无限重试不是例外，是事故放大器。

## 常见误区

- 调用方与被调用方各自无限重试。
- 只有文档没有兼容测试，错误码用词不一致（例如一边写 timeout，一边写 DEADLINE_EXCEEDED）。
- 约定了地址，却没有写清谁拥有 deadline。

## 设计权衡

统一契约降低联动故障，但写死所有重试也会让某些降级路径变僵。例外必须写进契约并被测试允许，而不是在代码里悄悄多试几次。

## 动手实践

实践包在 `practice204/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice204 "$tmp_dir/"
cd "$tmp_dir/practice204"
bash test.sh
```

本次实测：合规路径存储调用 2 次；deadline 与 NOT_FOUND 不重试；双边重试放大到 16 次被检出。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Engagement Model](https://sre.google/workbook/engagement-model/)：HTTP 200，`Date` 2026-11-15 16:53:21 GMT（Asia/Shanghai 2026-11-16 00:53:21），`Last-Modified` 2026-10-08 04:48:00 GMT（Asia/Shanghai 2026-10-08 12:48:00）。该章讲 SRE 与产品团队的协作模型；本课落实的是网关/存储契约，没有模拟完整的参与式值班流程。

## 自测题

**1. 为什么 DEADLINE_EXCEEDED 不能再由网关重试？**

答：再试一次通常会再次超过同一条预算，并放大存储负载。本次超时路径只调用存储 1 次。

**2. 怎样证明错误码语义是一致的？**

答：用同一组契约码跑兼容测试。本次 UNAVAILABLE / DEADLINE_EXCEEDED / NOT_FOUND 的重试行为与表一致。

**3. 双方都「再试 3 次」会怎样？**

答：调用次数按乘法放大。本次对照为 16 次，已被兼容测试拒绝。

## 推荐资源

- [Google SRE Workbook — Engagement Model](https://sre.google/workbook/engagement-model/)（英文，文档，专家，免费）：协作责任与接口。
- 第 193 课：网关强制 deadline 的实现背景。

## 本课小结

跨团队接口要有版本化契约、超时主人，以及能抓住双边重试的兼容测试。下一课是第 205 课 n-mentoring（技术传承与人才培养）。
