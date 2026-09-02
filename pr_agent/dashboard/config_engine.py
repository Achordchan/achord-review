"""Live read/write access to the deployment config.toml for the dashboard.

The deployment mounts its config at pr_agent/settings_prod/.secrets.toml (see
deploy/achord-review/docker-compose.yml), which the config loader already
merges into the global Dynaconf settings. This module edits that file directly:
values are validated, the file is atomically replaced after a timestamped
backup, and the in-memory Dynaconf object is updated in place so changes apply
without a container restart.

Every write keeps the rest of the file byte-identical: the file is parsed with
tomllib and re-dumped with targeted section updates, never regenerated from a
template.
"""

import glob
import os
import shutil
import tempfile
import tomllib
from typing import Any, Dict, List, Optional, Tuple

from pr_agent.log import get_logger

try:
    import tomli_w
except ImportError:  # pragma: no cover - tomli_w is an optional accelerator
    tomli_w = None

try:
    import tomlkit
except ImportError:  # pragma: no cover
    tomlkit = None

MAX_BACKUPS = 5

# Field registry: dashboard field name -> (table, key, validator, default).
# `secret=True` fields are masked in GET responses; an empty/None submission
# leaves the stored value untouched.
STRING_FIELDS = {
    "model": ("config", "model"),
    "reasoning_effort": ("config", "reasoning_effort"),
    "api_base": ("openai", "api_base"),
    "key": ("openai", "key"),
    "extra_instructions": ("pr_reviewer", "extra_instructions"),
}
INT_FIELDS = {
    "ai_timeout": ("config", "ai_timeout", 60, 3600),
    "max_model_tokens": ("config", "max_model_tokens", 1000, 2000000),
    "num_max_findings": ("pr_reviewer", "num_max_findings", 1, 30),
}
SEVERITIES = {"P0", "P1", "P2", "P3"}


def find_config_path() -> Optional[str]:
    """Locate the live config file, preferring the deployment mount."""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "settings_prod", ".secrets.toml"),
        os.environ.get("DASHBOARD_CONFIG_PATH", ""),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:5]}****{value[-4:]}"


