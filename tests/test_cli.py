from pdfcompressor.cli import main
from test_core import fixture_pdf


def test_batch_continues_after_failure(tmp_path, capsys):
    src = fixture_pdf(tmp_path/'ok.pdf')
    status = main(['--preset','high','--',str(tmp_path/'missing.pdf'),str(src)])
    captured = capsys.readouterr()
    assert status == 1
    assert (tmp_path/'ok_high.pdf').exists()
    assert 'missing.pdf' in captured.err
    assert 'before_bytes' in captured.out
