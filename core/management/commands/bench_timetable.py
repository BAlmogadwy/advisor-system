"""Benchmark the timetable builder (Generate) and optimiser (Optimise Current).

    python manage.py bench_timetable --database C:/copies/bench.sqlite3 \\
        --scenarios 663 --action optimise_current --flags production --repeats 3 --json out.json

Run it on a COPY of the database: it refuses the repository's own ``db.sqlite3``. Each run is
done inside a transaction that is rolled back, so the copy is unchanged afterwards and every
repeat starts from the same stored board. Measurement only; no engine behaviour changes. See
``core/services/timetable_benchmark.py`` for what a row holds and where each number comes from.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections

from core.services import timetable_benchmark as bench


def _repository_databases() -> list[Path]:
    """The repository's own database files: this checkout's, the settings' default, and the
    main checkout's when this is a git worktree (whose ``.git`` is a file pointing home)."""
    base = Path(settings.BASE_DIR)
    found = [base / "db.sqlite3"]
    name = settings.DATABASES["default"].get("NAME")
    if name:
        found.append(Path(str(name)))
    marker = base / ".git"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            git_dir = (base / text.split(":", 1)[1].strip()).resolve()  # absolute or relative
            # <main>/.git/worktrees/<name>
            if len(git_dir.parents) >= 3:
                found.append(git_dir.parents[2] / "db.sqlite3")
    return found


def point_default_database_at(raw_path: str) -> Path:
    """Aim the default connection at an existing SQLite COPY; refuse the repository's own file."""
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise CommandError(f"--database {raw_path!r} is not an existing file")
    resolved = path.resolve()
    for protected in _repository_databases():
        if protected.exists() and os.path.samefile(resolved, protected):
            raise CommandError("refusing to run on the repository's db.sqlite3: run it on a copy")
    if resolved.name == "db.sqlite3" and (resolved.parent / "manage.py").exists():
        raise CommandError("refusing a db.sqlite3 that sits next to a manage.py: run it on a copy")
    if connection.vendor != "sqlite":
        raise CommandError("only a SQLite copy can be benchmarked (the default database is not)")
    connections["default"].close()
    settings.DATABASES["default"]["NAME"] = str(resolved)
    connections["default"].settings_dict["NAME"] = str(resolved)
    connections["default"].close()
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA database_list")
        attached = [row[2] for row in cursor.fetchall() if row[1] == "main"]
    if not attached or Path(attached[0]).resolve() != resolved:
        raise CommandError(f"the connection is on {attached!r}, not on {str(resolved)!r}")
    return resolved


