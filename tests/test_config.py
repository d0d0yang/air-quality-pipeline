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


def test_local_conn_str_fills_password(monkeypatch):
    monkeypatch.delenv("SQL_CONN_STR", raising=False)
    monkeypatch.setenv("LOCAL_SA_PASSWORD", "not-a-real-pw")
    conn = config.sql_conn_str()
    assert "Pwd=not-a-real-pw;" in conn
    assert "{LOCAL_SA_PASSWORD}" not in conn


@pytest.mark.db
def test_select_version():
    from pipeline import db

    assert "Microsoft SQL Server" in db.server_version()
