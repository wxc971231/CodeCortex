from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    identifier: str
    value: int
