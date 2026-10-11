#!/usr/bin/env python3
"""Remove only PDFCompressor-owned workflows and its dedicated runtime."""
from __future__ import annotations
import argparse
from pathlib import Path
import plistlib
import shutil
from install import PRESETS, PREFIX, refresh_services


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    home = args.home.expanduser().absolute()
    for preset, label in PRESETS.items():
        bundle = home / "Library" / "Services" / f"PDF压缩 · {label}.workflow"
        if not bundle.exists():
            continue
        try:
            with (bundle / "Contents" / "Info.plist").open("rb") as f:
                info = plistlib.load(f)
            if info.get("PDFCompressorOwner", info.get("CFBundleIdentifier")) == PREFIX + preset:
                shutil.rmtree(bundle)
                print(f"已删除：{bundle}")
        except (OSError, plistlib.InvalidFileException):
            print(f"保留无法确认归属的文件：{bundle}")
    runtime = home / "Library" / "Application Support" / "PDFCompressor"
    if runtime.is_dir():
        shutil.rmtree(runtime)
    if home == Path.home():
        refresh_services()
    print("卸载完成。PDF 原件、压缩副本及诊断日志均保留。")


if __name__ == "__main__":
    main()
