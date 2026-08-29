# achord-review

**中文** · [English](README.md)

一个自建的 AI 代码审查机器人。以单个 GitHub App 的身份覆盖你安装它的所有仓库，
每次审查只回**一条评审**：一份摘要、标在出问题那几行上的发现，以及一个正式的
Approve / Request changes 结论。

基于 [PR-Agent](https://github.com/qodo-ai/pr-agent)（MIT 许可证）改造，
只做一件事——审代码；只用一种形态——一条评审、一次通知。

---

## 评审长什么样

PR 里有真 bug 时，机器人留下一条评审：

> **PR Reviewer Guide** 🔍
>
> 🧪 **No relevant tests** · 🔒 **No security concerns identified**
>
> **Verdict:** blocking issues found (P1).

发现直接挂在出问题的代码行上：

> <sub>![P1](https://img.shields.io/badge/P1-orange?style=flat)</sub> **Append the final chunk before returning**
>
> The remaining `current` chunk is never added to `chunks`. Any non-empty input therefore
> loses its trailing lines; when the patch never exceeds the budget, the function returns
> an empty list and drops the entire patch.

同时把 PR 标记为 **Changes requested**。

没问题的时候，它会明说，并批准：

> ✅ No blocking issues found in this diff. Looks good to merge.

在 PR 里 @ 它，会先回一个 👀 表示接手，然后重新审一遍。

---

## 和上游 PR-Agent 的区别

下面每一项**上游默认都是关的**，不开启就是原版行为。

| | |
|---|---|
| **一条评审，一次通知** | 上游把摘要、行内评论、结论拆成三个 GitHub 事件，一次审查发三封邮件。`single_review_submission` 让三者合并进同一个 `create_review` 调用。 |
| **正式结论** | `enable_review_verdict` 把评审变成 **Approve** / **Request changes** / **Comment**，由严重度和安全发现决定，而不是等人读完摘要自己判断。 |
| **P0–P3 严重度** | 每条发现带明确的严重度和彩色标签，标题用祈使句（"Append the final chunk before returning"）而不是笼统的 "Possible Bug"。P0/P1 阻断合并，具体哪几级阻断可配置。 |
| **发现标在代码上** | `inline_key_issues` 把每条发现锚定到具体行。GitHub 的实现是这个项目补的——上游只在 Azure DevOps 上有它依赖的读回钩子。 |
| **同一个 commit 不重审** | 结论里记录了被审的 commit SHA。代码没变时自动触发保持沉默；但人主动要求，一定给回应。 |
| **`@mention` 触发** | `mention_trigger` 把 `@你的机器人 review` 映射成 `/review`。 |
| **默认跳过 fork PR** | fork 的内容不可信，而进程持有模型 API key，所以 `skip_fork_prs` 默认拒审。 |
| **不发占位评论** | "Preparing review..." 会在评审落地后被删除，但 GitHub 早就把邮件发出去了。👀 表情能免费传达同样的意思。 |

其余部分——各平台适配、prompt 构建、diff 压缩、其他工具——都是上游 PR-Agent，
参见[它的文档](https://docs.pr-agent.ai/)。

---

## 部署

完整手册见 [`deploy/achord-review/README.md`](deploy/achord-review/README.md)：
按最小权限创建 GitHub App、nginx 与证书、`docker compose`、以及怎么端到端验证。

```bash
cd deploy/achord-review
cp config.toml.example config.toml && chmod 600 config.toml
$EDITOR config.toml          # 模型、中转、App 凭据
docker compose up -d --build
```

`config.toml` 装着全部密钥，已被 gitignore，以只读方式挂进容器。
**敏感信息既不进镜像，也不进仓库。**

任何 OpenAI 兼容的接口都能用。本部署指向自建中转；`config.toml.example` 里注明了
两个最容易踩的设置——会悄悄砍掉大上下文的 token 上限，以及推理强度的透传。

---

## 回滚

把 `enable_review_verdict = false`、`single_review_submission = false`、
`mention_trigger = ""`、`skip_fork_prs = false` 一改，就回到原版 PR-Agent 行为，
不需要动代码。

---

## 开发

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/unittest -q     # 全量单测
./.venv/bin/ruff check pr_agent/                      # line-length 120
```

需要 Python ≥ 3.12。[`CLAUDE.md`](CLAUDE.md) 说明 prompt、配置、provider 三者如何衔接，
[`AGENTS.md`](AGENTS.md) 是仓库约定。

---

## 致谢与许可

achord-review 是 **[PR-Agent](https://github.com/qodo-ai/pr-agent)**（Qodo 出品）的衍生项目，
依据 MIT 许可证使用。难啃的部分都是上游做的：各平台适配层、diff 压缩、prompt 构建、
token 预算。本仓库在其之上加了一层评审投递逻辑，独立维护——
**与 Qodo 无隶属关系，也未获其背书，本项目的问题请不要提到上游仓库去。**

上游原版 README 保留在 [`docs/README.upstream.md`](docs/README.upstream.md)。

依据 [MIT 许可证](LICENSE)授权，原始版权声明已保留。
