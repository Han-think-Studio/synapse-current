[English](RESULTS.md) · [한국어](RESULTS.ko.md) · **中文**

# 实机运行结果

十个想法，一个本地模型，一台机器，一天。每一行都是按顺序跑完下面三步，并记录每一步返回了什么。

- 日期：2026-09-15
- 模型：`nvidia-nemotron-3.5-lightning-30b-a3b`
- 接口：一个兼容 OpenAI 的本地服务
- 10 次运行，125 个文件，模型写出 119 KB 文字
- **READY 10 次，BLOCKED 0 次**
- 被检查器拒绝的回答 39 次，其中在后续尝试中恢复 39 次

## plan → fill → check

| 想法 | 预设 | 计划 | 写出 | 判定 | 通过 | 模型输出 |
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

`计划`是模型介入之前由 Python 定下的。`写出`统计的是可用状态返回的文件数。
`模型输出`只统计模型写的字节 —— `00_intake/idea.md` 是原样复制的，清单由计划渲染，两者都不计入。

## 结构拦住了什么

**一个成功率数字会盖掉的，正是这一节。** 下面每一次拒绝都由
`fill_with_local_model.py` 里的检查器算出，没有一条来自模型对自己的说法；送进下一次尝试
的理由，也由那个检查器来写，**而且每次写法相同。**

| 想法 | 文件 | 检查器拒绝的原因 | 尝试 | 恢复 |
|---|---|---|---:|---|
| `local_docs_search` | `README.md` | 正文里有标题行 | 2 | 是 |
| `local_docs_search` | `02_model/relations.md` | 正文里有标题行 | 2 | 是 |
| `codebase_assistant` | `04_work/next_steps.md` | 正文里有标题行 | 2 | 是 |
| `codebase_assistant` | `99_review/open_questions.md` | 正文里有标题行 | 2 | 是 |
| `youtube_digest` | `03_rules/invariants.md` | 正文里有标题行 | 2 | 是 |
| `youtube_digest` | `99_review/open_questions.md` | 正文里有标题行 | 2 | 是 |
| `youtube_digest` | `03_rules/safety.md` | 正文里有标题行 | 3 | 是 |
| `youtube_digest` | `03_rules/safety.md` | 重复文件自己的标题 | 3 | 是 |
| `youtube_digest` | `04_work/runs.md` | 正文里有标题行 | 3 | 是 |
| `youtube_digest` | `04_work/runs.md` | 重复文件自己的标题 | 3 | 是 |
| `image_translation` | `README.md` | 重复文件自己的标题 | 2 | 是 |
| `image_translation` | `01_context/goals.md` | 重复文件自己的标题 | 2 | 是 |
| `image_translation` | `02_model/entities.md` | 正文里有标题行 | 2 | 是 |
| `image_translation` | `04_work/runs.md` | 正文里有标题行 | 2 | 是 |
| `meeting_notes` | `README.md` | 正文里有标题行 | 2 | 是 |
| `meeting_notes` | `01_context/goals.md` | 正文里有标题行 | 2 | 是 |
| `meeting_notes` | `03_rules/safety.md` | 正文里有标题行 | 2 | 是 |
| `meeting_notes` | `04_work/runs.md` | 重复文件自己的标题 | 2 | 是 |
| `webnovel_translation` | `README.md` | 正文里有标题行 | 2 | 是 |
| `webnovel_translation` | `01_context/constraints.md` | 重复文件自己的标题 | 2 | 是 |
| `webnovel_translation` | `02_model/relations.md` | 重复文件自己的标题 | 2 | 是 |
| `webnovel_translation` | `02_model/subjects.md` | 重复文件自己的标题 | 2 | 是 |
| `webnovel_translation` | `04_work/outline.md` | 重复文件自己的标题 | 2 | 是 |
| `persona_chatbot` | `04_work/outline.md` | 正文里有标题行 | 2 | 是 |
| `local_model_bench` | `README.md` | 正文里有标题行 | 2 | 是 |
| `local_model_bench` | `01_context/constraints.md` | 正文里有标题行 | 2 | 是 |
| `local_model_bench` | `02_model/relations.md` | 正文里有标题行 | 2 | 是 |
| `local_model_bench` | `02_model/sources.md` | 正文里有标题行 | 2 | 是 |
| `local_model_bench` | `04_work/experiments.md` | 正文里有标题行 | 2 | 是 |
| `prompt_ab_test` | `README.md` | 正文里有标题行 | 2 | 是 |
| `prompt_ab_test` | `01_context/goals.md` | 重复文件自己的标题 | 2 | 是 |
| `prompt_ab_test` | `01_context/constraints.md` | 正文里有标题行 | 2 | 是 |
| `prompt_ab_test` | `02_model/entities.md` | 正文里有标题行 | 2 | 是 |
| `prompt_ab_test` | `02_model/sources.md` | 正文里有标题行 | 2 | 是 |
| `prompt_ab_test` | `03_rules/method.md` | 正文里有标题行 | 4 | 是 |
| `prompt_ab_test` | `03_rules/method.md` | 用了别的文件的标题 | 4 | 是 |
| `prompt_ab_test` | `03_rules/method.md` | 用了别的文件的标题 | 4 | 是 |
| `workflow_audit` | `99_review/open_questions.md` | 重复前面的标题 | 3 | 是 |
| `workflow_audit` | `99_review/open_questions.md` | 重复前面的标题 | 3 | 是 |

**不同的拒绝需要不同的应对，而它们确实得到了不同的应对。** 被截断的回答，是请求本身没问题
而上限太低，于是预算翻倍、措辞不动。正文里带着自己标题的回答，是对一个明确请求给了错误的
答案，于是下一次尝试把检查器查明的事实写进去，**并且保留此前收到的每一条理由。**

最后这一点很关键：**一个在学会下一条规则时忘掉上一条的请求，会在两者之间来回打转。**

## 复现

```
python fill_with_local_model.py --idea examples/local_docs_search.md \
    --preset software --model 你的模型ID --out fill.json --receipt receipt.json
python synapse_mini.py demo --idea examples/local_docs_search.md \
    --preset software --content-json fill.json
```

`plan` 是确定性的：同样的想法和预设，在任何地方都给出同样的文件清单和同样的计划 id。
**填充不是确定性的** —— 换个模型，甚至同一个模型跑两次，都会写出不同的文字，也可能因不同的
理由被拒绝。是哪一种，回执里有记录。

## READY 度量的是什么

READY 表示每个必需文件的每个标题下面都有写下的内容。**它是一项结构性结果，而把它的边界说
清楚，正是它能被使用的原因。**

**不意味着真实。** 上面被拒绝的 39 次回答里，**有 14 次属于检查器根本看不见
的那一类** —— 用文件自己的名字当小节标题，或者用了别的文件已经用过的标题。它们既不空也不是
模板，所以**每一个都会成为一个 READY 文件**。是更靠前的一层抓住了它们：一个知道自己请求过
什么的脚本。检查器没有错，它回答的正是它被问到的那个问题。

**不意味着领域质量。** 这里不判断内容在它所属领域里好不好。律师、编辑和安全评审各自要做
的事一件都没少；**这套东西的目的不是取代他们，而是把一份完整到可供评审的材料交到他们手上。**

**不意味着许可。** PASS 不授予任何「可以应用」的权限。提案和已确认状态在这里是分开的，
**为的是由人来决定哪一个变成哪一个。**

上面那张表就是这三点的证据 —— 这也是它按原始数字发布、而不是被概括掉的原因。
