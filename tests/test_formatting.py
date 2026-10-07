import pytest
from telethon.helpers import add_surrogate
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
)

from tgaibot.formatting import formatted_chunks, plain_fallback, render, spoken_markdown


def test_markdown_features():
    text, entities = render(
        "**Жирный** *курсив* [ссылка](https://example.com)\n\n- один\n- два\n\n```python\nprint('hi')\n```"
    )
    assert "• один" in text and "• два" in text
    assert all(
        any(isinstance(e, kind) for e in entities)
        for kind in (MessageEntityBold, MessageEntityItalic, MessageEntityPre, MessageEntityTextUrl)
    )
    assert any(isinstance(e, MessageEntityPre) and e.language == "python" for e in entities)


def test_long_code_unicode_entities_are_valid_and_preserved():
    source = "```python\n" + "print('🙂')\n" * 1200 + "```"
    expected, _ = render(source)
    chunks = list(formatted_chunks(source, prefix=""))
    assert "".join(t for t, _ in chunks) == expected
    assert len(chunks) > 2
    for text, entities in chunks:
        length = len(add_surrogate(text))
        assert length <= 3500
        assert any(isinstance(e, MessageEntityPre) for e in entities)
        assert all(0 <= e.offset < e.offset + e.length <= length for e in entities)
        text.encode("utf-8")


def test_html_input_is_literal_and_unsafe_links_not_entities():
    text, entities = render("<script>alert(1)</script> [bad](javascript:alert(1))")
    assert "<script>" in text
    assert not any(isinstance(e, MessageEntityTextUrl) for e in entities)


def entity_text(text, entity):
    from telethon.helpers import del_surrogate

    return del_surrogate(add_surrogate(text)[entity.offset : entity.offset + entity.length])


def test_image_markdown_preserves_caption_and_destination():
    text, entities = render("![Котик](https://example.com/cat.png)")
    assert text == "Котик"
    assert entities[0].url == "https://example.com/cat.png"
    assert list(formatted_chunks("![Котик](https://example.com/cat.png)"))


def test_linked_image_does_not_create_nested_links():
    text, entities = render("[![Кот](https://example.com/image)](https://example.com/page)")
    assert text == "Кот" and len(entities) == 1
    assert entities[0].url == "https://example.com/page"


def test_code_is_not_nested_in_other_entities():
    text, entities = render(
        "**до `код` после**\n\n> цитата\n> ```python\n> print(1)\n> ```\n> конец"
    )
    for code in (e for e in entities if isinstance(e, (MessageEntityCode, MessageEntityPre))):
        assert all(
            e is code or e.offset + e.length <= code.offset or e.offset >= code.offset + code.length
            for e in entities
        )
    assert "после" in text and "конец" in text
    assert any(
        isinstance(e, MessageEntityBold) and "после" in entity_text(text, e) for e in entities
    )


def test_nested_quote_keeps_tail_and_heading_styles():
    text, entities = render("# Заголовок\n\n> начало\n>\n> > внутри\n>\n> конец")
    quotes = [e for e in entities if isinstance(e, MessageEntityBlockquote)]
    assert len(quotes) == 1
    assert entity_text(text, quotes[0]).endswith("конец")
    assert any(
        isinstance(e, MessageEntityBold) and entity_text(text, e) == "Заголовок" for e in entities
    )


def test_table_cells_and_links_become_mobile_friendly_rows():
    text, entities = render(
        "| Товар | Цена |\n| --- | --- |\n| [Чай](https://example.com) | **100 ₽** |\n| Кофе | 200 ₽ |"
    )
    assert "Товар: Чай" in text and "Цена: 100 ₽" in text
    assert "Товар: Кофе" in text and "200 ₽" in text
    assert "|" not in text
    assert any(isinstance(e, MessageEntityTextUrl) for e in entities)


def test_task_list_and_numbered_nested_list():
    text, _ = render("3. Первый\n   - [ ] Купить\n   - [x] Сделано\n4. Второй")
    assert "3. Первый" in text and "4. Второй" in text
    assert "☐ Купить" in text and "☑ Сделано" in text


def test_dense_formatting_splits_without_dropping_entities():
    source = " ".join(f"**слово{i}**" for i in range(250))
    plain, _ = render(source)
    parts = list(formatted_chunks(source, prefix=""))
    assert len(parts) >= 3
    assert "".join(text for text, _ in parts) == plain
    assert sum(len(entities) for _, entities in parts) == 250
    assert all(len(entities) <= 90 for _, entities in parts)


def test_spoiler_is_hidden_also_in_audio_and_fallback():
    source = "Ответ: ||**секрет** и `пароль`||. ~~Старый вариант~~"
    text, entities = render(source)
    assert any(isinstance(e, MessageEntitySpoiler) for e in entities)
    assert any(isinstance(e, MessageEntityStrike) for e in entities)
    assert not any(isinstance(e, MessageEntityCode) for e in entities)
    for output in (spoken_markdown(source), plain_fallback(text, entities)):
        assert "секрет" not in output and "пароль" not in output
        assert "Скрытый фрагмент" in output


def test_escaped_markdown_and_literals_are_not_formatted():
    text, entities = render(r"\*не курсив\* и \||не спойлер||")
    assert "*не курсив*" in text and "||не спойлер||" in text
    assert not entities


@pytest.mark.parametrize("limit,prefix", [(1, ""), (20, "x" * 20), (4097, "")])
def test_invalid_chunk_limits_fail_instead_of_looping(limit, prefix):
    with pytest.raises(ValueError):
        list(formatted_chunks("🙂test", prefix=prefix, limit=limit))


def test_astral_symbols_at_every_chunk_boundary():
    plain, _ = render("**🙂🙂🙂🙂🙂🙂🙂🙂**")
    parts = list(formatted_chunks("**🙂🙂🙂🙂🙂🙂🙂🙂**", prefix="Я: ", limit=10))
    assert "".join(text[3:] for text, _ in parts) == plain
    for text, entities in parts:
        text.encode("utf-8")
        assert len(add_surrogate(text)) <= 10
        assert all(e.offset + e.length <= len(add_surrogate(text)) for e in entities)


def test_audio_handles_tilde_fences_and_keeps_words_after_block():
    text = spoken_markdown("Пример\n~~~python\nprivate_code()\n~~~\n**Продолжение**")
    assert "private_code" not in text
    assert "Продолжение" in text


def test_spoiler_delimiters_inside_inline_code_do_not_reveal_tail():
    source = "||hidden `password||42` tail|| end"
    text, entities = render(source)
    assert (
        entity_text(text, next(e for e in entities if isinstance(e, MessageEntitySpoiler)))
        == "hidden password||42 tail"
    )
    for output in (spoken_markdown(source), plain_fallback(text, entities)):
        assert "42" not in output and "tail" not in output
        assert output.endswith(" end")
