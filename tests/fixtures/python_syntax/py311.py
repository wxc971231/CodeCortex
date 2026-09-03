def grouped() -> str:
    try:
        raise ExceptionGroup("errors", [ValueError("bad")])
    except* ValueError:
        return "value"
