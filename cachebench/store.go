package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Entry 为缓存库条目的内存表示（始终归一化为当前版本字段全集）。
type Entry struct {
	Version    int    `json:"version"`
	Key        string `json:"key"`
	Digest     string `json:"digest"`
	Size       int64  `json:"size"`
	Stage      string `json:"stage"`
	References []Ref  `json:"references"`
	Compressed bool   `json:"compressed"`
}

// FormatError 记录单条目的解码失败，不影响其余条目。
type FormatError struct {
	File    string `json:"file"`
	Version int    `json:"version"`
	Reason  string `json:"reason"`
}

func (e *FormatError) Error() string {
	return fmt.Sprintf("%s field=%s msg=条目版本 %d 不受支持: %s", CodeFormat, e.File, e.Version, e.Reason)
}

// Checkpoint 记录升级进度，供中断恢复。
type Checkpoint struct {
	StoreVersion int      `json:"storeVersion"`
	Upgraded     []string `json:"upgraded"`
	Complete     bool     `json:"complete"`
}

// Store 为本地目录形式的缓存库。
type Store struct {
	Dir          string
	Entries      map[string]*Entry
	FormatErrors []*FormatError
	UpgradedKeys []string // 本次打开时升级的条目（稳定序）
}

func entryFileName(key string) string { return key + ".entry.json" }

// OpenStore 打开缓存库：逐条解码、未知版本记 E_FORMAT 并跳过、低版本自动升级。
func OpenStore(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, bizErr(CodeIO, "", "创建缓存库目录失败: %v", err)
	}
	s := &Store{Dir: dir, Entries: map[string]*Entry{}}
	names, err := listEntryFiles(dir)
	if err != nil {
		return nil, err
	}
	for _, name := range names {
		e, ferr := decodeEntryFile(filepath.Join(dir, name))
		if ferr != nil {
			s.FormatErrors = append(s.FormatErrors, ferr)
			continue
		}
		s.Entries[e.Key] = e
	}
	if err := s.upgradeStale(); err != nil {
		return nil, err
	}
	return s, nil
}

func listEntryFiles(dir string) ([]string, error) {
	items, err := os.ReadDir(dir)
	if err != nil {
		return nil, bizErr(CodeIO, "", "读取缓存库目录失败: %v", err)
	}
	var names []string
	for _, it := range items {
		if it.IsDir() {
			continue
		}
		n := it.Name()
		if strings.HasSuffix(n, ".entry.json") {
			names = append(names, n)
		}
	}
	sort.Strings(names)
	return names, nil
}

