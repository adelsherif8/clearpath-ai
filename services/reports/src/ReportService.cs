using Microsoft.EntityFrameworkCore;

namespace Reports;

/// <summary>Takes snapshots from ClearPath and turns stored snapshots into reports.</summary>
public class ReportService(ReportsDb db, HttpClient http, TimeProvider clock)
{
    public async Task<Snapshot> TakeSnapshotAsync(CancellationToken ct = default)
    {
        var m = await http.GetFromJsonAsync<EvalMetrics>("metrics", ct)
                ?? throw new InvalidOperationException("ClearPath returned no metrics");
        var snap = new Snapshot
        {
            TakenAt = clock.GetUtcNow(),
            TotalCases = m.TotalCases,
            AutoResolved = m.AutoResolved,
            Escalated = m.Escalated,
            AvgCostPerDecision = m.AvgCostPerDecision,
            AvgDecisionMs = m.AvgDecisionMs,
            Cases = m.Rows.Select(r => new CaseRow
            {
                CaseId = r.Id, Procedure = r.Procedure, Decision = r.Decision,
                Route = r.Route, Confidence = r.Confidence, Cost = r.Cost,
            }).ToList(),
        };
        db.Snapshots.Add(snap);
        await db.SaveChangesAsync(ct);
        return snap;
    }

    Task<Snapshot?> LatestAsync(CancellationToken ct) =>
        db.Snapshots.Include(s => s.Cases).OrderByDescending(s => s.TakenAt).FirstOrDefaultAsync(ct);

    public async Task<Summary?> SummaryAsync(CancellationToken ct = default)
    {
        var s = await LatestAsync(ct);
        return s is null ? null : ToSummary(s);
    }

    public async Task<List<ProcedureReport>> ByProcedureAsync(CancellationToken ct = default)
    {
        var s = await LatestAsync(ct);
        if (s is null) return [];
        return s.Cases
            .GroupBy(c => c.Procedure)
            .Select(g => new ProcedureReport(
                g.Key,
                g.Count(),
                Math.Round(g.Count(c => c.Decision == "approve") / (double)g.Count(), 3),
                Math.Round(g.Count(c => c.Route == "needs_human") / (double)g.Count(), 3),
                Math.Round(g.Average(c => c.Cost), 6)))
            .OrderByDescending(r => r.Cases).ThenBy(r => r.Procedure)
            .ToList();
    }

    public async Task<List<Summary>> HistoryAsync(int take, CancellationToken ct = default)
    {
        var rows = await db.Snapshots.OrderByDescending(s => s.TakenAt).Take(Math.Clamp(take, 1, 500)).ToListAsync(ct);
        return rows.Select(ToSummary).ToList();
    }

    static Summary ToSummary(Snapshot s) => new(
        s.TakenAt, s.TotalCases,
        s.TotalCases == 0 ? 0 : Math.Round(s.AutoResolved / (double)s.TotalCases, 3),
        s.TotalCases == 0 ? 0 : Math.Round(s.Escalated / (double)s.TotalCases, 3),
        s.AvgCostPerDecision, s.AvgDecisionMs);
}
