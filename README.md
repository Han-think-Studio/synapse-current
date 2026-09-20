**English** · [한국어](README.ko.md) · [中文](README.zh.md)

# Synapse mini

A small model forgets the shape of a project. Synapse mini keeps the shape in
Python and asks the model for one slot at a time.

It runs on its own, with one runtime dependency, and does not need a model in
order to work.

## What it does

1. **plan** — turn an idea into a deterministic list of files. Same idea in,
   same list out. No model, no network.
2. **check** — read an existing folder and report which required files contain
   authored content.
3. **demo** — judge a path-to-content proposal without writing anything.

`check` and `demo` print a JSON receipt naming each file and why it passed or
did not; `plan` prints the deterministic plan.

## Try it

```
python _mini/synapse_mini.py plan _mini/examples/local_docs_search.md --preset software
python _mini/synapse_mini.py demo --idea _mini/examples/local_docs_search.md \
    --preset software --content-json _mini/examples/local_docs_search_fill.json
```

The second command exits non-zero when any required file is empty, templated,
or missing.

## Portable Windows checkout

The public mini edition includes launchers that detect the folder containing
themselves, so the checkout can be moved or started from any current directory.
Double-click [`setup.bat`](setup.bat) once, then use
[`run.bat`](run.bat) for the recorded example or
[`run-core.bat`](run-core.bat) for the deterministic plan check. They
prepare a local Python 3.12 virtual environment beside the checkout when it
is missing or invalid; no developer-machine path is embedded.

To see the result before running anything, open [`evidence/`](evidence/live-idea2-nemotron-generated-20260916/README.md):
one live run's workspace, kept as the files it actually produced.

For how to write an idea that produces a structure worth keeping, see
[the guide](_mini/examples/HOWTO.md). It is the part that is in your
language rather than in the code.

To produce a fill yourself, use an OpenAI-compatible endpoint that accepts the
strict JSON Schema response format — LM Studio, llama.cpp, or vLLM:

```
python _mini/fill_with_local_model.py --idea _mini/examples/local_docs_search.md \
    --preset software --model YOUR-MODEL-ID --out fill.json
```

## Why it exists

The field's main bet is scale: a larger model, trained on more, needing less
scaffolding around it. That bet has paid off repeatedly, and nothing here
argues it was the wrong one.

In May 2026, asked where Google stood, its chief executive described the
company as strong on general capability — text, multimodality, reasoning — and
"a bit behind at this moment" on agentic coding: tool use, instruction
following, long-horizon tasks. He put part of that down to not having had the
surface through which that kind of work comes back as data, naming other
people's coding tools as the ones that did.

