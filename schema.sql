-- Options Wheel Tracker — Supabase Schema
-- Run this once in the Supabase SQL editor for your project.

create extension if not exists "pgcrypto";

-- ─────────────────────────────────────────
-- WHEEL CYCLES
-- Must be created before trades (FK reference)
-- ─────────────────────────────────────────
create table if not exists wheel_cycles (
  id            uuid primary key default gen_random_uuid(),
  symbol        text not null,
  status        text not null default 'active' check (status in ('active', 'completed')),
  started_at    date,
  completed_at  date,
  notes         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ─────────────────────────────────────────
-- TRADES
-- ─────────────────────────────────────────
create table if not exists trades (
  id              uuid primary key default gen_random_uuid(),
  symbol          text not null,
  -- CC = Covered Call, CSP = Cash-Secured Put, Call, Put, Stock
  option_type     text not null check (option_type in ('CC', 'CSP', 'Call', 'Put', 'Stock')),
  strike          numeric(12, 4),
  expiration_date date,
  quantity        integer not null default 1,

  -- Premiums are per-share; multiply by 100 * |quantity| for total dollar value
  open_premium    numeric(12, 4),   -- credit received when opening (positive)
  close_premium   numeric(12, 4),   -- debit paid when closing (positive)
  fees            numeric(8, 4) not null default 0,

  open_date       date not null,
  close_date      date,

  status          text not null default 'open'
                  check (status in ('open', 'closed', 'expired', 'assigned')),

  wheel_cycle_id  uuid references wheel_cycles(id) on delete set null,

  -- Audit / import tracking
  import_source   text not null default 'manual'
                  check (import_source in ('manual', 'csv_import', 'ai_import')),
  rh_trans_code   text,   -- raw Robinhood code: STO, BTC, OEXP, Buy, Sell, STC

  notes           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists trades_symbol_idx         on trades(symbol);
create index if not exists trades_status_idx         on trades(status);
create index if not exists trades_expiration_idx     on trades(expiration_date);
create index if not exists trades_wheel_cycle_idx    on trades(wheel_cycle_id);

-- ─────────────────────────────────────────
-- POSITIONS (stock holdings)
-- ─────────────────────────────────────────
create table if not exists positions (
  id              uuid primary key default gen_random_uuid(),
  symbol          text not null,
  shares          integer not null,
  purchase_price  numeric(12, 4) not null,   -- average cost basis per share
  close_price     numeric(12, 4),            -- price when position was sold/assigned
  open_date       date not null,
  close_date      date,
  status          text not null default 'open' check (status in ('open', 'closed')),
  notes           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists positions_symbol_idx on positions(symbol);
create index if not exists positions_status_idx on positions(status);

-- ─────────────────────────────────────────
-- AUTO-UPDATE updated_at
-- ─────────────────────────────────────────
create or replace function update_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

create or replace trigger trades_updated_at
  before update on trades
  for each row execute function update_updated_at();

create or replace trigger positions_updated_at
  before update on positions
  for each row execute function update_updated_at();

create or replace trigger wheel_cycles_updated_at
  before update on wheel_cycles
  for each row execute function update_updated_at();
