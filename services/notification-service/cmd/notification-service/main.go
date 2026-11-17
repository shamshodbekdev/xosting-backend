// Command notification-service consumes domain events from Redis Streams and
// delivers customer/staff notifications over email (SMTP) and Telegram.
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

	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/channels"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/config"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/contacts"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/dispatcher"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/events"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/healthcheck"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/server"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/templates"
	"github.com/shamshodbekdev/xosting-backend/shared/logger"
	"github.com/shamshodbekdev/xosting-backend/shared/tracing"
)

// version is overridden at build time: -ldflags "-X main.version=<tag>".
var version = "dev"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		addr := os.Getenv("HTTP_ADDR")
		if addr == "" {
			addr = ":8090"
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
	rdb := redis.NewClient(redisOpts)
	defer rdb.Close()

	registry, err := templates.NewRegistry()
	if err != nil {
		return err
	}
	email, err := channels.NewEmailSender(channels.SMTPConfig{
		Host: cfg.SMTPHost, Port: cfg.SMTPPort, Username: cfg.SMTPUsername, Password: cfg.SMTPPassword,
		From: cfg.SMTPFrom, TLSMode: cfg.SMTPTLSMode,
	})
	if err != nil {
		return err
	}
	telegram := channels.NewTelegramSender(cfg.TelegramAPIURL, cfg.TelegramBotToken, tracing.Transport(http.DefaultTransport))
	if !telegram.Enabled() {
		log.Warn("TELEGRAM_BOT_TOKEN not set; Telegram notifications are disabled")
	}

	d := &dispatcher.Dispatcher{
		Templates:   registry,
		Contacts:    contacts.NewClient(cfg.AuthServiceURL, cfg.InternalAPIToken, cfg.ContactCacheTTL, tracing.Transport(http.DefaultTransport)),
		Email:       email,
		Telegram:    telegram,
		AdminChatID: cfg.TelegramAdminChatID,
		Redis:       rdb,
		DedupeTTL:   cfg.DedupeTTL,
		Log:         log,
	}
	consumer := events.NewConsumer(rdb, events.ConsumerConfig{
		Stream: cfg.EventsStream, Group: cfg.ConsumerGroup, Name: cfg.ConsumerName,
		DeadLetter: cfg.DeadLetter, BatchSize: cfg.BatchSize, Block: cfg.BlockTimeout,
		ClaimMinIdle: cfg.ClaimMinIdle, ReclaimEvery: cfg.ReclaimEvery, MaxDeliveries: cfg.MaxDeliveries,
	}, d.Handle, log)

	srv := server.New(cfg.HTTPAddr, rdb)
	srvErr := make(chan error, 1)
	go func() {
		log.Info("http server listening", "addr", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			srvErr <- err
		}
	}()

	consumerErr := make(chan error, 1)
	go func() { consumerErr <- consumer.Run(ctx) }()

	select {
	case <-ctx.Done():
		log.Info("shutdown signal received")
	case err := <-srvErr:
		return err
	case err := <-consumerErr:
		if err != nil {
			return err
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	stop()
	_ = srv.Shutdown(shutdownCtx)
	select {
	case <-consumerErr:
	case <-shutdownCtx.Done():
	}
	log.Info("shutdown complete")
	return nil
}
