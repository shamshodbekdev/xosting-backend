package channels

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/mail"
	"strings"
	"testing"
)

func TestTelegramSendsMessage(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/botTOKEN/sendMessage" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	sender := NewTelegramSender(srv.URL, "TOKEN", nil)
	if err := sender.Send(context.Background(), Message{To: "42", Body: "hello"}); err != nil {
		t.Fatal(err)
	}
	if got["chat_id"] != "42" || got["text"] != "hello" {
		t.Fatalf("unexpected payload %v", got)
	}
}

func TestTelegramErrorsDoNotLeakToken(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"ok":false,"parameters":{"retry_after":7}}`))
	}))
	defer srv.Close()

	err := NewTelegramSender(srv.URL, "SECRET", nil).Send(context.Background(), Message{To: "1", Body: "x"})
	if err == nil || !strings.Contains(err.Error(), "retry after 7s") || strings.Contains(err.Error(), "SECRET") {
		t.Fatalf("unexpected error: %v", err)
	}
	if err := NewTelegramSender(srv.URL, "", nil).Send(context.Background(), Message{}); err != ErrDisabled {
		t.Fatalf("expected ErrDisabled, got %v", err)
	}
}

func TestBuildMIMEEncodesUnicodeSubject(t *testing.T) {
	from := &mail.Address{Name: "xosting.net", Address: "no-reply@xosting.net"}
	to := &mail.Address{Address: "ali@example.com"}
	raw := string(BuildMIME(from, to, "Hisob-faktura to‘landi", "line1\nline2"))

	for _, want := range []string{
		"Subject: =?utf-8?q?", "Content-Type: text/plain; charset=UTF-8\r\n", "\r\n\r\nline1\r\nline2",
		"Message-ID: <", "@xosting.net>",
	} {
		if !strings.Contains(raw, want) {
			t.Errorf("MIME output missing %q:\n%s", want, raw)
		}
	}
}
