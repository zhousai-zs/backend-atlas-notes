# 第 155/208 课：灰度发布与回滚

> 序号 155/208 · id `j-release` · 领域 J · 阶段 4 · 难度 深入 · 子类 交付与安全
> 前置：[`j-kubernetes`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/154.html)
> 节点摘要：灰度用小份额流量暴露新版本，以可观察指标决定继续或回滚。

## 导读

滚动升级解决的是「如何换成新副本」。灰度解决的是「要不要把新版本当作默认」。5% 的流量如果已经明显变慢，就应该停，而不是等 100% 用户一起来报案。

教学假设：一次配置变更要先以 5% 流量观察延迟，劣化则回滚到可定位的旧产物。以下阈值和端口是教学设计。

**本机已验证的是决策逻辑：** 两个真实的本地 HTTP 进程同时在线，200 次请求里 10 次（5%）打到注入了延迟的 v2，p99(v2)=129.499 ms 超过 50 ms 阈值，决策为 rollback，并继续使用带哈希的 v1 产物；回滚后 40 次请求不再打 v2。数据迁移文件在应用回滚后仍然存在。`is_live_prod_traffic=false`。未验证：把 `nginx/canary-5percent.conf` 真正 reload 到 nginx，或 Istio / Flagger / Ingress 的金丝雀控制器，以及生产长尾观察窗口。

## 学习目标

1. 解释金丝雀指标及适用边界
2. 独立完成实践：为配置变更设置 5% 流量灰度，注入延迟退化触发回滚。

## 为什么后端要懂

节点理由指向安全发布。后端要能回答三件事：流量怎么切、用哪一个指标停、回滚后哪一份产物在跑、数据迁移清不干净。

## 核心知识点

### 1. 金丝雀指标

**直觉。** 金丝雀不是「感觉还行」，而是事先写好的停止条件。本课用 v2 的 p99 延迟对比固定阈值。

**原理。** 2026-11-15 抓取 Google SRE Workbook *Canarying Releases*（`https://sre.google/workbook/canarying-releases/`，Date 2026-11-15 22:58:53 CST，Last-Modified 2026-10-08 12:48:00 CST，80800 字节）。工作手册讨论如何用小份额流量比较新旧，并以可观察信号决定推进或放弃。本课把停止条件写成代码：

```python
def maybe_rollback(samples, threshold_ms=50.0):
    p99_v1 = percentile(samples["v1"], 99)
    p99_v2 = percentile(samples["v2"], 99)
    decision = "continue"
    if p99_v2 is not None and p99_v2 > threshold_ms:
        decision = "rollback"
    return {"p99_v1": p99_v1, "p99_v2": p99_v2, "threshold_ms": threshold_ms, "decision": decision}
```

百分位按升序排列后取 `ceil(q/100 * (n-1))` 位置（与第 150 课一致，避免把「各组 p99 再平均」误当成合并样本的 p99）。v1 约 6.5 ms，v2 约 129–130 ms，决策 rollback。这是**同一台机器上两个进程 + 确定路由**的结果，不是生产流量形状。

5% 怎么来：`use_canary = (i % 20) < 1`，200 次里恰好 10 次。真实代理常用权重，短样本不会精确到 5%。教学 nginx 片段：

```nginx
upstream atlas_kv {
    server 127.0.0.1:18881 weight=19;  # v1
    server 127.0.0.1:18882 weight=1;   # v2 = 5%
}
```

本机未执行 `nginx -s reload`。你在自己的实验室可以用这份配置，再用访问日志按 upstream 分 p99，套同一套 `maybe_rollback()`。

### 2. 自动停止条件与可定位产物

**直觉。** 「回滚」若找不到当时在跑的那份制品，就只是口头回滚。

**原理。** 每次发布把 `{version, delay, notes}` 写成 JSON，文件名带内容哈希：本机 v1 为 `artifacts/v1-f77f0c83ccd13890.json`，sha256 完整值记在 `results.json`。回滚是把路由切回这份文件对应的进程，而不是「再部署一次最新的 main」。节点验收「回滚产物可定位」「新旧版本可同时运行」在本机成立：金丝雀期间 v1、v2 同时监听 18881/18882。

### 3. 向后兼容迁移

**直觉。** 应用进程回到 v1，不等于数据库或本地数据文件回到 v1。

**实践对照。** v2 启动时写入 `reports/migration.dat`（含 `schema=v2-migrated`）。应用回滚后该文件仍在，`migration_file_remains_after_app_rollback=true`。这是节点陷阱「认为应用回滚能撤销所有数据迁移」的反例，且是本机真实文件，不是口头说明。

## 常见误区

- 认为应用回滚能撤销所有数据迁移。
- 用 10 个样本的 p99 代表生产长尾。
- 把本地确定性 5% 分流写成「已验证 Istio 金丝雀」。
- 停止条件只看平均值，吃掉已经恶化的尾延迟。

## 设计权衡

小流量更安全，但低频故障可能需要更长观察。更敏感的阈值减少用户伤害，也增加误回滚。同时跑两个版本要求配置和协议向后兼容，否则「可同时运行」本身就不成立。

## 动手实践

```bash
cd practice155
bash test.sh
# 阅读 nginx/canary-5percent.conf 与 scripts/diagnose.sh
```

本机：`canary_share_observed=0.05`，`decision=rollback`，迁移文件残留。`service_mesh_or_ingress_canary_verified=false`。

## 源码与文档阅读

`driver.py` 的 `route` / `maybe_rollback` / 产物哈希；`service.py`；`nginx/canary-5percent.conf`。

## 自测题

**1. 为什么 5% 金丝雀仍可能漏掉故障？**

答案：故障若只打在另外 95% 的特定用户、地域或低频接口上，短窗口的 10 个 v2 样本看不到。小流量换来的是更安全，不是完备。

**2. 回滚后发现写库仍然按新 schema 进行，说明什么？**

答案：应用产物回滚了，数据迁移没有自动撤销。需要单独的迁移兼容策略或前向修复。本课用残留的 `migration.dat` 演示这一点。

**3. 本机实验能代替 nginx 权重测试吗？**

答案：不能。本机用确定下标保证恰好 5%，用来练停止条件。权重代理的实际份额和失败重试要在真实代理上另测。

## 推荐资源

| 资源 | 字节 | 抓取时间 (CST) | Last-Modified (CST) | 说明 |
| --- | --- | --- | --- | --- |
| [Google SRE Workbook — Canarying Releases](https://sre.google/workbook/canarying-releases/) | 80800 | 2026-11-15 22:58:53 | 2026-10-08 12:48:00 | 核对金丝雀指标与停止条件。英文，深入，免费。 |

## 本课小结

灰度的核心是预先写好的指标停止条件，加上可定位的旧产物，并承认数据迁移不会随进程一起回去。本机验证了 5% 分流、p99 回滚和产物哈希，没有验证网关或服务网格的金丝雀控制器。
