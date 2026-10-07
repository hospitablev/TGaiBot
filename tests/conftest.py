from dataclasses import replace

import pytest

from tgaibot.config import Settings
from tgaibot.storage import History


@pytest.fixture
def settings(tmp_path):
    return Settings(api_key="unit-test-placeholder", data_dir=tmp_path / "data")


@pytest.fixture
def history(settings):
    value = History(settings)
    yield value
    value.close()


@pytest.fixture
def change_settings(settings):
    return lambda **kwargs: replace(settings, **kwargs)
