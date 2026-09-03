def read_lines(payload: str) -> list[str]:
    return [line for line in payload.splitlines() if line.strip()]
