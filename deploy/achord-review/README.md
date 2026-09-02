# achord-review — deployment

A single GitHub App that reviews pull requests across all installed repositories as
`achord-review[bot]`, using the self-hosted `gpt-5.6-sol` relay.

Target host: `95.169.2.68` (Ubuntu 22.04) · webhook endpoint: `https://review.achord.cn`

The host already runs nginx (ports 80 / 8443) and certbot with an active `certbot.timer`.
This deployment reuses both — it does **not** introduce Caddy or acme.sh, and it does not
touch ports 80 or 443 from Docker.

## 1. Create the GitHub App

Settings → Developer settings → GitHub Apps → **New GitHub App**

| Field | Value |
|---|---|
| Name | `achord-review` (the bot appears as `achord-review[bot]`) |
| Webhook URL | `https://review.achord.cn/api/v1/github_webhooks` |
| Webhook secret | generate a random string; keep it for `config.toml` |

Permissions (least privilege):

| Permission | Access |
|---|---|
| Contents | Read-only |
| Pull requests | Read & write |
| Issues | Read & write |
| Metadata | Read-only |

Do **not** grant Contents write, Actions, Administration, Deployments, Secrets or Workflows —
the bot must not be able to modify or push code.

Subscribe to events: **Pull request**, **Issue comment**.

Then generate a private key (`.pem`, downloaded once), note the **App ID**, and
**Install App → All repositories**.

## 2. Configure

```bash
cp config.toml.example config.toml
chmod 600 config.toml
$EDITOR config.toml      # fill in every REPLACE_WITH_* placeholder
```

`config.toml` is gitignored and mounted read-only into the container at
`/app/pr_agent/settings_prod/.secrets.toml`, which `pr_agent/config_loader.py` loads.
Secrets never enter the image or the repository.

## 3. Set up nginx and the certificate

```bash
sudo cp nginx-achord-review.conf /etc/nginx/sites-available/achord-review.conf
sudo ln -s /etc/nginx/sites-available/achord-review.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot certonly --webroot -w /var/www/letsencrypt -d review.achord.cn \
  --cert-name review.achord.cn --key-type ecdsa --non-interactive --agree-tos
sudo systemctl reload nginx
```

Renewal is handled by the host's existing `certbot.timer`. Note that this host's root
crontab is not a reliable place for scheduled jobs — a past cleanup script wiped it and
left `/etc/crontab` immutable — which is why certificates are managed through systemd.

## 4. Run

```bash
docker compose up -d --build
docker compose logs -f achord-review
```

The SQLite database for the control panel lives in `./data/review.db` on the host
(created on first start); back it up like any other state.

## 5. Verify

1. GitHub App → Advanced → **Recent Deliveries**: the webhook should return 200.
2. Open a test PR in an installed repository → a review from `achord-review[bot]` appears,
   with a formal **Approve** / **Request changes** / **Comment** verdict.
3. Comment `@achord-review review` on that PR → the comment gets a 👀 reaction, then a re-review.
4. Push a new commit → nothing happens on its own; mentioning the bot is what asks for the
   re-review. Turn `github_app.handle_push_trigger` back on to review every push instead.

## 6. Control panel

The web panel is served by the same process at `https://review.achord.cn/dashboard`
(no extra container, port, or nginx server — the SPA is a static build inside the
image and the API routes share the webhook process).

- Set the login password in `config.toml` under `[dashboard] admin_password`
  (copy the section from `config.toml.example`). Leaving it empty disables the
  panel: every login attempt is rejected with 503.
- Login is rate-limited to 5 failures per IP per 15 minutes.
- Every review run is recorded to `./data/review.db`, which feeds the overview
  stats, the review history, and the per-run detail pages. Config saves and ops
  actions (restart, git pull) are written to the audit log.
- The config page edits the mounted `config.toml` live: each save is validated,
  backed up (5 copies kept), applied to the running process, and — when a
  restart-requiring field changed — can trigger a one-click container restart.
- Some navigation entries are greyed out with a "Phase N" badge: those features
  are planned but not shipped yet; their API routes answer 501 COMING_SOON.

## Behaviour summary

| Behaviour | Where it is configured |
|---|---|
| Review only (no `/describe`, no `/improve`) | `github_app.pr_commands` |
| Auto review on open / reopen / ready-for-review | `github_app.handle_pr_actions` |
| Re-review on push (off — a mention is the request) | `github_app.handle_push_trigger` |
| Draft PRs skipped | `github_app.feedback_on_draft_pr = false` |
| Fork PRs skipped automatically | `github_app.skip_fork_prs = true` |
| `@achord-review review` trigger | `github_app.mention_trigger` |
| No "Preparing review..." placeholder comment | `config.publish_output_progress = false` |
| P0/P1 → Request changes, P2/P3 → Comment, none → Approve | `pr_reviewer.enable_review_verdict` + `verdict_blocking_severities` |
| Severity definitions and noise rules | `pr_reviewer.extra_instructions` |

## Rollback

Every behaviour above is off by default upstream. Setting `enable_review_verdict = false`,
`mention_trigger = ""` and `skip_fork_prs = false` restores stock PR-Agent behaviour
without touching code.
