"""A hard cap on Anthropic spend, summed from every stored response."""

from adsight.llm.base import BudgetExceeded
from adsight.llm.cache import ResponseCache


class BudgetGuard:
    """Raises BudgetExceeded before any Anthropic request once spend reaches the budget.

    The budget comes from ADSIGHT_ANTHROPIC_BUDGET_USD; the default 0 allows no call.
    """

    def __init__(self, budget_usd: float, cache: ResponseCache, provider: str = "anthropic"):
        self.budget_usd, self.cache, self.provider = budget_usd, cache, provider

    def spent(self) -> float:
        return round(self.cache.spent_usd(self.provider), 9)  # no float dust at the cap

    def check(self) -> None:
        spent = self.spent()
        if spent >= self.budget_usd:
            raise BudgetExceeded(spent, self.budget_usd)
