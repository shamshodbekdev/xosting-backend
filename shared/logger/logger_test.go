package logger

import (
	"bytes"
	"context"
	"encoding/json"
	"testing"
)

func TestLoggerEmitsJSONWithServiceAndRequestID(t *testing.T) {
	var buf bytes.Buffer
	log := NewWithWriter(&buf, "test-service", "debug")

	ctx := WithRequestID(context.Background(), "req-123")
	log.InfoContext(ctx, "hello", "answer", 42)

	var entry map[string]any
	if err := json.Unmarshal(buf.Bytes(), &entry); err != nil {
		t.Fatalf("log line is not valid JSON: %v (%s)", err, buf.String())
	}
	checks := map[string]any{
		"service":    "test-service",
		"request_id": "req-123",
		"message":    "hello",
		"level":      "info",
		"answer":     float64(42),
	}
	for key, want := range checks {
		if entry[key] != want {
			t.Errorf("%s = %v, want %v", key, entry[key], want)
		}
	}
	if _, ok := entry["timestamp"]; !ok {
		t.Error("timestamp field missing")
	}
}

func TestParseLevel(t *testing.T) {
	if ParseLevel("WARNING").String() != "WARN" {
		t.Fatal("expected WARN")
	}
	if ParseLevel("nonsense").String() != "INFO" {
		t.Fatal("expected INFO default")
	}
}
