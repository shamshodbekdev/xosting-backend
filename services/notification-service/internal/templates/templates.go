// Package templates maps domain events to rendered notifications.
package templates

import (
	"bytes"
	"fmt"
	"strings"
	"text/template"
)

// Audience decides who receives a notification.
type Audience int

const (
	// Customer notifications go to the user the event is about.
	Customer Audience = iota
	// Staff notifications go to the operations Telegram chat.
	Staff
)

// Spec describes how one event type is rendered.
type Spec struct {
	Audience Audience
	Subject  string // email subject (customer only)
	Email    string // email body (customer only)
	Telegram string // Telegram text
}

// Data is passed to every template.
type Data struct {
	EventID   string
	EventType string
	FirstName string
	Payload   map[string]any
}

// Rendered is the output of Render.
type Rendered struct {
	Audience Audience
	Subject  string
	Email    string
	Telegram string
}

const signature = "\n\n— xosting.net team\nhttps://xosting.net"

var specs = map[string]Spec{
	"user.registered": {
		Subject: "Welcome to xosting.net",
		Email: "Hi {{name .}},\n\nyour xosting.net account is ready. You can now order hosting, " +
			"register domains and open support tickets from your dashboard." + signature,
	},
	"user.password_changed": {
		Subject: "Your password was changed",
		Email: "Hi {{name .}},\n\nthe password of your xosting.net account was just changed and all " +
			"other sessions were signed out. If this was not you, contact support immediately." + signature,
		Telegram: "🔐 Your xosting.net password was changed. Not you? Contact support immediately.",
	},
	"invoice.created": {
		Subject:  "New invoice {{p . \"number\"}}",
		Email:    "Hi {{name .}},\n\ninvoice {{p . \"number\"}} for {{money .}} has been issued and is due on {{date (p . \"due_at\")}}." + signature,
		Telegram: "🧾 Invoice {{p . \"number\"}}: {{money .}}, due {{date (p . \"due_at\")}}.",
	},
	"invoice.paid": {
		Subject:  "Payment received — {{p . \"number\"}}",
		Email:    "Hi {{name .}},\n\nwe received your payment of {{money .}} for invoice {{p . \"number\"}}. Thank you!" + signature,
		Telegram: "✅ Payment received for invoice {{p . \"number\"}} ({{money .}}). Thank you!",
	},
	"invoice.overdue": {
		Subject:  "Invoice {{p . \"number\"}} is overdue",
		Email:    "Hi {{name .}},\n\ninvoice {{p . \"number\"}} ({{money .}}) is overdue. Please pay it to avoid service suspension." + signature,
		Telegram: "⚠️ Invoice {{p . \"number\"}} ({{money .}}) is overdue. Pay now to avoid suspension.",
	},
	"subscription.suspended": {
		Subject:  "Service suspended due to non-payment",
		Email:    "Hi {{name .}},\n\none of your services was suspended because an invoice remained unpaid. Pay the outstanding invoice to restore it automatically." + signature,
		Telegram: "⛔ A service was suspended due to an unpaid invoice. Pay it to restore service.",
	},
	"hosting.account.provisioned": {
		Subject: "Your hosting for {{p . \"domain\"}} is ready",
		Email: "Hi {{name .}},\n\nhosting for {{p . \"domain\"}} is active.\n\n" +
			"Control panel: {{p . \"panel_url\"}}\nUsername: {{p . \"panel_username\"}}\nServer: {{p . \"server\"}}\n\n" +
			"Use the password shown when the account was created, or reset it from your dashboard." + signature,
		Telegram: "🚀 Hosting for {{p . \"domain\"}} is ready. Panel: {{p . \"panel_url\"}} (user {{p . \"panel_username\"}}).",
	},
	"domain.registered": {
		Subject:  "{{p . \"domain\"}} is registered",
		Email:    "Hi {{name .}},\n\n{{p . \"domain\"}} is now registered to you and valid until {{date (p . \"expires_at\")}}." + signature,
		Telegram: "🌐 {{p . \"domain\"}} is registered (valid until {{date (p . \"expires_at\")}}).",
	},
	"support.ticket.replied": {
		Subject:  "New reply on your ticket: {{p . \"subject\"}}",
		Email:    "Hi {{name .}},\n\nour support team replied to \"{{p . \"subject\"}}\":\n\n{{p . \"preview\"}}\n\nOpen your dashboard to continue the conversation." + signature,
		Telegram: "💬 Support replied to \"{{p . \"subject\"}}\":\n{{p . \"preview\"}}",
	},
	"support.ticket.created": {
		Audience: Staff,
		Telegram: "🆕 Ticket [{{p . \"priority\"}}/{{p . \"department\"}}]: {{p . \"subject\"}}\nID: {{p . \"ticket_id\"}}",
	},
	"hosting.account.failed": {
		Audience: Staff,
		Telegram: "🔥 Provisioning FAILED for {{p . \"domain\"}} (account {{p . \"account_id\"}}). Check hosting-service logs.",
	},
	"domain.registration_requested": {
		Audience: Staff,
		Telegram: "📝 Domain registration requested: {{p . \"domain\"}} for {{p . \"years\"}} year(s), {{p . \"amount\"}} {{p . \"currency\"}}.",
	},
}

var funcs = template.FuncMap{
	"p": func(d Data, key string) string {
		if v, ok := d.Payload[key]; ok && v != nil {
			return fmt.Sprint(v)
		}
		return ""
	},
	"name": func(d Data) string {
		if d.FirstName != "" {
			return d.FirstName
		}
		return "there"
	},
	"money": func(d Data) string {
		amount, _ := d.Payload["total"].(string)
		currency, _ := d.Payload["currency"].(string)
		return strings.TrimSpace(amount + " " + currency)
	},
	"date": func(iso string) string {
		if len(iso) >= 10 {
			return iso[:10]
		}
		return iso
	},
}

type compiled struct {
	spec                     Spec
	subject, email, telegram *template.Template
}

// Registry holds parsed templates.
type Registry struct {
	byType map[string]compiled
}

// NewRegistry parses all templates; it fails fast on syntax errors.
func NewRegistry() (*Registry, error) {
	reg := &Registry{byType: make(map[string]compiled, len(specs))}
	for eventType, spec := range specs {
		c := compiled{spec: spec}
		var err error
		parse := func(name, text string) *template.Template {
			if text == "" || err != nil {
				return nil
			}
			var t *template.Template
			t, err = template.New(eventType + "." + name).Funcs(funcs).Option("missingkey=zero").Parse(text)
			return t
		}
		c.subject = parse("subject", spec.Subject)
		c.email = parse("email", spec.Email)
		c.telegram = parse("telegram", spec.Telegram)
		if err != nil {
			return nil, fmt.Errorf("template %s: %w", eventType, err)
		}
		reg.byType[eventType] = c
	}
	return reg, nil
}

// Supports reports whether eventType produces a notification.
func (r *Registry) Supports(eventType string) bool {
	_, ok := r.byType[eventType]
	return ok
}

// Render renders the notification for an event.
func (r *Registry) Render(eventType string, data Data) (Rendered, error) {
	c, ok := r.byType[eventType]
	if !ok {
		return Rendered{}, fmt.Errorf("no template for %s", eventType)
	}
	out := Rendered{Audience: c.spec.Audience}
	var err error
	exec := func(t *template.Template) string {
		if t == nil || err != nil {
			return ""
		}
		var buf bytes.Buffer
		err = t.Execute(&buf, data)
		return buf.String()
	}
	out.Subject = exec(c.subject)
	out.Email = exec(c.email)
	out.Telegram = exec(c.telegram)
	return out, err
}
