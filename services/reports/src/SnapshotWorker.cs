using Prometheus;

namespace Reports;

/// <summary>Takes a snapshot on start and then every interval; a failed attempt is logged and retried next time.</summary>
public class SnapshotWorker(IServiceScopeFactory scopes, IConfiguration config, ILogger<SnapshotWorker> log) : BackgroundService
{
    static readonly Counter Taken = Metrics.CreateCounter("reports_snapshots_total", "Snapshots taken", "result");

    protected override async Task ExecuteAsync(CancellationToken stop)
    {
        var every = TimeSpan.FromSeconds(config.GetValue("SNAPSHOT_EVERY_SECONDS", 300));
        while (!stop.IsCancellationRequested)
        {
            try
            {
                using var scope = scopes.CreateScope();
                var snap = await scope.ServiceProvider.GetRequiredService<ReportService>().TakeSnapshotAsync(stop);
                Taken.WithLabels("ok").Inc();
                log.LogInformation("snapshot {Id}: {Cases} cases", snap.Id, snap.TotalCases);
            }
            catch (Exception e) when (e is not OperationCanceledException)
            {
                Taken.WithLabels("error").Inc();
                log.LogWarning("snapshot failed: {Error}", e.Message);
            }
            try { await Task.Delay(every, stop); } catch (OperationCanceledException) { }
        }
    }
}
