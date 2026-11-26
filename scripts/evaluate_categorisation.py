"""Export classification requests and score supplied answers without network or database access."""

import argparse
import hashlib
import json
from pathlib import Path

from monzo_backend.category_policy import (
    CATEGORIES,
    CLASSIFICATION_POLICY_VERSION,
    classification_request,
)

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/categorisation-evaluation.json"
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def keyed(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty list of cases")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("Every case needs a nonempty string id")
        if row["id"] in result:
            raise ValueError("Duplicate case id: " + row["id"])
        result[row["id"]] = row
    return result


def load_dataset(path):
    dataset = json.loads(path.read_text())
    if not isinstance(dataset, dict) or not isinstance(dataset.get("version"), str):
        raise ValueError("Dataset needs a version string")
    cases = keyed(dataset.get("cases"))
    for case in cases.values():
        if set(case) != {"id", "merchant", "bank_category", "expected_category"}:
            raise ValueError("Unexpected dataset fields")
        if not all(isinstance(case[k], str) for k in ("merchant", "bank_category")):
            raise ValueError("Merchant and bank category must be strings")
        if case["expected_category"] is not None and case["expected_category"] not in CATEGORIES:
            raise ValueError("Unknown expected category")
    return dataset


def export_requests(dataset, model):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Model must be a nonempty string")
    manifest = {
        "format_version": 1,
        "dataset_version": dataset["version"],
        "dataset_sha256": digest(dataset),
        "policy_version": CLASSIFICATION_POLICY_VERSION,
        "model": model,
        "cases": [
            {
                "id": case["id"],
                "request": classification_request(case["merchant"], case["bank_category"], model),
            }
            for case in dataset["cases"]
        ],
    }
    return {**manifest, "request_set_sha256": digest(manifest)}


def score(dataset, requests, predictions):
    cases = keyed(dataset["cases"])
    if not isinstance(requests, dict) or requests.get("format_version") != 1:
        raise ValueError("Unsupported request manifest")
    manifest = {k: v for k, v in requests.items() if k != "request_set_sha256"}
    if requests.get("request_set_sha256") != digest(manifest):
        raise ValueError("Request manifest checksum mismatch")
    if (
        requests.get("dataset_sha256") != digest(dataset)
        or requests.get("dataset_version") != dataset["version"]
    ):
        raise ValueError("Requests were exported for a different dataset")
    request_cases = keyed(requests.get("cases"))
    if request_cases.keys() != cases.keys():
        raise ValueError("Request case ids do not match the dataset")
    if (
        not isinstance(predictions, dict)
        or predictions.get("request_set_sha256") != requests["request_set_sha256"]
    ):
        raise ValueError("Predictions must reference this exact request manifest")
    answers = keyed(predictions.get("predictions"))
    if answers.keys() != cases.keys():
        raise ValueError("Prediction ids must match every dataset case exactly")
    counts = dict.fromkeys(
        [
            "correct_suggestions",
            "wrong_suggestions",
            "abstentions",
            "correct_abstentions",
            "missed_suggestions",
            "invalid_outputs",
            "false_confident_answers",
        ],
        0,
    )
    results = []
    for case_id, case in cases.items():
        answer = answers[case_id]
        output = answer.get("output")
        try:
            if set(answer) != {"id", "output"}:
                raise ValueError()
            if isinstance(output, str):
                output = json.loads(output)
            if not isinstance(output, dict) or set(output) != {"category", "needs_review"}:
                raise ValueError()
            if type(output["needs_review"]) is not bool:
                raise ValueError()
            category = output["category"]
            if category is not None and category not in CATEGORIES:
                raise ValueError()
            if category is None and output["needs_review"] is not True:
                raise ValueError()
        except (ValueError, TypeError):
            counts["invalid_outputs"] += 1
            results.append({"id": case_id, "outcome": "invalid_output"})
            continue
        expected = case["expected_category"]
        if category is None:
            counts["abstentions"] += 1
            outcome = "correct_abstention" if expected is None else "missed_suggestion"
            counts[outcome + "s"] += 1
        else:
            outcome = "correct_suggestion" if category == expected else "wrong_suggestion"
            counts[outcome + "s"] += 1
            if category != expected and not output["needs_review"]:
                counts["false_confident_answers"] += 1
        results.append({"id": case_id, "outcome": outcome})
    total = len(cases)
    suggested = counts["correct_suggestions"] + counts["wrong_suggestions"]
    return {
        **{
            k: requests[k]
            for k in (
                "dataset_version",
                "dataset_sha256",
                "policy_version",
                "model",
                "request_set_sha256",
            )
        },
        "total": total,
        **counts,
        "suggestion_coverage": suggested / total,
        "suggestion_accuracy": counts["correct_suggestions"] / suggested if suggested else None,
        "expected_outcome_rate": (counts["correct_suggestions"] + counts["correct_abstentions"])
        / total,
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export", help="Print exact production requests, without expected labels"
    )
    export.add_argument("--model", required=True)
    scoring = commands.add_parser("score", help="Score a complete set of supplied model answers")
    scoring.add_argument("--requests", type=Path, required=True)
    scoring.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    try:
        dataset = load_dataset(args.dataset)
        result = (
            export_requests(dataset, args.model)
            if args.command == "export"
            else score(
                dataset,
                json.loads(args.requests.read_text()),
                json.loads(args.predictions.read_text()),
            )
        )
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
