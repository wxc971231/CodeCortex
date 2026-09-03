from m1a_fixture.models import Record
from m1a_fixture.reporting.formatters import format_total
from m1a_fixture.reporting.summary import summarize


def render_report(records: list[Record]) -> str:
    """Render the report after imported records were validated."""
    return format_total(summarize(records))
