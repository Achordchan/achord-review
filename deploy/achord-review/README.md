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
| Webhook secret | generate a random string; keep it for `config/.secrets.toml` |

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
mkdir -p config
cp config.toml.example config/.secrets.toml
chmod 600 config/.secrets.toml
$EDITOR config/.secrets.toml   # fill in every REPLACE_WITH_* placeholder
```

`config/.secrets.toml` is gitignored and the directory holding it is mounted into the
container: the host `./config/` directory maps to `/app/pr_agent/settings_prod`,
and the container loads `/app/pr_agent/settings_prod/.secrets.toml`. The panel's
config editor backs up and atomically replaces the file inside that directory.
Secrets never enter the image or the repository.

Migrating from the earlier single-file layout (`./config.toml` on the host):

```bash
mkdir -p config && mv config.toml config/.secrets.toml && chmod 600 config/.secrets.toml
```

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

- Set the login password in `config/.secrets.toml` under `[dashboard] admin_password`
  (copy the section from `config.toml.example`). Leaving it empty disables the
  panel: every login attempt is rejected with 503.
- Login is rate-limited to 5 failures per IP per 15 minutes; the client IP is
  derived from the trusted proxy hops only (tune with `DASHBOARD_TRUSTED_PROXY_HOPS`,
  default 1 for the shipped nginx setup).
- Cookie-authenticated writes require the exact external browser origin. The
  shipped Compose file pins `DASHBOARD_EXTERNAL_ORIGIN=https://review.achord.cn`;
  custom domains must change this value together with nginx.
- Every review run is recorded to `./data/review.db`, which feeds the overview
  stats, the review history, and the per-run detail pages. Config saves and ops
  actions are written to the audit log. Active reviews refresh a durable
  heartbeat every 60 seconds; only a RUNNING row with no heartbeat for 6 hours
  is reconciled as interrupted.
- The config page edits the mounted `config/.secrets.toml` live: each save is validated,
  backed up (5 copies kept, comments preserved), applied to the running process, and —
  when a restart-requiring field changed — can trigger a restart. A restart happens one
  of two ways: through a controlled Docker endpoint when one is mounted, or, when
  `ACHORD_REVIEW_SELF_RESTART=1`, by a **socket-free self-exit** — the panel terminates
  the gunicorn master (PID 1) and the container's `restart: unless-stopped` policy
  respawns a fresh process. Either way the restart is queued as an after-response task,
  so the browser acknowledgment and audit row are emitted before the process stops.
- Some navigation entries are greyed out with a "Phase N" badge: those features
  are planned but not shipped yet; their API routes answer 501 COMING_SOON.

### Version panel & in-panel updates

- The version chip (top bar, and the sidebar) opens the **version & update** panel.
  It reports the running version — a single baked constant, `pr_agent/dashboard/version.py`
  `APP_VERSION` (currently `0.0.1`; bump it by one in the PR that ships each change and
  tag the merge `v<APP_VERSION>`) — and, when a checkout is mounted, compares it against
  the tracked remote so you can update and restart in place.
- **In-panel updates are opt-in**, because the shipped image bakes its code in rather than
  running from managed releases. To enable the sub2api-style "check → one-click update →
  restart" flow, uncomment the opt-in `command`, the source/releases/config mounts, and all
  six opt-in environment variables in `docker-compose.yml` (`ACHORD_REVIEW_CONFIG_DIR` is
  mandatory: staging is refused without a persistent config directory). The launcher creates an initial
  detached worktree under `./releases` and runs from its stable `current` symlink. **Check
  update** fetches and compares `origin/main`; **one-click update** prepares a second detached
  worktree without modifying the running release, and the panel then shows it as
  *prepared* rather than re-offering the same update; **restart** atomically redirects
  `current` at the moment the restart is initiated (rolled back if the restart cannot
  start), then self-exits so the fresh process imports the new release. The old process
  keeps absolute paths to its original Python and static files, so no request can combine a
  new frontend with the old backend. The browser polls the service back up and reloads
  automatically; the HttpOnly cookie and SQLite session survive.
- **The host always wins.** Each image build carries a fresh stamp; on the first boot of a
  rebuilt image the launcher runs the source checkout's `HEAD` (what the image was built
  from), discards any older pending release, and prunes superseded worktrees down to the
  active one plus one rollback. So `git pull --ff-only && docker compose up -d --build` on
  the host takes effect with no panel involvement, and a stale staged revision can never
  boot on a newer image. `./releases` is ignored by Git and by the Docker build context.
- **The one limit, surfaced honestly:** a restart only re-imports Python — it cannot
  install packages. When a pull changes `requirements.txt` / `pyproject.toml` / the
  Dockerfile or release configuration, the panel detects it and tells you to update
  the source checkout and rebuild on the host (`git pull --ff-only && docker compose
  up -d --build`) instead of implying a restart is enough.
- Left fully commented, the buttons stay disabled and releases remain host-managed
  (`git pull --ff-only` then `docker compose up -d --build`).
- **Migrating from in-place updates.** Earlier builds enabled the update button by
  setting only `ACHORD_REVIEW_REPO_DIR` on a mounted checkout and ran `git pull` into
  the serving directory. That mode is retired (it rewrote live Python and static files
  under a running process) and is not silently emulated: with only `REPO_DIR` set the
  panel reports the update as unavailable and names this section. To migrate, replace
  the checkout mount with the opt-in block in `docker-compose.yml` — the launcher
  `command`, the `../..:/app/source`, `./releases:/app/releases` and `./config:/app/config`
  mounts, and the six `ACHORD_REVIEW_*` variables (`REPO_DIR` now points at
  `/app/releases/current`) — then `docker compose up -d --build` once from the host. The
  first boot stages the checkout's `HEAD` as the initial release; nothing else changes.

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
