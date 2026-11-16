package engine

import (
	"encoding/json"
	"fmt"
	"sort"
	"sync"
	"time"

	"local/message-interop/internal/protocol"
	"local/message-interop/internal/store"
)

type Session struct {
	ID         string
	ConsumerID string
	Version    int
	Partition  string
	Wake       chan struct{}
	active     bool
}

type deliveryState struct {
	GlobalOffset   int64
	DeliveryID     string
	ConsumerOffset int64
	Semantics      protocol.Semantics
	Attempts       int
	Acked          bool
	Completed      bool
	LastSession    string
	LastAttemptAt  time.Time
}

type consumerState struct {
	ID              string
	DeclaredVersion int
	Partition       string
	NextOffset      int64
	AckedOffset     int64
	Deliveries      map[int64]*deliveryState
	ByDeliveryID    map[string]*deliveryState
}

type Engine struct {
	mu          sync.Mutex
	store       *store.Store
	messages    []*protocol.StoredMessage
	byOffset    map[int64]*protocol.StoredMessage
	dedup       map[int64]*protocol.StoredMessage
	consumers   map[string]*consumerState
	sessions    map[string]*Session
	activeByCID map[string]string
	sessionSeq  int64
	retryAfter  time.Duration
	scan        store.ScanResult
}

type OffsetConsumer struct {
	ConsumerID      string  `json:"consumer_id"`
	DeclaredVersion int     `json:"declared_version"`
	PartitionKey    string  `json:"partition_key,omitempty"`
	NextOffset      int64   `json:"next_offset"`
	AckedOffset     int64   `json:"acked_offset"`
	Pending         int     `json:"pending"`
	Completed       int     `json:"completed"`
	AckedOffsets    []int64 `json:"acked_offsets"`
}

type OffsetSnapshot struct {
	Version   int                       `json:"version"`
	UpdatedAt string                    `json:"updated_at"`
	Consumers map[string]OffsetConsumer `json:"consumers"`
}

type AckResult struct {
	OK           bool   `json:"ok"`
	Type         string `json:"type"`
	Acked        bool   `json:"acked"`
	AlreadyAcked bool   `json:"already_acked"`
	ConsumerID   string `json:"consumer_id"`
	DeliveryID   string `json:"delivery_id"`
	Offset       int64  `json:"offset"`
}

func Open(dataDir string, retryAfter time.Duration) (*Engine, store.RecoveryReport, error) {
	if retryAfter <= 0 {
		retryAfter = time.Second
	}
	persistentStore, scan, err := store.Open(dataDir)
	if err != nil {
		return nil, store.RecoveryReport{}, err
	}
	engine := &Engine{
		store:       persistentStore,
		byOffset:    make(map[int64]*protocol.StoredMessage),
		dedup:       make(map[int64]*protocol.StoredMessage),
		consumers:   make(map[string]*consumerState),
		sessions:    make(map[string]*Session),
		activeByCID: make(map[string]string),
		retryAfter:  retryAfter,
		scan:        scan,
	}
	if err := engine.recoverMessages(scan.Messages); err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	if err := engine.recoverDeliveries(scan.Deliveries); err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	if err := engine.recoverAcks(scan.Acks); err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	dedupEntries, err := engine.rebuildDedup(scan.Dedup)
	if err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	report, err := engine.buildRecoveryReport(dedupEntries)
	if err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	if err := engine.persistOffsetsLocked(); err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	if err := persistentStore.WriteRecoveryReport(report); err != nil {
		_ = persistentStore.Close()
		return nil, store.RecoveryReport{}, err
	}
	return engine, report, nil
}

