#!/bin/sh
set -eu

# Opt-in release launcher. All release bookkeeping lives in pr_agent.dashboard.ops
# (unit-tested) and runs from the image's own copy of the code before Gunicorn:
#   - first boot: stage the source checkout's HEAD as the initial worktree;
#   - a rebuilt image (fresh /app/.deps-baked/build-id): run the rebuilt source
#     HEAD and discard any older pending release, so a host
#     `git pull --ff-only && docker compose up -d --build` always takes effect;
#   - otherwise: activate a pending release whose dependencies match the image;
#   - then prune superseded worktrees beyond the rollback retention.
export ACHORD_REVIEW_SOURCE_DIR="${ACHORD_REVIEW_SOURCE_DIR:-/app/source}"
export ACHORD_REVIEW_RELEASES_DIR="${ACHORD_REVIEW_RELEASES_DIR:-/app/releases}"
export ACHORD_REVIEW_REPO_DIR="${ACHORD_REVIEW_REPO_DIR:-/app/releases/current}"
export ACHORD_REVIEW_CONFIG_DIR="${ACHORD_REVIEW_CONFIG_DIR:-/app/config}"

python -c 'from pr_agent.dashboard import ops; print("release:", ops.reconcile_boot_release())'

cd "$ACHORD_REVIEW_REPO_DIR"
exec python -m gunicorn -k uvicorn.workers.UvicornWorker \
    -c pr_agent/servers/gunicorn_config.py --forwarded-allow-ips '*' \
    pr_agent.servers.github_app:app
