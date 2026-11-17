// Package ratelimit implements a distributed fixed-window rate limiter on
// Redis. The INCR + PEXPIRE pair runs atomically in a Lua script, so all
// gateway replicas share one consistent counter per client.
package ratelimit

import (
	"context"
	"time"

	"github.com/redis/go-redis/v9"
)

var script = redis.NewScript(`
local current = redis.call("INCR", KEYS[1])
if current == 1 then
  redis.call("PEXPIRE", KEYS[1], ARGV[1])
end
local ttl = redis.call("PTTL", KEYS[1])
if ttl < 0 then
  redis.call("PEXPIRE", KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {current, ttl}
`)

// Result describes the outcome of one Allow call.
type Result struct {
	Allowed   bool
	Limit     int
	Remaining int
	ResetIn   time.Duration
}

// Limiter enforces limits per key.
type Limiter struct {
	rdb    *redis.Client
	window time.Duration
	prefix string
}

// New builds a Limiter.
func New(rdb *redis.Client, window time.Duration) *Limiter {
	return &Limiter{rdb: rdb, window: window, prefix: "ratelimit:"}
}

// Allow records one hit for key and reports whether it is within limit.
func (l *Limiter) Allow(ctx context.Context, key string, limit int) (Result, error) {
	values, err := script.Run(ctx, l.rdb, []string{l.prefix + key}, l.window.Milliseconds()).Int64Slice()
	if err != nil {
		return Result{}, err
	}
	count, ttl := int(values[0]), time.Duration(values[1])*time.Millisecond
	remaining := limit - count
	if remaining < 0 {
		remaining = 0
	}
	return Result{Allowed: count <= limit, Limit: limit, Remaining: remaining, ResetIn: ttl}, nil
}
