package gateway

import (
	"bufio"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/golang-jwt/jwt/v5"
	"github.com/redis/go-redis/v9"

	"github.com/shamshodbekdev/xosting-backend/services/api-gateway/internal/config"
)

const secret = "test-jwt-secret-with-at-least-32-bytes!!"

type echo struct {
	Service string            `json:"service"`
	Path    string            `json:"path"`
	Headers map[string]string `json:"headers"`
}

func upstream(t *testing.T, name string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Upgrade") == "websocket" {
			hijackEcho(t, w)
			return
		}
		headers := map[string]string{}
		for _, h := range []string{"X-User-ID", "X-User-Role", "X-Request-ID", "X-Forwarded-For"} {
			headers[h] = r.Header.Get(h)
		}
		_ = json.NewEncoder(w).Encode(echo{Service: name, Path: r.URL.Path, Headers: headers})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// hijackEcho completes a fake WebSocket handshake and echoes raw bytes.
func hijackEcho(t *testing.T, w http.ResponseWriter) {
	conn, buf, err := w.(http.Hijacker).Hijack()
	if err != nil {
		t.Error(err)
		return
	}
	defer conn.Close()
	_, _ = buf.WriteString("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
	_ = buf.Flush()
	line, _ := buf.ReadString('\n')
	_, _ = buf.WriteString("echo:" + line)
	_ = buf.Flush()
}

func newGateway(t *testing.T, mutate func(*config.Config)) (*httptest.Server, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	cfg := config.Config{
		Upstreams: config.Upstreams{
			Auth: upstream(t, "auth").URL, Billing: upstream(t, "billing").URL,
			Hosting: upstream(t, "hosting").URL, Domain: upstream(t, "domain").URL,
			Support: upstream(t, "support").URL,
		},
		UpstreamTimeout:   5 * time.Second,
		JWTSecret:         secret,
		JWTIssuer:         "xosting-auth",
		JWTAudience:       "xosting-api",
		RateLimitDefault:  100,
		RateLimitAuth:     3,
		RateLimitWindow:   time.Minute,
		CORSAllowedOrigin: []string{"https://xosting.net"},
		MaxBodyBytes:      1 << 20,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	g, err := New(cfg, redis.NewClient(&redis.Options{Addr: mr.Addr()}), nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(g.Handler())
	t.Cleanup(srv.Close)
	return srv, mr
}

func token(t *testing.T, sub, role, tokenType string, exp time.Duration) string {
	t.Helper()
	now := time.Now()
	signed, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": sub, "role": role, "token_type": tokenType, "iss": "xosting-auth", "aud": "xosting-api",
		"iat": now.Unix(), "exp": now.Add(exp).Unix(),
	}).SignedString([]byte(secret))
	if err != nil {
		t.Fatal(err)
	}
	return signed
}

func do(t *testing.T, method, url, bearer string, headers map[string]string) (*http.Response, echo) {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body echo
	_ = json.NewDecoder(resp.Body).Decode(&body)
	return resp, body
}

func TestRoutesToUpstreamsAndInjectsIdentity(t *testing.T) {
	gw, _ := newGateway(t, nil)
	tok := token(t, "user-1", "customer", "access", time.Minute)

	cases := map[string]string{
		"/api/v1/billing/invoices":    "billing",
		"/api/v1/payments/checkout":   "billing",
		"/api/v1/hosting/accounts":    "hosting",
		"/api/v1/domains":             "domain",
		"/api/v1/support/tickets/abc": "support",
		"/api/v1/auth/me":             "auth",
	}
	for path, service := range cases {
		resp, body := do(t, http.MethodGet, gw.URL+path, tok, map[string]string{"X-User-ID": "spoofed"})
		if resp.StatusCode != http.StatusOK || body.Service != service || body.Path != path {
			t.Fatalf("%s -> %d %+v", path, resp.StatusCode, body)
		}
		if body.Headers["X-User-ID"] != "user-1" || body.Headers["X-User-Role"] != "customer" {
			t.Fatalf("identity headers not injected/sanitised: %+v", body.Headers)
		}
		if body.Headers["X-Request-ID"] == "" || resp.Header.Get("X-Request-ID") != body.Headers["X-Request-ID"] {
			t.Fatal("request id must be generated and propagated")
		}
	}
}

func TestAuthenticationPolicy(t *testing.T) {
	gw, _ := newGateway(t, nil)

	if resp, _ := do(t, http.MethodGet, gw.URL+"/api/v1/billing/invoices", "", nil); resp.StatusCode != 401 {
		t.Fatalf("missing token: got %d", resp.StatusCode)
	}
	if resp, _ := do(t, http.MethodGet, gw.URL+"/api/v1/billing/invoices", "garbage", nil); resp.StatusCode != 401 {
		t.Fatalf("garbage token: got %d", resp.StatusCode)
	}
	expired := token(t, "u", "customer", "access", -time.Hour)
	if resp, _ := do(t, http.MethodGet, gw.URL+"/api/v1/billing/invoices", expired, nil); resp.StatusCode != 401 {
		t.Fatalf("expired token: got %d", resp.StatusCode)
	}
	refresh := token(t, "u", "customer", "refresh", time.Hour)
	if resp, _ := do(t, http.MethodGet, gw.URL+"/api/v1/billing/invoices", refresh, nil); resp.StatusCode != 401 {
		t.Fatalf("refresh token used as access token: got %d", resp.StatusCode)
	}
	// Public endpoints work anonymously; an invalid token there is ignored, not fatal.
	if resp, body := do(t, http.MethodGet, gw.URL+"/api/v1/billing/plans", "", nil); resp.StatusCode != 200 || body.Service != "billing" {
		t.Fatalf("public plans: %d", resp.StatusCode)
	}
	if resp, body := do(t, http.MethodPost, gw.URL+"/api/v1/payments/webhook/click/", "", nil); resp.StatusCode != 200 || body.Service != "billing" {
		t.Fatalf("public payment webhook: %d", resp.StatusCode)
	}
	if resp, _ := do(t, http.MethodPost, gw.URL+"/api/v1/payments/checkout", "", nil); resp.StatusCode != 401 {
		t.Fatalf("checkout requires a token: got %d", resp.StatusCode)
	}
	if resp, _ := do(t, http.MethodGet, gw.URL+"/api/v1/domains/availability?name=a.uz", "garbage", nil); resp.StatusCode != 200 {
		t.Fatalf("public availability: %d", resp.StatusCode)
	}
}

func TestUnknownAndInternalPathsAre404(t *testing.T) {
	gw, _ := newGateway(t, nil)
	tok := token(t, "u", "admin", "access", time.Minute)
	for _, path := range []string{"/internal/v1/users/x/contact", "/api/v2/anything", "/api/v1/authz"} {
		if resp, _ := do(t, http.MethodGet, gw.URL+path, tok, nil); resp.StatusCode != 404 {
			t.Fatalf("%s: got %d", path, resp.StatusCode)
		}
	}
}

func TestCredentialEndpointsAreRateLimitedPerIP(t *testing.T) {
	gw, _ := newGateway(t, nil)
	for i := 0; i < 3; i++ {
		if resp, _ := do(t, http.MethodPost, gw.URL+"/api/v1/auth/token", "", nil); resp.StatusCode != 200 {
			t.Fatalf("attempt %d: %d", i, resp.StatusCode)
		}
	}
	resp, _ := do(t, http.MethodPost, gw.URL+"/api/v1/auth/token", "", nil)
	if resp.StatusCode != http.StatusTooManyRequests || resp.Header.Get("Retry-After") == "" {
		t.Fatalf("expected 429 with Retry-After, got %d", resp.StatusCode)
	}
	if resp.Header.Get("X-RateLimit-Remaining") != "0" {
		t.Fatalf("remaining = %q", resp.Header.Get("X-RateLimit-Remaining"))
	}
}

func TestRateLimiterFailsOpen(t *testing.T) {
	gw, mr := newGateway(t, nil)
	mr.Close()
	if resp, _ := do(t, http.MethodGet, gw.URL+"/api/v1/billing/plans", "", nil); resp.StatusCode != 200 {
		t.Fatalf("redis outage must not take the API down: %d", resp.StatusCode)
	}
}

func TestCORSPreflight(t *testing.T) {
	gw, _ := newGateway(t, nil)
	resp, _ := do(t, http.MethodOptions, gw.URL+"/api/v1/billing/invoices", "", map[string]string{
		"Origin": "https://xosting.net", "Access-Control-Request-Method": "POST",
	})
	if resp.StatusCode != http.StatusNoContent || resp.Header.Get("Access-Control-Allow-Origin") != "https://xosting.net" {
		t.Fatalf("preflight failed: %d %v", resp.StatusCode, resp.Header)
	}
	resp, _ = do(t, http.MethodOptions, gw.URL+"/api/v1/billing/invoices", "", map[string]string{
		"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
	})
	if resp.Header.Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("untrusted origin must not be allowed")
	}
}

