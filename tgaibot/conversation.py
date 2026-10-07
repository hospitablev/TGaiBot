"""Plain-language controls; technical commands remain optional aliases."""

import re


def background_request(text):
    match = re.match(
        r"^(?:пожалуйста[, ]+)?(?:в фоне|в фоновом режиме|сделай в фоне|выполни в фоне|фоновая задача)\s*[:—,-]?\s+(.+)$",
        text.strip(),
        re.I | re.S,
    )
    return match[1].strip() if match else None


def task_control(text):
    value = text.lower().strip(" .!?")
    if value in {
        "мои задачи",
        "список задач",
        "какие задачи выполняются",
        "что сделано",
        "что уже сделано",
        "как продвигается",
        "покажи задачи",
        "статус задач",
        "/tasks",
    }:
        return "list", None
    match = re.fullmatch(
        r"(?:что с задачей|статус задачи|покажи задачу|задача|/task)\s*#?\s*(\d+)", value
    )
    if match:
        return "get", int(match[1])
    match = re.fullmatch(r"(?:отмени|останови)\s+задачу(?:\s*#?\s*(\d+))?", value)
    if match:
        return "cancel", int(match[1]) if match[1] else None
    return None


def control_intent(text):
    normalized = text.strip().lower().strip(".!? ")
    normalized = re.sub(r"^пожалуйста[, ]+", "", normalized)
    normalized = re.sub(r"[, ]+пожалуйста$", "", normalized)
    # Only a complete short format command; quoted text and new writing tasks stay with the agent.
    if re.fullmatch(r"пиши(?: текстом)?(?:[, ]+(?:блин|бля|блядь|еблан|дурак|идиот))?", normalized):
        return "/ai_voice_off"
    aliases = {
        "/memory_retry": {"обнови память", "повтори разбор памяти"},
        "/my_memory": {
            "что ты помнишь обо мне",
            "покажи память",
            "покажи свою память обо мне",
            "моя память",
        },
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