func (e *Engine) recoverMessages(rawRecords []json.RawMessage) error {
	for index, raw := range rawRecords {
		var message protocol.StoredMessage
		if err := json.Unmarshal(raw, &message); err != nil {
			return fmt.Errorf("decode message record %d: %w", index+1, err)
		}
		expectedOffset := int64(index + 1)
		if message.GlobalOffset != expectedOffset {
			return fmt.Errorf("message %s has global offset %d, expected %d", message.DeliveryID, message.GlobalOffset, expectedOffset)
		}
		if len(protocol.FieldsForVersion(message.Version)) == 0 || !message.Semantics.Valid() {
			return fmt.Errorf("message %s has invalid version or semantics", message.DeliveryID)
		}
		copyMessage := message
		stored := &copyMessage
		e.messages = append(e.messages, stored)
		e.byOffset[stored.GlobalOffset] = stored
		if stored.Semantics == protocol.ExactlyOnce {
			if stored.IdempotencySeq <= 0 {
				return fmt.Errorf("exactly_once message %s has no idempotency sequence", stored.DeliveryID)
			}
			if previous, exists := e.dedup[stored.IdempotencySeq]; exists {
				return fmt.Errorf("duplicate idempotency sequence %d in message log (%s and %s)", stored.IdempotencySeq, previous.DeliveryID, stored.DeliveryID)
			}
			e.dedup[stored.IdempotencySeq] = stored
		}
	}
	sort.Slice(e.messages, func(i, j int) bool { return e.messages[i].GlobalOffset < e.messages[j].GlobalOffset })
	return nil
}

func (e *Engine) consumerLocked(consumerID string) *consumerState {
	state := e.consumers[consumerID]
	if state == nil {
		state = &consumerState{
			ID:           consumerID,
			NextOffset:   1,
			Deliveries:   make(map[int64]*deliveryState),
			ByDeliveryID: make(map[string]*deliveryState),
		}
		e.consumers[consumerID] = state
	}
	return state
}

func (e *Engine) recoverDeliveries(rawRecords []json.RawMessage) error {
	for index, raw := range rawRecords {
		var record protocol.DeliveryRecord
		if err := json.Unmarshal(raw, &record); err != nil {
			return fmt.Errorf("decode delivery record %d: %w", index+1, err)
		}
		message := e.byOffset[record.GlobalOffset]
		if message == nil {
			return fmt.Errorf("delivery %s references missing message offset %d", record.DeliveryID, record.GlobalOffset)
		}
		if record.ConsumerOffset < 1 {
			return fmt.Errorf("delivery %s has invalid consumer offset %d", record.DeliveryID, record.ConsumerOffset)
		}
		state := e.consumerLocked(record.ConsumerID)
		state.DeclaredVersion = record.ConsumerVersion
		delivery := state.Deliveries[record.GlobalOffset]
		if delivery == nil {
			delivery = &deliveryState{
				GlobalOffset:   record.GlobalOffset,
				DeliveryID:     record.DeliveryID,
				ConsumerOffset: record.ConsumerOffset,
				Semantics:      record.Semantic.Mode,
			}
			state.Deliveries[record.GlobalOffset] = delivery
			if _, exists := state.ByDeliveryID[record.DeliveryID]; !exists {
				state.ByDeliveryID[record.DeliveryID] = delivery
			}
		} else if delivery.ConsumerOffset != record.ConsumerOffset {
			return fmt.Errorf("delivery %s changed consumer offset from %d to %d", record.DeliveryID, delivery.ConsumerOffset, record.ConsumerOffset)
		}
		delivery.Attempts++
		// No session survives a process restart. Clearing this marker makes
		// unacknowledged at-least-once work immediately eligible on the first
		// subscription after recovery, independent of the retry interval.
		delivery.LastSession = ""
		delivery.LastAttemptAt = parseTimeOrZero(record.DeliveredAt)
		if record.Semantic.Mode == protocol.AtMostOnce {
			delivery.Completed = true
		}
		if state.NextOffset <= record.ConsumerOffset {
			state.NextOffset = record.ConsumerOffset + 1
		}
		if delivery.Completed && state.AckedOffset < delivery.ConsumerOffset {
			state.AckedOffset = delivery.ConsumerOffset
		}
	}
	return nil
}

