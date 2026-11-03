package main

import (
	"sync"
	"time"

	"golang.org/x/time/rate"
)

// Limiter keeps one token bucket per client key and forgets clients idle longer than ttl.
type Limiter struct {
	mu      sync.Mutex
	clients map[string]*client
	rate    rate.Limit
	burst   int
	ttl     time.Duration
	now     func() time.Time
}

type client struct {
	bucket   *rate.Limiter
	lastSeen time.Time
}

func NewLimiter(perSecond float64, burst int, ttl time.Duration) *Limiter {
	return &Limiter{clients: map[string]*client{}, rate: rate.Limit(perSecond), burst: burst, ttl: ttl, now: time.Now}
}

// Allow reports whether the client may make a request now, spending one token if so.
func (l *Limiter) Allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	c, ok := l.clients[key]
	if !ok {
		c = &client{bucket: rate.NewLimiter(l.rate, l.burst)}
		l.clients[key] = c
	}
	c.lastSeen = now
	return c.bucket.AllowN(now, 1)
}

// Clients is the number of client keys currently tracked.
func (l *Limiter) Clients() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.clients)
}

// Evict drops clients not seen for longer than the ttl.
func (l *Limiter) Evict() {
	l.mu.Lock()
	defer l.mu.Unlock()
	cutoff := l.now().Add(-l.ttl)
	for k, c := range l.clients {
		if c.lastSeen.Before(cutoff) {
			delete(l.clients, k)
		}
	}
}

// Sweep evicts idle clients every interval, forever.
func (l *Limiter) Sweep(interval time.Duration) {
	for range time.Tick(interval) {
		l.Evict()
	}
}
