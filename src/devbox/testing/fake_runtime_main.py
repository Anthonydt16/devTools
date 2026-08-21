"""Fake runtime payload used by the Devbox test suite.

It stands in for ``node`` / ``python`` / a database server so that the whole
orchestrator — install, up, healthchecks, ps, logs, exec, down — can be tested on
a machine with none of them installed, and with no network access.

Sub-commands
------------
``<script>``             stand in for running a real script — see "Script mode"
``--version``            print ``v<version>`` (baked in at install time) and exit
``serve``                bind a TCP port, answer HTTP ``/health``, stay alive
``sleep``                stay alive for N seconds doing nothing
``fail``                 write to stderr and exit with a chosen code
``crash``                exit immediately with code 1, no output
``spawn-child``          start a grandchild that outlives its parent's own exit
``echo-args``            print every argv entry on its own line, verbatim
``print-env``            print the value of an environment variable
``print-path``           print PATH, one entry per line

The version comes from ``DEVBOX_FAKE_RUNTIME_VERSION``, which the generated
launcher sets.  That is what makes ``devbox exec legacy-api node --version``
print ``v18.19.0`` while ``devbox exec new-api node --version`` prints
``v24.6.0`` in the same ``devbox up``.

Script mode
-----------
``node server.js`` cannot work without Node, but an *example* that says
``command: python app.py`` to stay testable is not an example of anything.  So
when the first argument is a script path rather than a sub-command, the fake
runtime looks for ``<script>.fake`` beside it — a small JSON file describing what
the real script would do::

    {"port_env": "PORT", "default_port": 3000, "health_path": "/health"}

and serves that.  The example therefore ships the *real* ``server.js`` and runs
unchanged with real Node; the ``.fake`` file is only consulted by this stand-in.
Without one, script mode fails loudly rather than pretending to succeed.
"""

from __future__ import annotations

import argparse
import http.server
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

VERSION = os.environ.get("DEVBOX_FAKE_RUNTIME_VERSION", "0.0.0")
RUNTIME_NAME = os.environ.get("DEVBOX_FAKE_RUNTIME_NAME", "fake")

_stop = threading.Event()


def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: types.FrameType | None) -> None:
        print(f"{RUNTIME_NAME}: received signal {signum}, shutting down", flush=True)
        _stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


class _Handler(http.server.BaseHTTPRequestHandler):
    health_path = "/health"
    health_status = 200
    body = b'{"status":"ok"}'

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == self.health_path:
            status = self.health_status
        elif path == "/":
            status = 200
        else:
            status = 404
        payload = self.body if status < 400 else b'{"status":"error"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"{RUNTIME_NAME}: {fmt % args}\n")
        sys.stderr.flush()


def _serve(args: argparse.Namespace) -> int:
    _Handler.health_path = args.health_path
    _Handler.health_status = args.health_status

    if args.start_delay > 0:
        print(f"{RUNTIME_NAME} {VERSION}: warming up for {args.start_delay}s", flush=True)
        time.sleep(args.start_delay)

    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), _Handler)
    except OSError as exc:
        sys.stderr.write(f"{RUNTIME_NAME}: cannot bind {args.host}:{args.port}: {exc}\n")
        sys.stderr.flush()
        return 3
    server.daemon_threads = True
    actual_port = server.server_address[1]

    # The readiness line is the contract the integration tests rely on: once it
    # is in stdout, the port is bound and /health answers.
    print(f"{RUNTIME_NAME} {VERSION} listening on {args.host}:{actual_port}", flush=True)
    sys.stderr.write(f"{RUNTIME_NAME} {VERSION}: startup diagnostics on stderr\n")
    sys.stderr.flush()

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()

    deadline = time.monotonic() + args.max_seconds if args.max_seconds > 0 else None
    try:
        while not _stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            if args.exit_after > 0 and time.monotonic() >= args.exit_after_deadline:
                print(f"{RUNTIME_NAME}: exiting with {args.exit_code}", flush=True)
                break
            _stop.wait(0.05)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(f"{RUNTIME_NAME}: stopped", flush=True)
    return args.exit_code if args.exit_after > 0 else 0


def _sleep(args: argparse.Namespace) -> int:
    print(f"{RUNTIME_NAME} {VERSION}: sleeping {args.seconds}s", flush=True)
    end = time.monotonic() + args.seconds
    while not _stop.is_set() and time.monotonic() < end:
        _stop.wait(0.05)
    print(f"{RUNTIME_NAME}: done", flush=True)
    return 0


def _fail(args: argparse.Namespace) -> int:
    print(f"{RUNTIME_NAME} {VERSION}: about to fail", flush=True)
    sys.stderr.write(f"{args.message}\n")
    sys.stderr.flush()
    return int(args.code)


def _spawn_child(args: argparse.Namespace) -> int:
    """Start a grandchild, then keep running.

    Used to prove that stopping a service kills the whole tree: the grandchild
    binds its own port and only a job-object / process-group kill takes it down.
    """
    child = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "serve",
            "--port",
            str(args.child_port),
            "--max-seconds",
            str(args.max_seconds),
        ],
        env=dict(os.environ, DEVBOX_FAKE_RUNTIME_NAME=f"{RUNTIME_NAME}-child"),
    )
    print(f"{RUNTIME_NAME}: spawned child pid {child.pid}", flush=True)
    return _serve(args)