func (e *Engine) recoverAcks(rawRecords []json.RawMessage) error {
	for index, raw := range rawRecords {
		var record protocol.AckRecord
		if err := json.Unmarshal(raw, &record); err != nil {
			return fmt.Errorf("decode ack record %d: %w", index+1, err)
		}
		state := e.consumers[record.ConsumerID]
		if state == nil {
			return fmt.Errorf("ack references unknown consumer %s", record.ConsumerID)
		}
		delivery := state.ByDeliveryID[record.DeliveryID]
		if delivery == nil {
			return fmt.Errorf("ack references unknown delivery %s for consumer %s", record.DeliveryID, record.ConsumerID)
		}
		if delivery.ConsumerOffset != record.ConsumerOffset {
			return fmt.Errorf("ack offset %d does not match delivery offset %d for %s", record.ConsumerOffset, delivery.ConsumerOffset, record.DeliveryID)
		}
		delivery.Acked = true
		delivery.Completed = true
		if state.AckedOffset < record.ConsumerOffset {
			state.AckedOffset = record.ConsumerOffset
		}
	}
	return nil
}

func (e *Engine) rebuildDedup(rawRecords []json.RawMessage) (int, error) {
	existing := make(map[int64]bool)
	for index, raw := range rawRecords {
		var record protocol.DedupRecord
		if err := json.Unmarshal(raw, &record); err != nil {
			return 0, fmt.Errorf("decode dedup record %d: %w", index+1, err)
		}
		if record.IdempotencySeq <= 0 {
			return 0, fmt.Errorf("dedup index contains invalid sequence %d", record.IdempotencySeq)
		}
		existing[record.IdempotencySeq] = true
	}
	for seq, message := range e.dedup {
		if existing[seq] {
			continue
		}
		_, err := e.store.Append(store.Dedup, protocol.DedupRecord{
			IdempotencySeq: seq,
			DeliveryID:     message.DeliveryID,
			GlobalOffset:   message.GlobalOffset,
			Semantics:      message.Semantics,
		})
		if err != nil {
			return 0, err
		}
	}
	return len(e.dedup), nil
}

func parseTimeOrZero(value string) time.Time {
	parsed, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		return time.Time{}
	}
	return parsed
}

func (e *Engine) buildRecoveryReport(dedupEntries int) (store.RecoveryReport, error) {
	snapshot := e.snapshotLocked()
	consumerStates, err := json.Marshal(snapshot.Consumers)
	if err != nil {
		return store.RecoveryReport{}, err
	}
	offsets := make(map[string]int64, len(e.consumers))
	for consumerID, state := range e.consumers {
		offsets[consumerID] = state.AckedOffset
	}
	tailAction := "none"
	if len(e.scan.TailIssues) > 0 {
		tailAction = "truncated_incomplete_tail"
	}
	var lastOffset int64
	if len(e.messages) > 0 {
		lastOffset = e.messages[len(e.messages)-1].GlobalOffset
	}
	return store.RecoveryReport{
		Version:             1,
		GeneratedAt:         store.Now(),
		DataDir:             e.store.Dir(),
		RecoveredOffsets:    offsets,
		RebuiltDedupEntries: dedupEntries,
		RecoveredMessages:   len(e.messages),
		RecoveredDeliveries: len(e.scan.Deliveries),
		RecoveredAcks:       len(e.scan.Acks),
		LastMessageOffset:   lastOffset,
		TailAction:          tailAction,
		TailIssues:          e.scan.TailIssues,
		TruncatedBytes:      e.scan.TruncatedBytes,
		ConsumerStates:      consumerStates,
	}, nil
}

