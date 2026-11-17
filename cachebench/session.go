package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
)

// HitRecord 记录一次命中判定结论。
type HitRecord struct {
	Key    string `json:"key"`
	Result string `json:"result"` // HIT / MISS
	Digest string `json:"digest"`
}

// TraceRecord 记录一次失效传播轨迹。
type TraceRecord struct {
	Root        string   `json:"root"`
	Invalidated []string `json:"invalidated"`
}

// Snapshot 为会话快照。
type Snapshot struct {
	SnapshotVersion   int           `json:"snapshotVersion"`
	ManifestVersion   int           `json:"manifestVersion"`
	GraphVersion      int           `json:"graphVersion"`
	Entries           []*Entry      `json:"entries"`
	InvalidationTrace []TraceRecord `json:"invalidationTrace"`
	HitHistory        []HitRecord   `json:"hitHistory"`
}

// Session 为一次工作台会话的全部状态。
type Session struct {
	Workspace         string
	Manifest          *Manifest
	ManifestPath      string
	Store             *Store
	Graph             *Graph
	InvalidationTrace []TraceRecord
	HitHistory        []HitRecord
	LastSnapshot      *Snapshot
}

func NewSession(workspace string) *Session {
	return &Session{Workspace: workspace}
}

// LoadManifest 加载并强校验清单，失败时保持会话原状（不部分载入）。
func (s *Session) LoadManifest(path string) error {
	m, err := LoadManifest(path, s.Workspace)
	if err != nil {
		return err
	}
	s.Manifest = m
	s.ManifestPath = path
	s.Graph = NewGraph(m)
	return nil
}

// OpenStore 打开缓存库（含自动升级）；已加载清单时富化升级条目缺失的字段。
func (s *Session) OpenStore(dir string) error {
	st, err := OpenStore(dir)
	if err != nil {
		return err
	}
	s.Store = st
	if s.Manifest != nil {
		for _, key := range st.UpgradedKeys {
			a := s.Manifest.Artifact(key)
			if a == nil {
				continue
			}
			e := st.Entries[key]
			e.Stage = a.Stage
			e.References = expectedRefs(a)
			if _, size, derr := s.computeDigest(a); derr == nil {
				e.Size = size
			}
			if err := st.WriteEntry(e); err != nil {
				return err
			}
		}
	}
	return nil
}

func (s *Session) requireManifest() error {
	if s.Manifest == nil {
		return bizErr(CodeState, "", "尚未加载清单，请先执行 load")
	}
	return nil
}

func (s *Session) requireStore() error {
	if s.Store == nil {
		return bizErr(CodeState, "", "尚未打开缓存库，请先执行 open")
	}
	return nil
}

// computeDigest 计算制品文件内容摘要（确定性规则）。
func (s *Session) computeDigest(a *ArtifactDecl) (string, int64, error) {
	d, n, err := DigestFile(a.AbsPath)
	if err != nil {
		return "", 0, bizErr(CodeIO, a.Key, "读取制品文件失败: %v", err)
	}
	return d, n, nil
}

// expectedRefs 返回清单声明的引用列表（稳定序）。
func expectedRefs(a *ArtifactDecl) []Ref {
	refs := append([]Ref{}, a.References...)
	sort.Slice(refs, func(i, j int) bool { return refs[i].Key < refs[j].Key })
	return refs
}

func refsEqual(a, b []Ref) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// evaluate 对单个制品做命中判定：键 + 内容摘要 + 引用列表共同决定。
// 返回 (是否命中, 实际摘要, 错误)。
func (s *Session) evaluate(key string) (bool, string, error) {
	a := s.Manifest.Artifact(key)
	if a == nil {
		return false, "", bizErr(CodeRef, key, "制品未在清单中声明")
	}
	digest, _, err := s.computeDigest(a)
	if err != nil {
		return false, "", err
	}
	if digest != a.Digest {
		return false, digest, &BizError{Code: CodeHit, Field: key,
			Msg:    "制品摘要与清单声明不一致",
			Detail: map[string]string{"expected": a.Digest, "actual": digest}}
	}
	entry := s.Store.Entries[key]
	if entry == nil {
		return false, digest, nil
	}
	if entry.Digest != digest {
		return false, digest, nil // 条目过期：不命中，由构建重写
	}
	if !refsEqual(entry.References, expectedRefs(a)) {
		return false, digest, nil // 引用列表变化：不命中，由构建重写
	}
	return true, digest, nil
}

