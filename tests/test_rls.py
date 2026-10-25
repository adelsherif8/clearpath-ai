"""Database-level permissions: these tests talk to Postgres directly as `clearpath_app`,
bypassing the API completely. Every refusal here comes from the database itself
(RLS policies, missing GRANTs or guard triggers), not from Python."""

import psycopg
import pytest

from tests.conftest import Users, _app_url, stored_case

DENIED = (psycopg.errors.InsufficientPrivilege,)


@pytest.fixture()
def as_user():
    """Open a connection as the app role and act as `uid` inside one transaction."""
    conns = []

    def _as(uid: str | None):
        conn = psycopg.connect(_app_url(), autocommit=False)
        conns.append(conn)
        conn.execute("select set_config('app.user_id', %s, true)", (uid or "",))
        return conn

    yield _as
    for c in conns:
        c.rollback()
        c.close()


# ── the role itself ──────────────────────────────────────────────────────────
def test_app_role_is_least_privilege(admin_db):
    row = admin_db.execute("""select rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
                                from pg_roles where rolname = 'clearpath_app'""").fetchone()
    assert row == (False, False, False, False)
    for table in ("user_roles", "cases", "decisions", "audit_log", "llm_usage"):
        for priv in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not admin_db.execute("select has_table_privilege('clearpath_app', %s, %s)",
                                        (f"clearpath.{table}", priv)).fetchone()[0], (table, priv)
    for table in ("decisions", "audit_log"):
        assert not admin_db.execute("select has_table_privilege('clearpath_app', %s, 'UPDATE')",
                                    (f"clearpath.{table}",)).fetchone()[0]
    assert not admin_db.execute("select has_schema_privilege('clearpath_app', 'clearpath', 'CREATE')").fetchone()[0]


def test_rls_is_enabled_and_forced_everywhere(admin_db):
    rows = admin_db.execute("""select relname, relrowsecurity, relforcerowsecurity from pg_class
                                where relnamespace = 'clearpath'::regnamespace and relkind = 'r'""").fetchall()
    assert len(rows) == 5
    assert all(r[1] and r[2] for r in rows), rows


# ── cases ────────────────────────────────────────────────────────────────────
def test_submitter_cannot_read_other_submitters_case(admin_db, as_user):
    a = stored_case(admin_db, submitter=Users.submitter_a)
    b = stored_case(admin_db, submitter=Users.submitter_b, reviewer=Users.reviewer_2)
    conn = as_user(Users.submitter_b)
    visible = {r[0] for r in conn.execute("select id from clearpath.cases").fetchall()}
    assert visible == {b}
    assert conn.execute("select count(*) from clearpath.cases where id = %s", (a,)).fetchone()[0] == 0


def test_unassigned_reviewer_cannot_read_case(admin_db, as_user):
    case_id = stored_case(admin_db, reviewer=Users.reviewer_1)
    conn = as_user(Users.reviewer_2)
    assert conn.execute("select count(*) from clearpath.cases where id = %s", (case_id,)).fetchone()[0] == 0


def test_no_identity_sees_nothing(admin_db, as_user):
    stored_case(admin_db)
    conn = as_user(None)
    assert conn.execute("select count(*) from clearpath.cases").fetchone()[0] == 0
    assert conn.execute("select count(*) from clearpath.decisions").fetchone()[0] == 0
    assert conn.execute("select count(*) from clearpath.audit_log").fetchone()[0] == 0


def _insert_case(conn, submitter, status="pending_review"):
    conn.execute(
        """insert into clearpath.cases (id, submitter_id, status, requested_procedure, plan, condition,
               sex, notes, agent_decision, agent_confidence, agent_route)
           values ('PA-0000ABCD', %s, %s, 'MRI', 'Commercial', 'pain', 'F', 'notes here', 'approve', 0.9,
                   'needs_human')""", (submitter, status))


def test_submitter_cannot_create_case_in_someone_elses_name(as_user):
    conn = as_user(Users.submitter_a)
    with pytest.raises(DENIED):
        _insert_case(conn, Users.submitter_b)


def test_submitter_cannot_create_a_pre_finalized_case(as_user):
    conn = as_user(Users.submitter_a)
    with pytest.raises(DENIED):
        _insert_case(conn, Users.submitter_a, status="approved")


