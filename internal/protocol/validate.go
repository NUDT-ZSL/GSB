package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

var versionFields = map[int][]string{
	1: {"version", "delivery_id", "body"},
	2: {"version", "delivery_id", "body", "timestamp_ms", "retry_count"},
	3: {"version", "delivery_id", "body", "timestamp_ms", "retry_count", "partition_key", "idempotency_seq"},
}

func FieldsForVersion(version int) []string {
	fields := versionFields[version]
	return append([]string(nil), fields...)
}

func fieldSet(fields []string) map[string]bool {
	out := make(map[string]bool, len(fields))
	for _, field := range fields {
		out[field] = true
	}
	return out
}

func decodeObject(raw []byte, code, label string) (map[string]json.RawMessage, *APIError) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var object map[string]json.RawMessage
	if err := decoder.Decode(&object); err != nil {
		return nil, NewError(code, label+" must be a JSON object")
	}
	if object == nil {
		return nil, NewError(code, label+" must be a JSON object")
	}
	return object, nil
}

func decodeString(raw json.RawMessage, code, field string) (string, *APIError) {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", NewError(code, field+" must be a string")
	}
	return value, nil
}

func decodeInteger(raw json.RawMessage, code, field string) (int64, *APIError) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var number json.Number
	if err := decoder.Decode(&number); err != nil {
		return 0, NewError(code, field+" must be an integer")
	}
	value, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil {
		return 0, NewError(code, field+" must be an integer")
	}
	return value, nil
}

func parseSemantics(raw json.RawMessage) (Semantics, *APIError) {
	value, apiErr := decodeString(raw, CodeFormat, "semantics")
	if apiErr != nil {
		return "", apiErr
	}
	normalized := strings.ToLower(strings.TrimSpace(value))
	normalized = strings.ReplaceAll(normalized, "-", "_")
	semantics := Semantics(normalized)
	if !semantics.Valid() {
		return "", NewError(CodeSemantic, "unsupported delivery semantics: "+value)
	}
	return semantics, nil
}

// ParsePublish validates a complete publisher request and returns only a fully
// validated message. A rejected message never reaches the delivery engine.
func ParsePublish(raw []byte) (Message, Semantics, *APIError) {
	var message Message
	request, apiErr := decodeObject(raw, CodeFormat, "request")
	if apiErr != nil {
		return message, "", apiErr
	}
	typeRaw, ok := request["type"]
	if !ok {
		return message, "", NewError(CodeFormat, "missing required field: type")
	}
	requestType, apiErr := decodeString(typeRaw, CodeFormat, "type")
	if apiErr != nil {
		return message, "", apiErr
	}
	if requestType != "publish" {
		err := NewError(CodeProtocol, "expected request type publish")
		err.Details = map[string]any{"got": requestType}
		return message, "", err
	}
	messageRaw, ok := request["message"]
	if !ok {
		return message, "", NewError(CodeFormat, "missing required field: message")
	}

	semantics := AtLeastOnce
	var selected json.RawMessage
	for _, field := range []string{"semantics", "delivery_semantics"} {
		if value, present := request[field]; present {
			if selected != nil {
				return message, "", NewError(CodeFormat, "specify only one of semantics or delivery_semantics")
			}
			selected = value
		}
	}
	if selected != nil {
		parsed, semanticErr := parseSemantics(selected)
		if semanticErr != nil {
			return message, "", semanticErr
		}
		semantics = parsed
	}

	object, apiErr := decodeObject(messageRaw, CodeFormat, "message")
	if apiErr != nil {
		return message, "", apiErr
	}
	versionRaw, ok := object["version"]
	if !ok {
		return message, "", NewError(CodeVersion, "missing message version")
	}
	versionValue, apiErr := decodeInteger(versionRaw, CodeVersion, "message.version")
	if apiErr != nil {
		return message, "", apiErr
	}
	version := int(versionValue)
	allowed, ok := versionFields[version]
	if !ok {
		err := NewError(CodeVersion, fmt.Sprintf("unsupported message version %d", version))
		err.Details = map[string]any{"supported_versions": []int{1, 2, 3}}
		return message, "", err
	}

	// Exactly-once is a semantic capability, so its missing key is reported as
	// E_SEMANTIC rather than being hidden behind an ordinary format error.
	if semantics == ExactlyOnce {
		seqRaw, present := object["idempotency_seq"]
		if version < 3 || !present {
			err := NewError(CodeSemantic, "exactly_once requires v3 idempotency_seq")
			err.Details = map[string]any{"version": version, "missing_field": "idempotency_seq"}
			return message, "", err
		}
		seq, seqErr := decodeInteger(seqRaw, CodeSemantic, "idempotency_seq")
		if seqErr != nil || seq <= 0 {
			return message, "", NewError(CodeSemantic, "exactly_once requires a positive integer idempotency_seq")
		}
	}

	allowedSet := fieldSet(allowed)
	for field := range object {
		if !allowedSet[field] {
			err := NewError(CodeFormat, fmt.Sprintf("field %s is not supported by version v%d", field, version))
			err.Details = map[string]any{"field": field, "version": version, "allowed": allowed}
			return message, "", err
		}
	}
	for _, field := range allowed {
		if _, present := object[field]; !present {
			err := NewError(CodeFormat, fmt.Sprintf("missing required field for v%d: %s", version, field))
			err.Details = map[string]any{"field": field, "version": version}
			return message, "", err
		}
	}

	deliveryID, apiErr := decodeString(object["delivery_id"], CodeFormat, "delivery_id")
	if apiErr != nil {
		return message, "", apiErr
	}
	if strings.TrimSpace(deliveryID) == "" {
		return message, "", NewError(CodeFormat, "delivery_id must not be empty")
	}
	if bytes.Equal(bytes.TrimSpace(object["body"]), []byte("null")) {
		return message, "", NewError(CodeFormat, "body must not be null")
	}

	message = Message{
		Version:      version,
		DeliveryID:   deliveryID,
		Body:         append(json.RawMessage(nil), object["body"]...),
		PartitionKey: DefaultPartition,
	}
	if version >= 2 {
		timestamp, tsErr := decodeInteger(object["timestamp_ms"], CodeFormat, "timestamp_ms")
		if tsErr != nil {
			return Message{}, "", tsErr
		}
		retry, retryErr := decodeInteger(object["retry_count"], CodeFormat, "retry_count")
		if retryErr != nil || retry < 0 {
			return Message{}, "", NewError(CodeFormat, "retry_count must be a non-negative integer")
		}
		message.TimestampMS = timestamp
		message.RetryCount = int(retry)
	}
	if version == 3 {
		partition, partitionErr := decodeString(object["partition_key"], CodeFormat, "partition_key")
		if partitionErr != nil {
			return Message{}, "", partitionErr
		}
		if strings.TrimSpace(partition) == "" {
			return Message{}, "", NewError(CodeFormat, "partition_key must not be empty")
		}
		seq, seqErr := decodeInteger(object["idempotency_seq"], CodeFormat, "idempotency_seq")
		if seqErr != nil || seq < 0 {
			return Message{}, "", NewError(CodeFormat, "idempotency_seq must be a non-negative integer")
		}
		message.PartitionKey = partition
		message.IdempotencySeq = seq
	}
	return message, semantics, nil
}