// decodeEntryFile 逐版本解码；未知版本返回 E_FORMAT。
func decodeEntryFile(path string) (*Entry, *FormatError) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, &FormatError{File: filepath.Base(path), Reason: "读取失败: " + err.Error()}
	}
	var head struct {
		Version int `json:"version"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, &FormatError{File: filepath.Base(path), Reason: "JSON 解析失败"}
	}
	base := filepath.Base(path)
	switch head.Version {
	case 1:
		var v1 struct {
			Version int    `json:"version"`
			Key     string `json:"key"`
			Digest  string `json:"digest"`
		}
		if err := json.Unmarshal(data, &v1); err != nil {
			return nil, &FormatError{File: base, Version: 1, Reason: "v1 解码失败"}
		}
		return &Entry{Version: 1, Key: v1.Key, Digest: v1.Digest, References: []Ref{}}, nil
	case 2:
		var v2 struct {
			Version int    `json:"version"`
			Key     string `json:"key"`
			Digest  string `json:"digest"`
			Size    int64  `json:"size"`
			Stage   string `json:"stage"`
		}
		if err := json.Unmarshal(data, &v2); err != nil {
			return nil, &FormatError{File: base, Version: 2, Reason: "v2 解码失败"}
		}
		return &Entry{Version: 2, Key: v2.Key, Digest: v2.Digest, Size: v2.Size, Stage: v2.Stage, References: []Ref{}}, nil
	case 3:
		var e Entry
		if err := json.Unmarshal(data, &e); err != nil {
			return nil, &FormatError{File: base, Version: 3, Reason: "v3 解码失败"}
		}
		if e.References == nil {
			e.References = []Ref{}
		}
		return &e, nil
	default:
		return nil, &FormatError{File: base, Version: head.Version, Reason: "未知格式版本"}
	}
}

// upgradeStale 将低于当前版本的条目逐个升级重写，并维护检查点。
func (s *Store) upgradeStale() error {
	cp, err := s.loadCheckpoint()
	if err != nil {
		return err
	}
	done := map[string]bool{}
	if cp != nil {
		for _, k := range cp.Upgraded {
			done[k] = true
		}
	}
	var pending []string
	for _, e := range s.Entries {
		if e.Version < CurrentFormatVersion && !done[e.Key] {
			pending = append(pending, e.Key)
		}
	}
	sort.Strings(pending)
	if len(pending) == 0 {
		if cp != nil && !cp.Complete {
			cp.Complete = true
			return s.writeCheckpoint(cp)
		}
		return nil
	}
	if cp == nil {
		cp = &Checkpoint{StoreVersion: CurrentFormatVersion, Upgraded: []string{}}
	}
	cp.Complete = false
	for _, key := range pending {
		e := s.Entries[key]
		e.Version = CurrentFormatVersion
		if e.References == nil {
			e.References = []Ref{}
		}
		if err := s.WriteEntry(e); err != nil {
			return err
		}
		cp.Upgraded = append(cp.Upgraded, key)
		if err := s.writeCheckpoint(cp); err != nil {
			return err
		}
		s.UpgradedKeys = append(s.UpgradedKeys, key)
	}
	cp.Complete = true
	return s.writeCheckpoint(cp)
}

func (s *Store) checkpointPath() string { return filepath.Join(s.Dir, "checkpoint.json") }

func (s *Store) loadCheckpoint() (*Checkpoint, error) {
	data, err := os.ReadFile(s.checkpointPath())
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, bizErr(CodeIO, "", "读取检查点失败: %v", err)
	}
	var cp Checkpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, bizErr(CodeIO, "checkpoint.json", "检查点解析失败: %v", err)
	}
	return &cp, nil
}

func (s *Store) writeCheckpoint(cp *Checkpoint) error {
	return atomicWriteJSON(s.checkpointPath(), cp)
}

// WriteEntry 以当前格式版本原子落盘（临时文件 + 重命名，中断不留半成品）。
func (s *Store) WriteEntry(e *Entry) error {
	out := *e
	out.Version = CurrentFormatVersion
	if out.References == nil {
		out.References = []Ref{}
	}
	if err := atomicWriteJSON(filepath.Join(s.Dir, entryFileName(e.Key)), &out); err != nil {
		return err
	}
	stored := out
	s.Entries[e.Key] = &stored
	return nil
}

// RemoveEntry 删除条目。
func (s *Store) RemoveEntry(key string) {
	delete(s.Entries, key)
	_ = os.Remove(filepath.Join(s.Dir, entryFileName(key)))
}

// Reset 清空缓存库全部条目（整体替换用）。
func (s *Store) Reset() error {
	names, err := listEntryFiles(s.Dir)
	if err != nil {
		return err
	}
	for _, n := range names {
		if err := os.Remove(filepath.Join(s.Dir, n)); err != nil {
			return bizErr(CodeIO, "", "清空缓存库失败: %v", err)
		}
	}
	s.Entries = map[string]*Entry{}
	return nil
}

// SortedKeys 返回条目键的稳定序。
func (s *Store) SortedKeys() []string {
	keys := make([]string, 0, len(s.Entries))
	for k := range s.Entries {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// DigestSet 返回 key->digest 摘要集合，用于快照一致性比较。
func (s *Store) DigestSet() map[string]string {
	out := make(map[string]string, len(s.Entries))
	for k, e := range s.Entries {
		out[k] = e.Digest
	}
	return out
}

func atomicWriteJSON(path string, v interface{}) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return bizErr(CodeIO, "", "序列化失败: %v", err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return bizErr(CodeIO, "", "写入临时文件失败: %v", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return bizErr(CodeIO, "", "原子重命名失败: %v", err)
	}
	return nil
}
