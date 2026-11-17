package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// 搭建确定性测试工作区：两个上游 + 一个下游引用。
func setupWorkspace(t *testing.T) (string, *Session) {
	t.Helper()
	ws := t.TempDir()
	writeFile(t, filepath.Join(ws, "a.txt"), "alpha-v1\n")
	writeFile(t, filepath.Join(ws, "b.txt"), "beta-v1\n")
	writeFile(t, filepath.Join(ws, "c.txt"), "gamma-v1\n")
	dA, _, _ := DigestFile(filepath.Join(ws, "a.txt"))
	dB, _, _ := DigestFile(filepath.Join(ws, "b.txt"))
	dC, _, _ := DigestFile(filepath.Join(ws, "c.txt"))
	manifest := fmt.Sprintf(`{
  "manifestVersion": 1,
  "stages": [{"id":"fetch"},{"id":"compile"}],
  "artifacts": [
    {"key":"art-a","stage":"fetch","path":"a.txt","formatVersion":1,"digest":%q},
    {"key":"art-b","stage":"fetch","path":"b.txt","formatVersion":2,"digest":%q},
    {"key":"art-c","stage":"compile","path":"c.txt","formatVersion":3,"digest":%q,
     "references":[{"key":"art-a","version":1,"digest":%q},{"key":"art-b","version":2,"digest":%q}]}
  ]
}`, dA, dB, dC, dA, dB)
	mpath := filepath.Join(ws, "manifest.json")
	writeFile(t, mpath, manifest)
	sess := NewSession(ws)
	if err := sess.LoadManifest(mpath); err != nil {
		t.Fatalf("load manifest: %v", err)
	}
	if err := sess.OpenStore(filepath.Join(ws, "store")); err != nil {
		t.Fatalf("open store: %v", err)
	}
	return ws, sess
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestManifestValidationRejectsBadManifest(t *testing.T) {
	ws := t.TempDir()
	writeFile(t, filepath.Join(ws, "ok.txt"), "x\n")
	bad := `{
  "manifestVersion": 1,
  "stages": [{"id":"s"},{"id":"s"}],
  "artifacts": [
    {"key":"bad key","stage":"ghost","path":"ok.txt","formatVersion":9,"digest":"nope",
     "references":[{"key":"missing","version":1,"digest":"sha256:zz"}]},
    {"key":"esc","stage":"s","path":"../out.txt","formatVersion":1,
     "digest":"sha256:f92f2765e3435bbd67f8221855fefbab578eec11918c3ee09e84d2449b963b04"}
  ]
}`
	mpath := filepath.Join(ws, "bad.json")
	writeFile(t, mpath, bad)
	sess := NewSession(ws)
	err := sess.LoadManifest(mpath)
	if err == nil {
		t.Fatal("期望加载被拒绝")
	}
	me, ok := err.(*ManifestError)
	if !ok {
		t.Fatalf("期望 ManifestError, 实际 %T", err)
	}
	if len(me.Errs) < 5 {
		t.Fatalf("期望至少 5 个字段错误, 实际 %d: %v", len(me.Errs), me.Errs)
	}
	for _, e := range me.Errs {
		if e.Code != CodeManifest {
			t.Fatalf("业务码应为 E_MANIFEST: %v", e)
		}
		if e.Field == "" {
			t.Fatalf("错误缺少字段位置: %v", e)
		}
	}
	if sess.Manifest != nil {
		t.Fatal("加载失败后不得部分载入")
	}
}

func TestManifestRejectsCycle(t *testing.T) {
	ws := t.TempDir()
	writeFile(t, filepath.Join(ws, "x.txt"), "x\n")
	d, _, _ := DigestFile(filepath.Join(ws, "x.txt"))
	cyc := fmt.Sprintf(`{
  "manifestVersion": 1,
  "stages": [{"id":"s"}],
  "artifacts": [
    {"key":"p","stage":"s","path":"x.txt","formatVersion":1,"digest":%q,
     "references":[{"key":"q","version":1,"digest":%q}]},
    {"key":"q","stage":"s","path":"x.txt","formatVersion":1,"digest":%q,
     "references":[{"key":"p","version":1,"digest":%q}]}
  ]
}`, d, d, d, d)
	mpath := filepath.Join(ws, "cyc.json")
	writeFile(t, mpath, cyc)
	if err := NewSession(ws).LoadManifest(mpath); err == nil ||
		!strings.Contains(err.Error(), "循环引用") {
		t.Fatalf("期望循环引用被拒绝, 实际 %v", err)
	}
}

func TestStoreCodecMultiVersion(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "a.entry.json"),
		`{"version":1,"key":"a","digest":"sha256:aaa"}`)
	writeFile(t, filepath.Join(dir, "b.entry.json"),
		`{"version":2,"key":"b","digest":"sha256:bbb","size":7,"stage":"fetch"}`)
	writeFile(t, filepath.Join(dir, "c.entry.json"),
		`{"version":3,"key":"c","digest":"sha256:ccc","size":9,"stage":"compile","references":[],"compressed":true}`)
	writeFile(t, filepath.Join(dir, "d.entry.json"),
		`{"version":42,"key":"d","digest":"sha256:ddd"}`)
	st, err := OpenStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(st.FormatErrors) != 1 || st.FormatErrors[0].Version != 42 {
		t.Fatalf("未知版本应以 E_FORMAT 记录: %+v", st.FormatErrors)
	}
	// 低版本自动升级到当前版本
	for _, k := range []string{"a", "b", "c"} {
		e := st.Entries[k]
		if e == nil || e.Version != CurrentFormatVersion {
			t.Fatalf("条目 %s 应升级为 v%d: %+v", k, CurrentFormatVersion, e)
		}
	}
	if st.Entries["b"].Size != 7 || st.Entries["b"].Stage != "fetch" {
		t.Fatal("v2 字段升级后应保留")
	}
	if !st.Entries["c"].Compressed {
		t.Fatal("v3 压缩标记应保留")
	}
	// 落盘应为当前版本
	e, ferr := decodeEntryFile(filepath.Join(dir, "a.entry.json"))
	if ferr != nil || e.Version != CurrentFormatVersion {
		t.Fatalf("重写后应为 v%d", CurrentFormatVersion)
	}
	// 检查点完整
	cp, _ := st.loadCheckpoint()
	if cp == nil || !cp.Complete {
		t.Fatal("升级完成后检查点应标记 complete")
	}
}

