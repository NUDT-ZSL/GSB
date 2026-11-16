package protocol

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestParsePublishVersions(t *testing.T) {
	tests := []struct {
		name      string
		raw       string
		version   int
		fields    []string
		semantics Semantics
	}{
		{
			name:      "v1",
			raw:       `{"type":"publish","semantics":"at_most_once","message":{"version":1,"delivery_id":"a","body":{"x":1}}}`,
			version:   1,
			fields:    []string{"version", "delivery_id", "body"},
			semantics: AtMostOnce,
		},
		{
			name:      "v2",
			raw:       `{"type":"publish","delivery_semantics":"at-least-once","message":{"version":2,"delivery_id":"b","body":"x","timestamp_ms":123,"retry_count":4}}`,
			version:   2,
			fields:    []string{"version", "delivery_id", "body", "timestamp_ms", "retry_count"},
			semantics: AtLeastOnce,
		},
		{
			name:      "v3",
			raw:       `{"type":"publish","semantics":"exactly_once","message":{"version":3,"delivery_id":"c","body":[1,2],"timestamp_ms":0,"retry_count":0,"partition_key":"p","idempotency_seq":9}}`,
			version:   3,
			fields:    []string{"version", "delivery_id", "body", "timestamp_ms", "retry_count", "partition_key", "idempotency_seq"},
			semantics: ExactlyOnce,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			message, semantics, apiErr := ParsePublish([]byte(test.raw))
			if apiErr != nil {
				t.Fatalf("unexpected error: %v", apiErr)
			}
			if message.Version != test.version || semantics != test.semantics {
				t.Fatalf("got version=%d semantics=%s", message.Version, semantics)
			}
			if !reflect.DeepEqual(FieldsForVersion(message.Version), test.fields) {
				t.Fatalf("field set mismatch: %v", FieldsForVersion(message.Version))
			}
			if len(message.Body) == 0 || !json.Valid(message.Body) {
				t.Fatalf("body was not preserved: %q", message.Body)
			}
		})
	}
}

func TestParsePublishRejections(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		code string
	}{
		{
			name: "unknown version",
			raw:  `{"type":"publish","message":{"version":9,"delivery_id":"x","body":{}}}`,
			code: CodeVersion,
		},
		{
			name: "missing version",
			raw:  `{"type":"publish","message":{"delivery_id":"x","body":{}}}`,
			code: CodeVersion,
		},
		{
			name: "v1 cannot carry timestamp",
			raw:  `{"type":"publish","message":{"version":1,"delivery_id":"x","body":{},"timestamp_ms":1}}`,
			code: CodeFormat,
		},
		{
			name: "negative retry",
			raw:  `{"type":"publish","message":{"version":2,"delivery_id":"x","body":{},"timestamp_ms":1,"retry_count":-1}}`,
			code: CodeFormat,
		},
		{
			name: "fractional timestamp",
			raw:  `{"type":"publish","message":{"version":2,"delivery_id":"x","body":{},"timestamp_ms":1.5,"retry_count":0}}`,
			code: CodeFormat,
		},
		{
			name: "exactly once without key",
			raw:  `{"type":"publish","semantics":"exactly_once","message":{"version":2,"delivery_id":"x","body":{},"timestamp_ms":1,"retry_count":0}}`,
			code: CodeSemantic,
		},
		{
			name: "exactly once zero key",
			raw:  `{"type":"publish","semantics":"exactly_once","message":{"version":3,"delivery_id":"x","body":{},"timestamp_ms":1,"retry_count":0,"partition_key":"p","idempotency_seq":0}}`,
			code: CodeSemantic,
		},
		{
			name: "empty partition",
			raw:  `{"type":"publish","message":{"version":3,"delivery_id":"x","body":{},"timestamp_ms":1,"retry_count":0,"partition_key":"","idempotency_seq":1}}`,
			code: CodeFormat,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, _, apiErr := ParsePublish([]byte(test.raw))
			if apiErr == nil || apiErr.Code != test.code {
				t.Fatalf("got %+v, want %s", apiErr, test.code)
			}
		})
	}
}

func TestParseSubscribeAndAck(t *testing.T) {
	subscribe, apiErr := ParseSubscribe([]byte(`{"type":"subscribe","consumer_id":"c","version":2,"partition_key":"p","semantics":"at_least_once"}`))
	if apiErr != nil || subscribe.ConsumerID != "c" || subscribe.Version != 2 || subscribe.PartitionKey != "p" {
		t.Fatalf("unexpected subscribe result: %+v %v", subscribe, apiErr)
	}
	if _, apiErr := ParseSubscribe([]byte(`{"type":"subscribe","consumer_id":"c","version":4}`)); apiErr == nil || apiErr.Code != CodeVersion {
		t.Fatalf("expected E_VERSION, got %+v", apiErr)
	}
	ack, apiErr := ParseAck([]byte(`{"type":"ack","consumer_id":"c","delivery_id":"m","offset":3}`))
	if apiErr != nil || ack.Offset != 3 || ack.DeliveryID != "m" {
		t.Fatalf("unexpected ack result: %+v %v", ack, apiErr)
	}
}
