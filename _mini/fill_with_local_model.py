"""Ask a local OpenAI-compatible model to fill a Synapse plan.

This script is the part that is allowed to be wrong.

It talks to whatever server is listening -- LM Studio, llama.cpp, vLLM, any
OpenAI-compatible endpoint -- and asks it to write the body of each planned
file.  It then writes a JSON proposal and stops.  It does not decide whether
the result is good enough.  "synapse_mini.py demo" does that, using the
canonical checker, and it will say BLOCKED whenever this script produced
something empty or templated.

Two jobs are deliberately kept away from the model:

* "00_intake/idea.md" is copied verbatim, because its stated purpose is to
  preserve the idea as written.  Asking a model to reproduce text it was just
  given is a way to lose it.
* the project manifest is rendered from the plan.  The model supplies one
  prose line; the structure around it is not a language problem.

Everything else is one request per file, with a JSON schema that names the
shape.  The schema, not the wording of the prompt, is what makes a small model
return sections instead of an apology -- and the renderer below turns those
sections into Markdown in which every heading has a body, which is what the
canonical checker actually requires.

Standard library only, so it runs wherever the release runs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synapse_mini

DEFAULT_ENDPOINT = "http://127.0.0.1:1234/v1"
MIN_BODY_CHARS = 80
MAX_BODY_CHARS = 800
MIN_SECTIONS = 2
# Three, not four. The budget has to hold every section of the answer, and a
# fourth long one is what pushed two files past the output limit on 2026-09-15.
# Bounding the request is cheaper than raising the ceiling and retrying.
MAX_SECTIONS = 3

SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "minItems": MIN_SECTIONS,
            "maxItems": MAX_SECTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "heading": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 60,
                        "description": "Section title as plain text, with no leading # marks.",
                    },
                    "body": {
                        "type": "string",
                        "minLength": MIN_BODY_CHARS,
                        "maxLength": MAX_BODY_CHARS,
                        # The prompt asks for this too. The schema is what the
                        # model obeys -- minLength on this field is what turned
                        # one-line apologies into sections in the first place --
                        # so the rule that matters most is stated here as well.
                        "description": (
                            "Prose for this one section. Plain paragraphs and list items only. "
                            "Never a Markdown heading line: the heading is the field above, and "
                            "one is added for you."
                        ),
                    },
                },
                "required": ["heading", "body"],
            },
        }
    },
    "required": ["sections"],
}

PURPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"purpose": {"type": "string", "minLength": 20, "maxLength": 200}},
    "required": ["purpose"],
}


class FillError(RuntimeError):
    """Raised when the endpoint could not be used as asked.

    `correction` is what gets added to the next request. It is written here,
    from what this checker determined, and never from anything the model said
    about itself -- a model that has just answered the wrong question is not
    the authority on why. Feeding its own words back would also make the run
    unreproducible, because the next attempt would depend on prose that varies.
    """

    correction = ""


class TruncatedError(FillError):
    """The provider stopped at the output limit rather than at the end.

    No correction: the request was right and the ceiling was too low, so the
    caller raises the budget instead of rewording anything.
    """


class HeadingInBodyError(FillError):
    """A body carried its own Markdown heading, so the renderer would nest it."""

    def __init__(self, index: int) -> None:
        super().__init__(f"section {index} put a Markdown heading inside its body")
        self.correction = (
            "\n\n앞선 답변은 거부되었습니다. 검증기가 확인한 사실: "
            f"{index}번째 절의 body에 '#'으로 시작하는 줄이 있었습니다. "
            "body에는 제목 줄을 쓸 수 없습니다. 제목은 heading 필드에만 쓰고, "
            "body에는 문단과 목록만 쓰세요. 문서 한 편이 아니라 한 절의 본문입니다."
        )


class ResponseFormatInBodyError(FillError):
    """A body ran on into the JSON shape it was being returned in."""

    def __init__(self, index: int) -> None:
        super().__init__(f"section {index} wrote the response format into its body")
        self.correction = (
            "\n\n앞선 답변은 거부되었습니다. 검증기가 확인한 사실: "
            f"{index}번째 절의 body 안에 응답 형식의 키 이름이 들어 있었습니다. "
            "body에는 사람이 읽을 글만 쓰고, JSON 구조를 본문에 적지 마세요."
        )


def _post(endpoint: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise FillError(f"HTTP {exc.code} from {endpoint}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FillError(f"no answer from {endpoint}: {exc}") from exc


def _ask(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    payload = _post(
        endpoint,
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "synapse_mini_fill",
                    "strict": True,
                    "schema": schema,
                },
            },
        },
        timeout,
    )
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise FillError(f"unexpected response shape: {json.dumps(payload)[:300]}") from exc
    finish_reason = str(choice.get("finish_reason") or "")
    # The same rule the canonical runtime uses. A truncated body is reported,
    # never quietly accepted -- an answer cut off mid-sentence can still look
    # like prose and pass a careless eye.
    if finish_reason.strip().lower() in {"length", "max_tokens"}:
        raise TruncatedError(f"the model hit its output limit (finish_reason={finish_reason})")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise FillError(f"answer was not JSON: {content[:200]}") from exc
    if not isinstance(parsed, dict):
        raise FillError("answer was JSON but not an object")
    return parsed, finish_reason


# Fragments that can only be here because the model started writing the
# response format inside a string instead of finishing the sentence. The
# grammar cannot catch this: the result is still valid JSON and the string is
# still long enough, so it renders, and the content checker passes it -- a
# heading with a body under it is exactly what it sees. Observed live on
# 2026-09-15 in 04_work/runs.md, where a body ran on into
#   ... 상대 경로 사용 권장) } , { "heading": "파일의 역할", "body": "1. ...
# Constrained decoding guarantees the shape. Nothing guarantees the sense.
_SCHEMA_LEAK_MARKERS = ('"heading"', '"body"', '"sections"')

# The other way a body stops being a body. Observed live on 2026-09-15 in the
# content example: asked for the body of one section, the model answered with a
# whole Markdown document, headings and all, and then repeated it. The renderer
# prefixed "## " to a heading that was already "# ...", which produced a heading
# with nothing under it.
#
# That one the canonical checker caught, because heading structure is the thing
# it measures. The JSON leak above it did not, because that damage stayed inside
# a paragraph. Same cause -- the schema constrains the container, never the
# string inside it -- and the difference in outcome is only whether the damage
# happened to land where the checker was looking. Neither is the checker's job.
# The renderer owns heading levels, so a body carrying its own is refused here.
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE)


class NeighbourHeadingError(FillError):
    """A section was titled after another role instead of this one's content."""

    def __init__(self, index: int, title: str) -> None:
        super().__init__(f"section {index} is titled after another role: {title!r}")
        self.correction = (
            "\n\n앞선 답변은 거부되었습니다. 검증기가 확인한 사실: "
            f"{index}번째 절의 제목이 다른 담당자의 항목 이름과 같았습니다. "
            "절 제목은 맡은 항목의 내용에서 뽑아야 하고, 다른 항목의 이름을 "
            "그대로 쓸 수 없습니다."
        )


