# Gateway (Go)

A small reverse proxy that sits between the nginx Ingress and the ClearPath app in Kubernetes.

- **Per-client rate limit:** a token bucket per client IP (`golang.org/x/time/rate`), taken from the left-most
  `X-Forwarded-For` entry the Ingress sets. Over the limit: `429 Too Many Requests` with `Retry-After: 1`.
  Idle clients are forgotten after 10 minutes. Each replica keeps its own buckets, so with 2 replicas the
  effective limit is up to twice the configured rate.
- **Metrics:** `gateway_requests_total{code}`, `gateway_rate_limited_total` and a latency histogram on
  `/gateway/metrics`, scraped by a ServiceMonitor and shown on the ClearPath Grafana dashboard.
- **Health:** `/healthz` (never rate limited). Upstream down: `502`.
- **Image:** static binary on distroless, non-root, read-only root filesystem, about 19 MB.

| Env | Default | |
|---|---|---|
| `UPSTREAM` | `http://clearpath` | the app's Service |
| `RATE_PER_SECOND` | `20` | refill rate per client |
| `BURST` | `40` | bucket size |
| `PORT` | `8080` | |

```bash
go test -race ./...                       # or: docker run --rm -v "$PWD":/src -w /src golang:1.27-alpine go test ./...
docker build -t clearpath-gateway:local .
kind load docker-image clearpath-gateway:local --name clearpath
```

Checked on the local kind cluster with the limit lowered to 2/s, burst 5: 40 parallel requests through the
Ingress gave 6 × 200 and 34 × 429.
