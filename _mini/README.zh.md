[English](README.md) · [한국어](README.ko.md) · **中文**

# Synapse mini

一个通往规范 Synapse 内核的小型本地入口。它是投影和启动器，不是第二套实现：
它**导入**规划器和实质内容检查器，而不是重新实现它们；它不调用模型，不改变
规范状态，也不会写出你没要求过的工作目录。

关于为什么这样设计，见[顶层 README](../README.zh.md)。

## 命令

生成计划。不给 `--out` 就什么都不写：

```
python synapse_mini.py plan examples/local_docs_search.md --preset software
```

只读地检查一个已有的工作目录：

```
python synapse_mini.py check path/to/workspace --idea examples/local_docs_search.md --preset software
```

在不写入的前提下判定一份「路径→内容」提案：

```
python synapse_mini.py demo --idea examples/local_docs_search.md --preset software --content-json examples/local_docs_search_fill.json
```

只有当每个必需文件都通过规范的 `has_substantive_workspace_content` 判定时，
`demo` 和 `check` 才会以 0 退出。**这是一个最低内容结果**，不是质量批准，也不
授予任何「可以应用」的权限。

## 内核从哪里来

这个文件夹位于仓库内部，所以默认来源是它的上级目录 —— 也就是装着 `synapse/`
的那个目录。公开构建会把内核一并放在旁边，解析方式完全相同。其它布局请传
`--source-root` 或设置 `SYNAPSE_SOURCE_ROOT`。

**这里故意不写死任何路径。** 写死的路径只对一台机器正确，而且会跟着这个文件夹
一路进入公开发布包。

## 示例想法

`examples/` 里有十个实测想法，覆盖全部五个预设，另有一个独立的英文示例。这样读者的第一个问题 ——
**把我的想法放进去会得到什么** —— 用一条命令就能回答，而不是靠读一段说明。

| 想法 | 预设 | 计划文件数 |
|---|---|---:|
| `local_docs_search.md` 本机文档检索 | software | 12 |
| `codebase_assistant.md` 向自己的代码库提问 | software | 12 |
| `idea.md`（英文） | software | 12 |
| `youtube_digest.md` 字幕摘要流水线 | automation | 13 |
| `image_translation.md` 图片内文字翻译 | automation | 13 |
| `meeting_notes.md` 录音转会议纪要与待办 | automation | 13 |
| `webnovel_translation.md` 连载翻译一致性 | content | 13 |
| `persona_chatbot.md` 人设不崩的对话机器人 | content | 13 |
| `local_model_bench.md` 本地模型对比记录 | research | 13 |
| `prompt_ab_test.md` 提示词改动的度量 | research | 13 |
| `workflow_audit.md` 挑出该自动化的事 | general | 10 |

它们是普通的需求而不是展示素材，而且**刻意不属于同一类项目**。结构要么是领域
中立的，要么一文不值；十个实测想法配五个预设，比一段声称这件事的文字便宜。其中
一个是英文、其余是韩文，理由相同。

它们还兼着第二份工。一个预设无非是一份文件清单，每个文件配一行职责说明；这么短
的清单**读起来句句在理，却无从判断**。让十个不同的实测想法穿过去，薄弱处就露出来
了：哪些职责说明模型每次都读成同一个意思，哪些会和邻居混淆，哪个文件是这个预设
缺的。这是对预设数据所能得到的最便宜的反馈，除了跑一遍之外不花什么。

实机运行的结果在 [RESULTS.md](examples/RESULTS.zh.md)。

## 用本地模型填充计划

`fill_with_local_model.py` 会请求一个兼容 OpenAI 接口的服务 —— LM Studio、
llama.cpp、vLLM，任何说这套协议的东西 —— 逐个写出计划中的文件，写下一份 JSON
提案，然后停下。

```
python fill_with_local_model.py --idea examples/local_docs_search.md --preset software --model 你的模型ID --out fill.json --receipt receipt.json
python synapse_mini.py demo --idea examples/local_docs_search.md --preset software --content-json fill.json
```

**请按顺序读这两条命令，它们被分开这件事本身就是重点。**

第一个脚本是允许出错的那一半。模型有时会回答错的问题、中途停下、或者返回一段
道歉而不是文档，所以这个脚本**不拥有任何判断**。它以 0 退出表示这次运行结束了，
而不是结果不错。

第二个是做判断的那一半，而且它用的是规范检查器本身，不是副本。空的小节、下面
什么都没有的标题、把模板职责原样抄回来 —— `demo` 会说 BLOCKED，并点出是哪些
文件。

有两件事被刻意挡在模型之外。`00_intake/idea.md` 原样复制，因为它的职责就是
**按写下的样子保存想法**，而让模型重打一遍刚交给它的文字是弄丢它的办法。项目
清单由计划渲染，模型只提供一行文字；那一行外面的结构不是语言问题。

请求里带着 JSON schema，而 **schema 干的活比提示词多**。用自然语言要小模型给出
「有实质内容的小节」，得到的是一篇关于小节的文章；在 body 字段上声明 `minLength`，
得到的就是小节。渲染器随后把它们变成**每个标题下面都有正文**的 Markdown，于是
检查器的要求是靠构造成立的，而不是靠模型表现良好。

schema 做不到的是**让里面那串字符有意义**。实机跑出的两次失败，都是模型往 body
字段里写了不是散文的东西，同时仍然返回了形状正确的合法 JSON。两者现在都在
**这个已被声明为不可信的脚本里**被拒绝，而不是在检查器里 —— 见 RESULTS.md。

只用标准库，所以发布包能跑的地方它都能跑。

## 怎么选模型

只有一条要求，而且**是关于服务端而不是模型的**：接口必须接受
`response_format: {"type": "json_schema", ...}` 并支持 `strict`。不接受的话，
每个请求都会以 HTTP 400 返回，这里的一切都跑不起来。

2026 年 9 月，在一台机器上实际量到的：

| 模型 | 做到了什么 |
|---|---|
| `nvidia-nemotron-3.5-lightning-30b-a3b`（MoE，激活 3B） | 全部十个示例：125 个文件，39 次拒绝，39 次恢复 |
| `qwen3.5-4b-uncensored-hauhaucs-aggressive` | 一个示例：13 个里写出 9 个，25 次拒绝，5 次恢复 |

同一个文件也交给了四个模型 —— 上面两个、一个 `glm-4.7-flash-uncensored-heretic-neo-code-imatrix-max`，以及一个 30B 推理蒸馏
模型 —— **四个都给出了可用的小节。**

**规模没有体现在第一次回答上，而是体现在恢复上。** 4B 模型也写出了属于自己文件的小节：
24 个标题全不相同，也没有跑到邻项的职责里去。但**当检查器拒绝其中一个时，它大多用不上
那条纠正**，最终有 4 个文件没有写出来。更大的那个模型，在它收到的每一次拒绝之后都恢复了。
**如果要在模型之间做选择，就按这个标准选。**

### 在怪罪模型之前

这个仓库里记录着 24 次被标为失败的模型运行。**18 次是 HTTP 400，5 次是三十秒的客户端
超时，剩下 1 次还是 400。没有一次是内容问题。**

在那张表里**三战三败的那个模型，后来毫无问题地写出了 125 个文件**；而另一个曾因那张表
被移除的模型，**用现在的请求再问一次，第一次尝试就答对了。**

本地回答可能要九十秒；这里默认超时是 300 秒，原因就在这里。
**在对一个模型下结论之前，先看 `finish_reason` 和回执。多数时候，变的是请求那一边。**
