package protocol

import "encoding/json"

const (
	WireProtocol = "message-interop/1"

	DefaultPartition = "_default"

	CodeFormat   = "E_FORMAT"
	CodeVersion  = "E_VERSION"
	CodeSemantic = "E_SEMANTIC"
	CodeVerify   = "E_VERIFY"
	CodeLimit    = "E_LIMIT"
	CodeInternal = "E_INTERNAL"
	CodeNotFound = "E_NOT_FOUND"
	CodeConflict = "E_CONFLICT"
	CodeProtocol = "E_PROTOCOL"
)

type Semantics string

const (
	AtMostOnce  Semantics = "at_most_once"
	AtLeastOnce Semantics = "at_least_once"
	ExactlyOnce Semantics = "exactly_once"
)

func (s Semantics) Valid() bool {
	switch s {
	case AtMostOnce, AtLeastOnce, ExactlyOnce:
		return true
	default:
		return false
	}
}

type APIError struct {
	Code       string         `json:"code"`
	Message    string         `json:"message"`
	DeliveryID string         `json:"delivery_id,omitempty"`
	Details    map[string]any `json:"details,omitempty"`
}

func (e *APIError) Error() string {
	return e.Code + ": " + e.Message
}

func NewError(code, message string) *APIError {
	return &APIError{Code: code, Message: message}
}

// Message is the validated internal representation of a producer payload.
// Presence of version-specific fields is defined by Version, not zero values.
type Message struct {
	Version        int             `json:"version"`
	DeliveryID     string          `json:"delivery_id"`
	Body           json.RawMessage `json:"body"`
	TimestampMS    int64           `json:"timestamp_ms"`
	RetryCount     int             `json:"retry_count"`
	PartitionKey   string          `json:"partition_key"`
	IdempotencySeq int64           `json:"idempotency_seq"`
}

type StoredMessage struct {
	GlobalOffset   int64           `json:"global_offset"`
	Version        int             `json:"version"`
	DeliveryID     string          `json:"delivery_id"`
	Body           json.RawMessage `json:"body"`
	TimestampMS    int64           `json:"timestamp_ms"`
	RetryCount     int             `json:"retry_count"`
	PartitionKey   string          `json:"partition_key"`
	IdempotencySeq int64           `json:"idempotency_seq"`
	Semantics      Semantics       `json:"semantics"`
	AcceptedAt     string          `json:"accepted_at"`
}

type SubscribeRequest struct {
	ConsumerID    string    `json:"consumer_id"`
	Version       int       `json:"version"`
	PartitionKey  string    `json:"partition_key,omitempty"`
	SemanticsHint Semantics `json:"semantics,omitempty"`
}

type AckRequest struct {
	ConsumerID string `json:"consumer_id"`
	DeliveryID string `json:"delivery_id"`
	Offset     int64  `json:"offset"`
}

type Conversion struct {
	ConversionID     string            `json:"conversion_id"`
	SourceDeliveryID string            `json:"source_delivery_id"`
	DeliveryID       string            `json:"delivery_id"`
	SourceVersion    int               `json:"source_version"`
	DeclaredVersion  int               `json:"declared_version"`
	Direction        string            `json:"direction"`
	SourceFields     []string          `json:"source_fields"`
	TargetFields     []string          `json:"target_fields"`
	StrippedFields   []string          `json:"stripped_fields"`
	FilledFields     []string          `json:"filled_fields"`
	MutatedFields    []string          `json:"mutated_fields"`
	FieldOrigins     map[string]string `json:"field_origins"`
}

type SemanticDecision struct {
	Mode           Semantics `json:"mode"`
	Decision       string    `json:"decision"`
	Reason         string    `json:"reason"`
	RetryCount     int       `json:"retry_count"`
	IdempotencySeq int64     `json:"idempotency_seq,omitempty"`
	DedupKey       string    `json:"dedup_key,omitempty"`
}

type DeliveryRecord struct {
	EventID          int64            `json:"event_id,omitempty"`
	DeliveryID       string           `json:"delivery_id"`
	SourceDeliveryID string           `json:"source_delivery_id"`
	ConsumerID       string           `json:"consumer_id"`
	SessionID        string           `json:"session_id"`
	ConsumerVersion  int              `json:"consumer_version"`
	ConsumerOffset   int64            `json:"consumer_offset"`
	GlobalOffset     int64            `json:"global_offset"`
	Attempt          int              `json:"attempt"`
	Payload          json.RawMessage  `json:"payload"`
	Conversion       Conversion       `json:"conversion"`
	Semantic         SemanticDecision `json:"semantic"`
	DeliveredAt      string           `json:"delivered_at"`
}

type AckRecord struct {
	ConsumerID     string    `json:"consumer_id"`
	DeliveryID     string    `json:"delivery_id"`
	GlobalOffset   int64     `json:"global_offset"`
	ConsumerOffset int64     `json:"consumer_offset"`
	SessionID      string    `json:"session_id"`
	Semantics      Semantics `json:"semantics"`
	AckedAt        string    `json:"acked_at"`
}

type DedupRecord struct {
	IdempotencySeq int64     `json:"idempotency_seq"`
	DeliveryID     string    `json:"delivery_id"`
	GlobalOffset   int64     `json:"global_offset"`
	Semantics      Semantics `json:"semantics"`
}

type Delivery struct {
	Type             string           `json:"type"`
	DeliveryID       string           `json:"delivery_id"`
	SourceDeliveryID string           `json:"source_delivery_id"`
	ConsumerID       string           `json:"consumer_id"`
	ConsumerVersion  int              `json:"consumer_version"`
	ConsumerOffset   int64            `json:"consumer_offset"`
	GlobalOffset     int64            `json:"global_offset"`
	Attempt          int              `json:"attempt"`
	Payload          map[string]any   `json:"payload"`
	Conversion       Conversion       `json:"conversion"`
	Semantic         SemanticDecision `json:"semantic"`
}

type ExistingResult struct {
	DeliveryID   string `json:"delivery_id"`
	GlobalOffset int64  `json:"global_offset"`
	AcceptedAt   string `json:"accepted_at"`
}

type PublishResult struct {
	OK             bool            `json:"ok"`
	Type           string          `json:"type"`
	Accepted       bool            `json:"accepted"`
	Duplicate      bool            `json:"duplicate"`
	DeliveryID     string          `json:"delivery_id"`
	GlobalOffset   int64           `json:"global_offset"`
	SourceVersion  int             `json:"source_version"`
	Semantics      Semantics       `json:"semantics"`
	IdempotencySeq int64           `json:"idempotency_seq,omitempty"`
	DedupKey       string          `json:"dedup_key,omitempty"`
	ExistingResult *ExistingResult `json:"existing_result,omitempty"`
}

type ErrorResponse struct {
	OK    bool      `json:"ok"`
	Type  string    `json:"type"`
	Error *APIError `json:"error"`
}
