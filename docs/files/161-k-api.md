# 第 161/208 课：模型 API 与可靠客户端

> 序号 161/208 · id `k-api` · 领域 K · 阶段 2 · 难度 基础 · 子类 模型接口与输出
> 前置：[`k-tokens`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/159.html)
> 节点摘要：模型调用是有超时、限流、流式中断和计费的远程请求，需要请求级预算与可观测性。

## 导读

正式节点把第 159 课 `k-tokens` 列为前置：生成长度和上下文窗口会变成本次请求的预算。截至 2026-11-15，本仓库 `lessons/` 中尚未看到第 159 课的完整课文，因此不要把它当作已经完成的读物，也不要补写该课内容。本课只解决另一件事：一次模型调用怎样从演示脚本变成可维护的服务调用。

教学假设：一个内容后台要把用户评论摘要或草稿回复交给远程模型 API。这是为讲解超时、限流和流式中断而设计的场景，不代表腾讯或微信的内部接口、协议或实现。实践完全走本机回环上的 HTTP 模拟服务，没有密钥，也没有对付费 API 做实调。

## 学习目标

1. 解释 streaming 及适用边界。
2. 独立完成实践：实现统一客户端，注入超时、429 和半途断流并记录 usage。
3. 用负例确认：不对所有错误无限重试，并区分未完成输出与最终答案。

## 为什么后端要懂

节点给出的理由是「把 demo 接入升级为可维护服务」。演示脚本往往只打印一次成功响应；后台服务还要面对限流、超时、连接断开和按 token 计费。如果把半截流式输出当成最终答案，或在客户端超时后自动重放已经产生副作用的工具调用，问题会进入账本、工单或下游系统，而不是停留在终端里。

## 核心知识点

### 1. streaming

**直觉。** 非流式调用等完整结果一次性返回；流式调用把生成过程切成事件，先到达的文本可以先展示。没有看到结束事件，就还没有最终答案。

