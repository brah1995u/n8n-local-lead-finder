import os
import pytest
import psycopg

from app import db


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    original=os.environ.get("DATABASE_URL","")
    if not original:
        pytest.skip("Integration tests require dedicated Docker PostgreSQL")
    url=original.rsplit("/",1)[0]+"/cortex_test"
    monkeypatch.setenv("DATABASE_URL",url)
    with psycopg.connect(original.rsplit("/",1)[0]+"/postgres",autocommit=True) as c:
        exists=c.execute("SELECT 1 FROM pg_database WHERE datname='cortex_test'").fetchone()
        if not exists:
            c.execute("CREATE DATABASE cortex_test")
    db.init()
    with db.connect() as c:
        c.execute("TRUNCATE settings,jobs,passes,places,job_places,suppressions,budgets,events RESTART IDENTITY CASCADE")
    yield
