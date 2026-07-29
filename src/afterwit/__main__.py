"""`python -m afterwit` — the entrypoint when no console script is on PATH."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
