package engine

import (
	"encoding/json"
	"fmt"
	"sync"
	"testing"
	"time"

	"local/message-interop/internal/protocol"
)

func newTestEngine(t *testing.T) (*Engine, string) {
	t.Helper()
	dir := t.TempDir()
	core, _, err := Open(dir, 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = core.Close() })
	return core, dir
}

func parseMessage(t *testing.T, raw string) (protocol.Message, protocol.Semantics) {
	t.Helper()
	message, semantics, apiErr := protocol.ParsePublish([]byte(raw))
	if apiErr != nil {
		t.Fatalf("parse: %v", apiErr)
	}
	return message, semantics
}

func TestPublishConvertRetryAndAck(t *testing.T) {
	core, _ := newTestEngine(t)
	message, semantics := parseMessage(t, `{"type":"publish","semantics":"at_least_once","message":{"version":2,"delivery_id":"m1","body":{"x":1},"timestamp_ms":42,"retry_count":0}}`)
	result, apiErr := core.Publish(message, semantics)
	if apiErr != nil || !result.Accepted || result.GlobalOffset != 1 {
		t.Fatalf("publish: %+v %v", result, apiErr)
	}
	session, apiErr := core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 1})
	if apiErr != nil {
		t.Fatal(apiErr)
	}
	deliveries, err := core.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != 1 {
		t.Fatalf("deliveries: %v %v", deliveries, err)
	}
	first := deliveries[0]
	if first.Conversion.Direction != "downgrade" || len(first.Conversion.StrippedFields) != 2 || first.ConsumerOffset != 1 {
		t.Fatalf("unexpected conversion: %+v", first)
	}
	core.CloseSession(session.ID)
	session, apiErr = core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 2})
	if apiErr != nil {
		t.Fatal(apiErr)
	}
	deliveries, err = core.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != 1 {
		t.Fatalf("retry: %v %v", deliveries, err)
	}
	retry := deliveries[0]
	if retry.Attempt != 2 || retry.Payload["retry_count"] != 1 || retry.Semantic.Decision != "redeliver_unacked" {
		t.Fatalf("unexpected retry: %+v", retry)
	}
	ack, ackErr := core.Ack(session.ID, protocol.AckRequest{ConsumerID: "c1", DeliveryID: "m1", Offset: 1})
	if ackErr != nil || !ack.Acked {
		t.Fatalf("ack: %+v %v", ack, ackErr)
	}
	core.CloseSession(session.ID)
	session, apiErr = core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 2})
	if apiErr != nil {
		t.Fatal(apiErr)
	}
	deliveries, err = core.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != 0 {
		t.Fatalf("acked message redelivered: %v %v", deliveries, err)
	}
}

func TestAtMostOnceNeverRetries(t *testing.T) {
	core, _ := newTestEngine(t)
	message, semantics := parseMessage(t, `{"type":"publish","semantics":"at_most_once","message":{"version":1,"delivery_id":"m1","body":"x"}}`)
	if _, apiErr := core.Publish(message, semantics); apiErr != nil {
		t.Fatal(apiErr)
	}
	session, _ := core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 1})
	deliveries, _ := core.NextDeliveries(session.ID)
	if len(deliveries) != 1 {
		t.Fatalf("first delivery count = %d", len(deliveries))
	}
	core.CloseSession(session.ID)
	session, _ = core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 1})
	deliveries, _ = core.NextDeliveries(session.ID)
	if len(deliveries) != 0 {
		t.Fatalf("at-most-once redelivered: %+v", deliveries)
	}
}

