from m1a_fixture.models import Record


def parse_line(line: str) -> Record:
    identifier, value = line.split(",", 1)
    return Record(identifier.strip(), int(value))
