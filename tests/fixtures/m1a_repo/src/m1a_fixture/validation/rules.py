from m1a_fixture.models import Record


def validate_record(record: Record) -> bool:
    return bool(record.identifier) and record.value >= 0
