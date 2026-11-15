# 第 166/208 课：模型推理服务与批调度

> 序号 166/208 · id `k-inference` · 领域 K · 阶段 3 · 难度 进阶 · 子类 服务化与评测
> 前置：[`k-api`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/161.html)、[`i-metrics`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/128.html)、[`f-model`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/83.html)
> 节点摘要：推理服务需管理 KV cache、动态批处理和队列；首 token 与 token 间延迟反映不同瓶颈。

## 导读

推理服务的体验不是一张「每秒多少 token」的峰值表。TTFT 描述用户等到第一个 token 的时间，TPOT 描述后续 token 的间隔，排队描述别人的长请求把你挡住了多久。本机没有 GPU，也没有 vLLM，所以本课用一个标明教学常数的调度器把这些关系跑清楚，并给出官方 vLLM 的启动命令供你在有 GPU 的机器上复测。调度器不是 vLLM。

## 学习目标

1. 解释 TTFT / TPOT 及适用边界。
2. 独立完成实践：用小模型或可复用测量数据分析并发增加时 TTFT 和吞吐变化。本机没有可服务的小模型和 GPU，因此测量数据来自教学调度器的可复跑轨迹，并逐项标明未实测的 vLLM / GPU 路径。

## 为什么后端要懂

SLO 经常被写成「吞吐最大化」。交互式会话却更在乎首 token。一个长请求如果独占到自己全部生成完，后面的短请求会在队列里空等——这是本课官方误区，也是 FIFO 相对连续批处理最明显的差别。

## 核心知识点

### 1. TTFT 与 TPOT，并且必须带上输入 / 输出长度

**直觉。** Prefill（吃掉整段输入）主要影响首 token；decode（逐个生成）主要影响 token 间隔。同一条策略，短请求和长请求的观感完全不同，所以每条测量都要报告 `input_tokens` 和 `output_tokens`。这是验收要求，不是可选项。

**定义（本课）。**

- TTFT = 第一个输出 token 的时刻 − 到达时刻；
- TPOT = （结束时刻 − 首 token 时刻）/ (output_tokens − 1)，仅当输出多于 1 个 token；
- 吞吐 = 全部输出 token / 从 0 到最后一条结束的墙钟。

`scheduler.py` 里 prefill、decode 的毫秒系数是教学常数，不是某块 GPU 的实测。结果里写了 `scheduler_is_teaching_sketch=true`、`is_live_vllm=false`、`is_live_gpu=false`。

### 2. 连续批处理，以及「长请求挡住别人」

**原理。** FIFO 会把一条请求的 prefill 和全部 decode 做完再接下一条：本机轨迹里，与长请求同时到达的 `short-a`，FIFO 的 TTFT 是 138.8 ms，连续批处理草图是 46.92 ms；更晚到达的 `tiny` 则是 163.6 ms 对 38.92 ms。连续批处理在已 prefill 的请求之间共享 decode 步，新到达的请求 prefill 完即可加入批次，不必等长请求把话说完。

这只是教学草图：没有 chunked prefill，也没有 paged attention。官方 vLLM 的调度远比这复杂，不能把 46.92 ms 抄到产品材料里。

### 3. 最大 token/s 不是最佳交互体验

**实践对照。** 同一工作负载上再加最短作业优先（SJF：一次只跑一条，先跑短的）。本机：

- 吞吐（输出 token / ms）：连续批处理 1.14079，FIFO 与 SJF 都是 0.925508，赢家是批处理；
- `tiny` 的 TTFT：SJF 8.6 ms，批处理 38.92 ms，FIFO 163.6 ms，赢家是 SJF。

吞吐冠军和交互 TTFT 冠军不是同一个策略，`tokens_per_ms_and_ttft_diverge=true`。若只把「最大 token/s」当成最佳体验，会选错给交互用户的策略。

**KV 预算。** 每个 token 占用的 KV 字节在本课也是教学常数。已有两条大请求时，再预约一个巨大 prompt 会被拒绝；空闲时小请求可以通过。这是在讲准入，不是在测某模型的真实显存。

### 4. 官方 vLLM 路径（本机未跑）

节点资源原址 `https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html` 在 2026-11-15 抓取时，先进入一个薄跳转页（本批另存为 `vllm-openai-server`，Date 2026-11-15 23:13:25 Asia/Shanghai），最终内容在 [Online Serving](https://docs.vllm.ai/en/latest/serving/online_serving/)（Date 2026-11-15 23:13:42 Asia/Shanghai，Last-Modified 2026-11-13 01:10:00 Asia/Shanghai）。请用新地址，不要继续把旧文件名当成独立章节。

`scripts/vllm-openai.sh` 给出本机**没有执行**的启动命令：`python3 -m vllm.entrypoints.openai.api_server --model ...` 或官方页上的 `vllm serve`。在有 GPU 的机器上复测时，请同时记下每条请求的输入 / 输出长度、TTFT 和 TPOT，不要只报峰值 token/s。不要把付费 API 密钥写进该脚本。

## 常见误区

- 忽略长请求对其他请求的排队影响。
- 把最大 token/s 当作最佳交互体验。
- 报告延迟却不写输入 / 输出长度。
- 把教学调度器称为「本机 vLLM 实测」。

## 设计权衡

批量提高设备利用率，也可能提高单请求等待；SJF 让短请求很快出首 token，但 decode 无法重叠，吞吐往往更低。生产调度还要在 prefill 与 decode 之间切时间片，本课没有实现。

## 动手实践

```bash
cd practice166
bash test.sh          # 只跑教学调度器，不启动 vLLM
# 有 GPU 时再阅读并按需运行 scripts/vllm-openai.sh
```

## 源码与文档阅读

`scheduler.py` 的 FIFO / 连续批处理草图 / SJF；`driver.py` 的工作负载与指标；`scripts/vllm-openai.sh`。

## 自测题

**1. 为什么每条延迟数字都要附带输入、输出长度？**

答案：Prefill 成本随输入增长，decode 成本随输出增长。不写长度，TTFT / TPOT 无法比较，也不满足本课验收。

**2. 连续批处理的吞吐更高，是否就应该作为唯一上线策略？**

答案：不一定。本课里 SJF 的 `tiny` TTFT 明显更好。要按 SLA 在吞吐和交互延迟之间取舍，而不是只看 token/s。

**3. 本课的 46.92 ms 能否写成「vLLM 的 TTFT」？**

答案：不能。那是教学常数下的草图轨迹，`is_live_vllm=false`。vLLM 路径在 `scripts/vllm-openai.sh`，需在真实 GPU 上另测。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | Last-Modified (Asia/Shanghai) | 说明 |
| --- | --- | --- | --- |
| [vLLM — Online Serving](https://docs.vllm.ai/en/latest/serving/online_serving/) | 2026-11-15 23:13:42 | 2026-11-13 01:10:00 | 旧的 `openai_compatible_server` 页面已跳转到这里。英文，进阶，免费。本机未启动 vLLM。 |

## 本课小结

TTFT、TPOT 和吞吐描述的是不同瓶颈，必须连同输入 / 输出长度一起报告。FIFO 会让长请求堵住短请求；吞吐最高的策略不必是交互体验最好的。本课用教学调度器把这些关系跑出来；vLLM 与真实 GPU 数字未在本机验证。
