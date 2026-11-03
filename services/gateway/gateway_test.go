package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestBurstThenRefuse(t *testing.T) {
	l := NewLimiter(1, 3, time.Minute)
	frozen := time.Unix(1_700_000_000, 0)
	l.now = func() time.Time { return frozen }
	for i := 0; i < 3; i++ {
		if !l.Allow("a") {
			t.Fatalf("request %d inside the burst was refused", i+1)
		}
	}
	if l.Allow("a") {
		t.Fatal("fourth request at the same instant should be refused")
	}
	if !l.Allow("b") {
		t.Fatal("another client must have its own bucket")
	}
	frozen = frozen.Add(time.Second)
	if !l.Allow("a") {
		t.Fatal("one token should refill after a second")
	}
}

func TestIdleClientsAreForgotten(t *testing.T) {
	l := NewLimiter(1, 1, time.Minute)
	now := time.Unix(1_700_000_000, 0)
	l.now = func() time.Time { return now }
	l.Allow("old")
	now = now.Add(2 * time.Minute)
	l.Allow("new")
	l.Evict()
	if l.Clients() != 1 {
		t.Fatalf("want only the recent client kept, have %d", l.Clients())
	}
}

func gatewayFor(t *testing.T, burst int) (*httptest.Server, *httptest.Server) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "upstream saw "+r.URL.Path)
	}))
	u, _ := url.Parse(upstream.URL)
	gw := httptest.NewServer(NewGateway(u, NewLimiter(0.001, burst, time.Minute)))
	t.Cleanup(func() { gw.Close(); upstream.Close() })
	return gw, upstream
}

func get(t *testing.T, url, xff string) (int, string) {
	req, _ := http.NewRequest("GET", url, nil)
	if xff != "" {
		req.Header.Set("X-Forwarded-For", xff)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	b, _ := io.ReadAll(res.Body)
	return res.StatusCode, string(b)
}

func TestProxiesThenLimitsPerClient(t *testing.T) {
	gw, _ := gatewayFor(t, 2)
	for i := 0; i < 2; i++ {
		if code, body := get(t, gw.URL+"/api/samples", "203.0.113.7"); code != 200 || body != "upstream saw /api/samples" {
			t.Fatalf("request %d: %d %q", i+1, code, body)
		}
	}
	if code, _ := get(t, gw.URL+"/api/samples", "203.0.113.7"); code != http.StatusTooManyRequests {
		t.Fatalf("third request should be 429, got %d", code)
	}
	if code, _ := get(t, gw.URL+"/", "198.51.100.2, 10.0.0.1"); code != 200 {
		t.Fatalf("a different client should pass, got %d", code)
	}
}

func TestHealthAndMetricsAreNotLimited(t *testing.T) {
	gw, _ := gatewayFor(t, 1)
	get(t, gw.URL+"/x", "192.0.2.1")
	get(t, gw.URL+"/x", "192.0.2.1")
	for i := 0; i < 5; i++ {
		if code, _ := get(t, gw.URL+"/healthz", "192.0.2.1"); code != 200 {
			t.Fatalf("health check limited: %d", code)
		}
	}
	code, body := get(t, gw.URL+"/gateway/metrics", "")
	if code != 200 || !strings.Contains(body, "gateway_rate_limited_total") || !strings.Contains(body, `gateway_requests_total{code="429"}`) {
		t.Fatalf("metrics missing: %d\n%s", code, body)
	}
}

func TestUpstreamDownIsA502(t *testing.T) {
	u, _ := url.Parse("http://127.0.0.1:1")
	gw := httptest.NewServer(NewGateway(u, NewLimiter(10, 10, time.Minute)))
	defer gw.Close()
	if code, _ := get(t, gw.URL+"/", ""); code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d", code)
	}
}
