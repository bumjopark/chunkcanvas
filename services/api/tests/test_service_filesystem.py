from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings
from app.serve_isolated import command


def test_only_data_has_a_writable_host_mount(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", soffice=tmp_path / "disabled")
    argv = command(settings, 8452)
    writable = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == "--bind"]
    assert writable == [[str(tmp_path / "data"), "/srv/chunkcanvas/data"]]
    cleared = {argv[i + 1] for i, arg in enumerate(argv) if arg == "--unsetenv"}
    assert {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"} <= cleared
    assert "--unshare-pid" in argv and "--unshare-user" in argv
    sources = [Path(argv[i + 1]) for i, value in enumerate(argv) if value == "--ro-bind"]
    assert all(not (REPO_ROOT / ".env").is_relative_to(source) for source in sources)
    assert "/srv/chunkcanvas/disabled-office" in argv


def test_reject_repository_as_data_root():
    with pytest.raises(ValueError, match="too broad"):
        command(Settings(data_dir=REPO_ROOT), 8452)
