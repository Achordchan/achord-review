# Achord Review — 基于 PR-Agent 的部署与改造方案（V1）

> 目标：一个 GitHub App 覆盖全部仓库，以独立 `achord-review[bot]` 身份自动审查 PR，
> 调用自有中转 `gpt-5.6-sol`，尽量复刻 Codex 的 Review / 👀 / 复审 / APPROVE·REQUEST_CHANGES 体验。
>
> 底座：**PR-Agent**（`qodo-ai/pr-agent`，MIT 许可证，可自由改用商用）。

---

## 0. 结论速览（TL;DR）

- **项目无硬伤**：MIT 许可证；中央 GitHub App、Webhook 签名校验、Inline 评论、增量复审、👀 表情、
  push 后取消旧任务——**全部已内置**。
- 我们只做三件事：**① 配置**（接中转 + 装 App，0 代码）→ **② 两处小改**（打回结论、触发词）→ **③ 调 Prompt**（P0–P3 + 去噪）。
- 预计工时：**约 3–4 天**到能用的 V1（对比从零 1–2 周）。

---

## 1. 需求 → PR-Agent 映射

| PRD 要求 | 状态 | 说明 |
|---|---|---|
| 一个 App 覆盖全部仓库，不用每仓库配 workflow | ✅ 内置 | `pr_agent/servers/github_app.py` 中央服务 |
| 独立 `[bot]` 身份 | ✅ 内置 | GitHub App 身份；需把 `github_app.bot_user` 改成我们的 App 名 |
| 走自有中转 + `gpt-5.6-sol` | ✅ 配置 | litellm `openai/` 前缀 + `OPENAI__API_BASE` |
| PR opened/reopened/ready_for_review 自动审 | ✅ 内置 | `handle_pr_actions = ['opened','reopened','ready_for_review']` |
| Draft PR 跳过 | ✅ 内置 | `feedback_on_draft_pr = false` |
| push 后复审 + 取消旧任务只审最新 | ✅ 内置 | 需开 `handle_push_trigger=true`；去重逻辑已在 |
| `@achord-review review` 手动召回 | 🔧 小改 | 默认只认 `/review`，需加 mention 解析 |
| 手动召回先加 👀 | ✅ 内置 | `add_eyes_reaction()` |
| Inline 行内评论 | ✅ 内置 | 含行号→diff 位置映射（最难的部分，已解决） |
| **P0/P1 → REQUEST_CHANGES；无阻断 → APPROVE；仅建议 → COMMENT** | 🔧 小改 | 模型已输出风险字段，缺"映射成正式结论"的一层 |
| 去噪（不评格式/风格/formatter 能处理的） | 🔧 调 Prompt | 写进 review prompt |
| 忽略 lock/dist/build/min/generated | ✅ 配置 | `pr_agent/settings/ignore.toml` |
| 只审当前 PR 引入的问题 | ✅/🔧 | 默认只送 diff；P0–P3 定义写进 prompt |
| 失败重试、并发单任务 | ✅ 内置 | litellm 重试 + push 去重 |

**要动的地方一共就 3 处：`bot_user`、打回结论、触发词；外加一份 prompt 与若干配置。**

---

## 2. 部署步骤（VPS：Debian / Ubuntu）

### 前置准备（已实测：95.169.2.68）
- 系统 Ubuntu 22.04.5，3 vCPU / 2 GB RAM / 39 G 磁盘（已用 56%，余 17 G）；Docker + containerd 已装并在跑。
- **端口现状**：`80` `8443` = nginx；`1234` `2096` = 3x-ui 面板；`1443` = xray；`22` = ssh。
  **`443` 空闲** —— 留给 achord-review。
- **已有 certbot + `certbot.timer`**（每日两次、运行正常），webroot 为 `/var/www/letsencrypt`，
  并有 `renewal-hooks/deploy/reload-nginx`。→ **证书直接复用这套，不要再引入 Caddy/acme.sh。**
- 需要一条解析到该 IP 的域名，例如 `review.achord.cn`（Webhook 需要公网 HTTPS）。

### Step 1 — 创建 GitHub App
在 GitHub → Settings → Developer settings → **GitHub Apps → New GitHub App**：

