#!/usr/bin/env python3
"""export_db_to_s3.py — sync a deployment's SQLite tables with Parquet in S3.

The stepping-stone piece of moving client data out of Git and toward
Snowflake: scripts/s3_assets.py already gets creative *images* to S3;
this does the same for the *data* sitting in data/<client>.db, in a shape
Snowflake can `COPY INTO` directly later (typed, columnar, one object per
table) rather than as an opaque SQLite blob nobody but this codebase can read.

Three subcommands:

    export   local db  -> S3 (Parquet, one file per table, + schema.json)
    restore  S3         -> local db (rebuilds it fresh from Parquet + schema)
    sync     compares local db's mtime against S3's, does whichever of the
             above the more-recently-modified side implies. Exists so
             different operators on different machines running audits
             against the same client don't have to think about which
             direction is "correct" — see "Multi-operator note" below for
             what this precedence rule does and does not protect against.

Format is Parquet, not CSV — Snowflake loads it natively and it preserves
column types (a CSV re-derived from SQLite's weak typing round-trips badly:
every column becomes a string, integers and floats become indistinguishable,
NULL vs empty string is ambiguous). The one deliberate exception: every
*_json TEXT column (raw_json, analysis_json, config_json, meta_json, ...) is
kept as a plain string, not parsed into a nested Parquet type. These blobs
are not schema-consistent enough across rows to infer one nested type safely,
and Snowflake's own PARSE_JSON(column) on a VARCHAR is the standard, robust
way to turn a JSON-string column into VARIANT downstream — better done there,
once, than guessed at here per-table.

Schema fidelity for `restore`: Parquet-inferred types alone would lose things
like `INTEGER PRIMARY KEY AUTOINCREMENT` and any other constraint SQLite's own
schema carries. `export` also captures the exact `CREATE TABLE` statements
from sqlite_master and uploads them as tables/_schema.json — `restore` replays
those verbatim before loading any data, so a rebuilt db is schema-identical to
the one that produced the export, not an approximation.

Layout, deliberately NOT the by-hash/sidecars content-addressed scheme those
use: table exports are a "latest full snapshot" of each table, one stable key
per table, overwritten every run — there is no local file to hash the way an
image or a video_meta.json sidecar has, since this is generated FROM the db,
not archived AS-IS. Key: <prefix>/<client>/tables/<table>.parquet. prune()
in s3_assets.py has been taught to leave anything under /tables/ alone for
exactly this reason — it has no corresponding local asset-tree file to check
against, so treating it like an orphan would be wrong, not a bug to fix later.

Excluded by default: sqlite_sequence (SQLite's own internal bookkeeping,
never meaningful data), and eval_runs/eval_task_results/llm_calls (this
codebase's own eval-harness and LLM-call telemetry — pipeline ops data, not
client-facing competitive/performance data). Override with --include-tables
or --exclude-tables if a specific export needs something different.

Multi-operator note: "most-recently-modified wins" is a real, deliberate
choice, not a placeholder for something smarter — and it is a heuristic, not
a merge. It resolves "which whole snapshot do I trust" across machines; it
does NOT reconcile row-level differences if two operators both changed
different things since the last sync (the loser's changes are gone, not
merged in). `restore` backs up an existing local db to <db>.pre-sync-backup
before overwriting it for exactly this reason — reversible if the heuristic
picked the wrong side, but it does not protect S3's copy if a bad `export`
overwrites it first. Clock skew between machines is a real risk this
precedence rule inherits and does not correct for.

One more sharp edge, found by testing this against a real db rather than
just reading the code: right after a `restore`, the local file's mtime is
"now" (it was just written), which is almost always newer than S3's
LastModified from the export it just pulled. A `sync` run immediately
afterward will see local as "more recently modified" and push straight back
to S3 — harmless (same data going back) but worth knowing so it doesn't
look like the precedence rule picked the wrong side.

`restore` rebuilds db_path from nothing but what S3 had for the exported
tables — it does NOT merge into whatever local file was there. Anything
local-only that was never in the export scope (this codebase's own
eval_runs/eval_task_results/llm_calls, or anything a future DEFAULT_EXCLUDE
addition leaves out) would otherwise only survive in the one-shot
.pre-sync-backup file, which the *next* restore silently overwrites — so
`restore` also carries those tables forward from the backup into the
rebuilt db automatically, rather than leaving that as the sole safety net.

Usage:
    python3 scripts/export_db_to_s3.py export --db data/trex.db --dry-run
    python3 scripts/export_db_to_s3.py export --db data/trex.db \
        --bucket next-ext-commerce-us-east-1 --prefix outbound/competitive-intel
    python3 scripts/export_db_to_s3.py restore --db data/trex.db \
        --bucket ... --prefix ...              # refuses if data/trex.db exists
    python3 scripts/export_db_to_s3.py restore --db data/trex.db --force ...
    python3 scripts/export_db_to_s3.py sync --db data/trex.db --bucket ... --prefix ...
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from s3_assets import _client, client_prefix  # noqa: E402 — reuse, no duplication

TABLE_SEG = "tables"
SCHEMA_NAME = "_schema.json"
DEFAULT_EXCLUDE = {"sqlite_sequence", "eval_runs", "eval_task_results", "llm_calls"}


def db_client_slug(db_path: Path) -> str:
    """'data/trex.db' -> 'trex'. Same role as s3_assets.client_slug(), just
    keyed off the db filename instead of a data/<client>_assets/ dirname."""
    return db_path.stem


def db_prefix(base_prefix: str, client: str) -> str:
    """client_prefix() expects a '<client>_assets'-shaped dirname to derive
    the slug from — this just gives it one, so the tables/ segment lands
    under the exact same <prefix>/<client>/ root the image/sidecar pipeline
    already uses for this client, without duplicating client_slug's
    strip-suffix logic for a differently-shaped input."""
    return client_prefix(base_prefix, f"{client}_assets")


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return sorted(r[0] for r in rows)


def resolve_tables(conn: sqlite3.Connection, exclude_arg: str, include_arg: str) -> list[str]:
    exclude = set(exclude_arg.split(",")) if exclude_arg else set(DEFAULT_EXCLUDE)
    include = set(include_arg.split(",")) if include_arg else None
    all_tables = list_tables(conn)
    return [t for t in all_tables if t not in exclude and (include is None or t in include)]


def table_to_parquet_bytes(conn: sqlite3.Connection, table: str) -> tuple[bytes, int]:
    """Read every row of `table` and return (parquet file bytes, row count)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    import io

    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    columns: dict[str, list] = {c: [] for c in cols}
    for row in rows:
        for c, v in zip(cols, row):
            columns[c].append(v)

    arrays = {}
    for c, values in columns.items():
        try:
            arrays[c] = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            # Mixed/unrepresentable types in one column (SQLite's weak typing
            # allows this even if it's rare in practice) — fall back to string
            # rather than fail the whole table's export.
            arrays[c] = pa.array([None if v is None else str(v) for v in values], type=pa.string())
        if pa.types.is_null(arrays[c].type):
            # An all-NULL column infers as Arrow's `null` type, which some
            # Parquet consumers (Snowflake included, historically) handle
            # poorly. Cast to string so the column is at least typed.
            arrays[c] = arrays[c].cast(pa.string())

    table_obj = pa.table(arrays)
    buf = io.BytesIO()
    pq.write_table(table_obj, buf)
    return buf.getvalue(), len(rows)