// Build 按阶段顺序构建全部制品，返回 (命中数, 未命中键, 错误)。
func (s *Session) Build() (int, []string, error) {
	if err := s.requireManifest(); err != nil {
		return 0, nil, err
	}
	if err := s.requireStore(); err != nil {
		return 0, nil, err
	}
	hits := 0
	var missed []string
	for _, key := range s.Manifest.OrderedKeys() {
		a := s.Manifest.Artifact(key)
		hit, digest, err := s.evaluate(key)
		if err != nil {
			return hits, missed, err
		}
		if hit {
			hits++
			continue
		}
		missed = append(missed, key)
		_, size, err := s.computeDigest(a)
		if err != nil {
			return hits, missed, err
		}
		entry := &Entry{
			Key:        key,
			Digest:     digest,
			Size:       size,
			Stage:      a.Stage,
			References: expectedRefs(a),
			Compressed: false,
		}
		if err := s.Store.WriteEntry(entry); err != nil {
			return hits, missed, err
		}
	}
	return hits, missed, nil
}

// Hit 对单个制品做命中判定并记录历史。
func (s *Session) Hit(key string) (bool, string, error) {
	if err := s.requireManifest(); err != nil {
		return false, "", err
	}
	if err := s.requireStore(); err != nil {
		return false, "", err
	}
	hit, digest, err := s.evaluate(key)
	if err != nil {
		return false, digest, err
	}
	result := "MISS"
	if hit {
		result = "HIT"
	}
	s.HitHistory = append(s.HitHistory, HitRecord{Key: key, Result: result, Digest: digest})
	return hit, digest, nil
}

// Invalidate 沿引用关系使全部下游缓存条目失效并记录轨迹。
func (s *Session) Invalidate(root string) ([]string, error) {
	if err := s.requireManifest(); err != nil {
		return nil, err
	}
	if err := s.requireStore(); err != nil {
		return nil, err
	}
	if s.Manifest.Artifact(root) == nil {
		return nil, bizErr(CodeRef, root, "制品未在清单中声明")
	}
	downstream := s.Graph.Downstream(root)
	for _, k := range downstream {
		s.Store.RemoveEntry(k)
	}
	s.InvalidationTrace = append(s.InvalidationTrace, TraceRecord{Root: root, Invalidated: downstream})
	return downstream, nil
}

// Remove 删除制品声明与缓存条目；仍被引用时返回 E_REF。
func (s *Session) Remove(key string) error {
	if err := s.requireManifest(); err != nil {
		return err
	}
	if err := s.requireStore(); err != nil {
		return err
	}
	if s.Manifest.Artifact(key) == nil {
		return bizErr(CodeRef, key, "制品未在清单中声明")
	}
	if err := s.Graph.Remove(key); err != nil {
		return err
	}
	s.Store.RemoveEntry(key)
	return nil
}

// SaveSnapshot 持久化会话快照；快照文件置为只读。
func (s *Session) SaveSnapshot(path string) (*Snapshot, error) {
	if err := s.requireManifest(); err != nil {
		return nil, err
	}
	if err := s.requireStore(); err != nil {
		return nil, err
	}
	snap := &Snapshot{
		SnapshotVersion:   1,
		ManifestVersion:   s.Manifest.ManifestVersion,
		GraphVersion:      s.Graph.Version,
		InvalidationTrace: append([]TraceRecord{}, s.InvalidationTrace...),
		HitHistory:        append([]HitRecord{}, s.HitHistory...),
	}
	for _, k := range s.Store.SortedKeys() {
		snap.Entries = append(snap.Entries, s.Store.Entries[k])
	}
	_ = os.Remove(path) // 旧快照为只读，先移除以保证可重复演示
	if err := atomicWriteJSON(path, snap); err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0o444); err != nil {
		return nil, bizErr(CodeIO, path, "快照置只读失败: %v", err)
	}
	s.LastSnapshot = snap
	return snap, nil
}

// RestoreResult 描述恢复结果。
type RestoreResult struct {
	Replaced bool
	DiffKeys []string // 快照与当前不一致的条目键（稳定序）
}

