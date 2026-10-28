import importlib.util
import json
from pathlib import Path
import pytest

module_spec = importlib.util.spec_from_file_location("history_import", Path(__file__).parents[1] / "tools/import_history.py")
module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(module)

@pytest.mark.parametrize("payload", [[], {}, {"data": []}, {"data": {"A": {"dates": ["bad"], "last": [1]}}}])
def test_import_rejects_unusable_input(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        module.validate_cache(path)


def test_import_accepts_zero_and_preserves_source(tmp_path):
    path = tmp_path / "cache.json"
    raw = json.dumps({"data": {"A": {"dates": ["2026-10-23"], "last": [0]}}})
    path.write_text(raw)
    module.validate_cache(path)
    assert path.read_text() == raw
