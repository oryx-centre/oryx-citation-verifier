# paper-verify 전반 점검 및 개선 — 2026-09-08

> 첫 로컬 점검 시점의 기록이다. 이후 원격 변경 통합·심리학 20편 실전 검증·추가 수정이 이어졌다.
> 최종 버전은 0.2.0, JSON schema는 7이다. 최신 검증은 [20편 실전 보고서](2026-09-08_psychology-live20.md)를 따른다.

기준 커밋 `7bb7f43`의 로컬 작업본을 점검하여 확인된 개선 항목 6묶음을 적용했다.
전체 테스트는 **358 → 456개**, 기능 게이트는 **19개 검증 통과 / DRIFT 0 / LIE 0 / open 0**이다.
기존 사용자의 README 변경은 보존했다. 커밋·푸시·PyPI 발행은 수행하지 않았다.

## 우선순위와 적용 결과

| 우선순위 | 항목 | 이전 문제와 변경 결과 | 회귀 검증 |
|---|---|---|---|
| P0 | R1 판정과 합의 | 해석 불가 응답의 기본 Partial을 Uncertain으로 변경. CLI 비정상 종료와 stderr를 판정 근거에서 제외. 같은 심판 반복과 공동 판단 유보는 교차검증 점수를 받지 않음. 중재 판정을 JSON에 보존 | `tests/test_review_judging.py` |
| P0 | R2 수집과 출처 식별 | URL 문자열에 삽입된 다른 사이트의 논문 ID로 메타데이터를 빌리지 않음. arXiv 버전을 유지. L1 연결 실패·오류 페이지·보관본을 구분. PDF 바이너리는 텍스트 근거로 사용하지 않음. 제목만 있으면 판단 유보 | `tests/test_review_fetching.py` |
| P0 | R3 외부 evidence | 숫자·문자열·불리언·배열 자료형을 검사. 중복 인용 ID와 fetch ID 불일치를 거부. 잘못된 입력은 인용 위치를 명시하는 EvidenceError로 반환. 중재 판정도 가져오기 가능 | `tests/test_review_evidence.py` |
| P1 | R4 CLI·MCP·게이트 | 파일/출력/인코딩 오류는 CLI exit 2. 추출 전용과 L1은 심판 설치 탐색 생략. worker 1–32, MCP 텍스트·파일 크기 제한. 없는 기능 ID, 잘못된 상태·의존성·순환·동시 active를 게이트에서 검출 | `tests/test_review_operations.py` |
| P1 | R5 추출·보고 | URL·DOI의 균형 잡힌 괄호와 대소문자를 구분해야 하는 URL 경로 보존. Markdown에 개별 심판과 중재 근거 표시. 접근 불가를 곧바로 조작으로 단정하지 않음. 인용 0건·L1의 해석 범위를 표시 | `tests/test_review_reporting.py` |
| P1 | R6 실제 입력에서 발견한 문맥 오류 | URL/DOI 문자열이 단어·연도 점수에 섞이지 않도록 제거. 주변 문맥을 같은 문단으로 제한. 저자·출판연도 메타데이터를 심판 입력에 제공. LeCun 같은 내부 대문자 성 처리. arXiv 재시도도 3초 간격 적용 | `tests/test_review_context.py` |

부가적으로 영문·한국어 README와 웹챗 안내의 점수 해석을 맞췄다. L3를 전문 검증으로
오해하지 않도록 실제 수집·심판 입력 길이 제한을 명시했다. release workflow는 발행 전에
전체 테스트·기능 게이트·웹챗 ZIP 검사를 수행하도록 수정했다. 소스 배포본에 누락되어 있던
게이트 도구·스키마·기능 목록·한국어 README도 포함한다.

## Astra·ultra 설정 확인

[프로젝트 설정](../../.codex/config.toml)에 다음 값을 적용했다.

```toml
model = "gpt-6-astra"
model_reasoning_effort = "ultra"
plan_mode_reasoning_effort = "ultra"
```

