from chatspark.documents import ContentType
from chatspark.extraction.files import parse_bytes
from chatspark.plugins.contracts import ProviderSelection


def test_extensionless_html_uses_selected_extractor_and_cleanup():
    result = parse_bytes(
        b'<main><div class="noise">Discard</div><p>Keep guide</p></main>',
        name="download",
        mime_type="text/html; charset=utf-8",
        source_url="https://example.test/guide",
        html_config=ProviderSelection(provider="html", settings={"remove_selectors": [".noise"]}),
    )
    assert result.content_type == ContentType.HTML
    assert "Keep guide" in result.content.text
    assert "Discard" not in result.content.text
    assert "<main>" not in result.content.text


def test_cleanup_is_not_undone_when_all_text_is_removed():
    from chatspark.extraction.html import extract_html
    from chatspark.profiles.models import HtmlSettings

    result = extract_html(
        "<html><body><nav><p>Removed navigation fact ORCHID-42</p></nav></body></html>",
        "https://example.test",
        HtmlSettings(remove_selectors=["nav"]),
    )
    assert not result.text.strip()
    assert "ORCHID-42" not in result.markdown
    assert not any("ORCHID-42" in section.content for section in result.sections)
