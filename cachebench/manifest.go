package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// 受支持的制品格式版本集合。
var SupportedFormatVersions = map[int]bool{1: true, 2: true, 3: true}

// CurrentFormatVersion 为当前落盘格式版本，新写入始终使用该版本。
const CurrentFormatVersion = 3

var keyPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Ref 描述下游制品对上游制品的引用（摘要 + 版本）。
type Ref struct {
	Key     string `json:"key"`
	Version int    `json:"version"`
	Digest  string `json:"digest"`
}

// ArtifactDecl 为清单中的制品声明。
type ArtifactDecl struct {
	Key           string `json:"key"`
	Stage         string `json:"stage"`
	Path          string `json:"path"`
	FormatVersion int    `json:"formatVersion"`
	Digest        string `json:"digest"`
	References    []Ref  `json:"references,omitempty"`

	AbsPath string `json:"-"`
}

// StageDecl 为阶段声明，制品按阶段顺序构建。
type StageDecl struct {
	ID string `json:"id"`
}

// Manifest 为流水线清单。
type Manifest struct {
	ManifestVersion int            `json:"manifestVersion"`
	Stages          []StageDecl    `json:"stages"`
	Artifacts       []ArtifactDecl `json:"artifacts"`

	byKey map[string]*ArtifactDecl
	order []string // 按阶段顺序展开的制品键
}

// ManifestError 汇总全部校验错误，保证整体拒绝而非部分载入。
type ManifestError struct {
	Errs []*BizError
}

func (m *ManifestError) Error() string {
	parts := make([]string, len(m.Errs))
	for i, e := range m.Errs {
		parts[i] = e.Error()
	}
	return strings.Join(parts, "; ")
}

// OrderedKeys 返回按阶段声明顺序展开的制品键（稳定顺序）。
func (m *Manifest) OrderedKeys() []string {
	out := make([]string, len(m.order))
	copy(out, m.order)
	return out
}

// Artifact 按键查找制品声明。
func (m *Manifest) Artifact(key string) *ArtifactDecl {
	return m.byKey[key]
}

// LoadManifest 从本地 JSON 加载清单并做强校验；任一不合法整体拒绝。
func LoadManifest(path, workspace string) (*Manifest, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, bizErr(CodeManifest, "", "无法读取清单文件: %v", err)
	}
	var m Manifest
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		return nil, bizErr(CodeManifest, "", "JSON 解析失败: %v", err)
	}
	if err := m.validate(workspace); err != nil {
		return nil, err
	}
	return &m, nil
}

func (m *Manifest) validate(workspace string) error {
	var errs []*BizError
	fail := func(field, format string, args ...interface{}) {
		errs = append(errs, bizErr(CodeManifest, field, format, args...))
	}

	if m.ManifestVersion < 1 {
		fail("manifestVersion", "清单版本必须为正整数, 实际=%d", m.ManifestVersion)
	}

	stageSet := map[string]bool{}
	for i, s := range m.Stages {
		field := fmt.Sprintf("stages[%d].id", i)
		if s.ID == "" {
			fail(field, "阶段标识不能为空")
			continue
		}
		if !keyPattern.MatchString(s.ID) {
			fail(field, "阶段标识不合法: %q", s.ID)
		}
		if stageSet[s.ID] {
			fail(field, "阶段标识重复: %q", s.ID)
		}
		stageSet[s.ID] = true
	}
	if len(m.Stages) == 0 {
		fail("stages", "至少声明一个阶段")
	}

	wsAbs, err := filepath.Abs(workspace)
	if err != nil {
		fail("", "工作区路径无效: %v", err)
		return &ManifestError{Errs: errs}
	}

	m.byKey = map[string]*ArtifactDecl{}
	seen := map[string]bool{}
	for i := range m.Artifacts {
		a := &m.Artifacts[i]
		base := fmt.Sprintf("artifacts[%d]", i)
		if !keyPattern.MatchString(a.Key) {
			fail(base+".key", "制品键不合法: %q", a.Key)
		}
		if seen[a.Key] {
			fail(base+".key", "制品键重复: %q", a.Key)
		}
		seen[a.Key] = true
		if !stageSet[a.Stage] {
			fail(base+".stage", "引用了未声明的阶段: %q", a.Stage)
		}
		if !SupportedFormatVersions[a.FormatVersion] {
			fail(base+".formatVersion", "不支持的格式版本: %d", a.FormatVersion)
		}
		if !digestPattern.MatchString(a.Digest) {
			fail(base+".digest", "摘要格式非法, 期望 sha256:<64hex>, 实际=%q", a.Digest)
		}
		abs, aerr := filepath.Abs(filepath.Join(wsAbs, a.Path))
		if aerr != nil {
			fail(base+".path", "路径无效: %v", aerr)
		} else {
			rel, rerr := filepath.Rel(wsAbs, abs)
			if rerr != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
				fail(base+".path", "制品文件路径越出工作区: %q", a.Path)
			} else if st, serr := os.Stat(abs); serr != nil || st.IsDir() {
				fail(base+".path", "制品文件不存在: %q", a.Path)
			} else {
				a.AbsPath = abs
			}
		}
		m.byKey[a.Key] = a
	}

	// 引用校验：目标存在、版本可比较、摘要格式合法、无自引用、无重复。
	for i := range m.Artifacts {
		a := &m.Artifacts[i]
		refSeen := map[string]bool{}
		for j, r := range a.References {
			field := fmt.Sprintf("artifacts[%d].references[%d]", i, j)
			if r.Key == a.Key {
				fail(field+".key", "不允许自引用: %q", r.Key)
			}
			target, ok := m.byKey[r.Key]
			if !ok {
				fail(field+".key", "引用目标不存在: %q", r.Key)
				continue
			}
			if refSeen[r.Key] {
				fail(field+".key", "重复引用目标: %q", r.Key)
			}
			refSeen[r.Key] = true
			if r.Version < 1 || r.Version > target.FormatVersion {
				fail(field+".version", "引用版本不可比较: ref=%d 目标格式版本=%d", r.Version, target.FormatVersion)
			}
			if !digestPattern.MatchString(r.Digest) {
				fail(field+".digest", "引用摘要格式非法: %q", r.Digest)
			}
		}
	}

	// 循环依赖检测。
	if cyc := findCycle(m); cyc != nil {
		fail("artifacts", "存在循环引用: %s", strings.Join(cyc, " -> "))
	}

	if len(errs) > 0 {
		return &ManifestError{Errs: errs}
	}

	// 按阶段顺序展开制品键。
	byStage := map[string][]string{}
	for _, a := range m.Artifacts {
		byStage[a.Stage] = append(byStage[a.Stage], a.Key)
	}
	for _, s := range m.Stages {
		keys := byStage[s.ID]
		sort.Strings(keys)
		m.order = append(m.order, keys...)
	}
	return nil
}

func findCycle(m *Manifest) []string {
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := map[string]int{}
	var stack []string
	var cycle []string
	var visit func(key string) bool
	visit = func(key string) bool {
		color[key] = gray
		stack = append(stack, key)
		a := m.byKey[key]
		if a != nil {
			for _, r := range a.References {
				switch color[r.Key] {
				case gray:
					idx := 0
					for i, k := range stack {
						if k == r.Key {
							idx = i
						}
					}
					cycle = append(append([]string{}, stack[idx:]...), r.Key)
					return true
				case white:
					if visit(r.Key) {
						return true
					}
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[key] = black
		return false
	}
	for _, a := range m.Artifacts {
		if color[a.Key] == white {
			if visit(a.Key) {
				return cycle
			}
		}
	}
	return nil
}
