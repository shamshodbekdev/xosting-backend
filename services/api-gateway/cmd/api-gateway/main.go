// Command api-gateway is the single public entry point of xosting.net: it
// authenticates requests, applies rate limits and proxies them (including
// WebSocket upgrades) to the internal services.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/config"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/gateway"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/healthcheck"
	"github.com/shamshodbekdev/xosting-backend/shared/logger"
	"github.com/shamshodbekdev/xosting-backend/shared/tracing"
)

// version is overridden at build time: -ldflags "-X main.version=<tag>".
var version = "dev"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		addr := os.Getenv("HTTP_ADDR")
		if addr == "" {
			addr = ":8080"
		}
		os.Exit(healthcheck.Probe(addr))
	}
	if err := run(); err != nil {
		slog.Error("fatal", "error", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	log := logger.New(cfg.ServiceName, cfg.LogLevel)
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	shutdownTracing, err := tracing.Setup(ctx, cfg.ServiceName, version)
	if err != nil {
		return err
	}
	defer func() { _ = shutdownTracing(context.Background()) }()

	redisOpts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return err
	}
	// Redis sits on the hot path (rate limiting): fail fast and let the limiter
	// fail open rather than retrying past the request budget.
	redisOpts.MaxRetries = 1
	redisOpts.DialTimeout = 300 * time.Millisecond
	redisOpts.ReadTimeout = 200 * time.Millisecond
	redisOpts.WriteTimeout = 200 * time.Millisecond
	rdb := redis.NewClient(redisOpts)
	defer rdb.Close()

	gw, err := gateway.New(cfg, rdb, nil, log)
	if err != nil {
		return err
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           gw.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
		// No Read/WriteTimeout: they would kill long-lived WebSocket tunnels.
		// Upstream slowness is bounded by the transport's ResponseHeaderTimeout.
	}

	errCh := make(chan error, 1)
	go func() {
		log.Info("api-gateway listening", "addr", cfg.HTTPAddr, "environment", cfg.Environment)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case <-ctx.Done():
		log.Info("shutdown signal received; draining connections")
	case err := <-errCh:
		return err
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return err
	}
	log.Info("shutdown complete")
	return nil
}
