using System.Net;
using System.Net.Http.Json;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Reports;

namespace Reports.Tests;

/// <summary>Stands in for ClearPath's /metrics and records the requests it gets.</summary>
class FakeClearPath(Func<HttpResponseMessage> respond) : HttpMessageHandler
{
    public List<HttpRequestMessage> Seen { get; } = new();
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage r, CancellationToken ct)
    {
        Seen.Add(r);
        return Task.FromResult(respond());
    }
}

class FixedClock : TimeProvider
{
    public DateTimeOffset Now = new(2026, 11, 3, 9, 0, 0, TimeSpan.Zero);
    public override DateTimeOffset GetUtcNow() => Now;
}

public class ReportsApiTests : IDisposable
{
    const string Metrics = """
    {"total_cases": 4, "auto_resolved": 2, "escalated": 2, "avg_cost_per_decision": 0.005, "avg_decision_ms": 3500,
     "rows": [
       {"id": "PA-1", "procedure": "MRI Brain", "decision": "approve", "route": "auto_approved", "confidence": 1.0, "cost": 0.004},
       {"id": "PA-2", "procedure": "MRI Brain", "decision": "deny", "route": "needs_human", "confidence": 0.95, "cost": 0.006},
       {"id": "PA-3", "procedure": "MRI Brain", "decision": "approve", "route": "auto_approved", "confidence": 1.0, "cost": 0.005},
       {"id": "PA-4", "procedure": "Knee Replacement", "decision": "deny", "route": "needs_human", "confidence": 0.9, "cost": 0.005}
     ]}
    """;

    readonly string _db = Path.Combine(Path.GetTempPath(), $"reports-{Guid.NewGuid():N}.db");
    readonly FixedClock _clock = new();
    bool _clearPathDown;
    readonly FakeClearPath _clearPath;
    readonly WebApplicationFactory<Program> _app;

    public ReportsApiTests()
    {
        _clearPath = new FakeClearPath(() => _clearPathDown
            ? new HttpResponseMessage(HttpStatusCode.ServiceUnavailable)
            : new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(Metrics) });
        _app = new WebApplicationFactory<Program>().WithWebHostBuilder(b =>
        {
            b.UseSetting("DB", $"Data Source={_db}");
            b.UseSetting("SNAPSHOT_WORKER", "false");
            b.UseSetting("CLEARPATH_URL", "http://clearpath.test");
            b.ConfigureTestServices(s =>
            {
                s.AddSingleton<TimeProvider>(_clock);
                s.AddHttpClient<ReportService>().ConfigurePrimaryHttpMessageHandler(() => _clearPath);
            });
        });
    }

    async Task<Snapshot> Snap()
    {
        using var scope = _app.Services.CreateScope();
        return await scope.ServiceProvider.GetRequiredService<ReportService>().TakeSnapshotAsync();
    }

    [Fact]
    public async Task Before_any_snapshot_the_summary_is_404_and_lists_are_empty()
    {
        var http = _app.CreateClient();
        Assert.Equal(HttpStatusCode.NotFound, (await http.GetAsync("/reports/summary")).StatusCode);
        Assert.Empty(await http.GetFromJsonAsync<List<ProcedureReport>>("/reports/procedures") ?? []);
    }

    [Fact]
    public async Task Summary_reports_the_rates_of_the_latest_snapshot()
    {
        await Snap();
        var s = await _app.CreateClient().GetFromJsonAsync<Summary>("/reports/summary");
        Assert.Equal(4, s!.TotalCases);
        Assert.Equal(0.5, s.AutoResolutionRate);
        Assert.Equal(0.5, s.EscalationRate);
        Assert.Equal(_clock.Now, s.TakenAt);
    }

    [Fact]
    public async Task Procedures_are_grouped_with_approval_and_human_rates()
    {
        await Snap();
        var rows = await _app.CreateClient().GetFromJsonAsync<List<ProcedureReport>>("/reports/procedures");
        Assert.Equal(["MRI Brain", "Knee Replacement"], rows!.Select(r => r.Procedure));
        var mri = rows[0];
        Assert.Equal(3, mri.Cases);
        Assert.Equal(0.667, mri.ApprovalRate);
        Assert.Equal(0.333, mri.SentToHumanRate);
        Assert.Equal(0.005, mri.AvgCost);
        Assert.Equal(1.0, rows[1].SentToHumanRate);
    }

    [Fact]
    public async Task History_lists_snapshots_newest_first()
    {
        await Snap();
        _clock.Now = _clock.Now.AddMinutes(5);
        await Snap();
        var h = await _app.CreateClient().GetFromJsonAsync<List<Summary>>("/reports/history?take=10");
        Assert.Equal(2, h!.Count);
        Assert.True(h[0].TakenAt > h[1].TakenAt);
    }

    [Fact]
    public async Task ClearPath_is_asked_for_metrics_as_localhost()
    {
        await Snap();
        var r = Assert.Single(_clearPath.Seen);
        Assert.Equal("http://clearpath.test/metrics", r.RequestUri!.ToString());
        Assert.Equal("localhost", r.Headers.Host);
    }

    [Fact]
    public async Task A_failed_snapshot_stores_nothing()
    {
        _clearPathDown = true;
        await Assert.ThrowsAsync<HttpRequestException>(Snap);
        Assert.Empty(await _app.CreateClient().GetFromJsonAsync<List<Summary>>("/reports/history") ?? []);
    }

    [Fact]
    public async Task Health_and_prometheus_metrics_answer()
    {
        var http = _app.CreateClient();
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync("/healthz")).StatusCode);
        await http.GetAsync("/reports/procedures");
        var metrics = await http.GetStringAsync("/metrics");
        Assert.Contains("http_requests_received_total", metrics);
    }

    public void Dispose()
    {
        _app.Dispose();
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        File.Delete(_db);
    }
}