def parquet_bytes_to_rows(data: bytes) -> "pyarrow.Table":  # noqa: F821 — typing only
    import pyarrow.parquet as pq
    import io
    return pq.read_table(io.BytesIO(data))


# ------------------------------------------------------------------------- export


def cmd_export(args) -> int:
    db_path = Path(args.db) if Path(args.db).is_absolute() else ROOT / args.db
    if not db_path.is_file():
        sys.exit(f"not a file: {db_path}")

    client = db_client_slug(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = resolve_tables(conn, args.exclude_tables, args.include_tables)
    if not tables:
        print(f"no tables to export from {db_path} (all excluded/not included)")
        return 0

    # The exact CREATE TABLE SQL, not just Parquet-inferred types — see the
    # module docstring's "Schema fidelity" note on why this matters for restore.
    schema = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN (%s)"
            % ",".join("?" * len(tables)),
            tables,
        )
    }

    local_out = ROOT / "data" / f"{client}_tables"
    local_out.mkdir(parents=True, exist_ok=True)
    (local_out / SCHEMA_NAME).write_text(json.dumps(schema, indent=1, sort_keys=True))

    s3 = None
    if not args.dry_run and args.bucket and args.prefix:
        s3 = _client(args.region)
    prefix = db_prefix(args.prefix, client) if args.bucket else ""

    print(f"exporting {len(tables)} table(s) from {db_path.name} (client: {client})")
    total_rows = 0
    for t in tables:
        data, n = table_to_parquet_bytes(conn, t)
        local_path = local_out / f"{t}.parquet"
        local_path.write_bytes(data)
        total_rows += n
        line = f"  {t:<24} {n:>7} row(s)  {len(data)/1e3:.1f} KB  -> {local_path.relative_to(ROOT)}"
        if args.bucket:
            key = f"{prefix}/{TABLE_SEG}/{t}.parquet"
            if s3:
                s3.put_object(Bucket=args.bucket, Key=key, Body=data,
                             ContentType="application/octet-stream", CacheControl="no-cache")
                line += f"  -> s3://{args.bucket}/{key}"
            else:
                line += f"  would put s3://{args.bucket}/{key} (dry-run)"
        print(line)

    if args.bucket:
        skey = f"{prefix}/{TABLE_SEG}/{SCHEMA_NAME}"
        if s3:
            s3.put_object(Bucket=args.bucket, Key=skey,
                         Body=json.dumps(schema, indent=1, sort_keys=True).encode(),
                         ContentType="application/json", CacheControl="no-cache")
            print(f"  {'(schema)':<24} {'':<7}          -> s3://{args.bucket}/{skey}")
        else:
            print(f"  {'(schema)':<24} would put s3://{args.bucket}/{skey} (dry-run)")

    print(f"\n{len(tables)} table(s), {total_rows} total row(s)")
    if not args.bucket:
        print("no --bucket given — wrote local Parquet only, nothing uploaded")
    conn.close()
    return 0


