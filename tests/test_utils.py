from __future__ import annotations

import os
from pathlib import Path

import pytest

from telchines.utils import copy_tree_to_temp, remove_tree


def test_copy_tree_to_temp_excludes_runtime_and_build_dirs(work_root: Path) -> None:
    source = work_root / "project"
    (source / "rtl").mkdir(parents=True)
    (source / ".tel" / "task-artifacts").mkdir(parents=True)
    (source / "dist").mkdir()
    (source / "build").mkdir()
    (source / "__pycache__").mkdir()
    (source / "rtl" / "demo.sv").write_text("module demo; endmodule\n", encoding="utf-8")
    (source / ".tel" / "task-artifacts" / "request.json").write_text('{"secret": "value"}', encoding="utf-8")
    (source / "dist" / "pkg.whl").write_text("wheel", encoding="utf-8")
    (source / "build" / "temp.txt").write_text("build", encoding="utf-8")
    (source / "__pycache__" / "module.pyc").write_text("cache", encoding="utf-8")

    temp_root = copy_tree_to_temp(source)
    try:
        assert (temp_root / "rtl" / "demo.sv").exists()
        assert not (temp_root / ".tel").exists()
        assert not (temp_root / "dist").exists()
        assert not (temp_root / "build").exists()
        assert not (temp_root / "__pycache__").exists()
    finally:
        remove_tree(temp_root)


def test_copy_tree_to_temp_skips_symlinks(work_root: Path) -> None:
    source = work_root / "project"
    source.mkdir()
    outside = work_root / "outside.txt"
    outside.write_text("do not copy", encoding="utf-8")
    link = source / "outside_link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    temp_root = copy_tree_to_temp(source)
    try:
        assert not (temp_root / "outside_link.txt").exists()
    finally:
        remove_tree(temp_root)