- **Name**：`achord-review`（最终 bot 显示为 `achord-review[bot]`）
- **Webhook URL**：`https://review.achord.cn/api/v1/github_webhooks`
  （PR-Agent 的 github_app 路由，见 `github_app.py`）
- **Webhook secret**：随机生成一串，记下来（下面 `GITHUB__WEBHOOK_SECRET`）
- **权限（Permissions）** — 严格按 PRD 第 4 节，最小权限：

  | 权限 | 值 |
  |---|---|
  | Contents | Read-only |
  | Pull requests | Read & Write |
  | Issues | Read & Write |
  | Metadata | Read-only |

  ❌ **明确不给**：Contents Write、Actions、Administration、Deployments、Secrets、Workflows

- **订阅事件（Subscribe to events）**：`Pull request`、`Issue comment`、（可选）`Pull request review comment`
- 创建后：记下 **App ID**；点 **Generate a private key** 下载 `.pem`。
- **Install App** → 装到你的账号/组织 → 选 **All repositories**。

### Step 2 — 准备密钥与配置（密钥只进不提交的文件）
在 VPS 项目目录建 `.secrets`（**加进 `.gitignore`，绝不提交**）。推荐用环境变量文件 `pr-agent.env`：

```env
# ---- 中转模型 ----
CONFIG__MODEL=openai/gpt-5.6-sol
CONFIG__FALLBACK_MODELS=["openai/gpt-5.6-sol"]
CONFIG__CUSTOM_MODEL_MAX_TOKENS=1000000       # gpt-5.6-sol 真实上下文 1M（已确认）
CONFIG__MAX_MODEL_TOKENS=200000               # 全局硬顶，默认 32000 会把上面 1M 压回 32k！
OPENAI__API_BASE=https://sub.achord.cn:8443/v1
OPENAI__KEY=sk-********（你的中转 key，只放这里）

# ---- GitHub App ----
GITHUB__DEPLOYMENT_TYPE=app
GITHUB__APP_ID=你的AppID
GITHUB__WEBHOOK_SECRET=你的WebhookSecret
# 私钥：把 .pem 内容作为多行环境变量，或挂载文件后用 GITHUB__PRIVATE_KEY 指向
GITHUB__PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n....\n-----END RSA PRIVATE KEY-----\n"
```

> 说明：PR-Agent 用**双下划线**表示嵌套配置（`[openai] key` → `OPENAI__KEY`）。
> `openai/` 前缀是告诉 litellm "走 OpenAI 兼容协议访问自定义 base_url"。
> `gpt-5.6-sol` 不在 litellm 的内置模型表里，所以**必须**设 `CUSTOM_MODEL_MAX_TOKENS`。

### Step 3 — 起服务（Docker）+ 复用现有 nginx / certbot

> ⚠️ **不要用 Caddy。** 这台机器上 nginx 已经占用 `80`，Caddy 再去绑 `80/443` 会直接启动失败。
> 正确做法：容器只监听 `127.0.0.1`，由现有 nginx 反代 + 现有 certbot 签证书（和 `sub.achord.cn` 完全同一套）。

PR-Agent 的 github_app 镜像入口（见 `docker/Dockerfile` 的 `github_app` 目标）：
```
gunicorn -k uvicorn.workers.UvicornWorker -c pr_agent/servers/gunicorn_config.py \
  --forwarded-allow-ips '*' pr_agent.servers.github_app:app
```

`docker-compose.yml`（只开本地端口，不碰 80/443）：
```yaml
services:
  pr-agent:
    build:
      context: .
      dockerfile: docker/Dockerfile
      target: github_app
    env_file: ./pr-agent.env
    restart: unless-stopped
    ports: ["127.0.0.1:33001:3000"]   # 只绑 loopback，由 nginx 反代
```

nginx 站点 `/etc/nginx/sites-available/achord-review.conf`（照抄现有 sub.achord.cn 的写法）：
```nginx
server {
    listen 80;
    listen [::]:80;
    server_name review.achord.cn;
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; default_type text/plain; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name review.achord.cn;

    ssl_certificate     /etc/letsencrypt/live/review.achord.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/review.achord.cn/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 32m;
    proxy_read_timeout 600s;      # AI 审查可能跑几十秒
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://127.0.0.1:33001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

签证书（走已在运行的 certbot，自动续期由 `certbot.timer` 负责，无需额外 cron）：
```bash
ln -s /etc/nginx/sites-available/achord-review.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot certonly --webroot -w /var/www/letsencrypt -d review.achord.cn \
  --cert-name review.achord.cn --key-type ecdsa --non-interactive --agree-tos
