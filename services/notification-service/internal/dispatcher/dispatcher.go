// Package dispatcher turns events into delivered notifications.
package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/redis/go-redis/v9"

	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/channels"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/contacts"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/events"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/templates"
)

var sent = promauto.NewCounterVec(prometheus.CounterOpts{
	Name: "notifications_sent_total",
	Help: "Notifications delivered, by channel, event type and outcome.",
}, []string{"channel", "type", "outcome"})

// Dispatcher routes events to channels. Each (event, channel) pair is
// delivered at most once thanks to a Redis dedupe key, so stream redeliveries
// after partial failures never spam users.
type Dispatcher struct {
	Templates   *templates.Registry
	Contacts    contacts.Resolver
	Email       channels.Sender
	Telegram    channels.Sender
	AdminChatID string
	Redis       *redis.Client
	DedupeTTL   time.Duration
	Log         *slog.Logger
}

// Handle implements events.Handler.
func (d *Dispatcher) Handle(ctx context.Context, evt events.Event) error {
	if !d.Templates.Supports(evt.Type) {
		return nil // not every domain event is user-facing
	}

	data := templates.Data{EventID: evt.ID, EventType: evt.Type, Payload: evt.Payload}
	var contact contacts.Contact
	rendered, err := d.Templates.Render(evt.Type, data)
	if err != nil {
		return fmt.Errorf("%w: %v", events.ErrPermanent, err)
	}

	if rendered.Audience == templates.Customer {
		if evt.UserID == "" {
			return fmt.Errorf("%w: customer event without user_id", events.ErrPermanent)
		}
		contact, err = d.Contacts.Resolve(ctx, evt.UserID)
		if errors.Is(err, contacts.ErrNotFound) {
			d.Log.WarnContext(ctx, "recipient not found; skipping", "user_id", evt.UserID)
			return nil
		}
		if err != nil {
			return err // transient: retry later
		}
		if !contact.IsActive {
			return nil
		}
		data.FirstName = contact.FirstName
		if rendered, err = d.Templates.Render(evt.Type, data); err != nil {
			return fmt.Errorf("%w: %v", events.ErrPermanent, err)
		}
	}

	var errs []error
	deliver := func(sender channels.Sender, msg channels.Message) {
		if sender == nil || msg.To == "" || msg.Body == "" {
			return
		}
		if err := d.deliverOnce(ctx, evt, sender, msg); err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", sender.Name(), err))
		}
	}

	switch rendered.Audience {
	case templates.Customer:
		deliver(d.Email, channels.Message{To: contact.Email, Subject: rendered.Subject, Body: rendered.Email})
		deliver(d.Telegram, channels.Message{To: contact.TelegramChatID, Body: rendered.Telegram})
	case templates.Staff:
		deliver(d.Telegram, channels.Message{To: d.AdminChatID, Body: rendered.Telegram})
	}
	return errors.Join(errs...)
}

func (d *Dispatcher) deliverOnce(ctx context.Context, evt events.Event, sender channels.Sender, msg channels.Message) error {
	key := fmt.Sprintf("notification:sent:%s:%s", evt.ID, sender.Name())
	if done, err := d.Redis.Exists(ctx, key).Result(); err == nil && done == 1 {
		return nil
	}

	sendCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	err := sender.Send(sendCtx, msg)
	switch {
	case errors.Is(err, channels.ErrDisabled):
		sent.WithLabelValues(sender.Name(), evt.Type, "disabled").Inc()
		return nil
	case err != nil:
		sent.WithLabelValues(sender.Name(), evt.Type, "error").Inc()
		return err
	}

	sent.WithLabelValues(sender.Name(), evt.Type, "ok").Inc()
	if err := d.Redis.Set(ctx, key, 1, d.DedupeTTL).Err(); err != nil {
		d.Log.WarnContext(ctx, "failed to store dedupe key", "error", err)
	}
	d.Log.InfoContext(ctx, "notification sent", "channel", sender.Name(), "event_type", evt.Type, "event_id", evt.ID)
	return nil
}
