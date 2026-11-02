# paper-verify — 한국어 가이드

**문서 속 인용을 사실검증합니다.** 마크다운/텍스트 문서에서 출처(URL · DOI ·
PMC · PMID · arXiv)를 전부 추출하고, 각 원문을 가져와 **인용된 주장이 실제로
지지되는지** LLM 심판으로 판정한 뒤, 투명한 **100점 루브릭**으로 채점해
보고서를 만듭니다. AI가 지어낸 인용·오인용·죽은 링크를 발행 전에 잡는
도구입니다. (대학원생·연구자·강사·블로거용)

전문 검토를 대체하지 않습니다 — 사람이 직접 봐야 할 출처를 **작게 추려주는
triage 도구**입니다.

## 빠른 시작 (터미널)

설치 없이 바로 ([uv](https://docs.astral.sh/uv/) 필요):

```bash
uvx paper-verify 문서.md --level L2
```

설치해서 쓰기:

```bash
pip install paper-verify        # 또는 pipx install paper-verify
# 개발용
git clone https://github.com/nolainjin/paper-verify && cd paper-verify && pip install -e ".[dev]"
```

API 키 없이도 동작합니다(`keyword` 심판 — 저신뢰 스모크 테스트). 실전 검증은
LLM 심판을 붙이세요:

```bash
export ANTHROPIC_API_KEY=sk-...
paper-verify 문서.md --level L2 --judge anthropic:claude-sonnet-4-6
```

> PyPI 배포: [pypi.org/project/paper-verify](https://pypi.org/project/paper-verify/) (v0.1.0, 2026-06-11)

## 💬 터미널 없이 — 웹챗에서

1. **아무 웹챗** (Claude / ChatGPT / Gemini, 웹검색 가능 모델):
   [`docs/webchat/webchat-prompt.ko.md`](docs/webchat/webchat-prompt.ko.md)를
   통째로 복사해 붙여넣고, 검증할 문서를 이어서 붙여넣으면 끝. 모델이 출처를
   직접 열어보고 같은 100점 루브릭으로 채점합니다.
2. **claude.ai 스킬 업로드**: 릴리즈의
   `paper-verify-webchat-skill.zip`을 claude.ai 스킬로 업로드하면 추출·채점은
   **코드가 결정적으로** 수행하고(fetch·판정은 Claude 웹도구), 점수는 CLI와
   동일한 루브릭(`--from-evidence`)에서 나옵니다.

## 검증 레벨

| 레벨 | 깊이 | 비용 |
|---|---|---|
| **L1** | 링크 생존만 (HTTP 2xx) | 네트워크만, LLM 0 |
| **L2** | 초록 근거와 주장 대조 (**기본**), 제목만 있으면 판단 유보 | 선택한 심판별 인용당 1회 |
| **L3** | 수집 가능한 페이지 본문 우선, 없으면 메타데이터로 대조 | L2와 동일, 불일치 시 중재 심판 추가 |

L1 점수는 링크 접근성만 의미합니다. 연결 실패나 보관본만 있는 링크는 원래
주소가 살아 있다고 처리하지 않습니다. 오류 페이지로 의심되는 HTTP 200은 50점입니다.
L3도 전문 검증을 보장하지 않습니다. 추출문은 약 50K 문자, SDK/CLI 심판 입력은
앞 8,000 문자로 제한되며 PDF/OCR·자바스크립트 렌더링은 지원하지 않습니다.

## 심판 (`--judge`, 반복 지정 = 교차검증)

| 스펙 | 요구사항 |
|---|---|
| `keyword` (기본) | 없음 — 항상 동작, 단 어휘 겹침만 보는 저신뢰 |
| `anthropic[:모델]` | extras `[anthropic]` + `ANTHROPIC_API_KEY` |
| `openai[:모델]` | extras `[openai]` + `OPENAI_API_KEY` |
| `gemini[:모델]` | extras `[gemini]` + `GEMINI_API_KEY` |
| `cli:claude` `cli:codex` `cli:gemini` | 해당 CLI가 PATH에 설치 |

심판 2개 이상이 갈리면 `--tiebreak <스펙>`이 갈린 인용만 재판정합니다.
같은 심판을 반복해도 독립적인 교차검증으로 인정하지 않습니다. 판정 응답이
비었거나 해석할 수 없으면 `Uncertain`으로 유보하며 CLI 오류 출력을 근거로 쓰지 않습니다.

## 100점 루브릭

| Item | Points | Criterion |
|---|---|---|
| URL accessible | 20 | HTTP 2xx |
| Author / year match | 20 / 10 / 0 | 저자+연도 모두 일치 20 / 하나만 10 / 불일치 0 / 비교할 메타데이터 없음 = 중립 10 |
| Claim match | 50 | Match = 50 · Partial = 25 · Uncertain = 15 · Mismatch = 0 · Inaccessible = 10 |
| Cross-check agreement | 10 | 서로 다른 주 심판 2개 이상이 최종 실질 판정에 합의할 때만; 중재 단독·판단 유보의 일치는 제외 |

| Tier | Score | 의미 |
|---|---|---|
| 🟢 A | 90–100 | 루브릭 고득점 — 실제 근거와 검증 범위를 확인 후 인용 |
| 🟡 B | 70–89 | 강의/블로그 OK, 소수정 |
| 🟠 C | 50–69 | 재확인 필요 |
| 🔴 F | 0–49 | 검증 실패 — 접근 불가·근거 부족·불일치 원인을 확인 |

F 인용이 하나라도 있으면 문서 전체에 ⚠️ 경고가 붙습니다.
낮은 점수만으로 조작이나 허위 인용을 단정하지 않습니다. 인용 0건은 검증한
주장이 없다는 뜻이며, 문서의 품질을 평가한 결과가 아닙니다.

## 에이전트에서 쓰기

- `--json`: 전체 결과를 JSON으로 stdout에 (사람용 요약은 stderr).
- `--extract-only`: 추출만 (네트워크·LLM 0).
- `--from-evidence evidence.json`: 외부에서 fetch·판정한 증거를 표준 루브릭으로
  채점 — 웹챗 스킬의 엔진. 예시: [`examples/evidence-sample.json`](examples/evidence-sample.json).
- MCP 서버: extras `[mcp]` 설치 후 `claude mcp add paper-verify -- paper-verify-mcp`.

JSON 스키마 6은 `citations[].tiebreak_judgement`에 중재 판정과 이유를 보존합니다.
외부 evidence의 자료형, 중복 인용 ID, 인용과 수집 결과 ID 불일치를 검사하며
잘못된 입력은 해당 인용 위치를 알리는 오류로 반환합니다.

## 한계

- LLM 심판도 초록을 오독할 수 있습니다 — 중요 인용은 교차검증(`--judge` 2개 +
  `--tiebreak`) 후 직접 확인하세요.
- 페이월 본문이 필요한 주장은 메타데이터만으로 C 티어에 머물 수 있습니다(정상).
- soft-404 감지는 휴리스틱이고, JS 위주 페이지는 본문 추출이 빈약할 수 있습니다.
- 같은 출처의 반복 인용은 중복 제거 후 하나의 주변 문맥만 대조합니다. 같은
  출처를 여러 주장에 재사용한 경우에는 각각의 주장을 별도로 확인해야 합니다.

MIT © 2026 진두찬 — 상세는 영문 [README.md](README.md).


### 심리학 20편 실전 재검증 (v0.2.0)

Crossref 초록이 없으면 Europe PMC → Semantic Scholar 순서로 조회한다. **DOI와
정규화한 제목이 모두 일치할 때만** 채택한다. JSON schema **7**은 초록의
`abstract_source`·`abstract_url`과 중재 심판의 근거를 보존한다. 키워드 전용 결과는
`evidence_quality: strong`이 될 수 없으며, 제목만 확보하면 판단을 유보한다.

소스 체크아웃에서 다음 명령으로 공개 논문 20편을 재실행할 수 있다.

```bash
python tools/live_corpus_check.py --manifest examples/psychology-live20.json --out dist/live20 --live
python tools/live_corpus_check.py --manifest examples/psychology-live20.json --out dist/live20-replay --replay dist/live20
```

출력 폴더는 회차마다 새 경로를 쓴다. 검사는 서지 동일성·출력 파일·보수적 판정 계약을
확인하며, **의미 정확도나 논문 품질의 인증은 아니다.** 실질적 주장 검토는 원문을 읽은
근거를 `--from-evidence`로 가져오거나 인증된 LLM 심판의 사유를 검토한다.
PDF 텍스트 변환과 식별자가 없는 참고문헌 검토는 별도 과정이다.
[20편 실전 검증 보고서](docs/harness/2026-09-08_psychology-live20.md).