Source note: the [episode listing](https://podcasts.apple.com/us/podcast/our-field-trip-to-google-i-o-a-sit-down-with/id1528594034?i=1000769070660)
confirms the May 22, 2026 episode and Sundar Pichai as guest. The coding-gap
and data-surface paraphrase is checked against this [public transcript](https://bidclub.ai/zh/e/hard-fork-live-part-2-dylan-field-standing-out-i).

Read one way, that is a distribution story. Read another, it is about what the
gap was made of. General capability was not the thing in short supply. What was
short was everything a long task needs around the capability: a set of
instructions that survives to the end of it, premises that do not quietly move,
a record of what was already decided. That is structure, and it was thin.

So this went the other way. They went at the model; this went at the structure,
and had been going at it for some time already.

Where it was worked out was a long-form fiction project — months of work in
which a decision made early has to still hold much later. Nothing there is hard
for a model sentence by sentence. What is hard is that the premises must not
drift, and a model asked to carry them will eventually revise one without
mentioning it.

That is a personal bet rather than a proven one, and these were the reasons for
making it:

**Less waste.** A structure that holds across projects is not rebuilt for each
one. The file list, the roles and the checks are written once and reused; only
the content is new each time.

**Work that accumulates.** Revisions, working files and notes have somewhere to
go, and the structure says where. When it is a file rather than a habit, a
change to it is visible, reviewable and reversible — progress stops being
something somebody has to remember.

**A boundary the model does not cross.** This is the one that decided it. When
the region a model may rewrite is marked explicitly, everything outside it is
safe by construction rather than by supervision — and whoever reads the result
afterwards does not have to work out which parts were agreed and which were
improvised.

**Less for the model to carry.** Asked for one slot with one purpose, a model
is not also holding the shape of the project, and the run is shorter, cheaper
and easier to read. The attention goes into the work rather than into
reconstructing what the work was.

Putting that down first is not overhead paid before the interesting part. It is
what makes the interesting part reachable at all.

So the structure is written down. That choice predates any particular model and
outlives each one: when the model improves, the same contract holds and the
prose gets better. When the model is small, the same contract holds and the
prose gets plainer. Neither case asks anyone to re-derive what the project was
supposed to be.

## How it works

Give a small model a whole project and ask it to keep everything straight, and
it will lose the thread — a constraint quietly dropped, a file written for the
wrong purpose, a section that restates the one above it.

The answer here is to not ask. **Python owns the file list, the role of each
file, the order they come in, and the verdict.** The model is asked for one
thing: the prose that goes in one slot, given the purpose of that slot. It is
never asked to remember the project, to decide, or to declare anything
finished.

That is why the same contract holds whether the idea is a document search tool,
a meeting-notes system, or a novel. None of them needs the model to hold the
whole thing in mind, because nothing about the structure was the model's to
hold.

Two rules follow from that, and they are load-bearing:

**A model's output is a proposal.** Nothing a model returns changes state
directly. The filling script exits zero to mean *the run finished*, never *the
result is good*. A separate command decides that, and it is the same checker
the full system uses rather than a copy of it.

**The deterministic core runs with nothing installed.** No model, no GPU, no
vector database, no cloud service. That is why `plan` is useful before you have
decided anything about models, and why the core's tests run in CI.

## How it is put together

Five pieces, and which of them is allowed to be wrong is the whole design.

| | what it owns |
|---|---|
| `synapse/core/` | the planner and the content checker. Decides what exists and what counts as written |
| `domain_packs/_presets/` | five presets: a file list with one line of purpose each |
| `_mini/synapse_mini.py` | the CLI. Imports the core rather than reimplementing it, and judges nothing itself |
| `_mini/fill_with_local_model.py` | the only file that talks to a model. Owns no decisions |
| `_mini/examples/` | ten live-run ideas with their fills, plus one separate English sample |

The split between the last two is the one to read twice. The filling script is
declared untrusted: a model will sometimes answer the wrong question, stop
halfway, or return an apology instead of a document, so that script renders
what came back and exits zero to mean the run finished. The checker decides,
and it is the canonical one — not a copy that can drift from it.

## What it checks

`demo` reports READY when every required file has authored content under every
heading. It is a structural check, and it is precise about its own edges.

It does not judge whether the content is right for your domain, and it grants
no authority to apply anything. A person decides that — which is why a proposal
and confirmed state are separate things here rather than the same thing at two
different moments.

## What it catches

Ten example ideas, filled by one local model on one machine, are recorded in
[`_mini/examples/RESULTS.md`](_mini/examples/RESULTS.md): **10 READY, 0
blocked, 125 files.**

Along the way the structure refused 39 answers before they reached a file, and
all 39 were written on a later attempt. **Fourteen of those refusals were for
problems a content check cannot see** — a section named after the file it sits
in, or after a heading another file had already used. Nothing about them is
empty or templated; each would otherwise have become a finished file that read
like every other one.

Four things make that work, and they are worth stating plainly because none of
them is obvious:

**The schema is the real prompt.** Asking a small model in prose for
substantial sections gets apologies and one-liners. Declaring `minLength` on
the body field in the response schema gets sections. The wording of a request
is not what a model under constrained decoding is obeying.

**A check that cannot fail is not a check.** A truncated answer that arrives
labelled complete is worse than no truncation check at all, because people
trust it. Truncation is read from the provider's own stop reason, on every
dispatch path, and reported rather than inferred.

**Constrained decoding guarantees the shape, not the sense.** A schema
constrains the container and says nothing about the string inside it: valid
JSON of the right shape can still carry a whole Markdown document, or the
response format itself, written mid-sentence. Those are refused by the filling
script — the part that is allowed to be wrong — rather than by the checker,
which answers the question it was asked.

**A retry has to change the request.** A truncated answer gets a larger budget.
A refused one gets the reason the checker computed, and keeps every reason it
has already been given. Sending the same words again is a wish, not a second
attempt.

## Requirements

Python 3.12 and `pyyaml`. That is the whole list, and a release is not built
unless a bare interpreter with only that installed can run every command.

## Status

Pre-release. The deterministic core, the checks and the CLI are stable and
covered by tests that run without a model, a GPU or a network.

The larger system this was cut from is internal and is not offered here. No
performance or quality claims are made: the checks described above are
structural, and structural is what they are for.

## Edition and use

This public Git edition is **Synapse mini**: a small, read-only entry point to
the canonical Synapse core. The original Synapse working tree is the internal
development edition and is not included in this public package.

Personal, educational, research, and other genuinely non-commercial use is
permitted under the included custom license. Commercial use, paid services,
hosted offerings, and commercial redistribution are not permitted without
written permission. For permission questions, contact `yahoo4660@gmail.com`.

## License

See `LICENSE`.
