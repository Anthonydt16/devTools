"""``python -m devbox``.

A real module rather than a shim so that PyInstaller and ``python -m`` resolve
the same entry point.
"""

from devbox.cli.main import main

if __name__ == "__main__":
    main()