func (e *Engine) Publish(message protocol.Message, semantics protocol.Semantics) (protocol.PublishResult, *protocol.APIError) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !semantics.Valid() {
		return protocol.PublishResult{}, protocol.NewError(protocol.CodeSemantic, "invalid delivery semantics")
	}
	if semantics == protocol.ExactlyOnce {
		if message.Version < 3 || message.IdempotencySeq <= 0 {
			return protocol.PublishResult{}, protocol.NewError(protocol.CodeSemantic, "exactly_once requires v3 idempotency_seq")
		}
		if existing := e.dedup[message.IdempotencySeq]; existing != nil {
			return protocol.PublishResult{
				OK:             true,
				Type:           "publish_ack",
				Accepted:       false,
				Duplicate:      true,
				DeliveryID:     existing.DeliveryID,
				GlobalOffset:   existing.GlobalOffset,
				SourceVersion:  existing.Version,
				Semantics:      semantics,
				IdempotencySeq: existing.IdempotencySeq,
				DedupKey:       dedupKey(existing.IdempotencySeq),
				ExistingResult: &protocol.ExistingResult{
					DeliveryID:   existing.DeliveryID,
					GlobalOffset: existing.GlobalOffset,
					AcceptedAt:   existing.AcceptedAt,
				},
			}, nil
		}
	}
	stored := &protocol.StoredMessage{
		Version:        message.Version,
		DeliveryID:     message.DeliveryID,
		Body:           append(json.RawMessage(nil), message.Body...),
		TimestampMS:    message.TimestampMS,
		RetryCount:     message.RetryCount,
		PartitionKey:   message.PartitionKey,
		IdempotencySeq: message.IdempotencySeq,
		Semantics:      semantics,
		AcceptedAt:     store.Now(),
	}
	if stored.PartitionKey == "" {
		stored.PartitionKey = protocol.DefaultPartition
	}
	if _, err := e.store.AppendMessage(stored); err != nil {
		return protocol.PublishResult{}, internalError(err)
	}
	e.messages = append(e.messages, stored)
	e.byOffset[stored.GlobalOffset] = stored
	if semantics == protocol.ExactlyOnce {
		e.dedup[stored.IdempotencySeq] = stored
		if _, err := e.store.Append(store.Dedup, protocol.DedupRecord{
			IdempotencySeq: stored.IdempotencySeq,
			DeliveryID:     stored.DeliveryID,
			GlobalOffset:   stored.GlobalOffset,
			Semantics:      stored.Semantics,
		}); err != nil {
			return protocol.PublishResult{}, internalError(err)
		}
	}
	e.wakeAllLocked()
	result := protocol.PublishResult{
		OK:            true,
		Type:          "publish_ack",
		Accepted:      true,
		Duplicate:     false,
		DeliveryID:    stored.DeliveryID,
		GlobalOffset:  stored.GlobalOffset,
		SourceVersion: stored.Version,
		Semantics:     stored.Semantics,
	}
	if semantics == protocol.ExactlyOnce {
		result.IdempotencySeq = stored.IdempotencySeq
		result.DedupKey = dedupKey(stored.IdempotencySeq)
	}
	return result, nil
}

func (e *Engine) Subscribe(request protocol.SubscribeRequest) (*Session, *protocol.APIError) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, exists := e.activeByCID[request.ConsumerID]; exists {
		err := protocol.NewError(protocol.CodeConflict, "consumer already has an active subscription")
		err.Details = map[string]any{"consumer_id": request.ConsumerID}
		return nil, err
	}
	e.sessionSeq++
	session := &Session{
		ID:         fmt.Sprintf("sess-%06d", e.sessionSeq),
		ConsumerID: request.ConsumerID,
		Version:    request.Version,
		Partition:  request.PartitionKey,
		Wake:       make(chan struct{}, 1),
		active:     true,
	}
	state := e.consumerLocked(request.ConsumerID)
	state.DeclaredVersion = request.Version
	state.Partition = request.PartitionKey
	e.sessions[session.ID] = session
	e.activeByCID[request.ConsumerID] = session.ID
	if err := e.persistOffsetsLocked(); err != nil {
		delete(e.sessions, session.ID)
		delete(e.activeByCID, request.ConsumerID)
		return nil, internalError(err)
	}
	return session, nil
}

