package store

import (
	"sort"

	"locklease/internal/model"
)

// nowMs returns the injected time when the client supplied one (the only
// authoritative time source for deterministic runs), otherwise the server
// wall clock in milliseconds.
func resolveNow(injected int64) int64 {
	if injected > 0 {
		return injected
	}
	return timeNowMs()
}

func timeNowMs() int64 {
	return currentWallMs()
}

// Acquire implements lock acquisition with lazy expiry, duplicate rejection
// (E_LOCKED) and optional force preemption which triggers propagation.
func (s *Store) Acquire(key, holder string, ttlMs, nowInjected int64, force bool) (*model.Lock, []model.TraceEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := resolveNow(nowInjected)
	if !s.graph.Has(key) {
		return nil, nil, bizErr(ENotFound, "unknown lock key "+key, map[string]any{"key": key})
	}
	if holder == "" || ttlMs <= 0 {
		return nil, nil, bizErr(EBadRequest, "holder and positive ttl_ms are required", nil)
	}

	lk := s.locks[key]
	if lk != nil && lk.ExpiresAt <= now {
		// Lazy reclamation; reuse the deterministic reaper path.
		s.reapExpiredLocked(now, "expired")
		lk = s.locks[key]
	}

	if lk != nil {
		if lk.Holder == holder && !force {
			return nil, nil, bizErr(ELocked, "lock already held by requester", map[string]any{
				"key": key, "holder": lk.Holder, "expires_at_ms": lk.ExpiresAt,
				"lease_version": lk.Version,
			})
		}
		if !force {
			return nil, nil, bizErr(ELocked, "lock is already held", map[string]any{
				"key": key, "holder": lk.Holder, "expires_at_ms": lk.ExpiresAt,
				"lease_version": lk.Version,
			})
		}
		// Preemption: replace holder and invalidate downstream leases.
		oldHolder := lk.Holder
		oldVersion := lk.Version
		lk.Holder = holder
		lk.ExpiresAt = now + ttlMs
		lk.AcquiredAt = now
		lk.Version++
		s.keyVersions[key] = lk.Version
		traces := s.propagateLocked(key, "preempt", now, oldHolder, oldVersion)
		s.tableVersion++
		if err := s.persistLocked(); err != nil {
			return nil, nil, err
		}
		return cloneLock(lk), traces, nil
	}

	lk = &model.Lock{
		Key:        key,
		Holder:     holder,
		ExpiresAt:  now + ttlMs,
		Version:    1,
		AcquiredAt: now,
		DependsOn:  s.graph.DependsOn(key),
	}
	s.locks[key] = lk
	s.keyVersions[key] = lk.Version
	s.tableVersion++
	if err := s.persistLocked(); err != nil {
		return nil, nil, err
	}
	return cloneLock(lk), nil, nil
}

// Renew extends a lease only when holder and lease version both match. A lock
// that has already expired is reaped first, so renewal cannot double-write.
func (s *Store) Renew(key, holder string, version, ttlMs, nowInjected int64) (*model.Lock, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := resolveNow(nowInjected)
	lk := s.locks[key]
	if lk == nil {
		return nil, bizErr(ENotFound, "no active lock for key "+key, map[string]any{"key": key})
	}
	if lk.ExpiresAt <= now {
		s.reapExpiredLocked(now, "expired")
		return nil, bizErr(EVersion, "lease already expired and was reclaimed", map[string]any{
			"key": key, "expected_version": version,
		})
	}
	if lk.Holder != holder || lk.Version != version {
		return nil, bizErr(EVersion, "holder or lease version mismatch", map[string]any{
			"key": key, "requested_version": version,
			"current_holder": lk.Holder, "current_version": lk.Version,
		})
	}
	lk.ExpiresAt = now + ttlMs
	s.tableVersion++
	if err := s.persistLocked(); err != nil {
		return nil, err
	}
	return cloneLock(lk), nil
}

