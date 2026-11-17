package channels

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// TelegramSender delivers messages through the Telegram Bot API.
type TelegramSender struct {
	baseURL string
	token   string
	http    *http.Client
}

// NewTelegramSender returns a sender; an empty token yields a disabled sender.
func NewTelegramSender(baseURL, token string, transport http.RoundTripper) *TelegramSender {
	return &TelegramSender{
		baseURL: baseURL,
		token:   token,
		http:    &http.Client{Timeout: 10 * time.Second, Transport: transport},
	}
}

// Name implements Sender.
func (t *TelegramSender) Name() string { return "telegram" }

// Enabled reports whether a bot token is configured.
func (t *TelegramSender) Enabled() bool { return t.token != "" }

type telegramResponse struct {
	OK          bool   `json:"ok"`
	Description string `json:"description"`
	Parameters  struct {
		RetryAfter int `json:"retry_after"`
	} `json:"parameters"`
}

// Send implements Sender.
func (t *TelegramSender) Send(ctx context.Context, msg Message) error {
	if !t.Enabled() {
		return ErrDisabled
	}
	payload, err := json.Marshal(map[string]any{
		"chat_id":                  msg.To,
		"text":                     msg.Body,
		"disable_web_page_preview": true,
	})
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/bot%s/sendMessage", t.baseURL, t.token)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.http.Do(req)
	if err != nil {
		// Never wrap the raw error: it contains the URL, i.e. the bot token.
		return fmt.Errorf("telegram request failed")
	}
	defer resp.Body.Close()

	var body telegramResponse
	_ = json.NewDecoder(resp.Body).Decode(&body)
	if resp.StatusCode == http.StatusOK && body.OK {
		return nil
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		return fmt.Errorf("telegram rate limited, retry after %ds", body.Parameters.RetryAfter)
	}
	return fmt.Errorf("telegram error %d: %s", resp.StatusCode, body.Description)
}
