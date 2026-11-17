package metrics

import (
	"bufio"
	"errors"
	"net"
	"net/http"
)

// StatusRecorder captures the response status while remaining transparent
// for http.Hijacker (WebSocket upgrades) and http.Flusher (streaming).
type StatusRecorder struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
}

// NewStatusRecorder wraps w.
func NewStatusRecorder(w http.ResponseWriter) *StatusRecorder {
	return &StatusRecorder{ResponseWriter: w, status: http.StatusOK}
}

// Status returns the recorded status code.
func (r *StatusRecorder) Status() int { return r.status }

// WriteHeader records the status code.
func (r *StatusRecorder) WriteHeader(code int) {
	if !r.wroteHeader {
		r.status = code
		r.wroteHeader = true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *StatusRecorder) Write(b []byte) (int, error) {
	r.wroteHeader = true
	return r.ResponseWriter.Write(b)
}

// Flush implements http.Flusher.
func (r *StatusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Hijack implements http.Hijacker so WebSocket upgrades keep working.
func (r *StatusRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	h, ok := r.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("response writer does not support hijacking")
	}
	r.status = http.StatusSwitchingProtocols
	r.wroteHeader = true
	return h.Hijack()
}

// Unwrap supports http.ResponseController.
func (r *StatusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }
