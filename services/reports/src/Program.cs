using Microsoft.EntityFrameworkCore;
using Prometheus;
using Reports;

var builder = WebApplication.CreateBuilder(args);
var cfg = builder.Configuration;

builder.Services.AddDbContext<ReportsDb>(o => o.UseSqlite(cfg.GetValue("DB", "Data Source=/data/reports.db")));
builder.Services.AddSingleton(TimeProvider.System);
builder.Services.AddHttpClient<ReportService>(c =>
{
    // ClearPath only answers allowed hostnames; inside the cluster we present as localhost.
    c.BaseAddress = new Uri(cfg.GetValue("CLEARPATH_URL", "http://clearpath")!.TrimEnd('/') + "/");
    c.DefaultRequestHeaders.Host = "localhost";
    c.Timeout = TimeSpan.FromSeconds(10);
});
if (cfg.GetValue("SNAPSHOT_WORKER", true))
    builder.Services.AddHostedService<SnapshotWorker>();
builder.Services.AddHealthChecks().AddDbContextCheck<ReportsDb>();

var app = builder.Build();

using (var scope = app.Services.CreateScope())
    scope.ServiceProvider.GetRequiredService<ReportsDb>().Database.EnsureCreated();

app.UseHttpMetrics();
app.MapHealthChecks("/healthz");
app.MapMetrics("/metrics");

var reports = app.MapGroup("/reports");
reports.MapGet("/summary", async (ReportService r, CancellationToken ct) =>
    await r.SummaryAsync(ct) is { } s ? Results.Ok(s) : Results.NotFound(new { error = "no snapshot yet" }));
reports.MapGet("/procedures", (ReportService r, CancellationToken ct) => r.ByProcedureAsync(ct));
reports.MapGet("/history", (ReportService r, int? take, CancellationToken ct) => r.HistoryAsync(take ?? 50, ct));

app.Run();

public partial class Program;
