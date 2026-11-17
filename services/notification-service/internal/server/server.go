// Package server exposes operational endpoints (probes and metrics).
package server

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/shamshodbekdev/xosting-backend/shared/metrics"
)

// New returns the HTTP server for /healthz, /readyz and /metrics.
func New(addr string, rdb *redis.Client) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := rdb.Ping(ctx).Err(); err != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{
				"status": "unavailable", "checks": map[string]string{"redis": "unavailable"},
			})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "checks": map[string]string{"redis": "ok"}})
	})
	mux.Handle("GET /metrics", metrics.Handler())

	return &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