func TestUpgradeCheckpointRecovery(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "x.entry.json"), `{"version":1,"key":"x","digest":"sha256:xxx"}`)
	writeFile(t, filepath.Join(dir, "y.entry.json"), `{"version":1,"key":"y","digest":"sha256:yyy"}`)
	// 模拟中断：x 已重写为当前版本且记入检查点，y 尚未升级
	st, err := OpenStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(st.UpgradedKeys) != 2 {
		t.Fatalf("首次打开应升级两个条目: %v", st.UpgradedKeys)
	}
	// 回滚 y 为 v1 并构造部分检查点，模拟崩溃现场
	writeFile(t, filepath.Join(dir, "y.entry.json"), `{"version":1,"key":"y","digest":"sha256:yyy"}`)
	writeFile(t, filepath.Join(dir, "checkpoint.json"),
		fmt.Sprintf(`{"storeVersion":%d,"upgraded":["x"],"complete":false}`, CurrentFormatVersion))
	// 遗留临时文件不得被误认为完整条目
	writeFile(t, filepath.Join(dir, "y.entry.json.tmp"), `{"version":3,"key":"y"}`)
	st2, err := OpenStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(st2.UpgradedKeys) != 1 || st2.UpgradedKeys[0] != "y" {
		t.Fatalf("恢复时只应依据检查点升级 y: %v", st2.UpgradedKeys)
	}
	if st2.Entries["y"].Version != CurrentFormatVersion {
		t.Fatal("y 应被升级为当前版本")
	}
}

func TestDigestDeterministicAcrossConcurrency(t *testing.T) {
	ws := t.TempDir()
	p := filepath.Join(ws, "data.bin")
	writeFile(t, p, strings.Repeat("deterministic\n", 500))
	want, _, err := DigestFile(p)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	results := make([]string, 64)
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			d, _, _ := DigestFile(p)
			results[i] = d
		}(i)
	}
	wg.Wait()
	for i, d := range results {
		if d != want {
			t.Fatalf("并发摘要不一致: [%d]=%s want=%s", i, d, want)
		}
	}
}

func TestBuildHitAndHitCode(t *testing.T) {
	_, sess := setupWorkspace(t)
	hits, missed, err := sess.Build()
	if err != nil || hits != 0 || len(missed) != 3 {
		t.Fatalf("冷缓存构建: hits=%d missed=%v err=%v", hits, missed, err)
	}
	hits, missed, err = sess.Build()
	if err != nil || hits != 3 || len(missed) != 0 {
		t.Fatalf("二次构建应全部命中: hits=%d missed=%v err=%v", hits, missed, err)
	}
	// 清单摘要与文件不一致 -> E_HIT 报告期望与实际结构
	ws := sess.Workspace
	writeFile(t, filepath.Join(ws, "a.txt"), "alpha-changed\n")
	_, _, err = sess.Hit("art-a")
	be, ok := err.(*BizError)
	if !ok || be.Code != CodeHit {
		t.Fatalf("期望 E_HIT, 实际 %v", err)
	}
	if be.Detail["expected"] == "" || be.Detail["actual"] == "" || be.Detail["expected"] == be.Detail["actual"] {
		t.Fatalf("E_HIT 应报告期望与实际摘要差异: %+v", be.Detail)
	}
}

