package middleware

import (
	"net/http/httptest"
	"net/netip"
	"testing"
)

func TestClientIPIgnoresSpoofedHeadersFromUntrustedPeers(t *testing.T) {
	resolve := ClientIPResolver([]netip.Prefix{netip.MustParsePrefix("10.0.0.0/8")})

	direct := httptest.NewRequest("GET", "/", nil)
	direct.RemoteAddr = "203.0.113.7:5555"
	direct.Header.Set("X-Forwarded-For", "1.1.1.1")
	if got := resolve(direct); got != "203.0.113.7" {
		t.Fatalf("untrusted peer: got %s", got)
	}

	viaIngress := httptest.NewRequest("GET", "/", nil)
	viaIngress.RemoteAddr = "10.1.2.3:443"
	viaIngress.Header.Set("X-Forwarded-For", "6.6.6.6, 198.51.100.4, 10.0.0.9")
	if got := resolve(viaIngress); got != "198.51.100.4" {
		t.Fatalf("trusted proxy: got %s", got)
	}
}

func TestRedactQuery(t *testing.T) {
	if got := redactQuery("a=1&access_token=secret&b=2"); got != "a=1&access_token=REDACTED&b=2" {
		t.Fatalf("got %s", got)
	}
}
