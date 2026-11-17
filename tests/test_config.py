"""
test_config.py

Checks the settings parsing and that the local db answers when it's running.
"""

import pytest

from pipeline import config


def test_bbox_parses_four_numbers():
    assert config._bbox("-81.0,42.8,-78.5,44.6") == (-81.0, 42.8, -78.5, 44.6)


def test_bbox_rejects_wrong_length():
    with pytest.raises(ValueError):
        config._bbox("1,2,3")


def test_local_conn_str_uses_etl_app(monkeypatch):
    monkeypatch.delenv("SQL_CONN_STR", raising=False)
    monkeypatch.setenv("LOCAL_ETL_PASSWORD", "pw1")
    conn = config.sql_conn_str()
    assert "Uid=etl_app;Pwd=pw1;" in conn
    assert "{" + "LOCAL_ETL_PASSWORD}" not in conn


def test_admin_conn_str_uses_sa(monkeypatch):
    monkeypatch.delenv("SQL_ADMIN_CONN_STR", raising=False)
    monkeypatch.setenv("LOCAL_SA_PASSWORD", "pw2")
    assert "Uid=sa;Pwd=pw2;" in config.sql_conn_str(admin=True)


@pytest.mark.db
def test_select_version():
    from pipeline import db

    assert "Microsoft SQL Server" in db.server_version()
