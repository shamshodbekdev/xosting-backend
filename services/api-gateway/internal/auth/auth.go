// Package auth validates JWT access tokens issued by auth-service.
package auth

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// Claims are the access-token claims we rely on.
type Claims struct {
	jwt.RegisteredClaims
	TokenType string `json:"token_type"`
	Email     string `json:"email"`
	Role      string `json:"role"`
}

// Principal is the authenticated caller.
type Principal struct {
	UserID string
	Email  string
	Role   string
}

type ctxKey struct{}

// WithPrincipal stores p in ctx.
func WithPrincipal(ctx context.Context, p Principal) context.Context {
	return context.WithValue(ctx, ctxKey{}, p)
}

// FromContext returns the principal stored in ctx, if any.
func FromContext(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(ctxKey{}).(Principal)
	return p, ok
}

// Validator verifies HS256 tokens.
type Validator struct {
	parser *jwt.Parser
	secret []byte
}

// NewValidator builds a Validator enforcing issuer, audience and expiry.
func NewValidator(secret, issuer, audience string) *Validator {
	return &Validator{
		secret: []byte(secret),
		parser: jwt.NewParser(
			jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}),
			jwt.WithIssuer(issuer),
			jwt.WithAudience(audience),
			jwt.WithExpirationRequired(),
			jwt.WithIssuedAt(),
			jwt.WithLeeway(10*time.Second),
		),
	}
}

// ErrNoToken means the request carried no credentials.
var ErrNoToken = errors.New("no token")

// Validate parses and verifies a raw token.
func (v *Validator) Validate(raw string) (Principal, error) {
	claims := &Claims{}
	_, err := v.parser.ParseWithClaims(raw, claims, func(*jwt.Token) (any, error) { return v.secret, nil })
	if err != nil {
		return Principal{}, fmt.Errorf("invalid token: %w", err)
	}
	if claims.TokenType != "access" {
		return Principal{}, errors.New("invalid token: not an access token")
	}
	if claims.Subject == "" {
		return Principal{}, errors.New("invalid token: missing subject")
	}
	role := claims.Role
	if role == "" {
		role = "customer"
	}
	return Principal{UserID: claims.Subject, Email: claims.Email, Role: role}, nil
}

// TokenFromRequest extracts a bearer token. WebSocket handshakes may pass it as
// the access_token query parameter because browsers cannot set headers there.
func TokenFromRequest(r *http.Request) (string, error) {
	if header := r.Header.Get("Authorization"); header != "" {
		scheme, token, ok := strings.Cut(header, " ")
		if !ok || !strings.EqualFold(scheme, "Bearer") || token == "" {
			return "", errors.New("malformed Authorization header")
		}
		return strings.TrimSpace(token), nil
	}
	if IsWebSocket(r) {
		if token := r.URL.Query().Get("access_token"); token != "" {
			return token, nil
		}
	}
	return "", ErrNoToken
}

// IsWebSocket reports whether r is a WebSocket upgrade request.
func IsWebSocket(r *http.Request) bool {
	return strings.EqualFold(r.Header.Get("Upgrade"), "websocket") &&
		strings.Contains(strings.ToLower(r.Header.Get("Connection")), "upgrade")
}