func TestExactlyOnceDedupSequentialConcurrentAndRestart(t *testing.T) {
	core, dir := newTestEngine(t)
	request := `{"type":"publish","semantics":"exactly_once","message":{"version":3,"delivery_id":"exact-1","body":{"n":1},"timestamp_ms":1,"retry_count":0,"partition_key":"p","idempotency_seq":123}}`
	message, semantics := parseMessage(t, request)
	result, apiErr := core.Publish(message, semantics)
	if apiErr != nil || !result.Accepted {
		t.Fatalf("first publish: %+v %v", result, apiErr)
	}
	duplicate, apiErr := core.Publish(message, semantics)
	if apiErr != nil || !duplicate.Duplicate || duplicate.GlobalOffset != result.GlobalOffset {
		t.Fatalf("sequential duplicate: %+v %v", duplicate, apiErr)
	}

	concurrentDir := t.TempDir()
	concurrent, _, err := Open(concurrentDir, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	results := make(chan protocol.PublishResult, 20)
	for index := 0; index < 20; index++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			msg, sem := parseMessage(t, fmt.Sprintf(`{"type":"publish","semantics":"exactly_once","message":{"version":3,"delivery_id":"exact-%d","body":{"n":%d},"timestamp_ms":1,"retry_count":0,"partition_key":"p","idempotency_seq":777}}`, index, index))
			result, apiErr := concurrent.Publish(msg, sem)
			if apiErr != nil {
				t.Errorf("concurrent publish: %v", apiErr)
			}
			results <- result
		}(index)
	}
	wg.Wait()
	close(results)
	accepted := 0
	duplicates := 0
	var offset int64
	for result := range results {
		if result.Accepted {
			accepted++
			offset = result.GlobalOffset
		}
		if result.Duplicate {
			duplicates++
		}
	}
	if accepted != 1 || duplicates != 19 {
		t.Fatalf("concurrent convergence: accepted=%d duplicates=%d", accepted, duplicates)
	}
	_ = concurrent.Close()

	if err := core.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, report, err := Open(dir, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if report.RebuiltDedupEntries != 1 {
		t.Fatalf("dedup rebuild report = %d", report.RebuiltDedupEntries)
	}
	replayed, apiErr := reopened.Publish(message, semantics)
	if apiErr != nil || !replayed.Duplicate || replayed.GlobalOffset != offset {
		t.Fatalf("restart duplicate: %+v %v, want offset %d", replayed, apiErr, offset)
	}
}

func TestRecoveryPreservesUnackedRetryAndOffset(t *testing.T) {
	core, dir := newTestEngine(t)
	message, semantics := parseMessage(t, `{"type":"publish","semantics":"at_least_once","message":{"version":3,"delivery_id":"m1","body":"x","timestamp_ms":1,"retry_count":0,"partition_key":"p","idempotency_seq":11}}`)
	if _, apiErr := core.Publish(message, semantics); apiErr != nil {
		t.Fatal(apiErr)
	}
	session, _ := core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 3, PartitionKey: "p"})
	deliveries, err := core.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != 1 {
		t.Fatalf("initial: %v %v", deliveries, err)
	}
	if err := core.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, report, err := Open(dir, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if report.RecoveredOffsets["c1"] != 0 || report.RecoveredDeliveries != 1 {
		t.Fatalf("unexpected report: %+v", report)
	}
	session, _ = reopened.Subscribe(protocol.SubscribeRequest{ConsumerID: "c1", Version: 1, PartitionKey: "p"})
	deliveries, err = reopened.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != 1 {
		t.Fatalf("recovered delivery: %v %v", deliveries, err)
	}
	if deliveries[0].Attempt != 2 || deliveries[0].ConsumerOffset != 1 || deliveries[0].Semantic.RetryCount != 1 {
		t.Fatalf("unexpected recovered retry: %+v", deliveries[0])
	}
	if _, apiErr := reopened.Ack(session.ID, protocol.AckRequest{ConsumerID: "c1", DeliveryID: "m1", Offset: 1}); apiErr != nil {
		t.Fatal(apiErr)
	}
}

func TestPartitionOrdering(t *testing.T) {
	core, _ := newTestEngine(t)
	for index, partition := range []string{"a", "b", "a", "a"} {
		msg, sem := parseMessage(t, fmt.Sprintf(`{"type":"publish","semantics":"at_most_once","message":{"version":3,"delivery_id":"m%d","body":{"n":%d},"timestamp_ms":1,"retry_count":0,"partition_key":"%s","idempotency_seq":%d}}`, index, index, partition, index+1))
		if _, apiErr := core.Publish(msg, sem); apiErr != nil {
			t.Fatal(apiErr)
		}
	}
	session, _ := core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c-a", Version: 1, PartitionKey: "a"})
	deliveries, err := core.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != 3 {
		t.Fatalf("partition a deliveries: %v %v", deliveries, err)
	}
	for index, delivery := range deliveries {
		want := []string{"m0", "m2", "m3"}[index]
		if delivery.DeliveryID != want {
			t.Fatalf("delivery order = %v, want %s at %d", deliveries, want, index)
		}
	}
	body := string(deliveries[0].Payload["body"].(json.RawMessage))
	if body != `{"n":0}` {
		t.Fatalf("body = %s", body)
	}
}

