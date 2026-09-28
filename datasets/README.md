# 평가 데이터

`simple-qa.jsonl`, `report-analysis.jsonl`, `news-summary.jsonl`은 각 30건의 **합성 자료**다.
가상 기업과 임의 수치로 생성했으며 실제 금융 정보나 서비스 품질 증거가 아니다.
비슷한 템플릿의 변형이므로 모델 일반화 성능을 추정하는 용도로 쓰지 않는다.
`scripts/generate_evaluation_datasets.py`로 동일 파일을 재생성할 수 있다.
`benchmark-smoke.jsonl`은 Phase 7 성능 도구 전용으로 스키마가 다르다.

| 파일 | request_type | 확인할 동작 |
|---|---|---|
| simple-qa.jsonl | simple_qa | 숫자 추출, 정보 부족 인정, JSON 객체, 출처 |
| report-analysis.jsonl | report_analysis | 매출 변화·이익률 계산, 전망의 불확실성 |
| news-summary.jsonl | news_summary | 여러 출처 요약, 사실과 전망 구분 |

UTF-8 JSONL로 행마다 하나의 객체를 작성한다. 파일당 최대 1,000건, 20MB이며 ID는 파일 내에서
중복될 수 없다. 한 파일에는 한 request_type을 쓴다. ID와 설정의 dataset 이름은 영숫자로 시작하고
영숫자·`_`·`.`·`-`만 허용한다.

```json
{"id":"qa-001","request_type":"simple_qa","question":"매출은?","context":["평가용 합성자료: 가상기업 매출은 1200억원이다."],"reference_answer":"1200억원이다. [1]","must_include":["1200"],"must_not_include":[],"expected_numbers":["1200"],"min_chars":1,"max_chars":200,"expected_format":"text","require_citations":true}
```

`id`, `request_type`, `question`, `context`, `reference_answer`가 필수다. 나머지는 선택이며
`expected_format`은 `text` 또는 `json_object`다. `expected_numbers`는 부호 있는 소수 문자열이다.
규칙 채점은 유니코드·공백 정규화, 포함/금지어, 길이, 숫자 존재, JSON 객체, 출처 번호 범위를 검사한다.
숫자의 단위·역할이나 출처 내용의 정당성은 규칙 점수로 입증하지 않는다.
인용 번호 자체는 숫자 존재 검사에서 제외한다.

실제 서비스 평가로 전환할 때는 질의와 당시 검색된 context를 함께 추출하고 개인정보·비밀을 제거한다.
대표 질의와 사람이 확인한 기준 답변을 별도 파일에 작성한 뒤 평가 YAML에 `provenance: service`로 등록한다.
합성 파일의 provenance만 바꿔 실제 서비스 자료로 표시하지 않는다. 파일 내용의 SHA-256은 결과에 기록된다.

기준 답변과 규칙은 평가 대상 모델에 전송하지 않는다. Judge에는 기준 답변을 포함한 자료가 전달된다.
외부 모델을 judge로 설정한다면 해당 endpoint로 이 자료를 보내는 실행임을 고려한다.

사람 채점은 judge 점수를 먼저 보지 않고 질문·context·답변을 검토한다. 근거 충실성, 질문 충족도,
허위 내용의 부재, 필요한 인용의 정확성을 각각 0~1로 평가해 동일 가중 평균을 `human_score`에 기록한다.
인용이 요구되지 않고 답변에도 인용이 없다면 앞의 세 항목만 평균한다. `context_relevance`는 검색 품질이라
모델 종합 점수에서 제외한다. 낮은 점수와 높은 점수가 모두 포함된 대표 표본을 선택하고, 검증 결과를
좋게 만들기 위해 표본이나 점수를 고르지 않는다. 검토자가 실제로 채점한 값만 입력한다.
