# 第 206/208 课：技术战略与组合投入

> 序号 206/208 · id `n-strategy` · 领域 N · 阶段 6 · 难度 专家 · 子类 组织与技术影响
> 前置：[`n-planning`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/199.html)、[`n-evolution`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/201.html)
> 节点摘要：技术战略把业务方向、核心能力、约束和投资顺序连接起来，明确哪些能力自建、复用或停止投入。

## 导读

第 199、201 课分别处理容量和迁移。本课为「后台 + Agent」团队写一份一年能力组合：每项投入都要能指出业务风险、可观察收益和退出条件，并且允许 stop / reuse，而不是把所有新技术列为必选。教学假设：组合里引用的第 193、194、196 课指标是教学观测，不是真实年度预算或 ROI。`is_live_model=false`。

## 学习目标

1. 说明 build/buy、能力护城河与机会成本的边界。
2. 独立完成实践：规划一年能力组合，用业务风险解释优先级。
3. 验收：每项投入有可观察收益与退出条件；不以追热点替代需求。

## 为什么后端要懂

技术专家若只追逐框架清单，会把 toil 和切换成本当成进步。[Eliminating Toil](https://sre.google/workbook/eliminating-toil/) 把无尽的操作与切换视为需要被限制的工作，而不是默认的战略。

## 核心知识点

### 1. 每项投入都要能退出，不能只进不出

**直觉。** 没有退出条件的项目会永远占用编制。

**原理。** 合格组合里：巩固工具网关（build）对应越权与 deadline 被抬高的风险，观测越权拒绝和夹逼审计，连续两季度干净则停止加功能；任务成功 SLI（reuse）对应「HTTP 200 伪装健康」，差值持续为 0 则不再扩追踪后端；停掉「每个新 Agent 框架都必选」（stop）对应评测基线被打断的 toil；工作流引擎（buy_or_wait）先用第 194 课的租约与幂等验证，稳定则不自建。校验器要求每项都有 `business_risk`、`observable`、`exit_condition`。

自建换来控制力，也要承担维护和人才成本；复用或买入则接受外部约束。这些是组合问题，不是道德问题。

### 2. 追热点且全员必选的路线必须被拒绝

**直觉。** 「今年把所有新框架和最新 LSM 都上掉」不是战略。

**原理。** 对照组合把三项都标成 `because_it_is_hot: true` 且全部 `build`，既没有风险说明，也没有观测和退出条件。校验结果同时给出 `hot_without_risk`、`missing_*` 与 `all_items_are_mandatory_builds`。不以追热点替代需求，是可以直接测试的规则。

## 常见误区

- 所有新技术都列入必选路线。
- 只有方向名词，没有可观察收益和退出条件。
- 把教学课里的指标写成已经验证的生产 ROI。

## 设计权衡

自建获得控制力，也承担维护与人才成本。停止投入同样是战略决定，能把时间还给高风险项。

## 动手实践

实践包在 `practice206/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice206 "$tmp_dir/"
cd "$tmp_dir/practice206"
bash test.sh
```

本次实测：带风险、观测和退出条件且含 stop/reuse 的组合通过；全员必选的热点清单被拒。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Eliminating Toil](https://sre.google/workbook/eliminating-toil/)：HTTP 200，`Date` 2026-11-15 16:53:22 GMT（Asia/Shanghai 2026-11-16 00:53:22），`Last-Modified` 2026-10-08 04:48:00 GMT（Asia/Shanghai 2026-10-08 12:48:00）。该章把 toil 作为需要被限制的操作负担；本课用「停止无风险的框架必选」与「复用已有 SLI」来对应，而不是运行一个 SRE 工时统计系统。

## 自测题

**1. 为什么每项都要写退出条件？**

答：否则投入无法收敛。本次合格项都写了何时停止加功能或不再扩后端。

**2. 「上线所有新框架」错在哪里？**

答：用热点代替业务风险，并且全部必选自建。校验器会同时打上热点无风险和全员 build。

**3. 本课引用第 193、196 课的指标，能否写成今年已经赚到的 ROI？**

答：不能。那些是教学观测，未验证项里写明了没有真实年度预算。

## 推荐资源

- [Google SRE Workbook — Eliminating Toil](https://sre.google/workbook/eliminating-toil/)（英文，文档，专家，免费）：把操作负担从战略注意力里减下来。

## 本课小结

技术战略是一组带风险、观测和退出条件的 build/buy/stop 决定，而不是热点清单。下一课是第 207 课 n-governance（复杂系统治理与边界）。
