# achord-review

[中文](README.zh-CN.md) · **English**

A self-hosted AI pull-request reviewer that runs as a single GitHub App across every
repository you install it on, and answers with **one review**: a summary, findings
anchored to the offending lines, and a formal Approve / Request changes verdict.

Built on [PR-Agent](https://github.com/qodo-ai/pr-agent) (MIT), tuned for one job —
reviewing code — and for one delivery shape: one review, one notification.

---

## What a review looks like

On a pull request with a real bug, the bot leaves a single review:

> **PR Reviewer Guide** 🔍
>
> 🧪 **No relevant tests** · 🔒 **No security concerns identified**
>
> **Verdict:** blocking issues found (P1).

…with the findings attached to the lines that caused them:

> <sub>![P1](https://img.shields.io/badge/P1-orange?style=flat)</sub> **Append the final chunk before returning**
>
> The remaining `current` chunk is never added to `chunks`. Any non-empty input therefore
> loses its trailing lines; when the patch never exceeds the budget, the function returns
> an empty list and drops the entire patch.

and the pull request is marked **Changes requested**.

When nothing is wrong, it says so and approves:

> ✅ No blocking issues found in this diff. Looks good to merge.

Mention `@achord-review` on a pull request and it reacts 👀, then reviews again.

---

## How it differs from upstream PR-Agent

Every change below is **off by default** — the stock behaviour is untouched unless you
switch it on in configuration.

| | |
|---|---|
| **One review, one notification** | Upstream posts the summary, the inline comments and the verdict as three separate GitHub events, so a single review sends three emails. `single_review_submission` puts all three into one `create_review` call. |
| **A formal verdict** | `enable_review_verdict` turns a review into **Approve** / **Request changes** / **Comment**, decided by severity and security findings rather than by a human reading the summary. |
| **P0–P3 severities** | Findings carry an explicit severity with a colour chip, and an imperative title ("Append the final chunk before returning") instead of a generic label ("Possible Bug"). P0/P1 block the merge; which levels block is configurable. |
| **Findings on the code** | `inline_key_issues` anchors each finding to its line. Implemented for GitHub here — upstream only had the read-back hooks this needs on Azure DevOps. |
| **Never reviews a commit twice** | The verdict carries the reviewed commit SHA. An automatic re-run on unchanged code stays silent; a person asking always gets an answer. |
| **`@mention` trigger** | `mention_trigger` maps `@your-bot review` onto `/review`. |
| **Fork PRs skipped** | Fork contents are attacker-controlled and the process holds a model API key, so `skip_fork_prs` declines them by default. |
| **No placeholder comment** | The "Preparing review..." comment is deleted once the review lands, but GitHub has already emailed it. The 👀 reaction says the same thing for free. |

Everything else — providers, prompt building, compression, the other tools — is upstream
PR-Agent. See [its documentation](https://docs.pr-agent.ai/) for those.

---

## Deploying

The full runbook is in [`deploy/achord-review/README.md`](deploy/achord-review/README.md):
GitHub App creation with the least-privilege permission set, nginx and certificates,
`docker compose`, and how to verify it end to end.

```bash
cd deploy/achord-review
cp config.toml.example config.toml && chmod 600 config.toml
$EDITOR config.toml          # model, relay, App credentials
docker compose up -d --build
```

`config.toml` holds every secret, is gitignored, and is mounted read-only into the
container. Nothing sensitive enters the image or the repository.

Any OpenAI-compatible endpoint works. This deployment points at a self-hosted relay;
`config.toml.example` documents the two settings that are easy to get wrong — the token
ceiling that silently caps a large context window, and the reasoning-effort passthrough.

---

## Rolling back

Setting `enable_review_verdict = false`, `single_review_submission = false`,
`mention_trigger = ""` and `skip_fork_prs = false` restores stock PR-Agent behaviour
without touching code.

---

## Development

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/unittest -q     # full unit suite
./.venv/bin/ruff check pr_agent/                      # line-length 120
```

Python ≥ 3.12. See [`CLAUDE.md`](CLAUDE.md) for how prompts, settings and providers fit
together, and [`AGENTS.md`](AGENTS.md) for repository conventions.

---

## Credits and licence

achord-review is a derivative of **[PR-Agent](https://github.com/qodo-ai/pr-agent)** by
Qodo, used under the MIT licence. The upstream project does the hard parts: provider
adapters, diff compression, prompt construction, token budgeting. This repository adds a
review-delivery layer on top and is maintained independently — it is not affiliated with
or endorsed by Qodo, and issues here should not be reported upstream.

Upstream's own README is preserved at [`docs/README.upstream.md`](docs/README.upstream.md).

Licensed under the [MIT Licence](LICENSE); the original copyright notice is retained.
