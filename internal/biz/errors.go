package biz

import (
	"errors"
	"fmt"
)

// Code is a stable application-level result code. DNS itself can only carry a
// limited RCODE space; these codes are additionally exposed through a local
// EDNS option and through structured server logs.
type Code string

const (
	CodeWire     Code = "E_WIRE"
	CodeZone     Code = "E_ZONE"
	CodeCNAME    Code = "E_CNAME"
	CodeUpstream Code = "E_UPSTREAM"
	CodeChain    Code = "E_CHAIN"
	CodeQuery    Code = "E_QUERY"
)

// Error carries a stable code and, for wire errors, the byte offset at which
// decoding failed.
type Error struct {
	Code      Code
	Offset    int
	HasOffset bool
	Detail    string
	Cause     error
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	msg := string(e.Code)
	if e.HasOffset {
		msg = fmt.Sprintf("%s offset=%d", msg, e.Offset)
	}
	if e.Detail != "" {
		msg += ": " + e.Detail
	}
	return msg
}

func (e *Error) Unwrap() error { return e.Cause }

func New(code Code, detail string) *Error {
	return &Error{Code: code, Detail: detail}
}

func At(code Code, offset int, detail string) *Error {
	return &Error{Code: code, Offset: offset, HasOffset: true, Detail: detail}
}

func Wrap(code Code, detail string, cause error) *Error {
	return &Error{Code: code, Detail: detail, Cause: cause}
}

func CodeOf(err error) Code {
	var appErr *Error
	if errors.As(err, &appErr) {
		return appErr.Code
	}
	return ""
}