# ------------------------------------------------------------------------ restore


def _download_client_tables(s3, bucket: str, prefix: str, client: str) -> tuple[dict, dict]:
    """Return (schema dict, {table: parquet bytes}) for everything currently
    in S3 under this client's tables/ segment."""
    full_prefix = db_prefix(prefix, client)
    tprefix = f"{full_prefix}/{TABLE_SEG}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=tprefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))

    schema = {}
    tables: dict[str, bytes] = {}
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if name == SCHEMA_NAME:
            schema = json.loads(body)
        elif name.endswith(".parquet"):
            tables[name[: -len(".parquet")]] = body
    return schema, tables


def _rebuild_db(db_path: Path, schema: dict[str, str], tables: dict[str, bytes]) -> int:
    """Create db_path fresh (it must not already exist) from captured schema +
    Parquet bytes. Returns total rows written."""
    if db_path.exists():
        raise FileExistsError(db_path)  # callers are responsible for backing up/removing first
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    total = 0
    try:
        for name, create_sql in schema.items():
            conn.execute(create_sql)
        for name, data in tables.items():
            t = parquet_bytes_to_rows(data)
            cols = t.schema.names
            placeholders = ",".join("?" * len(cols))
            rows = list(zip(*[t.column(c).to_pylist() for c in cols])) if t.num_rows else []
            if rows:
                conn.executemany(f"INSERT INTO {name} ({','.join(cols)}) VALUES ({placeholders})", rows)
            total += len(rows)
        conn.commit()
    finally:
        conn.close()
    return total


