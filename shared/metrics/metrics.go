// Package metrics exposes Prometheus HTTP metrics using the same names and
// labels as the Python services (http_requests_total,
// http_request_duration_seconds, http_requests_in_progress).
package metrics

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// RouteFunc maps a request to a bounded route label (never the raw path).
type RouteFunc func(*http.Request) string

var (
	requestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total HTTP requests processed.",
	}, []string{"method", "route", "status"})

	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latency in seconds.",
		Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
	}, []string{"method", "route"})

	inProgress = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "http_requests_in_progress",
		Help: "HTTP requests currently being processed.",
	}, []string{"method"})
)

// Handler returns the /metrics exposition handler.
func Handler() http.Handler { return promhttp.Handler() }

// Middleware records RED metrics for every request.
func Middleware(route RouteFunc) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			gauge := inProgress.WithLabelValues(r.Method)
			gauge.Inc()
			defer gauge.Dec()

			rec := NewStatusRecorder(w)
			next.ServeHTTP(rec, r)

			label := route(r)
			requestsTotal.WithLabelValues(r.Method, label, strconv.Itoa(rec.Status())).Inc()
			requestDuration.WithLabelValues(r.Method, label).Observe(time.Since(start).Seconds())
		})
	}
}
