"""Due-date rule (PLAN 1.3): 7 days, Friday to Friday."""
from datetime import date, timedelta

FRIDAY = 4


def due_date_for(take: date, loan_days: int = 7) -> date:
    """A book taken on a Friday is due the next Friday. A book taken on any
    other day is due on the first Friday at least `loan_days` days later."""
    due = take + timedelta(days=loan_days)
    while due.weekday() != FRIDAY:
        due += timedelta(days=1)
    return due


def parse(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value[:10])
