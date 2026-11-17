// Package middleware contains the HTTP middleware chain of the gateway.
package middleware

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"strings"
	"time"

	"github.com/shamshodbekdev/xosting-backend/shared/logger"
	"github.com/shamshodbekdev/xosting-backend/shared/metrics"
)

// Middleware wraps a handler.
type Middleware func(http.Handler) http.Handler

// Chain applies middleware so that the first one listed runs first.
func Chain(h http.Handler, mws ...Middleware) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

// WriteError writes the platform-wide JSON error envelope.
func WriteError(w http.ResponseWriter, r *http.Request, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error":      map[string]any{"code": code, "message": message, "details": nil},
		"request_id": logger.RequestID(r.Context()),
	})
}

// RequestID propagates or generates X-Request-ID.
func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-ID")
		if id == "" || len(id) > 128 {
			buf := make([]byte, 16)
			_, _ = rand.Read(buf)
			id = hex.EncodeToString(buf)
			r.Header.Set("X-Request-ID", id)
		}
		w.Header().Set("X-Request-ID", id)
		next.ServeHTTP(w, r.WithContext(logger.WithRequestID(r.Context(), id)))
	})
}

// Recover turns panics into 500 responses.
func Recover(log *slog.Logger) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if rec := recover(); rec != nil {
					if rec == http.ErrAbortHandler {
						panic(rec)
					}
					log.ErrorContext(r.Context(), "panic recovered", "panic", rec)
					WriteError(w, r, http.StatusInternalServerError, "internal_error", "Internal server error.")
				}
			}()
			next.ServeHTTP(w, r)
		})
	}
}

// AccessLog writes one structured line per request (tokens redacted).
func AccessLog(log *slog.Logger, clientIP func(*http.Request) string) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			rec := metrics.NewStatusRecorder(w)
			next.ServeHTTP(rec, r)
			if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" || r.URL.Path == "/metrics" {
				return
			}
			log.InfoContext(r.Context(), "request",
				"http_method", r.Method,
				"http_path", r.URL.Path,
				"http_query", redactQuery(r.URL.RawQuery),
				"http_status", rec.Status(),
				"duration_ms", float64(time.Since(start).Microseconds())/1000,
				"client_ip", clientIP(r),
				"user_agent", r.UserAgent(),
			)
		})
	}
}

func redactQuery(raw string) string {
	if !strings.Contains(raw, "access_token=") {
		return raw
	}
	parts := strings.Split(raw, "&")
	for i, p := range parts {
		if strings.HasPrefix(p, "access_token=") {
			parts[i] = "access_token=REDACTED"
		}
	}
	return strings.Join(parts, "&")
}

// SecurityHeaders sets conservative defaults for an API.
func SecurityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
		next.ServeHTTP(w, r)
	})
}

// CORS allows browser calls from the configured origins.
func CORS(allowed []string) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			if origin != "" && (slices.Contains(allowed, origin) || slices.Contains(allowed, "*")) {
				h := w.Header()
				h.Set("Access-Control-Allow-Origin", origin)
				h.Set("Access-Control-Allow-Credentials", "true")
				h.Set("Access-Control-Expose-Headers", "X-Request-ID, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After")
				h.Add("Vary", "Origin")
				if r.Method == http.MethodOptions && r.Header.Get("Access-Control-Request-Method") != "" {
					h.Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
					h.Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, X-Request-ID")
					h.Set("Access-Control-Max-Age", "600")
					w.WriteHeader(http.StatusNoContent)
					return
				}
			}
			next.ServeHTTP(w, r)
		})
	}
}

// BodyLimit caps request bodies.
func BodyLimit(max int64) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.ContentLength > max {
				WriteError(w, r, http.StatusRequestEntityTooLarge, "payload_too_large", "Request body is too large.")
				return
			}
			if r.Body != nil {
				r.Body = http.MaxBytesReader(w, r.Body, max)
			}
			next.ServeHTTP(w, r)
		})
	}
}

// ClientIPResolver returns the real client IP. X-Forwarded-For is honoured
// only when the direct peer is a trusted proxy (e.g. the ingress controller),
// and the right-most untrusted hop wins, so clients cannot spoof their IP.
func ClientIPResolver(trusted []netip.Prefix) func(*http.Request) string {
	isTrusted := func(addr netip.Addr) bool {
		for _, p := range trusted {
			if p.Contains(addr) {
				return true
			}
		}
		return false
	}
	return func(r *http.Request) string {
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil {
			host = r.RemoteAddr
		}
		peer, err := netip.ParseAddr(host)
		if err != nil || !isTrusted(peer) {
			return host
		}
		hops := strings.Split(r.Header.Get("X-Forwarded-For"), ",")
		for i := len(hops) - 1; i >= 0; i-- {
			addr, err := netip.ParseAddr(strings.TrimSpace(hops[i]))
			if err != nil {
				break
			}
			if !isTrusted(addr) {
				return addr.String()
			}
		}
		return host
	}
}
