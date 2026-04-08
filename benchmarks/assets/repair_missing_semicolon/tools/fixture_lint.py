from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    target = Path(sys.argv[1])
    lines = target.read_text(encoding="utf-8").splitlines()
    for line_no, text in enumerate(lines, start=1):
        if "count <= 4'd0" in text and not text.strip().endswith(";"):
            print(f"ERROR: {target.as_posix()}:9: expected semicolon before end")
            return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
