package templates

import (
	"strings"
	"testing"
)

func TestAllTemplatesParseAndRender(t *testing.T) {
	reg, err := NewRegistry()
	if err != nil {
		t.Fatal(err)
	}
	for eventType := range specs {
		if _, err := reg.Render(eventType, Data{Payload: map[string]any{}}); err != nil {
			t.Errorf("%s: %v", eventType, err)
		}
	}
}

func TestInvoiceTemplate(t *testing.T) {
	reg, _ := NewRegistry()
	out, err := reg.Render("invoice.created", Data{
		FirstName: "Aziz",
		Payload: map[string]any{
			"number": "INV-2026-000042", "total": "59000.00", "currency": "UZS",
			"due_at": "2026-12-01T10:00:00+05:00",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Subject != "New invoice INV-2026-000042" {
		t.Errorf("subject = %q", out.Subject)
	}
	for _, want := range []string{"Hi Aziz", "59000.00 UZS", "2026-12-01"} {
		if !strings.Contains(out.Email, want) {
			t.Errorf("email body missing %q:\n%s", want, out.Email)
		}
	}
	if out.Audience != Customer {
		t.Error("invoice notifications target the customer")
	}
}

func TestStaffAudienceAndUnknownEvents(t *testing.T) {
	reg, _ := NewRegistry()
	out, _ := reg.Render("support.ticket.created", Data{Payload: map[string]any{"subject": "Down"}})
	if out.Audience != Staff || !strings.Contains(out.Telegram, "Down") {
		t.Fatalf("unexpected render %+v", out)
	}
	if reg.Supports("billing.something.unknown") {
		t.Fatal("unknown events must not be supported")
	}
}
