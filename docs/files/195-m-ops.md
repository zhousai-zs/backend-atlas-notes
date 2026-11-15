# 第 195/208 课：可验证的智能运维 Agent

> 序号 195/208 · id `m-ops` · 领域 M · 阶段 5 · 难度 专家 · 子类 知识与诊断
> 前置：[`m-knowledge`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/190.html)、[`m-metrics`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/192.html)、[`m-rpc-tools`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/193.html)、[`m-jobs`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/194.html)、[`l-eval`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/185.html)
> 节点摘要：运维 Agent 以只读诊断为起点，将观察、假设、验证与建议显式化，高影响执行走独立授权。

## 导读

第 190、192 课提供知识和指标，第 193、194 课提供受控工具与长任务。本课把它们收成一份 P10 诊断产物：输入服务与故障时间窗，输出证据、排除项、建议和未决问题。教学假设：诊断器是脚本化规则，不是已经上线的智能运维模型。`is_live_model=false`。禁止用自写的固定句式「证明」LLM 能力。

## 学习目标

1. 说明证据图及适用边界：先观察，再形成可被推翻的假设。
2. 独立完成实践：构建 P10，在隐藏故障集上计算根因命中率。
3. 验收：不得把未执行的修复宣称为已恢复；工具返回成功也不等于业务恢复。

## 为什么后端要懂

没有证据链的「根因」无法复查。更危险的是：重启工具返回 200，就把事故写成已恢复，而池子仍然耗尽。

## 核心知识点

### 1. 证据图：观察、假设、排除、建议

**直觉。** 诊断是可反驳的论证，不是一份感想。

**原理。** [Google SRE — Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/) 强调从观察出发形成假设再检验。本课诊断器读取可见的 metrics/logs（不读取隐藏答案），写下证据节点，例如 `in_use==pool_size` 且 checkout 等待很高，则假设 `connection_pool_exhaustion`，并排除「磁盘未满」等竞争性解释。产物固定为 `evidence`、`exclusions`、`suggestions`、`open_questions`、`hypothesized_root_cause`。

隐藏集有 6 个教学故障（池耗尽、磁盘满、配置哈希不匹配、CPU 饱和、依赖超时，以及再次出现的池耗尽）。本次脚本化诊断器命中 6/6。这只说明规则与教学数据对齐，**不是**生产事故上的模型质量。

**边界。** 命中率是对隐藏教学集的离线验证。换成真实日志分布或现场模型，数字会变，必须另测。

### 2. 诊断与执行分离，恢复必须被验证

**直觉。** 工具成功 ≠ 业务恢复。

**原理。** 案例 c6 里，`restart` 工具返回 `tool_ok=true`，但修复并未执行，也没有 postcheck。诊断器把 `recovered` 保持为 false，并把 `recovery_claim` 写成 `tool_returned_ok_but_repair_not_executed`。只有「已授权执行且 postcheck 通过」才能称为恢复。这是高影响操作必须另走授权的原因。

## 常见误区

- 工具返回成功就宣布整个业务恢复。
- 只有根因标签，没有证据和排除项。
- 用模型自己的固定输出给自己打分。
- 把教学集命中率写成生产模型能力。

## 设计权衡

提高自主程度之前，必须先有更强的权限、验证和回退。只读诊断可以较宽；执行必须窄，并且用同一套观测验证结果。

## 动手实践

实践包在 `practice195/`。隐藏答案在 `hidden_faults.json`，诊断器只读 `fixtures/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice195 "$tmp_dir/"
cd "$tmp_dir/practice195"
bash test.sh
```

本次实测：隐藏集命中率 6/6；c6 未宣称已恢复。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/)：HTTP 200，`Date` 2026-11-15 16:41:27 GMT（Asia/Shanghai 2026-11-16 00:41:27），`Last-Modified` 2026-10-08 04:48:00 GMT（Asia/Shanghai 2026-10-08 12:48:00）。本课没有调用现场值班系统。

## 自测题

**1. 为什么产物里必须有 exclusions？**

答：假设要能被推翻。本次每份报告都写下了未成立的竞争性原因，避免「只有一个标签」。

**2. 重启接口返回成功，能否写「已恢复」？**

答：不能。本次 c6 工具成功但修复未执行，`recovered` 仍为 false。

**3. 6/6 的命中率说明了什么？**

答：只说明脚本化规则在这份隐藏教学集上命中。不能外推为生产模型或真实事故的能力。

## 推荐资源

- [Google SRE — Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/)（英文，文档，专家，免费）：观察—假设—验证的工作顺序。

## 本课小结

运维 Agent 的价值在于可检查的证据图，以及把「建议」和「已恢复」分开。下一课是第 196 课 m-observability（Agent 服务的 SLO 与追踪）。
