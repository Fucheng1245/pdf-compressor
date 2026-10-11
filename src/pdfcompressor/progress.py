"""Transient native progress panel; UI failures never interrupt compression."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys

STAGES = {
    'reading': '正在读取 PDF…', 'cloning': '正在准备内容…',
    'images': '正在压缩图片…', 'cleaning': '正在清理冗余数据…',
    'writing': '正在保存副本…', 'validating': '正在检查输出…',
    'finished': '正在完成…', 'waiting': '正在等待前一个任务…',
}


class ProgressPanel:
    def __init__(self, label: str, files: int):
        self.process = None
        self.label = label
        self.files = files
        self.index = 0
        self.file = ''
        if sys.platform == 'darwin':
            binary = Path.home() / 'Library/Application Support/PDFCompressor/Progress.app/Contents/MacOS/PDFCompressorProgress'
            if binary.is_file():
                try:
                    self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                    text=True, encoding='utf-8')
                except OSError:
                    pass

    def select_file(self, file: str, index: int):
        self.file = Path(file).name
        self.index = index

    def update(self, stage: str, completed: int = 0, total: int = 0):
        if not self.process or not self.process.stdin:
            return
        title = 'PDF压缩 · ' + self.label
        name = self.file if self.files == 1 else f'{self.index}/{self.files} · {self.file}'
        detail = STAGES.get(stage, stage)
        if stage == 'images' and total:
            detail = f'正在处理页面 · {completed}/{total}'
        try:
            self.process.stdin.write(json.dumps({'title': title, 'file': name, 'stage': detail,
                                                'completed': completed, 'total': total}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            pass

    def close(self):
        if self.process:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