class FileTitleHeadingError(FillError):
    """A section reused the file title that the renderer already owns."""

    def __init__(self, index: int, title: str) -> None:
        super().__init__(f"section {index} repeats the file title: {title!r}")
        self.correction = (
            "\n\n앞선 답변은 거부되었습니다. 검증기가 확인한 사실: "
            f"{index}번째 절의 heading이 금지된 파일 제목 {title!r}과 완전히 같았습니다. "
            f"다음 답변의 heading에는 {title!r}을 문자 그대로 쓰지 마세요. "
            "렌더러가 이 제목을 이미 바깥 제목으로 추가하므로, 파일 내용의 한 측면을 나타내는 "
            "더 구체적인 제목을 선택하세요."
        )


class CrossFileHeadingError(FillError):
    """A heading already used by another file in this run.

    Every guard before this one looked inside a single answer, so a model that
    gave every file the same outline cleared all of them: within any one file
    the headings were distinct, and the canonical checker only ever sees one
    file at a time.

    Measured on 2026-09-15. One run of the meeting-notes example shared eight
    headings between files -- "받아쓰기 및 타임스탬프 처리" appeared in four,
    including the context, the rules and the review file -- and was READY 13/13.
    Fewer refusals, too: describing the same pipeline everywhere is an easy
    answer that nothing was rejecting.

    The driver fills a run's files in plan order, so it already knows what has
    been used. Repetition between files is rare when the answers are about
    their own subject: across ten examples filled with the subject named, the
    worst run shared two.
    """

    def __init__(self, index: int, heading: str, owner: str) -> None:
        super().__init__(f"section {index} reuses a heading from {owner}: {heading!r}")
        self.correction = (
            "\n\n앞선 답변은 거부되었습니다. 검증기가 확인한 사실: "
            f"{index}번째 절의 제목 {heading!r}은 이미 이 프로젝트의 다른 파일"
            f"({owner})에서 쓴 제목입니다. "
            "이 항목만의 측면을 가리키는 다른 제목을 쓰세요. "
            "모든 파일에 같은 목차를 반복하면 파일마다 같은 내용을 적게 됩니다."
        )


