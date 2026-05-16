"""Enable `python -m lumin` as an alias for the `lumin` console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
