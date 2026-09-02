"""Live read/write access to the deployment config.toml for the dashboard.

The deployment mounts its config at pr_agent/settings_prod/.secrets.toml (see
deploy/achord-review/docker-compose.yml), which the config loader already
merges into the global Dynaconf settings. This module edits that file directly:
values are validated, the file is atomically replaced after a timestamped
backup, and the in-memory Dynaconf object is updated in place so changes apply
without a container restart.

Every write uses a comment-preserving TOML document and changes only the
targeted fields, never regenerating the file from a template.
"""

import fcntl
import glob
import math
import os
import shutil
import stat
import tempfile
import time
import tomllib
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import tomlkit

from pr_agent.log import get_logger

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
    return f"****{value[-4:]}"


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
            if name == "model" and not value.strip():
                errors.append("model must not be empty")
                continue
            clean[name] = value
        elif name in INT_FIELDS:
            low, high = INT_FIELDS[name][2], INT_FIELDS[name][3]
            if value is None or value == "":
                continue  # unset optional field keeps its configured default
            if isinstance(value, bool):
                errors.append(f"{name} must be an integer")
                continue
            if isinstance(value, int):
                number = value
            elif isinstance(value, float):
                if not math.isfinite(value) or not value.is_integer():
                    errors.append(f"{name} must be an integer")
                    continue
                number = int(value)
            elif isinstance(value, str):
                candidate = value.strip()
                digits = candidate[1:] if candidate.startswith(("+", "-")) else candidate
                if not digits.isdigit():
                    errors.append(f"{name} must be an integer")
                    continue
                number = int(candidate)
            else:
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
        self._loaded_signature = self._file_signature()
        initial_raw = self._load_raw() or {}
        self._loaded_paths = self._flatten_paths(initial_raw)

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
        """Validate, back up, atomically replace and hot-apply the config.

        Writes go through tomlkit (a comment-preserving TOML document) so the
        operational documentation in the file — comments, ordering, formatting
        — survives every dashboard edit. A plain-dict TOML dump would rewrite
        the file bare, so it is never used.
        """
        if not self.config_path or not os.path.isfile(self.config_path):
            return False, ["config file not found"]
        if tomlkit is None:
            return False, ["no comment-preserving TOML writer available (install tomlkit)"]
        clean, errors = _validate(fields)
        if errors:
            return False, errors
        if not clean:
            return True, []  # nothing to change, e.g. the key field left blank
        try:
            with self._config_write_lock():
                # The lock spans the complete read-modify-backup-replace cycle.
                # A second gunicorn worker must read the first worker's result,
                # not an old document that later overwrites the first save.
                doc = self._load_document()
                if doc is None:
                    return False, ["config file could not be parsed"]
                if "key" in clean:
                    stored_key = str(doc.get("openai", {}).get("key", "") or "")
                    if clean["key"] == mask_secret(stored_key):
                        clean.pop("key")  # masked GET value round-tripped unchanged
                if not clean:
                    return True, []
                overridden_paths = sorted(
                    path for name in clean
                    if (path := self._field_path(name)) and self._has_environment_override(path))
                if overridden_paths:
                    return False, [
                        "fields controlled by environment cannot be changed here: "
                        + ", ".join(overridden_paths)
                    ]
                self._apply_fields(doc, clean)
                self._backup()
                self._atomic_dump(doc)
                # Keep the process-local apply and signature snapshot in the
                # same interprocess critical section as the file replacement.
                # Otherwise another worker can replace the file between these
                # steps and this worker can mark older values as current.
                raw = self._load_raw()
                if raw is None or not self._hot_reload(raw):
                    return True, ["configuration saved but hot reload failed; restart required"]
                self._loaded_signature = self._file_signature()
        except Exception as e:
            get_logger().warning(f"Dashboard config write failed, error: {e}")
            return False, [f"failed to write config: {e}"]
        return True, []

    @contextmanager
    def _config_write_lock(self):
        lock_path = f"{self.config_path}.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
        # time_ns keeps backups sortable; UUID guarantees uniqueness even on
        # filesystems/clocks whose effective timestamp resolution is coarser.
        backup = f"{self.config_path}.bak.{time.time_ns()}.{uuid.uuid4().hex}"
        directory = os.path.dirname(self.config_path)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".dashboard-backup-")
        try:
            with open(self.config_path, "rb") as source, os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, backup)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        backups = sorted(glob.glob(f"{self.config_path}.bak.*"))
        for stale in backups[:-MAX_BACKUPS]:
            try:
                os.remove(stale)
            except OSError as e:
                # best-effort retention: keeping one extra backup is harmless,
                # losing the head of the log because removal raised is not
                get_logger().debug(f"Dashboard config backup cleanup skipped a file, error: {e}")

    def _atomic_dump(self, doc) -> None:
        directory = os.path.dirname(self.config_path) or "."
        payload = tomlkit.dumps(doc)
        source_stat = os.stat(self.config_path, follow_symlinks=False)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
                temp_stat = os.fstat(f.fileno())
                if (temp_stat.st_uid, temp_stat.st_gid) != (source_stat.st_uid, source_stat.st_gid):
                    os.fchown(f.fileno(), source_stat.st_uid, source_stat.st_gid)
                # Preserve owner access while enforcing the secrets-file policy.
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp_path, self.config_path)
            self._fsync_directory(directory)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        """Make a completed atomic rename durable before reporting success."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _hot_reload(self, raw: Dict[str, Any]) -> bool:
        """Apply the complete saved document to in-memory Dynaconf settings.

        This updates the worker performing the save immediately. Other workers
        compare the file signature at the start of their next HTTP request and
        call reload_if_changed(), so the deployment can retain concurrency
        without reporting a process-local hot reload as globally complete.
        """
        from pr_agent.config_loader import global_settings, global_settings_lock
        current_paths = self._flatten_paths(raw)

        def _apply(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    _apply(child, f"{path}.{key}" if path else str(key))
                return
            if self._has_environment_override(path):
                return  # Dynaconf environment sources retain higher precedence.
            global_settings.set(path, value)

        try:
            with global_settings_lock:
                if self._is_dynaconf_managed_path() and callable(getattr(global_settings, "reload", None)):
                    # Rebuild from normal sources so removed file values fall
                    # back to defaults and environment precedence is replayed.
                    global_settings.reload()
                else:
                    for removed_path in self._loaded_paths - current_paths:
                        if not self._has_environment_override(removed_path):
                            global_settings.unset(removed_path)
                    _apply(raw, "")
                self._loaded_paths = current_paths
            return True
        except Exception as e:
            # The file is already correct; a reload miss only delays effect until restart.
            get_logger().warning(f"Dashboard config hot reload failed, error: {e}")
            return False

    @staticmethod
    def _has_environment_override(path: str) -> bool:
        environment_keys = {key.upper() for key in os.environ}
        nested_env_key = path.replace(".", "__").upper()
        legacy_env_key = path.replace(".", "_").upper()
        return nested_env_key in environment_keys or legacy_env_key in environment_keys

    @staticmethod
    def _field_path(name: str) -> str:
        if name in STRING_FIELDS:
            table, key = STRING_FIELDS[name]
            return f"{table}.{key}"
        if name in INT_FIELDS:
            table, key = INT_FIELDS[name][0], INT_FIELDS[name][1]
            return f"{table}.{key}"
        if name == "verdict_blocking_severities":
            return "pr_reviewer.verdict_blocking_severities"
        if name == "ignore_glob":
            return "ignore.glob"
        return ""

    @staticmethod
    def _flatten_paths(raw: Dict[str, Any], prefix: str = "") -> set:
        paths = set()
        for key, value in raw.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                paths.update(ConfigEngine._flatten_paths(value, path))
            else:
                paths.add(path)
        return paths

    def _is_dynaconf_managed_path(self) -> bool:
        managed = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "settings_prod", ".secrets.toml")
        return bool(self.config_path and os.path.realpath(self.config_path) == os.path.realpath(managed))

    def reload_if_changed(self) -> bool:
        """Apply external/dashboard config changes once per worker.

        Returns True only after a newer on-disk document was parsed and applied.
        File-stat and parse failures are fail-safe: the worker keeps its last
        known-good settings and retries on the next request.
        """
        signature = self._file_signature()
        if signature is None or signature == self._loaded_signature:
            return False
        raw = self._load_raw()
        if raw is None:
            return False
        if not self._hot_reload(raw):
            return False
        self._loaded_signature = signature
        return True

    def _file_signature(self) -> Optional[tuple]:
        if not self.config_path:
            return None
        try:
            stat_result = os.stat(self.config_path)
            return (stat_result.st_dev, stat_result.st_ino,
                    stat_result.st_mtime_ns, stat_result.st_size)
        except OSError:
            return None

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

    def _load_document(self):
        """Parse the config as a tomlkit document (comments preserved).

        Returns None when the file is missing or unparseable — callers treat
        that the same as _load_raw's failure.
        """
        if not self.config_path:
            return None
        try:
            with open(self.config_path, "rb") as f:
                return tomlkit.load(f)
        except Exception as e:
            get_logger().warning(f"Dashboard config document read failed, error: {e}")
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
