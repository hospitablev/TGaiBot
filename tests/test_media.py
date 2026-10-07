import base64
import io
import shutil
import zipfile

import pytest
from PIL import Image
from pypdf import PdfWriter

from tgaibot.media import MediaError, document, image_data, prepare, run_tool
from tgaibot.worker import prepare_isolated


def test_excel_round_trip_reading(settings, tmp_path):
    from tgaibot.artifacts import make_excel
    from tgaibot.media import prepare

    path = make_excel(
        tmp_path,
        "Бюджет",
        [{"name": "Расходы", "headers": ["Статья", "Сумма"], "rows": [["Еда", 8000]]}],
    )
    result = prepare(path, settings)
    assert "Еда" in result.text and "8000" in result.text
    assert "формулы" in result.notes[0]


def test_image_resized_and_transparency(settings, tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGBA", (2400, 1200), (255, 0, 0, 0)).save(path)
    data = image_data(path)
    decoded = Image.open(io.BytesIO(base64.b64decode(data.split(",")[1])))
    assert decoded.size == (1280, 640)
    assert decoded.getpixel((10, 10)) == (255, 255, 255)


def test_text_truncation_and_bad_encoding(change_settings, tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("Привет мир" * 20, encoding="utf-8")
    result = document(path, change_settings(max_document_chars=10))
    assert len(result.text) == 10
    assert "обрезан" in result.notes[0]
    path.write_bytes(b"\xff\x00")
    with pytest.raises(MediaError, match="UTF-8"):
        document(path, change_settings())


def test_docx_and_xml_entities(settings, tmp_path):
    path = tmp_path / "doc.docx"
    with zipfile.ZipFile(path, "w") as doc:
        doc.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Привет</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Таблица</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>""",
        )
    assert document(path, settings).text == "Привет\nТаблица"
    with zipfile.ZipFile(path, "w") as doc:
        doc.writestr(
            "word/document.xml", '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///secret">]><a>&x;</a>'
        )
    with pytest.raises(MediaError):
        document(path, settings)


def test_scanned_and_encrypted_pdf(settings, tmp_path):
    path = tmp_path / "doc.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    with pytest.raises(MediaError, match="нет извлекаемого текста"):
        document(path, settings)
    writer.encrypt("test-password")
    writer.write(path)
    with pytest.raises(MediaError, match="Зашифрованный"):
        document(path, settings)


async def test_worker_isolation(settings, tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("Документ для анализа", encoding="utf-8")
    prepared = await prepare_isolated(path, settings)
    assert prepared.text == "Документ для анализа"
    assert settings.api_key not in (tmp_path / "request.json").read_text()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_actual_video_frames_and_duration(settings, change_settings, tmp_path):
    path = tmp_path / "video.mp4"
    run_tool(
        [
            settings.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    result = prepare(path, settings)
    assert len(result.images) == 1
    assert "1.0 с" in result.text
    assert "не содержит звуковой" in result.notes[-1]
    with pytest.raises(MediaError, match="длительность"):
        prepare(path, change_settings(max_video_seconds=1))


def test_size_limit(change_settings, tmp_path):
    path = tmp_path / "test.txt"
    path.write_text("Hello")
    with pytest.raises(MediaError, match="20 МБ"):
        prepare(path, change_settings(max_file_bytes=2))
