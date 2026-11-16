package protocol

import "fmt"

const (
	CodeOK       = "OK"
	CodeAppend   = "E_APPEND"
	CodeFence    = "E_FENCE"
	CodeCommit   = "E_COMMIT"
	CodePoison   = "E_POISON"
	CodeLedger   = "E_LEDGER"
	CodeVerify   = "E_VERIFY"
	CodeProtocol = "E_PROTOCOL"
	CodeInternal = "E_INTERNAL"
	CodeNotFound = "E_NOT_FOUND"
	CodeUnblock  = "E_UNBLOCK"
)

type BizError struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Fields  map[string]any `json:"fields,omitempty"`
}

func NewError(code, format string, args ...any) *BizError {
	return &BizError{Code: code, Message: fmt.Sprintf(format, args...), Fields: map[string]any{}}
}

func (e *BizError) Error() string {
	return e.Code + ": " + e.Message
}

func (e *BizError) With(field string, value any) *BizError {
	if e.Fields == nil {
		e.Fields = map[string]any{}
	}
	e.Fields[field] = value
	return e
}

func AsBizError(err error) *BizError {
	if err == nil {
		return nil
	}
	if biz, ok := err.(*BizError); ok {
		return biz
	}
	return NewError(CodeInternal, "%v", err)
}
