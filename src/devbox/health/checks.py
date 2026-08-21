"""Healthchecks.

"Started" is not "ready".  A Node server has a PID milliseconds after
``devbox up`` launches it and is still not listening a second later; a PostgreSQL
instance accepts TCP connections before it will accept queries.  Devbox therefore
waits on an explicit readiness signal before declaring a service healthy and
before starting anything that depends on it.

Two probes ship: TCP (the port accepts a connection) and HTTP (an endpoint
answers acceptably).  Both are one-shot and pure enough to test directly; the
retry policy lives in :func:`wait_for_health`, which takes its clock and its
sleep function as arguments so the retry logic is tested without any test
actually sleeping.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from devbox.config.models import Healthcheck, HttpHealthcheck, TcpHealthcheck

__all__ = [
    "HealthProbe",
    "HealthResult",
    "HttpProbe",
    "TcpProbe",
    "probe_for",
    "wait_for_health",
]


@dataclass(frozen=True, slots=True)
class HealthResult:
    """The outcome of waiting for one healthcheck."""

    healthy: bool
    attempts: int
    elapsed: float
    description: str
    last_error: str = ""
    aborted: bool = False
    """True when the wait stopped early because the process died."""

    def render_failure(self) -> str:
        if self.aborted:
            return f"{self.description} was never reached: {self.last_error}"
        return (
            f"{self.description} did not become healthy after {self.attempts} attempt(s) "
            f"in {self.elapsed:.1f}s: {self.last_error or 'no response'}"
        )


@runtime_checkable
class HealthProbe(Protocol):
    """A single readiness attempt."""

    def describe(self) -> str:
        """Human-readable target, e.g. ``TCP 127.0.0.1:5432``."""
        ...

    def attempt(self, timeout: float) -> tuple[bool, str]:
        """Return ``(ready, detail)``.  Must never raise for a normal failure."""
        ...


@dataclass(frozen=True, slots=True)
class TcpProbe:
    """Ready when a TCP connection is accepted."""

    host: str
    port: int

    def describe(self) -> str:
        return f"TCP {self.host}:{self.port}"

    def attempt(self, timeout: float) -> tuple[bool, str]:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True, ""
        except TimeoutError:
            return False, f"connection to {self.host}:{self.port} timed out after {timeout:g}s"
        except ConnectionRefusedError:
            return False, f"nothing is listening on {self.host}:{self.port}"
        except socket.gaierror as exc:
            return False, f"cannot resolve host {self.host!r}: {exc}"
        except OSError as exc:
            return False, f"cannot connect to {self.host}:{self.port}: {exc}"


@dataclass(frozen=True, slots=True)
class HttpProbe:
    """Ready when an HTTP endpoint answers with an accepted status."""

    url: str
    method: str = "GET"
    accepted: tuple[int, ...] = ()

    def describe(self) -> str:
        return f"HTTP {self.method} {self.url}"

    def _accepts(self, status: int) -> bool:
        if self.accepted:
            return status in self.accepted
        return 200 <= status < 400

    def attempt(self, timeout: float) -> tuple[bool, str]:
        request = urllib.request.Request(self.url, method=self.method)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a real answer: the service is up but not ready, unless
            # the configuration explicitly accepts that status.
            status = int(exc.code)
            if self._accepts(status):
                return True, ""
            return False, f"{self.url} answered HTTP {status}"
        except urllib.error.URLError as exc:
            return False, f"{self.url} is not answering: {exc.reason}"
        except TimeoutError:
            return False, f"{self.url} did not answer within {timeout:g}s"
        except (OSError, ValueError) as exc:
            return False, f"{self.url} could not be reached: {exc}"
        if self._accepts(status):
            return True, ""
        expected = ", ".join(str(code) for code in self.accepted) or "2xx or 3xx"
        return False, f"{self.url} answered HTTP {status}, expected {expected}"


def probe_for(healthcheck: Healthcheck) -> HealthProbe:
    """Build the probe described by a validated configuration block."""
    match healthcheck:
        case TcpHealthcheck(host=host, port=port):
            return TcpProbe(host=host, port=port)
        case HttpHealthcheck(url=url, method=method, expect_status=expected):
            return HttpProbe(url=url, method=method, accepted=expected)


def wait_for_health(
    probe: HealthProbe,
    healthcheck: Healthcheck,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    should_abort: Callable[[], str | None] | None = None,
) -> HealthResult:
    """Retry *probe* until it succeeds or the retry budget runs out.

    *should_abort* is polled between attempts and returns a reason when waiting
    has become pointless — in practice "the process exited".  Waiting 30 seconds
    for a healthcheck on a service that died two seconds ago wastes the
    developer's time and hides the real error, which is in its stderr.

    ``sleep`` and ``monotonic`` are injected so the retry policy is tested
    exactly, in microseconds, with no real waiting.
    """
    started = monotonic()

    if healthcheck.start_period > 0:
        # Failures during the grace period are expected and are not counted.
        sleep(healthcheck.start_period)

    attempts = 0
    last_error = ""
    while attempts < healthcheck.retries:
        if should_abort is not None and (reason := should_abort()) is not None:
            return HealthResult(
                healthy=False,
                attempts=attempts,
                elapsed=monotonic() - started,
                description=probe.describe(),
                last_error=reason,
                aborted=True,
            )
        attempts += 1
        ready, detail = probe.attempt(healthcheck.timeout)
        if ready:
            return HealthResult(
                healthy=True,
                attempts=attempts,
                elapsed=monotonic() - started,
                description=probe.describe(),
            )
        last_error = detail
        if attempts < healthcheck.retries:
            sleep(healthcheck.interval)

    return HealthResult(
        healthy=False,
        attempts=attempts,
        elapsed=monotonic() - started,
        description=probe.describe(),
        last_error=last_error,
    )
