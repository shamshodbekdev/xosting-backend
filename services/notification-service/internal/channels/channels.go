// Package channels implements delivery channels (email, Telegram).
package channels

import (
	"context"
	"errors"
)

// Message is a rendered notification for one recipient.
type Message struct {
	To      string // email address or Telegram chat id
	Subject string // email only
	Body    string
}

// Sender delivers a message through one channel.
type Sender interface {
	Name() string
	Send(ctx context.Context, msg Message) error
}

// ErrDisabled is returned by channels that are not configured.
var ErrDisabled = errors.New("channel disabled")
