"""Healthchecks: probes and the retry policy.

The retry policy is tested with an injected clock and sleep, so a test covering
"30 retries at 1 second apart" runs in microseconds and asserts on the exact
number of attempts rather than on elapsed wall time.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from devbox.config.models import HttpHealthcheck, TcpHealthcheck
from devbox.health import HealthProbe, HttpProbe, TcpProbe, probe_for, wait_for_health

pytestmark = pytest.mark.unit


class ScriptedProbe:
    """Succeeds on the *n*-th attempt; records how many it received."""

    def __init__(self, succeed_on: int | None = None, error: str = "not ready") -> None:
        self.succeed_on = succeed_on
        self.error = error
        self.attempts = 0
        self.timeouts: list[float] = []

    def describe(self) -> str:
        return "scripted probe"

    def attempt(self, timeout: float) -> tuple[bool, str]:
        self.attempts += 1
        self.timeouts.append(timeout)
        if self.succeed_on is not None and self.attempts >= self.succeed_on:
            return True, ""
        return False, self.error


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class TestRetryPolicy:
    def test_success_on_the_first_attempt(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe(succeed_on=1)
        result = wait_for_health(
            probe,
            TcpHealthcheck(port=1, retries=10),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert result.healthy
        assert result.attempts == 1
        assert clock.slept == [], "a healthy service must not be waited on"

    def test_success_after_several_attempts(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe(succeed_on=4)
        result = wait_for_health(
            probe,
            TcpHealthcheck(port=1, retries=10, interval=0.5),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert result.healthy
        assert result.attempts == 4
        assert clock.slept == [0.5, 0.5, 0.5], "one interval between attempts, none after"

    def test_the_retry_budget_is_respected_exactly(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe()
        result = wait_for_health(
            probe,
            TcpHealthcheck(port=1, retries=7),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert not result.healthy
        assert probe.attempts == 7

    def test_the_start_period_delays_the_first_attempt(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe(succeed_on=1)
        wait_for_health(
            probe,
            TcpHealthcheck(port=1, start_period=5.0),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert clock.slept == [5.0]

    def test_the_per_attempt_timeout_is_passed_through(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe(succeed_on=1)
        wait_for_health(
            probe,
            TcpHealthcheck(port=1, timeout=2.5),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert probe.timeouts == [2.5]

    def test_the_failure_message_names_the_target_and_the_last_error(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe(error="nothing is listening on 127.0.0.1:3000")
        result = wait_for_health(
            probe,
            TcpHealthcheck(port=3000, retries=3),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        rendered = result.render_failure()
        assert "scripted probe" in rendered
        assert "3 attempt(s)" in rendered
        assert "nothing is listening on 127.0.0.1:3000" in rendered


class TestAbort:
    """Waiting 30s for a service that died 2s ago hides the real error."""

    def test_waiting_stops_when_the_process_exits(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe()
        died = {"yes": False}

        def should_abort() -> str | None:
            if probe.attempts >= 2:
                died["yes"] = True
                return "the service process exited with exit code 1"
            return None

        result = wait_for_health(
            probe,
            TcpHealthcheck(port=1, retries=50),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            should_abort=should_abort,
        )
        assert not result.healthy
        assert result.aborted
        assert probe.attempts == 2, "it must not keep polling a dead service"
        assert "exited with exit code 1" in result.render_failure()

    def test_no_abort_callback_means_the_full_budget(self) -> None:
        clock = FakeClock()
        probe = ScriptedProbe()
        wait_for_health(
            probe, TcpHealthcheck(port=1, retries=4), sleep=clock.sleep, monotonic=clock.monotonic
        )
        assert probe.attempts == 4


class TestTcpProbe:
    @pytest.fixture
    def listening_port(self) -> Iterator[int]:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            yield int(server.getsockname()[1])
        finally:
            server.close()

    @pytest.mark.integration
    def test_an_open_port_is_healthy(self, listening_port: int) -> None:
        ready, detail = TcpProbe("127.0.0.1", listening_port).attempt(1.0)
        assert ready
        assert detail == ""

    @pytest.mark.integration
    def test_a_closed_port_reports_that_nothing_is_listening(self, free_port) -> None:  # type: ignore[no-untyped-def]
        ready, detail = TcpProbe("127.0.0.1", free_port()).attempt(0.5)
        assert not ready
        assert "nothing is listening" in detail

    def test_an_unresolvable_host_is_reported_clearly(self) -> None:
        ready, detail = TcpProbe("devbox.invalid.example.", 1).attempt(0.5)
        assert not ready
        assert "cannot resolve host" in detail or "cannot connect" in detail

    def test_the_description_names_the_target(self) -> None:
        assert TcpProbe("127.0.0.1", 5432).describe() == "TCP 127.0.0.1:5432"


class TestHttpProbe:
    def test_the_description_names_the_target(self) -> None:
        assert HttpProbe("http://127.0.0.1:3000/health").describe() == (
            "HTTP GET http://127.0.0.1:3000/health"
        )

    def test_an_unreachable_url_is_reported_clearly(self, free_port) -> None:  # type: ignore[no-untyped-def]
        ready, detail = HttpProbe(f"http://127.0.0.1:{free_port()}/health").attempt(0.5)
        assert not ready
        assert "not answering" in detail

    def test_a_malformed_url_does_not_raise(self) -> None:
        ready, detail = HttpProbe("http://").attempt(0.5)
        assert not ready
        assert detail

    def test_default_acceptance_is_2xx_and_3xx(self) -> None:
        probe = HttpProbe("http://x")
        assert probe._accepts(200)
        assert probe._accepts(302)
        assert not probe._accepts(404)
        assert not probe._accepts(500)

    def test_explicit_statuses_replace_the_default(self) -> None:
        probe = HttpProbe("http://x", accepted=(404,))
        assert probe._accepts(404)
        assert not probe._accepts(200)


class TestProbeSelection:
    def test_a_tcp_healthcheck_builds_a_tcp_probe(self) -> None:
        probe = probe_for(TcpHealthcheck(host="127.0.0.1", port=5432))
        assert isinstance(probe, TcpProbe)
        assert probe.port == 5432

    def test_an_http_healthcheck_builds_an_http_probe(self) -> None:
        probe = probe_for(
            HttpHealthcheck(url="http://127.0.0.1:3000/health", expect_status=(200, 204))
        )
        assert isinstance(probe, HttpProbe)
        assert probe.accepted == (200, 204)

    def test_every_probe_satisfies_the_protocol(self) -> None:
        assert isinstance(TcpProbe("127.0.0.1", 1), HealthProbe)
        assert isinstance(HttpProbe("http://x"), HealthProbe)
