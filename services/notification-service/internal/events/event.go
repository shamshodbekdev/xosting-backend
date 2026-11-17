// Package events models domain events published on the Redis stream and
// implements a consumer-group reader with retries and dead-lettering.
package events

import (
	"encoding/json"
	"fmt"
)

// Event is the envelope written by every service (see shared/events).
type Event struct {
	StreamID   string         `json:"-"`
	ID         string         `json:"id"`
	Type       string         `json:"type"`
	Source     string         `json:"source"`
	OccurredAt string         `json:"occurred_at"`
	UserID     string         `json:"user_id"`
	Payload    map[string]any `json:"payload"`
}

// FromStream decodes a Redis stream entry.
func FromStream(streamID string, values map[string]any) (Event, error) {
	str := func(key string) string {
		if v, ok := values[key].(string); ok {
			return v
		}
		return ""
	}
	evt := Event{
		StreamID:   streamID,
		ID:         str("id"),
		Type:       str("type"),
		Source:     str("source"),
		OccurredAt: str("occurred_at"),
		UserID:     str("user_id"),
		Payload:    map[string]any{},
	}
	if evt.ID == "" || evt.Type == "" {
		return evt, fmt.Errorf("stream entry %s: missing id or type", streamID)
	}
	if raw := str("payload"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &evt.Payload); err != nil {
			return evt, fmt.Errorf("stream entry %s: invalid payload: %w", streamID, err)
		}
	}
	return evt, nil
}