def test_reviewer_cannot_create_cases(as_user):
    conn = as_user(Users.reviewer_1)
    with pytest.raises(DENIED):
        _insert_case(conn, Users.reviewer_1)


def test_submitter_cannot_update_own_case(admin_db, as_user):
    case_id = stored_case(admin_db, submitter=Users.submitter_a)
    conn = as_user(Users.submitter_a)
    cur = conn.execute("update clearpath.cases set status = 'approved' where id = %s", (case_id,))
    assert cur.rowcount == 0      # the row is invisible to UPDATE under RLS


def test_unassigned_reviewer_cannot_finalize(admin_db, as_user):
    case_id = stored_case(admin_db, reviewer=Users.reviewer_1)
    conn = as_user(Users.reviewer_2)
    cur = conn.execute("update clearpath.cases set status = 'approved' where id = %s", (case_id,))
    assert cur.rowcount == 0


def test_reviewer_cannot_reassign_case_to_someone_else(admin_db, as_user):
    case_id = stored_case(admin_db, reviewer=Users.reviewer_1)
    conn = as_user(Users.reviewer_1)
    with pytest.raises(DENIED):
        conn.execute("update clearpath.cases set assigned_reviewer_id = %s where id = %s",
                     (Users.reviewer_2, case_id))


def test_admin_cannot_finalize(admin_db, as_user):
    case_id = stored_case(admin_db)
    conn = as_user(Users.admin)
    with pytest.raises(DENIED):
        conn.execute("update clearpath.cases set status = 'denied' where id = %s", (case_id,))


def test_admin_can_only_assign_to_a_reviewer(admin_db, as_user):
    case_id = stored_case(admin_db)
    conn = as_user(Users.admin)
    with pytest.raises(DENIED):
        conn.execute("update clearpath.cases set assigned_reviewer_id = %s where id = %s",
                     (Users.submitter_b, case_id))


def test_finalized_case_is_immutable(admin_db, as_user):
    case_id = stored_case(admin_db)
    conn = as_user(Users.reviewer_1)
    conn.execute("update clearpath.cases set status = 'approved' where id = %s", (case_id,))
    with pytest.raises(DENIED):
        conn.execute("update clearpath.cases set status = 'denied' where id = %s", (case_id,))


def test_nothing_can_move_a_case_to_auto_approved_after_escalation(admin_db, as_user):
    case_id = stored_case(admin_db)
    conn = as_user(Users.reviewer_1)
    with pytest.raises(DENIED):
        conn.execute("update clearpath.cases set status = 'auto_approved' where id = %s", (case_id,))


def test_new_escalated_case_is_assigned_to_a_reviewer(as_user):
    conn = as_user(Users.submitter_a)
    _insert_case(conn, Users.submitter_a)
    assigned = conn.execute("select assigned_reviewer_id::text from clearpath.cases").fetchone()[0]
    assert assigned in (Users.reviewer_1, Users.reviewer_2)


# ── decisions ────────────────────────────────────────────────────────────────
def test_decision_requires_assigned_reviewer_in_own_name(admin_db, as_user):
    case_id = stored_case(admin_db, reviewer=Users.reviewer_1)
    conn = as_user(Users.reviewer_2)
    with pytest.raises(DENIED):
        conn.execute("insert into clearpath.decisions (case_id, reviewer_id, action) values (%s, %s, 'approve')",
                     (case_id, Users.reviewer_2))


def test_decision_cannot_be_forged_for_another_reviewer(admin_db, as_user):
    case_id = stored_case(admin_db, reviewer=Users.reviewer_1)
    conn = as_user(Users.reviewer_1)
    with pytest.raises(DENIED):
        conn.execute("insert into clearpath.decisions (case_id, reviewer_id, action) values (%s, %s, 'approve')",
                     (case_id, Users.reviewer_2))


def test_decisions_cannot_be_updated_or_deleted_by_app(admin_db, as_user):
    case_id = stored_case(admin_db)
    conn = as_user(Users.reviewer_1)
    conn.execute("insert into clearpath.decisions (case_id, reviewer_id, action) values (%s, %s, 'deny')",
                 (case_id, Users.reviewer_1))
    conn.execute("savepoint s")
    with pytest.raises(DENIED):
        conn.execute("update clearpath.decisions set action = 'approve'")
    conn.execute("rollback to savepoint s")
    with pytest.raises(DENIED):
        conn.execute("delete from clearpath.decisions")


