from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    target = Path(sys.argv[1])
    text = target.read_text(encoding="utf-8")
    if "endmodule" not in text:
        print(f"ERROR: {target.as_posix()}:7: expected endmodule at end of file")
        return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