// Restore 打开快照：清单版本与摘要集合一致则保持现状，否则整体替换。
func (s *Session) Restore(path string) (*RestoreResult, error) {
	if err := s.requireManifest(); err != nil {
		return nil, err
	}
	if err := s.requireStore(); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, bizErr(CodeIO, path, "读取快照失败: %v", err)
	}
	var snap Snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return nil, bizErr(CodeFormat, path, "快照解析失败: %v", err)
	}
	s.LastSnapshot = &snap

	res := &RestoreResult{}
	if snap.ManifestVersion != s.Manifest.ManifestVersion {
		res.Replaced = true
	}
	current := s.Store.DigestSet()
	snapSet := map[string]string{}
	for _, e := range snap.Entries {
		snapSet[e.Key] = e.Digest
	}
	diffSet := map[string]bool{}
	for k, d := range snapSet {
		if cd, ok := current[k]; !ok || cd != d {
			diffSet[k] = true
		}
	}
	for k := range current {
		if _, ok := snapSet[k]; !ok {
			diffSet[k] = true
		}
	}
	for k := range diffSet {
		res.DiffKeys = append(res.DiffKeys, k)
	}
	sort.Strings(res.DiffKeys)
	if len(res.DiffKeys) > 0 {
		res.Replaced = true
	}
	if !res.Replaced {
		return res, nil
	}
	// 整体替换：缓存库、引用图、命中历史、失效轨迹。
	if err := s.Store.Reset(); err != nil {
		return nil, err
	}
	for _, e := range snap.Entries {
		cp := *e
		if err := s.Store.WriteEntry(&cp); err != nil {
			return nil, err
		}
	}
	s.Graph = NewGraph(s.Manifest)
	s.Graph.Version = snap.GraphVersion
	s.HitHistory = append([]HitRecord{}, snap.HitHistory...)
	s.InvalidationTrace = append([]TraceRecord{}, snap.InvalidationTrace...)
	return res, nil
}

// Replay 依据快照记录重放命中结论与失效集合并逐项比较。
func (s *Session) Replay() (int, error) {
	if err := s.requireManifest(); err != nil {
		return 0, err
	}
	if err := s.requireStore(); err != nil {
		return 0, err
	}
	snap := s.LastSnapshot
	if snap == nil {
		return 0, bizErr(CodeState, "", "尚无快照，请先执行 snapshot 或 restore")
	}
	if snap.ManifestVersion != s.Manifest.ManifestVersion {
		return 0, &BizError{Code: CodeReplay, Field: "manifestVersion",
			Msg: fmt.Sprintf("清单版本不可复现: 快照=%d 当前=%d", snap.ManifestVersion, s.Manifest.ManifestVersion)}
	}
	checked := 0
	for _, rec := range snap.HitHistory {
		a := s.Manifest.Artifact(rec.Key)
		if a == nil {
			return checked, &BizError{Code: CodeReplay, Field: rec.Key,
				Msg: "快照记录的制品在当前清单中不存在"}
		}
		digest, _, err := s.computeDigest(a)
		if err != nil {
			return checked, err
		}
		if digest != rec.Digest {
			return checked, &BizError{Code: CodeReplay, Field: rec.Key,
				Msg:    "制品内容变化导致命中结论不可复现",
				Detail: map[string]string{"expected": rec.Digest, "actual": digest}}
		}
		entry := s.Store.Entries[rec.Key]
		var result string
		switch {
		case entry == nil:
			result = "MISS"
		case entry.Version != CurrentFormatVersion:
			return checked, &BizError{Code: CodeReplay, Field: rec.Key,
				Msg: fmt.Sprintf("格式版本不可复现: 期望=%d 实际=%d", CurrentFormatVersion, entry.Version)}
		case entry.Digest == digest && refsEqual(entry.References, expectedRefs(a)):
			result = "HIT"
		default:
			result = "MISS"
		}
		if result != rec.Result {
			return checked, &BizError{Code: CodeReplay, Field: rec.Key,
				Msg:    "命中结论与快照不一致",
				Detail: map[string]string{"expected": rec.Result, "actual": result}}
		}
		checked++
	}
	for _, tr := range snap.InvalidationTrace {
		got := s.Graph.Downstream(tr.Root)
		if !equalStrings(got, tr.Invalidated) {
			first := ""
			for _, k := range tr.Invalidated {
				found := false
				for _, g := range got {
					if g == k {
						found = true
					}
				}
				if !found {
					first = k
					break
				}
			}
			if first == "" && len(got) > 0 {
				first = got[0]
			}
			return checked, &BizError{Code: CodeReplay, Field: first,
				Msg: fmt.Sprintf("失效集合不可复现: root=%s", tr.Root)}
		}
		checked++
	}
	return checked, nil
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
