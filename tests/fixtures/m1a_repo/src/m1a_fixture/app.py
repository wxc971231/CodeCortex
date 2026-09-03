from .ingestion.service import import_records
from .reporting.service import render_report


def run(lines: list[str]) -> str:
    return render_report(import_records(lines))
