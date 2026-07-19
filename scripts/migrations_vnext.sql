-- Additive migration script (backup/reference)
-- Generated for Market Signal Lab vNext (snapshots/sentiment/risk events + attribution)
-- NOTE: This script is intended as a reference. The application also performs auto-migration
-- via core/portfolio.py:init_tables() using CREATE/ALTER with try/except.

-- 1) factor_snapshot (candidate factor/tag snapshot)
CREATE TABLE IF NOT EXISTS factor_snapshot (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_date VARCHAR(20),
  strategy VARCHAR(50),
  analysis_cycle VARCHAR(10) DEFAULT 'T+1',
  code VARCHAR(20),
  ts_code VARCHAR(20),
  name VARCHAR(50),
  snapshot_version VARCHAR(20),
  score_total FLOAT,
  factors_json LONGTEXT,
  tags_json LONGTEXT,
  data_quality VARCHAR(20),
  created_at DATETIME,
  INDEX idx_trade_date (trade_date),
  INDEX idx_strategy (strategy),
  INDEX idx_code_date (code, trade_date)
);

-- 2) market_sentiment_daily
CREATE TABLE IF NOT EXISTS market_sentiment_daily (
  trade_date VARCHAR(20) PRIMARY KEY,
  weather VARCHAR(10),
  risk_level VARCHAR(20),
  is_safe TINYINT(1) DEFAULT 1,
  message VARCHAR(255),
  limit_up INT,
  limit_down INT,
  limit_up_height INT,
  ladder_json LONGTEXT,
  sector_top_json LONGTEXT,
  ecosystem_json LONGTEXT,
  created_at DATETIME
);

-- 3) risk_event_log
CREATE TABLE IF NOT EXISTS risk_event_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_time DATETIME,
  account VARCHAR(20),
  code VARCHAR(20),
  event_type VARCHAR(30),
  weather VARCHAR(10),
  reason VARCHAR(255),
  params_json LONGTEXT,
  INDEX idx_event_time (event_time),
  INDEX idx_code_time (code, event_time)
);

-- 4) evolution_audit_log (for evolver changes)
CREATE TABLE IF NOT EXISTS evolution_audit_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_time DATETIME,
  dry_run TINYINT(1) DEFAULT 0,
  window_days INT,
  changes_json LONGTEXT,
  metrics_json LONGTEXT,
  created_at DATETIME,
  INDEX idx_run_time (run_time)
);

-- 5) Extend existing tables (best-effort; may fail on already-applied DB)
ALTER TABLE strategy_selection ADD COLUMN snapshot_id BIGINT AFTER analysis_cycle;
ALTER TABLE strategy_selection ADD COLUMN score_total FLOAT AFTER snapshot_id;
ALTER TABLE strategy_selection ADD COLUMN tags_json LONGTEXT AFTER score_total;
ALTER TABLE strategy_selection ADD COLUMN data_quality VARCHAR(20) AFTER tags_json;

ALTER TABLE positions ADD COLUMN entry_snapshot_id BIGINT AFTER created_at;
ALTER TABLE positions ADD COLUMN entry_strategy VARCHAR(50) AFTER entry_snapshot_id;
ALTER TABLE positions ADD COLUMN entry_tags_json LONGTEXT AFTER entry_strategy;

ALTER TABLE transactions ADD COLUMN snapshot_id BIGINT AFTER reason;
ALTER TABLE transactions ADD COLUMN source_strategy VARCHAR(50) AFTER snapshot_id;
ALTER TABLE transactions ADD COLUMN weather VARCHAR(10) AFTER source_strategy;
ALTER TABLE transactions ADD COLUMN signal_tags_json LONGTEXT AFTER weather;
ALTER TABLE transactions ADD COLUMN selection_id INT AFTER signal_tags_json;
