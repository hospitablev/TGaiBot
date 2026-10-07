import time

from tgaibot.storage import History


def test_isolation_persistence_and_reset(history, settings):
    history.add(10, 1, "Alice", "Hello Alice")
    history.add(20, 1, "Bob", "Hello Bob")
    assert history.messages(10)[0]["content"] == "Alice"
    assert history.messages(20)[0]["content"] == "Bob"
    second = History(settings)
    assert second.messages(10) == history.messages(10)
    second.close()
    history.reset(10)
    assert history.messages(10) == []
    assert history.messages(20)


def test_recent_budget_preserves_full_archive(change_settings):
    settings = change_settings(history_turns=2, history_chars=15)
    history = History(settings)
    for i in range(5):
        history.add(1, i, "12345", "67890")
    assert history.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 5
    assert len(history.messages(1)) == 2
    assert history.messages(1, "x" * 6) == []
    history.db.execute("UPDATE turns SET created=0")
    history.db.commit()
    assert len(history.messages(1)) == 2
    history.close()


def test_dedup_and_pause_survive_reset(history):
    history.mark_seen(1, 55)
    history.mark_seen(1, 55)
    history.pause(1)
    history.reset(1)
    assert history.seen(1, 55)
    assert history.is_paused(1)
    assert not history.seen(2, 55)
    history.pause(1, False)
    assert not history.is_paused(1)


def test_rate_limits(history):
    now = time.time()
    assert history.reserve(1, now)
    assert not history.reserve(1, now + 1)
    for i in range(1, 6):
        assert history.reserve(1, now + i * 11)
    assert not history.reserve(1, now + 100)
    assert history.reserve(2, now)


def test_image_budget(history):
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}
    history.add(1, 1, [image] * 12, "video")
    messages = history.messages(1, [image] * 5)
    assert messages and isinstance(messages[0]["content"], str)
    assert "Изображений: 12" in messages[0]["content"]
