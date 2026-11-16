package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"local/message-interop/internal/protocol"
)

func TestJournalRecoveryAndIncompleteTail(t *testing.T) {
	dir := t.TempDir()
	persistent, _, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	message := &protocol.StoredMessage{
		Version:      1,
		DeliveryID:   "m1",
		Body:         json.RawMessage(`{"x":1}`),
		PartitionKey: protocol.DefaultPartition,
		Semantics:    protocol.AtMostOnce,
		AcceptedAt:   Now(),
	}
	if seq, err := persistent.AppendMessage(message); err != nil || seq != 1 || message.GlobalOffset != 1 {
		t.Fatalf("append message: seq=%d offset=%d err=%v", seq, message.GlobalOffset, err)
	}
	if err := persistent.Close(); err != nil {
		t.Fatal(err)
	}

	path := filepath.Join(dir, "messages.log")
	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write([]byte(`{"seq":2,"crc32":"deadbeef","record":{"broken":`)); err != nil {
		t.Fatal(err)
	}
	file.Close()

	reopened, scan, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if scan.MessageCount != 1 || len(scan.TailIssues) != 1 || scan.TruncatedBytes == 0 {
		t.Fatalf("unexpected scan: %+v", scan)
	}
	if scan.TailIssues[0].Action != "truncated_incomplete_tail" {
		t.Fatalf("unexpected tail action: %+v", scan.TailIssues[0])
	}
	message2 := &protocol.StoredMessage{
		Version:      1,
		DeliveryID:   "m2",
		Body:         json.RawMessage(`{"x":2}`),
		PartitionKey: protocol.DefaultPartition,
		Semantics:    protocol.AtMostOnce,
		AcceptedAt:   Now(),
	}
	if seq, err := reopened.AppendMessage(message2); err != nil || seq != 2 {
		t.Fatalf("append after recovery: seq=%d err=%v", seq, err)
	}
}

func TestConcurrentAppendProducesFramedRecords(t *testing.T) {
	dir := t.TempDir()
	persistent, _, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	for index := 0; index < 40; index++ {
		go func(index int) {
			defer func() { done <- struct{}{} }()
			_, err := persistent.Append(Dedup, protocol.DedupRecord{
				IdempotencySeq: int64(index + 1),
				DeliveryID:     "m",
				GlobalOffset:   1,
				Semantics:      protocol.ExactlyOnce,
			})
			if err != nil {
				t.Errorf("append: %v", err)
			}
		}(index)
	}
	for index := 0; index < 40; index++ {
		<-done
	}
	if err := persistent.Close(); err != nil {
		t.Fatal(err)
	}
	_, scan, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if scan.DedupCount != 40 || len(scan.TailIssues) != 0 {
		t.Fatalf("unexpected scan: %+v", scan)
	}
}
