"""Run a one-pass job repeatedly inside one long-lived process.

process_due_messages and process_bulk_campaigns each do one pass and exit. They were
deployed as always-on ECS services, so ECS relaunched them the moment they exited:
~300 worker starts an hour, each booting Django and re-reading the whole due set, and the
worker's CPU autoscaling read those boot spikes as load and held it at ten tasks. Looping
in-process with a pause between passes keeps the same cadence without the churn.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Callable

from django.db import close_old_connections

logger = logging.getLogger(__name__)


class PassLoop:
    """Call ``run_pass(should_stop)`` every ``interval_seconds`` until SIGTERM/SIGINT.

    The interval is measured from the end of one pass to the start of the next, so a slow
    pass can never overlap the following one. ``should_stop`` lets a pass bail out between
    units of work, so a deploy's SIGTERM never abandons a send half-way; ECS waits the
    task's stopTimeout before SIGKILL.
    """

    def __init__(
        self,
        name: str,
        run_pass: Callable[[Callable[[], bool]], object],
        interval_seconds: float,
    ):
        self.name = name
        self.run_pass = run_pass
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()

    def stop(self, *_args) -> None:
        if not self._stop.is_set():
            logger.info('pass_loop_stop_requested name=%s', self.name)
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

    def run(self, max_passes: int | None = None) -> int:
        """Loop until stopped (or ``max_passes`` passes, for tests). Returns passes run."""
        passes = 0
        logger.info('pass_loop_started name=%s interval_s=%s', self.name, self.interval_seconds)
        while not self._stop.is_set():
            started = time.monotonic()
            # A connection can be dropped by the server between passes; Django only
            # recycles it per request, and a management command never ends one.
            close_old_connections()
            try:
                result = self.run_pass(self.should_stop)
                logger.info(
                    'pass_loop_pass_done name=%s result=%s duration_s=%.1f',
                    self.name,
                    result,
                    time.monotonic() - started,
                )
            except Exception:
                # One bad pass must not take the service down: ECS would relaunch it,
                # which is the churn this loop exists to remove.
                logger.exception('pass_loop_pass_failed name=%s', self.name)
            finally:
                close_old_connections()

            passes += 1
            if max_passes is not None and passes >= max_passes:
                break
            self._stop.wait(self.interval_seconds)

        logger.info('pass_loop_stopped name=%s passes=%s', self.name, passes)
        return passes