func (e *Engine) CloseSession(sessionID string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	session := e.sessions[sessionID]
	if session == nil || !session.active {
		return
	}
	session.active = false
	delete(e.sessions, sessionID)
	if e.activeByCID[session.ConsumerID] == sessionID {
		delete(e.activeByCID, session.ConsumerID)
	}
	close(session.Wake)
}

func (e *Engine) wakeAllLocked() {
	for _, session := range e.sessions {
		if !session.active {
			continue
		}
		select {
		case session.Wake <- struct{}{}:
		default:
		}
	}
}

func (e *Engine) NextDeliveries(sessionID string) ([]protocol.Delivery, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	session := e.sessions[sessionID]
	if session == nil || !session.active {
		return nil, fmt.Errorf("subscription %s is not active", sessionID)
	}
	state := e.consumerLocked(session.ConsumerID)
	now := time.Now()
	deliveries := make([]protocol.Delivery, 0)
	for _, message := range e.messages {
		if session.Partition != "" && message.PartitionKey != session.Partition {
			continue
		}
		existing := state.Deliveries[message.GlobalOffset]
		if existing != nil && existing.Completed {
			continue
		}
		if existing != nil {
			if existing.LastSession == sessionID {
				canRetry := message.Semantics == protocol.AtLeastOnce && now.Sub(existing.LastAttemptAt) >= e.retryAfter
				if !canRetry {
					continue
				}
			}
			if message.Semantics == protocol.AtMostOnce {
				continue
			}
		}
		delivery, err := e.makeDeliveryLocked(session, state, message, existing, now)
		if err != nil {
			return deliveries, err
		}
		deliveries = append(deliveries, delivery)
	}
	return deliveries, nil
}

func (e *Engine) makeDeliveryLocked(session *Session, state *consumerState, message *protocol.StoredMessage, existing *deliveryState, now time.Time) (protocol.Delivery, error) {
	attempt := 1
	consumerOffset := int64(0)
	if existing != nil {
		attempt = existing.Attempts + 1
		consumerOffset = existing.ConsumerOffset
	} else {
		consumerOffset = state.NextOffset
		state.NextOffset++
	}
	retryCount := message.RetryCount
	if message.Semantics == protocol.AtLeastOnce {
		retryCount = message.RetryCount + attempt - 1
	}
	conversionID := fmt.Sprintf("conv-%06d-%08x-%02d", message.GlobalOffset, crc32String(state.ID), attempt)
	payload, conversion := protocol.BuildCompatiblePayload(*message, session.Version, retryCount, conversionID)
	payloadBytes, err := protocol.MarshalPayload(payload)
	if err != nil {
		return protocol.Delivery{}, err
	}
	semantic := semanticDecision(message, attempt)
	record := protocol.DeliveryRecord{
		DeliveryID:       message.DeliveryID,
		SourceDeliveryID: message.DeliveryID,
		ConsumerID:       state.ID,
		SessionID:        session.ID,
		ConsumerVersion:  session.Version,
		ConsumerOffset:   consumerOffset,
		GlobalOffset:     message.GlobalOffset,
		Attempt:          attempt,
		Payload:          payloadBytes,
		Conversion:       conversion,
		Semantic:         semantic,
		DeliveredAt:      store.Now(),
	}
	eventSeq, err := e.store.Append(store.Deliveries, record)
	if err != nil {
		return protocol.Delivery{}, err
	}
	record.EventID = eventSeq
	if existing == nil {
		existing = &deliveryState{
			GlobalOffset:   message.GlobalOffset,
			DeliveryID:     message.DeliveryID,
			ConsumerOffset: consumerOffset,
			Semantics:      message.Semantics,
		}
		state.Deliveries[message.GlobalOffset] = existing
		state.ByDeliveryID[message.DeliveryID] = existing
	}
	existing.Attempts = attempt
	existing.LastSession = session.ID
	existing.LastAttemptAt = now
	if message.Semantics == protocol.AtMostOnce {
		existing.Completed = true
		if state.AckedOffset < consumerOffset {
			state.AckedOffset = consumerOffset
		}
	}
	if err := e.persistOffsetsLocked(); err != nil {
		return protocol.Delivery{}, err
	}
	return protocol.Delivery{
		Type:             "delivery",
		DeliveryID:       message.DeliveryID,
		SourceDeliveryID: message.DeliveryID,
		ConsumerID:       state.ID,
		ConsumerVersion:  session.Version,
		ConsumerOffset:   consumerOffset,
		GlobalOffset:     message.GlobalOffset,
		Attempt:          attempt,
		Payload:          payload,
		Conversion:       conversion,
		Semantic:         semantic,
	}, nil
}

