[English](README.md) · **한국어** · [中文](README.zh.md)

# Synapse mini

정본 Synapse 코어로 들어가는 작은 로컬 진입점입니다. 두 번째 구현이 아니라
투영이자 실행기입니다. 계획 생성기와 실질내용 검사기를 **다시 만들지 않고
가져다 씁니다.** 모델을 부르지 않고, 정본 상태를 바꾸지 않으며, 시키지 않은
작업 폴더를 쓰지 않습니다.

왜 이렇게 만들었는지는 [최상위 README](../README.ko.md)에 있습니다.

## 명령

계획을 만듭니다. `--out`을 주지 않으면 아무것도 쓰지 않습니다:

```
python synapse_mini.py plan examples/local_docs_search.md --preset software
```

이미 있는 작업 폴더를 건드리지 않고 읽습니다:

```
python synapse_mini.py check path/to/workspace --idea examples/local_docs_search.md --preset software
```

경로→내용 제안을 쓰지 않고 판정합니다:

```
python synapse_mini.py demo --idea examples/local_docs_search.md --preset software --content-json examples/local_docs_search_fill.json
```

`demo`와 `check`는 모든 필수 파일이 정본 `has_substantive_workspace_content`
판정을 통과할 때만 0으로 끝납니다. **이것은 최소내용 결과입니다.** 품질 승인이
아니고, 무엇을 반영해도 좋다는 권한을 주지 않습니다.

## 코어를 어디서 찾는가

이 폴더는 저장소 안에 있으므로 기본 출처는 부모 디렉터리 — `synapse/`를 담고 있는
그 폴더입니다. 공개 빌드는 코어를 나란히 실어 보내고 같은 방식으로 찾습니다.
그 밖의 배치라면 `--source-root`를 주거나 `SYNAPSE_SOURCE_ROOT`를 설정하세요.

**여기에 경로를 적어 두지 않은 것은 의도입니다.** 박아 넣은 경로는 정확히 한 대의
기계에서만 맞고, 공개 릴리스까지 이 폴더를 따라갑니다.

## 예시 아이디어

`examples/`에는 프리셋 다섯 종을 모두 덮는 라이브 실행 아이디어 10개와 별도 영어
샘플 1개가 있습니다. 읽는 사람이
처음 갖는 질문 — **내 아이디어를 넣으면 뭐가 나오는가** — 을 설명문이 아니라
명령 한 줄로 답하기 위해서입니다.

| 아이디어 | 프리셋 | 계획 파일 |
|---|---|---:|
| `local_docs_search.md` 내 PC 문서 검색기 | software | 12 |
| `codebase_assistant.md` 내 코드베이스에 질문하기 | software | 12 |
| `idea.md` (영어) | software | 12 |
| `youtube_digest.md` 유튜브 자막 요약 | automation | 13 |
| `image_translation.md` 이미지 속 글자 번역 | automation | 13 |
| `meeting_notes.md` 녹음에서 회의록과 할 일 | automation | 13 |
| `webnovel_translation.md` 연재물 번역 일관성 | content | 13 |
| `persona_chatbot.md` 캐릭터가 안 무너지는 봇 | content | 13 |
| `local_model_bench.md` 로컬 모델 비교 기록 | research | 13 |
| `prompt_ab_test.md` 프롬프트 변경 측정 | research | 13 |
| `workflow_audit.md` 자동화할 일 고르기 | general | 10 |

보여주기용이 아니라 흔한 요청들이고, **일부러 같은 종류로 모으지 않았습니다.**
구조는 도메인 중립이거나 아니면 아무 값이 없고, 프리셋 다섯 종에 아이디어 열한
개가 그것을 주장하는 문단보다 쌉니다. 하나만 영어이고 나머지는 한국어인 것도
같은 이유입니다.

두 번째 역할도 합니다. 프리셋은 파일 목록에 한 줄짜리 목적문이 붙은 게 전부라서,
**읽으면 다 맞는 말이고 판단할 거리가 없습니다.** 서로 다른 아이디어 열한 개를
통과시키면 얇은 데가 드러납니다. 어떤 목적문을 모델이 매번 같게 읽는지, 어떤
목적문을 옆 파일과 헷갈리는지, 어떤 파일이 그 프리셋에 없는지. 프리셋 데이터에
대해 얻을 수 있는 가장 싼 피드백이고, 실행 비용 말고는 들지 않습니다.

라이브 실행 결과는 [RESULTS.md](examples/RESULTS.ko.md)에 있습니다.

## 로컬 모델로 계획 채우기

`fill_with_local_model.py`는 OpenAI 호환 엔드포인트 — LM Studio, llama.cpp,
vLLM, 그 프로토콜을 말하는 무엇이든 — 에게 계획된 파일을 하나씩 써 달라고 하고,
JSON 제안을 쓴 다음, 멈춥니다.

