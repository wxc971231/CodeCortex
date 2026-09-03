from .reader import read_lines


def load_payload(payload: str) -> list[str]:
    return read_lines(payload)
