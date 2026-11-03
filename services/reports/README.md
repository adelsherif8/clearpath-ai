# Reports API (ASP.NET Core, .NET 10)

A small C# service next to ClearPath in Kubernetes. A background worker takes a snapshot of the app's
evaluation results (`GET /metrics` on the app) every few minutes and stores it with EF Core in SQLite on a
PersistentVolumeClaim; the API turns the stored snapshots into reports.

| Endpoint | |
|---|---|
| `GET /reports/summary` | latest snapshot: cases, auto-resolution and escalation rates, cost and time per decision (404 before the first snapshot) |
| `GET /reports/procedures` | per procedure: cases, approval rate, share sent to a human, average cost (LINQ group-by) |
| `GET /reports/history?take=50` | snapshots, newest first |
| `GET /healthz` | ASP.NET Core health checks, including the database |
| `GET /metrics` | Prometheus (prometheus-net): HTTP metrics and `reports_snapshots_total{result}` |

Minimal APIs, typed `HttpClient` (sends `Host: localhost`, which the app's host check expects),
`BackgroundService` that logs and retries on failure, `TimeProvider` for testable time. The image is the
chiseled ASP.NET runtime (no shell, non-root); the chart mounts the volume at `/data` and a scratch `/tmp`,
with a read-only root filesystem. One replica, because SQLite on a ReadWriteOnce volume has one writer.

| Env | Default |
|---|---|
| `CLEARPATH_URL` | `http://clearpath` |
| `SNAPSHOT_EVERY_SECONDS` | `300` |
| `DB` | `Data Source=/data/reports.db` |

```bash
cd tests && dotnet test     # 7 xUnit tests: WebApplicationFactory, fake ClearPath handler, fixed clock
docker build -t clearpath-reports:local . && kind load docker-image clearpath-reports:local --name clearpath
```

Checked on the local kind cluster: reports served through the Ingress at `/reports/*`, and after deleting the
pod the new one still had the earlier snapshots (the PVC kept the database).
