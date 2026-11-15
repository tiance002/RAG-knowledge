"""Offline business-source wiring; no business replay or external calls."""
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_rsi_offline as offline


@pytest.mark.parametrize("stage", ["prepare", "deliver"])
def test_archived_worker_imports_explicit_business_backend(tmp_path, stage):
    business = tmp_path / "baseline"
    backend = business / "backend"
    backend.mkdir(parents=True)
    (backend / "business_marker.py").write_text("VALUE = 'frozen business source'\n")
    recovery = tmp_path / "run" / "implementation"
    recovery.mkdir(parents=True)
    worker = recovery / "worker.py"
    worker.write_text("import sys,json\nfrom pathlib import Path\n"
                      "ROOT=Path(__file__).resolve().parents[1]\n"
                      "sys.path.insert(0,str(ROOT/'backend'))\n"
                      "sys.path.insert(0,str(ROOT/'scripts'))\n"
                      "import business_marker\n"
                      "Path(sys.argv[-1]).write_text(json.dumps({'marker':business_marker.VALUE,'file':business_marker.__file__,'stage':sys.argv[1]}))\n")
    cwd = tmp_path / stage
    cwd.mkdir()
    output = cwd / "out.json"
    result = offline.child(stage, [output], cwd, worker=worker, helper_dir=recovery, business_root=business)
    assert result["marker"] == "frozen business source" and result["stage"] == stage
    assert Path(result["file"]).is_relative_to(backend)


@pytest.mark.parametrize("dirty", [False, True])
def test_substrate_guard_checks_selected_business_root_and_rejects_dirty(tmp_path, monkeypatch, dirty):
    business = tmp_path / "business"
    calls = []
    def output(cmd, **kwargs):
        calls.append(cmd)
        return "" if "ls-files" in cmd else "head-sha\n"
    def run(cmd, **kwargs):
        calls.append(cmd)
        if dirty and "diff" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(offline.subprocess, "check_output", output)
    monkeypatch.setattr(offline.subprocess, "run", run)
    manifest = {"status": "READY", "base_commit": "frozen-sha", "fixed": offline.FIXED,
                "evaluator_version": offline.EVALUATOR}
    if dirty:
        with pytest.raises(subprocess.CalledProcessError):
            offline.validate_source_roots(manifest, business)
    else:
        roots = offline.validate_source_roots(manifest, business)
        assert roots["business_root"] == str(business.resolve()) and roots["control_root"] == str(offline.ROOT)
    diff = next(cmd for cmd in calls if "diff" in cmd)
    assert diff[2] == str(business.resolve()) and diff[-2:] == ["--", "backend/app"]
    assert "frozen-sha" in diff
