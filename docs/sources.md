# Sources, provenance, and data quality

Every stored fact or evidence row references a record in `sources`, including an HTTPS URL and retrieval date. The project recognizes these live source families:

- SEC Company Facts API for standardized issuer facts: `https://data.sec.gov/api/xbrl/companyfacts/`
- SEC Submissions API and filing archives for metadata and filing text: `https://data.sec.gov/submissions/` and `https://www.sec.gov/Archives/edgar/data/`
- Issuer investor-relations releases for non-GAAP and sector-specific context when available
- Yahoo Finance historical pages for dated ETF benchmark snapshots (IGV, XRT, IYT)

The project includes a fixed SEC tag registry, a read-through SEC cache, primary 10-K/10-Q filing extraction, and a dated Yahoo-history loader. `--mode live` retains the exact SEC period, form, filing date, accession, and direct filing URL used for each row. It extracts explicit employee totals and ranked filing sentences for strategy, growth, risk, competition, labor, operating levers, automation, and customer concentration, while filtering filing boilerplate. Only supported deterministic financial derivations are calculated.

The committed `data/financial_agent.db` is a point-in-time snapshot built in live mode from SEC filings and dated Yahoo Finance benchmark history. It contains 12 covered companies, 380 financial-metric rows, 55 operating signals, 148 qualitative-evidence rows, and 64 source records. It contains no synthetic fixture sources. Runtime MCP and agent queries read this local database and do not access the internet.

## Data-quality caveats

- **XBRL variation:** issuers can use extension concepts or different standard concepts for economically similar facts. A missing mapped tag must remain missing.
- **Fiscal alignment:** fiscal calendars and quarter boundaries differ, so cross-company periods may not be perfectly synchronized.
- **Non-GAAP comparability:** adjusted earnings and issuer-defined KPIs can use different exclusions and should not be compared without reconciliation.
- **Market staleness:** prices, market caps, enterprise values, and ETF returns are point-in-time snapshots with explicit `as_of` dates.
- **Headcount frequency:** employee disclosures are commonly annual and can materially lag the latest operating quarter.
- **Qualitative availability:** filing language varies. Live mode reports each company for which it cannot extract an explicit employee total or at least three useful qualitative disclosures; it never substitutes synthetic statements.
- **Debt composition:** live normalization prefers a reported total-debt concept. Otherwise it sums matching current and noncurrent components; if both are not present for the same filing context, debt remains missing rather than understated.
- **Quarterly contexts:** live normalization selects quarter-only duration contexts for quarterly income-statement metrics. Cumulative YTD cash-flow facts are omitted when a quarter-only context is unavailable; they are not mislabeled as quarterly values.
- **Mixed market provenance:** live market cap uses the last Yahoo close on or before the SEC reporting date and a composite source record naming both the SEC accession and Yahoo observation. Live enterprise-value multiples remain missing because the six-table schema cannot attach two source rows to one derived fact.

Model output is post-validated: companies, source IDs, and MCP tool names must have appeared in the current run, and all tool calls must match the request's sector. Unsupported company requests return `out_of_scope`; missing information must be stated as a limitation rather than silently estimated.
