from __future__ import annotations

from collections.abc import Mapping


SEC_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "cash_from_operations": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "shares": ("EntityCommonStockSharesOutstanding",),
}

DEBT_TOTAL_TAGS = (
    "LongTermDebtAndFinanceLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligations",
)
DEBT_CURRENT_TAGS = (
    "LongTermDebtAndFinanceLeaseObligationsCurrent",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "LongTermDebtCurrent",
)
DEBT_NONCURRENT_TAGS = (
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
    "LongTermDebtNoncurrent",
)


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6)


def calculate_derived_metrics(values: Mapping[str, float]) -> dict[str, float]:
    revenue = float(values["revenue"])
    gross_profit = float(values["gross_profit"])
    operating_income = float(values["operating_income"])
    cash_from_operations = float(values["cash_from_operations"])
    capex = abs(float(values["capex"]))
    cash = float(values["cash"])
    debt = float(values["debt"])
    market_cap = float(values["market_cap"])
    free_cash_flow = cash_from_operations - capex
    net_debt = debt - cash
    enterprise_value = market_cap + net_debt
    return {
        "gross_margin": _ratio(gross_profit, revenue),
        "operating_margin": _ratio(operating_income, revenue),
        "free_cash_flow": round(free_cash_flow, 6),
        "fcf_margin": _ratio(free_cash_flow, revenue),
        "net_debt": round(net_debt, 6),
        "enterprise_value": round(enterprise_value, 6),
        "ev_to_revenue": _ratio(enterprise_value, revenue),
    }
