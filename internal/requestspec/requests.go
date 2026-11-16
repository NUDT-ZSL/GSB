package requestspec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"

	"localmesh/internal/biz"
	"localmesh/internal/model"
)

type File struct {
	Round    string          `json:"round,omitempty"`
	Requests []model.Request `json:"requests"`
}

func Load(path string) (*File, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeRule, "load_requests", "cannot read requests file", err), "file")
	}
	var f File
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&f); err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeRule, "parse_requests", err.Error(), err), "file")
	}
	if len(f.Requests) == 0 {
		return nil, biz.WithField(biz.New(biz.CodeRule, "validate_requests", "requests must not be empty"), "requests")
	}
	seen := make(map[string]bool)
	for i := range f.Requests {
		req := &f.Requests[i]
		base := fmt.Sprintf("requests[%d]", i)
		req.ID = strings.TrimSpace(req.ID)
		if req.ID == "" {
			return nil, biz.WithField(biz.New(biz.CodeRule, "validate_requests", "request id must be non-empty"), base+".id")
		}
		if seen[req.ID] {
			return nil, biz.WithField(biz.New(biz.CodeRule, "validate_requests", "duplicate request id"), base+".id")
		}
		seen[req.ID] = true
		if req.Method == "" {
			req.Method = "GET"
		}
		req.Method = strings.ToUpper(req.Method)
		if strings.ContainsAny(req.Method, " \t\r\n") {
			return nil, biz.WithField(biz.New(biz.CodeRule, "validate_requests", "invalid HTTP method"), base+".method")
		}
		if !strings.HasPrefix(req.Path, "/") {
			return nil, biz.WithField(biz.New(biz.CodeRule, "validate_requests", "request path must start with /"), base+".path")
		}
		u, err := url.ParseRequestURI(req.Path)
		if err != nil || u.IsAbs() || u.Host != "" {
			return nil, biz.WithField(biz.New(biz.CodeRule, "validate_requests", "request path must be a local request URI"), base+".path")
		}
	}
	return &f, nil
}
