package protocol

import "encoding/json"

func contains(fields []string, field string) bool {
	for _, candidate := range fields {
		if candidate == field {
			return true
		}
	}
	return false
}

func difference(left, right []string) []string {
	out := make([]string, 0)
	for _, candidate := range left {
		if !contains(right, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func direction(sourceVersion, targetVersion int) string {
	switch {
	case targetVersion < sourceVersion:
		return "downgrade"
	case targetVersion > sourceVersion:
		return "upcast"
	default:
		return "same"
	}
}

// BuildCompatiblePayload derives a target-version payload from an immutable
// source message. Default fields are explicitly tracked in the conversion.
func BuildCompatiblePayload(message StoredMessage, targetVersion, retryCount int, conversionID string) (map[string]any, Conversion) {
	sourceFields := FieldsForVersion(message.Version)
	targetFields := FieldsForVersion(targetVersion)
	payload := map[string]any{
		"version":     targetVersion,
		"delivery_id": message.DeliveryID,
		"body":        message.Body,
	}
	origins := map[string]string{
		"version":     "declared",
		"delivery_id": "source",
		"body":        "source",
	}
	mutated := make([]string, 0)
	if targetVersion >= 2 {
		payload["timestamp_ms"] = message.TimestampMS
		if message.Version >= 2 {
			origins["timestamp_ms"] = "source"
		} else {
			origins["timestamp_ms"] = "default"
		}
		payload["retry_count"] = retryCount
		switch {
		case message.Version < 2 && retryCount == 0:
			origins["retry_count"] = "default"
		case message.Version < 2:
			origins["retry_count"] = "derived"
		case retryCount != message.RetryCount:
			origins["retry_count"] = "mutated"
			mutated = append(mutated, "retry_count")
		default:
			origins["retry_count"] = "source"
		}
	}
	if targetVersion == 3 {
		payload["partition_key"] = message.PartitionKey
		if message.Version == 3 {
			origins["partition_key"] = "source"
			origins["idempotency_seq"] = "source"
		} else {
			origins["partition_key"] = "default"
			origins["idempotency_seq"] = "default"
		}
		payload["idempotency_seq"] = message.IdempotencySeq
	}
	conversion := Conversion{
		ConversionID:     conversionID,
		SourceDeliveryID: message.DeliveryID,
		DeliveryID:       message.DeliveryID,
		SourceVersion:    message.Version,
		DeclaredVersion:  targetVersion,
		Direction:        direction(message.Version, targetVersion),
		SourceFields:     sourceFields,
		TargetFields:     targetFields,
		StrippedFields:   difference(sourceFields, targetFields),
		FilledFields:     difference(targetFields, sourceFields),
		MutatedFields:    mutated,
		FieldOrigins:     origins,
	}
	return payload, conversion
}

func MarshalPayload(payload map[string]any) (json.RawMessage, error) {
	return json.Marshal(payload)
}
