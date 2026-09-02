import math

import pytest

from codecortex.infrastructure.jsonio import canonical_json_bytes, write_json_atomic


def test_canonical_json_is_stable_and_lf_terminated(tmp_path):
    """Removing key sorting, indentation, or the final newline changes formal state bytes."""
    path = tmp_path / "state.json"

    write_json_atomic(path, {"z": 1, "a": [2, 1]})

    assert path.read_bytes() == b'{\n  "a": [\n    2,\n    1\n  ],\n  "z": 1\n}\n'


def test_canonical_json_rejects_non_finite_float_values():
    """Allowing NaN would produce JSON that cannot be a portable formal-state contract."""
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": math.nan})
