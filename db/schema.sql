-- Phase 1 starter schema (Postgres / Supabase).
-- Run once the connection test passes. Time-series design: one row per
-- account/campaign per day, so trends, fatigue, and blended ROAS are just queries.

create table if not exists accounts (
    id            text primary key,           -- act_1234567890
    account_id    text not null,
    name          text,
    currency      text,
    status        text,
    client_name   text,                        -- your label for the client
    created_at    timestamptz default now(),
    updated_at    timestamptz default now()
);

create table if not exists campaigns (
    id            text primary key,
    account_id    text references accounts(id),
    name          text,
    objective     text,
    status        text,                        -- ACTIVE / PAUSED / ...
    daily_budget  numeric,
    updated_at    timestamptz default now()
);

-- daily fact table — the backbone of the whole dashboard
create table if not exists insights_daily (
    account_id    text references accounts(id),
    campaign_id   text,
    date          date not null,
    spend         numeric default 0,
    impressions   bigint default 0,
    clicks        bigint default 0,
    conversions   numeric default 0,
    revenue       numeric default 0,          -- filled later for blended ROAS
    ctr           numeric,
    cpc           numeric,
    cpa           numeric,
    roas          numeric,
    primary key (account_id, campaign_id, date)
);

create table if not exists alerts (
    id            bigserial primary key,
    account_id    text,
    campaign_id   text,
    rule          text,                        -- which rule fired
    severity      text,                        -- info / warn / critical
    message       text,
    status        text default 'open',         -- open / acknowledged / resolved
    created_at    timestamptz default now()
);

create index if not exists idx_insights_date on insights_daily(date);
create index if not exists idx_insights_acct on insights_daily(account_id);
