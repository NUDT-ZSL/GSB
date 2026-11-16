package protocol

import (
	"encoding/json"
	"reflect"
	"testing"
)

func storedV1() StoredMessage {
	return StoredMessage{
		GlobalOffset: 1,
		Version:      1,
		DeliveryID:   "m1",
		Body:         json.RawMessage(`{"text":"v1"}`),
		PartitionKey: DefaultPartition,
	}
}

func storedV2() StoredMessage {
	return StoredMessage{
		GlobalOffset: 2,
		Version:      2,
		DeliveryID:   "m2",
		Body:         json.RawMessage(`{"text":"v2"}`),
		TimestampMS:  12345,
		RetryCount:   2,
		PartitionKey: DefaultPartition,
	}
}

func storedV3() StoredMessage {
	message := storedV2()
	message.GlobalOffset = 3
	message.Version = 3
	message.DeliveryID = "m3"
	message.PartitionKey = "orders"
	message.IdempotencySeq = 99
	return message
}

func TestConversionMatrix(t *testing.T) {
	sources := []StoredMessage{storedV1(), storedV2(), storedV3()}
	for _, source := range sources {
		for target := 1; target <= 3; target++ {
			payload, conversion := BuildCompatiblePayload(source, target, source.RetryCount, "conv")
			sourceFields := FieldsForVersion(source.Version)
			targetFields := FieldsForVersion(target)
			if conversion.SourceVersion != source.Version || conversion.DeclaredVersion != target {
				t.Fatalf("version metadata mismatch: %+v", conversion)
			}
			if !reflect.DeepEqual(conversion.SourceFields, sourceFields) || !reflect.DeepEqual(conversion.TargetFields, targetFields) {
				t.Fatalf("field metadata mismatch: %+v", conversion)
			}
			if !reflect.DeepEqual(conversion.StrippedFields, difference(sourceFields, targetFields)) {
				t.Fatalf("stripped mismatch for v%d->v%d: %v", source.Version, target, conversion.StrippedFields)
			}
			if !reflect.DeepEqual(conversion.FilledFields, difference(targetFields, sourceFields)) {
				t.Fatalf("filled mismatch for v%d->v%d: %v", source.Version, target, conversion.FilledFields)
			}
			if len(payload) != len(targetFields) {
				t.Fatalf("payload has wrong field count for v%d->v%d: %v", source.Version, target, payload)
			}
			if payload["version"] != target || payload["delivery_id"] != source.DeliveryID {
				t.Fatalf("payload identity mismatch: %v", payload)
			}
		}
	}
}

func TestConversionDefaultsAndMutation(t *testing.T) {
	payload, conversion := BuildCompatiblePayload(storedV1(), 3, 0, "c1")
	if payload["timestamp_ms"] != int64(0) || payload["retry_count"] != 0 || payload["partition_key"] != DefaultPartition || payload["idempotency_seq"] != int64(0) {
		t.Fatalf("unexpected v1->v3 defaults: %v", payload)
	}
	if conversion.FieldOrigins["timestamp_ms"] != "default" || conversion.FieldOrigins["partition_key"] != "default" {
		t.Fatalf("defaults were not marked: %v", conversion.FieldOrigins)
	}

	payload, conversion = BuildCompatiblePayload(storedV1(), 2, 3, "c2")
	if payload["retry_count"] != 3 || conversion.FieldOrigins["retry_count"] != "derived" {
		t.Fatalf("upcast retry count was not derived: %v %v", payload, conversion.FieldOrigins)
	}

	payload, conversion = BuildCompatiblePayload(storedV2(), 2, 3, "c3")
	if payload["retry_count"] != 3 || conversion.FieldOrigins["retry_count"] != "mutated" {
		t.Fatalf("retry mutation was not marked: %v %v", payload, conversion.FieldOrigins)
	}
	if !reflect.DeepEqual(conversion.MutatedFields, []string{"retry_count"}) {
		t.Fatalf("unexpected mutated fields: %v", conversion.MutatedFields)
	}

	_, conversion = BuildCompatiblePayload(storedV3(), 1, 0, "c4")
	if !reflect.DeepEqual(conversion.StrippedFields, []string{"timestamp_ms", "retry_count", "partition_key", "idempotency_seq"}) {
		t.Fatalf("unexpected v3->v1 strips: %v", conversion.StrippedFields)
	}
}
