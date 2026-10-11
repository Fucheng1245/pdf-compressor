from pathlib import Path
from pdfcompressor.core import compress
from pdfcompressor.progress import ProgressPanel
from test_core import fixture_pdf


def test_progress_reports_actual_pages_and_preserves_output(tmp_path):
    source = fixture_pdf(tmp_path / 'document.pdf', image=True, transparent=True)
    events = []
    with_progress = compress(source, 'low', progress=lambda *event: events.append(event))
    without_progress = compress(source, 'low')
    assert with_progress.output.read_bytes() == without_progress.output.read_bytes()
    assert events[0] == ('reading', 0, 0)
    assert ('images', 0, 1) in events
    assert ('images', 1, 1) in events
    assert [stage for stage, _, _ in events][-4:] == ['cleaning', 'writing', 'validating', 'finished']


def test_missing_or_closed_ui_does_not_interrupt_compression(monkeypatch, tmp_path):
    monkeypatch.setattr('pdfcompressor.progress.sys.platform', 'linux')
    panel = ProgressPanel('均衡压缩', 2)
    panel.select_file('/example/a.pdf', 1)
    panel.update('waiting')
    panel.close()
    class ClosedPipe:
        def write(self, text):
            raise BrokenPipeError()
        def flush(self):
            raise BrokenPipeError()
        def close(self):
            pass
    class ExitedProcess:
        stdin = ClosedPipe()
        def wait(self, timeout):
            return 0
    panel.process = ExitedProcess()
    source = fixture_pdf(tmp_path / 'document.pdf')
    result = compress(source, progress=panel.update)
    assert result.output.is_file()
    panel.close()