// Release frees a held lock after holder/version checks and propagates
// invalidation to every downstream dependent lock.
func (s *Store) Release(key, holder string, version, nowInjected int64) ([]model.TraceEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := resolveNow(nowInjected)
	lk := s.locks[key]
	if lk == nil {
		return nil, bizErr(ENotFound, "no active lock for key "+key, map[string]any{"key": key})
	}
	if lk.ExpiresAt <= now {
		s.reapExpiredLocked(now, "expired")
		return nil, bizErr(EVersion, "lease already expired and was reclaimed", map[string]any{"key": key})
	}
	if lk.Holder != holder || lk.Version != version {
		return nil, bizErr(EVersion, "holder or lease version mismatch", map[string]any{
			"key": key, "requested_version": version,
			"current_holder": lk.Holder, "current_version": lk.Version,
		})
	}
	oldVersion := lk.Version
	delete(s.locks, key)
	traces := s.propagateLocked(key, "release", now, holder, oldVersion)
	s.tableVersion++
	if err := s.persistLocked(); err != nil {
		return nil, err
	}
	return traces, nil
}

// ReapExpired is invoked by the background reaper using the server wall clock.
func (s *Store) ReapExpired(nowInjected int64) ([]model.ReclaimRecord, []model.TraceEntry) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := resolveNow(nowInjected)
	before := len(s.reclaims)
	s.reapExpiredLocked(now, "expired")
	if len(s.reclaims) == before {
		return nil, nil
	}
	return append([]model.ReclaimRecord(nil), s.reclaims[before:]...),
		append([]model.TraceEntry(nil), s.traces...)
}

// reapExpiredLocked reaps all expired locks in sorted key order. Each reaped
// lock propagates to its downstream closure; the visited-set guarantees a lock
// is never invalidated twice even when two triggers overlap.
func (s *Store) reapExpiredLocked(now int64, reason string) {
	var expired []string
	for key, lk := range s.locks {
		if lk.ExpiresAt <= now {
			expired = append(expired, key)
		}
	}
	sort.Strings(expired)
	for _, key := range expired {
		lk := s.locks[key]
		if lk == nil {
			continue
		}
		oldVersion := lk.Version
		holder := lk.Holder
		delete(s.locks, key)
		s.reclaims = append(s.reclaims, model.ReclaimRecord{
			Key: key, Holder: holder, Reason: reason, Version: oldVersion, AtMs: now,
		})
		s.propagateLocked(key, reason, now, holder, oldVersion)
		s.tableVersion++
	}
	if len(expired) > 0 {
		if err := s.persistLocked(); err != nil {
			panic(err)
		}
	}
}

// propagateLocked walks the deterministic downstream closure of source. Each
// affected held lock has its lease invalidated (bumped version, holder
// cleared) exactly once; every step is appended to the durable trace log.
func (s *Store) propagateLocked(source, cause string, now int64, oldHolder string, oldVersion int64) []model.TraceEntry {
	s.eventSeq++
	eventID := s.eventSeq
	closure := s.graph.DownstreamClosure(source)
	var emitted []model.TraceEntry
	visited := map[string]struct{}{}
	for _, affected := range closure {
		if _, dup := visited[affected]; dup {
			continue
		}
		visited[affected] = struct{}{}
		lk, held := s.locks[affected]
		if !held {
			// Nothing to invalidate: no lease, no version bump, no trace step.
			continue
		}
		lk.Version++
		newVersion := lk.Version
		s.keyVersions[affected] = newVersion
		// Invalidation releases the lease; re-acquisition starts fresh.
		delete(s.locks, affected)
		s.traceSeq++
		entry := model.TraceEntry{
			Seq: s.traceSeq, EventID: eventID, Cause: cause,
			SourceKey: source, Affected: affected,
			Version: newVersion, AtMs: now,
		}
		s.traces = append(s.traces, entry)
		emitted = append(emitted, entry)
	}
	_ = oldHolder
	_ = oldVersion
	return emitted
}

func cloneLock(lk *model.Lock) *model.Lock {
	cp := *lk
	if lk.DependsOn != nil {
		cp.DependsOn = append([]string(nil), lk.DependsOn...)
	}
	return &cp
}
