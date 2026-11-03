package main

import (
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	requests = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gateway_requests_total", Help: "Requests handled, by response code.",
	}, []string{"code"})
	limited = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "gateway_rate_limited_total", Help: "Requests refused with 429.",
	})
	latency = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name: "gateway_request_duration_seconds", Help: "Time to answer, including the upstream.",
		Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5},
	})
)

// NewGateway wires health, metrics and the rate-limited proxy into one handler.
func NewGateway(upstream *url.URL, limiter *Limiter) http.Handler {
	reg := prometheus.NewRegistry()
	reg.MustRegister(requests, limited, latency)

	proxy := httputil.NewSingleHostReverseProxy(upstream)
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, _ error) {
		http.Error(w, "upstream unavailable", http.StatusBadGateway)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })
	mux.Handle("/gateway/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, code: http.StatusOK}
		if !limiter.Allow(clientIP(r)) {
			limited.Inc()
			rec.Header().Set("Retry-After", "1")
			http.Error(rec, "too many requests", http.StatusTooManyRequests)
		} else {
			proxy.ServeHTTP(rec, r)
		}
		requests.WithLabelValues(strconv.Itoa(rec.code)).Inc()
		latency.Observe(time.Since(start).Seconds())
	})
	return mux
}

// clientIP is the left-most X-Forwarded-For entry set by the ingress, else the socket peer.
// The gateway is only reachable through the ingress, so the header is not client-controlled.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		return strings.TrimSpace(strings.Split(xff, ",")[0])
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

type statusRecorder struct {
	http.ResponseWriter
	code int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.code = code
	s.ResponseWriter.WriteHeader(code)
}
