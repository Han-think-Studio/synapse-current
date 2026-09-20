[English](RESULTS.md) · **한국어** · [中文](RESULTS.zh.md)

# 라이브 실행 결과

아이디어 열 개, 로컬 모델 하나, 기계 한 대, 하루. 각 행은 아래 세 단계를 순서대로 돌리고
각 단계가 무엇을 돌려줬는지 기록한 것이다.

- 날짜: 2026-09-15
- 모델: `nvidia-nemotron-3.5-lightning-30b-a3b`
- 엔드포인트: OpenAI 호환 로컬 서버
- 실행 10회, 파일 125개, 모델이 쓴 글 119 KB
- **READY 10건, BLOCKED 0건**
- 검사기가 거부한 답변 39건, 그중 다음 시도에서 회복 39건

## plan → fill → check

| 아이디어 | 프리셋 | 계획 | 작성 | 판정 | 통과 | 모델 출력 |
|---|---|---:|---:|---|---:|---:|
| `local_docs_search` | software | 12 | 12 | READY | 12/12 | 12.6 KB |
| `codebase_assistant` | software | 12 | 12 | READY | 12/12 | 11.1 KB |
| `youtube_digest` | automation | 13 | 13 | READY | 13/13 | 16.3 KB |
| `image_translation` | automation | 13 | 13 | READY | 13/13 | 13.2 KB |
| `meeting_notes` | automation | 13 | 13 | READY | 13/13 | 12.4 KB |
| `webnovel_translation` | content | 13 | 13 | READY | 13/13 | 10.4 KB |
| `persona_chatbot` | content | 13 | 13 | READY | 13/13 | 12.7 KB |
| `local_model_bench` | research | 13 | 13 | READY | 13/13 | 11.4 KB |
| `prompt_ab_test` | research | 13 | 13 | READY | 13/13 | 11.8 KB |
| `workflow_audit` | general | 10 | 10 | READY | 10/10 | 6.8 KB |

`계획`은 모델이 끼기 전에 Python이 정한 것이다. `작성`은 쓸 수 있는 상태로 돌아온 파일 수다.
`모델 출력`은 모델이 쓴 바이트만 센다 — `00_intake/idea.md`는 그대로 복사하고 매니페스트는
계획에서 렌더링하므로 둘 다 빠져 있다.

## 구조가 잡아낸 것

**성공률 한 줄로 덮이는 쪽이 이 절이다.** 아래 거부는 전부 `fill_with_local_model.py`의
검사기가 산출한 것이고, 모델이 스스로에 대해 한 말은 한 건도 들어가지 않는다.
다음 시도로 넘어가는 이유도 그 검사기가 쓰며, **매번 같은 문구로 쓴다.**

| 아이디어 | 파일 | 검사기가 거부한 이유 | 시도 | 회복 |
|---|---|---|---:|---|
| `local_docs_search` | `README.md` | 본문에 제목 줄 | 2 | 예 |
| `local_docs_search` | `02_model/relations.md` | 본문에 제목 줄 | 2 | 예 |
| `codebase_assistant` | `04_work/next_steps.md` | 본문에 제목 줄 | 2 | 예 |
| `codebase_assistant` | `99_review/open_questions.md` | 본문에 제목 줄 | 2 | 예 |
| `youtube_digest` | `03_rules/invariants.md` | 본문에 제목 줄 | 2 | 예 |
| `youtube_digest` | `99_review/open_questions.md` | 본문에 제목 줄 | 2 | 예 |
| `youtube_digest` | `03_rules/safety.md` | 본문에 제목 줄 | 3 | 예 |
| `youtube_digest` | `03_rules/safety.md` | 파일 제목 반복 | 3 | 예 |
| `youtube_digest` | `04_work/runs.md` | 본문에 제목 줄 | 3 | 예 |
| `youtube_digest` | `04_work/runs.md` | 파일 제목 반복 | 3 | 예 |
| `image_translation` | `README.md` | 파일 제목 반복 | 2 | 예 |
| `image_translation` | `01_context/goals.md` | 파일 제목 반복 | 2 | 예 |
| `image_translation` | `02_model/entities.md` | 본문에 제목 줄 | 2 | 예 |
| `image_translation` | `04_work/runs.md` | 본문에 제목 줄 | 2 | 예 |
| `meeting_notes` | `README.md` | 본문에 제목 줄 | 2 | 예 |
| `meeting_notes` | `01_context/goals.md` | 본문에 제목 줄 | 2 | 예 |
| `meeting_notes` | `03_rules/safety.md` | 본문에 제목 줄 | 2 | 예 |
| `meeting_notes` | `04_work/runs.md` | 파일 제목 반복 | 2 | 예 |
| `webnovel_translation` | `README.md` | 본문에 제목 줄 | 2 | 예 |
| `webnovel_translation` | `01_context/constraints.md` | 파일 제목 반복 | 2 | 예 |
| `webnovel_translation` | `02_model/relations.md` | 파일 제목 반복 | 2 | 예 |
| `webnovel_translation` | `02_model/subjects.md` | 파일 제목 반복 | 2 | 예 |
| `webnovel_translation` | `04_work/outline.md` | 파일 제목 반복 | 2 | 예 |
| `persona_chatbot` | `04_work/outline.md` | 본문에 제목 줄 | 2 | 예 |
| `local_model_bench` | `README.md` | 본문에 제목 줄 | 2 | 예 |
| `local_model_bench` | `01_context/constraints.md` | 본문에 제목 줄 | 2 | 예 |
| `local_model_bench` | `02_model/relations.md` | 본문에 제목 줄 | 2 | 예 |
| `local_model_bench` | `02_model/sources.md` | 본문에 제목 줄 | 2 | 예 |
| `local_model_bench` | `04_work/experiments.md` | 본문에 제목 줄 | 2 | 예 |
| `prompt_ab_test` | `README.md` | 본문에 제목 줄 | 2 | 예 |
| `prompt_ab_test` | `01_context/goals.md` | 파일 제목 반복 | 2 | 예 |
| `prompt_ab_test` | `01_context/constraints.md` | 본문에 제목 줄 | 2 | 예 |
| `prompt_ab_test` | `02_model/entities.md` | 본문에 제목 줄 | 2 | 예 |
| `prompt_ab_test` | `02_model/sources.md` | 본문에 제목 줄 | 2 | 예 |
| `prompt_ab_test` | `03_rules/method.md` | 본문에 제목 줄 | 4 | 예 |
| `prompt_ab_test` | `03_rules/method.md` | 다른 파일의 제목 | 4 | 예 |
| `prompt_ab_test` | `03_rules/method.md` | 다른 파일의 제목 | 4 | 예 |
| `workflow_audit` | `99_review/open_questions.md` | 같은 제목 반복 | 3 | 예 |
| `workflow_audit` | `99_review/open_questions.md` | 같은 제목 반복 | 3 | 예 |