func TestConcurrentPartitionOrderMatchesLog(t *testing.T) {
	core, _ := newTestEngine(t)
	var wg sync.WaitGroup
	for index := 0; index < 60; index++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			msg, sem := parseMessage(t, fmt.Sprintf(`{"type":"publish","semantics":"at_most_once","message":{"version":3,"delivery_id":"same-%02d","body":{"n":%d},"timestamp_ms":1,"retry_count":0,"partition_key":"same","idempotency_seq":%d}}`, index, index, index+1000))
			if _, apiErr := core.Publish(msg, sem); apiErr != nil {
				t.Errorf("publish: %v", apiErr)
			}
		}(index)
	}
	wg.Wait()
	logged := core.Messages()
	session, apiErr := core.Subscribe(protocol.SubscribeRequest{ConsumerID: "c", Version: 1, PartitionKey: "same"})
	if apiErr != nil {
		t.Fatal(apiErr)
	}
	deliveries, err := core.NextDeliveries(session.ID)
	if err != nil || len(deliveries) != len(logged) {
		t.Fatalf("delivery count: got=%d want=%d err=%v", len(deliveries), len(logged), err)
	}
	for index := range logged {
		if deliveries[index].GlobalOffset != logged[index].GlobalOffset || deliveries[index].DeliveryID != logged[index].DeliveryID {
			t.Fatalf("partition order diverged at %d: delivery=%+v log=%+v", index, deliveries[index], logged[index])
		}
	}
}

func TestConcurrentConsumersEachObserveStableOrder(t *testing.T) {
	core, _ := newTestEngine(t)
	for index := 0; index < 15; index++ {
		msg, sem := parseMessage(t, fmt.Sprintf(`{"type":"publish","semantics":"at_least_once","message":{"version":2,"delivery_id":"multi-%02d","body":{"n":%d},"timestamp_ms":1,"retry_count":0}}`, index, index))
		if _, apiErr := core.Publish(msg, sem); apiErr != nil {
			t.Fatal(apiErr)
		}
	}
	sessions := make([]*Session, 3)
	for index, version := range []int{1, 2, 3} {
		session, apiErr := core.Subscribe(protocol.SubscribeRequest{ConsumerID: fmt.Sprintf("consumer-%d", index), Version: version})
		if apiErr != nil {
			t.Fatal(apiErr)
		}
		sessions[index] = session
	}
	results := make(chan []protocol.Delivery, len(sessions))
	var wg sync.WaitGroup
	for _, session := range sessions {
		wg.Add(1)
		go func(session *Session) {
			defer wg.Done()
			deliveries, err := core.NextDeliveries(session.ID)
			if err != nil {
				t.Errorf("next deliveries: %v", err)
			}
			results <- deliveries
		}(session)
	}
	wg.Wait()
	close(results)
	for deliveries := range results {
		if len(deliveries) != 15 {
			t.Fatalf("consumer delivery count = %d", len(deliveries))
		}
		for index, delivery := range deliveries {
			want := fmt.Sprintf("multi-%02d", index)
			if delivery.DeliveryID != want || delivery.ConsumerOffset != int64(index+1) {
				t.Fatalf("consumer stream diverged at %d: %+v", index, delivery)
			}
		}
	}
}

func TestSemanticRejectionWithoutKey(t *testing.T) {
	_, _, err := Open(t.TempDir(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_, _, apiErr := protocol.ParsePublish([]byte(`{"type":"publish","semantics":"exactly_once","message":{"version":2,"delivery_id":"x","body":{},"timestamp_ms":1,"retry_count":0}}`))
	if apiErr == nil || apiErr.Code != protocol.CodeSemantic {
		t.Fatalf("want E_SEMANTIC, got %+v", apiErr)
	}
}
