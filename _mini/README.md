**English** · [한국어](README.ko.md) · [中文](README.zh.md)

# Synapse mini

A small local entry point for the canonical Synapse core. It is a projection and
a launcher, not a second implementation: it imports the planner and the
substantive-content checker rather than reimplementing them, never calls a
model, never changes canonical state, and never writes a workspace you did not
ask for.

For why it is built this way, see the [top-level README](../README.md).

## Commands

Build a plan. Writes nothing unless `--out` is given:

```
python synapse_mini.py plan examples/local_docs_search.md --preset software
```

Read an existing workspace without touching it:

```
python synapse_mini.py check path/to/workspace --idea examples/local_docs_search.md --preset software
```

Judge a path-to-content proposal without writing it:

```
python synapse_mini.py demo --idea examples/local_docs_search.md --preset software --content-json examples/local_docs_search_fill.json
```

`demo` and `check` exit zero only when every required file passes the canonical
`has_substantive_workspace_content` predicate. That is a minimum-content result.
It is not a quality approval and it grants no authority to apply anything.

## Where the core comes from

This folder sits inside the repository, so the default source is its parent --
the directory holding `synapse/`. A public build ships the core alongside it and
resolves the same way. For any other layout, pass `--source-root` or set
`SYNAPSE_SOURCE_ROOT`.

No path is written down here on purpose. A hardcoded one is correct for exactly
one machine and would follow this folder into a public release.

## The example ideas

`examples/` holds ten live-run ideas across all five presets, plus one separate
English sample, so that the first
question a reader has -- what does this do to an idea of mine? -- is answered by
running one command rather than by reading a description.

| idea | preset | planned files |
|---|---|---:|
| `local_docs_search.md` | software | 12 |
| `codebase_assistant.md` | software | 12 |
| `idea.md` (English) | software | 12 |
| `youtube_digest.md` | automation | 13 |
| `image_translation.md` | automation | 13 |
| `meeting_notes.md` | automation | 13 |
| `webnovel_translation.md` | content | 13 |
| `persona_chatbot.md` | content | 13 |
| `local_model_bench.md` | research | 13 |
| `prompt_ab_test.md` | research | 13 |
| `workflow_audit.md` | general | 10 |

They are ordinary requests rather than showcase material, and deliberately not
all the same kind of project. The structure is domain neutral or it is worth
nothing, and ten live-run ideas across five presets is a cheaper way to say that than
a paragraph claiming it. One is in English and the rest are in Korean, for the
same reason.

They also do a second job. A preset is a short list of files with one line of
purpose each, and a list that short is easy to agree with and hard to judge. Put
ten different live-run ideas through it and the thin spots show: which purposes a
model reads the same way every time, which ones it confuses with a neighbour,
and which file a preset is missing. That is the cheapest feedback available on
the preset data, and it costs nothing but the runs.

Results of the live runs are in [RESULTS.md](examples/RESULTS.md).

## Filling a plan with a local model

`fill_with_local_model.py` asks an OpenAI-compatible endpoint -- LM Studio,
llama.cpp, vLLM, anything speaking that protocol -- to write each planned file,
writes a JSON proposal, and stops.

```
python fill_with_local_model.py --idea examples/local_docs_search.md --preset software --model YOUR-MODEL-ID --out fill.json --receipt receipt.json
python synapse_mini.py demo --idea examples/local_docs_search.md --preset software --content-json fill.json
```

Read those two commands in order, because the split between them is the point.

The first script is the part that is allowed to be wrong. A model will sometimes
answer the wrong question, stop halfway, or return an apology instead of a
document, so this script owns none of the decisions. It exits zero to mean the
run finished, never that the result is good.

The second is the part that decides, and it is the canonical checker rather than
a copy of it. Empty sections, headings with nothing under them, the template
purpose echoed back -- `demo` says BLOCKED and names the files.

Two jobs are kept away from the model on purpose. `00_intake/idea.md` is copied
verbatim, because its stated purpose is to preserve the idea as written and
asking a model to retype text it was just handed is a way to lose it. The
project manifest is rendered from the plan, with the model supplying one prose
line; the structure around that line is not a language problem.

The request carries a JSON schema, and the schema does more work than the
prompt. Asking a small model in prose for sections with real content gets an
essay about sections; declaring `minLength` on the body field gets sections. The
renderer then turns those into Markdown in which every heading has a body, so
what the checker requires is true by construction rather than by the model's
good behaviour.

What the schema cannot do is make the string inside it mean anything. Two live
failures came from a model writing something other than prose into a body field
while still returning valid JSON of the right shape. Both are refused here, in
the untrusted script, rather than in the checker -- see RESULTS.md.

Standard library only, so it runs wherever the release runs.

## Choosing a model

One requirement, and it is about the server rather than the model: the endpoint
must accept `response_format: {"type": "json_schema", ...}` with `strict`. If it
does not, every request comes back HTTP 400 and nothing here runs.

What was actually measured, on one machine in September 2026:

| model | what it did |
|---|---|
| `nvidia-nemotron-3.5-lightning-30b-a3b` (MoE, 3B active) | all ten examples: 125 files, 39 refusals, 39 recoveries |
| `qwen3.5-4b-uncensored-hauhaucs-aggressive` | one example: 9 files of 13, 25 refusals, 5 recoveries |

Four models were also given the same single file — the two above, a
`glm-4.7-flash-uncensored-heretic-neo-code-imatrix-max` and a 30B reasoning distill — and all four returned usable
sections for it.

**Size did not show in the first answer. It showed in recovery.** The 4B model
wrote sections that belonged to their file; its 24 headings were all distinct
and none strayed into a neighbour's role. But when the checker refused one, it
mostly could not use the correction, and four files were never written. The
larger model recovered from every refusal it was given. If you are choosing
between models, choose on that.

### Before blaming a model

This repository has a record of 24 model runs marked failed. Eighteen were HTTP
400, five were a thirty-second client timeout, and the last was another 400.
**None of them was a content problem.** The model that went nought-for-three in
that table later wrote 125 files without trouble, and another that was dropped
on the strength of it answered correctly on the first attempt when asked again
with the current request.

A local answer can take ninety seconds; the default timeout here is 300 for
that reason. Read `finish_reason` and the receipt before concluding anything
about a model. Most of the time the request is what changed.
