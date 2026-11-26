"""Build the command line for parser subprocesses, optionally inside bubblewrap.

The bwrap sandbox has no network, a read-only view of /usr, the virtualenv, the app code and
the models, and write access only to the one document directory.
"""
import os
import json
import resource
import sys
import subprocess
import tempfile
import logging
from pathlib import Path

from .config import API_ROOT, Settings

THREAD_ENV = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "TORCH_NUM_THREADS")


def child_env(settings: Settings) -> dict[str, str]:
    threads = str(settings.parser_threads)
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "USER": "chunkcanvas",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    env.update({k: threads for k in THREAD_ENV})
    if settings.soffice_path:
        # The parser reads LibreOffice's OpenSymbol font to tell the symbols it drew.
        env["CC_SOFFICE"] = str(settings.soffice_path.resolve())
    return env


def python_command(settings: Settings, module: str, args: list[str], writable: Path) -> list[str]:
    python = sys.executable
    cmd = [python, "-m", module, *args]
    if settings.parser_sandbox == "landlock":
        read_only = [Path(p) for p in ("/usr", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/fonts", "/var/cache/fontconfig",
                     "/etc/localtime", "/etc/nsswitch.conf",
                     "/etc/mime.types", "/etc/ssl/openssl.cnf", "/etc/ssl/certs",
                     "/proc/cpuinfo", "/proc/meminfo", "/proc/stat", "/proc/self/status",
                     "/proc/self/maps", "/proc/self/oom_score_adj", "/sys/devices/system/cpu")]
        read_only += [Path(sys.prefix), API_ROOT / "app", settings.models_dir]
        if settings.soffice_path:
            soffice = settings.soffice_path.resolve()
            read_only.append(soffice.parents[1] if soffice.parent.name == "program" else soffice)
        read_only = [p for p in read_only if p.exists()]
        config = {"write": str(writable.resolve()), "read": [str(p) for p in set(read_only)],
                  "dirs": [str(API_ROOT), str(Path(sys.prefix).parent)]}
        return [python, "-m", "app.isolate", json.dumps(config), module, *args]
    if settings.parser_sandbox != "bwrap":
        return cmd
    venv = Path(sys.prefix)
    bwrap = [
        "/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    # fontconfig needs /etc/fonts, or converted Korean text renders as boxes.
    for name in ("ld.so.cache", "localtime", "nsswitch.conf", "fonts"):
        p = Path("/etc") / name
        if p.exists():
            bwrap += ["--ro-bind", str(p), str(p)]
    bwrap += ["--tmpfs", "/dev/shm"]
    read_only = {venv, API_ROOT / "app", settings.models_dir.resolve()}
    soffice = settings.soffice_path
    if soffice and not str(soffice.resolve()).startswith("/usr/"):
        read_only.add(soffice.resolve().parents[1])  # the LibreOffice install (…/program/soffice)
    for ro in read_only:
        bwrap += ["--ro-bind", str(ro), str(ro)]
    bwrap += ["--bind", str(writable), str(writable), "--chdir", str(API_ROOT)]
    return bwrap + cmd


def prefer_oom_victim() -> None:
    """Set before applying filesystem isolation, then retain the setting across exec."""
    if os.environ.get("CC_OOM_ADJUSTED") == "1":
        return
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
        os.environ["CC_OOM_ADJUSTED"] = "1"
    except OSError as exc:
        logging.getLogger("chunkcanvas.sandbox").warning("Could not set parser OOM priority: %s", type(exc).__name__)


def limit_self() -> None:
    """Called first thing in parser subprocesses: be the first OOM victim, cap written files."""
    prefer_oom_victim()
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Landlock installs priority/process-group restrictions after applying these
    # limits; its exec target must not try to change priority a second time.
    if os.environ.get('CC_JOB_ISOLATED') != 'landlock':
        os.nice(5)


def verify_sandbox(settings: Settings) -> None:
    """Fail at startup if the selected isolation cannot be installed; never fall back to none."""
    if settings.parser_sandbox == "none":
        return
    if settings.parser_sandbox == "landlock" and any(os.access(p, os.W_OK) for p in ("/tmp", "/var/tmp")):
        raise RuntimeError("Landlock conversion requires non-writable /tmp and /var/tmp. Use bwrap on a host with writable temporary directories.")
    with tempfile.TemporaryDirectory(prefix="sandbox-check-", dir=settings.data_dir) as folder:
        result = subprocess.run(python_command(settings, "app.sandbox", [], Path(folder)),
                                cwd=API_ROOT, env=child_env(settings), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=20)
        if result.returncode or result.stdout.strip() != b"sandbox-ready":
            raise RuntimeError(f"Cannot start {settings.parser_sandbox} sandbox: {result.stderr.decode(errors='replace')[-1000:]}")


if __name__ == "__main__":
    limit_self()
    print("sandbox-ready")
