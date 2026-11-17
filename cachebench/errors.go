package main

import "fmt"

// 业务码
const (
	CodeManifest = "E_MANIFEST"
	CodeFormat   = "E_FORMAT"
	CodeHit      = "E_HIT"
	CodeRef      = "E_REF"
	CodeReplay   = "E_REPLAY"
	CodeIO       = "E_IO"
	CodeUsage    = "E_USAGE"
	CodeState    = "E_STATE"
)

// BizError 携带业务码与字段位置的错误。
type BizError struct {
	Code   string
	Field  string
	Msg    string
	Detail map[string]string
}

func (e *BizError) Error() string {
	if e.Field != "" {
		return fmt.Sprintf("%s field=%s msg=%s", e.Code, e.Field, e.Msg)
	}
	return fmt.Sprintf("%s msg=%s", e.Code, e.Msg)
}

func bizErr(code, field, format string, args ...interface{}) *BizError {
	return &BizError{Code: code, Field: field, Msg: fmt.Sprintf(format, args...)}
}
