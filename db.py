"""SQLite store. Zero-install local DB (agency.db).
Holds 90d account-level + 30d campaign-level daily insights so the dashboard
can slice any period and compute vs-previous-period deltas."""
import os
import sqlite3
from pathlib import Path

# Override with AGENCY_DB env var (used for mock/test DBs); defaults to agency.db
DB_PATH = Path(os.environ.get("AGENCY_DB", Path(__file__).parent / "agency.db"))

SCHEMA = """
create table if not exists accounts (
    id          text primary key,
    account_id  text,
    name        text,
    currency    text,
    status      text,
    updated_at  text
);
create table if not exists campaigns (
    id           text primary key,
    account_id   text,
    name         text,
    objective    text,
    status       text,
    daily_budget real,
    updated_at   text
);
-- both account-level (campaign_id='ACCOUNT') and per-campaign daily rows
create table if not exists insights_daily (
    account_id   text,
    campaign_id  text,
    date         text,
    spend        real default 0,
    impressions  integer default 0,
    clicks       integer default 0,
    conversions  real default 0,
    conv_value   real default 0,
    primary key (account_id, campaign_id, date)
);
create table if not exists geo_daily (
    account_id   text,
    date         text,
    country      text,
    spend        real default 0,
    impressions  integer default 0,
    clicks       integer default 0,
    conversions  real default 0,
    primary key (account_id, date, country)
);
-- ad/creative level (for fatigue monitoring)
create table if not exists creatives (
    id           text primary key,         -- ad_id
    account_id   text,
    campaign_id  text,
    name         text,
    is_video     integer default 0,
    updated_at   text
);
create table if not exists creative_daily (
    ad_id        text,
    account_id   text,
    date         text,
    spend        real default 0,
    impressions  integer default 0,
    clicks       integer default 0,
    link_clicks  integer default 0,
    video_3s     integer default 0,        -- 3-sec views (true hook signal)
    thruplay     integer default 0,        -- hold-rate signal
    frequency    real default 0,
    cpm          real default 0,
    ctr          real default 0,
    primary key (ad_id, date)
);
create index if not exists idx_cd_acct on creative_daily(account_id, date);
create index if not exists idx_ins_date on insights_daily(date);
create index if not exists idx_ins_acct on insights_daily(account_id);
create index if not exists idx_geo_acct on geo_daily(account_id, date);
-- audit trail for every control action (pause/resume/budget)
create table if not exists actions_log (
    id            integer primary key autoincrement,
    ts            text,
    account_id    text,
    account_name  text,
    level         text,      -- campaign / ad
    object_id     text,
    object_name   text,
    action        text,      -- pause / resume / set_budget
    before_state  text,
    after_state   text,
    status        text,      -- success / failed / dryrun
    detail        text
);
-- monitoring alerts (dead spend, stalled, no-conv, fatigue, status, cpa spike)
create table if not exists alerts (
    id            integer primary key autoincrement,
    key           text unique,   -- account_id:type dedupe key
    account_id    text,
    account_name  text,
    severity      text,          -- info / warn / critical
    type          text,
    message       text,
    status        text default 'open',   -- open / ack / resolved
    delivered     integer default 0,
    created_at    text,
    updated_at    text
);
-- user-defined monthly budgets per account (Meta has no monthly budget concept)
create table if not exists account_budgets (
    account_id     text primary key,
    monthly_budget real,
    updated_at     text
);
-- ad sets (for Advantage+ audience / AI-targeting detection)
create table if not exists ad_sets (
    id                 text primary key,
    account_id         text,
    campaign_id        text,
    name               text,
    status             text,
    advantage_audience integer default 0,
    updated_at         text
);
-- per-account guardrail settings (expected landing-page pattern override)
create table if not exists account_settings (
    account_id    text primary key,
    expected_url  text,
    updated_at    text
);
-- sales from the bookings sheet — PRIVACY: no names/emails/phones stored,
-- only a one-way hash for dedup + non-identifying fields
create table if not exists sales (
    dedup_key    text primary key,   -- sha1(email|phone|product|date)
    location     text,
    date         text,
    ts           text,
    product      text,
    utm_source   text,
    utm_medium   text,
    utm_campaign text
);
create table if not exists app_meta (k text primary key, v text);
create index if not exists idx_sales_loc on sales(location);
"""


def _migrate(con):
    """Add columns to existing tables (idempotent)."""
    for stmt in (
        "alter table creatives add column final_url text",
        "alter table creatives add column status text",
        "alter table campaigns add column smart_promotion_type text",
    ):
        try:
            con.execute(stmt)
        except Exception:
            pass


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init():
    con = connect()
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    con.close()


def upsert(con, table, row):
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    con.execute(
        f"insert or replace into {table} ({','.join(cols)}) values ({placeholders})",
        [row[c] for c in cols],
    )
