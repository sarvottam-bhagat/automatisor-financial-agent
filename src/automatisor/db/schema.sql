PRAGMA foreign_keys = ON;

CREATE TABLE companies (
    id TEXT PRIMARY KEY,
    sector TEXT NOT NULL CHECK (sector IN ('tech', 'retail', 'logistics')),
    name TEXT NOT NULL,
    ticker TEXT NOT NULL UNIQUE,
    cik TEXT NOT NULL UNIQUE,
    fiscal_year_end TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    publisher TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    published_date TEXT,
    retrieved_date TEXT NOT NULL,
    accession_number TEXT
);

CREATE TABLE financial_metrics (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    metric_name TEXT NOT NULL,
    numeric_value REAL NOT NULL,
    unit TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    fiscal_period TEXT NOT NULL,
    form TEXT NOT NULL,
    is_derived INTEGER NOT NULL DEFAULT 0 CHECK (is_derived IN (0, 1)),
    source_id TEXT NOT NULL REFERENCES sources(id),
    UNIQUE (company_id, metric_name, period_end, fiscal_period)
);

CREATE TABLE operating_signals (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    signal_type TEXT NOT NULL,
    numeric_value REAL,
    text_value TEXT,
    unit TEXT,
    observed_period TEXT NOT NULL,
    confidence_level TEXT NOT NULL CHECK (confidence_level IN ('high', 'medium', 'low')),
    source_id TEXT NOT NULL REFERENCES sources(id)
);

CREATE TABLE qualitative_evidence (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    topic TEXT NOT NULL,
    statement TEXT NOT NULL,
    scope TEXT NOT NULL,
    period TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(id)
);

CREATE TABLE benchmarks (
    id INTEGER PRIMARY KEY,
    sector TEXT NOT NULL CHECK (sector IN ('tech', 'retail', 'logistics')),
    benchmark_ticker TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    price_return REAL NOT NULL,
    peer_metric TEXT NOT NULL,
    peer_median_value REAL NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(id),
    UNIQUE (sector, period_end, peer_metric)
);

CREATE INDEX idx_companies_sector ON companies(sector, active);
CREATE INDEX idx_financial_company_metric_period ON financial_metrics(company_id, metric_name, period_end DESC);
CREATE INDEX idx_financial_source ON financial_metrics(source_id);
CREATE INDEX idx_signals_company_period ON operating_signals(company_id, signal_type, observed_period DESC);
CREATE INDEX idx_evidence_company_topic ON qualitative_evidence(company_id, topic);
CREATE INDEX idx_evidence_source ON qualitative_evidence(source_id);
CREATE INDEX idx_benchmarks_sector_period ON benchmarks(sector, period_end DESC);

