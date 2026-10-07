"""Plain-language controls; technical commands remain optional aliases."""

import re


def control_intent(text):
    normalized = text.strip().lower().strip(".!? ")
    normalized = re.sub(r"^пожалуйста[, ]+", "", normalized)
    normalized = re.sub(r"[, ]+пожалуйста$", "", normalized)
    aliases = {
        "/ai_help": {
            "что ты умеешь",
            "как тобой пользоваться",
            "как пользоваться",
            "покажи помощь",
        },
        "/ai_voice_on": {
            "отвечай голосом",
            "давай голосом",
            "говори голосом",
            "хочу голосовые ответы",
            "включи голос",
        },
        "/ai_voice_off": {
            "пиши текстом",
            "отвечай текстом",
            "без голоса",
            "выключи голос",
            "давай текстом",
        },
        "/ai_stop": {"не отвечай мне", "хватит отвечать", "останови автоответы"},
        "/ai_start": {"давай продолжим", "продолжай отвечать", "снова отвечай мне"},
        "/ai_reset": {
            "забудь нашу переписку",
            "очисти свою память обо мне",
            "удали свою память обо мне",
        },
    }
    return next(
        (command for command, phrases in aliases.items() if normalized in phrases), normalized
    )


def simple_error(error):
    from .media import MediaError
    from .provider import ProviderError
    from .tts import TTSError

    if isinstance(error, TTSError):
        return "Сейчас не получилось озвучить ответ, поэтому оставил его текстом. Попробуем голос чуть позже."
    if isinstance(error, ProviderError):
        return "Сейчас ИИ-сервис не смог ответить. Попробуй ещё раз чуть позже."
    if isinstance(error, MediaError):
        message = str(error)
        if any(
            word in message for word in ("FFmpeg", "ffprobe", "ENABLE_", "pip install", "Whisper")
        ):
            return "Сейчас не получается разобрать звук или видео. Можно пока прислать текст или несколько кадров — я помогу."
        return message
    return "Что-то не получилось. Попробуй отправить сообщение ещё раз."
