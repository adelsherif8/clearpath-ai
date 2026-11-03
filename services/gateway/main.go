// Command gateway is a small reverse proxy in front of ClearPath: per-client rate limiting,
// Prometheus metrics and a health check, so the Python app does not have to carry them.
package main

import (
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envFloat(key string, fallback float64) float64 {
	if v, err := strconv.ParseFloat(os.Getenv(key), 64); err == nil && v > 0 {
		return v
	}
	return fallback
}

func main() {
	upstream, err := url.Parse(env("UPSTREAM", "http://clearpath"))
	if err != nil {
		log.Fatalf("bad UPSTREAM: %v", err)
	}
	limiter := NewLimiter(envFloat("RATE_PER_SECOND", 20), int(envFloat("BURST", 40)), 10*time.Minute)
	go limiter.Sweep(time.Minute)

	srv := &http.Server{
		Addr:              ":" + env("PORT", "8080"),
		Handler:           NewGateway(upstream, limiter),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("gateway on %s -> %s", srv.Addr, upstream)
	log.Fatal(srv.ListenAndServe())
}
