from m1a_fixture.models import Record


def summarize(records: list[Record]) -> int:
    return sum(record.value for record in records)
