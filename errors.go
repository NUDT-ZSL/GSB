package docquery

import (
	"errors"
	"fmt"
)

const (
	CodeOK     = "OK"
	CodeDoc    = "E_DOC"
	CodeQuery  = "E_QUERY"
	CodePlan   = "E_PLAN"
	CodeCache  = "E_CACHE"
	CodeReplay = "E_REPLAY"
)

// BusinessError is the stable error envelope returned by the public APIs.
// DocIndex is zero-based and FieldPath uses dot notation.
type BusinessError struct {
	Code         string `json:"code"`
	Message      string `json:"message"`
	Collection   string `json:"collection,omitempty"`
	DocIndex     *int   `json:"doc_index,omitempty"`
	FieldPath    string `json:"field_path,omitempty"`
	QueryID      string `json:"query_id,omitempty"`
	FirstDiffKey string `json:"first_diff_key,omitempty"`
	Expected     string `json:"expected,omitempty"`
	Actual       string `json:"actual,omitempty"`
}

func (e *BusinessError) Error() string {
	if e == nil {
		return ""
	}
	if e.Code == "" {
		return e.Message
	}
	return e.Code + ": " + e.Message
}

func newBusinessError(code, format string, args ...any) *BusinessError {
	return &BusinessError{Code: code, Message: fmt.Sprintf(format, args...)}
}

func docError(index int, path, format string, args ...any) *BusinessError {
	e := newBusinessError(CodeDoc, format, args...)
	e.DocIndex = &index
	e.FieldPath = path
	return e
}

func isBusinessCode(err error, code string) bool {
	var be *BusinessError
	return errors.As(err, &be) && be.Code == code
}

// AsBusinessError converts an ordinary error to a business error without
// losing an already structured error.
func AsBusinessError(code string, err error) *BusinessError {
	if err == nil {
		return nil
	}
	var be *BusinessError
	if errors.As(err, &be) {
		return be
	}
	return newBusinessError(code, "%s", err.Error())
}