func semanticDecision(message *protocol.StoredMessage, attempt int) protocol.SemanticDecision {
	decision := protocol.SemanticDecision{
		Mode:       message.Semantics,
		RetryCount: message.RetryCount + attempt - 1,
	}
	switch message.Semantics {
	case protocol.AtMostOnce:
		decision.Decision = "deliver_once"
		decision.Reason = "at_most_once completes at dispatch and is never redelivered"
		decision.RetryCount = message.RetryCount
	case protocol.AtLeastOnce:
		decision.RetryCount = message.RetryCount + attempt - 1
		if attempt == 1 {
			decision.Decision = "deliver"
			decision.Reason = "initial at_least_once delivery"
		} else {
			decision.Decision = "redeliver_unacked"
			decision.Reason = "previous delivery was not acknowledged"
		}
	case protocol.ExactlyOnce:
		decision.Decision = "deliver_exactly_once"
		decision.Reason = "idempotency sequence deduplicated before delivery"
		if attempt > 1 {
			decision.Decision = "redeliver_exactly_once_same_result"
			decision.Reason = "unacknowledged existing result is redelivered with the same idempotency key"
		}
		decision.IdempotencySeq = message.IdempotencySeq
		decision.DedupKey = dedupKey(message.IdempotencySeq)
		decision.RetryCount = message.RetryCount
	}
	return decision
}

func (e *Engine) Ack(sessionID string, request protocol.AckRequest) (AckResult, *protocol.APIError) {
	e.mu.Lock()
	defer e.mu.Unlock()
	session := e.sessions[sessionID]
	if session == nil || !session.active {
		return AckResult{}, protocol.NewError(protocol.CodeProtocol, "subscription is not active")
	}
	if request.ConsumerID != session.ConsumerID {
		return AckResult{}, protocol.NewError(protocol.CodeConflict, "ack consumer_id does not match subscription")
	}
	state := e.consumers[request.ConsumerID]
	if state == nil {
		return AckResult{}, protocol.NewError(protocol.CodeNotFound, "unknown consumer")
	}
	delivery := state.ByDeliveryID[request.DeliveryID]
	if delivery == nil {
		return AckResult{}, protocol.NewError(protocol.CodeNotFound, "unknown delivery_id for this consumer")
	}
	if delivery.ConsumerOffset != request.Offset {
		err := protocol.NewError(protocol.CodeConflict, "ack offset does not match the delivered offset")
		err.Details = map[string]any{"expected_offset": delivery.ConsumerOffset, "got": request.Offset}
		return AckResult{}, err
	}
	if delivery.Acked {
		return AckResult{
			OK:           true,
			Type:         "ack_ack",
			Acked:        false,
			AlreadyAcked: true,
			ConsumerID:   request.ConsumerID,
			DeliveryID:   request.DeliveryID,
			Offset:       request.Offset,
		}, nil
	}
	if len(e.messages) > 0 {
		message := e.byOffset[delivery.GlobalOffset]
		ackRecord := protocol.AckRecord{
			ConsumerID:     request.ConsumerID,
			DeliveryID:     request.DeliveryID,
			GlobalOffset:   delivery.GlobalOffset,
			ConsumerOffset: delivery.ConsumerOffset,
			SessionID:      sessionID,
			Semantics:      delivery.Semantics,
			AckedAt:        store.Now(),
		}
		if message != nil {
			ackRecord.Semantics = message.Semantics
		}
		if _, err := e.store.Append(store.Acks, ackRecord); err != nil {
			return AckResult{}, internalError(err)
		}
	}
	delivery.Acked = true
	delivery.Completed = true
	if state.AckedOffset < delivery.ConsumerOffset {
		state.AckedOffset = delivery.ConsumerOffset
	}
	if err := e.persistOffsetsLocked(); err != nil {
		return AckResult{}, internalError(err)
	}
	e.wakeSessionLocked(session)
	return AckResult{
		OK:           true,
		Type:         "ack_ack",
		Acked:        true,
		AlreadyAcked: false,
		ConsumerID:   request.ConsumerID,
		DeliveryID:   request.DeliveryID,
		Offset:       request.Offset,
	}, nil
}

