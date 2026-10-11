import importlib.util
from pathlib import Path
import plistlib
import shlex

import pytest

spec = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_generated_workflows_filter_pdf_and_quote_paths(tmp_path):
    python = tmp_path / 'folder with spaces' / 'venv/bin/python'
    bundles = installer.install_workflows(tmp_path, python)
    assert len(bundles) == 3
    for preset, bundle in zip(installer.PRESETS, bundles):
        info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
        assert info['NSServices'][0]['NSSendFileTypes'] == ['com.adobe.pdf']
        assert info['NSServices'][0]['NSIconName'] == 'NSActionTemplate'
        assert info['NSServices'][0]['NSBackgroundColorName'] == 'background'
        doc = plistlib.loads((bundle / 'Contents/document.wflow').read_bytes())
        assert doc['workflowMetaData']['serviceInputTypeIdentifier'] == 'com.apple.Automator.fileSystemObject.PDF'
        action = doc['actions'][0]['action']
        command = action['ActionParameters']['COMMAND_STRING']
        assert shlex.quote(str(python)) in command
        assert f'--preset {preset}' in command
        assert '"$@"' in command
        assert action['ActionParameters']['inputMethod'] == 1


def test_installer_never_overwrites_unknown_workflow(tmp_path):
    bundle = tmp_path / 'Library/Services/PDF压缩 · 画质优先.workflow/Contents'
    bundle.mkdir(parents=True)
    (bundle/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'another.app'}))
    with pytest.raises(RuntimeError, match='不覆盖'):
        installer.install_workflows(tmp_path, Path('/example/python'))


def test_update_and_uninstall_preserve_other_workflows(tmp_path):
    import subprocess
    import sys
    other = tmp_path / 'Library/Services/Other.workflow/Contents'
    other.mkdir(parents=True)
    (other / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'other.app'}))
    python = Path(sys.executable)
    installer.install_workflows(tmp_path, python)
    bundles = installer.install_workflows(tmp_path, python)
    assert len(bundles) == 3
    result = subprocess.run([sys.executable, str(Path(installer.__file__).with_name('uninstall.py')),
                             '--home', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert other.exists()
    assert not any(bundle.exists() for bundle in bundles)
