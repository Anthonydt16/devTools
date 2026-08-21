"""Meta-tests: prove the suite fails when the code is wrong.

A green suite only means something if it would go red for the right reasons.
Each test here **sabotages a mandatory behaviour** and asserts that the guarantee
disappears — which is what makes the corresponding non-regression test
load-bearing rather than decorative.

Two levels:

* in-process mutation, which is fast and precise;
* one real ``pytest`` run against a sabotaged build, which proves the *suite*
  itself goes red, not merely that a helper changed its answer.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from devbox.errors import IntegrityError, SecurityError
from devbox.security.archive import extract_archive
from devbox.security.hashing import verify_sha256

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestGuaranteesAreRealNotVacuous:
    def test_zip_slip_protection_is_what_rejects_the_evil_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 5 passes because of the containment check, not by accident."""
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../../evil.exe", "MZ")

        # Baseline: the guarantee holds.
        with pytest.raises(SecurityError):
            extract_archive(archive, tmp_path / "a")

        # Sabotage the containment check.
        monkeypatch.setattr(
            "devbox.security.paths.is_safe_relative_path", lambda _candidate: (True, "")
        )
        monkeypatch.setattr("devbox.security.archive.normalise_archive_member", lambda name: name)

        with pytest.raises(SecurityError) as caught:
            extract_archive(archive, tmp_path / "b")
        # The second containment check — on the *resolved* path — still catches it.
        assert "resolves outside" in str(caught.value), (
            "with member validation disabled, the resolved-path check must be what "
            "stops the escape; if neither fires, rule 5 is not enforced"
        )

    def test_integrity_verification_is_what_refuses_a_bad_artifact(self) -> None:
        """Rule 4 passes because digests are compared, not because nothing runs."""
        with pytest.raises(IntegrityError):
            verify_sha256("a" * 64, "b" * 64, subject="node 18.19.0")

        # And it really compares — a matching pair must not raise.
        verify_sha256("a" * 64, "A" * 64, subject="node 18.19.0")

    def test_integrity_has_two_independent_guards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_registry, host_platform
    ) -> None:  # type: ignore[no-untyped-def]
        """Disabling the digest comparison alone does not let a bad artifact in.

        Two separate checks stand between a tampered lockfile and an install: the
        lockfile-versus-registry comparison in ``Downloader.fetch``, and the
        digest of the bytes actually received.  Neutralising the second still
        leaves the first, which is why the subprocess meta-test below sabotages
        path containment (a single mechanism) rather than integrity.
        """
        from devbox.host.paths import DevboxPaths
        from devbox.lockfile.models import LockedRuntime
        from devbox.runtime.manager import RuntimeManager
        from devbox.runtime.registry import FilesystemRegistry

        monkeypatch.setattr(
            "devbox.runtime.download.verify_sha256",
            lambda actual, expected, *, subject: None,
        )
        manager = RuntimeManager(
            DevboxPaths(root=tmp_path / "home").ensure(), platform=host_platform
        )
        tampered = LockedRuntime(
            name="node",
            version="18.19.0",
            platform=host_platform.tag,
            sha256="f" * 64,
            source=str(fake_registry.root),
            url=fake_registry.artifact("node", "18.19.0").relative_url,
        )

        with pytest.raises(IntegrityError):
            manager.install_locked(tampered, FilesystemRegistry(fake_registry.root))

    def test_pid_ownership_is_what_refuses_the_kill(self, tmp_path: Path) -> None:
        """Rule 7 passes because creation times are compared."""
        from devbox.process.fake import FakeSupervisor
        from devbox.process.supervisor import LaunchSpec

        working = tmp_path / "app"
        working.mkdir()
        supervisor = FakeSupervisor()
        spec = LaunchSpec(
            name="api",
            argv=("node", "s.js"),
            working_dir=working,
            env={},
            stdout_path=tmp_path / "o.log",
            stderr_path=tmp_path / "e.log",
        )
        record = supervisor.start(spec, project_id="p")

        # Baseline: a matching record stops cleanly.
        assert supervisor.stop(record).stopped

        second = supervisor.start(spec, project_id="p")
        supervisor.simulate_pid_reuse("api")
        assert supervisor.stop(second).refused, (
            "if the creation time were ignored, this would report a successful stop"
        )

    def test_shared_ownership_is_what_keeps_infrastructure_running(self) -> None:
        """Rule 2 passes because of the ownership rule, which is one method."""
        from devbox.state.models import InfraOwnership, InfraRecord

        globally_owned = InfraRecord(
            instance="pg",
            provider="postgres",
            pid=1,
            port=5432,
            ownership=InfraOwnership.GLOBAL,
            claims=("app-a",),
        )
        allowed, reason = globally_owned.may_be_stopped_by_project("app-a")
        assert not allowed and reason

        project_owned = InfraRecord(
            instance="pg", provider="postgres", pid=1, port=5432
        ).with_claim("app-a")
        allowed, _ = project_owned.may_be_stopped_by_project("app-a")
        assert allowed, "the rule must not simply refuse everything"

    def test_argument_splitting_is_what_prevents_injection(self) -> None:
        """Rule 8 passes because no shell is involved, not because input is filtered."""
        from devbox.security.command import split_command

        argv = split_command("echo hi && rm -rf /")
        assert "&&" in argv, "the metacharacter is preserved as a literal argument"
        assert argv[0] == "echo"
        assert len(argv) == 6, "and it is not collapsed or dropped"