class RepeatedHeadingError(FillError):
    """Every section was given the same title, so they name nothing.

    The failure a negative instruction produces on its own. Told which titles
    it may not use and not what a section title is, the model stopped choosing:
    `02_model/entities.md` came back as three sections all called 핵심 대상 --
    the file's own name -- and `05_validation/tests.md` as two called 검증 계획.

    Both passed the neighbour check, because a file's own title is not a
    neighbour's, and both passed the canonical checker, because each was a
    heading with a paragraph under it. Only a reader would notice.
    """

    def __init__(self, index: int, heading: str) -> None:
        super().__init__(f"section {index} repeats an earlier heading: {heading!r}")
        self.correction = (
            "\n\n앞선 답변은 거부되었습니다. 검증기가 확인한 사실: "
            f"{index}번째 절의 제목이 앞의 절과 같았습니다. "
            "절 제목은 이 항목 안에서 서로 다른 측면을 가리켜야 하고, "
            "같은 제목을 두 번 쓰거나 항목 이름을 그대로 반복할 수 없습니다."
        )


def _render_sections(
    title: str,
    parsed: dict[str, Any],
    *,
    reserved: frozenset[str] = frozenset(),
    taken: Mapping[str, str] | None = None,
) -> str:
    sections = parsed.get("sections")
    if not isinstance(sections, list) or not sections:
        raise FillError("answer carried no sections")
    lines = [f"# {title}", ""]
    title = title.strip()
    seen: set[str] = set()
    for index, item in enumerate(sections, start=1):
        if not isinstance(item, dict):
            raise FillError("a section was not an object")
        heading = str(item.get("heading") or "").strip()
        body = str(item.get("body") or "").strip()
        if not heading or not body:
            raise FillError("a section was missing its heading or body")
        if any(marker in body for marker in _SCHEMA_LEAK_MARKERS):
            raise ResponseFormatInBodyError(index)
        if _MARKDOWN_HEADING.search(body) or heading.lstrip().startswith("#"):
            raise HeadingInBodyError(index)
        if heading.strip() == title:
            raise FileTitleHeadingError(index, title)
        if heading.strip() in reserved:
            raise NeighbourHeadingError(index, heading.strip())
        if heading.strip() in seen:
            raise RepeatedHeadingError(index, heading.strip())
        owner = (taken or {}).get(heading.strip())
        if owner:
            raise CrossFileHeadingError(index, heading.strip(), owner)
        seen.add(heading.strip())
        lines.extend([f"## {heading}", "", body, ""])
    return "\n".join(lines).rstrip() + "\n"


def _render_manifest(plan: Any, preset_id: str, purpose: str) -> str:
    return (
        f"name: {plan.project_name}\n"
        f"preset: {preset_id}\n"
        "status: PROPOSED\n"
        f"purpose: {purpose.strip()}\n"
    )


def _model_written(plan: Any) -> list[Any]:
    """The planned files the model is asked for -- the rest are built here."""

    return [
        item
        for item in plan.files
        if not item.path.endswith((".yaml", ".yml")) and item.path != "00_intake/idea.md"
    ]