def _echo_args(args: argparse.Namespace) -> int:
    for value in args.values:
        print(value, flush=True)
    return 0


def _print_env(args: argparse.Namespace) -> int:
    print(os.environ.get(args.name, ""), flush=True)
    return 0


SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".py")


def _run_script(script: str, argv: list[str]) -> int:
    """Stand in for a real script, driven by its ``<script>.fake`` sidecar."""
    import json

    path = Path(script)
    directive = Path(f"{script}.fake")
    if not directive.is_file():
        sys.stderr.write(
            f"{RUNTIME_NAME}: cannot run {script!r}: this is the Devbox fake runtime, "
            f"and no directive file was found at {directive}.\n"
            "Install a real runtime, or add a .fake file; see "
            "tests/fixtures/fake-runtime/README.md.\n"
        )
        sys.stderr.flush()
        return 2

    try:
        document = json.loads(directive.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"{RUNTIME_NAME}: {directive} is unreadable: {exc}\n")
        sys.stderr.flush()
        return 2

    port_env = str(document.get("port_env", "PORT"))
    port = int(os.environ.get(port_env) or document.get("default_port") or 0)
    forwarded = [
        "serve",
        "--host",
        str(document.get("host", "127.0.0.1")),
        "--port",
        str(port),
        "--health-path",
        str(document.get("health_path", "/health")),
        "--max-seconds",
        str(document.get("max_seconds", 900)),
        *argv,
    ]
    print(f"{RUNTIME_NAME} {VERSION}: running {path.name} (fake)", flush=True)
    return int(main(forwarded))


def _print_path(_args: argparse.Namespace) -> int:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            print(entry, flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=RUNTIME_NAME, add_help=True)
    parser.add_argument("--version", action="store_true", help="print the runtime version")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="bind a port and answer /health")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--health-path", default="/health")
    serve.add_argument("--health-status", type=int, default=200)
    serve.add_argument("--start-delay", type=float, default=0.0)
    serve.add_argument("--max-seconds", type=float, default=120.0)
    serve.add_argument("--exit-after", type=float, default=0.0)
    serve.add_argument("--exit-code", type=int, default=0)
    serve.set_defaults(handler=_serve)

    sleeper = sub.add_parser("sleep", help="stay alive doing nothing")
    sleeper.add_argument("--seconds", type=float, default=60.0)
    sleeper.set_defaults(handler=_sleep)

    failer = sub.add_parser("fail", help="write to stderr and exit non-zero")
    failer.add_argument("--code", type=int, default=1)
    failer.add_argument("--message", default="fake runtime failed on purpose")
    failer.set_defaults(handler=_fail)

    crash = sub.add_parser("crash", help="exit immediately")
    crash.set_defaults(handler=lambda _args: 1)

    spawn = sub.add_parser("spawn-child", help="spawn a grandchild, then serve")
    spawn.add_argument("--host", default="127.0.0.1")
    spawn.add_argument("--port", type=int, default=0)
    spawn.add_argument("--child-port", type=int, default=0)
    spawn.add_argument("--health-path", default="/health")
    spawn.add_argument("--health-status", type=int, default=200)
    spawn.add_argument("--start-delay", type=float, default=0.0)
    spawn.add_argument("--max-seconds", type=float, default=120.0)
    spawn.add_argument("--exit-after", type=float, default=0.0)
    spawn.add_argument("--exit-code", type=int, default=0)
    spawn.set_defaults(handler=_spawn_child)

    echo = sub.add_parser("echo-args", help="print every argument verbatim")
    echo.add_argument("values", nargs="*")
    echo.set_defaults(handler=_echo_args)

    env = sub.add_parser("print-env", help="print one environment variable")
    env.add_argument("name")
    env.set_defaults(handler=_print_env)

    show_path = sub.add_parser("print-path", help="print PATH entries")
    show_path.set_defaults(handler=_print_path)

    return parser


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


KNOWN_COMMANDS = frozenset(
    {"serve", "sleep", "fail", "crash", "spawn-child", "echo-args", "print-env", "print-path"}
)


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    arguments = list(sys.argv[1:] if argv is None else argv)

    # Script mode: `node server.js` rather than one of our own sub-commands.
    if arguments and not arguments[0].startswith("-") and arguments[0] not in KNOWN_COMMANDS:
        first = arguments[0]
        if first.lower().endswith(SCRIPT_SUFFIXES):
            return _run_script(first, arguments[1:])

    args = build_parser().parse_args(arguments)
    if getattr(args, "version", False):
        print(f"v{VERSION}", flush=True)
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        print(f"v{VERSION}", flush=True)
        return 0
    if getattr(args, "exit_after", 0) > 0:
        args.exit_after_deadline = time.monotonic() + args.exit_after
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
