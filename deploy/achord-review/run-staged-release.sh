#!/bin/sh
set -eu

source_dir=${ACHORD_REVIEW_SOURCE_DIR:-/app/source}
releases_dir=${ACHORD_REVIEW_RELEASES_DIR:-/app/releases}
active_link=${ACHORD_REVIEW_REPO_DIR:-/app/releases/current}

mkdir -p "$releases_dir"
if [ ! -L "$active_link" ]; then
    revision=$(git -C "$source_dir" rev-parse HEAD)
    release_dir="$releases_dir/$revision"
    if [ ! -d "$release_dir" ]; then
        git -C "$source_dir" worktree add --detach "$release_dir" "$revision"
    fi
    if [ ! -e "$release_dir/pr_agent/settings_prod" ]; then
        ln -s /app/config "$release_dir/pr_agent/settings_prod"
    fi
    temporary_link="$active_link.$$.tmp"
    ln -s "$release_dir" "$temporary_link"
    mv -Tf "$temporary_link" "$active_link"
fi

# A dependency-changing update deliberately leaves its pending marker in place.
# After the operator rebuilds the image, the baked fingerprint now matches and
# the launcher can safely perform the same atomic activation before Gunicorn starts.
python -c 'from pr_agent.dashboard import ops
if ops._pending_release() and not ops.rebuild_required():
    ops._activate_pending_release()'

if [ ! -e "$active_link/pr_agent/settings_prod" ]; then
    ln -s /app/config "$active_link/pr_agent/settings_prod"
fi

cd "$active_link"
exec python -m gunicorn -k uvicorn.workers.UvicornWorker \
    -c pr_agent/servers/gunicorn_config.py --forwarded-allow-ips '*' \
    pr_agent.servers.github_app:app
