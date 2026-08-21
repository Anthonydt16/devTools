"""Entry point for the fake runtime, kept here so the fixture directory is
self-contained and directly runnable::

    python tests/fixtures/fake-runtime/runtime_main.py serve --port 8080

The implementation lives in ``devbox.testing.fake_runtime_main`` so that it is
linted and type-checked like the rest of the code, and so that
``devbox.testing.fake_runtime`` can install it into a generated runtime tree.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from devbox.testing.fake_runtime_main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