def _build_prompt(plan: Any, item: Any, idea: str) -> str:
    """Give the model the whole contract and mark the one part it owns.

    The plan already knows every file, what each is for, and the order they
    come in. Not showing that was the cause of two separate failures rather
    than one: a model asked to write "03_rules/invariants.md" with no idea what
    the neighbouring files cover writes goals into it, and a model asked to
    write "README.md" writes a whole README -- headings, sections and all --
    because that is what a README is.

    So the file name is not in the request at all. The renderer owns the file;
    the model is given a role from a list of roles and asked for its body.
    """

    others = [
        f"  - {other.title}: {other.purpose}"
        for other in _model_written(plan)
        if other.path != item.path
    ]

    # The assignment is stated first and again last. In between, the other
    # roles appear only as things to leave out.
    #
    # An earlier version listed all the roles together with the assigned one
    # marked, which fixed one failure and caused another: asked for the
    # invariants, the model returned sections titled "사용 안내", "목표" and
    # "제약과 경계" -- the names of its neighbours, read off the list as an
    # outline. It passed the content checker, because they were headings with
    # bodies under them. A prompt that supplies a list of headings gets that
    # list back.
    return (
        f"프로젝트: {plan.project_name}\n\n"
        f"아이디어:\n{idea.strip()}\n\n"
        f"맡은 항목: {item.title}\n"
        f"이 항목이 적어야 하는 것: {item.purpose}\n\n"
        "아래는 다른 담당자가 이미 맡은 항목입니다. 여기에는 쓰지 마세요.\n"
        + "\n".join(others)
        + "\n\n아이디어에 없는 사실을 지어내지 말고, 아이디어에서 읽어낼 수 있는 것을 "
        "구체적으로 적으세요.\n"
        f"'{item.title}'의 본문만 {MIN_SECTIONS}~{MAX_SECTIONS}개의 절로 나눠 쓰세요.\n\n"
        # Telling a model only what a title may not be leaves it with nothing to
        # choose, and it then repeats the one name it is sure of. The positive
        # form is the whole fix; the guards below it are for the days it is not.
        # Naming the subject is what keeps the answer about this file. Replacing
        # these three mentions of the title with the abstract phrase "담당 역할"
        # was measured on 2026-09-15: the model lost what the file was about and
        # fell back on a description of the pipeline, which is the same in every
        # file. Eight headings were then shared between files -- one appeared in
        # four of them -- and the run passed, because no check was watching for
        # that. See RESULTS for the numbers.
        f"절 제목은 '{item.title}'을 이루는 서로 다른 측면을 가리키는 짧은 말입니다. "
        f"'{item.title}'을 세 측면으로 나눈다면 각 측면의 이름이 절 제목이 됩니다. "
        f"'{item.title}'을 그대로 반복하거나, 같은 제목을 두 번 쓰거나, "
        "위 목록에 있는 다른 항목의 이름을 가져오면 안 됩니다.\n"
        "제목은 heading 필드에만 쓰고, body에는 제목 줄('#'으로 시작하는 줄) 없이 "
        "문단과 목록만 씁니다."
    )