func TestWebSocketUpgradeIsProxiedWithQueryToken(t *testing.T) {
	gw, _ := newGateway(t, nil)
	tok := token(t, "user-ws", "customer", "access", time.Minute)

	conn, err := net.Dial("tcp", strings.TrimPrefix(gw.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	_, _ = conn.Write([]byte("GET /ws/support/tickets/1?access_token=" + tok + " HTTP/1.1\r\n" +
		"Host: gateway\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"))

	reader := bufio.NewReader(conn)
	resp, err := http.ReadResponse(reader, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", resp.StatusCode)
	}
	_, _ = conn.Write([]byte("hello\n"))
	line, _ := reader.ReadString('\n')
	if line != "echo:hello\n" {
		t.Fatalf("unexpected tunnel payload %q", line)
	}
}

func TestWebSocketWithoutTokenIsRejected(t *testing.T) {
	gw, _ := newGateway(t, nil)
	resp, _ := do(t, http.MethodGet, gw.URL+"/ws/support/tickets/1", "", map[string]string{
		"Upgrade": "websocket", "Connection": "Upgrade",
	})
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("got %d", resp.StatusCode)
	}
}

func TestRequestIDHeaderIsNotDuplicated(t *testing.T) {
	upstreamWithID := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Request-ID", r.Header.Get("X-Request-ID"))
	}))
	defer upstreamWithID.Close()
	gw2, _ := newGateway(t, func(c *config.Config) { c.Upstreams.Billing = upstreamWithID.URL })
	resp, _ := do(t, http.MethodGet, gw2.URL+"/api/v1/billing/plans", "", nil)
	if got := resp.Header.Values("X-Request-ID"); len(got) != 1 {
		t.Fatalf("expected exactly one X-Request-ID, got %v", got)
	}
}
