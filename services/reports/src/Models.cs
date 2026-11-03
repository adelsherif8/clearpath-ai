using System.Text.Json.Serialization;

namespace Reports;

/// <summary>One copy of ClearPath's evaluation results, taken by the snapshot worker.</summary>
public class Snapshot
{
    public int Id { get; set; }
    public DateTimeOffset TakenAt { get; set; }
    public int TotalCases { get; set; }
    public int AutoResolved { get; set; }
    public int Escalated { get; set; }
    public double AvgCostPerDecision { get; set; }
    public int AvgDecisionMs { get; set; }
    public List<CaseRow> Cases { get; set; } = new();
}

/// <summary>One case decision inside a snapshot.</summary>
public class CaseRow
{
    public int Id { get; set; }
    public int SnapshotId { get; set; }
    public string CaseId { get; set; } = "";
    public string Procedure { get; set; } = "";
    public string Decision { get; set; } = "";
    public string Route { get; set; } = "";
    public double Confidence { get; set; }
    public double Cost { get; set; }
}

/// <summary>The JSON ClearPath serves at /metrics (only the fields we keep).</summary>
public record EvalMetrics(
    [property: JsonPropertyName("total_cases")] int TotalCases,
    [property: JsonPropertyName("auto_resolved")] int AutoResolved,
    [property: JsonPropertyName("escalated")] int Escalated,
    [property: JsonPropertyName("avg_cost_per_decision")] double AvgCostPerDecision,
    [property: JsonPropertyName("avg_decision_ms")] int AvgDecisionMs,
    [property: JsonPropertyName("rows")] List<EvalRow> Rows);

public record EvalRow(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("procedure")] string Procedure,
    [property: JsonPropertyName("decision")] string Decision,
    [property: JsonPropertyName("route")] string Route,
    [property: JsonPropertyName("confidence")] double Confidence,
    [property: JsonPropertyName("cost")] double Cost);

public record Summary(DateTimeOffset TakenAt, int TotalCases, double AutoResolutionRate, double EscalationRate,
                      double AvgCostPerDecision, int AvgDecisionMs);

public record ProcedureReport(string Procedure, int Cases, double ApprovalRate, double SentToHumanRate, double AvgCost);