설치된 Codex 0.153.4의 모델 카탈로그에서 Astra의 ultra 지원을 확인했다.
최초 `config/read` 조회에서는 프로젝트 신뢰 등록이 없어 설정이 무시되고 있었다.
사용자 config에 이 저장소의 신뢰 항목만 추가하고 다른 설정 값이 같음을 확인한 뒤,
Codex app-server `config/read`를 다시 호출하여 **세 값 모두 project 계층에서 로드됨**을 확인했다.
다른 저장소의 전역 medium/xhigh 기본값은 변경하지 않았다.

이 검증은 프로젝트의 유효 설정을 확인한 것이다. 진행 중인 이 대화의 실제 추론 모드를
전환했다는 실행 증거는 아니다. 기존 대화의 UI/세션 오버라이드는 별도로 확인해야 하며,
이 저장소를 루트로 새 Codex 대화를 시작하면 프로젝트 설정을 읽을 수 있다.
[실행 안내](../providers/codex.md)를 함께 참고한다.

## 검증 증거

- Python **3.13.2**, `.venv/bin/python -m pytest -q`: **456 passed**.
- `.venv/bin/python tools/feature_gate.py`: **19 verified**, drift/lie/open 모두 0.
- `git diff --check`: exit 0.
- 최종 소스로 wheel과 sdist 빌드 성공. 별도 가상환경에 `--no-index --no-deps`로 설치한
  console CLI에서 sample evidence 처리, JSON schema **6**, 인용 2건 출력 확인.
- wheel 내부의 모든 `paperverify/*.py` 및 하위 패키지 파일을 현재 작업본과 바이트 단위로 대조.
- sdist의 feature gate, 스키마, 기능 목록, 한국어 문서, 신규 회귀 테스트 존재 확인.
- 웹챗 ZIP 생성·검사 후 압축을 푼 독립 경로에서 offline CLI 실행 성공.
- release workflow의 GitHub Actions 실실행은 수행하지 않았다. 동일한 로컬 테스트·게이트·빌드
  단계는 통과했으며 Python 3.10은 이번 로컬 환경에서 별도로 실행하지 않았다.

## 실제 네트워크 스모크

공개 논문 두 건에 L2/keyword/worker 2로 실제 CLI를 실행했다. API 키를 사용하는 LLM 호출은 없었다.

| 인용 | 메타데이터 경로 | API 결과 / 원문 주소 상태 | 수정 후 keyword |
|---|---|---|---|
| `https://arxiv.org/abs/1706.03762` | arxiv | 200 / 200 | Match, low confidence |
| `https://doi.org/10.1038/nature14539` | crossref | 200 / 200 | Match, low confidence |

처음 실행에서는 URL·인접 문단이 섞여 둘 다 Mismatch였다. 문맥 수정 후 실제 재수집 실행은
각각 90점·80점이었다. 이후 LeCun 성 파싱을 수정한 최종 채점 코드로 같은 수집 근거를
오프라인 재채점하여 각각 **90점·90점**을 확인했다. 이 점수는 휴리스틱 결과이며,
LLM이나 사람이 논문의 의미를 검증했다는 증거로 해석해서는 안 된다.

## 산출물과 남은 제약

- 빌드·검증 파일: `dist/review-2026-09-08/` (Git 제외 로컬 산출물).
- 기계 판독용 결과: [verification-final.json](../../dist/review-2026-09-08/verification-final.json).
- 원시 테스트·게이트·빌드 로그도 위 디렉터리에 보존했다.
- JSON schema는 **5 → 6**으로 변경되었다. 엄격한 소비자는 `tiebreak_judgement` 필드를 반영해야 한다.
- SDK/CLI 심판의 정상·실패 분기는 모의 객체로 검증했다. 각 공급자의 실제 인증·과금·모델 응답 품질은 미검증이다.
- PDF/OCR, JS 렌더링, 8,000자 이후의 SDK/CLI 심판 입력, 동일 출처의 모든 반복 주장 검증은 지원 범위 밖이다.
- 기존 URL 방어의 DNS 검사와 실제 연결 사이 주소 변경 가능성은 남아 있다. 이번 작업은 완전한 공유 서비스 보안 인증을 의미하지 않는다.

완료 범위는 위 6묶음에서 확인·수정·검증한 항목이다. 게이트의 open 0은 등록된 기능의 상태이며,
알려지지 않은 결함이나 모든 실제 논문·공급자 조합까지 없음을 보증하지 않는다.
