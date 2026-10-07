import pytest

from tgaibot.config import ConfigError, Settings


@pytest.mark.parametrize(
    "url",
    [
        "http://api.a6api.com/v1",
        "https://other.example/v1",
        "https://api.a6api.com.evil.example/v1",
        "https://api.a6api.com/v1?x=1",
    ],
)
def test_credentials_cannot_be_sent_elsewhere(monkeypatch, url):
    monkeypatch.setenv("MODEL_API_BASE", url)
    monkeypatch.setenv("MODEL_API_KEY", "fake-test-key")
    with pytest.raises(ConfigError):
        Settings.load()


def test_secrets_hidden_in_repr(settings):
    assert settings.api_key not in repr(settings)
