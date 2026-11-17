package events

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func setup(t *testing.T, handler Handler) (*Consumer, *redis.Client, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	c := NewConsumer(rdb, ConsumerConfig{
		Stream: "events", Group: "g", Name: "c1", DeadLetter: "events.dead",
		BatchSize: 10, Block: 50 * time.Millisecond, ClaimMinIdle: 0,
		ReclaimEvery: time.Hour, MaxDeliveries: 2,
	}, handler, slog.New(slog.NewTextHandler(io.Discard, nil)))
	return c, rdb, mr
}

func publish(t *testing.T, rdb *redis.Client, values map[string]any) {
	t.Helper()
	if err := rdb.XAdd(context.Background(), &redis.XAddArgs{Stream: "events", Values: values}).Err(); err != nil {
		t.Fatal(err)
	}
}

func TestConsumerDeliversAndAcks(t *testing.T) {
	var mu sync.Mutex
	var got []Event
	c, rdb, _ := setup(t, func(_ context.Context, e Event) error {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, e)
		return nil
	})
	publish(t, rdb, map[string]any{"id": "1", "type": "invoice.paid", "user_id": "u", "payload": `{"n":1}`})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error)
	go func() { done <- c.Run(ctx) }()

	deadline := time.Now().Add(3 * time.Second)
	for {
		mu.Lock()
		n := len(got)
		mu.Unlock()
		if n == 1 || time.Now().After(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}

	if len(got) != 1 || got[0].Payload["n"] != float64(1) {
		t.Fatalf("unexpected events %+v", got)
	}
	pending, _ := rdb.XPending(context.Background(), "events", "g").Result()
	if pending.Count != 0 {
		t.Fatalf("expected no pending entries, got %d", pending.Count)
	}
}

func TestFailingEventIsRetriedThenDeadLettered(t *testing.T) {
	attempts := 0
	c, rdb, _ := setup(t, func(context.Context, Event) error {
		attempts++
		return errors.New("smtp down")
	})
	ctx := context.Background()
	if err := c.EnsureGroup(ctx); err != nil {
		t.Fatal(err)
	}
	publish(t, rdb, map[string]any{"id": "1", "type": "invoice.paid", "payload": "{}"})

	msgs, _ := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "c1", Streams: []string{"events", ">"}}).Result()
	c.process(ctx, msgs[0].Messages[0]) // delivery 1 fails
	_ = c.ProcessPending(ctx)           // delivery 2 fails
	_ = c.ProcessPending(ctx)           // max deliveries reached -> dead letter

	if attempts != 2 {
		t.Fatalf("handler called %d times, want 2", attempts)
	}
	dead, _ := rdb.XLen(ctx, "events.dead").Result()
	if dead != 1 {
		t.Fatalf("dead-letter stream length = %d, want 1", dead)
	}
}

func TestMalformedEventsAreDeadLetteredImmediately(t *testing.T) {
	c, rdb, _ := setup(t, func(context.Context, Event) error { t.Fatal("must not be called"); return nil })
	ctx := context.Background()
	_ = c.EnsureGroup(ctx)
	publish(t, rdb, map[string]any{"type": "x"}) // no id

	msgs, _ := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "c1", Streams: []string{"events", ">"}}).Result()
	c.process(ctx, msgs[0].Messages[0])

	if n, _ := rdb.XLen(ctx, "events.dead").Result(); n != 1 {
		t.Fatalf("dead-letter length = %d", n)
	}
}