```
python fill_with_local_model.py --idea examples/local_docs_search.md --preset software --model 사용할-모델-ID --out fill.json --receipt receipt.json
python synapse_mini.py demo --idea examples/local_docs_search.md --preset software --content-json fill.json
```

**이 두 명령을 순서대로 읽으세요. 둘로 나뉘어 있다는 것이 요점입니다.**

첫 번째 스크립트가 틀려도 되는 쪽입니다. 모델은 가끔 다른 질문에 답하고, 중간에
멈추고, 문서 대신 사과문을 돌려줍니다. 그래서 이 스크립트는 **아무 결정도 소유하지
않습니다.** 종료 코드 0은 실행이 끝났다는 뜻이지 결과가 좋다는 뜻이 아닙니다.

두 번째가 판정하는 쪽이고, 사본이 아니라 정본 검사기 그 자체입니다. 빈 절, 밑에
아무것도 없는 제목, 템플릿 목적문을 그대로 되돌려준 것 — `demo`가 BLOCKED라고
말하고 파일 이름을 댑니다.

두 가지 일은 일부러 모델에게 주지 않습니다. `00_intake/idea.md`는 그대로
복사합니다. 이 파일의 역할이 **쓰인 그대로 보존하는 것**이고, 방금 건네준 글을
모델에게 다시 쓰라고 하는 것은 그것을 잃는 방법이기 때문입니다. 프로젝트 매니페스트는
계획에서 렌더링하고 모델은 문장 한 줄만 댑니다. 그 줄을 둘러싼 구조는 언어 문제가
아닙니다.

요청에는 JSON 스키마가 실리고, **스키마가 프롬프트보다 많은 일을 합니다.** 작은
모델에게 말로 "내용 있는 절"을 달라고 하면 절에 대한 수필이 오고, body 필드에
`minLength`를 선언하면 절이 옵니다. 그다음 렌더러가 그것을 **모든 제목 밑에 본문이
있는** 마크다운으로 바꾸기 때문에, 검사기가 요구하는 것이 모델의 선의가 아니라
구성상 참이 됩니다.

스키마가 할 수 없는 일은 **그 안의 문자열이 말이 되게 하는 것**입니다. 라이브에서
나온 실패 두 건 모두, 모델이 body 필드에 글이 아닌 것을 쓰면서도 모양이 맞는 유효한
JSON을 돌려준 경우였습니다. 둘 다 검사기가 아니라 **믿지 않는 쪽인 이 스크립트에서**
거부합니다 — RESULTS.md를 보세요.

표준 라이브러리만 씁니다. 릴리스가 도는 곳이면 어디서든 돕니다.

## 모델 고르기

요구사항은 하나이고, **모델이 아니라 서버 이야기**입니다. 엔드포인트가
`response_format: {"type": "json_schema", ...}` 와 `strict` 를 받아야 합니다.
안 받으면 모든 요청이 HTTP 400으로 돌아오고 여기 있는 것은 하나도 안 돕니다.

2026년 9월, 기계 한 대에서 실제로 잰 것:

| 모델 | 무엇을 했는가 |
|---|---|
| `nvidia-nemotron-3.5-lightning-30b-a3b` (MoE, 활성 3B) | 예시 10개 전부: 파일 125개, 거부 39건, 회복 39건 |
| `qwen3.5-4b-uncensored-hauhaucs-aggressive` | 예시 1개: 13개 중 9개, 거부 25건, 회복 5건 |

같은 파일 하나를 모델 넷에게도 시켰습니다 — 위 둘, `glm-4.7-flash-uncensored-heretic-neo-code-imatrix-max`, 그리고 30B 추론
증류 모델. **넷 다 쓸 만한 절을 돌려줬습니다.**

**크기는 첫 답에서 드러나지 않았습니다. 회복에서 드러났습니다.** 4B 모델도 제 파일에
맞는 절을 썼습니다 — 제목 24개가 전부 다르고 옆 역할을 침범한 것도 없습니다. 그런데
**검사기가 하나를 거부하면 그 교정을 대체로 쓰지 못했고**, 파일 4개가 끝내 안 쓰였습니다.
큰 쪽은 받은 거부 전부에서 회복했습니다. **모델을 고른다면 그 기준으로 고르세요.**

### 모델을 탓하기 전에

이 저장소에는 실패로 기록된 모델 실행 24건이 있습니다. **18건이 HTTP 400, 5건이 30초
클라이언트 타임아웃, 나머지 1건도 400입니다. 내용 문제는 한 건도 없습니다.**

그 표에서 **3전 3패였던 모델이 나중에 파일 125개를 문제없이 썼고**, 그 표를 근거로
치워졌던 다른 모델은 **지금 요청으로 다시 물었더니 첫 시도에 제대로 답했습니다.**

로컬 응답은 90초가 걸릴 수 있습니다. 여기 기본 타임아웃이 300초인 이유가 그겁니다.
**모델에 대해 결론 내리기 전에 `finish_reason`과 영수증을 보세요. 대개 바뀐 것은
요청 쪽입니다.**
