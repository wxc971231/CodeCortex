from m1a_fixture.ingestion.parser import parse_line
from m1a_fixture.models import Record
from m1a_fixture.validation.rules import validate_record


def import_records(lines: list[str]) -> list[Record]:
    """Parse and validate imported records before storage."""
    records = [parse_line(line) for line in lines]
    return [record for record in records if validate_record(record)]
