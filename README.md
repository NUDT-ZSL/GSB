# localdns：本地 DNS 解析与缓存服务

面向 Windows/本地回环环境的确定性 DNS 服务。项目同时提供回环 UDP/TCP 服务、交互式 CLI、可重复演示器、RFC 1035 线格式样本、JSON 权威区样本和本地 TCP 上游解析器子进程。运行期间不访问真实域名或线上 DNS 服务。

## 环境与构建

- Go 1.21 或更高版本，`go` 必须在 `PATH` 中。
- Windows PowerShell 7/5.1，或 Git Bash/WSL 的 Bash。
- 仅使用 `127.0.0.1` 回环地址，不需要 Docker。

Windows 可先安装 Go 并确认：

```powershell
go version
```

本项目不使用第三方 Go 依赖。构建命令：

```powershell
go build -o bin\localdns.exe ./cmd/localdns
go build -o bin\dnsupstream.exe ./cmd/dnsupstream
go build -o bin\dnsdemo.exe ./cmd/dnsdemo
```

## 可重复演示

Windows：

```powershell
.\scripts\demo.ps1
```

Git Bash / WSL：

```bash
./scripts/demo.sh
```

演示器会启动本地上游解析器子进程，并依次执行四个场景：

1. 冷启动：线格式样本重放、JSON 权威命中、TCP 上游 CNAME 链。
2. 固定逻辑时钟前进 3 秒后重查：`www.demo.test A` 从 TTL 10 降为 7。
3. 上游给出 `198.51.100.99`，与本地权威 `192.0.2.44` 冲突；本地权威胜出，并保留双方来源。
4. 24 个 UDP/TCP 混合并发查询同一键：唯一缓存版本、唯一回填、唯一胜出记录。

演示器最后额外验证 `E_WIRE`、`E_ZONE`、`E_CNAME`、`E_UPSTREAM`、`E_CHAIN`，并输出 `verdict=PASS`。UDP/TCP 临时端口每次可能不同，场景顺序与汇总字段固定。

## 交互式应用

Windows：

```powershell
.\scripts\interactive.ps1
```

Bash：

```bash
./scripts/interactive.sh
```

常用命令：

```text
udp same.demo.test A
tcp alias.demo.test A
tcp api.external.test A
tcp portal.external.test A
wire
wiretcp
stats
zone
help
quit
```

普通客户端只看标准 DNS 内容；交互 CLI 额外读取服务在 EDNS OPT 私有选项 `65002` 中返回的 JSON 元数据，因此可展示命中来源、缓存版本、TTL、匹配方式、CNAME 链、并发合并状态、冲突双方和业务码偏移。

## 样本

- `samples/zone.json`：JSON 简化权威区，包含 A、AAAA、CNAME、NS、SOA、通配符、CNAME 环和冲突目标。
- `samples/query.wire`：RFC 1035 查询报文。
- `samples/answer.wire`：带两字节 TCP 长度前缀的 RFC 1035 应答，应答所有者名称使用压缩指针。
- `same.demo.test A 192.0.2.77 TTL 45` 同时存在于 JSON 与线格式应答中；加载后归一为一条记录，来源为 `json+wire-sample`。

重新生成线格式样本：

```bash
./scripts/make_samples.sh
```

JSON 区格式：

```json
{
  "apex": "demo.test",
  "records": [
    {
      "name": "www.demo.test",
      "type": "A",
      "ttl": 10,
      "rdata": "192.0.2.10"
    }
  ]
}
```

加载时要求：名称合法、类型属于 A/AAAA/CNAME/NS/SOA、TTL 为非负整数且不超过 `-max-ttl`、RDATA 可规范化、每条记录属于区 apex、恰好一个 apex SOA、同一 RRset TTL 一致。任一记录非法时整个 JSON 区拒绝加载，返回 `E_ZONE`。

## 主要行为

### 线格式与传输

- 完整读取头部计数字段、问题段和三个 RR 段。
- 支持压缩指针展开；拒绝指针环/前向指针、越界目标、非法标签类型、截断 RDATA。
- 所有线格式错误返回 `E_WIRE`，并记录出错字节偏移；错误同时写入结构化日志和本地 EDNS 元数据。
- UDP 响应超过 512 字节时设置 `TC`，降级为保留问题段和空 RR 段的合法响应。
- TCP 使用两字节长度前缀分帧，同一连接可连续处理多个独立请求；UDP/TCP 各自使用独立缓冲区与报文状态。

