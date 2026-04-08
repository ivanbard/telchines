from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    target = Path(sys.argv[1])
    text = target.read_text(encoding="utf-8")
    if "coutn" in text:
        print(f"ERROR: {target.as_posix()}:10: unknown identifier coutn")
        return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