systemctl reload nginx
```

启动：`docker compose up -d --build`

### Step 4 — 验证
1. GitHub App → Advanced → **Recent Deliveries**，看 webhook 是否 200。
2. 在任一已安装仓库开一个测试 PR → 应自动出现 `achord-review[bot]` 的 Review。
3. 在 PR 里评论 `@achord-review review` → 该评论先出现 👀，随后复审。

---

## 3. 代码改动（已实现，分支 `feature/achord-review`）

三处改动，全部**默认关闭**，不改变 PR-Agent 原有行为；靠 `deploy/achord-review/config.toml` 打开。

### 改动 ① — 正式结论 APPROVE / REQUEST_CHANGES / COMMENT
- `pr_reviewer_prompts.toml`：给每条 issue 增加 `severity` 字段（P0–P3 定义写在 Field 描述里），
  用 `{%- if require_severity %}` 包住，关闭时模板输出与原来逐字节相同。
- `git_provider.py` / `github_provider.py`：新增 `submit_review_verdict(event, body)`。
  GitHub 侧复用现成的 `create_review`，并补上 GitHub 的硬性要求——REQUEST_CHANGES 和 COMMENT 必须带 body。
- `pr_reviewer.py`：新增 `_determine_review_verdict()`（纯映射，易测）与 `_submit_review_verdict()`。
  判定顺序：`security_concerns` 非 "No" → REQUEST_CHANGES；存在 P0/P1 → REQUEST_CHANGES；
  仅有其它 issue → COMMENT；无 issue → APPROVE。**任何解析异常都退回 COMMENT，绝不阻断 PR**（PRD 第 8 节）。

### 改动 ② — 触发词 `@achord-review review`
`github_app.py` 新增 `normalize_mention_command()`，在命令分发前把 mention 规整成斜杠命令：
`@achord-review review` → `/review`；裸 mention → `mention_default_command`；
`@achord-review ask ...` → `/ask ...`。👀 表情是 PR-Agent 原有逻辑，自动生效。

### 改动 ③ — 跳过 fork PR（PRD 第 7 节要求，PR-Agent 无内置开关）
`github_app.py` 新增 `is_fork_pr()`，在 `_perform_auto_commands_github()` 里紧挨 draft 判断处拦截。
只拦**自动**触发；维护者仍可手动 @ 机器人审查 fork PR。head.repo 缺失（fork 已删除）时按 fork 处理。

### 测试
新增 49 个用例（`test_review_verdict / test_mention_trigger / test_fork_pr_gate` 系列三个文件），覆盖结论映射、配置开关、
大小写与畸形数据容错、mention 解析、fork 判定，以及 prompt 在开/关两种状态下的渲染。
**完整单测 2643 passed**，Ruff 相对改动前**零新增告警**。

## 4. Prompt 调优（P0–P3 + 去噪）
**文件**：`pr_agent/settings/pr_reviewer_prompts.toml`

写入（对齐 PRD 第 5 节）：
- **分级定义**：
  - P0 严重安全/权限绕过/Secret 泄露/不可逆数据损坏 → 必须 REQUEST_CHANGES
  - P1 明确功能 Bug/回归/严重逻辑错误/数据不一致/竞态/API 破坏 → 默认 REQUEST_CHANGES
  - P2 重要边界/异常缺失/明显性能退化/重要测试缺失 → 通常 COMMENT
  - P3 有明确收益的小维护 → 仅评论
- **去噪规则**：不评论纯格式、个人风格、无实际影响的重构；Formatter/Linter 能处理的默认忽略。
- **范围**：只审当前 PR 引入/暴露的问题；可读 README/CONTRIBUTING/AGENTS.md，但禁止审整个历史仓库。
- **输出**：每条含 Severity / File / Location / Problem / Impact / Suggested Fix；无问题时明确
  返回 "No blocking issues found"。

配套配置（`configuration.toml` 或用 env 覆盖）：
- `pr_reviewer.num_max_findings`：默认 3，按需调
- 关掉噪音项：`require_ticket_analysis_review=false`（无 issue 关联时）、
  `require_can_be_split_review=false` 等，按实际保留有用维度。

---

## 5. 配置改动速查表

| 配置 / 文件 | 默认 | 改成 | 目的 |
|---|---|---|---|
| `config.model` | `gpt-5.6` | `openai/gpt-5.6-sol` | 走中转 |
| `config.fallback_models` | `["gpt-5.6-terra"]` | `["openai/gpt-5.6-sol"]` | 备用同模型 |
| `config.custom_model_max_tokens` | `-1` | `1000000` | 自定义模型必填；gpt-5.6-sol 上下文 1M |
| `config.max_model_tokens` | `32000` | `200000`（建议） | **全局硬顶**，`get_max_tokens()` 取 `min(两者)`；不改的话 1M 白设 |
| `config.max_output_tokens` | `0`（用服务端默认） | 保持 `0` | 模型输出上限 128k，审查结论很短，无需设 |
| `openai.api_base` | 空 | `https://sub.achord.cn:8443/v1` | 中转地址 |
| `github.deployment_type` | `user` | `app` | 以 App 身份运行 |
| `github_app.bot_user` | `github-actions[bot]` | `achord-review[bot]` | 识别自身评论 |
| `github_app.pr_commands` | `/describe,/review,/improve` | 仅 `/review` | V1 只审查 |
| `github_app.handle_push_trigger` | `false` | `true` | 开启 push 复审 |
| `github_app.push_commands` | `/describe,/review` | 仅 `/review` | push 只复审 |
| `github_app.feedback_on_draft_pr` | `false` | 保持 `false` | Draft 跳过 |
| `ignore.toml` glob/regex | 少量 | 加 lock/dist/build/*.min.*/generated | 跳过噪音文件 |