# ── audit log: append-only ───────────────────────────────────────────────────
def _audit(conn, actor):
    conn.execute("""insert into clearpath.audit_log (event, actor_id, actor_role, outcome)
                    values ('test_event', %s, 'reviewer', 'ok')""", (actor,))


def test_app_role_cannot_update_audit_log(as_user):
    conn = as_user(Users.admin)
    _audit(conn, Users.admin)
    with pytest.raises(DENIED):
        conn.execute("update clearpath.audit_log set outcome = 'denied'")


def test_app_role_cannot_delete_audit_log(as_user):
    conn = as_user(Users.admin)
    _audit(conn, Users.admin)
    with pytest.raises(DENIED):
        conn.execute("delete from clearpath.audit_log")


def test_app_role_cannot_truncate_audit_log(as_user):
    conn = as_user(Users.admin)
    with pytest.raises(DENIED):
        conn.execute("truncate clearpath.audit_log")


def test_even_the_owner_cannot_rewrite_audit_history(admin_db):
    admin_db.execute("insert into clearpath.audit_log (event, actor_role, outcome) values ('x', 'anonymous', 'ok')")
    with pytest.raises(DENIED):
        admin_db.execute("update clearpath.audit_log set event = 'y'")
    with pytest.raises(DENIED):
        admin_db.execute("delete from clearpath.audit_log")


def test_audit_entries_cannot_be_forged_in_another_users_name(as_user):
    conn = as_user(Users.reviewer_1)
    with pytest.raises(DENIED):
        _audit(conn, Users.admin)


def test_only_admin_can_read_audit_log(admin_db, as_user):
    admin_db.execute("insert into clearpath.audit_log (event, actor_role, outcome) values ('x', 'anonymous', 'ok')")
    for uid in (Users.submitter_a, Users.reviewer_1):
        assert as_user(uid).execute("select count(*) from clearpath.audit_log").fetchone()[0] == 0
    assert as_user(Users.admin).execute("select count(*) from clearpath.audit_log").fetchone()[0] == 1


# ── roles: no privilege escalation ───────────────────────────────────────────
def test_new_user_cannot_self_register_as_admin(as_user):
    newbie = "00000000-0000-0000-0000-0000000000ee"
    conn = as_user(newbie)
    with pytest.raises(DENIED):
        conn.execute("insert into clearpath.user_roles (user_id, role) values (%s, 'admin')", (newbie,))


def test_user_cannot_register_someone_else(as_user):
    conn = as_user("00000000-0000-0000-0000-0000000000ee")
    with pytest.raises(DENIED):
        conn.execute("insert into clearpath.user_roles (user_id) values ('00000000-0000-0000-0000-0000000000ff')")


def test_submitter_cannot_promote_themself(as_user):
    conn = as_user(Users.submitter_a)
    cur = conn.execute("update clearpath.user_roles set role = 'admin' where user_id = %s", (Users.submitter_a,))
    assert cur.rowcount == 0


def test_admin_cannot_change_own_role(as_user):
    conn = as_user(Users.admin)
    cur = conn.execute("update clearpath.user_roles set role = 'reviewer' where user_id = %s", (Users.admin,))
    assert cur.rowcount == 0


def test_supabase_data_api_roles_have_no_access(admin_db):
    for role in ("anon", "authenticated"):
        if admin_db.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
            assert not admin_db.execute("select has_schema_privilege(%s, 'clearpath', 'USAGE')",
                                        (role,)).fetchone()[0]


def test_supabase_data_api_roles_cannot_read_tables_even_with_default_grants(admin_db):
    # conftest grants anon/authenticated ALL on new tables by default, like Supabase does;
    # the migration must still leave them with nothing.
    for role in ("anon", "authenticated"):
        for table in ("cases", "decisions", "audit_log", "user_roles"):
            assert not admin_db.execute("select has_table_privilege(%s, %s, 'SELECT')",
                                        (role, f"clearpath.{table}")).fetchone()[0], (role, table)