def _carry_over_local_only_tables(db_path: Path, backup_path: Path, restored_tables: set[str]) -> list[str]:
    """A restore rebuilds db_path from nothing but what S3 had — anything
    local-only that was never part of the export scope (this codebase's own
    eval_runs/eval_task_results/llm_calls, or any future DEFAULT_EXCLUDE
    addition) would otherwise only survive in the one-shot .pre-sync-backup
    file, which the *next* restore overwrites. Copy those tables forward
    from the backup into the freshly rebuilt db instead of leaving that as
    the sole safety net. Returns the list of table names carried over."""
    old = sqlite3.connect(backup_path)
    old_tables = [
        r[0] for r in old.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchall()
    ]
    carry = [t for t in old_tables if t not in restored_tables]
    old.close()
    if not carry:
        return []

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ATTACH DATABASE ? AS old", (str(backup_path),))
        for t in carry:
            create_sql = conn.execute(
                "SELECT sql FROM old.sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()[0]
            conn.execute(create_sql)
            conn.execute(f"INSERT INTO main.{t} SELECT * FROM old.{t}")
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE old")
        conn.close()
    return carry


def cmd_restore(args) -> int:
    db_path = Path(args.db) if Path(args.db).is_absolute() else ROOT / args.db
    client = db_client_slug(db_path)

    if db_path.exists() and not args.force:
        sys.exit(f"{db_path} already exists — pass --force to overwrite "
                 f"(a backup is still made first) or use `sync` instead")

    s3 = _client(args.region)
    print(f"downloading tables/ for client '{client}' from s3://{args.bucket}/"
          f"{db_prefix(args.prefix, client)}/{TABLE_SEG}/")
    schema, tables = _download_client_tables(s3, args.bucket, args.prefix, client)
    if not tables:
        sys.exit(f"no table exports found in S3 for client '{client}' — nothing to restore")

    backup = None
    if db_path.exists():
        backup = db_path.with_suffix(db_path.suffix + ".pre-sync-backup")
        shutil.copy2(db_path, backup)
        db_path.unlink()
        print(f"  backed up existing {db_path.relative_to(ROOT)} -> {backup.relative_to(ROOT)}")

    total = _rebuild_db(db_path, schema, tables)
    print(f"rebuilt {db_path.relative_to(ROOT)}: {len(tables)} table(s), {total} total row(s)")

    if backup is not None:
        carried = _carry_over_local_only_tables(db_path, backup, set(tables))
        if carried:
            print(f"  carried over local-only table(s) not in S3 scope: {', '.join(carried)}")
    return 0


# --------------------------------------------------------------------------- sync


def _s3_latest_modified(s3, bucket: str, prefix: str, client: str):
    """Max LastModified across this client's tables/ objects, or None if S3
    has nothing for this client yet."""
    full_prefix = db_prefix(prefix, client)
    paginator = s3.get_paginator("list_objects_v2")
    latest = None
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{full_prefix}/{TABLE_SEG}/"):
        for o in page.get("Contents", []):
            if latest is None or o["LastModified"] > latest:
                latest = o["LastModified"]
    return latest


def cmd_sync(args) -> int:
    db_path = Path(args.db) if Path(args.db).is_absolute() else ROOT / args.db
    client = db_client_slug(db_path)
    s3 = _client(args.region)

    local_exists = db_path.is_file()
    local_mtime = (
        datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc) if local_exists else None
    )
    s3_mtime = _s3_latest_modified(s3, args.bucket, args.prefix, client)

    print(f"sync '{client}': local mtime = {local_mtime or 'missing'}, "
          f"S3 latest table modified = {s3_mtime or 'nothing in S3 yet'}")

    if not local_exists and s3_mtime is None:
        print("neither side has data for this client — nothing to do")
        return 0
    if not local_exists:
        print("local db missing — pulling from S3 (restore)")
        return cmd_restore(argparse.Namespace(db=args.db, bucket=args.bucket,
                                              prefix=args.prefix, region=args.region, force=False))
    if s3_mtime is None:
        print("S3 has nothing for this client yet — pushing local (export)")
        return cmd_export(argparse.Namespace(db=args.db, bucket=args.bucket, prefix=args.prefix,
                                             region=args.region, exclude_tables=args.exclude_tables,
                                             include_tables=args.include_tables, dry_run=False))

    if local_mtime > s3_mtime:
        print("local is more recently modified — pushing to S3 (export)")
        return cmd_export(argparse.Namespace(db=args.db, bucket=args.bucket, prefix=args.prefix,
                                             region=args.region, exclude_tables=args.exclude_tables,
                                             include_tables=args.include_tables, dry_run=False))
    elif s3_mtime > local_mtime:
        print("S3 is more recently modified — pulling from S3 (restore), "
              "backing up local first")
        return cmd_restore(argparse.Namespace(db=args.db, bucket=args.bucket,
                                              prefix=args.prefix, region=args.region, force=True))
    else:
        print("in sync (timestamps match) — nothing to do")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--db", required=True, help="e.g. data/trex.db")
        p.add_argument("--bucket", default="")
        p.add_argument("--prefix", default="outbound/competitive-intel")
        p.add_argument("--region", default="us-east-1")

    ex = sub.add_parser("export", help="local db -> S3 (Parquet + schema)")
    common(ex)
    ex.add_argument("--exclude-tables", default="",
                    help=f"comma-separated, overrides the default exclude set ({','.join(sorted(DEFAULT_EXCLUDE))})")
    ex.add_argument("--include-tables", default="",
                    help="comma-separated allow-list; if set, only these tables are considered")
    ex.add_argument("--dry-run", action="store_true",
                    help="write local Parquet only, skip the S3 upload even if --bucket is given")
    ex.set_defaults(func=cmd_export)

    rs = sub.add_parser("restore", help="S3 -> local db (rebuilds it fresh)")
    common(rs)
    rs.add_argument("--force", action="store_true",
                    help="overwrite an existing local db (still backed up first)")
    rs.set_defaults(func=cmd_restore)

    sy = sub.add_parser("sync", help="most-recently-modified side wins; see module docstring")
    common(sy)
    sy.add_argument("--exclude-tables", default="")
    sy.add_argument("--include-tables", default="")
    sy.set_defaults(func=cmd_sync)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
