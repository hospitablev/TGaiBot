import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader
from telethon import errors
from telethon.tl.types import InputMediaGeoPoint

from tgaibot.agent import Agent
from tgaibot.artifacts import make_excel, make_pdf, make_text


def call(name, args, identifier="1"):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def response(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


@pytest.fixture
def agent(settings):
    return Agent(
        settings,
        SimpleNamespace(step=AsyncMock()),
        SimpleNamespace(web=AsyncMock(), places=AsyncMock()),
        SimpleNamespace(send_file=AsyncMock()),
    )


async def test_file_sent_to_requester_once_even_when_model_repeats(agent):
    action = call("create_text_file", {"name": "../hello", "content": "Привет", "format": "txt"})
    agent.provider.step.side_effect = [response(action), response(action), {"content": "Готово"}]
    assert await agent.answer([], 45, 123) == "Готово"
    agent.client.send_file.assert_awaited_once()
    args = agent.client.send_file.await_args
    assert args.args[0] == 45 and args.kwargs["reply_to"] == 123
    assert len(list((agent.settings.data_dir / "attachments" / "45").glob("*.txt"))) == 1


async def test_unknown_tool_or_cross_chat_parameter_cannot_send(agent):
    agent.provider.step.side_effect = [
        response(
            call("shell", {"command": "anything"}),
            call(
                "create_text_file",
                {"name": "test", "content": "x", "format": "txt", "user_id": 99},
                "2",
            ),
        ),
        {"content": "Не получилось"},
    ]
    await agent.answer([], 45, 123)
    agent.client.send_file.assert_not_awaited()


async def test_memory_tool_scopes_search_to_requester(agent, history):
    from unittest.mock import Mock

    agent.memory = SimpleNamespace(search=Mock(return_value={"facts": []}), history=history)
    agent.provider.step.side_effect = [
        response(call("search_memory", {"query": "имя", "user_id": 99})),
        response(call("search_memory", {"query": "имя"}, "2")),
        {"content": "Сведений нет"},
    ]
    await agent.answer([], 45, 123)
    agent.memory.search.assert_called_once_with(45, "имя")
    agent.client.send_file.assert_not_awaited()


async def test_location_requires_search_and_uses_verified_coordinates(agent):
    place = {
        "name": "Алматы",
        "latitude": 43.2,
        "longitude": 76.9,
        "url": "https://www.openstreetmap.org/",
    }
    agent.search.places.return_value = {"places": [place]}
    agent.provider.step.side_effect = [
        response(call("send_location", {"place_id": "made-up"})),
        response(call("search_places", {"query": "Алматы"})),
        response(call("send_location", {"place_id": "place-1"})),
        {"content": "Отправлено"},
    ]
    await agent.answer([], 45, 123)
    agent.client.send_file.assert_awaited_once()
    geo = agent.client.send_file.await_args.args[1]
    assert isinstance(geo, InputMediaGeoPoint) and geo.geo_point.lat == 43.2
    assert "place_id" not in place


async def test_reset_before_tool_execution_prevents_send(agent):
    agent.provider.step.return_value = response(
        call("create_text_file", {"name": "x", "content": "y", "format": "txt"})
    )
    await agent.answer([], 45, 123, lambda: False)
    agent.client.send_file.assert_not_awaited()


async def test_successful_action_is_reported_if_next_model_request_fails(agent):
    agent.provider.step.side_effect = [
        response(call("create_text_file", {"name": "x", "content": "y", "format": "txt"})),
        RuntimeError(),
    ]
    result = await agent.answer([], 45, 123)
    assert "файл" in result and "Готово" in result


async def test_floodwait_propagates_without_retry(agent):
    agent.provider.step.return_value = response(
        call("create_text_file", {"name": "x", "content": "y", "format": "txt"})
    )
    agent.client.send_file.side_effect = errors.FloodWaitError(request=None, capture=10)
    with pytest.raises(errors.FloodWaitError):
        await agent.answer([], 45, 123)
    agent.client.send_file.assert_awaited_once()


async def test_tool_budget_is_bounded(agent):
    agent.provider.step.return_value = response(
        *[call("search_web", {"query": str(i)}, str(i)) for i in range(8)]
    )
    agent.search.web.return_value = {"results": []}
    await agent.answer([], 45, 123)
    assert agent.search.web.await_count == 8
    assert agent.provider.step.await_count == 1


def test_excel_preserves_numbers_and_neutralizes_formula_strings(tmp_path):
    path = make_excel(
        tmp_path,
        "Бюджет",
        [
            {
                "name": "Расходы",
                "headers": ["Статья", "Сумма"],
                "rows": [["Еда", 2500], ['=HYPERLINK("https://example.com")', 0]],
            }
        ],
    )
    book = load_workbook(path)
    sheet = book.active
    assert sheet["B2"].value == 2500
    assert sheet["A3"].data_type == "s"
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:B3"


def test_pdf_contains_readable_russian_and_escapes_markup(tmp_path):
    path = make_pdf(
        tmp_path, "Поездка", '# План поездки\n\nПривет, Алматы!\n\n<img src="file:///secret">'
    )
    text = "\n".join(p.extract_text() for p in PdfReader(path).pages)
    assert "План поездки" in text and "Привет, Алматы!" in text
    assert '<img src="file:///secret">' in text


@pytest.mark.parametrize(
    "sheets",
    [
        [],
        [{"name": "x", "headers": ["a"], "rows": [[1, 2]]}],
        [{"name": "x", "headers": ["a"], "rows": [[float("nan")]]}],
    ],
)
def test_invalid_spreadsheet_rejected(tmp_path, sheets):
    with pytest.raises(ValueError):
        make_excel(tmp_path, "x", sheets)


def test_artifact_path_stays_inside_directory(tmp_path):
    path = make_text(tmp_path, "../../secret", "hello")
    assert path.parent.resolve() == tmp_path.resolve()


async def test_uncertain_delivery_is_not_retried(agent):
    action = call("create_text_file", {"name": "x", "content": "y", "format": "txt"})
    agent.provider.step.side_effect = [
        response(action),
        response(action),
        {"content": "Отправку подтвердить не удалось"},
    ]
    agent.client.send_file.side_effect = ConnectionError()
    await agent.answer([], 45, 123)
    agent.client.send_file.assert_awaited_once()
