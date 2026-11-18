"""Run the existing ORM placers on connection-private tables.

The placers read one another's intermediate rows and refer to sections by their
real primary keys. Temporary tables preserve both properties. The job row,
catalogues and other read-only inputs continue to come from the database.
Only rows the engine actually wrote form the proposal published by the runner.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from django.db import connection, transaction

from core.models import (
    BoardSectionVisibility,
    SectionInstructor,
    SectionPlacement,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableRepairGlobalPlanItem,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.timetable_board_persistence import recreate_snapshot_rows

# Parents precede children for publication.
_TIMETABLE_MODELS = (
    TimetableScenario,
    TermSection,
    SectionInstructor,
    SectionPlacement,
    TermSectionMeeting,
    TermSectionProgram,
    StudentTermSection,
    BoardSectionVisibility,
)
# Isolate Django's SET_NULL writes during candidate evaluation. Repair runs and
# approvals have independent writers and own their live state throughout a rebuild.
_WORKSPACE_MODELS = (*_TIMETABLE_MODELS, TimetableRepairRun, TimetableRepairGlobalPlanItem)
_DIRTY = "planner_rebuild_changes"


@dataclass
class _Changes:
    model: type
    original_ids: list
    rows: list[dict]


class RebuildProposal:
    def __init__(self):
        self.changes: list[_Changes] = []

    def capture(self):
        """Read only changed rows while the private tables are still visible."""
        with connection.cursor() as cursor:
            for index, model in enumerate(_TIMETABLE_MODELS):
                cursor.execute(
                    "SELECT row_id, existed FROM planner_rebuild_changes WHERE model_id = %s",
                    [index],
                )
                touched = cursor.fetchall()
                if not touched:
                    continue
                field = model._meta.pk
                ids = [field.to_python(pk) for pk, _ in touched]
                rows = []
                for start in range(0, len(ids), 500):
                    rows.extend(model.objects.filter(pk__in=ids[start : start + 500]).values())
                originals = [
                    field.get_db_prep_value(field.to_python(pk), connection)
                    for pk, existed in touched
                    if existed
                ]
                self.changes.append(_Changes(model, originals, rows))

    def publish(self):
        """Caller owns the running job row and the surrounding final transaction."""
        # The solver already collected timetable cascades. Repair links are
        # resolved against live rows, including analyses created during solving.
        with connection.cursor() as cursor:
            for change in reversed(self.changes):
                table = connection.ops.quote_name(change.model._meta.db_table)
                retained_ids = {row["id"] for row in change.rows}
                for start in range(0, len(change.original_ids), 500):
                    ids = change.original_ids[start : start + 500]
                    params = ", ".join(["%s"] * len(ids))
                    # The table is quoted model metadata. Every ID is bound.
                    cursor.execute(f"DELETE FROM {table} WHERE id IN ({params})", ids)  # nosec B608
                    # DELETE holds the parent locks until commit. A completed
                    # analysis is now visible, and a new FK cannot commit first.
                    removed = [pk for pk in ids if pk not in retained_ids]
                    if removed and change.model is SectionPlacement:
                        TimetableRepairRun.objects.filter(target_placement_id__in=removed).update(
                            target_placement=None
                        )
                        TimetableRepairGlobalPlanItem.objects.filter(
                            placement_id__in=removed
                        ).update(placement=None)
                    elif removed and change.model is TermSection:
                        TimetableRepairRun.objects.filter(target_section_id__in=removed).update(
                            target_section=None
                        )
        for change in self.changes:
            recreate_snapshot_rows(change.model, change.rows)


def _postgres_table(cursor, table, index):
    # Resolve the permanent relation before its private namesake exists.
    cursor.execute(
        "SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname), "
        "pg_get_serial_sequence(%s, 'id') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = %s::regclass",
        [table, table],
    )
    source, sequence = cursor.fetchone()
    name = connection.ops.quote_name(table)
    cursor.execute(
        f"CREATE TEMP TABLE {name} (LIKE {source} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES) ON COMMIT DROP"
    )
    # Reserve real IDs through the source sequence, even for identity columns.
    if sequence:
        cursor.execute(
            f"ALTER TABLE pg_temp.{name} ALTER COLUMN id SET DEFAULT nextval(%s::regclass)",
            [sequence],
        )
    # Both relation names are quoted by the database/backend above.
    cursor.execute(f"INSERT INTO pg_temp.{name} SELECT * FROM {source}")  # nosec B608
    cursor.execute(
        f"CREATE TRIGGER track_write AFTER INSERT OR UPDATE OR DELETE ON pg_temp.{name} FOR EACH ROW EXECUTE FUNCTION pg_temp.planner_track_write('{index}')"
    )


def _sqlite_allocate_id(table):
    # Run inside the private INSERT's transaction. Advance the real sequence
    # so other connections can insert normally while this proposal is pending.
    db = connection.connection
    if not db.execute("UPDATE main.sqlite_sequence SET seq=seq+1 WHERE name=?", (table,)).rowcount:
        db.execute("INSERT INTO main.sqlite_sequence(name, seq) VALUES (?, 1)", (table,))
    return db.execute("SELECT seq FROM main.sqlite_sequence WHERE name=?", (table,)).fetchone()[0]


def _sqlite_table(cursor, table, index):
    name = connection.ops.quote_name(table)
    cursor.execute(f"PRAGMA main.table_info({name})")
    columns = []
    for _, column, db_type, required, default, primary in cursor.fetchall():
        definition = f"{connection.ops.quote_name(column)} {db_type}"
        if primary and db_type.upper() == "INTEGER":
            # INT (unlike INTEGER) honors the default instead of using ROWID.
            definition = f"id INT PRIMARY KEY DEFAULT (planner_next_id('{table}'))"
        elif primary:
            definition += " PRIMARY KEY"
        if required:
            definition += " NOT NULL"
        if default is not None:
            definition += f" DEFAULT {default}"
        columns.append(definition)
    cursor.execute(f"CREATE TEMP TABLE {name} ({', '.join(columns)})")
    cursor.execute(f"INSERT INTO temp.{name} SELECT * FROM main.{name}")  # nosec B608
    cursor.execute(f"PRAGMA main.index_list({name})")
    for _, idx, unique, _, _ in cursor.fetchall():
        cursor.execute("SELECT sql FROM main.sqlite_master WHERE type='index' AND name=%s", [idx])
        sql = cursor.fetchone()[0]
        if sql:
            # Preserve expression indexes and partial uniqueness predicates.
            clause = sql[sql.index("(") :]
        else:
            cursor.execute(f"PRAGMA main.index_info({connection.ops.quote_name(idx)})")
            clause = (
                "("
                + ", ".join(connection.ops.quote_name(row[2]) for row in cursor.fetchall())
                + ")"
            )
        idx_name = connection.ops.quote_name(f"work_{idx}")
        cursor.execute(
            f"CREATE {'UNIQUE' if unique else ''} INDEX temp.{idx_name} ON {name} {clause}"
        )
    for op, row in (("insert", "NEW"), ("update", "OLD"), ("delete", "OLD")):
        cursor.execute(
            f"CREATE TEMP TRIGGER track_{index}_{op} AFTER {op} ON temp.{name} BEGIN "  # nosec B608
            f"INSERT INTO {_DIRTY} VALUES ({index}, {row}.id, {int(op != 'insert')}) "
            "ON CONFLICT(model_id, row_id) DO NOTHING; END"
        )


@contextmanager
def rebuild_workspace():
    """Discard the private tables on exit, including when the process dies."""
    postgres = connection.vendor == "postgresql"
    # A PostgreSQL transaction pins the backend (also under transaction pooling).
    # It writes only temporary timetable rows. The live publication is separate.
    with transaction.atomic() if postgres else nullcontext():
        with transaction.atomic(), connection.cursor() as cursor:
            drop = " ON COMMIT DROP" if postgres else ""
            cursor.execute(
                f"CREATE TEMP TABLE {_DIRTY} (model_id integer, row_id text, existed integer, PRIMARY KEY (model_id, row_id)){drop}"
            )
            if postgres:
                # An explicitly configured pg_temp later in search_path disables
                # PostgreSQL's usual implicit temporary-table precedence.
                cursor.execute("SHOW search_path")
                original_path = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT set_config('search_path', %s, true)", [f"pg_temp, {original_path}"]
                )
                cursor.execute("""
                    CREATE OR REPLACE FUNCTION pg_temp.planner_track_write() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        INSERT INTO pg_temp.planner_rebuild_changes VALUES (
                            TG_ARGV[0]::integer,
                            CASE WHEN TG_OP='INSERT' THEN NEW.id::text ELSE OLD.id::text END,
                            CASE WHEN TG_OP='INSERT' THEN 0 ELSE 1 END
                        ) ON CONFLICT DO NOTHING;
                        RETURN NULL;
                    END $$
                """)
            else:
                connection.connection.create_function("planner_next_id", 1, _sqlite_allocate_id)
            for index, model in enumerate(_WORKSPACE_MODELS):
                create = _postgres_table if postgres else _sqlite_table
                create(cursor, model._meta.db_table, index)
        try:
            yield RebuildProposal()
        finally:
            # Explicit cleanup also works when a caller owns an outer transaction.
            schema = "pg_temp" if postgres else "temp"
            if not connection.needs_rollback and not connection.closed_in_transaction:
                with transaction.atomic(), connection.cursor() as cursor:
                    for model in reversed(_WORKSPACE_MODELS):
                        cursor.execute(
                            f"DROP TABLE {schema}.{connection.ops.quote_name(model._meta.db_table)}"
                        )
                    cursor.execute(f"DROP TABLE {schema}.{_DIRTY}")
                    if postgres:
                        cursor.execute(
                            "SELECT set_config('search_path', %s, true)", [original_path]
                        )
                    else:
                        connection.connection.create_function("planner_next_id", 1, None)
