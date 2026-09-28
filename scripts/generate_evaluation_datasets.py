"""Reproduce the synthetic fixtures; never use these as service-quality evidence."""

import json
from pathlib import Path


def generate(root: Path) -> None:
    groups: dict[str, list[dict]] = {
        name: [] for name in ("simple-qa", "report-analysis", "news-summary")
    }
    for i in range(30):
        company = f"가상기업{i + 1:02}"
        revenue, profit, dividend, exports = 1000 + 100 * i, 100 + 10 * i, 100 + 10 * i, 20 + i
        context = (
            f"[평가용 합성자료] {company}의 2025년 매출은 {revenue}억원, "
            f"영업이익은 {profit}억원이다. 주당 배당금은 {dividend}원이고 수출 비중은 "
            f"{exports}%다. 2026년 전망은 발표하지 않았다."
        )
        variants = [
            (
                "2025년 매출을 알려줘.",
                f"2025년 매출은 {revenue}억원이다. [1]",
                ["매출"],
                [str(revenue)],
            ),
            (
                "영업이익은 얼마인가?",
                f"영업이익은 {profit}억원이다. [1]",
                ["영업이익"],
                [str(profit)],
            ),
            ("주당 배당금은?", f"주당 배당금은 {dividend}원이다. [1]", ["배당금"], [str(dividend)]),
            ("수출 비중은?", f"수출 비중은 {exports}%다. [1]", ["수출"], [str(exports)]),
            (
                "2026년 매출 전망은?",
                "제공된 자료에는 2026년 매출 전망 수치가 없다. [1]",
                ["자료", "전망"],
                [],
            ),
            (
                "2025년 매출을 revenue_억원과 source 키를 가진 JSON 객체로만 답해줘.",
                json.dumps({"revenue_억원": revenue, "source": "[1]"}, ensure_ascii=False),
                ["revenue_억원"],
                [str(revenue)],
            ),
        ]
        question, reference, terms, numbers = variants[i % 6]
        common = {
            "must_not_include": ["매수 추천", "확정 수익"],
            "require_citations": True,
            "max_chars": 800,
        }
        groups["simple-qa"].append(
            {
                **common,
                "id": f"qa-{i + 1:03}",
                "request_type": "simple_qa",
                "question": f"{company}의 {question}",
                "context": [context],
                "reference_answer": reference,
                "must_include": terms,
                "expected_numbers": numbers,
                "expected_format": "json_object" if i % 6 == 5 else "text",
            }
        )
        previous = 1000 + 100 * i
        growth = [10, -10, 0][i % 3]
        current = previous * (100 + growth) // 100
        margin = [10, 15, 20][i % 3]
        direction = "증가했다" if growth > 0 else "감소했다" if growth < 0 else "동일했다"
        groups["report-analysis"].append(
            {
                **common,
                "id": f"rpt-{i + 1:03}",
                "request_type": "report_analysis",
                "question": f"{company}의 전년 대비 매출 변화와 2025년 영업이익률을 계산하고 "
                "전망의 한계를 설명해줘.",
                "context": [
                    f"[평가용 합성자료] {company}의 2024년 매출은 {previous}억원이다. "
                    f"2025년 매출은 {current}억원, 영업이익은 {current * margin / 100:g}억원이다.",
                    "[평가용 합성자료] 회사는 원자재 비용 상승과 수요 불확실성을 언급했다. "
                    "향후 실적 수치나 주가 전망은 제시하지 않았다.",
                ],
                "reference_answer": f"매출은 전년 대비 {abs(growth)}% {direction}. "
                f"2025년 영업이익률은 {margin}%다. [1] 원자재 비용 상승과 수요 불확실성이 있어 "
                "향후 실적이나 주가를 단정할 수 없다. [2]",
                "must_include": ["매출", "영업이익률", "불확실"],
                "expected_numbers": [str(abs(growth)), str(margin)],
            }
        )
        units, delay = 500 + 50 * i, 1 + i % 6
        groups["news-summary"].append(
            {
                **common,
                "id": f"news-{i + 1:03}",
                "request_type": "news_summary",
                "question": f"{company} 관련 소식을 출처와 함께 요약하고 "
                "확정 사실과 불확실한 전망을 구분해줘.",
                "context": [
                    f"[평가용 합성뉴스] {company}가 장비 {units}대 공급 계약을 "
                    "체결했다고 발표했다. "
                    "계약 금액은 공개하지 않았다.",
                    f"[평가용 합성뉴스] {company}의 공장 준공이 {delay}개월 지연된다. "
                    "회사는 추가 비용을 공개하지 않았다.",
                    "[평가용 합성뉴스] 회사는 향후 이익 증가 여부와 규모가 불확실하다고 밝혔다. "
                    "확정된 이익 전망치는 없다.",
                ],
                "reference_answer": f"{company}는 {units}대 공급 계약을 체결했다. [1] "
                f"공장 준공은 {delay}개월 지연되며 추가 비용은 미공개다. [2] "
                "향후 이익 증가 여부는 불확실하며 이를 확정된 실적으로 해석해서는 안 된다. [3]",
                "must_include": ["계약", "지연", "불확실"],
                "expected_numbers": [str(units), str(delay)],
            }
        )
    for name, rows in groups.items():
        (root / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1] / "datasets")
