package protocolmeta

import (
	"encoding/json"
	"errors"
	"sort"

	"localdns/internal/biz"
	"localdns/internal/model"
)

type ConflictSide struct {
	Source    model.SourceKind `json:"source"`
	SourceSeq uint64           `json:"source_seq"`
	Origin    string           `json:"origin,omitempty"`
	Values    []string         `json:"values"`
}

type ConflictInfo struct {
	Name     string       `json:"name"`
	Type     string       `json:"type"`
	Winner   string       `json:"winner"`
	Local    ConflictSide `json:"local"`
	Upstream ConflictSide `json:"upstream"`
}

type ErrorInfo struct {
	Code      string `json:"code"`
	Offset    int    `json:"offset,omitempty"`
	HasOffset bool   `json:"has_offset,omitempty"`
	Detail    string `json:"detail,omitempty"`
}

type Metadata struct {
	Source     string        `json:"source,omitempty"`
	SourceSeq  uint64        `json:"source_seq,omitempty"`
	CacheHit   bool          `json:"cache_hit"`
	Coalesced  bool          `json:"coalesced,omitempty"`
	Version    uint64        `json:"version,omitempty"`
	Matches    []string      `json:"matches,omitempty"`
	Origins    []string      `json:"origins,omitempty"`
	CNAMEChain []string      `json:"cname_chain,omitempty"`
	Conflict   *ConflictInfo `json:"conflict,omitempty"`
	Error      *ErrorInfo    `json:"error,omitempty"`
}

func FromResolution(res model.Resolution, cacheHit, coalesced bool, version uint64) Metadata {
	meta := Metadata{
		Source: string(res.PrimarySource), SourceSeq: res.SourceSeq,
		CacheHit: cacheHit, Coalesced: coalesced, Version: version,
		CNAMEChain: append([]string(nil), res.CNAMEChain...),
	}
	matchSet := make(map[string]bool)
	originSet := make(map[string]bool)
	for _, ar := range append(append([]model.AnswerRecord(nil), res.Answers...), res.Authorities...) {
		matchSet[string(ar.Provenance.Match)] = true
		if ar.Provenance.Origin != "" {
			originSet[ar.Provenance.Origin] = true
		}
	}
	meta.Matches = sortedKeys(matchSet)
	meta.Origins = sortedKeys(originSet)
	if res.Conflict != nil {
		meta.Conflict = &ConflictInfo{
			Name: res.Conflict.Name, Type: res.Conflict.Type.String(), Winner: string(res.Conflict.Winner),
			Local: ConflictSide{
				Source: res.Conflict.Local.Source, SourceSeq: res.Conflict.Local.SourceSeq,
				Origin: res.Conflict.Local.Origin, Values: valuesOf(res.Conflict.Local.Records),
			},
			Upstream: ConflictSide{
				Source: res.Conflict.Upstream.Source, SourceSeq: res.Conflict.Upstream.SourceSeq,
				Origin: res.Conflict.Upstream.Origin, Values: valuesOf(res.Conflict.Upstream.Records),
			},
		}
	}
	return meta
}

func FromError(err error) Metadata {
	meta := Metadata{Error: &ErrorInfo{}}
	var appErr *biz.Error
	if errors.As(err, &appErr) {
		meta.Error.Code = string(appErr.Code)
		meta.Error.Offset = appErr.Offset
		meta.Error.HasOffset = appErr.HasOffset
		meta.Error.Detail = appErr.Detail
		return meta
	}
	meta.Error.Code = string(biz.CodeUpstream)
	meta.Error.Detail = err.Error()
	return meta
}

func Encode(meta Metadata) ([]byte, error) {
	return json.Marshal(meta)
}

func Decode(data []byte) (Metadata, error) {
	var meta Metadata
	err := json.Unmarshal(data, &meta)
	return meta, err
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for key := range set {
		out = append(out, key)
	}
	sort.Strings(out)
	return out
}

func valuesOf(records []model.Record) []string {
	values := make([]string, 0, len(records))
	for _, rec := range records {
		values = append(values, rec.Data)
	}
	sort.Strings(values)
	return values
}
