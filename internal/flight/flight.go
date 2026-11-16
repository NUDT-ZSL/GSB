package flight

import (
	"context"
	"fmt"
	"sync"
)

type call struct {
	done chan struct{}
	val  any
	err  error
}

// Group coalesces concurrent calls with the same key. The leader executes fn;
// every waiter observes the leader's complete result.
type Group struct {
	mu    sync.Mutex
	calls map[string]*call
}

func New() *Group { return &Group{calls: make(map[string]*call)} }

func (g *Group) Do(ctx context.Context, key string, fn func() (any, error)) (val any, err error, shared bool) {
	g.mu.Lock()
	if existing, ok := g.calls[key]; ok {
		g.mu.Unlock()
		select {
		case <-existing.done:
			return existing.val, existing.err, true
		case <-ctx.Done():
			return nil, ctx.Err(), true
		}
	}
	current := &call{done: make(chan struct{})}
	g.calls[key] = current
	g.mu.Unlock()

	defer func() {
		if recovered := recover(); recovered != nil {
			val = nil
			err = fmt.Errorf("coalesced call panicked: %v", recovered)
			current.val = val
			current.err = err
		}
		g.mu.Lock()
		delete(g.calls, key)
		g.mu.Unlock()
		close(current.done)
	}()
	current.val, current.err = fn()
	return current.val, current.err, false
}
