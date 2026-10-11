#!/usr/bin/env python3
"""Install isolated runtime and three user-owned Finder Quick Actions."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
PRESETS = {"high": "画质优先", "mid": "均衡压缩", "low": "体积优先"}
PREFIX = "io.github.pdfcompressor.quickaction."


def workflow_documents(preset: str, python: Path) -> tuple[dict, dict]:
    label = "PDF压缩 · " + PRESETS[preset]
    command = f'exec {shlex.quote(str(python))} -m pdfcompressor.cli --finder --preset {preset} -- "$@"'
    action = {
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {"CheckedForUserDefaultShell": True, "COMMAND_STRING": command,
                             "inputMethod": 1, "shell": "/bin/bash", "source": ""},
        "AMAccepts": {"Container": "List", "Optional": False, "Types": ["com.apple.cocoa.path"]},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3", "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in ("CheckedForUserDefaultShell", "COMMAND_STRING", "inputMethod", "shell", "source")},
        "BundleIdentifier": "com.apple.RunShellScript", "CFBundleVersion": "2.0.3",
        "Class Name": "RunShellScriptAction", "CanShowWhenRun": False,
        "CanShowSelectedItemsWhenRun": False, "Category": ["AMCategoryUtilities"],
        "InputUUID": str(uuid.uuid4()), "OutputUUID": str(uuid.uuid4()), "UUID": str(uuid.uuid4()),
        "UnlocalizedApplications": ["Automator"], "Keywords": ["Shell", "Script"],
    }
    document = {
        "actions": [{"action": action, "isViewVisible": False}],
        "AMApplicationBuild": "521", "AMApplicationVersion": "2.10", "AMDocumentVersion": "2",
        "connectors": {},
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "applicationBundleIDsByPath": {"/System/Library/CoreServices/Finder.app": "com.apple.finder"},
            "applicationPaths": ["/System/Library/CoreServices/Finder.app"],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject.PDF",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject.PDF",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    info = {
        # Match Automator's own workflow plist rather than pretending this is
        # an executable app bundle. Keep ownership in a tool-specific key.
        "PDFCompressorOwner": PREFIX + preset,
        "NSServices": [{"NSMenuItem": {"default": label}, "NSMessage": "runWorkflowAsService",
                        "NSIconName": "NSActionTemplate",
                        "NSBackgroundColorName": "background",
                        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
                        "NSSendFileTypes": ["com.adobe.pdf"]}],
    }
    return info, document


def install_workflows(home: Path, python: Path) -> list[Path]:
    outputs = []
    for preset, label in PRESETS.items():
        bundle = home / "Library" / "Services" / f"PDF压缩 · {label}.workflow"
        contents = bundle / "Contents"
        if bundle.exists():
            try:
                with (contents / "Info.plist").open("rb") as stream:
                    previous = plistlib.load(stream)
                if previous.get("PDFCompressorOwner", previous.get("CFBundleIdentifier")) != PREFIX + preset:
                    raise RuntimeError(f"不覆盖未知工作流：{bundle}")
            except (OSError, plistlib.InvalidFileException) as exc:
                raise RuntimeError(f"不覆盖未知工作流：{bundle}") from exc
        contents.mkdir(parents=True, exist_ok=True)
        info, doc = workflow_documents(preset, python)
        for name, value in (("Info.plist", info), ("document.wflow", doc)):
            with (contents / name).open("wb") as stream:
                plistlib.dump(value, stream)
        outputs.append(bundle)
    return outputs


def finder_preferences(data: dict, enable: bool, labels=None) -> dict:
    """Update only our entries, preserving native plist bool/int types.

    SERVICE identifiers below were verified in Finder on macOS 14.6.
    Never use defaults -dict-add with untyped values: it writes strings.
    """
    labels = list(PRESETS.values() if labels is None else labels)
    active = data.setdefault("FinderActive", {})
    ordering = data.setdefault("FinderOrdering", {})
    status = data.setdefault("NSServicesStatus", {})
    for label in labels:
        name = "PDF压缩 · " + label
        key = "SERVICE-(null) - " + name
        service = "(null) - " + name + " - runWorkflowAsService"
        # Remove the unused identifier variant left by early local debugging.
        active.pop(key + " - runWorkflowAsService", None)
        ordering.pop(key + " - runWorkflowAsService", None)
        if enable:
            active[key] = True
            others = [v for k, v in ordering.items()
                      if k != key and type(v) is int]
            ordering[key] = ordering.get(key) if type(ordering.get(key)) is int else max(others, default=-1) + 1
            modes = status.setdefault(service, {}).setdefault("presentation_modes", {})
            modes.update({k: True for k in ("ContextMenu", "FinderPreview", "ServicesMenu", "TouchBar")})
        else:
            active.pop(key, None)
            ordering.pop(key, None)
            status.pop(service, None)
    return data


def configure_finder(enable: bool, labels=None) -> None:
    result = subprocess.run(["defaults", "export", "pbs", "-"], check=True, capture_output=True)
    data = finder_preferences(plistlib.loads(result.stdout), enable, labels)
    for section in ("FinderActive", "FinderOrdering", "NSServicesStatus"):
        subprocess.run(["defaults", "write", "pbs", section,
                        plistlib.dumps(data[section]).decode("utf-8")], check=True)


def refresh_services() -> None:
    tool = Path("/System/Library/CoreServices/pbs")
    if tool.exists():
        subprocess.run([str(tool), "-flush"], check=False, capture_output=True)
        subprocess.run([str(tool), "-update"], check=False, capture_output=True)


def install_progress(runtime: Path) -> None:
    contents = runtime / "Progress.app" / "Contents"
    binary = contents / "MacOS" / "PDFCompressorProgress"
    binary.parent.mkdir(parents=True, exist_ok=True)
    source = REPO / "src" / "pdfcompressor" / "native" / "Progress.m"
    subprocess.run(["/usr/bin/clang", "-O2", "-fobjc-arc", "-fblocks", "-framework", "Cocoa",
                    str(source), "-o", str(binary.with_suffix(".new"))], check=True)
    os.replace(binary.with_suffix(".new"), binary)
    info = {"CFBundleIdentifier": "io.github.pdfcompressor.progress",
            "CFBundleName": "PDFCompressorProgress", "CFBundleExecutable": binary.name,
            "CFBundlePackageType": "APPL", "LSUIElement": True,
            "NSHighResolutionCapable": True, "CFBundleVersion": "1"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(), help="工作流目标用户目录")
    parser.add_argument("--workflows-only", action="store_true", help="仅生成工作流，供验证或已有运行环境使用")
    parser.add_argument("--python", type=Path, help="已有运行环境的 Python（配合 --workflows-only）")
    args = parser.parse_args()
    if sys.platform != "darwin" and not args.workflows_only:
        parser.error("Finder 安装仅支持 macOS。")
    home = args.home.expanduser().absolute()
    runtime = home / "Library" / "Application Support" / "PDFCompressor"
    python = args.python or runtime / "venv" / "bin" / "python"
    if not args.workflows_only:
        uv = shutil.which("uv")
        if not uv:
            parser.error("请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/")
        runtime.mkdir(parents=True, exist_ok=True)
        if not python.exists():
            subprocess.run([uv, "venv", "--python", "3.12", str(runtime / "venv")], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(python), str(REPO)], check=True)
        install_progress(runtime)
    for output in install_workflows(home, python.absolute()):
        print(output)
    if home == Path.home() and sys.platform == "darwin":
        configure_finder(True)
        refresh_services()
    print("已安装并启用三档快速操作。入口未出现时请重新打开 Finder 窗口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
