package events

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/redis/go-redis/v9"
)

// Handler processes one event. Returning an error leaves the entry pending so
// it is retried; returning ErrPermanent sends it straight to the dead-letter stream.
type Handler func(ctx context.Context, evt Event) error

// ErrPermanent marks failures that retrying cannot fix (malformed events, ...).
var ErrPermanent = errors.New("permanent failure")

var processed = promauto.NewCounterVec(prometheus.CounterOpts{
	Name: "events_processed_total",
	Help: "Events consumed from the stream, by outcome.",
}, []string{"type", "outcome"})

// ConsumerConfig configures a Consumer.
type ConsumerConfig struct {
	Stream        string
	Group         string
	Name          string
	DeadLetter    string
	BatchSize     int64
	Block         time.Duration
	ClaimMinIdle  time.Duration
	ReclaimEvery  time.Duration
	MaxDeliveries int64
}

// Consumer reads a stream through a consumer group (at-least-once delivery).
type Consumer struct {
	rdb     *redis.Client
	cfg     ConsumerConfig
	handler Handler
	log     *slog.Logger
}

// NewConsumer builds a Consumer.
func NewConsumer(rdb *redis.Client, cfg ConsumerConfig, handler Handler, log *slog.Logger) *Consumer {
	return &Consumer{rdb: rdb, cfg: cfg, handler: handler, log: log}
}

// EnsureGroup creates the consumer group (and stream) if needed.
func (c *Consumer) EnsureGroup(ctx context.Context) error {
	err := c.rdb.XGroupCreateMkStream(ctx, c.cfg.Stream, c.cfg.Group, "0").Err()
	if err != nil && !strings.Contains(err.Error(), "BUSYGROUP") {
		return err
	}
	return nil
}

// Run blocks until ctx is cancelled.
func (c *Consumer) Run(ctx context.Context) error {
	if err := c.EnsureGroup(ctx); err != nil {
		return err
	}
	c.log.InfoContext(ctx, "consumer started", "stream", c.cfg.Stream, "group", c.cfg.Group, "consumer", c.cfg.Name)

	go c.reclaimLoop(ctx)

	for ctx.Err() == nil {
		streams, err := c.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    c.cfg.Group,
			Consumer: c.cfg.Name,
			Streams:  []string{c.cfg.Stream, ">"},
			Count:    c.cfg.BatchSize,
			Block:    c.cfg.Block,
		}).Result()
		switch {
		case errors.Is(err, redis.Nil):
			continue
		case ctx.Err() != nil:
			return nil
		case err != nil:
			c.log.ErrorContext(ctx, "xreadgroup failed", "error", err)
			sleep(ctx, 2*time.Second)
			continue
		}
		for _, stream := range streams {
			for _, msg := range stream.Messages {
				c.process(ctx, msg)
			}
		}
	}
	return nil
}

// ProcessPending runs one reclaim pass (exported for tests).
func (c *Consumer) ProcessPending(ctx context.Context) error {
	pending, err := c.rdb.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: c.cfg.Stream,
		Group:  c.cfg.Group,
		Idle:   c.cfg.ClaimMinIdle,
		Start:  "-",
		End:    "+",
		Count:  c.cfg.BatchSize,
	}).Result()
	if err != nil {
		return err
	}
	for _, p := range pending {
		msgs, err := c.rdb.XClaim(ctx, &redis.XClaimArgs{
			Stream:   c.cfg.Stream,
			Group:    c.cfg.Group,
			Consumer: c.cfg.Name,
			MinIdle:  c.cfg.ClaimMinIdle,
			Messages: []string{p.ID},
		}).Result()
		if err != nil || len(msgs) == 0 {
			continue
		}
		if p.RetryCount >= c.cfg.MaxDeliveries {
			c.deadLetter(ctx, msgs[0], "max deliveries exceeded")
			continue
		}
		c.process(ctx, msgs[0])
	}
	return nil
}

func (c *Consumer) reclaimLoop(ctx context.Context) {
	ticker := time.NewTicker(c.cfg.ReclaimEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := c.ProcessPending(ctx); err != nil && ctx.Err() == nil {
				c.log.ErrorContext(ctx, "reclaim pass failed", "error", err)
			}
		}
	}
}

func (c *Consumer) process(ctx context.Context, msg redis.XMessage) {
	evt, err := FromStream(msg.ID, msg.Values)
	if err != nil {
		c.deadLetter(ctx, msg, err.Error())
		return
	}
	log := c.log.With("event_id", evt.ID, "event_type", evt.Type, "stream_id", msg.ID)

	if err := c.handler(ctx, evt); err != nil {
		if errors.Is(err, ErrPermanent) {
			c.deadLetter(ctx, msg, err.Error())
			return
		}
		processed.WithLabelValues(evt.Type, "retry").Inc()
		log.WarnContext(ctx, "event handling failed; will retry", "error", err)
		return // stays pending -> reclaimed later
	}
	if err := c.rdb.XAck(ctx, c.cfg.Stream, c.cfg.Group, msg.ID).Err(); err != nil {
		log.ErrorContext(ctx, "xack failed", "error", err)
		return
	}
	processed.WithLabelValues(evt.Type, "ok").Inc()
}

func (c *Consumer) deadLetter(ctx context.Context, msg redis.XMessage, reason string) {
	values := make(map[string]any, len(msg.Values)+2)
	for k, v := range msg.Values {
		values[k] = v
	}
	values["dead_letter_reason"] = reason
	values["original_stream_id"] = msg.ID

	pipe := c.rdb.TxPipeline()
	pipe.XAdd(ctx, &redis.XAddArgs{Stream: c.cfg.DeadLetter, Values: values})
	pipe.XAck(ctx, c.cfg.Stream, c.cfg.Group, msg.ID)
	if _, err := pipe.Exec(ctx); err != nil {
		c.log.ErrorContext(ctx, "dead-lettering failed", "error", err, "stream_id", msg.ID)
		return
	}
	eventType, _ := msg.Values["type"].(string)
	processed.WithLabelValues(eventType, "dead_letter").Inc()
	c.log.WarnContext(ctx, "event dead-lettered", "stream_id", msg.ID, "reason", reason)
}

func sleep(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}