@pytest.mark.slow
@pytest.mark.integration
class TestTheSuiteReallyGoesRed:
    """Run pytest for real against a sabotaged build and check it fails."""

    def test_breaking_path_containment_fails_the_suite(self, tmp_path: Path) -> None:
        plugin = tmp_path / "sabotage_containment.py"
        plugin.write_text(
            "\n".join(
                [
                    '"""Sabotage plugin: accept every archive member as safe."""',
                    "import devbox.security.paths as paths",
                    "import devbox.security.archive as archive",
                    "",
                    "",
                    "def pytest_configure(config):",
                    "    paths.is_safe_relative_path = lambda candidate: (True, '')",
                    "    paths.normalise_archive_member = lambda name: name",
                    "    paths.resolve_within = (",
                    "        lambda base, relative, *, what='path': base / relative",
                    "    )",
                    "    archive.normalise_archive_member = paths.normalise_archive_member",
                    "    archive.resolve_within = paths.resolve_within",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        sabotaged = _run_pytest(
            ["-k", "Rule05", "tests/unit/test_non_regression.py", "-p", "sabotage_containment"],
            extra_path=tmp_path,
        )
        assert sabotaged.returncode != 0, (
            "with path containment disabled the suite still passed, so rule 5 is not "
            "actually enforced by a test\n" + sabotaged.stdout[-2000:]
        )

        healthy = _run_pytest(["-k", "Rule05", "tests/unit/test_non_regression.py"])
        assert healthy.returncode == 0, healthy.stdout[-2000:]

    def test_breaking_the_ownership_rule_fails_the_suite(self, tmp_path: Path) -> None:
        plugin = tmp_path / "sabotage_ownership.py"
        plugin.write_text(
            "\n".join(
                [
                    '"""Sabotage plugin: let any project stop any shared server."""',
                    "import devbox.state.models as models",
                    "",
                    "",
                    "def pytest_configure(config):",
                    "    models.InfraRecord.may_be_stopped_by_project = (",
                    "        lambda self, project_id: (True, '')",
                    "    )",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        sabotaged = _run_pytest(
            ["-k", "Rule02", "tests/unit/test_non_regression.py", "-p", "sabotage_ownership"],
            extra_path=tmp_path,
        )
        assert sabotaged.returncode != 0, (
            "with the ownership rule disabled the suite still passed, so rule 2 is "
            "not actually enforced\n" + sabotaged.stdout[-2000:]
        )


def _run_pytest(
    arguments: list[str], *, extra_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    entries = [str(REPO_ROOT / "src")]
    if extra_path is not None:
        entries.insert(0, str(extra_path))
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["NO_COLOR"] = "1"
    # A nested pytest run must not inherit the parent's cache or plugin autoload.
    env.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        check=False,
    )
