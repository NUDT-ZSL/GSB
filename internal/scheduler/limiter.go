package scheduler

import (
	"context"
	"sync"
)

type limiter struct {
	mu            sync.Mutex
	cond          *sync.Cond
	globalLimit   int
	serviceLimits map[string]int
	activeGlobal  int
	activeService map[string]int
	peakGlobal    int
	peakService   map[string]int
}

func newLimiter(global int, limits map[string]int) *limiter {
	l := &limiter{
		globalLimit:   global,
		serviceLimits: make(map[string]int, len(limits)),
		activeService: make(map[string]int, len(limits)),
		peakService:   make(map[string]int, len(limits)),
	}
	for k, v := range limits {
		l.serviceLimits[k] = v
	}
	l.cond = sync.NewCond(&l.mu)
	return l
}

func (l *limiter) acquire(ctx context.Context, service string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	for l.activeGlobal >= l.globalLimit || l.activeService[service] >= l.serviceLimits[service] {
		if err := ctx.Err(); err != nil {
			return err
		}
		l.cond.Wait()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	l.activeGlobal++
	l.activeService[service]++
	if l.activeGlobal > l.peakGlobal {
		l.peakGlobal = l.activeGlobal
	}
	if l.activeService[service] > l.peakService[service] {
		l.peakService[service] = l.activeService[service]
	}
	return nil
}

func (l *limiter) release(service string) {
	l.mu.Lock()
	if l.activeGlobal > 0 {
		l.activeGlobal--
	}
	if l.activeService[service] > 0 {
		l.activeService[service]--
	}
	l.cond.Broadcast()
	l.mu.Unlock()
}

func (l *limiter) snapshot() (int, map[string]int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	active := make(map[string]int, len(l.activeService))
	for k, v := range l.activeService {
		active[k] = v
	}
	return l.activeGlobal, active
}

func (l *limiter) peaks() (int, map[string]int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	peaks := make(map[string]int, len(l.peakService))
	for k, v := range l.peakService {
		peaks[k] = v
	}
	return l.peakGlobal, peaks
}

func (l *limiter) limits() map[string]int {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make(map[string]int, len(l.serviceLimits))
	for k, v := range l.serviceLimits {
		out[k] = v
	}
	return out
}
