**English** · [한국어](HOWTO.ko.md) · [中文](HOWTO.zh.md)

# Writing an idea that produces a structure worth keeping

The code is in English. The thinking that goes into the input is not, and that
is the part this page is about.

Everything here runs on one input: a few paragraphs you write in your own
language describing what you want. The structure that comes out is only as
useful as those paragraphs, and the difference between a description that
produces a usable project and one that produces a pile of headings is small,
learnable, and has nothing to do with prompt tricks.

## What goes in, and what comes out

You write a file. Then:

```
python synapse_mini.py plan examples/local_docs_search.md --preset software
```

turns it into a fixed list of files — no model, no network, same list every
time. Then a model writes each file's body, and the canonical checker decides
whether what came back is authored content or an empty shell.

Three things are decided before any model is involved: which files exist, what
each one is for, and what counts as finished. That is the whole method. The
model is asked for prose, one slot at a time, and is never asked to remember
the project or to declare it done.

## The five parts of an idea that works

Look at any file in this folder. They all have the same shape, and it is not a
template — it is the shortest form that carries enough for the structure to be
built.

1. **A title that names the thing**, not the technology.
   `이미지 속 글자 번역해서 다시 넣기`, not `OCR + MT 파이프라인`.
2. **The situation you are actually in.** What you do today and why it does not
   work. Two or three sentences. This is what stops the model inventing a
   different problem.
3. **What you want instead.** One paragraph.
4. **The conditions that matter.** Four to six lines. This is the load-bearing
   part; see below.
5. **The first step.** One sentence: what to do first and what to leave until
   later. Without it you get a roadmap nobody asked for.

## The one rule that decides whether this is worth doing

**A condition you can check becomes a rule. A condition you cannot check stays
a wish.**

The structure has a file for rules that must always hold, and a check that asks
whether each file was actually written. Neither can do anything with a wish.

| a wish | a rule |
|---|---|
| Translate accurately | Proper nouns keep one spelling across the whole series; changing one leaves a record |
| Handle errors well | A region whose text could not be read is marked as unread, not left blank |
| Be fast | Only changed files are re-indexed; a full re-read is a bug, not a slow path |
| Don't hallucinate | An answer with no source paragraph must say "none" instead of answering |
| Keep the data safe | Document text never leaves this machine; embeddings are made locally |

The right-hand column is not more detailed. It is **falsifiable**: you can look
at a result and say whether it happened.

### Write the failure you are afraid of

The strongest line in any of these examples is this one, from the image
translation idea:

> If the translated text is longer than the space it goes into, do not shrink
> it — mark it as overflowing. Silently shipping a cut-off line is the worst
> outcome.

That sentence does three jobs at once. It names a failure, it forbids the
convenient workaround, and it says which of two bad outcomes is worse. A
structure built from it can be checked; a structure built from "make it look
good" cannot.

Every example here has at least one line of that kind. They are the reason the
examples are worth reading even if your project is nothing like them.

## What the check will tell you, and what it will not

`demo` and `check` report READY when every required file has authored content
under every heading. That is a floor.

It will tell you a file is empty, templated, or missing. It will not tell you
the content is correct, complete, or sensible. In the ten runs recorded in
[RESULTS.md](RESULTS.md), 14 answers were refused for problems the checker
cannot see at all — sections named after the file itself, or after a heading
another file had already used — and every one of them would otherwise have
passed.

So the loop is: write the idea, generate, read what came back, fix the idea,
generate again. The check catches the cases not worth your attention so that
your attention goes to the ones that are.

## Running it again

The plan is deterministic. The same idea and preset produce the same file list
and the same plan id, on any machine, with nothing installed but Python and
`pyyaml`. That is what makes this usable as a starting point rather than a
one-off: you can change three lines of your idea, re-plan, and see exactly what
moved.

Nothing is written to disk unless you ask for it. `plan` prints. `demo` judges a
proposal without creating it. `check` reads a folder you already have and does
not touch it. There is no state to corrupt while you are still deciding.

## The examples, and what each one is showing

They are ordinary requests, deliberately spread across different kinds of work.
Each live-run example demonstrates a different kind of condition — that is why
there are ten rather than one. The separate `idea.md` is an English sample, not
an eleventh live run.

| example | preset | the kind of condition it shows |
|---|---|---|
| `local_docs_search.md` | software | Refusing to answer without a source |
| `codebase_assistant.md` | software | Requiring provenance on every output |
| `youtube_digest.md` | automation | Resuming without repeating work |
| `image_translation.md` | automation | Marking failure instead of hiding it |
| `meeting_notes.md` | automation | Leaving a blank when unsure, on purpose |
| `webnovel_translation.md` | content | Recording a change to a decision already made |
| `persona_chatbot.md` | content | Keeping settings separate from history |
| `local_model_bench.md` | research | Fixing what must match before comparing |
| `prompt_ab_test.md` | research | Writing the criteria before seeing results |
| `workflow_audit.md` | general | Admitting only observations as candidates |
| `idea.md` | software | The same structure from an English input |

Copy the one closest to your problem, replace the content, keep the shape.

## Starting your own

```
cp examples/local_docs_search.md my_idea.md
# edit it
python synapse_mini.py plan my_idea.md --preset software
```

Presets: `general`, `software`, `research`, `content`, `automation`. If you are
unsure, `general` is the smallest and adds the fewest files.
