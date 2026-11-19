package gateway

import (
	"net/http"
	"net/http/httputil"
	"strings"
)

// Route maps a path prefix to an upstream service.
type Route struct {
	Name     string
	Prefix   string // without trailing slash, e.g. "/api/v1/billing"
	Upstream string
	proxy    *httputil.ReverseProxy
}

func (rt *Route) matches(path string) bool {
	return path == rt.Prefix || strings.HasPrefix(path, rt.Prefix+"/")
}

// publicRule marks endpoints reachable without a JWT.
type publicRule struct {
	method string // "" = any
	path   string
	prefix bool
}

func (p publicRule) matches(r *http.Request) bool {
	if p.method != "" && r.Method != p.method {
		return false
	}
	if p.prefix {
		return r.URL.Path == p.path || strings.HasPrefix(r.URL.Path, p.path+"/")
	}
	return r.URL.Path == p.path
}

// publicRules lists anonymous endpoints. Everything else requires a valid
// access token at the edge (services re-validate it: defence in depth).
var publicRules = []publicRule{
	{method: http.MethodPost, path: "/api/v1/auth/register"},
	{method: http.MethodPost, path: "/api/v1/auth/token"},
	{method: http.MethodPost, path: "/api/v1/auth/token/refresh"},
	{method: http.MethodPost, path: "/api/v1/auth/token/verify"},
	{path: "/api/v1/auth/admin", prefix: true},  // Django admin: session auth
	{path: "/api/v1/auth/static", prefix: true}, // admin assets
	{method: http.MethodGet, path: "/api/v1/billing/plans", prefix: true},
	{path: "/api/v1/billing/admin", prefix: true},  // Django admin: session auth
	{path: "/api/v1/billing/static", prefix: true}, // admin assets
	{method: http.MethodGet, path: "/api/v1/payments/providers", prefix: true},
	// Provider callbacks authenticate with provider-specific credentials/signatures.
	{method: http.MethodPost, path: "/api/v1/payments/webhook", prefix: true},
	{method: http.MethodGet, path: "/api/v1/domains/tlds"},
	{method: http.MethodGet, path: "/api/v1/domains/availability"},
	// API documentation of every service.
	{method: http.MethodGet, path: "/api/v1/auth/docs", prefix: true},
	{method: http.MethodGet, path: "/api/v1/auth/schema", prefix: true},
	{method: http.MethodGet, path: "/api/v1/billing/docs", prefix: true},
	{method: http.MethodGet, path: "/api/v1/billing/schema", prefix: true},
	{method: http.MethodGet, path: "/api/v1/hosting/docs", prefix: true},
	{method: http.MethodGet, path: "/api/v1/hosting/openapi.json"},
	{method: http.MethodGet, path: "/api/v1/domains/docs", prefix: true},
	{method: http.MethodGet, path: "/api/v1/domains/openapi.json"},
	{method: http.MethodGet, path: "/api/v1/support/docs", prefix: true},
	{method: http.MethodGet, path: "/api/v1/support/openapi.json"},
}

// credentialRules get the strict per-IP rate limit (brute-force protection).
var credentialRules = []publicRule{
	{method: http.MethodPost, path: "/api/v1/auth/register"},
	{method: http.MethodPost, path: "/api/v1/auth/token"},
	{method: http.MethodPost, path: "/api/v1/auth/token/refresh"},
	{method: http.MethodPost, path: "/api/v1/auth/password/change"},
}

func matchesAny(rules []publicRule, r *http.Request) bool {
	for _, rule := range rules {
		if rule.matches(r) {
			return true
		}
	}
	return false
}
