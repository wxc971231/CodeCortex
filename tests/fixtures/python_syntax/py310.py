def classify(value: object) -> str:
    match value:
        case {"kind": kind}:
            return str(kind)
        case _:
            return "unknown"