func (e *Engine) wakeSessionLocked(session *Session) {
	if session == nil || !session.active {
		return
	}
	select {
	case session.Wake <- struct{}{}:
	default:
	}
}

func (e *Engine) snapshotLocked() OffsetSnapshot {
	consumers := make(map[string]OffsetConsumer, len(e.consumers))
	for consumerID, state := range e.consumers {
		pending := 0
		completed := 0
		ackedOffsets := make([]int64, 0)
		for _, message := range e.messages {
			if state.Partition != "" && message.PartitionKey != state.Partition {
				continue
			}
			delivery := state.Deliveries[message.GlobalOffset]
			if delivery == nil || !delivery.Completed {
				pending++
			}
			if delivery != nil && delivery.Completed {
				completed++
			}
		}
		for _, delivery := range state.Deliveries {
			if delivery.Acked {
				ackedOffsets = append(ackedOffsets, delivery.ConsumerOffset)
			}
		}
		sort.Slice(ackedOffsets, func(i, j int) bool { return ackedOffsets[i] < ackedOffsets[j] })
		consumers[consumerID] = OffsetConsumer{
			ConsumerID:      consumerID,
			DeclaredVersion: state.DeclaredVersion,
			PartitionKey:    state.Partition,
			NextOffset:      state.NextOffset,
			AckedOffset:     state.AckedOffset,
			Pending:         pending,
			Completed:       completed,
			AckedOffsets:    ackedOffsets,
		}
	}
	return OffsetSnapshot{
		Version:   1,
		UpdatedAt: store.Now(),
		Consumers: consumers,
	}
}

func (e *Engine) persistOffsetsLocked() error {
	return e.store.WriteJSONAtomic("consumer-offsets.json", e.snapshotLocked())
}

func (e *Engine) Snapshot() OffsetSnapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.snapshotLocked()
}

func (e *Engine) Messages() []protocol.StoredMessage {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]protocol.StoredMessage, len(e.messages))
	for index, message := range e.messages {
		out[index] = *message
	}
	return out
}

func (e *Engine) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	for _, session := range e.sessions {
		if session.active {
			session.active = false
			close(session.Wake)
		}
	}
	e.sessions = make(map[string]*Session)
	e.activeByCID = make(map[string]string)
	_ = e.persistOffsetsLocked()
	return e.store.Close()
}

func dedupKey(seq int64) string { return fmt.Sprintf("idem:%d", seq) }

func crc32String(value string) uint32 {
	var hash uint32 = 2166136261
	for index := 0; index < len(value); index++ {
		hash ^= uint32(value[index])
		hash *= 16777619
	}
	return hash
}

func internalError(err error) *protocol.APIError {
	apiErr := protocol.NewError(protocol.CodeInternal, "persistence operation failed")
	apiErr.Details = map[string]any{"cause": err.Error()}
	return apiErr
}
