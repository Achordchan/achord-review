from pathlib import Path


def test_achord_review_runtime_secrets_are_excluded_from_docker_context():
    dockerignore = Path(__file__).resolve().parents[2] / ".dockerignore"
    patterns = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        "**/.secrets.toml*",
        "deploy/achord-review/config/",
        "deploy/achord-review/config.toml",
        "deploy/achord-review/config.toml.bak*",
        "deploy/achord-review/data/",
    }.issubset(patterns)
