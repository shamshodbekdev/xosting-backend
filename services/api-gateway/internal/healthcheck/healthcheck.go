// Package healthcheck implements the `healthcheck` sub-command used by Docker
// HEALTHCHECK in distroless images (which have no shell or curl).
package healthcheck

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Probe performs GET http://127.0.0.1<port>/healthz and returns an exit code.
func Probe(addr string) int {
	port := addr
	if i := strings.LastIndex(addr, ":"); i >= 0 {
		port = addr[i:]
	}
	client := http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://127.0.0.1" + port + "/healthz")
	if err != nil {
		fmt.Println("unhealthy:", err)
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fmt.Println("unhealthy: status", resp.StatusCode)
		return 1
	}
	return 0
}
