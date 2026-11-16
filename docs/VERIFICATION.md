# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

## 环境

- Host：Linux x86_64（WSL2）
- Go：`go1.23.12 linux/amd64`
- 项目：零第三方 Go 依赖
- Windows 目标：`GOOS=windows GOARCH=amd64`
- Linux cgo 竞态检测：便携 Zig 0.13.0 作为 C 编译器，并为 Go race runtime 外部链接补齐 `__popcountdi2`

## 测试与静态检查

执行：

```bash
go test -count=1 ./...
go vet ./...
gofmt -l .
git diff --check
```

结果：

```text
ok  localdns/internal/cache
ok  localdns/internal/dnswire
ok  localdns/internal/engine
ok  localdns/internal/server
ok  localdns/internal/upstream
ok  localdns/internal/zone
format/vet/tests: PASS
```

竞态检测执行：

```bash
CGO_ENABLED=1 CC='<zig>/zig cc' \
  go test -count=1 -race \
  -ldflags '-linkmode=external -extldflags /tmp/popcount.o' ./...
```

结果：

```text
ok  localdns/internal/cache
ok  localdns/internal/dnswire
ok  localdns/internal/engine
ok  localdns/internal/server
ok  localdns/internal/upstream
ok  localdns/internal/zone
```

Windows 交叉构建：

```bash
GOOS=windows GOARCH=amd64 go build -o /tmp/localdns-win.exe ./cmd/localdns
GOOS=windows GOARCH=amd64 go build -o /tmp/dnsupstream-win.exe ./cmd/dnsupstream
GOOS=windows GOARCH=amd64 go build -o /tmp/dnsdemo-win.exe ./cmd/dnsdemo
```

结果：三个 Windows amd64 可执行文件均生成成功。

## 可重复演示

执行两次：

```bash
./scripts/demo.sh > /tmp/demo-run-a.txt
./scripts/demo.sh > /tmp/demo-run-b.txt
```

两次输出中 `=== stable summary ===` 之后的内容经 `diff -u` 比较完全一致：

```text
=== stable summary ===
cold_start=PASS local_json=PASS wire_normalization=json+wire-sample forward_chain=PASS
ttl_cache=PASS before=10 after=7 cache_hit=true
conflict=PASS winner=authority local=192.0.2.44 upstream=198.51.100.99
concurrency=PASS requests=24 unique_versions=1 fills=1 flights=1
business_codes=E_WIRE,E_ZONE,E_CNAME,E_UPSTREAM,E_CHAIN
verdict=PASS
```

关键过程值：

- `same.demo.test A` 由 JSON 与压缩线格式样本归一为同一记录，来源 `json+wire-sample`，TTL 45。
- `www.demo.test A` 冷启动 TTL 10；逻辑时钟前进 3 秒后缓存命中 TTL 7。
- `api.external.test A` 经 TCP 上游得到 CNAME `edge.external.test` 和 A `203.0.113.10`。
- `portal.external.test A` 的上游 A `198.51.100.99` 与本地权威 `clash.demo.test A 192.0.2.44` 冲突；本地权威胜出，CNAME 保留，双方序列号分别为 1/2。
- 24 个 UDP/TCP 混合并发查询只产生缓存版本 5、一次缓存回填和一次引擎 flight，所有应答 TTL 90。
- 业务码守卫实际得到：`E_WIRE offset=12`、`E_CHAIN`、`E_CNAME`、`E_ZONE`、`E_UPSTREAM`。

## 交互脚本冒烟测试

执行：

```bash
printf 'udp same.demo.test A\ntcp alias.demo.test A\ntcp portal.external.test A\nstats\nquit\n' \
  | timeout 20 ./scripts/interactive.sh
```

结果摘要：

- UDP `same.demo.test A`：`source=authority`、`origins=json+wire-sample`、`version=1`、TTL 45。
- TCP `alias.demo.test A`：CNAME `alias.demo.test -> www.demo.test`，终端 A `192.0.2.10`。
- TCP `portal.external.test A`：冲突胜者 `authority`，本地值 `192.0.2.44`，上游值 `198.51.100.99`。
- `stats` 显示 3 个缓存条目、3 次 flight、3 个胜出记录。

## 说明

- UDP/TCP 监听端口使用 `127.0.0.1:0` 时，实际临时端口每次不同；稳定汇总不包含这些随机端口。
- `scripts/test.sh` 在系统没有 C 编译器时会明确提示并跳过 `-race`；本轮另用上述 Zig cgo 命令完成了完整竞态检测。

