"""
inspect_db.py
-------------
Prints metadata and all rows for every table in gtm_hunter.db.
Run from any directory:
    python data/DB/inspect_db.py
"""

import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).parent / "gtm_hunter.db"


def hr(char="-", width=80):
    print(char * width)


def inspect(db_path: Path) -> None:
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return

    print()
    hr("=")
    print(f"  Database: {db_path}")
    hr("=")

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        tables = [
            r[0] for r in
            con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        ]

        for table in tables:
            # ── Schema ────────────────────────────────────────────────────────
            print(f"\n  TABLE: {table}")
            hr()
            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            print(f"  {'#':<4} {'Column':<30} {'Type':<16} {'NotNull':<8} {'Default':<20} {'PK'}")
            hr()
            for col in cols:
                print(
                    f"  {col['cid']:<4} {col['name']:<30} {col['type']:<16} "
                    f"{bool(col['notnull'])!s:<8} {str(col['dflt_value']):<20} {bool(col['pk'])}"
                )

            # ── Row count ─────────────────────────────────────────────────────
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"\n  Rows: {count}")

            # ── Data ──────────────────────────────────────────────────────────
            if count == 0:
                print("  (empty)")
                hr()
                continue

            rows = con.execute(f"SELECT * FROM {table}").fetchall()
            col_names = [c["name"] for c in cols]

            hr()
            print(f"\n  Data ({count} row{'s' if count != 1 else ''}):\n")
            for i, row in enumerate(rows, 1):
                print(f"  Row {i}:")
                for name in col_names:
                    val = row[name]
                    # Truncate long text values for readability
                    if isinstance(val, str) and len(val) > 120:
                        val = val[:117] + "..."
                    print(f"    {name:<30} {val}")
                if i < len(rows):
                    print()
            hr()

    print()


if __name__ == "__main__":
    inspect(DB_PATH)
