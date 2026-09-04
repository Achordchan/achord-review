FROM python:3.12.14-slim AS base

RUN apt update && apt install --no-install-recommends -y git curl && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ADD pyproject.toml .
ADD requirements.txt .
# Bake the Dockerfile into the fingerprint dir too, at the same relative path the
# checkout uses (docker/Dockerfile), so a base-image/system-package change is
# detected as needing a rebuild.
ADD docker/Dockerfile /app/.deps-baked/docker/Dockerfile
ADD deploy/achord-review/docker-compose.yml /app/.deps-baked/deploy/achord-review/docker-compose.yml
ADD deploy/achord-review/run-staged-release.sh /app/.deps-baked/deploy/achord-review/run-staged-release.sh
ADD docs docs
# Keep a copy of the dependency-/build-defining files the image was built from, so
# the dashboard can compare them against a mounted checkout and refuse an in-place
# restart when a pulled update needs packages or a base image this image lacks.
RUN pip install --no-cache-dir . \
    && cp pyproject.toml requirements.txt /app/.deps-baked/ \
    && rm pyproject.toml requirements.txt
ENV PYTHONPATH=/app

FROM base AS github_app
ADD pr_agent pr_agent
# Per-build stamp for the release launcher: a stamp it has not seen means the
# image was rebuilt, so the rebuilt source checkout - not an older staged
# release - is what must run. Placed after the code so a cached, identical
# build keeps its stamp.
RUN python -c "import uuid; print(uuid.uuid4())" > /app/.deps-baked/build-id
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.github_app:app"]

FROM base AS bitbucket_app
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_app.py"]

FROM base AS bitbucket_server_webhook
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_server_webhook.py"]

FROM base AS github_polling
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/github_polling.py"]

FROM base AS gitlab_webhook
ADD pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.gitlab_webhook:app"]

FROM base AS azure_devops_webhook
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/azuredevops_server_webhook.py"]

FROM base AS gitea_app
ADD pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py","pr_agent.servers.gitea_app:app"]

FROM base AS mosaico_agent
ADD pr_agent pr_agent
EXPOSE 9000
CMD ["python", "pr_agent/mosaico/server.py"]


FROM base AS test
ADD requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt && rm requirements-dev.txt
ADD pr_agent pr_agent
ADD tests tests
ADD docker/mosaico docker/mosaico
ADD .dockerignore .dockerignore

FROM base AS cli
ADD pr_agent pr_agent
ENTRYPOINT ["python", "pr_agent/cli.py"]
