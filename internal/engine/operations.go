package engine

import (
	"context"
	"sort"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/store"
)

func (e *Engine) Append(ctx context.Context, requestID, partitionKey, producerSession string, producerSeq int64, body string) (AppendResult, *protocol.BizError) {
	if err := e.assertContext(ctx); err != nil {
		return AppendResult{}, protocol.AsBizError(err)
	}
	if err := validatePartitionKey(partitionKey); err != nil {
		e.emit("append_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "code": err.Code, "field": err.Fields["field"]})
		return AppendResult{}, err
	}
	if err := validateSession("producer_session", producerSession); err != nil {
		e.emit("append_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "code": err.Code, "field": err.Fields["field"]})
		return AppendResult{}, err
	}
	if producerSeq < 1 {
		err := protocol.NewError(protocol.CodeAppend, "producer sequence must be positive").With("field", "producer_seq").With("partition_key", partitionKey)
		e.emit("append_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "code": err.Code, "field": "producer_seq"})
		return AppendResult{}, err
	}
	if len([]byte(body)) > e.maxBodyBytes {
		err := protocol.NewError(protocol.CodeAppend, "message body exceeds the configured limit").
			With("field", "body").
			With("partition_key", partitionKey).
			With("body_bytes", len([]byte(body))).
			With("max_body_bytes", e.maxBodyBytes)
		e.emit("append_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "code": err.Code, "field": "body"})
		return AppendResult{}, err
	}

	producer := e.producerState(producerSession)
	producer.mu.Lock()
	defer producer.mu.Unlock()
	if producerSeq <= producer.lastSeq {
		err := protocol.NewError(protocol.CodeAppend, "producer sequence is not strictly increasing").
			With("field", "producer_seq").
			With("partition_key", partitionKey).
			With("producer_session", producerSession).
			With("last_sequence", producer.lastSeq).
			With("submitted_sequence", producerSeq)
		e.emit("append_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "producer_session": producerSession, "code": err.Code, "field": "producer_seq", "last_sequence": producer.lastSeq, "submitted_sequence": producerSeq})
		return AppendResult{}, err
	}

	partition := e.getOrCreatePartition(partitionKey)
	partition.mu.Lock()
	defer partition.mu.Unlock()
	state := partition.state
	next := state.clone()
	offset := next.NextOffset
	next.NextOffset++
	e.revision(&next)
	now := e.clock.Now()
	event := store.EventRow{
		PartitionKey:  partitionKey,
		Kind:          "append",
		Session:       producerSession,
		Offset:        offset,
		StartOffset:   offset,
		EndOffset:     offset,
		Code:          protocol.CodeOK,
		Detail:        jsonDetail(map[string]any{"producer_seq": producerSeq, "body_bytes": len([]byte(body))}),
		CreatedUnixMS: now.UnixMilli(),
	}
	tx, err := e.store.DB().BeginTx(ctx, nil)
	if err != nil {
		return AppendResult{}, protocol.NewError(protocol.CodeInternal, "begin append transaction: %v", err)
	}
	defer tx.Rollback()
	if err := e.store.InsertMessageTx(ctx, tx, store.MessageRow{
		PartitionKey:    partitionKey,
		Offset:          offset,
		ProducerSession: producerSession,
		ProducerSeq:     producerSeq,
		Body:            body,
		AppendedUnixMS:  now.UnixMilli(),
	}); err != nil {
		e.emit("append_failed", map[string]any{"request_id": requestID, "partition_key": partitionKey, "code": protocol.CodeInternal, "error": err.Error()})
		return AppendResult{}, protocol.NewError(protocol.CodeInternal, "persist message: %v", err)
	}
	if err := e.store.UpsertPartitionTx(ctx, tx, next.row()); err != nil {
		return AppendResult{}, protocol.NewError(protocol.CodeInternal, "persist partition append offset: %v", err)
	}
	if err := e.store.UpsertProducerSessionTx(ctx, tx, producerSession, producerSeq); err != nil {
		return AppendResult{}, protocol.NewError(protocol.CodeInternal, "persist producer sequence: %v", err)
	}
	eventID, err := e.store.InsertEventTx(ctx, tx, event)
	if err != nil {
		return AppendResult{}, protocol.NewError(protocol.CodeInternal, "persist append event: %v", err)
	}
	if err := tx.Commit(); err != nil {
		return AppendResult{}, protocol.NewError(protocol.CodeInternal, "commit append transaction: %v", err)
	}
	partition.state = next
	producer.lastSeq = producerSeq
	e.emit("append_committed", map[string]any{
		"request_id":       requestID,
		"partition_key":    partitionKey,
		"producer_session": producerSession,
		"producer_seq":     producerSeq,
		"offset":           offset,
		"next_offset":      next.NextOffset,
		"event_id":         eventID,
		"durable":          true,
	})
	return AppendResult{PartitionKey: partitionKey, Offset: offset, NextOffset: next.NextOffset}, nil
}

func (e *Engine) Pull(ctx context.Context, requestID, partitionKey, consumerSession string, limit int, timeoutMS int64) (PullResult, *protocol.BizError) {
	if err := e.assertContext(ctx); err != nil {
		return PullResult{}, protocol.AsBizError(err)
	}
	if err := validatePartitionKey(partitionKey); err != nil {
		return PullResult{}, err
	}
	if err := validateSession("consumer_session", consumerSession); err != nil {
		return PullResult{}, err
	}
	if limit <= 0 || limit > e.maxBatch {
		return PullResult{}, protocol.NewError(protocol.CodeProtocol, "pull limit is outside the allowed range").
			With("partition_key", partitionKey).
			With("limit", limit).
			With("max_batch", e.maxBatch)
	}
	ttl, err := e.normalizeTimeout(timeoutMS)
	if err != nil {
		return PullResult{}, err.With("partition_key", partitionKey)
	}
	partition := e.getPartition(partitionKey)
	if partition == nil {
		return PullResult{PartitionKey: partitionKey, Session: consumerSession, RetryCounts: map[string]int{}}, nil
	}
	partition.mu.Lock()
	defer partition.mu.Unlock()
	now := e.clock.Now()
	expired, err := e.expireIfNeededLocked(ctx, partition, "pull", now)
	if err != nil {
		return PullResult{}, err
	}
	if expired {
		now = e.clock.Now()
	}
	if partition.state.Blocked {
		e.emit("pull_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "session": consumerSession, "code": protocol.CodePoison, "blocked_offset": partition.state.BlockedOffset})
		return PullResult{}, poisonError(partition.state, "partition is blocked by a poison offset")
	}
	if partition.state.Window != nil && partition.state.Window.Recovered {
		blocked, blockedOffset, blockedCount, err := e.applyRecoveryRedeliveryLocked(ctx, partition, requestID)
		if err != nil {
			return PullResult{}, err
		}
		if blocked {
			e.emit("pull_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "session": consumerSession, "code": protocol.CodePoison, "blocked_offset": blockedOffset, "blocked_retry_count": blockedCount})
			return PullResult{}, poisonError(partition.state, "recovered window exceeded the retry limit")
		}
		now = e.clock.Now()
	}
	if partition.state.Window != nil {
		holder := partition.state.Window.Holder
		e.emit("pull_fenced", map[string]any{
			"request_id":    requestID,
			"partition_key": partitionKey,
			"session":       consumerSession,
			"holder":        holder,
			"window_start":  partition.state.Window.StartOffset,
			"window_end":    partition.state.Window.EndOffset,
			"code":          protocol.CodeFence,
		})
		return PullResult{}, protocol.NewError(protocol.CodeFence, "partition already has an active delivery window").
			With("partition_key", partitionKey).
			With("holder", holder).
			With("window_start", partition.state.Window.StartOffset).
			With("window_end", partition.state.Window.EndOffset)
	}
	if partition.state.Blocked {
		return PullResult{}, poisonError(partition.state, "partition is blocked by a poison offset")
	}
	start := partition.state.CommittedOffset + 1
	if start >= partition.state.NextOffset {
		return PullResult{
			PartitionKey: partitionKey,
			Session:      consumerSession,
			RetryCounts:  retryCountsView(partition.state.RetryCounts),
		}, nil
	}
	end := minInt64(partition.state.NextOffset-1, start+int64(limit)-1)
	messages, loadErr := e.store.LoadMessagesRange(ctx, partitionKey, start, end)
	if loadErr != nil {
		return PullResult{}, protocol.NewError(protocol.CodeInternal, "load delivery window: %v", loadErr)
	}
	if int64(len(messages)) != end-start+1 {
		return PullResult{}, protocol.NewError(protocol.CodeLedger, "delivery window has a message-log gap").
			With("partition_key", partitionKey).
			With("window_start", start).
			With("window_end", end).
			With("loaded_messages", len(messages))
	}
	next := partition.state.clone()
	next.Window = &deliveryWindow{
		Holder:      consumerSession,
		StartOffset: start,
		EndOffset:   end,
		Deadline:    now.Add(ttl),
		IssuedAt:    now,
		TTL:         ttl,
	}
	next.PendingRedelivery = false
	next.RedeliveryFrom = 0
	e.revision(&next)
	event := store.EventRow{
		PartitionKey:  partitionKey,
		Kind:          "window_issued",
		Session:       consumerSession,
		StartOffset:   start,
		EndOffset:     end,
		Code:          protocol.CodeOK,
		Detail:        jsonDetail(map[string]any{"ttl_ms": ttl.Milliseconds(), "limit": limit}),
		CreatedUnixMS: now.UnixMilli(),
	}
	tx, txErr := e.store.DB().BeginTx(ctx, nil)
	if txErr != nil {
		return PullResult{}, protocol.NewError(protocol.CodeInternal, "begin pull transaction: %v", txErr)
	}
	defer tx.Rollback()
	if err := e.store.UpsertPartitionTx(ctx, tx, next.row()); err != nil {
		return PullResult{}, protocol.NewError(protocol.CodeInternal, "persist delivery window: %v", err)
	}
	eventID, eventErr := e.store.InsertEventTx(ctx, tx, event)
	if eventErr != nil {
		return PullResult{}, protocol.NewError(protocol.CodeInternal, "persist window event: %v", eventErr)
	}
	if err := tx.Commit(); err != nil {
		return PullResult{}, protocol.NewError(protocol.CodeInternal, "commit pull transaction: %v", err)
	}
	partition.state = next
	views := make([]protocol.MessageView, 0, len(messages))
	for _, message := range messages {
		views = append(views, protocol.MessageView{
			PartitionKey:    message.PartitionKey,
			Offset:          message.Offset,
			ProducerSession: message.ProducerSession,
			ProducerSeq:     message.ProducerSeq,
			Body:            message.Body,
			AppendedUnixMS:  message.AppendedUnixMS,
			RetryCount:      next.RetryCounts[message.Offset],
		})
	}
	window := &protocol.WindowInfo{
		Holder:         consumerSession,
		StartOffset:    start,
		EndOffset:      end,
		DeadlineUnixMS: next.Window.Deadline.UnixMilli(),
		TTLMS:          ttl.Milliseconds(),
	}
	e.emit("window_issued", map[string]any{
		"request_id":       requestID,
		"partition_key":    partitionKey,
		"session":          consumerSession,
		"window_start":     start,
		"window_end":       end,
		"deadline_mono_ns": e.nowMono(next.Window.Deadline),
		"now_mono_ns":      e.nowMono(now),
		"deadline_unix_ms": next.Window.Deadline.UnixMilli(),
		"event_id":         eventID,
		"retry_counts":     retryCountsView(next.RetryCounts),
	})
	return PullResult{
		PartitionKey: partitionKey,
		Session:      consumerSession,
		Window:       window,
		Messages:     views,
		RetryCounts:  retryCountsView(next.RetryCounts),
	}, nil
}

func (e *Engine) Commit(ctx context.Context, requestID, partitionKey, consumerSession string, offset int64) (CommitResult, *protocol.BizError) {
	if err := e.assertContext(ctx); err != nil {
		return CommitResult{}, protocol.AsBizError(err)
	}
	if err := validatePartitionKey(partitionKey); err != nil {
		return CommitResult{}, err
	}
	if err := validateSession("consumer_session", consumerSession); err != nil {
		return CommitResult{}, err
	}
	if offset < 1 {
		return CommitResult{}, protocol.NewError(protocol.CodeCommit, "commit offset must be positive").
			With("partition_key", partitionKey).
			With("offset", offset)
	}
	partition := e.getPartition(partitionKey)
	if partition == nil {
		return CommitResult{}, protocol.NewError(protocol.CodeCommit, "partition has no delivery window").
			With("partition_key", partitionKey).
			With("offset", offset)
	}
	partition.mu.Lock()
	defer partition.mu.Unlock()
	now := e.clock.Now()
	state := partition.state
	if state.Window != nil && !state.Window.Recovered {
		if !now.Before(state.Window.Deadline) {
			timeoutDeadline := state.Window.Deadline
			if _, err := e.expireIfNeededLocked(ctx, partition, "commit", now); err != nil {
				return CommitResult{}, err
			}
			e.emit("commit_rejected", map[string]any{
				"request_id":       requestID,
				"partition_key":    partitionKey,
				"session":          consumerSession,
				"offset":           offset,
				"code":             protocol.CodeFence,
				"decision":         "timeout_won",
				"basis":            "partition_mutex; now_mono_ns >= deadline_mono_ns",
				"now_mono_ns":      e.nowMono(now),
				"deadline_mono_ns": e.nowMono(timeoutDeadline),
				"first_event":      "window_timeout",
			})
			err := protocol.NewError(protocol.CodeFence, "delivery window expired before commit; timeout event won").
				With("partition_key", partitionKey).
				With("holder", consumerSession).
				With("timed_out", true)
			if partition.state.Blocked {
				return CommitResult{}, poisonError(partition.state, "delivery window timed out and the partition became poisoned")
			}
			return CommitResult{}, err
		}
	}
	state = partition.state
	if state.Blocked {
		return CommitResult{}, poisonError(state, "partition is blocked by a poison offset")
	}
	if state.Window == nil {
		return CommitResult{}, protocol.NewError(protocol.CodeCommit, "partition has no active delivery window").
			With("partition_key", partitionKey).
			With("offset", offset)
	}
	if state.Window.Recovered {
		return CommitResult{}, protocol.NewError(protocol.CodeFence, "recovered window must be redelivered before commit").
			With("partition_key", partitionKey).
			With("holder", state.Window.Holder)
	}
	if state.Window.Holder != consumerSession {
		e.emit("commit_rejected", map[string]any{"request_id": requestID, "partition_key": partitionKey, "session": consumerSession, "holder": state.Window.Holder, "code": protocol.CodeFence, "offset": offset})
		return CommitResult{}, protocol.NewError(protocol.CodeFence, "commit session does not hold the delivery window").
			With("partition_key", partitionKey).
			With("holder", state.Window.Holder)
	}
	if offset <= state.CommittedOffset {
		return CommitResult{}, protocol.NewError(protocol.CodeCommit, "commit offset moves backwards").
			With("partition_key", partitionKey).
			With("offset", offset).
			With("committed_offset", state.CommittedOffset)
	}
	if offset < state.Window.StartOffset || offset > state.Window.EndOffset {
		return CommitResult{}, protocol.NewError(protocol.CodeCommit, "commit offset is outside the active delivery window").
			With("partition_key", partitionKey).
			With("offset", offset).
			With("window_start", state.Window.StartOffset).
			With("window_end", state.Window.EndOffset)
	}
	next := state.clone()
	windowStart := next.Window.StartOffset
	windowEnd := next.Window.EndOffset
	before := next.CommittedOffset
	next.CommittedOffset = offset
	next.ConfirmedOffset = offset
	next.PendingRedelivery = false
	next.RedeliveryFrom = 0
	if offset == windowEnd {
		next.Window = nil
	} else {
		next.Window.StartOffset = offset + 1
	}
	e.revision(&next)
	event := store.EventRow{
		PartitionKey:    partitionKey,
		Kind:            "commit",
		Session:         consumerSession,
		Offset:          offset,
		StartOffset:     windowStart,
		EndOffset:       windowEnd,
		CommittedBefore: before,
		CommittedAfter:  offset,
		Code:            protocol.CodeOK,
		CreatedUnixMS:   now.UnixMilli(),
	}
	tx, err := e.store.DB().BeginTx(ctx, nil)
	if err != nil {
		return CommitResult{}, protocol.NewError(protocol.CodeInternal, "begin commit transaction: %v", err)
	}
	defer tx.Rollback()
	if err := e.store.UpsertPartitionTx(ctx, tx, next.row()); err != nil {
		return CommitResult{}, protocol.NewError(protocol.CodeInternal, "persist committed ledger: %v", err)
	}
	eventID, err := e.store.InsertEventTx(ctx, tx, event)
	if err != nil {
		return CommitResult{}, protocol.NewError(protocol.CodeInternal, "persist commit event: %v", err)
	}
	if err := tx.Commit(); err != nil {
		return CommitResult{}, protocol.NewError(protocol.CodeInternal, "commit ledger transaction: %v", err)
	}
	partition.state = next
	result := CommitResult{PartitionKey: partitionKey, CommittedOffset: next.CommittedOffset, ConfirmedOffset: next.ConfirmedOffset}
	if next.Window != nil {
		result.Window = &protocol.WindowInfo{
			Holder:         next.Window.Holder,
			StartOffset:    next.Window.StartOffset,
			EndOffset:      next.Window.EndOffset,
			DeadlineUnixMS: next.Window.Deadline.UnixMilli(),
			TTLMS:          next.Window.TTL.Milliseconds(),
		}
	}
	e.emit("commit_committed", map[string]any{
		"request_id":       requestID,
		"partition_key":    partitionKey,
		"session":          consumerSession,
		"offset":           offset,
		"committed_before": before,
		"committed_after":  next.CommittedOffset,
		"window_released":  next.Window == nil,
		"event_id":         eventID,
		"decision":         "commit_won",
		"basis":            "partition_mutex; now_mono_ns < deadline_mono_ns",
		"now_mono_ns":      e.nowMono(now),
	})
	return result, nil
}

func (e *Engine) Fail(ctx context.Context, requestID, partitionKey, consumerSession string, offset int64, reason string) (FailResult, *protocol.BizError) {
	if err := e.assertContext(ctx); err != nil {
		return FailResult{}, protocol.AsBizError(err)
	}
	if err := validatePartitionKey(partitionKey); err != nil {
		return FailResult{}, err
	}
	if err := validateSession("consumer_session", consumerSession); err != nil {
		return FailResult{}, err
	}
	partition := e.getPartition(partitionKey)
	if partition == nil {
		return FailResult{}, protocol.NewError(protocol.CodeCommit, "partition has no active delivery window").With("partition_key", partitionKey)
	}
	partition.mu.Lock()
	defer partition.mu.Unlock()
	now := e.clock.Now()
	if _, err := e.expireIfNeededLocked(ctx, partition, "fail", now); err != nil {
		return FailResult{}, err
	}
	state := partition.state
	if state.Blocked {
		return FailResult{}, poisonError(state, "partition is blocked by a poison offset")
	}
	if state.Window == nil {
		return FailResult{}, protocol.NewError(protocol.CodeCommit, "partition has no active delivery window").With("partition_key", partitionKey)
	}
	if state.Window.Recovered {
		return FailResult{}, protocol.NewError(protocol.CodeFence, "recovered window must be pulled before failure report").With("partition_key", partitionKey)
	}
	if state.Window.Holder != consumerSession {
		return FailResult{}, protocol.NewError(protocol.CodeFence, "failure report session does not hold the delivery window").
			With("partition_key", partitionKey).
			With("holder", state.Window.Holder)
	}
	if offset < state.Window.StartOffset || offset > state.Window.EndOffset {
		return FailResult{}, protocol.NewError(protocol.CodeCommit, "failed offset is outside the active delivery window").
			With("partition_key", partitionKey).
			With("offset", offset).
			With("window_start", state.Window.StartOffset).
			With("window_end", state.Window.EndOffset)
	}
	timeoutOrFailure := "fail"
	blocked, blockedOffset, blockedCount, err := e.applyRedeliveryLocked(ctx, partition, timeoutOrFailure, consumerSession, offset, validReason(reason), "client_fail", requestID)
	if err != nil {
		return FailResult{}, err
	}
	result := FailResult{PartitionKey: partitionKey, RedeliveryFrom: partition.state.CommittedOffset + 1, Blocked: blocked, BlockedOffset: blockedOffset, BlockedRetryCount: blockedCount}
	if blocked {
		return result, poisonError(partition.state, "retry limit exceeded while applying ordered redelivery")
	}
	e.emit("failure_recorded", map[string]any{
		"request_id":      requestID,
		"partition_key":   partitionKey,
		"session":         consumerSession,
		"failed_offset":   offset,
		"redelivery_from": result.RedeliveryFrom,
		"reason":          validReason(reason),
	})
	return result, nil
}

func (e *Engine) Unblock(ctx context.Context, requestID, partitionKey, operator string, expectedBlockedOffset int64) (UnblockResult, *protocol.BizError) {
	if err := e.assertContext(ctx); err != nil {
		return UnblockResult{}, protocol.AsBizError(err)
	}
	if err := validatePartitionKey(partitionKey); err != nil {
		return UnblockResult{}, err
	}
	operator = validReason(operator)
	partition := e.getPartition(partitionKey)
	if partition == nil {
		return UnblockResult{}, protocol.NewError(protocol.CodeCommit, "partition does not exist").With("partition_key", partitionKey)
	}
	partition.mu.Lock()
	defer partition.mu.Unlock()
	state := partition.state
	if !state.Blocked {
		return UnblockResult{}, protocol.NewError(protocol.CodeCommit, "partition is not blocked").With("partition_key", partitionKey)
	}
	if expectedBlockedOffset != 0 && expectedBlockedOffset != state.BlockedOffset {
		return UnblockResult{}, protocol.NewError(protocol.CodeCommit, "expected blocked offset does not match the ledger").
			With("partition_key", partitionKey).
			With("expected_blocked_offset", expectedBlockedOffset).
			With("blocked_offset", state.BlockedOffset)
	}
	next := state.clone()
	blockedOffset := next.BlockedOffset
	blockedCount := next.BlockedRetryCount
	next.Blocked = false
	next.BlockedOffset = 0
	next.BlockedRetryCount = 0
	next.PendingRedelivery = next.CommittedOffset < next.NextOffset-1
	if next.PendingRedelivery {
		next.RedeliveryFrom = next.CommittedOffset + 1
	} else {
		next.RedeliveryFrom = 0
	}
	for offset := range next.RetryCounts {
		if offset > next.CommittedOffset {
			delete(next.RetryCounts, offset)
		}
	}
	e.revision(&next)
	now := e.clock.Now()
	event := store.EventRow{
		PartitionKey:  partitionKey,
		Kind:          "unblock",
		Session:       operator,
		Offset:        blockedOffset,
		StartOffset:   next.CommittedOffset + 1,
		EndOffset:     next.NextOffset - 1,
		RetryCount:    blockedCount,
		Code:          protocol.CodeOK,
		Detail:        jsonDetail(map[string]any{"operator": operator, "blocked_offset": blockedOffset}),
		CreatedUnixMS: now.UnixMilli(),
	}
	tx, err := e.store.DB().BeginTx(ctx, nil)
	if err != nil {
		return UnblockResult{}, protocol.NewError(protocol.CodeInternal, "begin unblock transaction: %v", err)
	}
	defer tx.Rollback()
	if err := e.store.UpsertPartitionTx(ctx, tx, next.row()); err != nil {
		return UnblockResult{}, protocol.NewError(protocol.CodeInternal, "persist unblocked ledger: %v", err)
	}
	if err := e.store.DeleteRetryAfterTx(ctx, tx, partitionKey, next.CommittedOffset); err != nil {
		return UnblockResult{}, protocol.NewError(protocol.CodeInternal, "clear retry counters: %v", err)
	}
	eventID, err := e.store.InsertEventTx(ctx, tx, event)
	if err != nil {
		return UnblockResult{}, protocol.NewError(protocol.CodeInternal, "persist unblock event: %v", err)
	}
	if err := tx.Commit(); err != nil {
		return UnblockResult{}, protocol.NewError(protocol.CodeInternal, "commit unblock transaction: %v", err)
	}
	partition.state = next
	e.emit("partition_unblocked", map[string]any{"request_id": requestID, "partition_key": partitionKey, "operator": operator, "blocked_offset": blockedOffset, "blocked_retry_count": blockedCount, "event_id": eventID})
	return UnblockResult{PartitionKey: partitionKey}, nil
}

func (e *Engine) Status(ctx context.Context) (StatusResult, *protocol.BizError) {
	if err := e.assertContext(ctx); err != nil {
		return StatusResult{}, protocol.AsBizError(err)
	}
	e.partitionsMu.RLock()
	keys := make([]string, 0, len(e.partitions))
	partitions := make(map[string]*Partition, len(e.partitions))
	for key, partition := range e.partitions {
		keys = append(keys, key)
		partitions[key] = partition
	}
	e.partitionsMu.RUnlock()
	sort.Strings(keys)
	result := StatusResult{LogFormatVersion: store.LogFormatVersion, LedgerFormatVersion: store.LedgerFormatVersion, MaxRetries: e.maxRetries}
	for _, key := range keys {
		partition := partitions[key]
		partition.mu.Lock()
		now := e.clock.Now()
		if _, err := e.expireIfNeededLocked(ctx, partition, "status", now); err != nil {
			partition.mu.Unlock()
			return StatusResult{}, err
		}
		state := partition.state.clone()
		partition.mu.Unlock()
		result.Partitions = append(result.Partitions, state.view(now))
	}
	return result, nil
}

func (e *Engine) expireIfNeededLocked(ctx context.Context, partition *Partition, trigger string, now time.Time) (bool, *protocol.BizError) {
	state := partition.state
	if state.Window == nil || state.Window.Recovered || now.Before(state.Window.Deadline) {
		return false, nil
	}
	deadline := state.Window.Deadline
	holder := state.Window.Holder
	blocked, blockedOffset, blockedCount, err := e.applyRedeliveryLocked(ctx, partition, "timeout", holder, 0, "window deadline elapsed", trigger, "")
	if err != nil {
		return false, err
	}
	e.emit("window_timeout", map[string]any{
		"partition_key":       state.Key,
		"holder":              holder,
		"trigger":             trigger,
		"decision":            "timeout_won",
		"basis":               "partition_mutex; now_mono_ns >= deadline_mono_ns",
		"now_mono_ns":         e.nowMono(now),
		"deadline_mono_ns":    e.nowMono(deadline),
		"window_start":        state.Window.StartOffset,
		"window_end":          state.Window.EndOffset,
		"blocked":             blocked,
		"blocked_offset":      blockedOffset,
		"blocked_retry_count": blockedCount,
	})
	return true, nil
}

func (e *Engine) applyRecoveryRedeliveryLocked(ctx context.Context, partition *Partition, requestID string) (bool, int64, int, *protocol.BizError) {
	state := partition.state
	if state.Window == nil || !state.Window.Recovered {
		return false, 0, 0, nil
	}
	next := state.clone()
	window := *state.Window
	start := window.StartOffset
	end := window.EndOffset
	for offset := start; offset <= end; offset++ {
		next.RetryCounts[offset]++
	}
	blockedOffset, blockedCount, blocked := poisonIfExceeded(next.RetryCounts, start, end, e.maxRetries)
	next.Window = nil
	next.PendingRedelivery = !blocked
	next.RedeliveryFrom = next.CommittedOffset + 1
	next.Blocked = blocked
	next.BlockedOffset = blockedOffset
	next.BlockedRetryCount = blockedCount
	e.revision(&next)
	now := e.clock.Now()
	events := []store.EventRow{{
		PartitionKey:  state.Key,
		Kind:          "recovery_redelivery",
		Session:       window.Holder,
		Offset:        next.CommittedOffset,
		StartOffset:   start,
		EndOffset:     end,
		RetryCount:    maxRetryInRange(next.RetryCounts, start, end),
		Code:          protocol.CodeOK,
		Detail:        jsonDetail(map[string]any{"request_id": requestID, "recovered": true}),
		CreatedUnixMS: now.UnixMilli(),
	}}
	if blocked {
		events[0].Code = protocol.CodePoison
		events = append(events, store.EventRow{
			PartitionKey:  state.Key,
			Kind:          "poison",
			Session:       window.Holder,
			Offset:        blockedOffset,
			StartOffset:   start,
			EndOffset:     end,
			RetryCount:    blockedCount,
			Code:          protocol.CodePoison,
			Detail:        "retry limit exceeded during recovery redelivery",
			CreatedUnixMS: now.UnixMilli(),
		})
	}
	ids, err := e.persistRedeliveryLocked(ctx, partition, next, start, end, events)
	if err != nil {
		return false, 0, 0, err
	}
	e.emit("recovery_redelivery", map[string]any{
		"request_id":          requestID,
		"partition_key":       state.Key,
		"session":             window.Holder,
		"redelivery_from":     start,
		"redelivery_to":       end,
		"blocked":             blocked,
		"blocked_offset":      blockedOffset,
		"blocked_retry_count": blockedCount,
		"event_ids":           ids,
	})
	return blocked, blockedOffset, blockedCount, nil
}

func (e *Engine) applyRedeliveryLocked(ctx context.Context, partition *Partition, kind, session string, failedOffset int64, reason, trigger string, requestID string) (bool, int64, int, *protocol.BizError) {
	state := partition.state
	if state.Window == nil {
		return false, 0, 0, protocol.NewError(protocol.CodeFence, "delivery window disappeared during redelivery").
			With("partition_key", state.Key)
	}
	window := *state.Window
	start := window.StartOffset
	end := window.EndOffset
	next := state.clone()
	for offset := start; offset <= end; offset++ {
		next.RetryCounts[offset]++
	}
	blockedOffset, blockedCount, blocked := poisonIfExceeded(next.RetryCounts, start, end, e.maxRetries)
	next.Window = nil
	next.PendingRedelivery = !blocked
	next.RedeliveryFrom = next.CommittedOffset + 1
	next.Blocked = blocked
	next.BlockedOffset = blockedOffset
	next.BlockedRetryCount = blockedCount
	e.revision(&next)
	now := e.clock.Now()
	eventKind := kind
	if kind == "fail" {
		eventKind = "fail"
	}
	events := []store.EventRow{{
		PartitionKey:  state.Key,
		Kind:          eventKind,
		Session:       session,
		Offset:        failedOffset,
		StartOffset:   start,
		EndOffset:     end,
		RetryCount:    maxRetryInRange(next.RetryCounts, start, end),
		Code:          protocol.CodeOK,
		Detail:        jsonDetail(map[string]any{"request_id": requestID, "reason": reason, "trigger": trigger}),
		CreatedUnixMS: now.UnixMilli(),
	}}
	if blocked {
		events[0].Code = protocol.CodePoison
		events = append(events, store.EventRow{
			PartitionKey:  state.Key,
			Kind:          "poison",
			Session:       session,
			Offset:        blockedOffset,
			StartOffset:   start,
			EndOffset:     end,
			RetryCount:    blockedCount,
			Code:          protocol.CodePoison,
			Detail:        jsonDetail(map[string]any{"reason": reason, "trigger": trigger, "failed_offset": failedOffset}),
			CreatedUnixMS: now.UnixMilli(),
		})
	}
	ids, err := e.persistRedeliveryLocked(ctx, partition, next, start, end, events)
	if err != nil {
		return false, 0, 0, err
	}
	e.emit("redelivery_applied", map[string]any{
		"request_id":          requestID,
		"partition_key":       state.Key,
		"kind":                kind,
		"session":             session,
		"failed_offset":       failedOffset,
		"reason":              reason,
		"trigger":             trigger,
		"redelivery_from":     start,
		"redelivery_to":       end,
		"blocked":             blocked,
		"blocked_offset":      blockedOffset,
		"blocked_retry_count": blockedCount,
		"event_ids":           ids,
	})
	return blocked, blockedOffset, blockedCount, nil
}

func (e *Engine) persistRedeliveryLocked(ctx context.Context, partition *Partition, next partitionState, retryStart, retryEnd int64, events []store.EventRow) ([]int64, *protocol.BizError) {
	tx, err := e.store.DB().BeginTx(ctx, nil)
	if err != nil {
		return nil, protocol.NewError(protocol.CodeInternal, "begin redelivery transaction: %v", err)
	}
	defer tx.Rollback()
	if err := e.store.UpsertPartitionTx(ctx, tx, next.row()); err != nil {
		return nil, protocol.NewError(protocol.CodeInternal, "persist redelivery ledger: %v", err)
	}
	for offset := retryStart; offset <= retryEnd; offset++ {
		if err := e.store.UpsertRetryTx(ctx, tx, next.Key, offset, next.RetryCounts[offset]); err != nil {
			return nil, protocol.NewError(protocol.CodeInternal, "persist retry counter: %v", err)
		}
	}
	ids := make([]int64, 0, len(events))
	for _, event := range events {
		id, err := e.store.InsertEventTx(ctx, tx, event)
		if err != nil {
			return nil, protocol.NewError(protocol.CodeInternal, "persist redelivery event: %v", err)
		}
		ids = append(ids, id)
	}
	if err := tx.Commit(); err != nil {
		return nil, protocol.NewError(protocol.CodeInternal, "commit redelivery transaction: %v", err)
	}
	partition.state = next
	return ids, nil
}

func poisonIfExceeded(retries map[int64]int, start, end int64, maxRetries int) (int64, int, bool) {
	blockedOffset := int64(0)
	blockedCount := 0
	for offset := start; offset <= end; offset++ {
		attempts := retries[offset]
		if attempts > maxRetries {
			blockedOffset = offset
			blockedCount = attempts
			break
		}
	}
	return blockedOffset, blockedCount, blockedOffset != 0
}

func maxRetryInRange(retries map[int64]int, start, end int64) int {
	maxAttempts := 0
	for offset := start; offset <= end; offset++ {
		if retries[offset] > maxAttempts {
			maxAttempts = retries[offset]
		}
	}
	return maxAttempts
}

func poisonError(state partitionState, message string) *protocol.BizError {
	return protocol.NewError(protocol.CodePoison, message).
		With("partition_key", state.Key).
		With("blocked_offset", state.BlockedOffset).
		With("blocked_retry_count", state.BlockedRetryCount)
}
