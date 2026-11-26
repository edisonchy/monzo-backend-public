import json
import subprocess
import sys

from monzo_backend.category_policy import (
    CATEGORIES,
    CATEGORY_GUIDANCE,
    CLASSIFICATION_INSTRUCTIONS,
    RESEARCH_INSTRUCTIONS,
    classification_request,
)


def test_prompt_policy_defines_all_categories_for_both_provider_paths():
    for category in CATEGORIES:
        assert f"- {category}:" in CATEGORY_GUIDANCE
    assert CATEGORY_GUIDANCE in CLASSIFICATION_INSTRUCTIONS
    assert CATEGORY_GUIDANCE in RESEARCH_INSTRUCTIONS
    assert "Never use general as a substitute for uncertainty" in CATEGORY_GUIDANCE
    assert "purchased items or personal purpose" in CATEGORY_GUIDANCE


def test_shared_request_preserves_minimal_redacted_inputs():
    request = classification_request(
        "Market customer@example.test 123456 " + "x" * 200, "category_private", "test-model"
    )
    assert request["model"] == "test-model"
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0] == {"role": "system", "content": CLASSIFICATION_INSTRUCTIONS}
    assert json.loads(request["messages"][1]["content"]) == {
        "merchant": ("Market [redacted] [redacted] " + "x" * 200)[:160],
        "bank_category": "unknown",
    }
    assert (
        json.loads(classification_request("Example", "general", "m")["messages"][1]["content"])[
            "bank_category"
        ]
        == "unknown"
    )


def test_policy_import_does_not_load_database_or_application_settings():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import monzo_backend.category_policy; assert 'monzo_backend.db' not in sys.modules; assert 'monzo_backend.config' not in sys.modules",
        ],
        check=True,
    )
