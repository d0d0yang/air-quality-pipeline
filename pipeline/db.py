"""
db.py

Small helpers around pyodbc: connect, run a query, and (later) apply the sql/ files.
Run `python -m pipeline.db version` to check the connection.
"""

import sys

import pyodbc

from pipeline import config


def connect(autocommit=False):
    return pyodbc.connect(config.sql_conn_str(), autocommit=autocommit, timeout=30)


def scalar(sql, *params):
    with connect() as conn:
        row = conn.cursor().execute(sql, *params).fetchone()
        return row[0] if row else None


def server_version():
    return scalar("SELECT @@VERSION")


def main(argv):
    command = argv[0] if argv else "version"
    if command == "version":
        print(server_version())
    else:
        print(f"unknown command: {command} (try: version)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
