package domain

import (
	"errors"
	"fmt"
)

const (
	CodeSpec     = "E_SPEC"
	CodeDepend   = "E_DEPEND"
	CodeRef      = "E_REF"
	CodeCapacity = "E_CAPACITY"
	CodeLedger   = "E_LEDGER"
	CodeReplay   = "E_REPLAY"
	CodeStore    = "E_STORE"
)

type BusinessError struct {
	Code   string   `json:"code"`
	Item   string   `json:"item,omitempty"`
	Node   string   `json:"node,omitempty"`
	Reason string   `json:"reason"`
	Refs   []string `json:"references,omitempty"`
}

func (e *BusinessError) Error() string {
	if e.Item != "" {
		return fmt.Sprintf("%s %s: %s", e.Code, e.Item, e.Reason)
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Reason)
}

func BizError(code, item, reason string) *BusinessError {
	return &BusinessError{Code: code, Item: item, Reason: reason}
}

func BizErrorNode(code, item, node, reason string) *BusinessError {
	return &BusinessError{Code: code, Item: item, Node: node, Reason: reason}
}

func AsBusiness(err error) *BusinessError {
	var business *BusinessError
	if errors.As(err, &business) {
		return business
	}
	return nil
}