func ParseSubscribe(raw []byte) (SubscribeRequest, *APIError) {
	var request SubscribeRequest
	object, apiErr := decodeObject(raw, CodeFormat, "request")
	if apiErr != nil {
		return request, apiErr
	}
	requestType, apiErr := decodeString(object["type"], CodeFormat, "type")
	if apiErr != nil || requestType != "subscribe" {
		return request, NewError(CodeProtocol, "expected request type subscribe")
	}
	consumerID, apiErr := decodeString(object["consumer_id"], CodeFormat, "consumer_id")
	if apiErr != nil || strings.TrimSpace(consumerID) == "" {
		return request, NewError(CodeFormat, "consumer_id must be a non-empty string")
	}
	versionValue, apiErr := decodeInteger(object["version"], CodeVersion, "version")
	if apiErr != nil {
		return request, apiErr
	}
	if versionValue < 1 || versionValue > 3 {
		err := NewError(CodeVersion, fmt.Sprintf("unsupported consumer version %d", versionValue))
		err.Details = map[string]any{"supported_versions": []int{1, 2, 3}}
		return request, err
	}
	request.ConsumerID = consumerID
	request.Version = int(versionValue)
	if partition, present := object["partition_key"]; present {
		value, partitionErr := decodeString(partition, CodeFormat, "partition_key")
		if partitionErr != nil {
			return request, partitionErr
		}
		request.PartitionKey = value
	}
	if semantics, present := object["semantics"]; present {
		value, semanticErr := parseSemantics(semantics)
		if semanticErr != nil {
			return request, semanticErr
		}
		request.SemanticsHint = value
	}
	return request, nil
}

func ParseAck(raw []byte) (AckRequest, *APIError) {
	var request AckRequest
	object, apiErr := decodeObject(raw, CodeFormat, "request")
	if apiErr != nil {
		return request, apiErr
	}
	requestType, apiErr := decodeString(object["type"], CodeFormat, "type")
	if apiErr != nil || requestType != "ack" {
		return request, NewError(CodeProtocol, "expected request type ack")
	}
	consumerID, apiErr := decodeString(object["consumer_id"], CodeFormat, "consumer_id")
	if apiErr != nil || strings.TrimSpace(consumerID) == "" {
		return request, NewError(CodeFormat, "consumer_id must be a non-empty string")
	}
	deliveryID, apiErr := decodeString(object["delivery_id"], CodeFormat, "delivery_id")
	if apiErr != nil || strings.TrimSpace(deliveryID) == "" {
		return request, NewError(CodeFormat, "delivery_id must be a non-empty string")
	}
	offset, apiErr := decodeInteger(object["offset"], CodeFormat, "offset")
	if apiErr != nil || offset < 1 {
		return request, NewError(CodeFormat, "offset must be a positive integer")
	}
	request.ConsumerID = consumerID
	request.DeliveryID = deliveryID
	request.Offset = offset
	return request, nil
}

func DetectType(raw []byte) (string, *APIError) {
	object, apiErr := decodeObject(raw, CodeFormat, "request")
	if apiErr != nil {
		return "", apiErr
	}
	value, apiErr := decodeString(object["type"], CodeFormat, "type")
	if apiErr != nil {
		return "", apiErr
	}
	return value, nil
}