def _validate(model_fields: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize and validate a submission; returns (clean, errors)."""
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    for name, value in model_fields.items():
        if name in STRING_FIELDS:
            if value is None:
                continue
            if not isinstance(value, str):
                errors.append(f"{name} must be a string")
                continue
            clean[name] = value
        elif name in INT_FIELDS:
            low, high = INT_FIELDS[name][2], INT_FIELDS[name][3]
            try:
                number = int(value)
            except (TypeError, ValueError):
                errors.append(f"{name} must be an integer")
                continue
            if not low <= number <= high:
                errors.append(f"{name} must be between {low} and {high}")
                continue
            clean[name] = number
        elif name == "verdict_blocking_severities":
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append("verdict_blocking_severities must be a list of strings")
                continue
            normalized = [v.strip().upper() for v in value]
            invalid = [v for v in normalized if v not in SEVERITIES]
            if invalid:
                errors.append(f"unknown severities: {', '.join(invalid)}")
                continue
            clean[name] = normalized
        elif name == "ignore_glob":
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append("ignore_glob must be a list of glob strings")
                continue
            clean[name] = [v.strip() for v in value if v.strip()]
        else:
            errors.append(f"unknown field: {name}")
    return clean, errors


class ConfigEngine:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or find_config_path()

    # ------------------------------------------------------------------ read

    def read(self) -> Dict[str, Any]:
        """Return the dashboard-visible configuration, secrets masked."""
        raw = self._load_raw()
        if raw is None:
            return {"available": False, "path": self.config_path, "values": {}}
        values: Dict[str, Any] = {}
        for name, (table, key) in STRING_FIELDS.items():
            value = raw.get(table, {}).get(key, "")
            if name == "key":
                values[name] = mask_secret(str(value or ""))
            else:
                values[name] = str(value or "")
        for name, (table, key, _, _) in INT_FIELDS.items():
            values[name] = raw.get(table, {}).get(key)
        values["verdict_blocking_severities"] = list(
            raw.get("pr_reviewer", {}).get("verdict_blocking_severities", []))
        values["ignore_glob"] = list(raw.get("ignore", {}).get("glob", []))
        return {"available": True, "path": self.config_path, "values": values}

    # ----------------------------------------------------------------- write

    def write(self, fields: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate, back up, atomically replace and hot-apply the config."""
        if not self.config_path or not os.path.isfile(self.config_path):
            return False, ["config file not found"]
        if tomli_w is None and tomlkit is None:
            return False, ["no TOML writer available (install tomli-w)"]
        clean, errors = _validate(fields)
        if errors:
            return False, errors
        if not clean:
            return True, []  # nothing to change, e.g. the key field left blank
        try:
            raw = self._load_raw()
            if raw is None:
                return False, ["config file could not be parsed"]
            self._apply_fields(raw, clean)
            self._backup()
            self._atomic_dump(raw)
        except Exception as e:
            get_logger().warning(f"Dashboard config write failed, error: {e}")
            return False, [f"failed to write config: {e}"]
        self._hot_reload(clean)
        return True, []

    def _apply_fields(self, raw: Dict[str, Any], clean: Dict[str, Any]) -> None:
        for name, value in clean.items():
            if name in STRING_FIELDS:
                table, key = STRING_FIELDS[name]
                if name == "key" and not value:
                    continue  # empty secret = leave the stored value alone
                raw.setdefault(table, {})[key] = value
            elif name in INT_FIELDS:
                table, key = INT_FIELDS[name][0], INT_FIELDS[name][1]
                raw.setdefault(table, {})[key] = value
            elif name == "verdict_blocking_severities":
                raw.setdefault("pr_reviewer", {})["verdict_blocking_severities"] = value
            elif name == "ignore_glob":
                raw.setdefault("ignore", {})["glob"] = value

    def _backup(self) -> None:
        backup = f"{self.config_path}.bak.{int(__import__('time').time())}"
        shutil.copy2(self.config_path, backup)
        backups = sorted(glob.glob(f"{self.config_path}.bak.*"))
        for stale in backups[:-MAX_BACKUPS]:
            try:
                os.remove(stale)
            except OSError:
                pass

    def _atomic_dump(self, raw: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.config_path)
        if tomli_w is not None:
            payload = tomli_w.dumps(raw)
        else:
            payload = tomlkit.dumps(raw)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_path)
            os.chmod(self.config_path, 0o600)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def _hot_reload(self, clean: Dict[str, Any]) -> None:
        """Apply saved values to the in-memory Dynaconf settings immediately."""
        from pr_agent.config_loader import global_settings
        try:
            for name, value in clean.items():
                if name in STRING_FIELDS or name in INT_FIELDS:
                    table, key = STRING_FIELDS.get(name, INT_FIELDS.get(name, ("", ""))[:2])
                    global_settings.set(f"{table}.{key}", value)
                elif name == "verdict_blocking_severities":
                    global_settings.set("pr_reviewer.verdict_blocking_severities", value)
                elif name == "ignore_glob":
                    global_settings.set("ignore.glob", value)
        except Exception as e:
            # The file is already correct; a reload miss only delays effect until restart.
            get_logger().warning(f"Dashboard config hot reload failed, error: {e}")

    # ------------------------------------------------------------------ misc

    def _load_raw(self) -> Optional[Dict[str, Any]]:
        if not self.config_path:
            return None
        try:
            with open(self.config_path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            get_logger().warning(f"Dashboard config read failed, error: {e}")
            return None

    def validate_toml_text(self, text: str) -> List[str]:
        """Dry-run validation used by the API before any write is attempted."""
        try:
            tomllib.loads(text)
            return []
        except Exception as e:
            return [f"invalid TOML: {e}"]


_engine: Optional[ConfigEngine] = None


def get_config_engine() -> ConfigEngine:
    global _engine
    if _engine is None:
        _engine = ConfigEngine()
    return _engine
