from app.capabilities import accepted_formats
from app.config import Settings
from app.formats import ALL, REQUIRES_ISOLATION


def test_raw_formats_require_job_isolation(tmp_path):
    executable = tmp_path / "soffice"
    executable.touch()
    for mode in ("landlock", "bwrap", "none"):
        setting = Settings(soffice=executable, parser_sandbox=mode)
        expected = ALL - REQUIRES_ISOLATION if mode == "none" else ALL
        assert accepted_formats(setting) == expected


def test_trusted_mode_refuses_normal_rtf_upload(client):
    from conftest import upload

    response = upload(client, br"{\rtf1 Normal document}", "ordinary.rtf")
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_format"


def test_queued_raw_conversion_after_change_to_trusted_mode(tmp_path, monkeypatch):
    from app import jobs

    settings = Settings(parser_sandbox="none", data_dir=tmp_path)
    runner = jobs.JobRunner(settings, None, None)
    failures = []
    monkeypatch.setattr(runner, "_fail", lambda _, code, message: failures.append(code))
    (tmp_path / "job.json").write_text('{"sourceFormat":"doc"}')
    assert runner._prepare("test", tmp_path, "hybrid") is None
    assert failures == ["unsupported_format"]
