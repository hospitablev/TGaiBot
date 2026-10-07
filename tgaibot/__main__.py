import argparse
import asyncio
import importlib.util
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from telethon import errors

from .config import ConfigError, Settings
from .media import image_data
from .provider import Provider, ProviderError
from .tts import FishTTS, TTSError
from .userbot import account_lock, login, run


async def doctor(settings, live=False, tts=False):
    checks = {
        "model": settings.model,
        "provider": settings.api_base,
        "telegram_api_configured": bool(settings.telegram_api_id and settings.telegram_api_hash),
        "telegram_session_exists": bool(settings.telegram_session)
        or (settings.data_dir / "telegram.session").exists(),
        "web_search": "tavily" if settings.tavily_key else "public_search",
        "maps": "Photon / OpenStreetMap",
        "pdf_excel_tools": True,
        "ffmpeg": bool(shutil.which(settings.ffmpeg)),
        "ffprobe": bool(shutil.which(settings.ffprobe)),
        "transcription_enabled": settings.transcription,
        "whisper_installed": importlib.util.find_spec("faster_whisper") is not None,
        "real_calls": "experimental_enabled" if settings.enable_calls else "experimental_disabled",
        "fish_key_configured": bool(settings.fish_key),
        "fish_model": settings.fish_model,
        "fish_voice_configured": bool(settings.fish_voice),
    }
    if live:
        provider = Provider(settings)
        try:
            models = await provider.models()
            checks["model_listed"] = settings.model in models
            if not checks["model_listed"]:
                raise ProviderError("Выбранная модель отсутствует в списке провайдера.")
            answer = await provider.answer(
                [{"role": "user", "content": "Reply with OK only."}], max_tokens=128
            )
            checks["text_test"] = answer.strip() == "OK"
            with tempfile.TemporaryDirectory(prefix="tgaibot-check-") as temp:
                path = Path(temp) / "check.png"
                im = Image.new("RGB", (320, 160), "white")
                draw = ImageDraw.Draw(im)
                draw.rectangle((20, 20, 140, 140), fill="red")
                draw.ellipse((180, 30, 280, 130), fill="blue")
                im.save(path)
                result = await provider.answer(
                    [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Describe the two shapes and their colors in English.",
                                },
                                {"type": "image_url", "image_url": {"url": image_data(path)}},
                            ],
                        }
                    ],
                    max_tokens=160,
                )
                lower = result.lower()
                checks["image_test"] = all(word in lower for word in ("red", "blue")) and (
                    "circle" in lower and ("square" in lower or "rectangle" in lower)
                )
            checks["model_response_id_verified"] = True
        finally:
            await provider.close()
    if tts:
        speech = FishTTS(settings)
        try:
            target = settings.data_dir.resolve() / "demo" / "fish-demo.mp3"
            target.parent.mkdir(parents=True, exist_ok=True)
            await speech.synthesize("Здравствуйте! Я ИИ-ассистент. Это проверка голоса.", target)
            checks["tts_test_file"] = str(target)
            checks["tts_audio_bytes"] = target.stat().st_size
        finally:
            await speech.close()
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if live and not (checks.get("text_test") and checks.get("image_test")):
        raise RuntimeError("Проверка ответа модели не пройдена.")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Telegram юзербот с Claude Sonnet 5.5")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Локальный интерактивный вход в обычный Telegram-аккаунт")
    sub.add_parser("run", help="Отвечать на входящие личные сообщения")
    sub.add_parser("export-session", help="Сохранить сессию для Railway в приватный файл")
    sub.add_parser("archive-view", help="Открыть локальный просмотр сохранённых диалогов")
    check = sub.add_parser("doctor", help="Проверить настройки без раскрытия секретов")
    check.add_argument(
        "--live", action="store_true", help="2 небольших платных запроса: текст и фото"
    )
    check.add_argument(
        "--tts", action="store_true", help="Реальный синтез короткой русской фразы Fish Audio"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # Never expose HTTP headers, payloads, session internals or Telegram phone numbers in logs.
    for name in ("httpx", "httpcore", "telethon"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.load(require_telegram=args.command not in {"doctor", "archive-view"})
        if args.command == "doctor":
            asyncio.run(doctor(settings, args.live, args.tts))
        elif args.command == "archive-view":
            import uvicorn

            from .archive import Archive
            from .archive_web import create_app

            archive = Archive(settings, recover_downloads=False)
            archive.close()
            print(f"Архив: http://127.0.0.1:{settings.archive_port}")
            uvicorn.run(
                create_app(settings),
                host="127.0.0.1",
                port=settings.archive_port,
                log_level="warning",
                access_log=False,
            )
        else:
            with account_lock(settings.data_dir):
                if args.command == "export-session":
                    from .deploy import export_session

                    export_session(settings)
                else:
                    asyncio.run(login(settings) if args.command == "login" else run(settings))
    except (ConfigError, ProviderError, TTSError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except errors.RPCError as exc:
        print(
            f"Ошибка Telegram ({type(exc).__name__}). Проверьте данные входа или повторите позже.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Остановлено.")
    except Exception as exc:
        print(
            f"Не удалось запустить ({type(exc).__name__}); проверьте подключение и настройки.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