**서로 다른 거부는 서로 다른 답을 요구하고, 실제로 다르게 대응한다.** 잘린 답은 요청이 맞고
천장이 낮았던 것이므로 예산을 두 배로 올리고 문구는 그대로 둔다. 본문에 제목을 넣은 답은
분명한 요청에 틀린 답을 한 것이므로, 다음 시도에 검사기가 확인한 사실을 적어 보내고
**이미 받은 이유를 전부 함께 들고 간다.**

마지막 부분이 중요하다. **다음 규칙을 배우면서 앞 규칙을 잊는 요청은 둘 사이를 왕복한다.**

## 재현하기

```
python fill_with_local_model.py --idea examples/local_docs_search.md \
    --preset software --model 사용할-모델-ID --out fill.json --receipt receipt.json
python synapse_mini.py demo --idea examples/local_docs_search.md \
    --preset software --content-json fill.json
```

`plan`은 결정적이다. 같은 아이디어와 프리셋이면 어디서든 같은 파일 목록과 같은 계획 id가 나온다.
**채우기는 결정적이지 않다** — 다른 모델은 물론 같은 모델도 두 번 돌리면 다른 글을 쓰고 다른
이유로 거부될 수 있다. 어느 쪽이었는지는 영수증에 남는다.

## READY가 재는 것

READY는 모든 필수 파일의 모든 제목 밑에 쓰인 내용이 있다는 뜻이다. **구조에 대한 결과이고,
그 경계를 분명히 하는 것이 이것을 쓸 수 있게 만든다.**

**사실성이 아니다.** 위에서 거부된 39건 중 **14건은 검사기가 아예 볼 수 없는
종류다** — 파일 자기 이름을 절 제목으로 쓴 것, 다른 파일이 이미 쓴 제목을 다시 쓴 것.
비어 있지도 템플릿도 아니므로 **전부 READY 파일이 되었을 것이다.** 한 층 앞에서, 무엇을
요청했는지 아는 스크립트가 잡았다. 검사기가 틀린 것이 아니라 **받은 질문에 답한 것이다.**

**도메인 품질이 아니다.** 내용이 그 분야에서 좋은지는 여기서 판정하지 않는다.
변호사도 편집자도 보안 검토자도 각자 할 일이 그대로 있고, **이것은 그들을 대신하는 것이 아니라
검토할 수 있을 만큼 갖춰진 것을 건네주도록** 만들어졌다.

**허가가 아니다.** PASS는 무엇을 반영해도 좋다는 권한을 주지 않는다. 제안과 확정 상태가 여기서
갈라져 있는 이유는 **어느 쪽이 될지를 사람이 정하게 하기 위해서**다.

위의 표가 그 셋의 증거다. 요약하지 않고 **숫자 그대로 싣는 이유**가 그것이다.
