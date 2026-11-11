import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init():
    with connect() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
          id uuid PRIMARY KEY, city_id text NOT NULL, city_name text NOT NULL,
          status text NOT NULL DEFAULT 'queued', mode text NOT NULL,
          pilot_limit int, reason text, created_at timestamptz DEFAULT now(),
          updated_at timestamptz DEFAULT now());
        CREATE TABLE IF NOT EXISTS passes (
          id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), key text NOT NULL,
          kind text NOT NULL, input jsonb NOT NULL, status text NOT NULL DEFAULT 'pending',
          run_id text, dataset_id text, offset_count int DEFAULT 0,
          reserved_usd numeric DEFAULT 0, cost_usd numeric DEFAULT 0, result_count int DEFAULT 0,
          attempts int DEFAULT 0, reason text, created_at timestamptz DEFAULT now(),
          updated_at timestamptz DEFAULT now(), UNIQUE(job_id,key));
        CREATE TABLE IF NOT EXISTS places (
          id uuid PRIMARY KEY, dedupe_key text UNIQUE NOT NULL, city_id text NOT NULL,
          name text NOT NULL, phone text, address text, place_id text,
          raw jsonb NOT NULL, inspection jsonb, inspect_status text DEFAULT 'pending',
          attempts int DEFAULT 0, airtable_id text, synced_hash text, crm_status text,
          created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
        CREATE UNIQUE INDEX IF NOT EXISTS places_google_id ON places(place_id) WHERE place_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS job_places (
          job_id uuid REFERENCES jobs(id), place_id uuid REFERENCES places(id),
          PRIMARY KEY(job_id,place_id));
        CREATE TABLE IF NOT EXISTS suppressions (
          key text PRIMARY KEY, reason text NOT NULL, source text NOT NULL,
          updated_at timestamptz DEFAULT now());
        CREATE TABLE IF NOT EXISTS budgets (
          service text, cycle text, calls int DEFAULT 0, reserved_usd numeric DEFAULT 0,
          PRIMARY KEY(service,cycle));
        CREATE TABLE IF NOT EXISTS events (
          id bigserial PRIMARY KEY, level text NOT NULL, stage text NOT NULL,
          message text NOT NULL, created_at timestamptz DEFAULT now());
        CREATE TABLE IF NOT EXISTS coverage_pages (
          id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), url text NOT NULL,
          status text NOT NULL DEFAULT 'pending', reason text, candidates int DEFAULT 0,
          UNIQUE(job_id,url));
        """)


def get(key, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else default


def put(key, value):
    with connect() as c:
        c.execute("INSERT INTO settings VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, Jsonb(value)))


def event(stage, message, level="info"):
    # Only operator-controlled summaries. Never store upstream bodies or secrets here.
    with connect() as c:
        c.execute("INSERT INTO events(level,stage,message) VALUES(%s,%s,%s)", (level, stage, message))


@contextmanager
def lock(name):
    with connect() as c:
        locked = c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (name,)).fetchone()["ok"]
        try:
            yield c if locked else None
        finally:
            if locked:
                c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