class Command(BaseCommand):
    help = "Benchmark Generate / Optimise Current on a database COPY (rolled back after each run)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database", required=True, help="path of a SQLite COPY of the database"
        )
        parser.add_argument("--scenarios", nargs="*", type=int, default=[], help="scenario ids")
        parser.add_argument(
            "--action",
            choices=[*bench.ALL_ACTIONS, "both"],
            default="optimise_current",
            help="generate = full rebuild from an empty board; optimise_current = improve the board; scheduler_generate = the newer engine, as a comparison row",
        )
        parser.add_argument(
            "--flags",
            choices=["production", "local"],
            default="production",
            help="production = the TIMETABLE_* values in render.yaml; local = this machine's settings",
        )
        parser.add_argument("--set", dest="overrides", action="append", metavar="KEY=VALUE")
        parser.add_argument("--repeats", type=int, default=1)
        parser.add_argument("--seed", type=int, default=1, help="first run seed")
        parser.add_argument(
            "--vary-seed",
            action="store_true",
            help="use seed+repeat for each repeat (default: the same seed, to measure repeatability)",
        )
        parser.add_argument(
            "--max-iterations", type=int, default=bench.DEFAULT_TUNING["max_iterations"]
        )
        parser.add_argument(
            "--max-chain-iterations", type=int, default=bench.DEFAULT_TUNING["max_chain_iterations"]
        )
        parser.add_argument(
            "--cpsat-limit", type=float, default=bench.DEFAULT_TUNING["cpsat_limit"]
        )
        parser.add_argument(
            "--scheduler-seconds", type=float, default=bench.SCHEDULER_TUNING["scheduler_seconds"]
        )
        parser.add_argument(
            "--scheduler-runs", type=int, default=bench.SCHEDULER_TUNING["scheduler_runs"]
        )
        parser.add_argument("--no-chain", action="store_true")
        parser.add_argument("--no-cpsat", action="store_true")
        parser.add_argument("--json", dest="json_path", help="write the full result as JSON here")
        parser.add_argument("--list", action="store_true", help="list the scenarios and exit")

    def handle(self, *args, **options):
        database = point_default_database_at(options["database"])
        if options["repeats"] < 1:
            raise CommandError("--repeats must be at least 1")
        if options["list"]:
            self._list_scenarios(database)
            return
        if not options["scenarios"]:
            raise CommandError("give --scenarios ids (or --list to see them)")
        try:
            flags = bench.production_flags() if options["flags"] == "production" else {}
            flags.update(bench.parse_overrides(options["overrides"]))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        actions = list(bench.ACTIONS) if options["action"] == "both" else [options["action"]]
        tuning = {
            "max_iterations": options["max_iterations"],
            "max_chain_iterations": options["max_chain_iterations"],
            "cpsat_limit": options["cpsat_limit"],
            "scheduler_seconds": options["scheduler_seconds"],
            "scheduler_runs": options["scheduler_runs"],
            "run_chain": not options["no_chain"],
            "run_cpsat": not options["no_cpsat"],
        }
        rows: list[dict] = []
        self.stderr.write(f"database: {database}  flags: {options['flags']} {flags}")
        with bench.flag_scope(flags):
            self.stderr.write(f"effective: {bench.effective_flags()}")
            for scenario_id in options["scenarios"]:
                for action in actions:
                    for repeat in range(1, options["repeats"] + 1):
                        seed = options["seed"] + (repeat - 1 if options["vary_seed"] else 0)
                        row = bench.run_once(
                            scenario_id, action, seed=seed, tuning=tuning, repeat=repeat
                        )
                        rows.append(row)
                        self.stderr.write(
                            f"scenario {scenario_id} {action} #{repeat}: {row['outcome']}"
                            f" in {row.get('wall_seconds', '-')}s"
                        )
        summary = bench.summarise(rows)
        self.stdout.write(bench.render_table(rows))
        self.stdout.write("")
        self.stdout.write(bench.render_stage_table(summary))
        if options["json_path"]:
            payload = {
                "meta": {
                    "database": str(database),
                    "flags_mode": options["flags"],
                    "flags": flags,
                    "effective_flags": None,
                    "effective_settings": bench.effective_settings(),
                    "tuning": tuning,
                    "python": sys.version.split()[0],
                    "repeats": options["repeats"],
                    "seed": options["seed"],
                    "vary_seed": options["vary_seed"],
                },
                "rows": rows,
                "summary": summary,
            }
            payload["meta"]["effective_flags"] = rows[0]["flags"] if rows else None
            Path(options["json_path"]).write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
            self.stderr.write(f"wrote {options['json_path']}")

    def _list_scenarios(self, database: Path) -> None:
        from core.models import SectionInstructor, SectionPlacement, TimetableScenario

        self.stdout.write(f"database: {database}")
        for scenario in TimetableScenario.objects.order_by("id"):
            meetings = SectionPlacement.objects.filter(board__scenario_id=scenario.id).count()
            teachers = SectionInstructor.objects.filter(scenario_id=scenario.id).count()
            self.stdout.write(
                f"{scenario.id}\t{scenario.name}\tmeetings={meetings}\tteacher_rows={teachers}"
            )
