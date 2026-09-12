from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanySeed:
    id: str
    sector: str
    name: str
    ticker: str
    cik: str
    fiscal_year_end: str
    revenue: float
    gross_margin: float
    operating_margin: float
    cash_from_operations: float
    capex: float
    cash: float
    debt: float
    market_cap: float
    net_income_margin: float
    headcount: int
    growth: float


COMPANIES: tuple[CompanySeed, ...] = (
    CompanySeed("box", "tech", "Box", "BOX", "0001372612", "01-31", 1090, 0.795, 0.12, 350, 25, 700, 470, 4200, 0.10, 2810, 0.05),
    CompanySeed("dropbox", "tech", "Dropbox", "DBX", "0001467623", "12-31", 2540, 0.81, 0.20, 900, 65, 620, 1550, 9200, 0.18, 2130, 0.02),
    CompanySeed("teradata", "tech", "Teradata", "TDC", "0000816761", "12-31", 1750, 0.60, 0.14, 355, 55, 340, 690, 2400, 0.08, 5700, -0.03),
    CompanySeed("rapid7", "tech", "Rapid7", "RPD", "0001560327", "12-31", 850, 0.70, -0.01, 110, 18, 225, 970, 1500, -0.04, 2200, 0.08),
    CompanySeed("anf", "retail", "Abercrombie & Fitch", "ANF", "0001018840", "01-31", 4950, 0.64, 0.15, 780, 220, 690, 310, 4700, 0.11, 31000, 0.12),
    CompanySeed("urban", "retail", "Urban Outfitters", "URBN", "0000912615", "01-31", 5550, 0.36, 0.10, 620, 250, 540, 120, 4900, 0.08, 29000, 0.10),
    CompanySeed("dicks", "retail", "Dick's Sporting Goods", "DKS", "0001089063", "01-31", 13400, 0.36, 0.11, 1500, 750, 1650, 1480, 12400, 0.08, 59000, 0.05),
    CompanySeed("best_buy", "retail", "Best Buy", "BBY", "0000764478", "01-31", 41500, 0.23, 0.04, 2100, 850, 1450, 3920, 14500, 0.03, 85000, -0.04),
    CompanySeed("gxo", "logistics", "GXO Logistics", "GXO", "0001852244", "12-31", 11500, 0.16, 0.04, 800, 525, 560, 2870, 6100, 0.02, 154000, 0.18),
    CompanySeed("hub_group", "logistics", "Hub Group", "HUBG", "0000940942", "12-31", 4000, 0.12, 0.05, 390, 170, 210, 390, 2300, 0.04, 6000, -0.02),
    CompanySeed("arcbest", "logistics", "ArcBest", "ARCB", "0000894405", "12-31", 4200, 0.12, 0.05, 350, 190, 170, 240, 1700, 0.04, 15000, -0.06),
    CompanySeed("ch_robinson", "logistics", "C.H. Robinson", "CHRW", "0001043277", "12-31", 17600, 0.07, 0.04, 750, 95, 180, 1250, 11900, 0.03, 13500, 0.03),
)

BENCHMARKS = {"tech": "IGV", "retail": "XRT", "logistics": "IYT"}

