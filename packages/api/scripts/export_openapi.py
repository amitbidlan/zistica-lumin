"""Export the FastAPI app's OpenAPI spec to a versioned file
(Slice 7A).

Usage:

    cd packages/api
    .venv/bin/python scripts/export_openapi.py --out openapi.json

CI runs this on main and asserts the committed file matches what
this script would produce — drift is caught at PR review time, not
at integration time.

The committed spec lives at ``packages/api/openapi.json`` so
downstream client generators can reference it via the same path
they'd use to install the API package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure ``packages/api`` is importable as the working directory
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from main import app  # noqa: E402  — sys.path adjustment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="Path to write the JSON spec to.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero if the on-disk spec at --out differs from the "
            "current app's spec. Use in CI to enforce 'export checked in'."
        ),
    )
    args = parser.parse_args()

    spec = app.openapi()
    serialised = json.dumps(spec, indent=2, sort_keys=True) + "\n"

    out_path = Path(args.out)
    if args.check:
        if not out_path.exists():
            print(
                f"ERROR: {out_path} does not exist. Run "
                f"'python scripts/export_openapi.py --out {out_path}' "
                f"and commit the result.",
                file=sys.stderr,
            )
            sys.exit(2)
        existing = out_path.read_text()
        if existing != serialised:
            print(
                f"ERROR: OpenAPI spec at {out_path} is stale. Re-run "
                f"the export and commit the diff.",
                file=sys.stderr,
            )
            # Show the first 20 lines of the diff for triage
            try:
                import difflib
                diff = difflib.unified_diff(
                    existing.splitlines(),
                    serialised.splitlines(),
                    fromfile=str(out_path),
                    tofile="(generated)",
                    lineterm="",
                    n=2,
                )
                for line in list(diff)[:60]:
                    print(line, file=sys.stderr)
            except Exception:
                pass
            sys.exit(3)
        print(f"OK: {out_path} matches current spec")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialised)
    print(f"wrote {out_path} ({len(serialised):,} bytes)")


if __name__ == "__main__":
    main()
