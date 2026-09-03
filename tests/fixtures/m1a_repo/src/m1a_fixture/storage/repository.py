from m1a_fixture.models import Record


class RecordRepository:
    def __init__(self) -> None:
        self._records: list[Record] = []

    def save_all(self, records: list[Record]) -> None:
        self._records.extend(records)

    def all(self) -> list[Record]:
        return list(self._records)
