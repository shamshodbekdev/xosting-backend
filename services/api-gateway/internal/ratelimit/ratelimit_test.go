package ratelimit

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestLimiterBlocksAfterLimitAndResets(t *testing.T) {
	mr := miniredis.RunT(t)
	l := New(redis.NewClient(&redis.Options{Addr: mr.Addr()}), time.Minute)
	ctx := context.Background()

	for i := 1; i <= 3; i++ {
		res, err := l.Allow(ctx, "ip:1.2.3.4", 3)
		if err != nil || !res.Allowed || res.Remaining != 3-i {
			t.Fatalf("hit %d: %+v %v", i, res, err)
		}
	}
	res, _ := l.Allow(ctx, "ip:1.2.3.4", 3)
	if res.Allowed || res.ResetIn <= 0 {
		t.Fatalf("4th hit should be rejected: %+v", res)
	}
	if other, _ := l.Allow(ctx, "ip:5.6.7.8", 3); !other.Allowed {
		t.Fatal("limits must be per key")
	}

	mr.FastForward(time.Minute + time.Second)
	if res, _ := l.Allow(ctx, "ip:1.2.3.4", 3); !res.Allowed {
		t.Fatal("window should have reset")
	}
}
