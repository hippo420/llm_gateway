import json
import re
import unicodedata
from decimal import Decimal

from ..models import Case

VERSION = "rules-v1"


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def score(case: Case, answer: str) -> tuple[float, dict[str, bool]]:
    text = normalize(answer)
    checks = {"nonempty": bool(text), "min_chars": len(answer.strip()) >= case.min_chars}
    if case.max_chars is not None:
        checks["max_chars"] = len(answer.strip()) <= case.max_chars
    for index, term in enumerate(case.must_include):
        checks[f"include_{index}"] = normalize(term) in text
    for index, term in enumerate(case.must_not_include):
        checks[f"exclude_{index}"] = normalize(term) not in text
    numeric_text = re.sub(r"\[\d+\]", "", text)
    numeric_text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", numeric_text)
    numbers = {
        Decimal(value)
        for value in re.findall(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?!\d|\.\d)", numeric_text)
    }
    for index, number in enumerate(case.expected_numbers):
        checks[f"number_{index}"] = Decimal(number) in numbers
    if case.expected_format == "json_object":
        try:
            checks["json_object"] = isinstance(json.loads(answer), dict)
        except ValueError:
            checks["json_object"] = False
    if case.require_citations:
        references = [int(value) for value in re.findall(r"\[(\d+)\]", answer)]
        checks["citation_indices"] = bool(references) and all(
            1 <= value <= len(case.context) for value in references
        )
    return sum(checks.values()) / len(checks), checks