### 名称、通配符与 CNAME

- 名称按 ASCII 大小写折叠；缓存键为 `name/type/class`。
- 通配符只能是完整最左标签；存在精确所有者节点时绝不回落到通配符。
- CNAME 链默认最多 16 跳；成环或超限返回 `E_CNAME`。
- 本地 CNAME 指向区外目标时会继续通过本地 TCP 上游解析目标，并拼成同一条应答链。

### 缓存与并发

- 正缓存 TTL 取答案/权威段最小 TTL；负缓存 TTL 取 `min(SOA TTL, SOA MINIMUM, 配置上限)`。
- 命中时按逻辑时钟经过的整秒递减，永不为负；过期条目在读取或 `Sweep` 时回收。
- 容量满时依次按最近访问序号、插入序号、规范化键名选择确定性受害者，不依赖 map 遍历顺序。
- 缓存条目在写入后不可变；读取返回深拷贝快照。同键并发回填由 singleflight 收敛为一次解析、一次写入和唯一版本。
- 条目整体替换，不合并来源序列号或时间基准不同的新旧字段。每次成功写入递增全局缓存版本。

### 上游校验与冲突仲裁

- 本地区未覆盖的名称只转发到明确的回环 TCP 上游；非回环地址在启动时拒绝。
- 校验事务标识、QR 位、问题回显、RCODE、应答所有者/类型链和 CNAME 跳数。
- 上游不可达、写入/读取失败为 `E_UPSTREAM`；事务、回显、链、压缩或报文内容不一致为 `E_CHAIN`。
- 可疑上游应答绝不写入缓存。
- 冲突规则：**同一 owner/type 上，本地精确权威 RRset 优先于上游 RRset**。本地 RRset 整体替换上游 RRset，并同时记录本地与上游来源、来源序列号和值；不会进行逐字段合并。

### 业务码

| 代码 | 含义 | DNS RCODE（错误应答） |
| --- | --- | --- |
| `E_WIRE` | 线格式非法，含偏移 | `FORMERR` |
| `E_ZONE` | JSON 区整体校验失败 | 本地加载阶段，不发 DNS 应答 |
| `E_CNAME` | CNAME 成环或超过最大跳数 | `SERVFAIL` |
| `E_UPSTREAM` | 回环上游不可达或 I/O 失败 | `SERVFAIL` |
| `E_CHAIN` | 上游应答事务/回显/链/线格式不一致 | `SERVFAIL` |
| `E_QUERY` | 非标准查询或不支持的查询形态 | `NOTIMP` |

## 测试

```bash
./scripts/test.sh
```

或分别执行：

```bash
go test ./...
go test -race ./...
go vet ./...
```

`-race` 需要可用的 C 编译器（`CGO_ENABLED=1` 与 `CC`）；普通测试不依赖 cgo。本轮实际验证环境、命令和结果见 `docs/VERIFICATION.md`。

测试覆盖线格式压缩攻击、全类型编解码、TCP 分帧、UDP 截断、JSON 原子拒绝、双通道归一、通配符精确优先、CNAME 环/跳数、TTL 递减与回收、确定性 LRU、整条替换、上游压缩展开/坏链/不可达、冲突仲裁、错误不缓存，以及真实 UDP/TCP 混合并发一致视图。

## 目录

```text
cmd/localdns      交互 CLI 与 UDP/TCP 服务入口
cmd/dnsupstream   确定性回环 TCP 上游子进程
cmd/dnsdemo       四场景演示与业务码守卫
cmd/mkfixtures    线格式样本生成器
internal/dnswire  DNS 线格式编解码、压缩防护、TCP 帧
internal/zone     JSON 区、线格式样本归一、名称匹配与 CNAME
internal/cache    TTL 缓存、确定性淘汰、不可变快照
internal/engine   解析编排、单飞回填、来源归因、冲突仲裁
internal/server   回环 UDP/TCP 服务与 EDNS 元数据
internal/upstream 本地 TCP 转发与应答链校验
samples           确定性 JSON/线格式样本
scripts           Windows PowerShell 与 Bash 入口
```

更详细的并发与状态不变量见 `docs/DESIGN.md`，实际验证记录见 `docs/VERIFICATION.md`。
