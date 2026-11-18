// Package gateway wires routing, authentication, rate limiting and proxying.
package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/redis/go-redis/v9"

	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/auth"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/config"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/middleware"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/proxy"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/ratelimit"
	"github.com/shamshodbekdev/xosting-backend/shared/metrics"
	"github.com/shamshodbekdev/xosting-backend/shared/tracing"
)

var (
	authFailures = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gateway_auth_failures_total",
		Help: "Requests rejected by JWT validation.",
	}, []string{"reason"})
	rateLimited = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gateway_rate_limited_total",
		Help: "Requests rejected by the rate limiter.",
	}, []string{"bucket"})
	limiterErrors = promauto.NewCounter(prometheus.CounterOpts{
		Name: "gateway_rate_limiter_errors_total",
		Help: "Rate-limiter backend errors (requests are allowed: fail-open).",
	})
)

// Gateway is the edge HTTP handler.
type Gateway struct {
	cfg       config.Config
	routes    []*Route
	validator *auth.Validator
	limiter   *ratelimit.Limiter
	rdb       *redis.Client
	clientIP  func(*http.Request) string
	log       *slog.Logger
}

// New builds a Gateway. transport may be nil (a pooled default is used).
func New(cfg config.Config, rdb *redis.Client, transport http.RoundTripper, log *slog.Logger) (*Gateway, error) {
	if transport == nil {
		transport = proxy.NewTransport(cfg.UpstreamTimeout)
	}
	g := &Gateway{
		cfg:       cfg,
		validator: auth.NewValidator(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience),
		limiter:   ratelimit.New(rdb, cfg.RateLimitWindow),
		rdb:       rdb,
		clientIP:  middleware.ClientIPResolver(cfg.TrustedProxies),
		log:       log,
		routes: []*Route{
			{Name: "auth", Prefix: "/api/v1/auth", Upstream: cfg.Upstreams.Auth},
			{Name: "billing", Prefix: "/api/v1/billing", Upstream: cfg.Upstreams.Billing},
			{Name: "payments", Prefix: "/api/v1/payments", Upstream: cfg.Upstreams.Billing},
			{Name: "hosting", Prefix: "/api/v1/hosting", Upstream: cfg.Upstreams.Hosting},
			{Name: "domains", Prefix: "/api/v1/domains", Upstream: cfg.Upstreams.Domain},
			{Name: "support", Prefix: "/api/v1/support", Upstream: cfg.Upstreams.Support},
			{Name: "support-ws", Prefix: "/ws/support", Upstream: cfg.Upstreams.Support},
		},
	}
	for _, rt := range g.routes {
		target, err := url.Parse(rt.Upstream)
		if err != nil || target.Scheme == "" || target.Host == "" {
			return nil, fmt.Errorf("route %s: invalid upstream %q", rt.Name, rt.Upstream)
		}
		rt.proxy = proxy.New(target, transport, g.clientIP, log)
	}
	return g, nil
}

// Handler returns the fully wrapped HTTP handler.
func (g *Gateway) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", g.readyz)
	mux.Handle("GET /metrics", metrics.Handler())
	mux.Handle("/", http.HandlerFunc(g.serveAPI))

	return middleware.Chain(mux,
		middleware.Recover(g.log),
		middleware.RequestID,
		tracing.Middleware("api-gateway"),
		metrics.Middleware(g.routeLabel),
		middleware.AccessLog(g.log, g.clientIP),
		middleware.SecurityHeaders,
		middleware.CORS(g.cfg.CORSAllowedOrigin),
		middleware.BodyLimit(g.cfg.MaxBodyBytes),
	)
}

func (g *Gateway) match(path string) *Route {
	for _, rt := range g.routes {
		if rt.matches(path) {
			return rt
		}
	}
	return nil
}

func (g *Gateway) routeLabel(r *http.Request) string {
	switch r.URL.Path {
	case "/healthz", "/readyz", "/metrics":
		return r.URL.Path
	}
	if rt := g.match(r.URL.Path); rt != nil {
		return rt.Name
	}
	return "<unmatched>"
}

func (g *Gateway) serveAPI(w http.ResponseWriter, r *http.Request) {
	rt := g.match(r.URL.Path)
	if rt == nil {
		middleware.WriteError(w, r, http.StatusNotFound, "not_found", "No route for this path.")
		return
	}

	public := matchesAny(publicRules, r)
	token, err := auth.TokenFromRequest(r)
	switch {
	case err == nil:
		principal, verr := g.validator.Validate(token)
		if verr != nil {
			if !public {
				authFailures.WithLabelValues("invalid").Inc()
				w.Header().Set("WWW-Authenticate", `Bearer error="invalid_token"`)
				middleware.WriteError(w, r, http.StatusUnauthorized, "not_authenticated", "Invalid or expired access token.")
				return
			}
		} else {
			r = r.WithContext(auth.WithPrincipal(r.Context(), principal))
		}
	case errors.Is(err, auth.ErrNoToken):
		if !public {
			authFailures.WithLabelValues("missing").Inc()
			w.Header().Set("WWW-Authenticate", "Bearer")
			middleware.WriteError(w, r, http.StatusUnauthorized, "not_authenticated", "Authentication credentials were not provided.")
			return
		}
	default:
		authFailures.WithLabelValues("malformed").Inc()
		middleware.WriteError(w, r, http.StatusUnauthorized, "not_authenticated", err.Error())
		return
	}

	if !g.allow(w, r) {
		return
	}
	rt.proxy.ServeHTTP(w, r)
}

// allow applies rate limits: strict per-IP limits for credential endpoints,
// otherwise a per-user (authenticated) or per-IP (anonymous) budget.
func (g *Gateway) allow(w http.ResponseWriter, r *http.Request) bool {
	bucket, limit := "default", g.cfg.RateLimitDefault
	subject := "ip:" + g.clientIP(r)
	if matchesAny(credentialRules, r) {
		bucket, limit = "credentials", g.cfg.RateLimitAuth
	} else if p, ok := auth.FromContext(r.Context()); ok {
		subject = "user:" + p.UserID
	}

	ctx, cancel := context.WithTimeout(r.Context(), 250*time.Millisecond)
	defer cancel()
	res, err := g.limiter.Allow(ctx, bucket+":"+subject, limit)
	if err != nil {
		limiterErrors.Inc()
		g.log.WarnContext(r.Context(), "rate limiter unavailable; allowing request", "error", err)
		return true
	}

	h := w.Header()
	h.Set("X-RateLimit-Limit", strconv.Itoa(res.Limit))
	h.Set("X-RateLimit-Remaining", strconv.Itoa(res.Remaining))
	reset := int(res.ResetIn.Round(time.Second).Seconds())
	h.Set("X-RateLimit-Reset", strconv.Itoa(reset))
	if !res.Allowed {
		rateLimited.WithLabelValues(bucket).Inc()
		h.Set("Retry-After", strconv.Itoa(max(reset, 1)))
		middleware.WriteError(w, r, http.StatusTooManyRequests, "throttled", "Too many requests. Slow down.")
		return false
	}
	return true
}

func (g *Gateway) readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := g.rdb.Ping(ctx).Err(); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"status": "unavailable", "checks": map[string]string{"redis": "unavailable"},
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "checks": map[string]string{"redis": "ok"}})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
