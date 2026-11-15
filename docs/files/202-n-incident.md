# 第 202/208 课：事故响应与复盘

> 序号 202/208 · id `n-incident` · 领域 N · 阶段 4 · 难度 深入 · 子类 可靠性与治理
> 前置：[`n-slo`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/198.html)、[`j-tracing`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/151.html)
> 节点摘要：事故响应先控制影响再收集证据，复盘分析系统条件并落实可跟踪改进，避免个人归责阻碍信息流。

## 导读

第 198 课给出错误预算，第 151 课讨论追踪。本课演练连接池耗尽：记录检测、缓解、恢复和用户影响，并写一份不以个人归责为目的的复盘。教学假设：这是本进程内的教学池，不是一次多团队的生产值班。`is_live_model=false`。

## 学习目标

1. 说明指挥与沟通角色及适用边界。
2. 独立完成实践：演练连接池耗尽故障，记录检测、缓解、恢复和用户影响。
3. 验收：复盘区分触发因素与系统原因；行动项有负责人和验证标准。不要在恢复后立刻清掉现场证据。

## 为什么后端要懂

恢复能力取决于信息是否还在、改进是否可验证。如果复盘变成追责，或恢复后立即清空日志，下一次只能重复同一故障。

## 核心知识点

### 1. 先控制影响，再保留证据

**直觉。** 先让用户不再被堵住，但不要把案发现场擦掉。

**原理。** [Incident Response](https://sre.google/workbook/incident-response/) 强调指挥与沟通分工；[Postmortem Culture](https://sre.google/sre-book/postmortem-culture/) 强调无责复盘。演练中角色预先写明：IC-1 指挥，COMMS-1 同步影响，OPS-1 操作。池大小为 2 且最初**没有** checkout 超时——这是系统原因。两个持有者占满池子后，6 个用户调用全部失败（客户端侧等待超时），影响被记入时间线。缓解是给池加上 0.05s 超时并快速失败，而不是无限阻塞。持有者归还连接后，池恢复 `free==size`。时间线、影响和最终快照写入 `reports/evidence/`，不删除。

### 2. 复盘要分开触发与系统原因，行动项必须可验证

**直觉。** 「今天流量突然变大」只是触发，不是根因的全部。

**原理。** 复盘写明：触发是短时占用打满两个连接；系统原因是池没有超时、也没有准入控制，于是超额请求会永远堵住。行动项 A1 由 OPS-1 负责「设置 checkout 超时」，验证标准是重放本演练时，池满后新调用必须抛出 `PoolExhausted` 而不是一直阻塞；A2 由 IC-1 负责「增加 in_use==size 告警」，验证标准是同一演练中告警会触发。没有负责人或没有验证标准的条目不能算行动项。

**边界。** 这不是多团队 pager 轮值，也没有真实的对外沟通稿。角色与时间线是教学结构。

## 常见误区

- 恢复后立即清理所有现场而无证据归档。
- 复盘只写触发（流量、某人操作），不写系统条件（无超时、无告警）。
- 行动项没有负责人或无法验证。
- 把演练写成对个人的追责。

## 设计权衡

快速缓解可能牺牲一部分诊断细节（例如未在阻塞期间抓完整的栈）。本课的权衡是显式的：先加超时止住阻塞，但把缓解前的影响和快照留下来。

## 动手实践

实践包在 `practice202/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice202 "$tmp_dir/"
cd "$tmp_dir/practice202"
bash test.sh
```

本次实测：检测时池已满；缓解前 6 个用户调用失败；加上超时后能够快速失败；恢复后池空闲；复盘区分触发与系统原因；行动项含负责人和验证标准；证据目录保留。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Postmortem Culture](https://sre.google/sre-book/postmortem-culture/) 与 [Incident Response](https://sre.google/workbook/incident-response/)：均 HTTP 200，`Date` 2026-11-15 16:41:28 GMT（Asia/Shanghai 2026-11-16 00:41:28），`Last-Modified` 2026-10-08 04:48:00 GMT（Asia/Shanghai 2026-10-08 12:48:00）。

## 自测题

**1. 为什么不能把触发写成全部原因？**

答：短时占用只是触发；系统原因是池没有超时和准入，超额请求会阻塞。两者都要写进复盘。

**2. 什么样的行动项才算合格？**

答：有负责人，并且有可用同一演练或同一指标验证的标准。本次 A1/A2 都满足。

**3. 恢复之后能否立刻删除日志？**

答：不能。本次证据留在 `reports/evidence/`。没有归档就不能复盘，也无法验证改进。

## 推荐资源

- [Google SRE — Postmortem Culture](https://sre.google/sre-book/postmortem-culture/)（英文，文档，深入，免费）：无责复盘与改进。
- [Google SRE Workbook — Incident Response](https://sre.google/workbook/incident-response/)（英文，文档，进阶，免费）：指挥、沟通与响应流程。

## 本课小结

事故响应先止血并保留证据，复盘区分触发与系统条件，行动项必须能被验证。本课是第五批 193–202 的最后一课，不进入第 203 课。
