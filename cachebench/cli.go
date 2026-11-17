package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
)

const helpText = `命令:
  load <manifest.json>   加载并强校验流水线清单
  open <dir>             打开缓存库目录（自动升级低版本条目）
  build                  按阶段顺序构建全部制品并登记缓存
  hit <key>              对单个制品做命中判定
  invalidate <key>       沿引用关系使下游缓存条目失效
  remove <key>           删除制品（仍被引用时拒绝, E_REF）
  upgrade                显式触发缓存库升级检查
  snapshot <file>        持久化会话快照（只读）
  restore <file>         打开快照并按版本一致性整体替换
  replay                 依据最近快照重放校验一致性
  status                 输出会话状态摘要
  serve <addr>           启动本地 HTTP 状态端点 /status
  help                   显示帮助
  quit                   退出
输出: OK ... 表示成功; ERR <业务码> field=<位置> msg=<说明> 表示失败`

// runCLI 以 stdin/stdout 命令循环驱动会话，输出稳定有序。
func runCLI(in io.Reader, out io.Writer, sess *Session) int {
	sc := bufio.NewScanner(in)
	sc.Buffer(make([]byte, 1024*1024), 1024*1024)
	exit := 0
	fmt.Fprintln(out, "cachebench ready; 输入 help 查看命令")
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if handleLine(out, sess, line) {
			break
		}
	}
	return exit
}

// handleLine 执行单行命令；返回 true 表示退出循环。
func handleLine(out io.Writer, sess *Session, line string) bool {
	fields := strings.Fields(line)
	cmd, args := fields[0], fields[1:]
	fail := func(err error) {
		if be, ok := err.(*BizError); ok {
			fmt.Fprintf(out, "ERR %s field=%s msg=%s", be.Code, be.Field, be.Msg)
			if len(be.Detail) > 0 {
				keys := make([]string, 0, len(be.Detail))
				for k := range be.Detail {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				for _, k := range keys {
					fmt.Fprintf(out, " %s=%s", k, be.Detail[k])
				}
			}
			fmt.Fprintln(out)
			return
		}
		if me, ok := err.(*ManifestError); ok {
			for _, e := range me.Errs {
				fmt.Fprintf(out, "ERR %s field=%s msg=%s\n", e.Code, e.Field, e.Msg)
			}
			return
		}
		fmt.Fprintf(out, "ERR %s msg=%v\n", CodeIO, err)
	}

	switch cmd {
	case "quit", "exit":
		fmt.Fprintln(out, "OK bye")
		return true
	case "help":
		fmt.Fprintln(out, helpText)
	case "load":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "load", "用法: load <manifest.json>"))
			break
		}
		if err := sess.LoadManifest(args[0]); err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK load manifestVersion=%d stages=%d artifacts=%d\n",
			sess.Manifest.ManifestVersion, len(sess.Manifest.Stages), len(sess.Manifest.Artifacts))
	case "open":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "open", "用法: open <dir>"))
			break
		}
		if err := sess.OpenStore(args[0]); err != nil {
			fail(err)
			break
		}
		st := sess.Store
		for _, fe := range st.FormatErrors {
			fmt.Fprintf(out, "ERR %s field=%s msg=条目版本 %d 不受支持: %s\n", CodeFormat, fe.File, fe.Version, fe.Reason)
		}
		fmt.Fprintf(out, "OK open entries=%d upgraded=%d formatErrors=%d\n",
			len(st.Entries), len(st.UpgradedKeys), len(st.FormatErrors))
		if len(st.UpgradedKeys) > 0 {
			fmt.Fprintf(out, "OK upgraded keys=%s\n", joinKeys(st.UpgradedKeys))
		}
	case "build":
		hits, missed, err := sess.Build()
		if err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK build hits=%d misses=%d missed=%s\n", hits, len(missed), joinKeys(missed))
	case "hit":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "hit", "用法: hit <key>"))
			break
		}
		hit, digest, err := sess.Hit(args[0])
		if err != nil {
			fail(err)
			break
		}
		result := "MISS"
		if hit {
			result = "HIT"
		}
		fmt.Fprintf(out, "OK hit key=%s result=%s digest=%s\n", args[0], result, digest)
	case "invalidate":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "invalidate", "用法: invalidate <key>"))
			break
		}
		downstream, err := sess.Invalidate(args[0])
		if err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK invalidate root=%s count=%d invalidated=%s\n", args[0], len(downstream), joinKeys(downstream))
	case "remove":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "remove", "用法: remove <key>"))
			break
		}
		if err := sess.Remove(args[0]); err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK remove key=%s\n", args[0])
	case "upgrade":
		if err := sess.requireStore(); err != nil {
			fail(err)
			break
		}
		if err := sess.Store.upgradeStale(); err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK upgrade current=%d upgraded=%d\n", CurrentFormatVersion, len(sess.Store.UpgradedKeys))
	case "snapshot":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "snapshot", "用法: snapshot <file>"))
			break
		}
		snap, err := sess.SaveSnapshot(args[0])
		if err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK snapshot entries=%d hits=%d traces=%d\n",
			len(snap.Entries), len(snap.HitHistory), len(snap.InvalidationTrace))
	case "restore":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "restore", "用法: restore <file>"))
			break
		}
		res, err := sess.Restore(args[0])
		if err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK restore replaced=%v diff=%s\n", res.Replaced, joinKeys(res.DiffKeys))
	case "replay":
		checked, err := sess.Replay()
		if err != nil {
			fail(err)
			break
		}
		fmt.Fprintf(out, "OK replay checked=%d\n", checked)
	case "status":
		printStatus(out, sess)
	case "serve":
		if len(args) != 1 {
			fail(bizErr(CodeUsage, "serve", "用法: serve <addr>"))
			break
		}
		if err := serveHTTP(out, sess, args[0]); err != nil {
			fail(err)
		}
	default:
		fmt.Fprintf(out, "ERR %s field=%s msg=未知命令\n", CodeUsage, cmd)
	}
	return false
}

func printStatus(out io.Writer, sess *Session) {
	mv, gv := 0, 0
	arts := 0
	if sess.Manifest != nil {
		mv = sess.Manifest.ManifestVersion
		arts = len(sess.Manifest.Artifacts)
	}
	if sess.Graph != nil {
		gv = sess.Graph.Version
	}
	entries, upgraded := 0, 0
	var keys []string
	if sess.Store != nil {
		entries = len(sess.Store.Entries)
		upgraded = len(sess.Store.UpgradedKeys)
		keys = sess.Store.SortedKeys()
	}
	fmt.Fprintf(out, "OK status manifestVersion=%d graphVersion=%d artifacts=%d entries=%d upgraded=%d hits=%d traces=%d\n",
		mv, gv, arts, entries, upgraded, len(sess.HitHistory), len(sess.InvalidationTrace))
	if len(keys) > 0 {
		fmt.Fprintf(out, "OK entries keys=%s\n", joinKeys(keys))
	}
}

// serveHTTP 提供只读状态端点（标准库 net/http）。
func serveHTTP(out io.Writer, sess *Session, addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		payload := map[string]interface{}{
			"hits":   sess.HitHistory,
			"traces": sess.InvalidationTrace,
		}
		if sess.Store != nil {
			payload["entries"] = sess.Store.SortedKeys()
		}
		if sess.Manifest != nil {
			payload["manifestVersion"] = sess.Manifest.ManifestVersion
		}
		_ = json.NewEncoder(w).Encode(payload)
	})
	fmt.Fprintf(out, "OK serve addr=%s\n", addr)
	return http.ListenAndServe(addr, mux)
}
