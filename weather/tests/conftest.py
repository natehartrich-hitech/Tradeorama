import json
from pathlib import Path

import pytest

from wxbrackets.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str):
    return json.loads(fixture_text(name))


@pytest.fixture
def cfg(tmp_path):
    text = (ROOT / "config.example.yaml").read_text().replace("you@example.com", "tests@wxbrackets.invalid")
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("database: wxbrackets.db", f"database: {tmp_path / 'test.db'}"))
    return load_config(path)
