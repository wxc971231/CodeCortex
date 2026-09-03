from m1a_fixture.app import run


def handle_import(payload: str) -> str:
    return run(payload.splitlines())
