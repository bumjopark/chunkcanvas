"""Start the systemd API in a filesystem built from its configured inputs."""
import argparse
import os
import sys
from pathlib import Path

from .config import API_ROOT, REPO_ROOT, Settings


def command(settings: Settings, port: int) -> list[str]:
    root = Path("/srv/chunkcanvas")
    api = root / "services/api"
    cmd = ["/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-user",
           "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
           "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
           "--symlink", "usr/sbin", "/sbin", "--symlink", "usr/lib", "/lib",
           "--symlink", "usr/lib64", "/lib64", "--dev", "/dev", "--proc", "/proc",
           "--dir", "/tmp", "--dir", "/var/tmp"]
    for name in ("ld.so.cache", "localtime", "hosts", "resolv.conf", "nsswitch.conf",
                 "gai.conf", "fonts", "mime.types", "ssl/certs", "ssl/openssl.cnf"):
        source = Path("/etc") / name
        if source.exists():
            cmd.extend(("--ro-bind", str(source), str(source)))

    def mount(source: Path, target: Path, writable: bool = False) -> None:
        source = source.resolve(strict=True)
        # Never expose the repository or the account home as a storage/input root.
        if REPO_ROOT.is_relative_to(source) or Path.home().is_relative_to(source):
            raise ValueError(f"Service mount is too broad: {source}")
        cmd.extend(("--bind" if writable else "--ro-bind", str(source), str(target)))

    mount(API_ROOT / "app", api / "app")
    mount(Path(sys.prefix), api / ".venv")
    data = settings.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    mount(data, root / "data", writable=True)
    for source, target in ((settings.models_dir, api / ".models"),
                           (settings.gallery_dir, root / "gallery"),
                           (settings.samples_dir if settings.basic_samples else None, root / "samples"),
                           (settings.web_dist, root / "web")):
        if source and source.is_dir():
            mount(source, target)

    office = settings.soffice_path
    office_path = root / "disabled-office"
    if office:
        office = office.resolve(strict=True)
        if office.is_relative_to("/usr"):
            office_path = office
        elif office.parent.name == "program":
            mount(office.parents[1], root / "office")
            office_path = root / "office/program" / office.name
        else:
            raise ValueError("Use an installed LibreOffice program/soffice or a system executable")
    for name in ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        cmd.extend(("--unsetenv", name))
    cmd.extend(("--setenv", "CC_SOFFICE", str(office_path),
                "--setenv", "CC_DATA_DIR", str(root / "data"),
                "--setenv", "CC_MODELS_DIR", str(api / ".models"),
                "--setenv", "CC_GALLERY_DIR", str(root / "gallery"),
                "--setenv", "CC_SAMPLES_DIR", str(root / "samples"),
                "--setenv", "CC_WEB_DIST", str(root / "web"),
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "TMPDIR", str(root / "data"), "--setenv", "HOME", "/nonexistent",
                "--remount-ro", "/dev", "--remount-ro", "/", "--chdir", str(api)))
    return [*cmd, str(api / ".venv/bin/python"), "-B", "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--no-proxy-headers", "--workers", "1"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    argv = command(Settings(), args.port)
    os.execv(argv[0], argv)