func TestInvalidationPropagationAndRefGuard(t *testing.T) {
	_, sess := setupWorkspace(t)
	if _, _, err := sess.Build(); err != nil {
		t.Fatal(err)
	}
	downstream, err := sess.Invalidate("art-a")
	if err != nil {
		t.Fatal(err)
	}
	if len(downstream) != 1 || downstream[0] != "art-c" {
		t.Fatalf("失效集合应为 [art-c]: %v", downstream)
	}
	if sess.Store.Entries["art-c"] != nil {
		t.Fatal("下游条目应被移除")
	}
	if sess.Store.Entries["art-a"] == nil || sess.Store.Entries["art-b"] == nil {
		t.Fatal("非下游条目应保留")
	}
	// 失效集合与重新计算一致
	if got := sess.Graph.Downstream("art-a"); !equalStrings(got, downstream) {
		t.Fatalf("失效集合与重算不一致: %v vs %v", downstream, got)
	}
	// 删除仍被引用的制品 -> E_REF 列出引用者
	err = sess.Remove("art-b")
	be, ok := err.(*BizError)
	if !ok || be.Code != CodeRef || be.Detail["referrers"] != "art-c" {
		t.Fatalf("期望 E_REF referrers=art-c, 实际 %v", err)
	}
	if err := sess.Remove("art-c"); err != nil {
		t.Fatalf("删除无引用制品应成功: %v", err)
	}
}

func TestSnapshotRestoreReplay(t *testing.T) {
	ws, sess := setupWorkspace(t)
	if _, _, err := sess.Build(); err != nil {
		t.Fatal(err)
	}
	if _, err := sess.Invalidate("art-a"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := sess.Hit("art-a"); err != nil {
		t.Fatal(err)
	}
	snapPath := filepath.Join(ws, "snap.json")
	if _, err := sess.SaveSnapshot(snapPath); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(snapPath)
	if info.Mode().Perm()&0o222 != 0 {
		t.Fatal("快照应为只读")
	}
	// 无差异恢复
	res, err := sess.Restore(snapPath)
	if err != nil || res.Replaced {
		t.Fatalf("一致快照不应触发替换: %+v err=%v", res, err)
	}
	if checked, err := sess.Replay(); err != nil || checked != 2 {
		t.Fatalf("重放应通过: checked=%d err=%v", checked, err)
	}
	// 制造差异：新增一个条目 -> 整体替换并列出差异
	sess2 := NewSession(ws)
	if err := sess2.LoadManifest(filepath.Join(ws, "manifest.json")); err != nil {
		t.Fatal(err)
	}
	if err := sess2.OpenStore(filepath.Join(ws, "store2")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := sess2.Build(); err != nil {
		t.Fatal(err)
	}
	res, err = sess2.Restore(snapPath)
	if err != nil || !res.Replaced {
		t.Fatalf("差异快照应整体替换: %+v err=%v", res, err)
	}
	if len(res.DiffKeys) != 1 || res.DiffKeys[0] != "art-c" {
		t.Fatalf("差异条目应为 [art-c]: %v", res.DiffKeys)
	}
	if sess2.Store.Entries["art-c"] != nil {
		t.Fatal("替换后缓存库应与快照一致（art-c 已失效）")
	}
	if len(sess2.HitHistory) != 1 || len(sess2.InvalidationTrace) != 1 {
		t.Fatal("命中历史与失效轨迹应随快照整体替换")
	}
	// 内容变化 -> E_REPLAY 报告首个差异制品键
	writeFile(t, filepath.Join(ws, "a.txt"), "alpha-mutated\n")
	_, err = sess2.Replay()
	be, ok := err.(*BizError)
	if !ok || be.Code != CodeReplay || be.Field != "art-a" {
		t.Fatalf("期望 E_REPLAY field=art-a, 实际 %v", err)
	}
}

func TestRestoreRejectsManifestVersionDrift(t *testing.T) {
	ws, sess := setupWorkspace(t)
	if _, _, err := sess.Build(); err != nil {
		t.Fatal(err)
	}
	snapPath := filepath.Join(ws, "snap.json")
	if _, err := sess.SaveSnapshot(snapPath); err != nil {
		t.Fatal(err)
	}
	// 清单版本变化 -> 恢复整体替换且重放拒绝
	data, _ := os.ReadFile(filepath.Join(ws, "manifest.json"))
	writeFile(t, filepath.Join(ws, "manifest.json"),
		strings.Replace(string(data), `"manifestVersion": 1`, `"manifestVersion": 2`, 1))
	sess2 := NewSession(ws)
	if err := sess2.LoadManifest(filepath.Join(ws, "manifest.json")); err != nil {
		t.Fatal(err)
	}
	if err := sess2.OpenStore(filepath.Join(ws, "store")); err != nil {
		t.Fatal(err)
	}
	res, err := sess2.Restore(snapPath)
	if err != nil || !res.Replaced {
		t.Fatalf("清单版本漂移应整体替换: %+v err=%v", res, err)
	}
	if _, err := sess2.Replay(); err == nil {
		t.Fatal("清单版本漂移后重放应报 E_REPLAY")
	} else if be, ok := err.(*BizError); !ok || be.Code != CodeReplay {
		t.Fatalf("期望 E_REPLAY, 实际 %v", err)
	}
}