**原理。** 2026-11-15 阅读的 [Streaming API responses](https://developers.openai.com/api/docs/guides/streaming-responses) 说明，HTTP 流式是 `stream=true` 的 SSE。官方示例走 Responses API，常用生命周期事件包括 `response.created`、`response.output_text.delta`、`response.completed` 和 `error`。同日迁移文档写明：Chat Completions 仍然受支持，新项目建议用 Responses；两者的流式形状不同——前者是带 `delta` 的 chunk，后者是带 `type` 的事件。文档还提醒：流式输出更难在生成中途做内容审核，审核分数会在完整文本之后到达。

OpenAI Python SDK（PyPI 当日稳定版 3.51.0）的 README 写得更工程化：消费 `Stream` 时，读超时变成 `APITimeoutError`，其它传输失败变成 `APIConnectionError`；**流式消费不会自动重试**，因为重放可能重复已经交给应用的输出。

本课模拟服务用精简的 Responses 风格事件。本次 `bash test.sh` 记录：

```text
success stream: status=completed incomplete=false finish_reason=stop
usage={"input_tokens": 5, "output_tokens": 4, "total_tokens": 9}
midstream_cut: status=incomplete output_text=未完成片 usage=null attempts=1
stream timeout: status=incomplete output_text=开头 finish_reason=timeout attempts=1
```

完整流带有 usage，并在 `response.completed` 后才标成最终答案。半途关连接或在已输出「开头」后超时，都保留部分文本，但不把它们当成完成结果，也不自动重试。

**适用边界。** 上述事件名、字段和「是否自动重试流式消费」来自当日公开文档与 SDK README，属于易变接口。本实验没有连接 `api.openai.com`，因此不能证明生产环境的事件顺序、usage 出现时机或中途断开的字节级行为与模拟服务相同。WebSocket 流式模式未覆盖。

### 2. 429 与重试退避

**直觉。** 429 和部分 5xx、连接失败常常是短暂的，可以退避后再试；400 和 401 通常不会因为再发一次就变对。重试必须有次数上限和总时限，否则会自己制造风暴。

**原理。** 3.51.0 文档写明：默认重试 2 次，短指数退避；连接错误、408、409、429 和 >=500 默认可重试，且仅在请求体可安全重发时重试。`_constants.py` 与 `v3.51.0` 标签一致：`DEFAULT_MAX_RETRIES = 2`，`INITIAL_RETRY_DELAY = 0.5`，`MAX_RETRY_DELAY = 8.0`，`MAX_RETRY_AFTER_DELAY = 120` 秒。源码在有限的 `Retry-After` 内等待该值，否则按指数退避并把时延随机减少 0–25%；若服务器要求等待超过 120 秒，则不重试。限流指南把 `Retry-After` 当作下限，并提醒不要重试额度或账单类错误；若应用自己重试，应关闭或计入 SDK 重试，避免嵌套放大。

教学客户端采用同样的分类，但把时延缩短到可课堂复跑的量级，并把抖动设为 0 以便断言。本次结果：

```text
429 then success: attempts=3 retry_sleeps=[0.12, 0.12] elapsed=0.244111s usage.total_tokens=10
400 / 401: attempts=1 finish_reason=non_retryable
500 then success: attempts=2
429 always: attempts=3 (max_retries+1), finish_reason=rate_limited
budget exhausted: attempts=2 finish_reason=budget_exhausted
```

三次 429 后成功时，两次睡眠都不小于模拟服务给出的 `Retry-After: 0.12`。总是 429 时在 3 次后停止，而不是无限重试。把总预算收到 0.10s 后，第二次 429 的下一次等待会超过剩余预算，客户端停在 `budget_exhausted`，即使 `max_retries` 仍有余额。

**适用边界。** 官方默认 0.5–8 秒退避、最多尊重 120 秒的 `Retry-After`、以及 0–25% 抖动，都没有在本实验里按原值复现。生产 429 的触发条件、额度耗尽与速率限制的区分，以及真实 `Retry-After` 量级，均未实调。

### 3. request ID 与取消

**直觉。** 每次尝试都应能对上服务端的请求标识；取消或超时意味着「这次还没有最终答案」，不等于自动再做一次。

**原理。** SDK 从响应头 `x-request-id` 露出公开属性 `_request_id`；失败请求要在 `APIStatusError` 上读取 `request_id`。超时方面，文档默认 10 分钟（`DEFAULT_TIMEOUT` 为 600 秒，连接超时 5 秒），超时抛 `APITimeoutError`，并且**请求级超时默认也会再试两次**。节点的陷阱正是：客户端超时后自动重复执行有副作用的工具。教学客户端因此分开两条路：没有工具、也还没有吐出任何文本的请求级超时，允许按预算重试；一旦带了工具，或流式已经给出部分文本，超时、取消和断流都不再自动重试。

本次结果：成功请求记录了 `req_39e062b3ee3644c2`；429 后成功的三次尝试分别是 `req_6f81020ea3e441b5`、`req_9c3bff771bd744e8`、`req_0b282a5eb1b94fb1`。取消用例在收到「第一」后把 `cancel_event` 置位，结果为 `status=incomplete finish_reason=cancelled usage=null`。带 `charge_once` 工具的超时用例在 0.200564s 返回，`attempts=1`，模拟服务的副作用计数从 0 变为 1，没有被重试放大。

**适用边界。** 请求 id 的编码格式、是否一定在超时前返回、以及服务端在客户端取消后是否回滚，都不由本实验保证。官方默认重试超时，本课故意收窄了带工具时的行为；把两种策略混用到生产代码前，需要按接口语义单独设计。

## 常见误区

1. **客户端超时后自动重复执行有副作用的工具（节点 pitfalls）。** 本次对照里，工具超时只发生一次副作用。若把这类超时与「无工具的连接超时」一样直接重试，计数会被放大。
2. **把部分流式文本当成最终答案。** `未完成片` 和 `开头` 都是可展示的中间结果，但 `incomplete=true` 且没有 usage，不能当作已完成输出。
3. **对所有错误无限重试。** 400、401 和校验失败都只尝试 1 次；429 也在 `max_retries+1` 或总预算处停住。
4. **用统一客户端掩盖供应商语义差异（节点 tradeoffs 的反面）。** Responses 的 typed events 与 Chat Completions 的 chunk `delta` 不是同一套字段，抽象不能假装它们可以无差别互换。
5. **以为本地模拟等于已经实调付费 API。** 本课明确没有密钥、没有出网调用。

## 设计权衡

节点给出的权衡是：抽象多供应商方便切换，但不能掩盖语义差异。

- **收益。** 把超时、重试资格、总预算、request id 和 usage 放进同一个客户端，调用方不必在每个业务路径里复制一套状态机；实验里同一套接口覆盖了成功、429、5xx、断流和取消。
- **代价。** 一旦抽象漏掉「流是否已开始」「是否已执行工具」「结束事件是什么」，切换供应商时会把错误的完成语义带到新接口上。Chat Completions 仍受支持，并不等于可以继续沿用旧的 chunk 处理代码去读 Responses 事件。

## 动手实践

实践包是 `practice161.tar.gz`，解压后用 Python 3 标准库即可，不需要安装 `openai`，也不需要编译器：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice161.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice161"
bash test.sh
```

脚本把完整输出写入 `run.log`，并生成 `results.json`。最后一行应为 `ALL_CHECKS_PASSED`；日志中的 400/401、中途断流和超时是受控负例，不代表脚本整体失败。教学协议是 `POST /v1/lesson/responses`，只接受 `127.0.0.1` / `localhost`，构造指向 `https://api.openai.com/v1` 的客户端会直接被拒绝。

本次机器环境为 CPython 3.13.5、`Linux-6.12.113+-x86_64-with-glibc2.41`，`OPENAI_API_KEY` 未设置，运行前后都未导入 `openai`。主线采纳后的官方实践目录运行在 2026-11-15 北京时间 23:04:43 开始，`driver_exit=0`，外层耗时 1.42855s，**52** 项检查全部通过（含 `is_live_model is false`）。协作稿初次运行是同日 22:48:58 的 51 项，随后补了一致性字段并复跑。请求 id、回环端口会随每次运行变化，应以新生成的 `run.log` / `results.json` 为准。

1. **成功路径并记录 usage。** 非流式与流式都得到 `可靠客户端`，`usage.total_tokens=9`，各 1 次尝试。
2. **注入超时。** 客户端超时 0.2s，本次 0.200733s 返回，结果为未完成，没有 usage。
3. **注入 429 与退避。** 前两次 429 尊重 `Retry-After: 0.12`，第三次成功并记录 usage 与三次 request id。
4. **负例：不可重试错误与校验。** 400、401 以及缺少 `id` / `usage` 的 200 响应均 `attempts=1`。
5. **注入半途断流。** 保留 `未完成片`，不标完成、不重试；流式超时保留 `开头`，同样不重试。
6. **预算与副作用。** 总是 429 时停在 3 次；更紧的总预算停在 2 次。带工具的超时副作用为 1，没有被放大。取消后得到 `第一`，仍是未完成输出。

`results.json` 保存环境版本、每项检查、各用例的退出语义和副作用计数。外层耗时包含驱动启动，不能与单次用例的 `elapsed_seconds` 混为一谈。

## 源码与文档阅读

节点建议阅读 [OpenAI — Python SDK](https://github.com/openai/openai-python)。2026-11-15 北京时间亲自抓取：仓库页与 raw README 均返回 200，无跳转；README 43144 字节，与同日 [Python 库文档](https://developers.openai.com/api/reference/python/index.md) 正文一致。PyPI 当前稳定版是 `openai 3.51.0`（上传时间为 2026-11-14 05:13:37 北京时间），要求 Python 3.10+。GitHub Releases API 当日返回 403，版本不以 GitHub 发行列表为准。节点 `verifiedAt` 是 2026-10-26，与本次核对日期不同。完整 URL、响应头和摘录见实践包中的 `sources/EVIDENCE.md`。

主线在 2026-11-15 22:58:56（北京时间）再次抓取 PyPI JSON 与 `v3.51.0` / `main` 的 `_constants.py`：稳定版仍是 `openai 3.51.0`、未 yank，两份常量文件内容相同。同日 23:03:58 再次抓取 [Deprecations](https://developers.openai.com/api/docs/deprecations)（标题 Deprecations | OpenAI API，Date 23:03:58，Last-Modified 2026-11-14 11:18:35 北京时间）：功能表将 Assistants API 的关停日期列为 Aug 26, 2026，替代为 Responses API and Conversations API，公布日期为 Aug 26, 2025；当日表中未将 Chat Completions 列为即将关停的端点。独立解包复跑通过后再写入 `is_live_model=false` 并得到 52 项检查通过，`is_live_model=false`，未导入 `openai`，未设置密钥。

跟随阅读了流式指南、Responses 迁移页、弃用页、错误码与限流指南。与本课直接相关的当日结论：

- 默认超时 10 分钟、默认重试 2 次；可重试集合为连接错误、408、409、429、>=500。
- 流式消费默认不自动重试；请求级超时在官方 SDK 中默认会重试。
- Chat Completions 仍受支持，新项目建议用 Responses；Assistants API 已在 2026-08-26 关停。当日弃用表未把 Chat Completions 列为即将关闭的端点。
- 默认 HTTP 栈已迁移到 HTTPX2，这是 SDK 接口细节，教学客户端并未使用该依赖。

与本课实验的差异：实践没有安装或调用官方 SDK，协议、退避时间和抖动都是为复跑缩短或简化后的教学实现；模拟事件只覆盖文档中的一小部分字段。文档说明的是生产 API 与 SDK 行为，本课另外用本地服务检查重试资格、未完成输出和副作用是否被放大。

## 自测题

**1. 为什么半途断流得到的「未完成片」不能当成最终答案？**

答：它没有 `response.completed`，也没有 usage，`status` 为 `incomplete`。流式接口允许先展示部分文本，但验收要求把未完成输出与最终答案区分开。自动重试还可能重复已经交付的部分。

**2. 收到 400 或 401 时，为什么客户端只尝试一次？**

答：这类错误通常不是短暂故障，再发同一请求不会变成合法调用。本次负例中两者都是 `attempts=1`。不对所有错误无限重试，也包括它们。

**3. 客户端超时后，为什么带工具的调用不能按默认超时重试来处理？**

答：官方 SDK 会重试请求级超时，但这不能保证服务端没有已经执行副作用。本次 `timeout_with_tool` 在 0.2s 级超时后副作用为 1、尝试次数为 1；若自动再试，计数会被放大。这正是节点列出的陷阱。

## 推荐资源

- [OpenAI — Python SDK](https://github.com/openai/openai-python)：节点主资源，英文、基础、免费；用于核对 streaming 与 429 重试退避，并将结论落实到实验。
- [Streaming API responses](https://developers.openai.com/api/docs/guides/streaming-responses)：跟随阅读，核对 SSE 事件与流式适用边界。
- [Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses) 与 [Deprecations](https://developers.openai.com/api/docs/deprecations)：核对 Chat Completions 仍受支持、Responses 的推荐定位，以及 Assistants 等已关停接口。
- [Rate limits](https://developers.openai.com/api/docs/guides/rate-limits)：核对 `Retry-After`、指数退避和嵌套重试。

## 本课小结

模型 API 调用要同时管理超时、限流、流式中断和 usage。流式可以尽早展示文本，但只有结束事件才能标记最终答案；429 可以退避重试，400/401 和已开始的流式输出不能按同样规则无限重试；request id 用于把每次尝试记下，取消和超时都不能悄悄变成带副作用的重放。本课用本地模拟服务验证了这些分支，没有实调付费 API。下一课是第 162 课 k-prompt（提示词与任务契约）。
