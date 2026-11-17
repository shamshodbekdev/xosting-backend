// Package config loads api-gateway configuration from the environment.
package config

import (
	"fmt"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"time"
)

// Upstreams holds backend base URLs.
type Upstreams struct {
	Auth, Billing, Hosting, Domain, Support string
}

// Config is the gateway configuration.
type Config struct {
	ServiceName string
	Environment string
	LogLevel    string
	HTTPAddr    string

	Upstreams       Upstreams
	UpstreamTimeout time.Duration

	JWTSecret   string
	JWTIssuer   string
	JWTAudience string

	RedisURL          string
	RateLimitDefault  int // requests per window per client
	RateLimitAuth     int // requests per window per IP on credential endpoints
	RateLimitWindow   time.Duration
	TrustedProxies    []netip.Prefix
	CORSAllowedOrigin []string
	MaxBodyBytes      int64
}

// Load reads configuration from environment variables.
func Load() (Config, error) {
	cfg := Config{
		ServiceName: "api-gateway",
		Environment: getenv("ENVIRONMENT", "development"),
		LogLevel:    getenv("LOG_LEVEL", "info"),
		HTTPAddr:    getenv("HTTP_ADDR", ":8080"),
		Upstreams: Upstreams{
			Auth:    getenv("AUTH_SERVICE_URL", "http://auth-service:8000"),
			Billing: getenv("BILLING_SERVICE_URL", "http://billing-service:8000"),
			Hosting: getenv("HOSTING_SERVICE_URL", "http://hosting-service:8000"),
			Domain:  getenv("DOMAIN_SERVICE_URL", "http://domain-service:8000"),
			Support: getenv("SUPPORT_SERVICE_URL", "http://support-service:8000"),
		},
		JWTSecret:         os.Getenv("JWT_SECRET"),
		JWTIssuer:         getenv("JWT_ISSUER", "xosting-auth"),
		JWTAudience:       getenv("JWT_AUDIENCE", "xosting-api"),
		RedisURL:          getenv("REDIS_URL", "redis://localhost:6379/0"),
		CORSAllowedOrigin: splitList(getenv("CORS_ALLOWED_ORIGINS", "https://xosting.net,https://my.xosting.net")),
	}
	if len(cfg.JWTSecret) < 32 {
		return cfg, fmt.Errorf("JWT_SECRET must be at least 32 characters")
	}

	var err error
	if cfg.RateLimitDefault, err = atoi("RATE_LIMIT_DEFAULT", 300); err != nil {
		return cfg, err
	}
	if cfg.RateLimitAuth, err = atoi("RATE_LIMIT_AUTH", 10); err != nil {
		return cfg, err
	}
	maxBody, err := atoi("MAX_BODY_BYTES", 10<<20)
	if err != nil {
		return cfg, err
	}
	cfg.MaxBodyBytes = int64(maxBody)
	if cfg.RateLimitWindow, err = time.ParseDuration(getenv("RATE_LIMIT_WINDOW", "1m")); err != nil {
		return cfg, fmt.Errorf("RATE_LIMIT_WINDOW: %w", err)
	}
	if cfg.UpstreamTimeout, err = time.ParseDuration(getenv("UPSTREAM_TIMEOUT", "30s")); err != nil {
		return cfg, fmt.Errorf("UPSTREAM_TIMEOUT: %w", err)
	}
	for _, raw := range splitList(os.Getenv("TRUSTED_PROXIES")) {
		prefix, err := netip.ParsePrefix(raw)
		if err != nil {
			return cfg, fmt.Errorf("TRUSTED_PROXIES: %w", err)
		}
		cfg.TrustedProxies = append(cfg.TrustedProxies, prefix)
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

func splitList(raw string) []string {
	var out []string
	for _, item := range strings.Split(raw, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}
