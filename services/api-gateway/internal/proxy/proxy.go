// Package proxy builds reverse proxies to upstream services.
package proxy

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/auth"
	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/middleware"
	"github.com/shamshodbekdev/xosting-backend/shared/tracing"
)

// Identity headers are set by the gateway only; inbound copies are stripped.
var identityHeaders = []string{"X-User-ID", "X-User-Role", "X-User-Email"}

// NewTransport returns a pooled transport with sane timeouts.
func NewTransport(responseHeaderTimeout time.Duration) http.RoundTripper {
	return tracing.Transport(&http.Transport{
		Proxy:                 nil,
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:          512,
		MaxIdleConnsPerHost:   128,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: responseHeaderTimeout,
		ExpectContinueTimeout: time.Second,
	})
}

// New builds a reverse proxy to target.
func New(target *url.URL, transport http.RoundTripper, clientIP func(*http.Request) string, log *slog.Logger) *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Transport: transport,
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			pr.Out.Host = target.Host

			for _, h := range identityHeaders {
				pr.Out.Header.Del(h)
			}
			if p, ok := auth.FromContext(pr.In.Context()); ok {
				pr.Out.Header.Set("X-User-ID", p.UserID)
				pr.Out.Header.Set("X-User-Role", p.Role)
				pr.Out.Header.Set("X-User-Email", p.Email)
			}

			proto := "http"
			if pr.In.TLS != nil {
				proto = "https"
			} else if fwd := pr.In.Header.Get("X-Forwarded-Proto"); fwd == "https" {
				proto = fwd
			}
			ip := clientIP(pr.In)
			pr.Out.Header.Set("X-Forwarded-For", ip)
			pr.Out.Header.Set("X-Real-IP", ip)
			pr.Out.Header.Set("X-Forwarded-Host", pr.In.Host)
			pr.Out.Header.Set("X-Forwarded-Proto", proto)
			pr.Out.Header.Set("X-Request-ID", pr.In.Header.Get("X-Request-ID"))
		},
		ModifyResponse: func(resp *http.Response) error {
			// The gateway already set X-Request-ID; avoid a duplicate header.
			resp.Header.Del("X-Request-ID")
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			status, code, msg := http.StatusBadGateway, "bad_gateway", "Upstream service is unavailable."
			if errors.Is(err, context.DeadlineExceeded) {
				status, code, msg = http.StatusGatewayTimeout, "gateway_timeout", "Upstream service timed out."
			} else if errors.Is(err, context.Canceled) {
				return // client went away
			}
			log.ErrorContext(r.Context(), "upstream error", "upstream", target.Host, "error", err)
			middleware.WriteError(w, r, status, code, msg)
		},
	}
}
