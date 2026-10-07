import httpx
import pytest

from tgaibot.metrics import Metrics, normalize_usage
from tgaibot.provider import Provider, ProviderError


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 20},
            },
            (100, 10, 40, 20),
        ),
        (
            {
                "input_tokens": 40,
                "output_tokens": 10,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 20,
            },
            (100, 10, 40, 20),
        ),
        ({"prompt_tokens": 100, "completion_tokens": 10}, (100, 10, None, None)),
        (
            {"prompt_tokens": -1, "completion_tokens": True, "prompt_tokens_details": "bad"},
            (None, None, None, None),
        ),
        (None, (None, None, None, None)),
    ],
)
def test_usage_formats_do_not_double_count_cache(usage, expected):
    assert normalize_usage(usage) == expected


def test_cost_persistence_and_missing_usage_are_explicit(tmp_path):
    metrics = Metrics(tmp_path)
    metrics.record(
        "model",
        model="claude-sonnet-5-5",
        usage={
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
            "prompt_tokens_details": {"cached_tokens": 400_000, "cache_write_tokens": 200_000},
        },
    )
    metrics.record("image", model="gpt-image-2.5-flare")
    # .144 input + .18 output + .0144 cache read + .09 write + .0012 image = .4296
    assert metrics.db.execute("SELECT sum(cost_nano) FROM events").fetchone()[0] == 429_600_000
    metrics.close()
    metrics = Metrics(tmp_path)
    assert "$0.42960000" in metrics.report(all_time=True)
    metrics.record(
        "model", model="claude-sonnet-5-5", usage={"prompt_tokens": 100, "completion_tokens": 10}
    )
    report = metrics.report(all_time=True)
    assert "данные 1/2 запросов" in report
    assert "Событий с неполной стоимостью: 1" in report
    assert "приняты за 0" in report
    metrics.close()


async def test_provider_counts_every_tool_and_summary_call_and_errors(settings):
    metrics = Metrics(settings.data_dir)
    responses = [
        httpx.Response(
            200,
            json={
                "model": settings.model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 25, "completion_tokens": 3},
            },
        ),
        httpx.Response(429),
    ]
    provider = Provider(
        settings,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: responses.pop(0))),
        metrics=metrics,
    )
    assert await provider.answer([], usage_kind="summary") == "ok"
    with pytest.raises(ProviderError):
        await provider.step([], [])
    rows = metrics.db.execute("SELECT kind,ok,input_tokens FROM events ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("summary", 1, 25), ("model", 0, None)]
    await provider.close()
    metrics.close()


async def test_web_statistics_requires_login_and_reports_prices(settings, history):
    from dataclasses import replace

    from tgaibot.archive_web import create_app

    settings = replace(settings, archive_password="private-test-password-123")
    metrics = Metrics(settings.data_dir)
    metrics.record("image")
    app = create_app(settings, metrics=metrics, history=history)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/metrics")).status_code == 401
        await client.post(
            "/api/login",
            json={"password": settings.archive_password},
            headers={"x-archive-request": "1"},
        )
        data = (await client.get("/api/metrics?all_time=true")).json()
        assert "$0.00120000" in data["report"]
        assert "Свежий контекст" in data["memory"]
    metrics.close()
