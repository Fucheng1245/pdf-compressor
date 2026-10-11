from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys

from .core import PRESETS, compress


def _notify_error(message: str) -> None:
    if sys.platform != "darwin":
        return
    # Pass text as argv, never interpolate paths or errors into AppleScript.
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "PDF压缩失败"\nend run'
    subprocess.run(["/usr/bin/osascript", "-e", script, message], capture_output=True, check=False)


def _finder_logger() -> tuple[logging.Logger, Path]:
    log_dir = Path.home() / "Library" / "Logs" / "PDFCompressor"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pdfcompressor")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_dir / "compress.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger, log_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 PDF 三档压缩，保留原件。")
    parser.add_argument("--preset", choices=PRESETS, default="high")
    parser.add_argument("--finder", action="store_true", help="静默成功、记录日志、仅失败时通知")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    logger = None
    lock = None
    if args.finder:
        import fcntl
        logger, log_dir = _finder_logger()
        lock = open(log_dir / "worker.lock", "a")
        os.chmod(log_dir / "worker.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
    failures = []
    try:
        for file in args.files:
            try:
                result = compress(file, args.preset)
                record = result.as_dict()
                if logger:
                    logger.info(json.dumps(record, ensure_ascii=False))
                else:
                    print(json.dumps(record, ensure_ascii=False))
            except Exception as exc:
                failures.append(f"{Path(file).name}: {exc}")
                if logger:
                    logger.error("%s: %s", file, exc)
                else:
                    print(failures[-1], file=sys.stderr)
        if failures and args.finder:
            _notify_error(f"{len(failures)} 个文件未完成。{failures[0][:180]}")
    finally:
        if lock:
            lock.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
