"""AI provider preflight tests."""

import pytest

from scripts.check_ai_provider import _error_message, build_chat_url


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://openrouter.ai/api", "https://openrouter.ai/api/v1/chat/completions"),
        ("https://example.test/api/v4", "https://example.test/api/v4/chat/completions"),
    ],
)
def test_build_chat_url_supports_common_openai_compatible_base_urls(base_url, expected):
    assert build_chat_url(base_url) == expected


def test_error_message_extracts_gateway_model_error():
    message = _error_message(
        '{"error":{"message":"model is not supported","type":"model_not_found"}}'
    )

    assert message == "model is not supported"
