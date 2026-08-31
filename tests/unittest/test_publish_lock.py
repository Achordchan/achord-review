import multiprocessing
import os
import time

import pytest

from pr_agent.algo import publish_lock as publish_lock_module
from pr_agent.algo.publish_lock import publish_lock


def _hold(key, path, seconds):
    """Take the lock in a separate process and hold it for `seconds`."""
    with publish_lock(key) as held:
        with open(path, "w") as f:
            f.write("1" if held else "0")
        time.sleep(seconds)


class TestPublishLock:
    """The lock has to be arbitrated by the kernel, or it is another read-then-act."""

    def test_an_uncontended_lock_is_held(self):
        with publish_lock("pr-1") as held:
            assert held

    def test_the_lock_is_released_for_the_next_run(self):
        with publish_lock("pr-2") as first:
            assert first
        with publish_lock("pr-2") as second:
            assert second, "a released lock must be available again"

    def test_the_body_still_runs_when_the_lock_is_unavailable(self, monkeypatch):
        """Failing open: a missed review is worse than a duplicated one."""
        monkeypatch.setattr(publish_lock_module, "fcntl", None)
        ran = False
        with publish_lock("pr-3") as held:
            ran = True
            assert not held
        assert ran

    def test_an_error_taking_the_lock_does_not_stop_the_publish(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(publish_lock_module.os, "open", boom)
        with publish_lock("pr-4") as held:
            assert not held

    def test_a_second_holder_waits_and_then_proceeds_unlocked(self, tmp_path):
        """The timeout must not deadlock a run - it publishes unlocked instead."""
        with publish_lock("pr-5"):
            start = time.monotonic()
            with publish_lock("pr-5", timeout=0.3) as second:
                waited = time.monotonic() - start
            assert not second, "the lock was already held"
            assert waited >= 0.3, "it must actually wait before giving up"

    def test_different_keys_do_not_contend(self):
        with publish_lock("pr-6") as first:
            with publish_lock("pr-7", timeout=0.3) as second:
                assert first and second, "separate PRs must not block each other"

    @pytest.mark.skipif(publish_lock_module.fcntl is None, reason="POSIX only")
    def test_the_lock_is_held_against_another_process(self, tmp_path):
        """The two triggers land in different gunicorn workers, so this is the real case."""
        marker = tmp_path / "child.txt"
        child = multiprocessing.Process(target=_hold, args=("pr-8", str(marker), 1.5))
        child.start()
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.read_text() == "1", "the child must have taken the lock"
            with publish_lock("pr-8", timeout=0.3) as held:
                assert not held, "one process must not enter while another holds it"
        finally:
            child.join(10)

    def test_the_lock_file_lives_outside_the_repository(self):
        path = publish_lock_module._lock_path("pr-9")
        assert os.path.isabs(path) and path.endswith(".lock")


class TestPublishingOffTheEventLoop:
    """The publish path waits on the lock in a thread, so the wait can afford to be long.

    That only holds if the request-scoped settings survive the hop. They are read from a
    contextvar, and losing them would silently fall back to global_settings - the publish
    would then run without the repo's overrides and without config.is_auto_command, which
    decides whether a review may be silenced at all. Nothing would raise.
    """

    def test_request_scoped_settings_survive_the_thread_hop(self):
        import asyncio

        from starlette_context import context, request_cycle_context

        async def main():
            with request_cycle_context({"settings": "request-scoped"}):
                def in_thread():
                    try:
                        return context["settings"]
                    except Exception as e:
                        return f"lost: {type(e).__name__}"

                return await asyncio.to_thread(in_thread)

        assert asyncio.run(main()) == "request-scoped"