> 覆盖方式：可直接改 `pr_agent/settings/configuration.toml`，或用环境变量
> （如 `GITHUB_APP__HANDLE_PUSH_TRIGGER=true`）覆盖，避免动源码便于日后 upstream 更新。

---

## 6. 安全 Checklist（对齐 PRD 第 7 节）
- [ ] 中转 Key / GitHub 私钥 / Installation Token **只在服务器 secret**，不入代码、日志、PR 评论、前端。
- [ ] `.secrets` / `pr-agent.env` / `*.pem` 已进 `.gitignore`。
- [ ] App 无 Contents Write —— Bot 无法改/推代码。
- [ ] 外部 Fork PR：V1 默认不对 fork PR 跑带 secret 的 AI 审查（防凭据泄露）。
      → 在 `handle_pr_actions` 流程里加 fork 判断跳过（待确认 PR-Agent 现有行为，作为收尾项）。
- [ ] V1 不设为 Branch Protection Required Check，先跑稳、验证误报率。

---

## 7. 里程碑

| 阶段 | 内容 | 预计 |
|---|---|---|
| M1 | 建 App + Docker 跑通，能自动出 Review（默认 prompt） | 0.5–1 天 |
| M2 | 改动①打回结论 + 改动②触发词 + bot_user | 1–1.5 天 |
| M3 | Prompt 调 P0–P3 + 去噪 + ignore + 关闭多余功能 | 1 天 |
| M4 | fork PR 处理 + 失败兜底核对 + 3 仓库验收（对齐 PRD 第 10 节 A1–A8） | 0.5 天 |

---

## 8. 收尾项状态

1. ~~VPS 与域名~~ ✅ `95.169.2.68` + `review.achord.cn`（解析已生效）
2. ~~上下文长度~~ ✅ 1M 上下文 / 128k 输出 → `custom_model_max_tokens=1000000` + `max_model_tokens=200000`
3. ~~Fork PR 策略~~ ✅ 自动跳过（`skip_fork_prs=true`），维护者手动 @ 仍可审
4. ~~/describe、/improve~~ ✅ 关闭，`pr_commands=["/review"]`

**仅剩需要你在 GitHub 网页上操作的一步**：创建 GitHub App 并安装到 All repositories，
拿到 App ID、Webhook Secret 和私钥 `.pem`。详细步骤见 `deploy/achord-review/README.md`。
