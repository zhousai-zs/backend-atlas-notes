# 第 84/208 课：gRPC C++同步与异步接口

## 元信息

| 字段 | 内容 |
| --- | --- |
| 节点 ID | `f-grpc` |
| 标题 | gRPC C++同步与异步接口 |
| 英文名 | gRPC C++ |
| 领域 / 阶段 | F / 3 |
| 子类 | 协议与框架 |
| 难度 | 进阶 |
| 摘要 | gRPC 提供基于服务定义的调用能力，C++ 有多种 API 形态，选择时需理解回调、完成队列和对象生命周期。 |
| 为什么学 | 通过成熟实现学习 RPC 框架的真实接口。 |
| 先修 | `f-model`、`c-thread` |
| 实践 | 按官方 C++ 教程实现 KV unary 服务，再加入截止时间和取消。 |
| 标签 | `gRPC C++`、`F`、`C++` |
| 参考资源 | [gRPC C++ 同步与异步接口：参考文档 / 原始资料](https://grpc.io/docs/languages/cpp/basics/)（grpc.io，英文，文档，进阶，免费） |

## 导读

C++ gRPC 的难点通常不在写 `.proto`，而在选对 API 形态并把线程归属、对象生命周期和超时策略落到代码里。同一个服务定义，可以生成同步调用、回调式异步调用，也可以基于完成队列手写事件循环；三者暴露的能力相近，但对“调用进行到哪一步、由哪个线程推进、状态对象何时可以销毁”的要求完全不同。

本课不追求覆盖全部 API，而是建立一套可复用的判断标准：什么时候用同步接口最省事，什么时候必须换成异步接口，以及切换之后哪些约束会随之改变。课程按「服务与 stub、unary 和 stream、同步 API、completion queue 异步、callback API、deadline 与取消」的顺序展开，最后用 KV 服务实践把这些约束落到代码里。

阅读时请始终带着两个问题：这次调用的 deadline 在哪里设置？异步状态对象由谁、在什么事件之后释放？节点里点出的边界也值得提前记住：不同 gRPC API 的线程和生命周期约束不能混用。成熟框架降低了协议成本，但依赖、调试和部署体积仍需要评估。

## 学习目标

完成本课后，你应当能够：

- 解释服务与 stub 的关系：服务定义描述“有哪些方法、参数和返回值”，stub 是客户端侧的代理，负责把一次本地方法调用变成可传输的 RPC。
- 按官方 C++ 教程实现一个 KV unary 服务，并在此基础上加入截止时间和取消，而不是只跑通一次“永远等待”的示例。
- 说明同步、回调异步、完成队列三种接口的线程模型差异，能判断某段代码运行在框架线程、业务线程还是调用方线程上。
- 为一个给定的后台调用场景选择接口形态，并写清楚选择理由与随之而来的生命周期约束。

本课不要求背 API 签名，而要求你能在代码评审中说清楚：这次调用的超时从哪里来，失败如何收敛，对象由谁释放。

### 验收标准

对照节点验收标准，完成本课后应满足：

1. **所有 RPC 都有 deadline。** 客户端发起的调用、服务端在处理函数里对下游发起的调用，都不能出现没有截止时间的“永久等待”。deadline 既可以是固定值，也可以由上游剩余时间推导，但必须显式设置并有测试覆盖。
2. **异步状态对象在最后完成事件前不释放。** 使用回调 API 时，调用对象和响应对象要活到回调真正执行；使用完成队列 API 时，状态对象要活到对应 tag 被 `Next()` 取出。提前析构不是“可能出错”，而是未定义行为。

### 先修自检

**f-model 自检**：能否说清一次 RPC 从 stub 方法调用到服务端处理函数执行，中间经历了序列化、传输、反序列化和分发？能否解释 deadline、错误码、取消分别在哪一层生效？如果这些问题答不上来，先回看 f-model 中关于调用语义和失败模型的部分。

**c-thread 自检**：能否判断一段代码运行在哪个线程上？是否理解 `join`、条件变量、回调在哪个线程执行，以及对象析构与线程退出的先后关系？异步 gRPC 的生命周期问题，本质上是“谁拥有对象、在哪个线程释放”的问题。如果对线程归属没有把握，先回看 c-thread。

自检不是形式要求。任何一项答不上来，先把对应问题记录下来，再回到先修内容补齐；带着模糊的线程模型直接写异步 gRPC，通常只会在压力测试时暴露问题。

## 为什么后端要懂

微信公众号后台并不是一个单体进程：消息接入、内容安全、粉丝资料、模板消息、统计计费等能力，往往由不同团队维护，甚至使用不同语言。gRPC 加 `.proto` 提供的价值，是把“调用哪个方法、传什么字段、失败如何表示”固化成契约。但契约只解决接口长什么样，不解决调用会不会拖垮线程池。下面几个场景，正好对应本课要练的判断。

### 场景一：消息与事件回调的内部分发

用户发给公众号的消息、关注取关等事件，进入后台后通常要分发给多个内部服务：内容安全做审核，客服系统做会话沉淀，统计服务做计数。同步客户端写起来最直接，但分发线程会被下游耗时直接占用；如果某个下游偶发变慢，接入线程池很快被占满，新的回调只能排队。这里需要 deadline，也需要判断是否该把串行调用改成并发或异步。

### 场景二：运营后台的粉丝资料聚合

运营同学打开一个粉丝详情页，后台可能要同时查询关注状态、标签、互动记录和历史消息。同步串行调用会把多个下游的尾延迟叠加起来，用户看到的就是页面越来越慢。常见做法是并发发起多个调用，并让整页请求共享一个总 deadline；这时就会遇到“多个异步调用如何汇总结果、任一失败如何处理、剩余时间如何传给下游”的问题。

### 场景三：模板消息与素材发送链路

发送一条模板消息前，可能要校验模板、检查素材、确认配额，再调用发送服务。链路一长，任何一个没有 deadline 的同步调用都可能把上游线程挂住。更隐蔽的是服务端在处理函数里再同步调用下游：当前线程被下游占用，线程池上限又固定，高并发时容易形成跨服务的等待环。把 deadline 一路传下去，是这类链路最基本的自我保护。

### 场景四：批量推送与高吞吐事件流

批量推送、素材同步、事件订阅这类场景，数据量天然是流式的。同步 API 每次调用占用一个线程，连接数和并发量上来后，线程数与内存会成为瓶颈；回调或完成队列把“等待响应”变成事件驱动，用少量线程推进大量在途调用。代价是对象生命周期更复杂：请求、响应、上下文和状态对象都必须活到最后一个完成事件。

### 这些场景的共同点

它们都不是“能不能调通”的问题，而是“在故障和高压下还能不能收敛”的问题。deadline 决定一次慢调用会不会无限占用资源；异步接口决定等待是否值得占用线程；生命周期约束决定程序在压力下是稳定报错还是随机崩溃。这也解释了本课为什么把“所有 RPC 有 deadline”和“异步状态对象在最后完成事件前不释放”写成验收标准。

接下来的核心内容会先建立服务与 stub 的直观模型，再对比 unary 与 stream 的适用边界，最后落到同步 API 的线程模型与取消行为。阅读时可以对照上面的场景，为每个场景标记你会选择的接口形态，并在学完后回来修正。

### 一个可操作的判断流程

真正写代码时，可以按下面四步做决定，而不是先挑 API：

1. **先确定 deadline。** 这次调用属于哪条请求链路？上游还剩多少时间？如果没有上游约束，就给一个明确的固定上限，并在测试里验证超时路径。deadline 应该先于接口形态确定，因为它是业务约束，不是实现细节。
2. **再判断并发形态。** 低频、链路短、实现简单的调用，同步接口完全可以胜任；需要同时发起大量在途调用，或者等待时间远大于计算时间时，才考虑回调或完成队列。不要为了“看起来高级”把简单调用异步化。
3. **为异步调用画生命周期。** 对象在哪里创建、由哪个线程推进、哪个事件代表最后一次完成、谁负责释放。画不出这张图，就不要写异步代码；混用不同 API 的线程假设，往往就是在这里出问题。
4. **最后写失败路径。** deadline 超时、主动取消、对端不可用分别返回什么状态？异步对象在失败路径上是否仍能走到完成事件并被释放？失败路径和成功路径一样需要测试。

这四步并不依赖具体框架版本，但能帮你避开大多数“本地能跑、线上偶发崩溃”的问题。等你熟悉之后再回头看官方示例，会更容易理解示例为什么那样组织对象和控制流。

### 与后续小节的分工

本课先建立问题意识，再拆解接口细节：2.1 讲服务定义与 stub 的角色，让你知道一次调用两端各自拿着什么；2.2 对比 unary 与 stream，说明什么时候一次请求不够用；2.3 展开同步 API 的线程模型；2.4 和 2.5 分别讲 completion queue 异步与 callback API 的生命周期；2.6 收拢 deadline 与取消。

阅读顺序上，建议先顺着核心概念读一遍，再回到这里对照四个后台场景，最后完成 KV unary 服务的实践。实践时至少保留一条超时用例和一条取消用例，否则“所有 RPC 有 deadline”无法被验证。异步接口的练习不必一开始就追求功能完整，先把对象生命周期画清楚更重要。

如果只能记住一句话：接口形态服务于失败模型。同步还是异步，取决于你希望失败时由谁等待、由谁释放资源，而不是取决于哪种写法更短。把这个问题想清楚，具体 API 的名字和参数只是查文档的成本。

### 从场景回到学习

- 读核心概念时，把每个接口形态对应到上面至少一个场景，并写下选择理由。
- 实践 KV unary 服务时，先加 deadline 和超时用例，再加取消，不要最后补。
- 完成后再回看这一节，检查自己的生命周期判断是否经得起“最后一个完成事件”的追问。

## 核心概念：从服务定义到三种调用形态

### 2.1 服务与 stub：一份定义，两端类型

gRPC 的起点不是 `Channel`，也不是 `ServerBuilder`，而是 `.proto` 里的服务定义。下面是一个最小但完整的 KV 服务定义，后面实践会继续使用它：

```proto
syntax = "proto3";

package kv;

service KVStore {
  rpc Put(PutRequest) returns (PutResponse);
  rpc Get(GetRequest) returns (GetResponse);
  rpc Delete(DeleteRequest) returns (DeleteResponse);
}

message PutRequest {
  string key = 1;
  string value = 2;
}

message PutResponse {
  uint64 version = 1;
}

message GetRequest {
  string key = 1;
}

message GetResponse {
  string value = 1;
  uint64 version = 2;
  bool found = 3;
}

message DeleteRequest {
  string key = 1;
}

message DeleteResponse {
  bool deleted = 1;
}
```

`syntax`、`package`、`service`、`message` 这些字段和 f-protobuf 课讲过的模式概念一致。这里要特别关注 `service KVStore`：它不是一个普通的 C++ 类，而是一份“有哪些方法、每个方法收什么消息、返回什么消息、是不是流式”的声明。代码生成器会围绕它生成两组完全不同的类型。

**服务端一侧**生成 `kv::KVStore::Service`。它是一个抽象基类，每个 unary 方法对应一个虚函数：

```cpp
class KVStore::Service {
 public:
  virtual grpc::Status Get(grpc::ServerContext* context,
                           const kv::GetRequest* request,
                           kv::GetResponse* response);
  virtual grpc::Status Put(grpc::ServerContext* context,
                           const kv::PutRequest* request,
                           kv::PutResponse* response);
  virtual grpc::Status Delete(grpc::ServerContext* context,
                              const kv::DeleteRequest* request,
                              kv::DeleteResponse* response);
};
```

你要做的是继承这个基类，覆盖自己关心的方法。返回 `grpc::Status`，而不是返回响应对象；响应通过输出参数填写。这个签名里有三个关键对象：

- `grpc::ServerContext*`：这一次服务端调用的上下文，包含 deadline、客户端 metadata、取消状态等。它由 gRPC 创建，只在本次处理期间有效。
- `const kv::GetRequest*`：已经反序列化好的请求。它由 gRPC 持有，处理函数不应保存这个指针供以后使用。
- `kv::GetResponse*`：由 gRPC 提供的响应缓冲区，处理函数填写它，然后通过返回的 `Status` 告诉框架这次调用是成功还是失败。

**客户端一侧**生成 `kv::KVStore::Stub`。它是一个具体类（通常通过 `NewStub` 创建），每个方法对应一个成员函数：

```cpp
class KVStore::Stub {
 public:
  grpc::Status Get(grpc::ClientContext* context,
                   const kv::GetRequest& request,
                   kv::GetResponse* response);
  grpc::Status Put(grpc::ClientContext* context,
                   const kv::PutRequest& request,
                   kv::PutResponse* response);
  grpc::Status Delete(grpc::ClientContext* context,
                      const kv::DeleteRequest& request,
                      kv::DeleteResponse* response);
};
```

注意客户端的签名和服务端并不对称：客户端接收 `const Request&`，服务端接收 `const Request*`；客户端多了一个 `ClientContext*`，服务端则拿到 `ServerContext*`。这些差异是生成器根据“调用方向”决定的，不是你可以随意互换的。

把两端的角色放在一张表里：

| 概念 | 所在端 | 作用 | 生命周期由谁负责 |
| --- | --- | --- | --- |
| `kv::KVStore::Service` | 服务端 | 方法声明的抽象基类，供你继承实现 | 你的实现对象，通常活得比 `Server` 久 |
| `kv::KVStore::Service` 实现 | 服务端 | 真正执行业务逻辑的对象 | 你；注册到 `ServerBuilder` 后不能提前销毁 |
| `grpc::Server` | 服务端 | 监听端口、接收连接、分发调用 | 你；`BuildAndStart()` 后由你持有 |
| `kv::KVStore::Stub` | 客户端 | 把本地调用编码成 RPC 的代理 | 你；通常由 `NewStub(channel)` 返回 |
| `grpc::Channel` | 客户端 | 到某个服务端的连接抽象，可被多个 stub 共享 | 你；stub 内部持有它的 `shared_ptr` |
| `grpc::ClientContext` | 客户端 | 单次调用的上下文：deadline、metadata、取消 | 你；必须活到本次调用结束 |
| `grpc::ServerContext` | 服务端 | 单次服务端调用的上下文 | gRPC；只在处理函数执行期间有效 |

最容易被混淆的是“服务”和“stub”这两个词。日常说“服务”可能指 `.proto` 里的 `service` 声明、服务端实现类、或者正在运行的服务进程；日常说“stub”可能指生成的客户端类、这个类的一个实例、或者这个实例背后共享的 channel。读文档和写代码时要把它们分开：

- **服务定义**是契约，写在 `.proto` 里，跨语言、跨进程都靠它对齐。
- **服务实现**是服务端的业务代码，实现 `Service` 基类里的虚函数。
- **stub** 是客户端的代理对象，负责把参数交给 channel、把响应带回来。
- **channel** 是连接层面的对象，一个 channel 可以被多个 stub 共享；创建 channel 不等于建立好了一条 TCP 连接，连接是按需建立和复用的。

一个典型的服务端启动顺序是：

```cpp
std::string server_address("0.0.0.0:50051");
KVServiceImpl service;

grpc::ServerBuilder builder;
builder.AddListeningPort(server_address, grpc::InsecureServerCredentials());
builder.RegisterService(&service);

std::unique_ptr<grpc::Server> server = builder.BuildAndStart();
if (!server) {
  std::cerr << "failed to start server on " << server_address << "\n";
  return 1;
}
std::cout << "server listening on " << server_address << "\n";
server->Wait();
```

`RegisterService(&service)` 注册的是指针，所以 `service` 必须活得比 `server` 久。`BuildAndStart()` 返回空指针通常意味着端口被占用、地址格式不对或者凭证配置失败。`Wait()` 会阻塞当前线程，直到有人调用 `Shutdown()`。这些细节看起来琐碎，但它们决定了程序退出时会不会崩溃：如果 `service` 先于 `server` 析构，而 server 还在分发请求，后果就是访问已经销毁的对象。

客户端则是：

```cpp
auto channel = grpc::CreateChannel("127.0.0.1:50051",
                                   grpc::InsecureChannelCredentials());
std::unique_ptr<kv::KVStore::Stub> stub = kv::KVStore::NewStub(channel);
```

`CreateChannel` 返回 `std::shared_ptr<grpc::Channel>`，`NewStub` 接收这个共享指针。多个线程可以共享同一个 channel；生成出来的 stub 通常也被设计成可以并发调用，但每次调用仍然要有自己的 `ClientContext` 和请求/响应对象。不要把 `ClientContext`、请求或响应对象做成全局共享，也不要在一次调用还没结束时复用它们。

### 2.2 unary 和 stream：四种消息流形态

gRPC 支持四种调用形态。它们共用同一份服务定义、同一套状态码和同一个 channel，但消息数量、完成点和 C++ 接口都不同。

| 形态 | `.proto` 写法 | 客户端发送 | 服务端发送 | 完成信号 |
| --- | --- | --- | --- | --- |
| unary | `rpc M(Req) returns (Resp)` | 1 条 | 1 条 | 客户端收到响应和状态 |
| server streaming | `rpc M(Req) returns (stream Resp)` | 1 条 | 0 到多条 | 服务端结束流并给出状态 |
| client streaming | `rpc M(stream Req) returns (Resp)` | 0 到多条 | 1 条 | 服务端返回响应和状态 |
| bidirectional streaming | `rpc M(stream Req) returns (stream Resp)` | 0 到多条 | 0 到多条 | 两个方向都结束并给出状态 |

**unary** 是最常见的形态，一次请求对应一次响应。同步客户端的签名就是普通的“传 context、传请求、传响应指针、返回 Status”。它的完成点很清晰：函数返回时，调用已经结束，`Status` 和响应对象都有确定内容。但要注意，“函数返回”只说明本次 RPC 在客户端侧收敛了，不代表服务端的副作用一定没有发生，也不代表响应一定被客户端完整理解。这些是 f-model 课讨论过的语义，在 gRPC 里同样成立。

**server streaming** 适合“一个请求触发一串结果”的场景，例如订阅变更、分页拉取、日志跟随。同步客户端拿到的是 `ClientReader<Resp>`：

```cpp
grpc::ClientContext context;
context.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::seconds(30));

kv::WatchRequest request;
request.set_prefix("user:");

auto reader = stub->Watch(&context, request);
kv::WatchEvent event;
while (reader->Read(&event)) {
  process(event);
}
grpc::Status status = reader->Finish();
if (!status.ok()) {
  log(status.error_code(), status.error_message());
}
```

这里有两个完成点需要区分：`Read` 返回 `false` 表示“不会再有消息了”，但它本身不告诉你原因是正常结束还是出错；真正的最终结果要看 `Finish()` 返回的 `Status`。因此，流式调用的正确收尾顺序永远是：先读到 `Read` 返回 `false`（或主动放弃），再调用 `Finish()`，再根据 `Status` 判断成功失败。不能因为已经收到几条消息就认为调用成功，也不能在 `Read` 返回 `false` 后直接丢弃 `Finish` 的结果。

**client streaming** 适合“客户端分多次上传、服务端最后给一个汇总”的场景，例如批量写入、指标聚合。同步客户端使用 `ClientWriter<Req>`：

```cpp
grpc::ClientContext context;
context.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::seconds(30));

kv::UploadResponse response;
auto writer = stub->Upload(&context, &response);

for (const auto& item : items) {
  kv::UploadRequest request;
  request.set_key(item.key);
  request.set_value(item.value);
  if (!writer->Write(request)) {
    break;
  }
}
grpc::Status status = writer->Finish();
```

`Write` 返回 `false` 通常意味着流已经不能继续写（对端关闭、deadline 到期、连接出错等），但真正的原因同样要看 `Finish()` 的 `Status`。`WritesDone()` 表示“我不再写了”，它不等于“服务端已经处理完”，更不等于“调用成功”。

**bidirectional streaming** 两端都可以独立读写，适合聊天、双向同步、交互式协议。同步客户端使用 `ClientReaderWriter<Req, Resp>`：

```cpp
grpc::ClientContext context;
context.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::seconds(60));

auto stream = stub->Chat(&context);
std::thread reader([&] {
  kv::ChatMessage message;
  while (stream->Read(&message)) {
    handle(message);
  }
});

kv::ChatMessage outgoing;
outgoing.set_text("hello");
stream->Write(outgoing);
stream->WritesDone();

reader.join();
grpc::Status status = stream->Finish();
```

双向流最容易犯的错误，是把“读线程”和“写线程”各自的阻塞当成整个调用的状态。读线程读到 `false` 只说明读方向结束了；写线程写失败只说明写方向出问题了；最终仍然要 `Finish()`。此外，`ClientReaderWriter` 的读写是否可以由不同线程并发调用，取决于具体接口和版本约定，不能凭“它是双向的”就假设任意线程任意操作都安全。实践里更稳妥的做法是明确一个读线程、一个写线程，并在关闭时按约定顺序收尾。

服务端的流式接口同样分四种。同步服务端用 `ServerWriter<Resp>` 写多条响应：

```cpp
grpc::Status Watch(grpc::ServerContext* context,
                   const kv::WatchRequest* request,
                   grpc::ServerWriter<kv::WatchEvent>* writer) override {
  for (int i = 0; i < 10 && !context->IsCancelled(); ++i) {
    kv::WatchEvent event;
    event.set_key(request->prefix() + std::to_string(i));
    if (!writer->Write(event)) {
      break;
    }
  }
  return grpc::Status::OK;
}
```

用 `ServerReader<Req>` 读客户端的多条消息，最后通过返回值给出一个响应；用 `ServerReaderWriter<Req, Resp>` 做双向。无论哪种，服务端都要检查 `context->IsCancelled()`，因为客户端可能在流中途取消或 deadline 到期，而你的循环未必会因此立刻停止。

四种形态的共同点是：**完成点是“流结束加最终状态”，不是“收到第一条消息”或“写出最后一条消息”。** 这个共同点会在异步 API 里被放大，因为异步形态下“消息到达”和“调用完成”是两类不同的事件。

### 2.3 同步 API：调用线程就是等待线程

同步 API 的最大优点是直观：调用一个成员函数，当前线程阻塞，返回时拿到 `Status` 和响应。它适合请求量可控、处理时间短、代码路径清晰的服务。

一个带 deadline 的同步客户端调用：

```cpp
grpc::Status GetValue(kv::KVStore::Stub* stub,
                      const std::string& key,
                      std::string* value) {
  grpc::ClientContext context;
  context.set_deadline(std::chrono::system_clock::now() +
                       std::chrono::milliseconds(300));

  kv::GetRequest request;
  request.set_key(key);
  kv::GetResponse response;

  grpc::Status status = stub->Get(&context, request, &response);
  if (!status.ok()) {
    return status;
  }
  if (!response.found()) {
    return grpc::Status(grpc::StatusCode::NOT_FOUND, "key not found");
  }
  *value = response.value();
  return grpc::Status::OK;
}
```

这段代码里有几个值得逐条确认的点：

1. **`ClientContext` 是每次调用独有的。** 它承载 deadline、metadata、取消标志和调用结束后的状态信息。一个 `ClientContext` 不能被两个并发调用共用，也不能在一次调用结束后重新“重置”再用。
2. **deadline 设置在 `ClientContext` 上。** `set_deadline` 接收一个绝对时间点，因此通常写成 `now() + 时长`。deadline 是端到端预算：它不仅让客户端停止等待，也会通过协议传播给服务端，服务端可以查询自己还剩多少时间。
3. **`Status` 是最终结果的唯一权威。** 不要用“响应对象有没有被填”来判断成功；失败时响应对象的内容没有意义。也不要把 `status.error_code()` 直接当成业务枚举，它是 gRPC 定义的状态码，业务错误需要通过状态码加错误详情，或者在响应消息里表达（具体选择应在接口设计时固定）。
4. **响应对象在调用期间必须存活。** 同步调用里它通常在栈上，函数返回后才销毁，所以很自然。但这个“自然”在异步 API 里会消失。

服务端的同步处理函数：

```cpp
class KVServiceImpl final : public kv::KVStore::Service {
 public:
  grpc::Status Get(grpc::ServerContext* context,
                   const kv::GetRequest* request,
                   kv::GetResponse* response) override {
    if (request->key().empty()) {
      return grpc::Status(grpc::StatusCode::INVALID_ARGUMENT,
                          "key must not be empty");
    }
    if (context->IsCancelled()) {
      return grpc::Status(grpc::StatusCode::CANCELLED, "cancelled");
    }
    if (context->deadline() <= std::chrono::system_clock::now()) {
      return grpc::Status(grpc::StatusCode::DEADLINE_EXCEEDED,
                          "deadline already passed");
    }

    std::lock_guard<std::mutex> lock(mu_);
    auto it = map_.find(request->key());
    if (it == map_.end()) {
      return grpc::Status(grpc::StatusCode::NOT_FOUND, "key not found");
    }
    response->set_value(it->second.value);
    response->set_version(it->second.version);
    response->set_found(true);
    return grpc::Status::OK;
  }

 private:
  struct Entry {
    std::string value;
    uint64_t version;
  };
  std::mutex mu_;
  std::map<std::string, Entry> map_;
};
```

同步服务端的线程模型需要说清楚：

- gRPC 内部有一个线程池负责接收连接、读取请求、调用你的处理函数、写回响应。你的处理函数运行在其中一个线程上。
- 你的处理函数阻塞，就占住这个线程。线程池大小有上限，占满之后新调用只能排队。
- 处理函数返回后，gRPC 负责序列化响应并发送。你在函数返回后继续持有 `request` 或 `response` 指针是危险的，因为它们的生命周期由框架管理。
- 如果处理函数里再发起一个同步 RPC（调用下游服务），当前线程会被下游调用继续占用。高并发下这容易形成线程池耗尽甚至跨服务死锁。

同步 API 也能取消，但取消动作通常来自另一个线程。`grpc::ClientContext::TryCancel()` 可以从别的线程调用，用来请求取消本次调用：

```cpp
grpc::ClientContext context;
context.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::seconds(5));

kv::GetRequest request;
request.set_key("slow-key");
kv::GetResponse response;

std::thread canceller([&context] {
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  context.TryCancel();
});

grpc::Status status = stub->Get(&context, request, &response);
canceller.join();

if (status.error_code() == grpc::StatusCode::CANCELLED) {
  std::cout << "call was cancelled\n";
}
```

这里必须保证 `context` 在 `TryCancel()` 调用时仍然存活，因此 canceller 线程要在 `stub->Get` 返回后、`context` 析构前 `join`。`TryCancel()` 是“请求取消”，不是“等待取消完成”；它返回后，调用可能还要过一小段时间才会在客户端收敛。取消也不会回滚服务端已经完成的副作用，这一点与 f-model 课的结论一致。

服务端检测取消的方式是 `context->IsCancelled()`，或者在长时间循环里周期性检查 deadline。对于同步处理函数，如果业务操作本身不可中断，`IsCancelled()` 只能在操作前后做检查，无法强行打断一次阻塞的数据库调用。这也是为什么“取消”在接口上是一个协作式信号，而不是强杀。

---

### 2.4 异步 API：completion queue 与 tag 生命周期

同步 API 的代价是：一次调用在返回前一直占着一个线程。前面的微信后台如果要同时处理几千个 `Get`，要么开几千个线程，要么接受排队。gRPC 的异步 API 换了一个模型：发起调用后立刻返回，完成事件稍后由 `CompletionQueue` 统一汇报。等待不再绑定在线程上，少量线程就能驱动大量在途调用。

这一节只看三件事：完成队列、tag、以及状态对象的生命周期。它们决定了异步代码能否写对。

#### CompletionQueue 与 Next/AsyncNext

每个异步调用在发起时都要登记一个完成队列，并附带一个 `void* tag`。调用完成（成功、失败、取消、队列关闭）时，gRPC 会把这个 tag 原样放回队列。你的主循环从队列里取出 tag，就知道“是哪一个调用”有了进展。

`CompletionQueue::Next(bool* ok, void** tag)` 会阻塞，直到有事件或队列关闭：

```cpp
void* tag = nullptr;
bool ok = false;
while (cq.Next(&tag, &ok)) {
  // 有事件；tag 指向发起调用时登记的 tag，
  // ok 表示这个事件是成功(true)还是失败/取消(false)
}
// Next 返回 false：队列已 Shutdown 且事件已取完
```

`AsyncNext(void** tag, bool* ok, deadline)` 多了超时，适合和其它事件循环（比如定时任务、其它队列）合并在一个线程里轮询：

```cpp
void* tag = nullptr;
bool ok = false;
while (true) {
  auto status = cq.AsyncNext(
      &tag, &ok,
      std::chrono::system_clock::now() + std::chrono::milliseconds(100));
  if (status == grpc::CompletionQueue::NextStatus::SHUTDOWN) break;
  if (status == grpc::CompletionQueue::NextStatus::TIMEOUT) {
    DoHousekeeping();   // 没有完成事件，做点别的事
    continue;
  }
  // GOT_EVENT
  HandleEvent(tag, ok);
}
```

注意 `ok` 的语义：它不是业务状态，而是“这次完成事件是否正常”。一次成功的 unary 调用结束时 `ok` 为 true；调用被取消、连接断开、或队列关闭导致事件失败时可以是 false。真正的业务结果要看 `Status`，两者不能互相替代。

#### tag 不是编号，而是状态对象指针

gRPC 不解释 tag，只负责把它还回来。因此最自然的用法不是维护一张 `id -> 对象` 的全局表，而是直接让 tag 指向这次调用的状态对象：

```cpp
struct Call {
  grpc::ClientContext context;
  kv::GetRequest request;
  kv::GetResponse response;
  grpc::Status status;
};

Call* call = new Call();
stub->async()->Get(&call->context, &call->request, &call->response,
                   [call](grpc::Status status) {
                     call->status = status;
                     // 这里回调完成后，才能安全地释放 call
                   });
```

上面是 callback 风格的客户端；在纯 completion queue 风格里，发起调用时传入的 tag 就是 `call` 本身（或者 `CallData*`）。取事件时：

```cpp
void* tag = nullptr;
bool ok = false;
cq.Next(&tag, &ok);
Call* call = static_cast<Call*>(tag);
// 此时 call 仍然必须存活，因为事件刚刚才把它还回来
```

关键词是“仍然”。tag 能在队列里出现，说明发起调用时那块内存被登记过，但 gRPC 并不替你管理它的生命周期。谁 `new` 的，谁负责在最后一个完成事件之后 `delete`。

#### CallData 状态机：CREATE / PROCESS / FINISH

服务端异步 unary 的官方写法是“每个在途请求一个状态对象”，常见命名 `CallData`。它把一次调用拆成三个阶段：

- `CREATE`：向服务注册“我要接一个请求”，登记 tag。此时还没有请求进来。
- `PROCESS`：请求到了，处理业务，调用 `Finish` 写回响应，再登记下一次完成事件。
- `FINISH`：完成事件回来了，这次调用彻底结束，可以销毁对象。

```cpp
class CallData {
 public:
  CallData(kv::Kv::AsyncService* service, grpc::ServerCompletionQueue* cq)
      : service_(service), cq_(cq), responder_(&ctx_), state_(CREATE) {
    Proceed();   // 构造即进入 CREATE，登记接收请求
  }

  void Proceed(bool ok = true) {
    switch (state_) {
      case CREATE:
        if (!ok) { delete this; return; }   // 队列关闭，不再接新请求
        state_ = PROCESS;
        service_->RequestGet(&ctx_, &request_, &responder_, cq_, cq_, this);
        break;

      case PROCESS: {
        // 先为“下一个请求”创建新的状态对象，保证并发接续。
        new CallData(service_, cq_);

        std::string value;
        if (store_.Get(request_.key(), &value)) {
          reply_.set_value(value);
          state_ = FINISH;
          responder_.Finish(reply_, grpc::Status::OK, this);
        } else {
          state_ = FINISH;
          responder_.Finish(
              reply_,
              grpc::Status(grpc::StatusCode::NOT_FOUND, "no such key"),
              this);
        }
        break;
      }

      case FINISH:
        // 到这里 gRPC 已经不再使用 responder_/ctx_/request_/reply_。
        delete this;
        break;
    }
  }

 private:
  enum State { CREATE, PROCESS, FINISH };

  kv::Kv::AsyncService* service_;
  grpc::ServerCompletionQueue* cq_;
  grpc::ServerContext ctx_;
  kv::GetRequest request_;
  kv::GetResponse reply_;
  grpc::ServerAsyncResponseWriter<kv::GetResponse> responder_;
  State state_;
};
```

几个容易看漏的点：

- `RequestGet(..., this)` 和 `responder_.Finish(..., this)` 都把 `this` 当作 tag。同一个对象在不同阶段会被还回来，靠 `state_` 区分当前该做什么。
- `PROCESS` 里必须立刻 `new CallData`，否则这个对象转去 `FINISH` 之后，就没有对象在接收下一个请求了，服务会“接完一个就停”。
- `Finish` 只是“提交响应”，不是“响应已发送”。提交之后还有一次完成事件，所以不能马上删除。

#### 什么时候 delete this

结论只有一句：**最后一个完成事件被处理完之前，状态对象不能释放。**

把这句话落到代码上，就是三条禁令：

1. `RequestGet` 之后不要删。请求还没来，gRPC 随时可能通过 `ctx_`、`request_` 写入数据。
2. `Finish` 调用之后不要删。`Finish` 是异步提交，响应可能还在发送，`reply_` 和 `responder_` 仍被使用。
3. 只有在 `FINISH` 分支、拿到该调用的完成事件之后才 `delete this`。

如果处理过程中 `ok == false`，说明这次调用没有正常走到预期阶段（客户端取消、连接断开、服务关闭等）。此时仍然要把状态机走完并释放对象，只是不再执行业务逻辑或不再发送响应：

```cpp
case PROCESS:
  if (!ok) {          // 请求没有真正到达，或已被取消
    delete this;
    return;
  }
  // ... 正常处理
```

服务关闭时，队列里可能还压着未处理的 tag。正确的收尾顺序是先 `server_->Shutdown()` 停止接收新请求，再 `cq_->Shutdown()` 让 `Next` 最终返回 false，然后把剩余事件取完并逐个释放。工程上常用一个哨兵 tag（例如 `nullptr`）来判断“队列已经排空”：

```cpp
server_->Shutdown();
cq_->Shutdown();

void* tag = nullptr;
bool ok = false;
while (cq_->Next(&tag, &ok)) {
  if (tag == nullptr) continue;        // 哨兵，不代表具体调用
  static_cast<CallData*>(tag)->Proceed(ok);
}
```

不要用 `detach` 式线程去“猜”某个对象还有没有事件没回来。异步 API 里所有生命周期判断都必须由完成事件驱动，这也是它比同步 API 难写的地方。

#### 客户端异步不是必须用 CallData

服务端用 `CallData` 是因为每个在途请求都要独立保存 `context/request/response/responder`。客户端如果只是少量并发，用 callback API（下一节）通常更省事；如果要把大量请求塞进同一个事件循环，则同样用“状态对象 + tag”的模式：

```cpp
struct GetCall {
  grpc::ClientContext context;
  kv::GetRequest request;
  kv::GetResponse response;
  grpc::Status status;
  std::unique_ptr<grpc::ClientAsyncResponseReader<kv::GetResponse>> reader;
};

GetCall* call = new GetCall();
call->request.set_key(key);
call->context.set_deadline(std::chrono::system_clock::now() +
                           std::chrono::milliseconds(200));

grpc::CompletionQueue* cq = &cq_;
call->reader = stub->AsyncGet(&call->context, call->request, cq);
call->reader->Finish(&call->response, &call->status, call);

// 事件循环里：
// static_cast<GetCall*>(tag) 取回对象
// 读取 status/response，然后 delete call
```

`AsyncGet` 返回 reader 后，`Finish(..., call)` 登记的是“整个调用结束”的事件。在这个事件被取到之前，`call` 里的 `context`、`request`、`response` 都必须保持有效。

### 2.5 callback API 与生命周期

callback API 是在 completion queue 之上包了一层：你不用自己写事件循环，gRPC 在完成时直接调用你提供的回调。服务端用 `CallbackService`，客户端用 `stub->async()`。

#### ServerUnaryReactor 与 CallbackService

服务定义不变，生成的基类换成 `CallbackService`。unary 处理函数返回一个 `ServerUnaryReactor*`：

```cpp
class KvServiceImpl final : public kv::Kv::CallbackService {
 public:
  grpc::ServerUnaryReactor* Get(
      grpc::CallbackServerContext* context,
      const kv::GetRequest* request,
      kv::GetResponse* response) override {
    grpc::ServerUnaryReactor* reactor = context->DefaultReactor();

    std::string value;
    if (store_.Get(request->key(), &value)) {
      response->set_value(value);
      reactor->Finish(grpc::Status::OK);
    } else {
      reactor->Finish(
          grpc::Status(grpc::StatusCode::NOT_FOUND, "no such key"));
    }
    return reactor;
  }
};
```

要点：

- `request`/`response` 的所有权仍然属于 gRPC，不能保存指针到函数返回之后；要异步处理，先把数据拷贝出来。
- `DefaultReactor()` 返回的 reactor 由 gRPC 持有，**不要 delete**。
- 处理函数返回后，框架会等待 reactor 上的 `Finish`。不调用 `Finish`，这次调用永远不会结束。

如果业务需要放到工作线程，做法是把需要的数据拷贝出去，立刻返回 reactor：

```cpp
grpc::ServerUnaryReactor* Get(
    grpc::CallbackServerContext* context,
    const kv::GetRequest* request,
    kv::GetResponse* response) override {
  auto* reactor = context->DefaultReactor();
  const std::string key = request->key();   // 拷贝，别存 request 指针

  pool_.Submit([reactor, response, key] {
    std::string value;
    if (store_.Get(key, &value)) {
      response->set_value(value);
      reactor->Finish(grpc::Status::OK);
    } else {
      reactor->Finish(grpc::Status(grpc::StatusCode::NOT_FOUND, "no such key"));
    }
  });

  return reactor;   // 立刻返回，回调线程不被阻塞
}
```

#### 不能阻塞回调线程

gRPC 用一组共享线程来执行回调和 reactor 的 `OnDone`。你在回调里 `sleep`、等锁、做慢查询，占的不是“这一次调用的线程”，而是“整个服务的回调线程”。占满之后，所有 RPC 都会卡住。

所以 callback API 的纪律是：

- 回调里只做轻量工作：拷贝数据、`Finish`、投递任务。
- 耗时操作交给自己的线程池，并把 `response` 的写入也放在那里（`response` 在调用完成前有效）。
- 不要在回调里发起另一个同步 RPC 并等待，否则等于把回调线程拖住。

`OnDone` 是这个 reactor 的终点通知，需要清理自定义资源时重写它：

```cpp
class GetReactor : public grpc::ServerUnaryReactor {
 public:
  void OnDone() override {
    // 本次调用彻底结束，可以释放自己附加的上下文
    delete this;
  }
};
```

（自己 `new` 的 reactor 才由自己 `delete`；`DefaultReactor()` 返回的不要删。）

#### 客户端：ClientContext 与回调的生命周期

客户端 callback 调用长这样：

```cpp
grpc::ClientContext context;
context.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::milliseconds(300));

kv::GetRequest request;
request.set_key("user:42");
kv::GetResponse response;

std::promise<grpc::Status> done;
stub->async()->Get(&context, &request, &response,
                   [&done](grpc::Status status) {
                     done.set_value(status);
                   });

grpc::Status status = done.get_future().get();
```

与同步 API 最大的区别是：`Get` 立刻返回，回调在 gRPC 的线程上执行。因此：

- `context`、`request`、`response` 必须活到回调执行完。它们通常是栈对象，所以要保证函数在回调结束前不返回（上面的 `future.get()` 就起这个作用）。
- 一个 `ClientContext` 只能用于一次调用，不能复用；`set_deadline`、`TryCancel` 都要在发起调用前或调用期间、且对象存活时进行。
- 回调返回后，gRPC 不再使用这些参数，可以安全销毁。

如果不想阻塞等待，就把 `context`、`request`、`response` 一起放进堆上的状态对象，在回调里写结果并释放，这与 2.4 的 tag 生命周期是同一条规则。

### 2.6 deadline 与取消

截止时间不是可选项。任何一个 RPC 都可能遇到下游变慢、网络卡住、服务端排队，没有 deadline 的调用会把客户端线程和连接资源一起挂住。微信后台的接口调用链里，**所有 RPC 都应当有 deadline**，并且下游的 deadline 要小于上游剩余预算。

```cpp
grpc::ClientContext context;
context.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::milliseconds(200));

grpc::Status status = stub->Get(&context, request, &response);
if (status.error_code() == grpc::StatusCode::DEADLINE_EXCEEDED) {
  // 预算耗尽，按超时处理；不要把它当成“服务端一定没执行”
}
```

几个语义边界：

- `DEADLINE_EXCEEDED` 表示客户端在预算内没拿到最终结果。服务端可能已经执行、可能执行到一半、也可能还没开始，不能假定幂等之外的任何事。
- deadline 是整体预算，不是单次重试预算。重试时应基于剩余时间重新计算，而不是每次都给完整 timeout。
- 服务端通过 `context->IsCancelled()` 感知取消。deadline 到期、客户端 `TryCancel`、连接断开，都会体现为取消信号。

```cpp
grpc::Status KvServiceImpl::Get(grpc::ServerContext* context,
                                const kv::GetRequest* request,
                                kv::GetResponse* response) {
  for (int i = 0; i < 100; ++i) {
    if (context->IsCancelled()) {
      return grpc::Status(grpc::StatusCode::CANCELLED, "client gone");
    }
    // ... 分片处理
  }
  // ...
}
```

主动取消用 `ClientContext::TryCancel()`，可以在另一个线程调用：

```cpp
std::thread([&context] {
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  context.TryCancel();
}).detach();
// 注意：context 必须比这个线程活得更久，detach 只适合演示
```

取消是协作式的：`TryCancel()` 只负责发出信号，不等待服务端停下，也不回滚已经产生的副作用。服务端只有在检查 `IsCancelled()` 或框架尝试写响应时才会感知。所以长耗时循环要主动周期检查，不能指望框架替你打断。

把两节合起来看，异步代码的验收标准其实很朴素：**每个 RPC 都有 deadline；每个异步状态对象都活到最后一个完成事件之后才释放。** 前者防止调用无限挂起，后者防止悬空指针和重复释放。
## 常见误区与设计权衡

前面三节分别讲了服务与 stub、unary 与 stream、同步 API。真正写进后台服务时，问题往往不在“会不会调”，而在“线程模型和对象生命周期有没有对齐”。gRPC C++ 提供了同步、完成队列异步、callback 异步三套接口，它们看起来只是写法不同，实际上对“谁在哪个线程上跑、对象活到什么时候”的约定完全不同。混用这些约定，是这一节要展开的核心误区。

下面的例子都围绕一个 KV 服务展开：微信公众号后台读取用户配置、写入草稿状态、订阅变更流。场景不复杂，但足以暴露生命周期问题。

### 误区一：异步状态对象在最后完成事件之前被释放

完成队列（completion queue）异步 API 里，`ClientContext`、请求、响应、`ClientAsyncResponseReader` 都要活到“这一个 RPC 的完成事件被 `Next()` 取出来”为止。最常见的错误是让这些对象留在栈上，函数一返回就析构，而此时完成队列里还挂着一个未取出的 tag。

错误示例：

```cpp
void GetConfigAsync(Kv::Stub* stub, const std::string& key,
                    grpc::CompletionQueue* cq) {
  grpc::ClientContext context;
  kv::GetRequest request;
  request.set_key(key);
  kv::GetResponse response;

  auto rpc = stub->AsyncGet(&context, request, cq);
  rpc->Finish(&response, &status_, (void*)1);
}  // context/request/response/rpc 全部在这里析构，但完成事件还没被取走
```

这段代码能编译通过，甚至偶尔“看起来正常”，因为完成事件可能在析构前就到达了。但只要网络慢一点、服务端处理久一点，`Finish` 写回的目标内存已经释放，就是未定义行为。另一个变体是把 `rpc` 用 `std::unique_ptr` 管起来，却在发起后立刻 `reset()`。

正确做法是把这些对象的所有权交给一个“调用状态”结构，由完成事件的处理逻辑负责销毁：

```cpp
struct GetCall {
  grpc::ClientContext context;
  kv::GetRequest request;
  kv::GetResponse response;
  grpc::Status status;
  std::unique_ptr<grpc::ClientAsyncResponseReader<kv::GetResponse>> rpc;
};

void StartGet(Kv::Stub* stub, const std::string& key,
              grpc::CompletionQueue* cq) {
  auto call = std::make_unique<GetCall>();
  call->request.set_key(key);
  call->rpc = stub->AsyncGet(&call->context, call->request, cq);
  call->rpc->Finish(&call->response, &call->status, call.get());
  // 所有权转交给完成事件处理方，函数返回不再析构 call
  pending_.emplace(call.get(), std::move(call));
}

void OnGetDone(void* tag, bool ok) {
  auto it = pending_.find(tag);
  std::unique_ptr<GetCall> call = std::move(it->second);
  pending_.erase(it);
  if (ok && call->status.ok()) {
    // 使用 call->response
  }
}  // 这里 call 才析构
```

判断标准很简单：**谁在完成事件里读这个对象，谁就要保证对象那时还活着**。栈上的局部变量几乎总是不满足这个条件。析构顺序也要注意：通常先等完成队列 `Shutdown` 并排空，再销毁保存调用状态的容器，否则连“最后一批事件”都没机会处理。

流式 RPC 更容易踩坑。`ClientAsyncReaderWriter` 的 `Read`、`Write`、`WritesDone`、`Finish` 各自会产生完成事件，必须等 `Finish` 的 tag 被取出后才能释放流对象。只等 `Read` 结束就销毁，是典型的提前释放。

### 误区二：在 callback 线程里做阻塞调用

callback API 的 reactor 回调（`OnDone`、`OnReadDone`、客户端的完成 lambda）跑在 gRPC 自己的线程池上。这些线程数量有限，是整个进程共享的事件处理资源。

错误示例：

```cpp
grpc::ServerUnaryReactor* Get(grpc::CallbackServerContext* ctx,
                              const kv::GetRequest* req,
                              kv::GetResponse* resp) override {
  auto* reactor = ctx->DefaultReactor();
  // 在回调线程里同步查 MySQL / 同步调下游 RPC，可能阻塞几百毫秒
  std::string v = mysql_client_.QueryBlocking(req->key());
  resp->set_value(v);
  reactor->Finish(grpc::Status::OK);
  return reactor;
}
```

公众号后台高峰期（比如大号推送后粉丝集中打开文章），这样的阻塞会把回调线程全部占满，连本来很快的请求也排不上队，表现为整体延迟飙升而 CPU 并不高。

正确做法：回调里只做非阻塞工作；需要等待的下游调用改成异步（callback 版本的下游 stub 调用），或者把工作投递到业务自己的线程池，完成后再调用 `reactor->Finish()`。记住 `Finish()` 可以在任意线程调用，但只能调用一次。

### 误区三：用同步 API 的线程假设去写异步或 callback 代码

同步服务端的约定是：一个请求占用一个 gRPC 管理的线程，handler 返回即调用结束，栈上的对象在 handler 内一直有效。很多人把这个心智模型直接搬到异步 API：

- 以为 handler 返回后请求就结束了，于是在 callback 方法返回前就析构了 `resp` 依赖的数据；
- 以为每个请求有独立线程，于是在 CallData 里用了不加锁的共享状态，而多个线程同时 `Next()` 同一个完成队列时，这些状态会被并发访问；
- 在一个 service 里一部分方法用同步、一部分用异步（`WithAsyncMethod_Xxx` 混合），却不清楚同步方法占用的是 gRPC 线程、异步方法依赖你自己的轮询线程，结果完成队列没人 `Next()`，异步方法全部挂住。

判断方法：写代码前先回答两个问题，“这段代码在哪个线程执行”“它引用的每个对象由谁、在什么事件之后释放”。三种 API 的答案不同，不能互相套用。

### 误区四：忘了给 RPC 设置 deadline

gRPC 默认没有截止时间。客户端不设 `set_deadline`，下游一旦卡住，调用就会无限等待，同步 API 下表现为线程被永久占住，异步 API 下表现为状态对象永远不释放、内存缓慢增长。这正是本课 acceptance 第一条「所有RPC有deadline」的由来。

正确做法是在创建 `ClientContext` 的地方统一设置，并且从上游剩余预算推导，而不是每处写死一个常数：

```cpp
grpc::ClientContext ctx;
ctx.set_deadline(std::chrono::system_clock::now() +
                 std::chrono::milliseconds(remaining_budget_ms));
```

服务端也可以在入口检查 `context->deadline()`，剩余时间已经不够做完工作时直接返回，避免做无用功。

### 误区五：复用 ClientContext

`ClientContext` 是一次调用的上下文，记录 deadline、metadata、取消状态和调用结果。官方明确说它不能在多次 RPC 之间复用。常见错误是把它做成成员变量或放进连接池对象里“省一次构造”，结果第二次调用行为异常甚至崩溃。

正确做法：每次 RPC 新建一个 `ClientContext`，和请求、响应一起放在本次调用的状态对象里，随调用结束一起释放。需要共享的是 `Channel` 和 `Stub`，它们是线程安全、可复用的。

### 权衡一：用成熟框架，还是自己写协议

本节点的 tradeoffs 是：成熟框架降低协议成本，但依赖、调试和部署体积需评估。

| 维度 | 使用 gRPC | 自研轻量 RPC |
|---|---|---|
| 协议成本 | HTTP/2、流控、deadline、取消、状态码都现成 | 都要自己设计和测试 |
| 跨语言 | 由 proto 生成多语言 stub | 需要每种语言各写一遍 |
| 依赖 | 引入 gRPC、protobuf、abseil 等，版本对齐有成本 | 依赖少，可控 |
| 调试 | 栈很深，问题可能在框架内部，需要读源码或开 trace 日志 | 代码都是自己的，好定位 |
| 部署体积 | 静态链接后二进制明显变大 | 小 |

在公司里，这个选择通常已经由基础架构团队做好（很多大厂内部有自己的 RPC 框架），你要做的是理解成熟框架替你解决了哪些问题，以便在内部框架里找到对应机制。

### 权衡二：同步、completion queue 异步、callback 三选一

| | 同步 API | completion queue 异步 | callback API |
|---|---|---|---|
| 写法难度 | 最低，像普通函数 | 最高，要手写状态机 | 中等 |
| 线程模型 | 一个调用占一个线程 | 你自己创建线程轮询 `Next()` | gRPC 内部线程池调回调 |
| 生命周期 | 跟随函数栈 | 状态对象由你在最后一个完成事件后释放 | reactor 在 `OnDone` 之后才能释放 |
| 适合场景 | 并发不高的管理接口、工具 | 需要精细控制线程和调度的高并发服务 | 新代码中的高并发服务，官方更推荐 |
| 主要风险 | 并发高时线程耗尽 | 提前释放、tag 混乱 | 回调里阻塞、在 `OnDone` 前释放 |

一个实用的起点：先用同步 API 把业务和 proto 跑通，有了压测数据证明线程成了瓶颈，再迁移到 callback 或异步。迁移时整条调用链的生命周期约定都要重新审视，不能只改方法签名。
## 动手实践：按官方C++教程实现KV unary服务，再加入截止时间和取消

本节把前面的概念落成一条可跑的链路。我们沿用官方 C++ 教程的骨架：先用 `kv.proto` 定义 `Get` / `Put` 两个 unary 方法，用同步 API 跑通服务端和客户端，再逐步加上 deadline、服务端取消检查和客户端 `TryCancel()`。最后给出异步 `CallData` 版本的最小改写方向。

目标：走通一条 RPC 从 proto 到 stub 的完整链路，让每个调用都带 deadline，并能在服务端观察到取消；最后能说清异步版本里状态对象为何不能提前释放。

建议在 Linux（Ubuntu 22.04 或同类环境）下完成。下面所有命令都在同一个工程目录里执行。

### 步骤 1：环境准备

两条路线，二选一即可。

**路线 A：系统包（最快跑通）**

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git \
    libgrpc++-dev libprotobuf-dev protobuf-compiler-grpc \
    pkg-config
```

确认版本与工具都在：

```bash
protoc --version
grpc_cpp_plugin --version || which grpc_cpp_plugin
pkg-config --modversion grpc++
```

**路线 B：官方 CMake 构建（版本可控，推荐写进实验记录）**

gRPC 的 C++ 快速上手文档给的是 CMake 的 `FetchContent` / `add_subdirectory` 方式。把 gRPC 作为子模块或源码目录引入时，构建时间会比较长，建议先装好基础依赖：

```bash
sudo apt-get install -y build-essential cmake git pkg-config \
    libssl-dev zlib1g-dev
```

然后在工程里建一个 `third_party/` 目录放置 gRPC 源码（版本以你实验当天官方文档标注的 release 为准，并在报告里记录 commit/tag）。注意：不同 gRPC 版本对 CMake 最低版本、Abseil 依赖的要求不同，混用系统包和源码构建很容易出现符号冲突，选定一条路线后不要中途切换。

> 记录一条实验环境信息：`uname -a`、`cmake --version`、gRPC 版本。后面排查“为什么我这边编译不过”时，这一行往往最关键。

### 步骤 2：写 kv.proto

新建目录结构：

```bash
mkdir -p kv/proto kv/server kv/client
```

`kv/proto/kv.proto`：

```proto
syntax = "proto3";

package kv;

service KV {
  // 读取一个 key，不存在时 found=false
  rpc Get(GetRequest) returns (GetResponse);
  // 写入一个 key
  rpc Put(PutRequest) returns (PutResponse);
}

message GetRequest {
  string key = 1;
}

message GetResponse {
  bool found = 1;
  string value = 2;
}

message PutRequest {
  string key = 1;
  string value = 2;
}

message PutResponse {
  bool ok = 1;
}
```

两个方法都是“一请求一响应”，正好覆盖 unary。字段编号一旦发布就不要改，proto3 里 `bool` 默认 `false`，所以 `found=false` 不需要额外标志位。

代码由下面 CMake 自动生成。想先看产物可手动执行 `protoc -I kv/proto --cpp_out=. --grpc_out=. --plugin=protoc-gen-grpc=$(which grpc_cpp_plugin) kv/proto/kv.proto`，得到 `kv.pb.*`（消息序列化）与 `kv.grpc.pb.*`（服务与 stub 胶水），生成物不要提交进仓库。

### 步骤 3：CMakeLists.txt 里生成代码

在工程根目录写 `kv/CMakeLists.txt`。思路是：用 `protobuf_generate_cpp` 生成消息代码，用 `protobuf_generate`（或自定义命令）调 `grpc_cpp_plugin` 生成服务代码，再把生成的头文件目录导出给 `server` / `client` 两个目标。

```cmake
cmake_minimum_required(VERSION 3.16)
project(kv_lab CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Protobuf REQUIRED)
find_package(gRPC CONFIG REQUIRED)

set(PROTO_SRC ${CMAKE_CURRENT_SOURCE_DIR}/proto/kv.proto)
set(GEN_DIR   ${CMAKE_CURRENT_BINARY_DIR}/gen)
file(MAKE_DIRECTORY ${GEN_DIR})

# 1) 消息代码
add_custom_command(
  OUTPUT
    ${GEN_DIR}/kv.pb.cc ${GEN_DIR}/kv.pb.h
  COMMAND ${Protobuf_PROTOC_EXECUTABLE}
  ARGS --proto_path=${CMAKE_CURRENT_SOURCE_DIR}/proto
       --cpp_out=${GEN_DIR}
       ${PROTO_SRC}
  DEPENDS ${PROTO_SRC}
  COMMENT "Generating kv.pb.*")

# 2) 服务/stub 代码
add_custom_command(
  OUTPUT
    ${GEN_DIR}/kv.grpc.pb.cc ${GEN_DIR}/kv.grpc.pb.h
  COMMAND ${Protobuf_PROTOC_EXECUTABLE}
  ARGS --proto_path=${CMAKE_CURRENT_SOURCE_DIR}/proto
       --grpc_out=${GEN_DIR}
       --plugin=protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>
       ${PROTO_SRC}
  DEPENDS ${PROTO_SRC} gRPC::grpc_cpp_plugin
  COMMENT "Generating kv.grpc.pb.*")

add_library(kv_proto STATIC
  ${GEN_DIR}/kv.pb.cc
  ${GEN_DIR}/kv.grpc.pb.cc)
target_include_directories(kv_proto PUBLIC ${GEN_DIR})
target_link_libraries(kv_proto PUBLIC gRPC::grpc++ protobuf::libprotobuf)

add_executable(kv_server server/kv_server.cc)
target_link_libraries(kv_server PRIVATE kv_proto)

add_executable(kv_client client/kv_client.cc)
target_link_libraries(kv_client PRIVATE kv_proto)
```

几个容易踩的点：

- `find_package(gRPC CONFIG REQUIRED)` 要求 gRPC 提供了 CMake config；用系统包路线时如果找不到，先确认 `libgrpc++-dev` 装上了，再检查 `CMAKE_PREFIX_PATH`。
- `gRPC::grpc_cpp_plugin` 是导入目标，用 `$<TARGET_FILE:...>` 取路径，比硬编码 `which` 更稳。
- 生成目录必须加进 include path，否则 `#include "kv.grpc.pb.h"` 找不到。

构建命令：

```bash
cmake -S kv -B kv/build -DCMAKE_BUILD_TYPE=Release
cmake --build kv/build -j"$(nproc)"
```

### 步骤 4：同步服务端实现

`kv/server/kv_server.cc`：用一个 `std::mutex` 保护的 `std::unordered_map` 当内存 KV，够验证语义即可。

```cpp
#include <chrono>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>

#include <grpcpp/grpcpp.h>
#include "kv.grpc.pb.h"

namespace {

class KVServiceImpl final : public kv::KV::Service {
 public:
  grpc::Status Get(grpc::ServerContext* ctx,
                   const kv::GetRequest* req,
                   kv::GetResponse* resp) override {
    const std::string key = req->key();
    std::cout << "Get key=" << key << std::endl;

    // 可选：模拟慢请求，便于观察取消。正常路径不要 sleep。
    if (key == "slow-key") {
      for (int i = 0; i < 50; ++i) {
        if (ctx->IsCancelled()) {
          std::cout << "Get key=" << key << " cancelled by client\n";
          return grpc::Status(grpc::StatusCode::CANCELLED,
                              "client cancelled");
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
    }

    std::lock_guard<std::mutex> lock(mu_);
    auto it = data_.find(key);
    if (it == data_.end()) {
      resp->set_found(false);
      return grpc::Status::OK;
    }
    resp->set_found(true);
    resp->set_value(it->second);
    return grpc::Status::OK;
  }

  grpc::Status Put(grpc::ServerContext* ctx,
                   const kv::PutRequest* req,
                   kv::PutResponse* resp) override {
    if (ctx->IsCancelled()) {
      return grpc::Status(grpc::StatusCode::CANCELLED, "client cancelled");
    }
    {
      std::lock_guard<std::mutex> lock(mu_);
      data_[req->key()] = req->value();
    }
    resp->set_ok(true);
    return grpc::Status::OK;
  }

 private:
  std::mutex mu_;
  std::unordered_map<std::string, std::string> data_;
};

}  // namespace

int main(int argc, char** argv) {
  std::string addr = argc > 1 ? argv[1] : "0.0.0.0:50051";

  KVServiceImpl service;
  grpc::ServerBuilder builder;
  builder.AddListeningPort(addr, grpc::InsecureServerCredentials());
  builder.RegisterService(&service);

  std::unique_ptr<grpc::Server> server = builder.BuildAndStart();
  if (!server) {
    std::cerr << "failed to start server on " << addr << std::endl;
    return 1;
  }
  std::cout << "kv_server listening on " << addr << std::endl;
  server->Wait();
  return 0;
}
```

要点回顾：`RegisterService` 传入的 `&service` 必须活到 `server->Wait()` 返回；`IsCancelled()` 是协作式检查，只能在循环里周期性调用，无法打断一次已经阻塞的底层调用。这里的 `sleep` 只是为了让你肉眼看到取消生效，真实业务应替换成可中断的分段处理。

### 步骤 5：客户端带 deadline

`kv/client/kv_client.cc` 用一个命令行子命令区分场景，避免反复改代码。核心是每次调用前都设置 `set_deadline`。

```cpp
#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <thread>

#include <grpcpp/grpcpp.h>
#include "kv.grpc.pb.h"

namespace {

std::unique_ptr<kv::KV::Stub> MakeStub(const std::string& target) {
  return kv::KV::NewStub(grpc::CreateChannel(
      target, grpc::InsecureChannelCredentials()));
}

grpc::Status DoPut(kv::KV::Stub* stub, const std::string& k,
                   const std::string& v) {
  grpc::ClientContext ctx;
  ctx.set_deadline(std::chrono::system_clock::now() +
                   std::chrono::seconds(3));
  kv::PutRequest req;
  req.set_key(k);
  req.set_value(v);
  kv::PutResponse resp;
  grpc::Status st = stub->Put(&ctx, req, &resp);
  std::cout << "Put status=" << st.error_code()
            << " ok=" << resp.ok() << std::endl;
  return st;
}

// timeout_ms 同时覆盖普通读取与短 deadline 两种场景
grpc::Status DoGet(kv::KV::Stub* stub, const std::string& k,
                   int timeout_ms, const char* label) {
  grpc::ClientContext ctx;
  ctx.set_deadline(std::chrono::system_clock::now() +
                   std::chrono::milliseconds(timeout_ms));
  kv::GetRequest req;
  req.set_key(k);
  kv::GetResponse resp;
  grpc::Status st = stub->Get(&ctx, req, &resp);
  std::cout << label << " status=" << st.error_code()
            << " found=" << resp.found() << " value=" << resp.value()
            << " msg=" << st.error_message() << std::endl;
  return st;
}

grpc::Status DoGetWithCancel(kv::KV::Stub* stub, const std::string& k) {
  grpc::ClientContext ctx;
  ctx.set_deadline(std::chrono::system_clock::now() +
                   std::chrono::seconds(10));
  kv::GetRequest req;
  req.set_key(k);
  kv::GetResponse resp;
  std::thread canceller([&ctx] {
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    ctx.TryCancel();
  });
  grpc::Status st = stub->Get(&ctx, req, &resp);
  canceller.join();  // 先 join，ctx 才能析构
  std::cout << "Cancel status=" << st.error_code()
            << " msg=" << st.error_message() << std::endl;
  return st;
}

}  // namespace

int main(int argc, char** argv) {
  std::string target = argc > 1 ? argv[1] : "127.0.0.1:50051";
  std::string cmd = argc > 2 ? argv[2] : "put";
  auto stub = MakeStub(target);
  if (cmd == "put")      return DoPut(stub.get(), "hello", "world").ok() ? 0 : 1;
  if (cmd == "get")      return DoGet(stub.get(), "hello", 3000, "Get").ok() ? 0 : 1;
  if (cmd == "deadline") return DoGet(stub.get(), "slow-key", 200, "ShortDeadline").ok() ? 0 : 1;
  if (cmd == "cancel")   return DoGetWithCancel(stub.get(), "slow-key").ok() ? 0 : 1;
  std::cerr << "usage: kv_client <target> <put|get|deadline|cancel>\n";
  return 2;
}
```

说明：

- `set_deadline` 的时钟是 `system_clock`，不要用 `steady_clock` 混进来。
- `DoGetWithCancel` 里 `join()` 的位置是有意为之：`ctx` 必须在 `TryCancel()` 期间存活，也必须活到 `stub->Get()` 返回。
- `TryCancel()` 只是发出取消请求，返回不代表调用已经结束；真正收敛到 `CANCELLED` 是在 `stub->Get()` 返回时看到的。
- deadline 与取消都会让 `Status` 不为 OK，但错误码不同，先打印 `error_code()` 再判断，别只写 `if (!st.ok())`。

### 步骤 6：可选——改写为异步 CallData 版本

同步版跑通后再动异步。官方异步教程的核心是一个 `CallData` 对象在“请求 → 处理 → 完成”三段之间被完成队列反复唤醒，状态对象必须活到最后一个完成事件被 `Next()` 取走。下面只给最小骨架，重点看生命周期而不是业务。

```cpp
class CallData {
 public:
  CallData(kv::KV::AsyncService* service, grpc::ServerCompletionQueue* cq,
           Store* store)
      : service_(service), cq_(cq), store_(store), responder_(&ctx_),
        state_(REQUEST) {
    Proceed();
  }

  void Proceed() {
    if (state_ == REQUEST) {
      state_ = PROCESS;
      service_->RequestGet(&ctx_, &req_, &responder_, cq_, cq_, this);
    } else if (state_ == PROCESS) {
      // 每条被唤醒的请求都要新建一个 CallData 继续接客
      new CallData(service_, cq_, store_);
      Handle(store_, req_, &resp_);
      state_ = FINISH;
      responder_.Finish(resp_, grpc::Status::OK, this);
    } else {  // FINISH
      delete this;  // 最后一个完成事件到达后才释放
    }
  }

 private:
  enum State { REQUEST, PROCESS, FINISH };
  kv::KV::AsyncService* service_;
  grpc::ServerCompletionQueue* cq_;
  Store* store_;
  grpc::ServerContext ctx_;
  kv::GetRequest req_;
  kv::GetResponse resp_;
  grpc::ServerAsyncResponseWriter<kv::GetResponse> responder_;
  State state_;
};
```

配套要点：

- 主线程循环 `cq->Next(&tag, &ok)`，`tag` 转回 `CallData*` 调 `Proceed()`；`ok == false` 表示该次调用已失败/关闭，不要假设还能读 `req_`。
- 完成队列可以有多个，线程数要和队列数匹配；不要把同一个 `CallData` 跨队列投递。
- 服务关闭时先 `server->Shutdown()`，再逐个 `cq->Shutdown()` 并把队列里剩余的 tag 全部消费掉，否则会漏 `delete`。
- 异步版不要混用同步服务端接口；同一服务实例的两种 API 线程与生命周期约束不同。

### 步骤 7：验证命令与预期输出

开两个终端。终端一启动服务端：

```bash
./kv/build/kv_server 0.0.0.0:50051
# 预期：kv_server listening on 0.0.0.0:50051
```

终端二依次执行：

```bash
./kv/build/kv_client 127.0.0.1:50051 put
# 预期：Put status=0 ok=1
./kv/build/kv_client 127.0.0.1:50051 get
# 预期：Get status=0 found=1 value=world
./kv/build/kv_client 127.0.0.1:50051 deadline
# 预期：ShortDeadline status=4 msg=Deadline Exceeded
#       （也可能先看到服务端打印 cancelled，取决于谁先收敛）
./kv/build/kv_client 127.0.0.1:50051 cancel
# 预期：Cancel status=1 msg=...
#       （error_code 1 = CANCELLED；服务端打印 cancelled by client）
```

对照错误码：`0=OK`、`1=CANCELLED`、`4=DEADLINE_EXCEEDED`。两条慢路径都应让你在服务端日志里看到 `IsCancelled()` 分支被触发。

### 步骤 8：验收清单

对照本节点的两条验收标准逐项自查：

- [ ] 所有 RPC 都有 deadline：`grep -n "set_deadline" kv/client/kv_client.cc` 应覆盖每一个 `ClientContext`；新增调用时先写 deadline 再写请求。
- [ ] 出错时能区分 `CANCELLED` 与 `DEADLINE_EXCEEDED`，并打印 `error_code()` 与 `error_message()`。
- [ ] 异步场景下，`CallData`（状态对象）在最后一个完成事件被 `Next()` 取走前不释放；对照 `Proceed()` 中 `delete this` 的位置检查。
- [ ] `TryCancel()` 的 `context` 在所有取消线程 `join()` 之前保持存活。
- [ ] 服务端慢请求循环里周期性检查 `IsCancelled()`，记录一次取消日志，并记下 gRPC / protobuf / CMake 版本便于复现。

完成以上检查，你就把“服务与 stub、unary、完成队列生命周期”在一条可运行链路上验证了一遍。
## 延伸阅读：sourceStudy

本节点的 sourceStudy 要求「沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件」。下面给出一条可复现的阅读路线，以及每一步应该落笔记录的内容。

**阅读路线（对应 grpc.io 的 C++ basics 文档）**

1. **服务定义先于代码**：先读文档开头对 `.proto` 服务、消息与四种 RPC 形态的说明。记录该文档使用的 proto3 语法版本，以及示例里的 package / service 命名。
2. **从定义到生成代码**：读它如何用 `protoc` + `grpc_cpp_plugin` 生成 `*.pb.cc/h` 与 `*.grpc.pb.cc/h`。记录生成命令、插件路径，以及生成物里 `Service`、`StubInterface`、`Stub` 三类角色的分工。
3. **服务端实现路径**：读同步服务端示例，记录 `ServerBuilder`、`AddListeningPort`、`RegisterService`、`BuildAndStart` 的调用顺序，以及 `Server::Wait()` 的阻塞语义。
4. **客户端 stub 路径**：读客户端示例，记录 `CreateChannel`、`NewStub`、`ClientContext`、`Status` 各自承担什么，以及一次 unary 调用的完整参数表。
5. **unary 与 stream 对照**：读四种 RPC 的示例，重点比较请求/响应侧的 `ReaderWriter` 用法差异。记录哪一侧是 `ClientReader`、`ClientWriter`、`ClientReaderWriter`，以及 `WritesDone()`、`Read()`、`Finish()` 的调用次序。
6. **错误处理与截止时间**：读文档中 `Status`、状态码、deadline 与取消相关段落。记录文档是否给出取消示例；若没有，按本课 2.3 的补充自行设计 `TryCancel()` 验证。
7. **异步/回调入口（若文档覆盖）**：若该版本文档包含 async 或 callback API，仅核对它如何声明完成队列或回调，不把两套 API 的线程约束混用。

**每步应记录的版本与边界**

- **工具链版本**：gRPC 版本、protobuf 版本、C++ 标准（如 C++14/17）、构建方式（CMake/Bazel）、操作系统。
- **API 形态边界**：本示例属于同步还是异步；服务端与客户端是否使用同一套 API；文档是否明确线程模型。
- **协议边界**：unary / server-streaming / client-streaming / bidirectional-streaming 中，哪些在示例里出现、哪些只是提及。
- **生命周期边界**：`ClientContext`、`ServerContext`、stream 对象、异步状态对象分别在什么时刻可以释放；文档的表述与你实测是否一致。
- **错误语义边界**：`DEADLINE_EXCEEDED` 与 `CANCELLED` 的触发条件，`Status` 是否可携带 details/metadata。
- **可复现性**：把上述记录写成简表，注明“文档版本 + 实测版本”，避免把某一版的示例当成跨版本通用结论。

阅读时不要只抄代码：每看到一个示例，先问“它依赖哪条生命周期约束”，再回到本课 2.1–2.3 的结论对照。两者不一致时，以你实际使用的 gRPC 版本实测为准，并记录差异。

---
## 自测题

### 1. 服务、stub 与 channel 的关系

**题目**：某同学在网关服务里用一个全局 `std::unique_ptr<kv::KV::Stub> g_stub` 处理所有请求，并说“stub 就是服务端对象”。请指出其中的错误，并说明 stub、channel、服务实现三者的角色与线程安全边界。

<details>
<summary>查看答案</summary>

错误有两处：

- **角色混淆**：服务端对象是实现 `kv::KV::Service` 的子类，重写 `Get`/`Put` 等方法，由 `ServerBuilder::RegisterService` 注册；stub 是客户端侧的代理，持有 channel，负责把方法调用序列化成 RPC。二者不是同一个东西。
- **“全局 stub 一定安全”的结论不严谨**：stub 本身通常可以跨线程共享（它不保存单次调用的可变状态），但**单次调用**的可变状态在 `ClientContext`、request、response 上，这些必须每次调用独立创建。把 `ClientContext` 也全局化才会出问题。

正确关系可以这样记：

- **服务实现**：定义“做什么”，运行在服务端 `Server` 的线程池里；一个实例通常被多个并发调用共享，处理函数里的成员变量需要自己加锁或改为无状态。
- **channel**：到某个目标地址的连接抽象，负责连接管理、负载均衡、重试等；应长期复用，不要每次调用 `CreateChannel`。
- **stub**：绑定在 channel 上的客户端代理，轻量、可复用；`NewStub(channel)` 得到的对象不拥有 channel 的所有权。
- **每次调用的状态**：`ClientContext`、请求/响应消息、stream 对象，都是“一调用一份”，不能跨调用复用，更不能在多线程间共享同一个 `ClientContext`。

后台场景里常见的写法是：进程启动建一个 channel，按下游服务维护少量 stub，请求处理时新建 `ClientContext` 和消息体。这样既复用了连接，又隔离了单次调用的状态。

</details>

### 2. completion queue 与异步状态对象的生命周期

**题目**：异步服务端在 `RequestGet` 之后把 `CallData*` 放进完成队列等待事件标签。有人在收到第一个事件时就 `delete call_data`，并在所有请求结束后直接析构 `Server`。请说明为什么错，以及正确的关闭顺序。

<details>
<summary>查看答案</summary>

异步 API 的核心约定是：**一个 RPC 会经历多个完成事件**（如 `REQUEST`、`READ`、`WRITE`、`FINISH`），状态对象必须活到最后一个事件被取出为止。只在第一个事件就释放，后续事件会访问已释放内存，属于悬垂指针。

常见错误还包括：

- 把 `CompletionQueue` 与 `Server` 的析构顺序弄反。直接析构 `Server` 时，队列里可能还有未取完的事件；正确做法是先 `server->Shutdown()`，再对完成队列调用 `Shutdown()`，然后持续 `Next()` 直到返回 `false`，把剩余事件全部排空，最后才析构队列和状态对象。
- 用同一个 `void*` 标签区分不了事件类型。标签通常指向携带状态的对象，或使用 `Tag` 结构体，必须能从标签恢复出“哪个调用、哪个阶段”。
- 在回调/异步路径里混用同步阻塞调用，占住完成队列的消费线程或 gRPC 内部线程。

一个可用的关闭顺序：

1. 业务侧停止发起新请求；
2. `server->Shutdown(deadline)`；
3. `cq->Shutdown()`；
4. 循环 `cq->Next(&tag, &ok)` 排空，按标签释放 `CallData`；
5. 确认没有未完成事件后，再析构 `Server`、channel、队列。

`ok == false` 表示该事件对应的操作未成功完成（例如被取消或关闭），仍然要处理并释放对应状态对象，不能当作“没有事件”跳过。

</details>

---
### 3. deadline 与取消

**题目**：客户端设置了 200ms deadline，服务端处理到一半时客户端又调用了 `TryCancel()`。请分别说明客户端可能看到哪些状态码、服务端如何感知、取消能否回滚副作用，并给出一个后台写场景下的取舍。

<details>
<summary>查看答案</summary>

客户端侧：

- 若 deadline 先到，`Status::error_code()` 通常是 `DEADLINE_EXCEEDED`。
- 若 `TryCancel()` 先生效，通常是 `CANCELLED`。
- 两者竞争时结果取决于到达顺序，不应依赖某个固定值；业务判断“是否可重试”时要把两者都考虑进去，并注意非幂等写操作不能盲目重试。

服务端侧：

- 同步处理函数可用 `context->IsCancelled()` 检查；也可以在长循环里周期性检查 `context->deadline()` 是否已过。
- 取消是**协作式**信号，不能强杀正在阻塞的数据库调用或下游 RPC；只能在操作前后检查，或把长任务拆成可检查的片段。
- `TryCancel()` 只是“请求取消”，返回时调用可能尚未在两端完全收敛。

副作用：

- 取消不会回滚服务端已经提交的写入。对微信公众号后台这类场景，例如“扣减配额后再写审计日志”，取消可能发生在两次写之间，必须靠幂等键、事务或补偿来保证一致性，而不是假设取消等于没发生。

取舍示例：写接口适合设置较短 deadline 并及时取消，避免请求线程被慢下游拖死；但写操作要配合幂等键和事务边界，读接口则可以适度放宽 deadline 并允许重试。无论哪种，**所有 RPC 都应有 deadline**，不要让默认的无限等待把线程池拖垮。

</details>

## 推荐资源

- [gRPC C++同步与异步接口：参考文档 / 原始资料](https://grpc.io/docs/languages/cpp/basics/)（grpc.io，英文，文档，进阶，免费）

**阅读重点**：服务与 stub 的对应关系、unary 与三类 stream 的示例差异，以及错误处理与状态码的表述。读的时候同步记录文档对应的 gRPC/protobuf/C++ 版本，并把“示例能跑”与“语义可迁移”分开：示例代码可以照做，线程模型、生命周期和取消语义要以你所装版本的实际行为复核。遇到文档未覆盖的取消细节，回到本课 2.3 的 `TryCancel()` 示例自行验证。

## 本课小结

- **服务与 stub 分工**：服务实现定义行为、运行在服务端线程池；stub 是绑定 channel 的客户端代理，本身可复用，但单次调用的 `ClientContext` 和消息必须独立。
- **四种 RPC 形态**：unary、server-streaming、client-streaming、bidirectional-streaming，区别在读写侧对象与调用次序，不要混用不同形态的收尾约定。
- **同步 API 的代价**：处理函数阻塞即占用 gRPC 线程；处理函数内再发起同步 RPC 会叠加占用，高并发下可能线程池耗尽。
- **异步 API 的核心**：完成队列 + 事件标签；一个 RPC 有多个完成事件，状态对象必须活到最后一个事件被取出。
- **关闭顺序**：先停新请求，再 `Server::Shutdown`、`CompletionQueue::Shutdown`，排空队列并释放状态对象，最后析构。
- **deadline 与取消**：所有 RPC 都要设 deadline；`TryCancel()` 是协作式请求，`DEADLINE_EXCEEDED` 与 `CANCELLED` 可能竞争；取消不回滚已产生的副作用。
- **工程取舍**：成熟框架降低协议成本，但依赖体积、调试复杂度和部署成本需要一并评估；不同 gRPC API 的线程与生命周期约束不可混用。
