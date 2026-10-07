import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    telegram_api_id: int = 0
    telegram_api_hash: str = field(default="", repr=False)
    api_base: str = "https://api.a6api.com/v1"
    model: str = "claude-sonnet-5-5"
    allowed_users: frozenset[int] = frozenset()
    data_dir: Path = Path("data")
    transcription: bool = False
    whisper_model: str = "base"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_file_bytes: int = 20_000_000
    max_video_seconds: int = 180
    max_frames: int = 12
    max_document_chars: int = 40_000
    max_pdf_pages: int = 100
    history_turns: int = 24
    history_bytes: int = 8_000_000
    history_chars: int = 100_000
    history_images: int = 16
    summary_threshold: int = 32
    summary_chars: int = 18_000
    fish_key: str = field(default="", repr=False)
    fish_model: str = "s2.1-pro"
    fish_voice: str = ""
    fish_speed: float = 1.0
    voice_replies: str = "auto"
    enable_calls: bool = False
    call_users: frozenset[int] = frozenset()
    telegram_session: str = field(default="", repr=False)
    tavily_key: str = field(default="", repr=False)
    archive_password: str = field(default="", repr=False)
    archive_port: int = 8080

    @classmethod
    def load(cls, *, require_telegram: bool = False):
        load_dotenv(encoding="utf-8-sig")
        key = os.getenv("MODEL_API_KEY", "").strip()
        telegram_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        try:
            telegram_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
        except ValueError:
            raise ConfigError("TELEGRAM_API_ID должен быть числом")
        base = os.getenv("MODEL_API_BASE", "https://api.a6api.com/v1").rstrip("/")
        url = urlparse(base)
        # This credential is authorized only for this provider; never follow redirects.
        if (
            url.scheme != "https"
            or url.netloc != "api.a6api.com"
            or url.path != "/v1"
            or url.query
            or url.fragment
        ):
            raise ConfigError("MODEL_API_BASE должен быть https://api.a6api.com/v1")
        if not key:
            raise ConfigError("Заполните MODEL_API_KEY в .env")
        if require_telegram and (telegram_id <= 0 or not telegram_hash):
            raise ConfigError(
                "Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env; "
                "затем выполните python -m tgaibot login"
            )
        if telegram_hash and (
            len(telegram_hash) != 32
            or any(c not in "0123456789abcdefABCDEF" for c in telegram_hash)
        ):
            raise ConfigError("TELEGRAM_API_HASH должен содержать 32 шестнадцатеричных символа")
        try:
            users = frozenset(
                int(v.strip()) for v in os.getenv("ALLOWED_USER_IDS", "").split(",") if v.strip()
            )
            if any(v <= 0 for v in users):
                raise ValueError
        except ValueError:
            raise ConfigError("ALLOWED_USER_IDS: нужны положительные числовые ID через запятую")
        audio = os.getenv("ENABLE_TRANSCRIPTION", "false").lower()
        if audio not in {"true", "false"}:
            raise ConfigError("ENABLE_TRANSCRIPTION: допустимы true или false")
        model = os.getenv("MODEL_NAME", "claude-sonnet-5-5").strip()
        if not model:
            raise ConfigError("MODEL_NAME не должен быть пустым")
        fish_model = os.getenv("FISH_MODEL", "s2.1-pro")
        if fish_model not in {"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3-preview"}:
            raise ConfigError("FISH_MODEL: неизвестная модель; автоматическая подмена запрещена")
        voice_replies = os.getenv("VOICE_REPLIES", "auto")
        if voice_replies not in {"auto", "always", "off"}:
            raise ConfigError("VOICE_REPLIES: auto, always или off")
        try:
            speed = float(os.getenv("FISH_SPEED", "1"))
            if not 0.5 <= speed <= 2:
                raise ValueError
            call_users = frozenset(
                int(v.strip())
                for v in os.getenv("CALL_ALLOWED_USER_IDS", "").split(",")
                if v.strip()
            )
            if any(v <= 0 for v in call_users):
                raise ValueError
        except ValueError:
            raise ConfigError("Проверьте FISH_SPEED (0.5–2) и числовые CALL_ALLOWED_USER_IDS")
        calls = os.getenv("ENABLE_CALLS", "false").lower()
        if calls not in {"true", "false"}:
            raise ConfigError("ENABLE_CALLS: true или false")
        archive_password = os.getenv("ARCHIVE_PASSWORD", "")
        if archive_password and len(archive_password) < 16:
            raise ConfigError("ARCHIVE_PASSWORD: задайте пароль не короче 16 символов")
        try:
            archive_port = int(os.getenv("PORT", os.getenv("ARCHIVE_PORT", "8080")))
            if not 1 <= archive_port <= 65535:
                raise ValueError
        except ValueError:
            raise ConfigError("PORT / ARCHIVE_PORT должен быть числом 1–65535")
        try:
            recent_turns = int(os.getenv("MEMORY_RECENT_TURNS", "24"))
            threshold = int(os.getenv("MEMORY_SUMMARY_THRESHOLD", "32"))
            context_chars = int(os.getenv("MEMORY_CONTEXT_CHARS", "100000"))
            summary_chars = int(os.getenv("MEMORY_SUMMARY_CHARS", "18000"))
            if not (
                4 <= recent_turns <= 100
                and recent_turns < threshold <= 200
                and 50000 <= context_chars <= 300000
                and 4000 <= summary_chars <= 30000
            ):
                raise ValueError
        except ValueError:
            raise ConfigError(
                "Проверьте MEMORY_*: свежих пар 4–100, порог выше свежих пар и ≤200, "
                "контекст 50000–300000 символов, резюме 4000–30000"
            )
        return cls(
            api_key=key,
            telegram_session=os.getenv("TELEGRAM_SESSION", "").strip(),
            tavily_key=os.getenv("TAVILY_API_KEY", "").strip(),
            archive_password=archive_password,
            archive_port=archive_port,
            telegram_api_id=telegram_id,
            telegram_api_hash=telegram_hash,
            api_base=base,
            model=model,
            allowed_users=users,
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            transcription=audio == "true",
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            ffmpeg=os.getenv("FFMPEG_BIN", "ffmpeg"),
            ffprobe=os.getenv("FFPROBE_BIN", "ffprobe"),
            fish_key=os.getenv("FISH_API_KEY", "").strip(),
            fish_model=fish_model,
            fish_voice=os.getenv("FISH_REFERENCE_ID", "").strip(),
            fish_speed=speed,
            voice_replies=voice_replies,
            enable_calls=calls == "true",
            call_users=call_users,
            history_turns=recent_turns,
            summary_threshold=threshold,
            history_chars=context_chars,
            summary_chars=summary_chars,
        )
