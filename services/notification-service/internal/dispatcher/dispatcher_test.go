package dispatcher

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/channels"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/contacts"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/events"
	"github.com/shamshodbekdev/xosting-backend/services/notification-service/internal/templates"
)

type fakeSender struct {
	name string
	err  error
	sent []channels.Message
}

func (f *fakeSender) Name() string { return f.name }
func (f *fakeSender) Send(_ context.Context, m channels.Message) error {
	if f.err != nil {
		return f.err
	}
	f.sent = append(f.sent, m)
	return nil
}

type fakeResolver map[string]contacts.Contact

func (f fakeResolver) Resolve(_ context.Context, id string) (contacts.Contact, error) {
	if c, ok := f[id]; ok {
		return c, nil
	}
	return contacts.Contact{}, contacts.ErrNotFound
}

func newDispatcher(t *testing.T) (*Dispatcher, *fakeSender, *fakeSender) {
	t.Helper()
	reg, err := templates.NewRegistry()
	if err != nil {
		t.Fatal(err)
	}
	mr := miniredis.RunT(t)
	email, tg := &fakeSender{name: "email"}, &fakeSender{name: "telegram"}
	return &Dispatcher{
		Templates: reg,
		Contacts: fakeResolver{"u1": {
			ID: "u1", Email: "ali@example.com", FirstName: "Ali", TelegramChatID: "777", IsActive: true,
		}},
		Email:       email,
		Telegram:    tg,
		AdminChatID: "-100500",
		Redis:       redis.NewClient(&redis.Options{Addr: mr.Addr()}),
		DedupeTTL:   time.Hour,
		Log:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, email, tg
}

func invoicePaid() events.Event {
	return events.Event{
		ID: "evt-1", Type: "invoice.paid", UserID: "u1",
		Payload: map[string]any{"number": "INV-1", "total": "1000.00", "currency": "UZS"},
	}
}

func TestCustomerEventGoesToEmailAndTelegram(t *testing.T) {
	d, email, tg := newDispatcher(t)
	if err := d.Handle(context.Background(), invoicePaid()); err != nil {
		t.Fatal(err)
	}
	if len(email.sent) != 1 || email.sent[0].To != "ali@example.com" {
		t.Fatalf("email not sent: %+v", email.sent)
	}
	if len(tg.sent) != 1 || tg.sent[0].To != "777" {
		t.Fatalf("telegram not sent: %+v", tg.sent)
	}
}

func TestRedeliveryDoesNotDuplicateSuccessfulChannels(t *testing.T) {
	d, email, tg := newDispatcher(t)
	tg.err = errors.New("telegram down")

	if err := d.Handle(context.Background(), invoicePaid()); err == nil {
		t.Fatal("expected error so the event is retried")
	}
	tg.err = nil
	if err := d.Handle(context.Background(), invoicePaid()); err != nil {
		t.Fatal(err)
	}
	if len(email.sent) != 1 {
		t.Fatalf("email delivered %d times, want exactly once", len(email.sent))
	}
	if len(tg.sent) != 1 {
		t.Fatalf("telegram delivered %d times, want 1", len(tg.sent))
	}
}

func TestStaffEventGoesToAdminChat(t *testing.T) {
	d, email, tg := newDispatcher(t)
	evt := events.Event{ID: "e2", Type: "support.ticket.created", Payload: map[string]any{"subject": "Help"}}
	if err := d.Handle(context.Background(), evt); err != nil {
		t.Fatal(err)
	}
	if len(email.sent) != 0 || len(tg.sent) != 1 || tg.sent[0].To != "-100500" {
		t.Fatalf("unexpected deliveries email=%v tg=%v", email.sent, tg.sent)
	}
}

func TestUnknownEventsAndRecipientsAreSkipped(t *testing.T) {
	d, email, tg := newDispatcher(t)
	_ = d.Handle(context.Background(), events.Event{ID: "e3", Type: "internal.audit"})
	evt := invoicePaid()
	evt.UserID = "ghost"
	if err := d.Handle(context.Background(), evt); err != nil {
		t.Fatal(err)
	}
	if len(email.sent)+len(tg.sent) != 0 {
		t.Fatal("nothing should be sent")
	}
}
