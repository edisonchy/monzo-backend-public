import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from monzo_backend.category_policy import classification_request

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/evaluate_categorisation.py"
evaluation = runpy.run_path(str(SCRIPT))


def sample():
    dataset = {
        "version": "test-v1",
        "cases": [
            {
                "id": "food",
                "merchant": "A Supermarket",
                "bank_category": "general",
                "expected_category": "groceries",
            },
            {
                "id": "unknown",
                "merchant": "XYZ",
                "bank_category": "general",
                "expected_category": None,
            },
            {
                "id": "dental",
                "merchant": "A Dental Clinic",
                "bank_category": "general",
                "expected_category": "health",
            },
        ],
    }
    requests = evaluation["export_requests"](dataset, "test-model")
    predictions = {
        "request_set_sha256": requests["request_set_sha256"],
        "predictions": [
            {"id": "food", "output": {"category": "groceries", "needs_review": False}},
            {"id": "unknown", "output": {"category": None, "needs_review": True}},
            {"id": "dental", "output": {"category": "health", "needs_review": True}},
        ],
    }
    return dataset, requests, predictions


def test_scoring_counts_suggestions_even_when_review_is_requested():
    dataset, requests, predictions = sample()
    result = evaluation["score"](dataset, requests, predictions)
    assert result["total"] == 3
    assert result["correct_suggestions"] == 2
    assert result["correct_abstentions"] == 1
    assert result["suggestion_coverage"] == 2 / 3
    assert result["suggestion_accuracy"] == result["expected_outcome_rate"] == 1
    assert result["wrong_suggestions"] == result["invalid_outputs"] == 0


def test_scoring_separates_wrong_answers_abstentions_and_invalid_outputs():
    dataset, requests, predictions = sample()
    predictions["predictions"][0]["output"] = {"category": None, "needs_review": True}
    predictions["predictions"][1]["output"] = {"category": "income", "needs_review": False}
    predictions["predictions"][2]["output"] = {"category": "health", "needs_review": "false"}
    result = evaluation["score"](dataset, requests, predictions)
    assert result["wrong_suggestions"] == result["false_confident_answers"] == 1
    assert result["abstentions"] == result["missed_suggestions"] == 1
    assert result["invalid_outputs"] == 1
    assert result["suggestion_coverage"] == 1 / 3
    assert result["suggestion_accuracy"] == result["expected_outcome_rate"] == 0


def test_wrong_category_without_review_is_a_false_confident_answer():
    dataset, requests, predictions = sample()
    predictions["predictions"][0]["output"] = {"category": "health", "needs_review": False}
    result = evaluation["score"](dataset, requests, predictions)
    assert result["wrong_suggestions"] == result["false_confident_answers"] == 1


@pytest.mark.parametrize(
    "output",
    [
        None,
        "not json",
        [],
        {"category": None, "needs_review": False},
        {"category": "fictional", "needs_review": True},
        {"category": "health", "needs_review": 1},
        {"category": "health", "needs_review": True, "extra": "x"},
    ],
)
def test_invalid_answers_remain_in_the_denominator(output):
    dataset, requests, predictions = sample()
    predictions["predictions"][2]["output"] = output
    result = evaluation["score"](dataset, requests, predictions)
    assert result["total"] == 3
    assert result["invalid_outputs"] == 1
    assert result["expected_outcome_rate"] == 2 / 3


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "unknown",
        "bad_id",
        "wrong_manifest",
        "changed_dataset",
        "tampered_request",
    ],
)
def test_incomplete_or_mismatched_runs_are_rejected(change):
    dataset, requests, predictions = sample()
    if change == "missing":
        predictions["predictions"].pop()
    elif change == "duplicate":
        predictions["predictions"].append(copy.deepcopy(predictions["predictions"][0]))
    elif change == "unknown":
        predictions["predictions"][0]["id"] = "another"
    elif change == "bad_id":
        predictions["predictions"][0]["id"] = 1
    elif change == "wrong_manifest":
        predictions["request_set_sha256"] = "another-run"
    elif change == "changed_dataset":
        dataset["cases"][0]["expected_category"] = "shopping"
    else:
        requests["cases"][0]["request"]["model"] = "other-model"
    with pytest.raises(ValueError):
        evaluation["score"](dataset, requests, predictions)


def test_no_suggestions_has_no_suggestion_accuracy():
    dataset, requests, predictions = sample()
    for answer in predictions["predictions"]:
        answer["output"] = '{"category":null,"needs_review":true}'
    result = evaluation["score"](dataset, requests, predictions)
    assert result["suggestion_accuracy"] is None
    assert result["suggestion_coverage"] == 0
    assert result["expected_outcome_rate"] == 1 / 3


def run_cli(*args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONZO_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], env=env, capture_output=True, text=True
    )


def test_cli_exports_production_requests_without_labels_or_database_config():
    exported = run_cli("export", "--model", "test-model")
    assert exported.returncode == 0, exported.stderr
    manifest = json.loads(exported.stdout)
    assert len(manifest["cases"]) == 30
    assert "expected_category" not in exported.stdout
    assert manifest["cases"][0]["request"] == classification_request(
        "Meadowbank Supermarket", "general", "test-model"
    )
    reference = next(case for case in manifest["cases"] if case["id"] == "reference")
    assert "123456789" not in json.dumps(reference)
    assert "[redacted]" in json.dumps(reference)


def test_cli_scores_and_rejects_partial_predictions(tmp_path):
    dataset, requests, predictions = sample()
    paths = [tmp_path / name for name in ("dataset.json", "requests.json", "predictions.json")]
    for path, value in zip(paths, (dataset, requests, predictions), strict=True):
        path.write_text(json.dumps(value))
    args = ["--dataset", paths[0], "score", "--requests", paths[1], "--predictions", paths[2]]
    result = run_cli(*args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["correct_suggestions"] == 2
    predictions["predictions"].pop()
    paths[2].write_text(json.dumps(predictions))
    rejected = run_cli(*args)
    assert rejected.returncode == 2
    assert "match every dataset case" in rejected.stderr
    assert not rejected.stdout
