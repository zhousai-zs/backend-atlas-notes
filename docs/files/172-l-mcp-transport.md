# 第 172/208 课：MCP 传输与连接管理

> 序号 172/208 · id `l-mcp-transport` · 领域 L · 阶段 3 · 难度 进阶 · 子类 MCP互操作
> 前置：[`l-mcp`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/171.html)
> 节点摘要：MCP 消息语义与传输绑定分离；连接断开不证明远端副作用已停止，需要把网络生命周期与工具任务生命周期分开。

## 导读

同一份 `diagnose` 工具，本课分别跑在 stdio 子进程和 Streamable HTTP 上。传输负责分帧、交付和取消；任务是否完成要看应用自己的 job 记录。2026-07-28 的「无状态核心」不等于业务无状态。把调试日志打进 stdio 的 stdout，协议会先坏掉。

## 学习目标

1. 说明三类生命周期：连接、请求、工具任务。
2. 独立完成实践：为同一个只读诊断工具启动 stdio 子进程和 HTTP 服务；在执行中关闭客户端或终止子进程；故意污染 stdout 再把日志迁到 stderr；记录协议与 SDK 版本。

## 为什么后端要懂

stdio 把 stdout 当成协议通道，普通日志一旦混入，对端解析的就不再是 JSON-RPC。HTTP 客户端断开之后，服务端任务可能还在跑，也可能已经写了副作用——断开不是回滚。取消和完成若没有单一终态，对账和重试都会乱。

## 核心知识点

### 1. stdio：换行分隔的 JSON-RPC，日志只能走 stderr

**原理。** MCP stdio 绑定是**换行分隔的 JSON-RPC**（每条消息一行 JSON），不是 LSP 那种 `Content-Length` 头。协议帧走 stdout，调试和日志走 stderr。`stdio_main.py` 把 `logging` 接到 stderr，并在启动时把 pid 写到文件以便回收。

**反例。** 设置 `POLLUTE_STDOUT=1` 后，进程先向 stdout 写下一行 `DEBUG: starting diagnose server`。原始探针读到的第一行就不是 JSON，`json.loads` 失败。把同一段信息改到 stderr 之后，第一行恢复为可解析的 JSON-RPC，官方 `Client(StdioServerParameters(...))` 能调通 `diagnose`，本机返回 `completed:stdio-ok`，协商版本 `2026-07-28`。

**子进程回收。** 让 `diagnose` 睡眠 8 秒，中途对子进程发 SIGTERM，必要时 SIGKILL，然后确认 pid 不在、`/proc` 下没有以它为父进程的孤儿。验收：「子进程退出后没有孤儿资源」。`reap.alive_after_reap=false`，`orphans=[]`。客户端会看到连接错误，这是预期，不是把工具结果假装成功。

### 2. HTTP：断开之后任务状态仍可解释，断开 ≠ 回滚

**实践对照。** `http_main.py` 用官方 `MCPServer.streamable_http_app` + uvicorn，在 `127.0.0.1` 的临时端口提供 `/mcp`。`Client("http://127.0.0.1:<port>/mcp")` 调通同一 `diagnose`，得到 `completed:http-ok`，协议同样是 2026-07-28。

随后发起一个 2.5 秒的 `http-disc` 任务：一旦 `jobs.start` 把「http-disc started」写入 `reports/probe.log` 并落下 job 文件，就取消客户端并关闭连接。之后再读 job 文件和探针日志：启动行还在，`rolled_back=false`；job 状态仍是 `running` / `completed` / `cancelled` / `failed` 之一，`job_still_interpretable=true`。诊断工具虽然是只读查询，但运行本身留下了运维记录——这些记录不会因为套接字关掉而被抹掉。若这是一次带业务副作用的工具，更不能把断连当成事务回滚。

### 3. 取消与完成的竞争：只能有一个终态

`jobs.claim_terminal` 用文件锁把状态从 `running` 推进到唯一的终点。对 `race1` 同时宣称 `completed` 和 `cancelled`，本机恰好一次 claim 成功（结果为 `completed`），另一次被记入 `rejected_terminals`，不会出现「既完成又取消」。应用层必须自己保证这一点；传输取消只是其中一个输入。

### 4. 三种生命周期，以及新旧修订不能直接互换

- **连接**：stdio 子进程的一生，或 HTTP 客户端的 TCP / 会话；
- **请求**：一条带 2026-07-28 per-request 信封的 JSON-RPC；
- **工具任务**：`reports/jobs/<id>.json` 里的 job，可以比连接活得更长。

2026-07-28 不再要求 initialize 握手，请求自己携带版本；2024-11-05 至 2025-11-25 则依赖握手。第 171 课已经在现代入口上对 `2025-11-25` 和 `1999-01-01` 得到 `-32022 Unsupported protocol version`。所以「新旧修订版示例不能直接互换」：不是因为工具名字不同，而是因为信封和握手假设不同。兼容旧客户端要单独开路径并验收，而不是把旧 SDK 示例复制到新传输上。

SDK 与协议已写入结果：`mcp==2.3.0`，`protocol=2026-07-28`。

## 常见误区

- 向 stdio 的 stdout 打调试日志。
- 把 HTTP 断连当作远端任务已停止或已回滚。
- 让取消和完成同时成为终态。
- 把 2026-07-28 的「无状态核心」理解成业务也无状态。

## 设计权衡

stdio 适合由宿主拉起的本地工具，生命周期与子进程绑定，但标准输出被协议占用。HTTP 适合远程，必须单独设计任务标识、取消和超时；本课的 job 文件是最小形态。生产上的代理超时、负载均衡和 OAuth 未在本机验证。

## 动手实践

```bash
cd practice172
bash test.sh
```

不要把本课 HTTP 服务暴露到非回环地址。依赖与第 171 课相同，另需本环境已具备的 uvicorn / starlette。

## 源码与文档阅读

`server_app.py` 的共用 `diagnose`；`jobs.py` 的单一终态；`stdio_main.py` 与 `http_main.py`；`driver.py` 的污染对照、回收、断连和竞争。

## 自测题

**1. 为什么调试日志不能写到 stdio 的 stdout？**

答案：stdout 的每一行都被当作 JSON-RPC 帧。本课在污染模式下第一行是 `DEBUG: ...`，解析失败；日志改到 stderr 后协议恢复。

**2. 客户端断开后，为什么还要去读 job 记录？**

答案：断开只结束了连接生命周期，不证明任务消失，更不构成回滚。本课的 `http-disc` 启动行在断连后仍在。

**3. 为什么 2025 年的 stdio 示例不能直接用在 2026-07-28 上？**

答案：旧示例走 initialize 握手；新修订在每条请求的 `_meta` 里声明版本。信封不兼容，现代入口会返回 -32022。要兼容必须单独验证。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | Last-Modified (Asia/Shanghai) | 说明 |
| --- | --- | --- | --- |
| [MCP Transports (2026-07-28)](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) | 2026-11-15 23:13:27 | 2026-11-15 03:15:50 | 传输与分帧。英文，进阶，免费。 |
| [MCP 规范 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) | 见第 171 课 | 2026-11-15 03:12:44 | 与传输页同一修订。 |

## 本课小结

传输和任务是两条生命周期。本课把同一个 `diagnose` 跑通了官方 SDK 的 stdio 与 Streamable HTTP：stdout 不能混日志，子进程能被回收，HTTP 断连不会回滚已记录的副作用，取消与完成只有一个终态。代理超时和受保护的远程部署未在本机验证。
