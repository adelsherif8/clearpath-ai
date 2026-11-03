using Microsoft.EntityFrameworkCore;

namespace Reports;

public class ReportsDb(DbContextOptions<ReportsDb> options) : DbContext(options)
{
    public DbSet<Snapshot> Snapshots => Set<Snapshot>();
    public DbSet<CaseRow> Cases => Set<CaseRow>();

    protected override void OnModelCreating(ModelBuilder b)
    {
        b.Entity<Snapshot>().HasMany(s => s.Cases).WithOne().HasForeignKey(c => c.SnapshotId);
        b.Entity<Snapshot>().HasIndex(s => s.TakenAt);
        // SQLite cannot order by DateTimeOffset natively; store it as UTC ticks.
        b.Entity<Snapshot>().Property(s => s.TakenAt)
            .HasConversion(v => v.UtcTicks, v => new DateTimeOffset(v, TimeSpan.Zero));
    }
}
