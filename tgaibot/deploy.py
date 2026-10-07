"""Export an existing local Telegram session without printing credentials."""

import os

from telethon.sessions import SQLiteSession, StringSession


def export_session(settings):
    path = settings.data_dir / "telegram.session"
    if not path.is_file():
        raise RuntimeError("Сначала войдите в Telegram: python -m tgaibot login")
    session = SQLiteSession(str(path))
    try:
        if not session.auth_key:
            raise RuntimeError("Сессия ещё не авторизована. Выполните login.")
        value = StringSession.save(session)
    finally:
        session.close()
    output = settings.data_dir / "railway-session.env"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("TELEGRAM_SESSION=" + value + "\n")
    print(
        "Сессия сохранена в data/railway-session.env. Перенесите TELEGRAM_SESSION в Variables Railway. Не публикуйте файл."
    )
