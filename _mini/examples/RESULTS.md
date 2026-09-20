**English** · [한국어](RESULTS.ko.md) · [中文](RESULTS.zh.md)

# Live run results

Ten ideas, one local model, one machine, one day. Each row was produced by
running the three steps below in order and recording what each returned.

- date: 2026-09-15
- model: `nvidia-nemotron-3.5-lightning-30b-a3b`
- endpoint: an OpenAI-compatible local server
- 10 runs, 125 files, 119 KB of model-written text
- **10 READY, 0 BLOCKED**
- 39 answers refused by the checker, 39 recovered on a later attempt

## plan → fill → check

| idea | preset | planned | written | verdict | passed | model output |
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

`planned` is what Python decided before any model was involved. `written` counts
the files that came back usable. `model output` counts only bytes the model
wrote: `00_intake/idea.md` is copied verbatim and the manifest is rendered from
the plan, so neither is included.

## What the structure caught

A success rate alone would hide this half. Every refusal below was computed by
the checker in `fill_with_local_model.py` -- never from the model's account of
itself -- and the reason carried into the next attempt is written by that
checker, the same way every time.

| idea | file | the checker refused because | attempts | recovered |
|---|---|---|---:|---|
| `local_docs_search` | `README.md` | heading in body | 2 | yes |
| `local_docs_search` | `02_model/relations.md` | heading in body | 2 | yes |
| `codebase_assistant` | `04_work/next_steps.md` | heading in body | 2 | yes |
| `codebase_assistant` | `99_review/open_questions.md` | heading in body | 2 | yes |
| `youtube_digest` | `03_rules/invariants.md` | heading in body | 2 | yes |
| `youtube_digest` | `99_review/open_questions.md` | heading in body | 2 | yes |
| `youtube_digest` | `03_rules/safety.md` | heading in body | 3 | yes |
| `youtube_digest` | `03_rules/safety.md` | file's own title | 3 | yes |
| `youtube_digest` | `04_work/runs.md` | heading in body | 3 | yes |
| `youtube_digest` | `04_work/runs.md` | file's own title | 3 | yes |
| `image_translation` | `README.md` | file's own title | 2 | yes |
| `image_translation` | `01_context/goals.md` | file's own title | 2 | yes |
| `image_translation` | `02_model/entities.md` | heading in body | 2 | yes |
| `image_translation` | `04_work/runs.md` | heading in body | 2 | yes |
| `meeting_notes` | `README.md` | heading in body | 2 | yes |
| `meeting_notes` | `01_context/goals.md` | heading in body | 2 | yes |
| `meeting_notes` | `03_rules/safety.md` | heading in body | 2 | yes |
| `meeting_notes` | `04_work/runs.md` | file's own title | 2 | yes |
| `webnovel_translation` | `README.md` | heading in body | 2 | yes |
| `webnovel_translation` | `01_context/constraints.md` | file's own title | 2 | yes |
| `webnovel_translation` | `02_model/relations.md` | file's own title | 2 | yes |
| `webnovel_translation` | `02_model/subjects.md` | file's own title | 2 | yes |
| `webnovel_translation` | `04_work/outline.md` | file's own title | 2 | yes |
| `persona_chatbot` | `04_work/outline.md` | heading in body | 2 | yes |
| `local_model_bench` | `README.md` | heading in body | 2 | yes |
| `local_model_bench` | `01_context/constraints.md` | heading in body | 2 | yes |
| `local_model_bench` | `02_model/relations.md` | heading in body | 2 | yes |
| `local_model_bench` | `02_model/sources.md` | heading in body | 2 | yes |
| `local_model_bench` | `04_work/experiments.md` | heading in body | 2 | yes |
| `prompt_ab_test` | `README.md` | heading in body | 2 | yes |
| `prompt_ab_test` | `01_context/goals.md` | file's own title | 2 | yes |
| `prompt_ab_test` | `01_context/constraints.md` | heading in body | 2 | yes |
| `prompt_ab_test` | `02_model/entities.md` | heading in body | 2 | yes |
| `prompt_ab_test` | `02_model/sources.md` | heading in body | 2 | yes |
| `prompt_ab_test` | `03_rules/method.md` | heading in body | 4 | yes |
| `prompt_ab_test` | `03_rules/method.md` | another file's heading | 4 | yes |
| `prompt_ab_test` | `03_rules/method.md` | another file's heading | 4 | yes |
| `workflow_audit` | `99_review/open_questions.md` | earlier heading | 3 | yes |
| `workflow_audit` | `99_review/open_questions.md` | earlier heading | 3 | yes |

Different refusals need different answers and get them. A truncated answer was
the right request against too low a ceiling, so the budget doubles and the
wording stays. A body carrying its own headings was the wrong answer to a clear
request, so the next attempt states what the checker found, and keeps every
reason it has already been given. That last part matters: a request that
forgets one rule while learning the next will cycle between them.

## Reproducing this

```
python fill_with_local_model.py --idea examples/local_docs_search.md \
    --preset software --model YOUR-MODEL-ID --out fill.json --receipt receipt.json
python synapse_mini.py demo --idea examples/local_docs_search.md \
    --preset software --content-json fill.json
```

`plan` is deterministic: the same idea and preset give the same file list and
the same plan id anywhere. The fill is not — a different model, or the same
model twice, will write different prose and may be refused for different
reasons. The receipt records which.

## What READY measures

READY means every required file has authored content under every heading. It is
a structural result, and being precise about its edges is what makes it usable.

**Not truth.** Of the 39 answers refused above, **14 were of a
kind the checker cannot see at all** -- a section titled after the file itself,
or after a heading another file had already used. Nothing about them is empty or
templated, so each would have become a READY file. They were caught one layer
earlier, by a script that knows what it asked for. The checker was not wrong; it
was answering the question it was asked.

**Not domain quality.** Nothing here judges whether the content is good for
its field. A lawyer, an editor and a security reviewer each still have their
own work to do, and this is designed to hand them something complete enough to
review rather than to replace them.

**Not permission.** A PASS grants no authority to apply anything. A proposal and
confirmed state are separate here so that a person decides which becomes which.

The table above is the evidence for all three, which is why it is published
with the numbers rather than summarised.
