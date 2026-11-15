# 第 171/208 课：MCP 角色与协议边界

> 序号 171/208 · id `l-mcp` · 领域 L · 阶段 3 · 难度 进阶 · 子类 MCP互操作
> 前置：[`l-tools`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/170.html)
> 节点摘要：MCP 是 AI 应用与外部工具、资源及提示能力互操作的协议；它不负责替应用决定任务目标，也不自动授予调用权限。

## 导读

第 170 课的 Tool Calling 发生在「一个应用内部，模型与执行器之间」。MCP 是应用与外部服务器之间的协议：host 面对用户和策略，client 与 server 按修订版交换消息。本课固定协议修订版 **2026-07-28** 与官方 Python SDK **mcp 2.3.0**，并且使用 `MCPServer`——`FastMCP` 在 2.x 中已重命名。

## 学习目标

1. 区分 Tool Calling 的模型接口与 MCP 的应用协议。
2. 独立完成实践：用官方 SDK 暴露只读 KV 工具和一份说明资源，从两个客户端完成发现与调用；构造不支持的版本、缺少必填参数和无权限请求；不以成功列出工具代替授权验收。

## 为什么后端要懂

同一套后台查询若要被多个 Agent 客户端复用，需要一条有版本、有发现、有错误码的协议。但协议标准化的是消息形状，不是「列出来的工具人人可调用」。2026-07-28 还改掉了旧的 initialize 握手假设，照抄 2025 年示例会直接撞上不支持的版本。

## 核心知识点

### 1. 角色、修订版、SDK 版本

**角色。** Host 管用户交互与策略；Client 代表 host 说话；Server 暴露 tools / resources / prompts。MCP 不替你决定任务目标，也不在 `tools/list` 成功后自动授权。

**版本。** `mcp.types.version` 在 2.3.0 中：`LATEST_MODERN_VERSION = LATEST_PROTOCOL_VERSION = "2026-07-28"`；`KNOWN_PROTOCOL_VERSIONS` 含 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`；其中只有 `2026-07-28` 属于 `MODERN_PROTOCOL_VERSIONS`，更早的属于握手世代（`HANDSHAKE_PROTOCOL_VERSIONS`）。2026-07-28 按请求在 `params._meta` 里携带 `io.modelcontextprotocol/protocolVersion` 与客户端元数据，不再依赖连接级 initialize 握手。

**SDK。** `from mcp.server import MCPServer`。`from mcp.server.fastmcp import FastMCP` 会失败，并指向迁移指南 `https://py.sdk.modelcontextprotocol.io/v2/migration/`。不要把旧博客里的 FastMCP 示例直接贴进 2.3.0。

### 2. 两个客户端、同一业务结果、列出 ≠ 授权

**实践对照。** `server.py` 用 `MCPServer("kv-reader", version="2.3.0")` 注册 `kv_get` 和资源 `resource://guide`。两个 `Client(server, client_info=Implementation(name=...))` 通过官方内存传输（`create_client_server_memory_streams` / 进程内 `Client(MCPServer)`）先后连接，协商版本都是 `2026-07-28`。

双方都能列出 `kv_get`，也都能读到同一份 guide。对键 `shared`，alice 与 bob 得到完全相同的 `kv-shared-ok`——这是「两个客户端能读取同一业务结果」。对键 `alice-private`，alice 读到 `kv-alice-ok`，bob 收到 JSON-RPC 错误：`unauthorized: bob-client cannot read key 'alice-private'`。授权看的是请求元数据里的 `io.modelcontextprotocol/clientInfo`，不是工具是否出现在列表中。验收：「不以成功列出工具代替兼容性或授权验收」。

### 3. 不支持的版本与缺少必填参数，必须是明确错误

**缺参。** `call_tool("kv_get", {})` 返回 `is_error=true`，正文是 pydantic 的 `Field required`（key 缺失）。这是工具参数错误，不是一次「空成功」。

**不支持的版本。** 本课通过官方内存流，向正在运行的 server 发送一条 `tools/list`，其 `_meta` 里写 `protocolVersion=1999-01-01`。现场响应是 JSON-RPC 错误：`code=-32022`，`message=Unsupported protocol version`，`data={supported: ["2026-07-28"], requested: "1999-01-01"}`。同一套官方函数 `classify_inbound_request` 对握手世代的 `2025-11-25` 给出同样的 -32022——在现代 per-request 路径上，旧版握手示例不能直接互换。兼容旧客户端必须单独验证，不能假设「已知版本列表里有它，现代入口就接受它」。

## 常见误区

- 把 MCP 当成「另一种 function calling」，忽略版本与传输。
- 继续使用已移除的 `FastMCP` 或已删除的 `streamablehttp_client` 导入路径。
- 用 `tools/list` 成功代替授权或兼容性验收。
- 把 2025 年的 initialize 握手示例原样打到 2026-07-28 的入口上。

## 设计权衡

现代修订去掉握手，降低了连接级状态，但每个请求都要带齐版本与客户端元数据；旧客户端不会自动适配。进程内内存传输适合单测，不能替代远程 HTTP 上的 OAuth（本课列为未验证）。

## 动手实践

```bash
cd practice171
bash test.sh
```

依赖：`mcp==2.3.0`（已在 `/workspace/atlas/cmvenv`）。

## 源码与文档阅读

`server.py` 的 `MCPServer`、ACL 与 `MCPError`；`driver.py` 的双客户端、缺参、现场 -32022 与 `classify_inbound_request`。

## 自测题

**1. Tool Calling 和 MCP 差在哪里？**

答案：前者是一个应用里模型提出结构化意图、宿主执行；后者是 host/client 与外部 server 之间带版本的应用协议。本课把这句话写进了 `tool_calling_vs_mcp`。

**2. bob 能列出 `kv_get`，为什么读 `alice-private` 仍失败？**

答案：列表只表示工具存在。授权按客户端身份与键级 ACL 执行。列出成功不是授权。

**3. 为什么 2025-11-25 会在 2026-07-28 入口上得到 -32022？**

答案：2025-11-25 是已知的握手世代版本，但不在 `MODERN_PROTOCOL_VERSIONS` 里。现代入口按请求信封校验版本，不会因为「听说过这个日期」就接受旧握手流量。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | 说明 |
| --- | --- | --- |
| [MCP 规范 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) | 索引 2026-11-15 23:13:26；规范 Last-Modified 2026-11-15 03:12:44 | 当日规范索引 307 到该修订，仍为当前版本。 |
| [mcp 2.x 迁移：FastMCP → MCPServer](https://py.sdk.modelcontextprotocol.io/v2/migration/) | 同批 | 2.3.0 必读。 |
| [Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 同批 | 本课使用的官方 SDK。 |

## 本课小结

先锁修订版和 SDK 版本，再用官方 `MCPServer` 说话。两个客户端可以读到同一条业务数据，但列出工具不等于授权；不支持的版本和缺参必须是明确错误。旧握手示例不能直接打在 2026-07-28 的入口上。远程 OAuth 未在本机验证。