def _fill_one(
    args: argparse.Namespace,
    plan: Any,
    idea: str,
    item: Any,
    max_tokens: int,
    correction: str = "",
    taken: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return the rendered body for one planned file and the finish reason."""

    prompt = _build_prompt(plan, item, idea) + correction
    if item.path.endswith((".yaml", ".yml")):
        parsed, finish_reason = _ask(
            endpoint=args.endpoint,
            model=args.model,
            prompt=prompt + "\n\n이 프로젝트의 목적을 한 문장으로 적으세요.",
            schema=PURPOSE_SCHEMA,
            max_tokens=max_tokens,
            temperature=args.temperature,
            timeout=args.timeout,
        )
        return _render_manifest(plan, args.preset, str(parsed.get("purpose", ""))), finish_reason
    parsed, finish_reason = _ask(
        endpoint=args.endpoint,
        model=args.model,
        prompt=prompt,
        schema=SECTION_SCHEMA,
        max_tokens=max_tokens,
        temperature=args.temperature,
        timeout=args.timeout,
    )
    reserved = frozenset(
        other.title.strip() for other in _model_written(plan) if other.path != item.path
    )
    return _render_sections(item.title, parsed, reserved=reserved, taken=taken), finish_reason


def fill(args: argparse.Namespace) -> dict[str, Any]:
    idea = synapse_mini._read_idea(args.idea)
    root, _, build_scaffold_plan, _ = synapse_mini._core(args.source_root)
    plan = build_scaffold_plan(idea, preset_id=args.preset, project_name=args.project_name)

    # Say the size of the work before starting it. The idea is sent with every
    # request, so a pasted folder rather than a paragraph is the difference
    # between a two minute run and a fifteen minute one -- measured, on a 19 KB
    # paste across twelve files. Nothing was printed until the first file came
    # back, which is a long time to look like nothing is happening.
    asked = [item for item in plan.files if item.path != "00_intake/idea.md"]
    print(
        f"{plan.project_name}: {len(asked)} files to ask for, "
        f"{len(idea.encode('utf-8')) / 1000:.1f} KB of idea in every request",
        flush=True,
    )

    contents: dict[str, str] = {}
    receipts: list[dict[str, Any]] = []
    # heading -> the file that used it first, in plan order.
    taken: dict[str, str] = {}
    for item in plan.files:
        if item.path == "00_intake/idea.md":
            contents[item.path] = idea if idea.endswith("\n") else idea + "\n"
            receipts.append({"path": item.path, "source": "idea_verbatim"})
            continue
        failures: list[str] = []
        budget = args.max_tokens
        # Every distinct correction the checker has raised for this file, kept
        # in the order they were learned. An earlier version replaced the
        # correction instead of adding to it, and the refusals then cycled:
        # heading-in-body, cross-file heading, heading-in-body again, because
        # the request that fixed the first had stopped mentioning it. Measured
        # 2026-09-15 on 02_model/entities.md and 03_rules/method.md, each of
        # which burned every attempt oscillating between two rules and was
        # never written. More attempts would not have helped -- the request was
        # forgetting faster than the model was learning.
        learned: dict[type[FillError], str] = {}
        for attempt in range(1, args.retries + 1):
            try:
                rendered, finish_reason = _fill_one(
                    args, plan, idea, item, budget, "".join(learned.values()), taken
                )
            except TruncatedError as exc:
                # Retrying a truncated answer at the same budget asks the same
                # question and gets the same cut-off. Raise the ceiling, or the
                # retry is a wish rather than a second attempt.
                failures.append(str(exc))
                budget *= 2
                print(
                    f"  {item.path}: attempt {attempt} truncated -- retrying with "
                    f"max_tokens={budget}",
                    file=sys.stderr,
                )
                continue
            except FillError as exc:
                failures.append(str(exc))
                # Same rule, the other failures. A rejected answer that gets the
                # identical prompt back produces the identical answer, which is
                # what three refusals of README.md in a row demonstrated. The
                # correction comes from the error the checker raised, so it says
                # what was actually wrong and says it the same way every time.
                if exc.correction:
                    learned[type(exc)] = exc.correction
                    print(
                        f"  {item.path}: attempt {attempt} rejected ({exc}) -- "
                        "retrying with the reason stated",
                        file=sys.stderr,
                    )
                else:
                    print(f"  {item.path}: attempt {attempt} failed -- {exc}", file=sys.stderr)
                continue
            contents[item.path] = rendered
            for line in rendered.splitlines():
                if line.startswith("## "):
                    taken.setdefault(line[3:].strip(), item.path)
            receipts.append(
                {
                    "path": item.path,
                    "source": "model",
                    "attempts": attempt,
                    "finish_reason": finish_reason,
                    "utf8_bytes": len(rendered.encode("utf-8")),
                    "failures": failures,
                }
            )
            # flush: piped or redirected, Python buffers stdout, and a long
            # run then shows nothing at all until it is over.
            print(f"  {item.path}: {len(rendered.encode('utf-8'))} bytes", flush=True)
            break
        else:
            receipts.append(
                {
                    "path": item.path,
                    "source": "model",
                    "attempts": args.retries,
                    "finish_reason": None,
                    "utf8_bytes": 0,
                    "failures": failures,
                }
            )
            print(f"  {item.path}: NOT WRITTEN after {args.retries} attempts", file=sys.stderr)

    return {
        "schema_version": "synapse.mini.fill.v1",
        # Named, not located. A receipt is something people paste into an
        # issue, and the absolute path it was produced at is this machine's
        # business rather than the reader's.
        "canonical_source": "bundled" if Path(__file__).resolve().parents[1] == root else "external",
        "endpoint": args.endpoint,
        "model": args.model,
        "preset": args.preset,
        "plan_id": plan.id,
        "planned_files": len(plan.files),
        "written_files": len(contents),
        "files": receipts,
        "contents": contents,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill a Synapse mini plan with a local OpenAI-compatible model"
    )
    parser.add_argument("--idea", required=True, help="idea text or a UTF-8 text file")
    parser.add_argument("--preset", default="general")
    parser.add_argument("--project-name", default="")
    parser.add_argument("--source-root", default=None)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", required=True, help="model id as the endpoint lists it")
    parser.add_argument("--out", required=True, help="where to write the content JSON")
    parser.add_argument("--receipt", default=None, help="where to write the per-file run record")
    parser.add_argument("--max-tokens", type=int, default=2_400)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--timeout", type=float, default=300.0)
    # Four, because four is what the published results needed. Three files in
    # that run took three attempts and one took four, and no documented command
    # passes this flag -- so a default of two handed a reader BLOCKED on the
    # same example the evidence page shows as READY. Someone filling one idea
    # would rather wait for a third request than be told to start over, and the
    # extra attempts are only ever spent on a file that was already refused.
    parser.add_argument("--retries", type=int, default=4)
    args = parser.parse_args()

    try:
        record = fill(args)
    except (FillError, OSError, ValueError, KeyError) as exc:
        print("synapse-mini-fill: " + str(exc), file=sys.stderr)
        return 2

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(record["contents"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.receipt:
        receipt = Path(args.receipt).expanduser().resolve()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        summary = {key: value for key, value in record.items() if key != "contents"}
        receipt.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(f"wrote {record['written_files']}/{record['planned_files']} files to {out}")
    print("This script did not judge the result. Run `synapse_mini.py demo` to find out.")
    # Zero means the run finished, not that the content is good enough. The
    # checker owns that verdict, and saying so here would be a second opinion.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
