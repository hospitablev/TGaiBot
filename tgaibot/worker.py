"""Run parsers separately so a bad file cannot block the Telegram event loop."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .media import MediaError, Prepared, prepare


async def prepare_isolated(path, settings):
    request = path.parent / "request.json"
    output = path.parent / "result.json"
    # Do not pass API keys or Telegram tokens to parser input files.
    request.write_text(
        json.dumps(
            {
                "path": str(path),
                "transcription": settings.transcription,
                "whisper_model": settings.whisper_model,
                "ffmpeg": settings.ffmpeg,
                "ffprobe": settings.ffprobe,
            }
        ),
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tgaibot.worker",
        str(request),
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        start_new_session=sys.platform != "win32",
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=300 if settings.transcription else 120)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await killer.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise MediaError("Время обработки вложения истекло. Отправьте меньший файл или фрагмент.")
    if process.returncode or not output.exists():
        raise MediaError("Не удалось обработать вложение. Попробуйте другой формат.")
    result = json.loads(output.read_text(encoding="utf-8"))
    if "error" in result:
        raise MediaError(result["error"])
    return Prepared(**result)


def main():
    request, output = map(Path, sys.argv[1:3])
    data = json.loads(request.read_text(encoding="utf-8"))
    path = Path(data.pop("path"))
    try:
        result = asdict(prepare(path, Settings(api_key="unused", **data)))
    except MediaError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "Вложение повреждено или не поддерживается."}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
