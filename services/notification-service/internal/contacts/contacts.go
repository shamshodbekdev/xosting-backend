// Package contacts resolves how to reach a user (email, Telegram) via the
// auth-service internal API, with a small in-memory TTL cache.
package contacts

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// ErrNotFound is returned when the user does not exist.
var ErrNotFound = errors.New("user not found")

// Contact is the projection returned by auth-service.
type Contact struct {
	ID             string `json:"id"`
	Email          string `json:"email"`
	FirstName      string `json:"first_name"`
	TelegramChatID string `json:"telegram_chat_id"`
	IsActive       bool   `json:"is_active"`
}

// Resolver looks up contacts.
type Resolver interface {
	Resolve(ctx context.Context, userID string) (Contact, error)
}

type cacheEntry struct {
	contact Contact
	expires time.Time
}

// Client talks to auth-service.
type Client struct {
	baseURL string
	token   string
	http    *http.Client
	ttl     time.Duration

	mu    sync.Mutex
	cache map[string]cacheEntry
}

// NewClient builds a Client.
func NewClient(baseURL, internalToken string, ttl time.Duration, transport http.RoundTripper) *Client {
	return &Client{
		baseURL: baseURL,
		token:   internalToken,
		http:    &http.Client{Timeout: 5 * time.Second, Transport: transport},
		ttl:     ttl,
		cache:   make(map[string]cacheEntry),
	}
}

// Resolve returns the contact for userID.
func (c *Client) Resolve(ctx context.Context, userID string) (Contact, error) {
	c.mu.Lock()
	if entry, ok := c.cache[userID]; ok && time.Now().Before(entry.expires) {
		c.mu.Unlock()
		return entry.contact, nil
	}
	c.mu.Unlock()

	endpoint := fmt.Sprintf("%s/internal/v1/users/%s/contact", c.baseURL, url.PathEscape(userID))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return Contact{}, err
	}
	req.Header.Set("X-Internal-Token", c.token)
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return Contact{}, fmt.Errorf("contact lookup: %w", err)
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode == http.StatusNotFound:
		return Contact{}, ErrNotFound
	case resp.StatusCode != http.StatusOK:
		return Contact{}, fmt.Errorf("contact lookup: unexpected status %d", resp.StatusCode)
	}

	var contact Contact
	if err := json.NewDecoder(resp.Body).Decode(&contact); err != nil {
		return Contact{}, fmt.Errorf("contact lookup: decode: %w", err)
	}

	c.mu.Lock()
	c.cache[userID] = cacheEntry{contact: contact, expires: time.Now().Add(c.ttl)}
	c.mu.Unlock()
	return contact, nil
}
