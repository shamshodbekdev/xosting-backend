// Package config loads notification-service configuration from the environment.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds every tunable of the service.
type Config struct {
	ServiceName string
	Environment string
	LogLevel    string
	HTTPAddr    string

	RedisURL      string
	EventsStream  string
	ConsumerGroup string
	ConsumerName  string
	DeadLetter    string
	BatchSize     int64
	BlockTimeout  time.Duration
	ClaimMinIdle  time.Duration
	MaxDeliveries int64
	DedupeTTL     time.Duration
	ReclaimEvery  time.Duration

	AuthServiceURL   string
	InternalAPIToken string
	ContactCacheTTL  time.Duration

	SMTPHost     string
	SMTPPort     int
	SMTPUsername string
	SMTPPassword string
	SMTPFrom     string
	SMTPTLSMode  string // "starttls" | "tls" | "none"

	TelegramBotToken    string
	TelegramAPIURL      string
	TelegramAdminChatID string
}

// Load reads configuration from environment variables.
func Load() (Config, error) {
	hostname, _ := os.Hostname()
	cfg := Config{
		ServiceName:         "notification-service",
		Environment:         getenv("ENVIRONMENT", "development"),
		LogLevel:            getenv("LOG_LEVEL", "info"),
		HTTPAddr:            getenv("HTTP_ADDR", ":8090"),
		RedisURL:            getenv("REDIS_URL", "redis://localhost:6379/0"),
		EventsStream:        getenv("EVENTS_STREAM", "xosting.events"),
		ConsumerGroup:       getenv("CONSUMER_GROUP", "notification-service"),
		ConsumerName:        getenv("CONSUMER_NAME", hostname),
		DeadLetter:          getenv("DEAD_LETTER_STREAM", "xosting.events.dead"),
		AuthServiceURL:      getenv("AUTH_SERVICE_URL", "http://auth-service:8000"),
		InternalAPIToken:    os.Getenv("INTERNAL_API_TOKEN"),
		SMTPHost:            getenv("SMTP_HOST", "localhost"),
		SMTPUsername:        os.Getenv("SMTP_USERNAME"),
		SMTPPassword:        os.Getenv("SMTP_PASSWORD"),
		SMTPFrom:            getenv("SMTP_FROM", "xosting.net <no-reply@xosting.net>"),
		SMTPTLSMode:         getenv("SMTP_TLS_MODE", "starttls"),
		TelegramBotToken:    os.Getenv("TELEGRAM_BOT_TOKEN"),
		TelegramAPIURL:      getenv("TELEGRAM_API_URL", "https://api.telegram.org"),
		TelegramAdminChatID: os.Getenv("TELEGRAM_ADMIN_CHAT_ID"),
	}

	var err error
	if cfg.SMTPPort, err = atoi("SMTP_PORT", 587); err != nil {
		return cfg, err
	}
	if cfg.BatchSize, err = atoi64("CONSUMER_BATCH_SIZE", 32); err != nil {
		return cfg, err
	}
	if cfg.MaxDeliveries, err = atoi64("CONSUMER_MAX_DELIVERIES", 5); err != nil {
		return cfg, err
	}
	durations := []struct {
		target *time.Duration
		key    string
		def    time.Duration
	}{
		{&cfg.BlockTimeout, "CONSUMER_BLOCK_TIMEOUT", 5 * time.Second},
		{&cfg.ClaimMinIdle, "CONSUMER_CLAIM_MIN_IDLE", time.Minute},
		{&cfg.ReclaimEvery, "CONSUMER_RECLAIM_INTERVAL", 30 * time.Second},
		{&cfg.DedupeTTL, "DEDUPE_TTL", 7 * 24 * time.Hour},
		{&cfg.ContactCacheTTL, "CONTACT_CACHE_TTL", 5 * time.Minute},
	}
	for _, d := range durations {
		if *d.target, err = duration(d.key, d.def); err != nil {
			return cfg, err
		}
	}

	if cfg.InternalAPIToken == "" {
		return cfg, fmt.Errorf("INTERNAL_API_TOKEN is required")
	}
	switch cfg.SMTPTLSMode {
	case "starttls", "tls", "none":
	default:
		return cfg, fmt.Errorf("SMTP_TLS_MODE must be starttls, tls or none (got %q)", cfg.SMTPTLSMode)
	}
	return cfg, nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func atoi(key string, def int) (int, error) {
	v := os.Getenv(key)
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", key, err)
	}
	return n, nil
}

func atoi64(key string, def int64) (int64, error) {
	n, err := atoi(key, int(def))
	return int64(n), err
}

func duration(key string, def time.Duration) (time.Duration, error) {
	v := os.Getenv(key)
	if v == "" {
		return def, nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", key, err)
	}
	return d, nil
}
