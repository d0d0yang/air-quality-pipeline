"""
db.py

Small helpers around pyodbc: connect, call procs, bulk load a landing csv into its stg table (the local
stand-in for ADF's Copy activity), and apply the sql/ files in order. `python -m pipeline.db apply` sets it all up.
"""

import csv
import io
import re
import sys
from pathlib import Path

import pyodbc

from pipeline import config, storage

SQL_DIR = config.ROOT / "sql"
GO_LINE = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)
LOAD_CHUNK = 20000


def connect(autocommit=True, admin=False, database=None):
    conn_str = config.sql_conn_str(admin=admin)
    if database:
        conn_str = re.sub(r"(?i)(Database|Initial Catalog)=[^;]*", f"Database={database}", conn_str)
    return pyodbc.connect(conn_str, autocommit=autocommit, timeout=30)


def scalar(sql, *params, admin=False):
    with connect(admin=admin) as conn:
        row = conn.cursor().execute(sql, *params).fetchone()
        return row[0] if row else None


def server_version():
    return scalar("SELECT @@VERSION")


def call(conn, proc, **params):
    """EXEC a proc with named params; returns the first result set as a list of dicts (or [])."""
    names = ", ".join(f"@{k} = ?" for k in params)
    cur = conn.cursor()
    cur.execute(f"SET NOCOUNT ON; EXEC {proc} {names}", *params.values())
    rows = []
    while True:
        if cur.description:
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
            break
        if not cur.nextset():
            break
    return rows


# --- bulk load ----------------------------------------------------------------------------------


def bulk_load(conn, table, path, item_id, store=None):
    """
    Loads one landing csv into its stg table. Deletes the item's old rows first (a pre-copy script in adf),
    so retries and parallel items of the same source never collide. Returns the number of rows loaded.
    """
    data = (store or storage.get_storage()).read_bytes(path).decode("utf-8")
    reader = csv.reader(io.StringIO(data))
    header = next(reader)
    # one transaction for the whole file: with autocommit every row commits on its own (~150 rows/s vs ~20k)
    was_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        total = _insert_rows(conn, table, header, reader, item_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = was_autocommit
    return total


def _insert_rows(conn, table, header, reader, item_id):
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table} WHERE item_id = ?", item_id)
    cur.fast_executemany = True
    # every stg column is text; saying so up front stops the driver guessing types from a null first row
    cur.setinputsizes([(pyodbc.SQL_WVARCHAR, 400, 0)] * len(header))
    sql = f"INSERT INTO {table} ({', '.join(header)}) VALUES ({', '.join('?' * len(header))})"
    total, chunk = 0, []
    for row in reader:
        chunk.append([v if v != "" else None for v in row])
        if len(chunk) >= LOAD_CHUNK:
            cur.executemany(sql, chunk)
            total += len(chunk)
            chunk = []
    if chunk:
        cur.executemany(sql, chunk)
        total += len(chunk)
    return total


# --- apply sql files ----------------------------------------------------------------------------


def split_batches(text):
    return [b.strip() for b in GO_LINE.split(text) if b.strip()]


def render(text, etl_password):
    if "$(ETL_PASSWORD)" in text:
        if not etl_password:
            raise RuntimeError("set LOCAL_ETL_PASSWORD (local) or AZ_SQL_ETL_PASSWORD (azure) "
                               "before applying 90_users.sql")
        if "'" in etl_password:
            raise ValueError("the etl password can't contain a single quote")
        text = text.replace("$(ETL_PASSWORD)", etl_password)
    return text


def ensure_local_database(name):
    """Local docker only: create the database and turn on contained users (azure has both already)."""
    with connect(admin=True, database="master") as conn:
        cur = conn.cursor()
        if cur.execute("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT)").fetchone()[0] == 5:
            return  # azure sql database
        cur.execute("EXEC sp_configure 'contained database authentication', 1; RECONFIGURE;")
        if cur.execute("SELECT DB_ID(?)", name).fetchone()[0] is None:
            cur.execute(f"CREATE DATABASE [{name}]")
        # only when needed: the alter wants an exclusive lock, which fails while anyone is connected
        if cur.execute("SELECT containment FROM sys.databases WHERE name = ?", name).fetchone()[0] == 0:
            cur.execute(f"ALTER DATABASE [{name}] SET CONTAINMENT = PARTIAL WITH NO_WAIT")


def apply(sql_dir=SQL_DIR, etl_password=None, verbose=True, database=None):
    etl_password = etl_password or config.ETL_PASSWORD
    ensure_local_database(database or config.database_name())
    files = sorted(Path(sql_dir).glob("*.sql"))
    with connect(admin=True, database=database) as conn:
        cur = conn.cursor()
        for f in files:
            batches = split_batches(render(f.read_text(encoding="utf-8"), etl_password))
            for batch in batches:
                cur.execute(batch)
                while cur.nextset():
                    pass
            if verbose:
                print(f"applied {f.name} ({len(batches)} batches)")
    return [f.name for f in files]


def main(argv):
    command = argv[0] if argv else "version"
    if command == "version":
        print(server_version())
    elif command == "apply":
        apply()
    else:
        print(f"unknown command: {command} (try: version, apply)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
