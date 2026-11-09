# 第 85/208 课：brpc通道与服务实现

## 元信息

| 项目 | 内容 |
| --- | --- |
| 节点 ID | `f-brpc` |
| 英文名 | brpc |
| 领域 / 阶段 | F / 第 3 阶段 |
| 子类 | 协议与框架 |
| 难度 | 进阶 |
| 前置节点 | `f-grpc`、`d-coroutine` |
| 标签 | `brpc`、`F`、`C++` |
| 参考文档 | [brpc 客户端基础](https://brpc.apache.org/docs/client/basics/)（brpc.apache.org） |
| 摘要 | brpc 提供多协议服务端与客户端通道；具体能力以公开文档和所用版本为准，不代表任何企业内部体系。 |

> 边界说明：本课只讨论公开 brpc 的用法、配置与可观察行为。文中不会把公开实现推断成任何企业的内部 RPC 体系；遇到默认值、协议名、连接复用和调度细节时，请以所用版本文档与源码为准。

## 导读

brpc 是 C++ 生态中常见的 RPC 框架。它同时提供两端能力：

- 服务端：把 `Service` 实现注册到 `Server`，由框架完成网络收发、协议解析和回调调度。
- 客户端：通过 `Channel` 连接目标服务，通过 `Controller` 控制单次调用，通过生成的 `Stub` 发起请求。

如果你已经学过 gRPC，可以把 brpc 先理解成“同样在解决 RPC 基本问题，但把连接、协议、负载均衡和线程模型做成了自己的组合”。如果你学过协程，本课的 `bthread` 会是一个自然的延伸：它关注的是大量 RPC 并发下的执行单元与阻塞边界。

本课的主线只有三个对象：

| 对象 | 所在侧 | 作用 | 生命周期直觉 |
| --- | --- | --- | --- |
| `Channel` | 客户端 | 指向一个服务集群的通信通道 | 长生命周期，初始化后复用 |
| `Controller` | 客户端/服务端 | 单次 RPC 的上下文 | 一次调用一个，不能跨请求复用 |
| `Service` | 服务端 | 业务接口实现 | 随服务进程存在，注册后由框架调用 |

围绕这三个对象，我们再看连接类型、负载策略和 bthread 回调边界。课程示例使用一个最小的 Echo 服务：客户端发送一段字符串，服务端原样返回。示例只用于说明公开 API 的组织方式。

一个典型 RPC 调用在概念上会经过这些阶段：

1. 客户端业务代码准备请求对象和 `Controller`。
2. `Channel` 根据目标、协议、连接类型和负载策略选择一条可用路径。
3. 请求被序列化并通过连接发送。
4. 服务端框架解析请求，找到注册的 `Service` 方法，在合适的执行单元中调用业务实现。
5. 业务实现填写响应，并触发完成回调。
6. 客户端收到响应，业务代码检查 `Controller` 上的错误与延迟信息。

真正排障时，最怕的不是“不知道框架有多少功能”，而是不知道一次调用中哪个配置在起作用。因此本课不追求罗列所有选项，而是围绕一条最小调用链，把每个配置放回它影响的位置。
## 学习目标

学完本课后，你应当能够：

1. 用自己的话解释 `Channel`、`Controller`、`Service` 各自负责什么，以及它们的生命周期差异。
2. 说明一个 proto 定义的 service 如何落到服务端实现类，再如何被客户端 `Stub` 调用。
3. 独立构建官方 Echo 风格的示例：服务端注册服务并启动，客户端初始化通道并完成同步调用。
4. 在客户端正确设置超时、观察失败状态、读取错误文本和调用延迟。
5. 比较 `single`、`pooled`、`short` 等连接模式的适用场景，并记录自己的配置。
6. 说明 `max_retry` 与超时、幂等性之间的关系，不把重试当成无条件安全的能力。
7. 理解 `done` 回调与 `ClosureGuard` 的作用，知道异步实现时回调边界在哪里。
8. 建立“公开 brpc 实现”与“企业内部 RPC 体系”之间的边界意识。

## 为什么后端要懂

后端服务很少孤立存在。以一个通用的内容后台场景为例：消息分发服务要调用用户资料服务，素材服务要调用存储服务，运营后台要聚合多个内部接口。每一次跨服务调用都可能遇到超时、连接不足、重试放大、线程阻塞和错误定位问题。

brpc 这类框架把这些问题的通用部分收进了 `Channel` 和 `Server`：

- 连接怎么建、建几条、是否复用；
- 请求发不出去时是否重试；
- 多个实例之间如何选择；
- 单次调用如何设置截止时间；
- 服务端在哪个执行单元里跑业务代码；
- 出错时从哪里拿到错误文本与延迟数据。

但“框架自带”不等于“业务不用理解”。默认值往往决定了故障时的行为：连接池大小不合适，会把延迟推高；超时设置过长，会拖住上游；对非幂等请求盲目重试，会制造重复写入；在回调里做阻塞操作，会占住执行单元。框架降低的是重复建设成本，不是理解成本。

对准备进入公众号后台类业务的后端工程师来说，这种理解尤其重要：后台链路通常调用多、流量有峰谷、故障需要快速定位。你需要能说清楚“我这条调用用的是哪种连接模式、超时多少、失败后会不会重试、延迟从哪里看”，而不是只会复制一段初始化代码。

同时要守住一条边界：公开 brpc 的 `Channel`、`Controller`、`Socket` 等实现，可以帮助你理解通用 RPC 框架的设计；但它不等同于任何企业的内部 RPC 体系。工程上引用公开资料时，结论要限定在公开版本和公开行为内。

本课不要求你背下所有选项，而是要求你能围绕一条调用链做三件事：写出来、跑起来、讲清楚配置为什么这样选。示例中的端口、超时和重试次数都只是演示值，实际项目要结合下游容量、网络质量和业务语义重新确定。
## 核心概念

### 1. Channel、Controller 和 Service

#### 从直觉开始

把一次 RPC 想成寄快递：

- `Channel` 像你常走的那条物流线路。它知道目的地在哪、走哪种运输协议、有几条通道可用。你不会每寄一件包裹就重新修一条线路。
- `Controller` 像这一件包裹的运单。它记录这次寄送的超时、是否失败、失败原因、花了多久。每件包裹一张运单，不能把上一件的运单贴到下一件上。
- `Service` 像目的地仓库里的收件流程。它定义了“收到什么请求、由谁处理、返回什么响应”。仓库注册好流程后，包裹到达就按流程处理。

这个类比能帮你先记住生命周期：`Channel` 长、`Controller` 短、`Service` 跟服务进程走。下面把它落到公开 API 上。

#### Channel：客户端的长生命周期对象

`brpc::Channel` 是客户端侧的核心对象。它通过 `Init` 绑定目标地址或命名服务，并接收一组 `ChannelOptions`。初始化完成后，同一个 `Channel` 通常可以被多个线程共享，用来发起对同一目标服务的调用。

关键直觉：

- `Channel` 通常按“目标服务”创建，而不是按“单次请求”创建。
- 把 `Channel` 放在请求处理函数里反复构造，会反复做初始化、连接和策略准备，通常不是好做法。
- `Channel` 的线程安全性、可共享范围以及具体选项语义，以所用版本文档为准。

`ChannelOptions` 里常见的配置方向包括：

| 配置方向 | 作用直觉 | 注意点 |
| --- | --- | --- |
| 协议 | 决定客户端如何编码和解析请求 | 必须与服务端匹配 |
| 连接类型 | 决定连接复用方式 | 影响并发能力与连接数 |
| 超时 | 单次调用的时间上限 | 还可在 `Controller` 上覆盖 |
| 重试 | 失败后是否再次尝试 | 必须考虑幂等性 |
| 负载均衡 | 多实例时如何选择目标 | 与命名服务、集群视图有关 |

具体字段名、默认值和可用取值请以所用版本文档为准。不要把示例里的值当成框架默认值。

#### Controller：单次 RPC 的上下文

`brpc::Controller` 承载一次调用的上下文。客户端侧常见用途包括：

- `set_timeout_ms(...)`：为这次调用设置超时；
- `Failed()`：判断调用是否失败；
- `ErrorText()`：读取错误描述；
- `latency_us()`：读取调用延迟（微秒）；
- `request_attachment()` / `response_attachment()`：读写附件数据；
- `log_id`：用于日志串联的标识。

服务端侧也能拿到 `Controller`，从中读取远端地址、日志标识等信息。不同版本的字段和方法可能不同，使用前请核对所用版本文档。

最重要的规则是：`Controller` 不是线程安全的，也不应该跨 RPC 复用。一次调用一个实例，调用期间保持存活。

#### Service：服务端的接口实现

服务端先由 proto 定义 `service`，生成 C++ 基类；业务代码继承该基类并实现方法；最后把实现对象注册到 `brpc::Server`。

服务端方法的典型签名包含四个部分：

```cpp
void Method(google::protobuf::RpcController* cntl_base,
            const Request* request,
            Response* response,
            google::protobuf::Closure* done);
```

其中 `done` 是完成回调。同步实现时，业务代码填完 `response` 后需要让 `done->Run()` 被调用；`brpc::ClosureGuard` 用 RAII 的方式保证离开作用域时自动执行，避免忘记回调导致请求悬挂。

`AddService` 注册时通常会指定所有权语义。例如 `SERVER_DOESNT_OWN_SERVICE` 表示服务器不负责删除该实现对象，调用方要保证其生命周期覆盖服务器运行期。反过来，如果使用由服务器拥有的选项，则要注意对象分配方式。具体枚举与语义以所用版本文档为准。
#### 最小示例：proto、服务端与客户端

下面给出一个公开 brpc 风格的最小 Echo 示例。它的目的不是覆盖所有功能，而是让你把 `Service`、`Server`、`Channel`、`Controller` 和 `Stub` 串起来。构建方式、依赖库和生成命令请参考所用版本的官方示例与文档。

**第一步：定义 proto**

```proto
syntax = "proto3";

package demo;

message EchoRequest {
  string message = 1;
}

message EchoResponse {
  string message = 1;
}

service EchoService {
  rpc Echo(EchoRequest) returns (EchoResponse);
}
```

**第二步：实现服务端**

```cpp
#include <brpc/server.h>
#include <glog/logging.h>

#include "echo.pb.h"

class EchoServiceImpl : public demo::EchoService {
 public:
  void Echo(google::protobuf::RpcController* cntl_base,
            const demo::EchoRequest* request,
            demo::EchoResponse* response,
            google::protobuf::Closure* done) override {
    brpc::ClosureGuard done_guard(done);

    auto* cntl = static_cast<brpc::Controller*>(cntl_base);
    response->set_message(request->message());

    LOG(INFO) << "remote=" << cntl->remote_side()
              << " log_id=" << cntl->log_id();
  }
};

int main() {
  brpc::Server server;
  EchoServiceImpl impl;

  if (server.AddService(&impl, brpc::SERVER_DOESNT_OWN_SERVICE) != 0) {
    LOG(ERROR) << "AddService failed";
    return -1;
  }

  brpc::ServerOptions options;
  options.idle_timeout_sec = -1;

  if (server.Start(8000, &options) != 0) {
    LOG(ERROR) << "Start server failed";
    return -1;
  }

  server.RunUntilAskedToQuit();
  return 0;
}
```

**第三步：客户端同步调用**

```cpp
#include <brpc/channel.h>
#include <glog/logging.h>

#include "echo.pb.h"

int main() {
  brpc::Channel channel;
  brpc::ChannelOptions options;
  options.protocol = "baidu_std";
  options.connection_type = "single";  // baidu_std 的默认值，写出来只为看清配置
  options.timeout_ms = 1000;
  options.max_retry = 2;

  if (channel.Init("127.0.0.1:8000", &options) != 0) {
    LOG(ERROR) << "Channel init failed";
    return -1;
  }

  demo::EchoService_Stub stub(&channel);

  demo::EchoRequest request;
  request.set_message("hello brpc");

  demo::EchoResponse response;
  brpc::Controller cntl;
  cntl.set_timeout_ms(300);

  stub.Echo(&cntl, &request, &response, nullptr);

  if (cntl.Failed()) {
    LOG(ERROR) << "rpc failed: " << cntl.ErrorText();
    return -1;
  }

  LOG(INFO) << "response=" << response.message()
            << " latency_us=" << cntl.latency_us();
  return 0;
}
```
#### 从示例看配置落点

把示例拆开看，每个配置都能对应到一个具体问题：

- `options.protocol = "baidu_std"`：客户端按该协议编码，服务端必须能解析同一协议。协议名、默认协议以及服务端协议配置方式，以所用版本文档为准。
- `options.connection_type = "single"`：baidu_std 默认就是单连接多路复用，一条连接同时承载多个在途请求。它决定下游看到的连接数，下一节详细比较。
- `options.timeout_ms = 1000`：通道级超时。它表达“这次调用最多等多久”的默认上限。
- `cntl.set_timeout_ms(300)`：单次调用级超时。它通常用于对某个关键请求收紧或放宽时间预算；两者的优先级和覆盖关系以所用版本文档为准。
- `options.max_retry = 2`：允许框架在满足条件时重试。重试次数、触发条件、是否重新计时、是否受总截止时间约束，都以所用版本文档为准。
- `cntl.Failed()`：判断本次调用是否成功。业务代码不能假设“没有抛异常就成功”。
- `cntl.ErrorText()`：失败时提供错误文本。日志里应同时带上目标服务、方法、`log_id` 和错误文本，方便串联。
- `cntl.latency_us()`：读取调用延迟，单位是微秒。它是观察框架层耗时最直接的指标之一。
- `server.AddService(...)`：把实现注册到服务器。所有权语义要与实现对象的生命周期匹配。
- `server.Start(...)`：开始监听并处理请求。端口、协议和服务器选项要一起核对。
- `server.RunUntilAskedToQuit()`：让服务端持续运行，直到收到退出信号。生产环境还要考虑优雅退出。
- `brpc::ClosureGuard done_guard(done)`：保证 `done->Run()` 在离开作用域时执行。同步实现依赖它避免请求悬挂。

重试需要单独强调：只有当请求语义允许重复执行时，重试才可能是安全的。查询类请求通常比写入类请求更容易满足幂等性；但这仍然取决于具体业务，而不是由框架自动保证。对于扣减、发券、消息投递等有副作用的操作，必须由业务层设计幂等键或去重机制。

#### 生命周期与线程边界速记

| 对象 | 建议生命周期 | 并发注意 |
| --- | --- | --- |
| `Channel` | 进程级或服务级复用 | 通常可被多线程共享，具体以版本文档为准 |
| `ChannelOptions` | 初始化 `Channel` 时使用 | 初始化后修改是否生效，以版本文档为准 |
| `Controller` | 一次 RPC 一个 | 不是线程安全的，不要跨请求复用 |
| `Request` / `Response` | 一次 RPC 一组 | 同步调用期间保持有效 |
| `Service` 实现 | 覆盖 `Server` 运行期 | 可能被并发调用，共享状态要自行保护 |
| `done` | 每次请求一个 | 必须且只能触发一次完成语义 |

`done` 回调不是“业务收尾随便做点事”的地方。它可能运行在框架的执行单元中；如果在里面做无界阻塞、长时间等待或再次发起同步 RPC，就可能占住执行资源。需要异步处理时，先把请求数据复制到安全的位置，再把任务交给自己的队列或执行单元，最后确保 `done->Run()` 只被调用一次。`bthread` 的具体调度行为和阻塞语义，以所用版本文档与实现为准。

读到这里，你应该能回答三个问题：

1. 这段客户端代码里，哪个对象活了很久，哪个对象只活一次调用？
2. 如果调用失败，应该先看哪个对象的哪个状态？
3. 如果要把超时从 300ms 改成 1s，应该改 `ChannelOptions` 还是 `Controller`，为什么？

把这三个问题讲清楚，比记住一堆选项更有价值。先把 `Channel`、`Controller`、`Service` 这条最小调用链走通，下面再看连接类型、负载策略和更细的回调边界。

### 2. 连接类型和负载策略

第 1 节用最朴素的 `Channel::Init("127.0.0.1:8000", &options)` 连上了一台 Echo 服务器。本节回答
更贴近线上后台的问题：**一条 Channel 到底维护哪些连接、请求如何在服务器间分摊、超时、重试与
backup request 如何叠加**。本节所有细节以公开文档与你所用版本为准；文中"公众号消息回调后台"
只是教学场景，不代表任何企业内部体系。

先说动机：在消息回调这类后台服务里（教学场景），一次回调往往要串行或并发调用多个下游，
客户端连接模型直接决定了：

- 尾延迟：连接排队、握手开销都会计入每次调用；
- 容量上限：端口、文件描述符、内存随连接数变化；
- 故障表现：连接级故障的重试与对冲策略是否安全，取决于下游操作是否幂等。

#### 2.1 连接类型：single / pooled / short

`ChannelOptions::connection_type` 决定 Channel 与每台下游服务器之间的连接管理方式，
它是纯客户端行为：不需要改 proto。下面的语义来自 brpc 官方 Client Basics 文档（以所用版本为准）。

**single：单连接、多路复用**

- 一个进程内的所有 Channel 到同一台服务器**至多一条**连接；
- 一条连接上可以**同时承载多个在途请求**，回包顺序不必与发送顺序一致（靠请求 ID 对应）；
- 多个请求的写入可以合并，系统调用少，客户端 CPU 最低；
- baidu_std、hulu_pbrpc、sofa_pbrpc 等协议**默认就用 single**。

直觉：它不是"一次只能跑一个请求的窄管道"，而是一条可以并行跑很多辆车的多车道高速。

**pooled：连接池，一条连接同一时刻只跑一个请求**

- 每次 RPC 前从池里取一条空闲连接，完成后归还；池空就新建；
- 一条连接**同一时刻最多承载一个请求**，所以服务端看到的连接数约为 `qps × latency`（Little 定律）；
- 池子上限由 gflag `-max_connection_pool_size` 控制；它小于实际并发时，多出来的连接会频繁建立又关闭，表现得像短连接；
- http/1.1 和使用 nshead 的协议**默认用 pooled**，因为这类协议本身不支持在一条连接上多路复用。

**short：短连接**

- 每次 RPC 前建连、完成后关闭；
- 每次多付约 1.5 个 RTT 的建连开销，CPU 也最高，峰值 QPS 还受本地端口数量限制；
- 没有协议默认用它，只适合偶尔发起的调用，或排查"连接状态被污染"时做对照。

两个易混点：

1. 文档的原话是：brpc 默认会为协议挑选最合适的连接类型，**一般不需要改**。改之前先说清楚理由；
2. "single 并发上不去"是常见的误解，它恰恰是为高并发复用设计的；pooled 主要服务于不能多路复用的协议。

```cpp
// 连接类型在 ChannelOptions 中指定；也可以写成字符串 "single" / "pooled" / "short"
brpc::Channel channel;
brpc::ChannelOptions opt;
opt.protocol = "baidu_std";                          // 该协议默认 single
opt.connection_type = brpc::CONNECTION_TYPE_POOLED;  // 仅作对比实验
if (channel.Init("127.0.0.1:8000", &opt) != 0) {
  LOG(ERROR) << "Fail to init channel";
  return -1;
}
```

观察连接行为的命令（压测时反复执行）：

```bash
# 客户端到下游 8000 端口的连接条数：
# single 恒为 1 条；pooled 约等于并发量；short 不断新建并留下大量 TIME_WAIT
ss -tn state established '( dport = :8000 )' | tail -n +2 | wc -l
ss -tan state time-wait '( dport = :8000 )' | tail -n +2 | wc -l
```

#### 2.2 三种连接类型对比（整理自官方文档）

| 维度 | short | pooled | single |
| --- | --- | --- | --- |
| 是否长连接 | 否 | 是 | 是 |
| 服务端看到的连接数（来自一个客户端） | qps × latency | qps × latency | 1 |
| 峰值 QPS | 差，受端口数限制 | 中 | 高 |
| 延迟 | 1.5RTT（建连）+ 1RTT + 处理时间 | 1RTT + 处理时间 | 1RTT + 处理时间 |
| 客户端 CPU | 高，每次都要建连 | 中，每个请求一次 write | 低，写入可以合并 |
| 默认用于 | 无 | http/1.1、nshead 系协议 | baidu_std 等 |

选择思路：先用协议的默认值；协议不支持多路复用时用 pooled，并把 `-max_connection_pool_size` 配到能覆盖并发；
short 只留给低频调用和排查实验。

#### 2.3 命名服务与负载均衡

Channel 还有另一种初始化方式：不给单点地址，而是给**命名服务名 + 负载均衡名**两个字符串，
由 Channel 在每次请求时挑选一台服务器。这个组合让"服务发现"与"流量分流"解耦：

```cpp
brpc::Channel channel;
brpc::ChannelOptions opt;

// 静态列表：把若干 ip:port 直接写进名字串
// 分隔符与字段格式以所用版本文档为准
if (channel.Init("list://10.0.0.11:8000,10.0.0.12:8000", "rr", &opt) != 0) {
  LOG(ERROR) << "Fail to init channel";
  return -1;
}

// 文件命名服务：文件中每行一个地址，修改文件即可变更节点
// 是否支持空行、注释等细节以所用版本文档为准
if (channel.Init("file://conf/echo_servers.txt", "rr", &opt) != 0) {
  return -1;
}
```

```text
# conf/echo_servers.txt（教学示例）
10.0.0.11:8000
10.0.0.12:8000
```

实践定位（教学参考）：

- list:// 适合写死少量稳定地址，例如本地联调、双人验证环境；
- file:// 适合"配置系统定时改文件"的简单发布链路；
- 更完整的注册中心接入方式因版本与编译选项而异，以所用版本文档为准。

常用负载均衡名（语义描述以所用版本文档为准）：

| 名字 | 语义 | 典型用途 |
| --- | --- | --- |
| rr | 轮询 round-robin | 后端同质、容量相近时首选 |
| random | 随机挑选 | 打散请求，避免轮询的同步倾斜 |
| la | 优先发给历史平均时延小的节点 | 后端异构、局部抖动时自动避让 |
| c_murmurhash | 一致性哈希（哈希键规则以文档为准） | 同类请求落同一台，吃满缓存 |
| c_md5 | 一致性哈希（算法不同） | 同上 |

一致性哈希的代价：用均匀性换"同 key 同机"，节点上下线时请求会迁移，业务必须能容忍
短暂的路由漂移。教学场景：内容后台的素材查询类请求可用 c_murmurhash 提升对端缓存
命中率；写请求仍走 rr 或 la，避免写压力集中在单台。

#### 2.4 超时、重试与 backup request 的关系

三个旋钮独立起作用，但要放在一起理解：

| 旋钮 | 触发条件 | 语义 | 对下游幂等的要求 |
| --- | --- | --- | --- |
| timeout | 始终生效 | 整次 RPC 的硬上限 | 无 |
| max_retry | 连接断开且未超时 | 换一台服务器补发（ChannelOptions 默认 3 次，以文档为准） | 有 |
| backup_request_ms | 未超时但偏慢 | 并发第二份请求，谁先回用谁 | 有（更强） |

要点展开：

- **timeout**：`cntl->set_timeout_ms(200)`。重试与 backup request 都逃不出这个 deadline，
  所以"重试 3 次 x 每次超时"不会把一次调用拖成三倍时长；
- **max_retry**：`cntl->set_max_retry(2)`。文档说明重试需要同时满足：连接断开、
  还没到超时、还有重试额度；服务器没回包但连接正常时不会重试，那是 backup request 的职责；
  `ChannelOptions.max_retry` 默认 3，设 0 关闭（以所用版本文档为准）；非幂等写接口慎开；
- **backup_request_ms**：`cntl->set_backup_request_ms(50)`。它不是"失败后再试"，
  而是**并发的第二份请求**：50ms 内没等到首份响应，就向另一台再发一份相同请求，
  谁先回用谁的，未胜出的那份随后作废（细节以所用版本文档为准）。对端可能重复执行，
  必须幂等。

```cpp
brpc::Controller cntl;
cntl.set_timeout_ms(200);         // 整次 RPC 上限
cntl.set_max_retry(2);            // 连接级失败时最多补试 2 次
cntl.set_backup_request_ms(50);   // 50ms 未响应则对冲第二发（需幂等）

stub.Echo(&cntl, &req, &resp, nullptr);
LOG(INFO) << "latency_us=" << cntl.latency_us();
```

时间轴（教学示意）：

```text
t=0      发出请求 A
t=50ms   A 未响应，向另一台发出 backup 请求 B
t=70ms   A 先返回，采用 A 的结果
t=200ms  若 A、B 均未返回，整次 RPC 按超时失败
```

消息回调链路（教学示例）对长尾敏感：读路径统一设小超时并开 backup request，把 P99
拉回来；计数、落库这类写操作关掉自动重试与对冲，宁可失败也不重复写。

下一节进入执行模型：请求到底跑在哪个线程上，回调又在哪个线程发生。

### 3. bthread 及回调边界

第 2 节所有的"等待"都发生在线程模型之上。brpc 的线程模型核心是 bthread，
理解它，才能安全地写服务端处理函数与客户端回调。

#### 3.1 bthread 与 pthread 的 M:N 关系

- pthread 是内核线程，1:1 映射：一个调用阻塞，就有一个内核线程被占住；
- bthread 是用户态有栈协程，M 个 bthread 被调度到 N 个 pthread worker 上（M:N），
  worker 数由启动 flag 控制（flag 名与默认值以所用版本文档为准），通常接近 CPU 核数；
- 切换发生在用户态、发生在明确的调度点：等待 RPC 响应、等待 bthread 锁、
  bthread_usleep 等；用户态切换的开销远小于内核线程切换；
- 一个 worker 上同一时刻只运行一个 bthread，切换不依赖时钟中断，
  而是"运行到调度点才让出"。

```bash
# worker 线程数与核数对照（线程数量与命名以所用版本实现为准）
nproc
ps -T -p <pid> | wc -l
```

#### 3.2 同步调用阻塞的是 bthread，不是 worker

```cpp
// 最后一个参数 done 传 NULL，即同步调用
stub.Echo(&cntl, &req, &resp, nullptr);
```

语义要点：

- "卡在这里"的是当前 bthread：它被挂起在框架内部的同步原语上，
  调度器随即去运行同一 worker 上的其他 bthread；
- 底层 pthread worker 既不空转，也不为这个 RPC 阻塞；
- 因此在 brpc 服务的处理函数里同步调用多个下游是可接受的常态（仍受 3.4 约束）。

教学场景：回调服务收到一条用户消息后，依次同步调用"审核服务""配置服务"，
两次 RPC 各自只挂起当前 bthread，worker 继续处理其他消息，吞吐不受影响。

#### 3.3 异步调用：done 闭包与生命周期

异步 = 给 CallMethod 传一个非空的 done，调用立即返回：

```cpp
class EchoDone : public google::protobuf::Closure {
 public:
  void Run() override {
    std::unique_ptr<EchoDone> self_guard(this);  // 惯例：done 自删
    if (!cntl.Failed()) {
      LOG(INFO) << "resp=" << resp.message();
    } else {
      LOG(WARNING) << cntl.ErrorText();
    }
  }
  echo::EchoResponse resp;
  brpc::Controller cntl;
};

EchoDone* done = new EchoDone;
stub.Echo(&done->cntl, &req, &done->resp, done);  // 立即返回
```

生命周期铁律：

1. response 与 controller 必须活得比 RPC 久：异步返回后当前栈帧已退出，
   所以把它们内嵌在堆上的 done 里，随 done 一起管理；
2. done->Run() 由框架在 RPC 终止（成功、失败、超时）时**恰好调用一次**，
   之后 done 的释放责任回到你手里（在 Run 里自删是常见惯例）；
3. request 在 CallMethod 返回后即可复用或析构（序列化发生在调用点；
   以所用版本文档为准）；
4. Run 里不要做重活：回调在框架的工作线程上执行，重活应抛回业务线程池，
   或再发起后续 RPC 形成异步链；
5. 也可用 brpc::NewCallback 或 protobuf 的 NewCallback 生成 Closure
   （签名与参数规则以所用版本文档为准）。

半同步：发起后记录 call_id，随后在当前 bthread 上等待，方便做受控的 fan-out：

```cpp
brpc::CallId cid = cntl->call_id();  // 发起前记录
stub.Echo(cntl, &req, resp, done);
brpc::Join(cid);                     // 等待该次调用结束（以所用版本文档为准）
```

#### 3.4 不要在 bthread 中长期阻塞 worker

bthread 的廉价来自"只在调度点让出"。以下操作**不会让出**，会把承载它的
pthread worker 整个卡住，同 worker 上的所有 bthread 一起挨饿：

- pthread 级锁：pthread_mutex_lock 等；协程内应改用 bthread_mutex_t，
  等锁时让出 worker 给其他 bthread；
- 阻塞系统调用：同步磁盘 IO、阻塞式网络收发、::sleep 等；
- 长纯 CPU 计算：不会阻塞 worker，但会占满时间片，同 worker 的其他
  bthread 难以及时被调度。

框架对长时间阻塞的 syscall 可能打印告警（阈值与行为以所用版本文档为准）。

实践建议：

1. 真正的阻塞操作交给独立线程池，或改造为异步接口；
2. 协程内的互斥优先使用 bthread 系列原语；
3. 吞吐上不去时，第一怀疑对象是"worker 被卡"：查有没有人在 bthread 里
   睡了觉、读了盘、拿了内核锁。

#### 3.5 bthread_usleep 与 sleep

```cpp
::usleep(1000);        // 阻塞 pthread worker：同 worker 的其他 bthread 全部停摆
bthread_usleep(1000);  // 只挂起当前 bthread，worker 继续跑别的 bthread
```

对比实验（与第 2 节的连接类型实验同机进行）：

1. 在 Echo 服务处理函数里分别放入 ::usleep(10 * 1000) 与 bthread_usleep(10 * 1000)；
2. 同等并发压测，记录吞吐与 worker 线程的 CPU 占用；
3. 预期现象：阻塞版 worker 被卡死、并发上不去；bthread 版 worker 持续可调度。

#### 3.6 本节实验记录模板

| 项 | 示例 |
| --- | --- |
| brpc 版本与编译方式 | 以你本机为准 |
| 协议 / payload | baidu_std / 1KB |
| 连接类型 | single、pooled、short 各测一次 |
| 并发 / QPS | 以实测为准 |
| 客户端 CPU 占用 | 以实测为准 |

把 single、pooled、short 三种配置各填一行，连同版本、payload 与客户端 CPU
一起写入实验报告，就是本节实验的最终产出。

## 常见误区与设计权衡

在进入实验之前，先把两个容易被忽视的前提摆在最前面。

> 不能把公开brpc实现推断成微信内部RPC实现。

这句话不是免责声明，而是方法论。brpc 是 Apache 开源的 RPC 框架，我们能读到它的 Channel、Controller、连接池、bthread、负载均衡等实现；但“公开实现”只能证明“brpc 在某个版本里是这样做的”，不能证明“某个超大规模后台系统内部也这样做”。在方案评审或面试中，正确表述是“根据 brpc 某版本文档/源码，X 行为如此”，而不是“大厂肯定都这么干”。凡涉及默认值、内部调度、灰度策略，都应补一句：以所用版本文档为准。

> 框架自带治理能力可减少自研，但业务需理解其默认值。

brpc 把超时、重试、负载均衡、连接池、健康检查、backup request 等能力做进了框架，这确实省掉大量基础设施代码。代价是：默认值通常是面向“通用场景”的折中，不是你的业务 SLA。

以读多写少、突发热点明显的内容类后台为例（教学假设，不代表任何企业内部情况），下面这些治理项都值得逐项确认：

| 治理项 | 常见默认倾向 | 需要确认的点 |
| --- | --- | --- |
| 超时 | 框架级默认值可能偏宽松 | 单次 RPC 的 deadline 是否满足接口 SLA，是否会拖垮上游线程 |
| 重试 | 文档写明 ChannelOptions.max_retry 默认 3 | 非幂等写接口重试会不会造成重复扣减、重复发消息 |
| 连接 | 长连接/连接池策略有默认 | 并发模型与连接数是否匹配，是否需要 pooled |
| 负载均衡 | 默认策略面向通用 | 下游实例权重、同机房/跨机房路由是否符合预期 |
| 线程模型 | bthread + IO 线程 | 回调里做阻塞操作会抢占关键线程 |

结论：框架替你解决了“能做”，业务团队必须回答“该不该、值多少”。上线前把关键参数写进配置基线，并在 `/status`、`/vars` 上持续观察。具体字段名与默认值以所用版本文档为准。

### 权衡的三个维度

1. 吞吐 vs 尾延迟：连接池化和批量能抬高吞吐，但排队会放大 P99。
2. 通用 vs 专用：直接改框架默认值影响全局，优先在 Channel/方法级覆盖。
3. 自研 vs 复用：治理能力复用框架，业务语义与幂等兜底必须自研。

### 四个高频误区

**误区一：忽略默认超时与重试。**
brpc 允许在 Controller 上设置超时，但“没设置”不等于“没有超时”，框架与下游都可能有各自的默认值；具体默认值以所用版本文档为准。风险在于：上游线程被长时间挂住，重试再叠加重试，雪崩时把下游打得更死。写接口尤其危险——重试一个“发消息/扣额度”的请求，可能产生重复副作用。

实践建议：Controller 上显式设 deadline；非幂等接口关闭自动重试或配合幂等键；启用 backup request 前先评估重复执行的代价。公众号后台的推送、配额扣减、订单状态流转，都应先问一句“重复执行一次会怎样”。

**误区二：混淆连接类型。**
single、pooled、short 不是简单的“性能档位”，而是不同的连接生命周期与并发模型：

- single：进程到每台服务器一条连接，连接上多路复用多个在途请求，baidu_std 等协议默认用它，CPU 最低。
- pooled：一条连接同一时刻只跑一个请求，连接数约等于 qps × latency，http/1.1 等不支持多路复用的协议默认用它。
- short：每次请求建连，开销大，只用于极低频调用或排查。

最常见的误解是"single 只有一条连接，高并发肯定不行"，于是把 baidu_std 改成 pooled，结果连接数暴涨、客户端 CPU 升高。另一个坑是用 pooled 却没把 `-max_connection_pool_size` 配到覆盖并发，多出来的连接频繁建立又关闭，效果接近短连接。官方文档的建议是默认值一般不用改。具体可用取值与 flag 名以示例源码和 `--help` 为准。

**误区三：在 done 回调里做重活。**
异步调用的 done 回调运行在框架的 bthread/IO 相关线程上，在里面做磁盘 IO、同步 RPC、抢大锁、复杂 JSON 解析，会阻塞关键执行路径，表现为整体延迟抖动甚至超时。正确做法：回调里只做轻量状态转移（置标志、投递结果），重活交给业务线程池或有界队列，并做好背压与拒绝策略。

判断标准很简单：如果这段代码在压力下可能等待，就不要放在回调里。

**误区四：复用 Controller。**
Controller 承载一次调用的超时、重试、错误码、response 等状态，生命周期应绑定单次调用。跨请求复用会串状态：上一个请求的错误码、超时设置污染下一个请求，排查极其困难，且往往只在并发时暴露。反过来，Channel 通常设计为线程安全、可长期复用，不要每次调用都 new 一个 Channel，否则连接池和负载均衡都失去意义。Controller 与 Channel 的线程安全边界以所用版本文档为准。

上述四点的共同点：把“框架能做什么”误当成“我已经配好了”。上线前的配置基线、压测和监控，缺一不可。

## 动手实践：构建官方Echo示例，比较single、pooled等适用连接模式并记录配置

本节目标：从零构建 brpc 官方 C++ Echo 示例，实际切换不同连接模式，测出客户端 CPU 与吞吐差异，并形成可复现的配置记录。所有命令以 Linux + 官方文档为准，具体 flag 名以示例源码和 `--help` 输出为准。

### 步骤 1：克隆源码并记录版本

```bash
git clone https://github.com/apache/brpc
cd brpc
git describe --tags --always
git rev-parse HEAD
```

把 `git describe` 与 commit 一并记入实验报告。不同版本示例的参数可能不同，版本号是复现实验的前提。

### 步骤 2：按官方文档安装依赖

依赖清单以仓库内 `README.md` / `docs/` 的安装说明为准（常见有 gflags、protobuf、leveldb、gperftools 等）。Ubuntu 上通常可用 `apt-get`，也可用官方提供的 `config_brpc.sh` 探测环境。不要照抄网上过时博客；装完后根据编译输出确认 protobuf 等版本兼容。

### 步骤 3：构建 echo 示例

官方示例位于 `example/echo_c++`。典型流程：

```bash
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)" echo_server echo_client
```

若目标名不同，先 `ls` 一下示例目录，以实际产物为准。构建失败多半是 gflags/protobuf 路径或版本问题，回到官方文档核对。

### 步骤 4：启动服务端与客户端

```bash
./echo_server --port=8000
# 另开一个终端
./echo_client --server=127.0.0.1:8000
```

先确认 `echo_client` 能收到正确回显，再开始改参数；否则后面的对比没有意义。建议服务端与客户端分机或至少分别绑核，避免相互干扰。

### 步骤 5：确认并切换连接模式

```bash
./echo_client --help | grep -i connection
```

以 `--help` 实际输出为准，找到连接类型相关 flag（示例中可能是 `-connection_type`，取值如 single、pooled、short 等；名称与取值以示例源码为准）。然后用 gflags 显式指定：

```bash
./echo_client --server=127.0.0.1:8000 --connection_type=pooled
```

每切换一次，记录：连接模式、并发线程数、payload 大小。一次只改一个变量。

### 步骤 6：调整 payload 与并发参数

payload 大小通常在示例源码里定义（常量或 flag）。先 grep 源码确认：

```bash
grep -n -i "payload\|request_size\|attachment" example/echo_c++/*.cpp
```

然后修改并重新编译，或使用已有的 size flag。压测节奏建议：先固定线程数与连接模式，只变 payload；再固定 payload，只变连接模式。这样每次差异都能归因。

### 步骤 7：记录客户端 CPU

压测时用 `top` 找到 echo_client 进程，或用 `pidstat` 采样：

```bash
pidstat -p "$(pgrep -f echo_client | head -1)" 1 30
top -b -n 1 -p "$(pgrep -f echo_client | head -1)"
```

关注：用户态 CPU%、上下文切换次数、RSS。同样 QPS 下客户端 CPU 越低越好；若 CPU 不高但延迟高，多半在等连接或排队，而不是算力不足。

### 步骤 8：观察内置监控页

brpc 服务端通常提供内置页面（端口与开关以所用版本文档为准）：

```bash
curl http://127.0.0.1:8000/status
curl http://127.0.0.1:8000/vars
```

也可用浏览器打开同样地址，查看连接数、QPS、延迟分位、bthread 数量等。把关键指标截图或抄进报告，作为连接模式差异的旁证。

### 结果记录表模板

| 实验编号 | 版本(git describe) | 连接模式 | 客户端线程 | payload(B) | QPS | 平均延迟 | P99 | 客户端CPU% | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | vX.Y.Z-N-gHASH | single | 1 | 64 | | | | | |
| 2 | 同上 | pooled | 8 | 64 | | | | | |
| 3 | 同上 | pooled | 8 | 4096 | | | | | |
| 4 | 同上 | short | 8 | 64 | | | | | |

每组重复 3 次取中位数，并记录机器核数、是否同机部署、brpc 版本。数据缺失比数据难看更糟。

### 对照验收自检

- [ ] 能清楚解释每个连接配置：single 多路复用、pooled 一连接一请求、short 每次建连；能说出各自的连接数、延迟和 CPU 代价，而不只是背名字。
- [ ] 能说明连接池大小、空闲回收与客户端线程数的关系（以所用版本文档为准）。
- [ ] 报告写明了 brpc 版本（git describe 输出）、payload 大小、客户端 CPU 的采样方法与数值。
- [ ] 结论能落到后台场景：比如能解释为什么 baidu_std 下保持默认 single 通常就够，什么情况下才需要 pooled。
- [ ] 所有参数名都来自 `--help` 或示例源码，而不是凭记忆。

### 小结

先建立基线，再逐项改一个变量，最后用 `/status`、`/vars` 与 CPU 数据把结论钉死。把版本、payload、连接模式、客户端 CPU 四件事写清楚，这个实验就具备了可复现性；至于更细的默认值，始终以所用版本文档为准。

### 附：连接模式选择速查

| 场景 | 建议起点 | 主要代价 |
| --- | --- | --- |
| 协议支持多路复用（如 baidu_std） | single（默认） | 单连接异常时影响该服务器上所有在途请求 |
| 协议不支持多路复用（如 http/1.1） | pooled（默认） | 连接池上限需要覆盖并发 |
| 极低频、临时性任务 | short | 每次建连的握手与开销 |
| 长耗时流式/大 payload | 视示例与文档而定 | 连接占用与内存放大 |

上表只是起点，最终以压测数据为准；具体可选值与默认行为以所用版本文档为准。

### 附：配置记录片段

把每次实验的配置固化成文本，便于复现：

```yaml
experiment: echo-connection-compare
brpc_version: "<git describe 输出>"
commit: "<git rev-parse HEAD>"
build: Release
server:
  addr: 127.0.0.1:8000
client:
  connection_type: pooled
  threads: 8
  payload_bytes: 64
  timeout_ms: 500
measure:
  tool: pidstat
  interval_s: 1
  duration_s: 30
result:
  qps: null
  avg_latency_ms: null
  p99_latency_ms: null
  client_cpu_percent: null
notes: "客户端与服务端同机，结果仅用于横向对比"
```

### 附：实验排障清单

- 编译报 protobuf/gflags 找不到：检查安装路径与 `CMAKE_PREFIX_PATH`，回到官方文档核对版本。
- 客户端连不上：确认服务端监听地址、防火墙、端口是否被占用。
- `--help` 里找不到连接 flag：说明该版本示例未暴露，直接看示例源码，别硬猜。
- QPS 很低但 CPU 不高：多半卡在连接或排队，检查连接模式与线程数。
- 延迟抖动大：先看是否同机互相干扰，再看回调里有没有阻塞操作。
- `/status`、`/vars` 打不开：确认端口与内置服务开关，以所用版本文档为准。
- 数据无法复现：回看版本、payload、线程数、机器负载四要素是否一致。

### 附：延伸思考

- 把 baidu_std 从默认 single 改成 pooled 后，连接数、客户端 CPU 和 P99 各变了多少？
- 当 payload 从 64B 增到 4KB，瓶颈从框架开销转向哪里？
- 同样的实验放到读接口与写接口上，结论会不会不同？写接口还要额外考虑幂等与重试。
- 你能否只用一页纸，向同事解释每个连接配置的适用边界与代价？

## 延伸阅读：sourceStudy

### 追踪目标与边界
- 目标：只用**公开**的 Apache brpc 源码，把一次 Echo 调用从客户端发出、到服务端处理、再到收到回包执行回调的完整链路走读一遍，建立关于「连接、超时、调度」的心智模型。
- 边界：brpc 是 Apache 开源项目，本节一切结论仅基于公开仓库与其文档；**不得**把公开实现直接推断成任何企业（例如微信公众号后台）的内部 RPC 体系。企业内部的协议选型、服务治理与部署形态另有约束，不在本课讨论范围。
- 源码中的类名、函数签名与文件路径在不同版本间会变动，下面给出的搜索位置均为示例，**以所用版本文档为准**。

### 准备
```bash
git clone https://github.com/apache/brpc.git
cd brpc && git tag                 # 找到与你编译产物匹配的 release tag
git checkout <your-tag>
# 先把官方 Echo 示例（example 下的 c++ 示例，目录名以所用版本为准）编译跑起来，边跑边对照读源码
```

### 建议的阅读顺序（一次调用的六步）
1. **入口**：从客户端的 `Channel::CallMethod` 开始。stub 生成的代理代码最终会调到这里：`Controller` 上的超时、重试次数、log_id 等设置在这里被带上，请求进入框架发送路径。
   - 搜索建议：在源码中全词搜索 `CallMethod`，先读头文件中的注释，再看实现。
2. **Controller**：通读 Controller 携带的一次调用状态：超时、重试、backup request 等字段（具体字段名与默认值以所用版本文档为准），并明确「一次调用对应一个 Controller」的使用约定。
   - 搜索建议：搜索 `set_timeout_ms`、`set_max_retry`、`backup_request`，观察哪些阶段会读取这些值。
3. **选服务器**：沿调用栈进入负载均衡层，结合文档理解 rr、random、locality_aware 等策略如何从 NamingService 给出的候选列表中挑出本次目标（完整策略列表以其文档为准）。
   - 搜索建议：搜索 `LoadBalancer`、`SelectServer` 一类符号（命名按版本可能不同），留意策略与「失败节点隔离/降权」逻辑的位置。
4. **连接写出**：进入 `Socket` 相关代码，对照 single / pooled / short 三种连接类型的实现差异，理解几种代价的来源：single 在一条连接上多路复用、按请求 ID 对应回包，pooled 每个请求独占一条连接，short 每次都要承受建连开销。
   - 搜索建议：在头文件中按 `single|pooled|short` 组合正则搜索 `ConnectionType`；再搜索写出接口（`Write` 等）与写缓冲、`EPOLLOUT` 相关处理，理解「写不下去时调用挂在哪」。
5. **收包解析**：以 baidu_std 协议的解析器为例，走读「按读缓冲切包 → 反序列化 → 找回本次调用对应的 Controller」的过程，重点看请求 ID 与回包如何关联、未知/残缺包如何被拒收。
   - 搜索建议：搜索 `baidu_std`、`ParseRpcMessage` 类符号（名称按版本可能不同），以及 `on_message` 相关注册点。
6. **回调边界**：最后抵达「唤醒」处：同步调用挂起的执行流如何在回包到达后被唤醒、异步调用的 `done` 回调在哪个执行域里被运行，即本次调用实际上**在哪个线程、哪个 bthread 里结束**。
   - 搜索建议：搜索 `bthread_start_background`、`bthread_start_urgent`，以及响应处理入口处对同步/异步分支的分叉。

### 一次调用走读笔记（示例叙事，供你对照源码修订）
- 发送侧大约经过：`CallMethod` 装填状态 → 负载均衡选目标 → 依据连接类型取得或建立连接 → 序列化与打包 → 经 `Socket` 写出（写不下则入队，等待可写事件）。
- 等待侧：同步调用把当前执行流挂起（框架等待原语，具体机制以所用版本为准），worker 线程被让出去跑别的 bthread；异步调用则把“继续干活”的接力棒交给用户 `done`。
- 接收侧：epoll 线程收到字节 → 协议解析器切包并反序列化 → 按请求 ID 找到对应 Controller → 经压缩/计时统计后触发唤醒：同步路径回到挂起点继续执行，异步路径执行 `done->Run()`。
- 超时与重试的判定点分散在选择目标、建立/复用连接、写出、等待回包等多个阶段；走读时留意「什么时刻判定超时、重试是否重新选服务器、重试对幂等性的要求」。

### 常用的 rg 搜索命令（正则与路径请按你的仓库版本调整）
```bash
rg -n "void Channel::CallMethod" src/brpc/
rg -n "set_timeout_ms|set_max_retry|backup_request" src/brpc/controller.h src/brpc/controller.cpp | head -40
rg -n "single|pooled|short" src/brpc/ | rg -i "connection" | head -40
rg -n "class .*LoadBalancer|LoadBalancingPolicy" src/brpc/ | head -20
rg -n "Write|writable" src/brpc/socket.h | head -40
rg -n "bthread_start_background|bthread_start_urgent" src/brpc/ | head -20
rg -n "baidu_std|ParseRpcMessage" src/brpc/ | head -20
rg -n "bthread_" src/brpc/ | rg -v "test" | head -60
```

### 实验记录模板（对照本课验收）
- 环境：brpc 版本与 git tag、编译选项、机器内核/网路环境（单机回环还是真实网络）。
- 变量：连接类型 single / pooled / short，请求 payload（如 1 字节哨兵、100B、1KB、10KB），客户端并发与调用频次。
- 观测：客户端 CPU、p50/p99 延迟、连接数（`ss` 或进程内统计）、错误率；同一组参数只改一个变量，结论才可信。

### 与后台场景的结合
- 假设你在做消息处理这类后台链路（教学假设）：入口服务在高峰期要向下行实例集群高频发起调用。当目标实例有限而调用频次极高时，支持多路复用的协议用默认 single 就能以最少的连接承载高并发；协议不支持多路复用才需要 pooled，并评估连接数是否会涨到 qps × latency 的量级。
- 若链路中存在时间敏感的上游（如到期必须给用户侧返回的超时预算），把「整体 deadline 减去已耗时」设置为下游调用的超时上限是通用做法——这是通用工程结论，与具体企业内部实现无关。
- 这些推论只来自 brpc 公开文档与公开实现，与你将来实际接触的任何内部 RPC 框架之间的关系，需要入职后用同样的实验方法去校准，而不是外推。
## 自测题

### 1. Controller 的生命周期
**题**：你写了一个异步的 Echo 客户端调用，回调里要读取响应与错误码。下列哪种做法正确，为什么？
A. 每次调用前创建一个 Controller 与 Closure，在 `done` 回调末尾清理；
B. 在栈上创建 Controller，客户端函数返回后立即销毁；
C. 多个并发调用复用同一个全局 Controller。
**答案**：A 是常见且可靠的做法；若要复用 Controller，必须使用框架明确支持的复用方式，并保证上一次调用完全结束后再重置使用，具体语义以所用版本文档为准。
**解析**：异步调用在 `done` 执行前，框架随时可能读写 Controller 上的回包、错误码与计时信息，所以其生命周期必须覆盖「从发出到 done 运行完毕」这段窗口。B 是同步调用里可行的「即用即毁」，照搬到异步会造成悬空引用；C 会让多个并发调用互相踩踏状态，是隐蔽的并发 bug 来源。同步与异步调用对 Controller 生命周期的要求不同，是很容易踩的坑。

### 2. 连接类型怎么选
**题**：服务 A 以每秒数万次的高频调用下游 B，而 B 只有几个实例、单次请求耗时很短。single / pooled / short 三种连接类型你会怎么选？反过来，如果目标实例数以千计、对每个实例的调用都很稀疏呢？
**答案**：如果用 baidu_std 这类支持多路复用的协议，前一种情况保持默认 single 就好：每台下游一条连接，同时跑很多在途请求，写入合并后客户端 CPU 最低；换成 pooled 反而让连接数涨到约 qps × latency。协议不支持多路复用（如 http/1.1）时才用 pooled，并把 `-max_connection_pool_size` 配到覆盖并发。后一种情况（实例上千、每台调用稀疏），single 也只是每台一条长连接；short 只在调用真的很偶尔时才划算，因为每次要多付建连的 RTT 和 CPU。
**解析**：三种类型的本质差异是一条连接能同时承载几个请求：single 承载多个，pooled 只承载一个，short 用完就关。官方文档的建议是默认值一般不需要改。选型看实测：p99 延迟、句柄数、失败率；不要照搬任何环境下的结论，尤其当你所处的后台有特殊中间层时，同一配置表现可能完全不同。

### 3. bthread 中阻塞的后果
**题**：你在某段跑在 bthread 里的代码中（例如异步 `done` 回调、服务端请求处理函数）直接调用了一个会阻塞几十毫秒的函数。这对进程有什么影响？正确做法是什么？
**答案**：该 bthread 所在的 worker 线程会被硬撑住几十毫秒，期间排在它后面等待的其他任务全部顺延，吞吐与延迟长尾同时恶化，阻塞比例越高恶化越快，极端情况下 worker 队列堆积会出现雪崩。
**解析**：bthread 的收益来自 n:m 调度——大量执行流共用少量 worker 线程。同步 RPC 之所以「安全」，是因为框架用挂起机制让出 worker 线程（具体等待原语以所用版本文档为准）；而手写的阻塞调用不经过这套机制，只能占死线程。正确做法：优先使用框架可挂起的接口（如同步 RPC 自身的等待机制），重活放入独立的业务线程池；`done` 回调内更要保持轻量，具体约束以所用版本文档为准。

## 推荐资源

- [brpc通道与服务实现：参考文档 / 原始资料](https://brpc.apache.org/docs/client/basics/)
  - 来源：brpc.apache.org ｜ 类型：文档 ｜ 语言：英文 ｜ 难度：进阶 ｜ 免费
  - 阅读重点一：**Channel、Controller 和 Service** 三者的分工与协作——Channel 持有连接与协议配置、Controller 携带单次调用的状态、Service 承载业务方法，以及对象各自的生命周期约定。
  - 阅读重点二：**连接类型与负载策略**——single / pooled / short 的语义与适用场景、负载均衡策略如何从候选服务器中做选择、超时与重试参数的含义与默认值。
  - 建议先读文档再做 sourceStudy 的源码走读，把「文档语义」与「代码现实」对齐，遇到分歧时以文档与所用版本的实现为准。

## 本课小结

- **三个核心对象**：Channel 是客户端「一条 RPC 通道」，配置协议与连接策略；Controller 代表一次调用的全部状态（超时、重试、回包、错误码）；Service 承载服务端业务方法。三者生命周期不同步，混用或悬空引用是常见 bug 来源。
- **一次调用的骨架**：CallMethod → Controller 装填状态与超时 → 负载均衡选目标 → 按连接类型取得连接并写出 → 收包切包反序列化 → 唤醒同步等待或执行 `done`。把这条骨架背下来，读源码和排查线上问题都有抓手。
- **连接类型**：single / pooled / short 的本质差异是一条连接能同时承载几个请求；默认值通常就是协议的最佳选择，要改得有实测理由，用 p99 与句柄数实测说话，并如实记录版本、payload 与客户端 CPU。
- **bthread 边界**：同步 RPC 的等待由框架挂起、worker 线程可复用；手写阻塞调用会卡死 worker 线程、放大长尾。「交给可挂起的接口，还是独立线程池」应当成为写每个回调前的本能检查。
- **边界意识**：本节一切结论都来自 Apache brpc 公开仓库与文档；它是一份值得通读的高性能 C++ RPC 参考实现，但任何企业内部后台体系（包括你将来服务的平台）都不能由公开代码直接推断，需用同样的实验方法独立验证。
