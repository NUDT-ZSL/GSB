package biz

import (
	"errors"
	"fmt"
	"sort"
)

const (
	CodeRule     = "E_RULE"
	CodeVerify   = "E_VERIFY"
	CodeUpstream = "E_UPSTREAM"
	CodeSnapshot = "E_SNAPSHOT"
	CodeReplay   = "E_REPLAY"
)

// Error is a business error with a stable code and machine-readable location.
type Error struct {
	Code    string
	Op      string
	Message string
	Field   string
	Fields  map[string]any
	Err     error
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	if e.Field != "" {
		return fmt.Sprintf("%s: %s (%s, field=%s)", e.Code, e.Message, e.Op, e.Field)
	}
	return fmt.Sprintf("%s: %s (%s)", e.Code, e.Message, e.Op)
}

func (e *Error) Unwrap() error { return e.Err }

func New(code, op, message string) *Error {
	return &Error{Code: code, Op: op, Message: message}
}

func Wrap(code, op, message string, err error) *Error {
	return &Error{Code: code, Op: op, Message: message, Err: err}
}

func WithField(err *Error, field string) *Error {
	if err != nil {
		err.Field = field
	}
	return err
}

func WithFields(err *Error, fields map[string]any) *Error {
	if err != nil {
		if err.Fields == nil {
			err.Fields = make(map[string]any)
		}
		for k, v := range fields {
			err.Fields[k] = v
		}
	}
	return err
}

func CodeOf(err error) string {
	var be *Error
	if errors.As(err, &be) {
		return be.Code
	}
	return ""
}

// ExitCode maps stable business codes to process exit statuses.
func ExitCode(err error) int {
	switch CodeOf(err) {
	case "":
		return 0
	case CodeRule:
		return 2
	case CodeVerify:
		return 3
	case CodeUpstream:
		return 4
	case CodeSnapshot:
		return 5
	case CodeReplay:
		return 6
	default:
		return 1
	}
}

// Fields converts an error to a structured log record.
func Fields(err error) map[string]any {
	out := map[string]any{}
	var be *Error
	if errors.As(err, &be) {
		out["code"] = be.Code
		out["op"] = be.Op
		out["message"] = be.Message
		if be.Field != "" {
			out["field"] = be.Field
		}
		for k, v := range be.Fields {
			out[k] = v
		}
	} else if err != nil {
		out["message"] = err.Error()
	}
	return out
}

// SortedKeys is useful for deterministic reports.
func SortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
