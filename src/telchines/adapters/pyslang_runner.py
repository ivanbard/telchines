from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import pyslang
    except ImportError:
        print("pyslang is not installed; install with: python -m pip install pyslang", file=sys.stderr)
        return 127

    args = list(sys.argv[1:] if argv is None else argv)
    files: list[str] = []
    include_dirs: list[str] = []
    defines: list[str] = []
    top_module: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--lint-only":
            index += 1
            continue
        if arg == "--top":
            index += 1
            if index >= len(args):
                print("--top requires a value", file=sys.stderr)
                return 2
            top_module = args[index]
            index += 1
            continue
        if arg == "-I":
            index += 1
            if index >= len(args):
                print("-I requires a value", file=sys.stderr)
                return 2
            include_dirs.append(args[index])
            index += 1
            continue
        if arg.startswith("-I") and len(arg) > 2:
            include_dirs.append(arg[2:])
            index += 1
            continue
        if arg == "-D":
            index += 1
            if index >= len(args):
                print("-D requires a value", file=sys.stderr)
                return 2
            defines.append(args[index])
            index += 1
            continue
        if arg.startswith("-D") and len(arg) > 2:
            defines.append(arg[2:])
            index += 1
            continue
        if arg.startswith("-"):
            print(f"pyslang fallback ignored unsupported slang option: {arg}", file=sys.stderr)
            index += 1
            continue
        files.append(arg)
        index += 1

    if not files:
        print("pyslang fallback requires at least one source file", file=sys.stderr)
        return 2

    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    driver.setTerminalColorsEnabled(False)
    driver.sourceLoader.addSeparateUnit(files, include_dirs, defines, "", [])
    parsed = driver.parseAllSources()
    parse_ok = driver.reportParseDiags()
    compilation = driver.createCompilation()
    driver.reportCompilation(compilation, quiet=True)
    diagnostics_ok = driver.reportDiagnostics(quiet=False)
    if parsed and parse_ok and diagnostics_ok:
        suffix = f" with top {top_module}" if top_module else ""
        print(f"pyslang lint passed for {len(files)} file(s){suffix}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
