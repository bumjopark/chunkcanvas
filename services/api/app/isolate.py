"""Unprivileged per-job Linux isolation for containers that cannot run bubblewrap.

Landlock ABI 6+ restricts filesystem access and cross-domain signals/abstract Unix sockets.
seccomp additionally denies network connections, non-Unix sockets and path metadata changes
that Landlock does not mediate. Failure to install either policy aborts before document code.
The API invokes this module with trusted configuration, never with document-supplied paths.
"""
import ctypes as C
import errno
import json
import os
from pathlib import Path
import socket
import sys


class Ruleset(C.Structure):
    _fields_ = [("fs", C.c_uint64), ("net", C.c_uint64), ("scoped", C.c_uint64)]


class PathRule(C.Structure):
    _pack_ = 1
    _fields_ = [("access", C.c_uint64), ("fd", C.c_int32)]


class Arg(C.Structure):
    _fields_ = [("arg", C.c_uint), ("op", C.c_int), ("a", C.c_uint64), ("b", C.c_uint64)]


READ = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read directory
FS = (1 << 16) - 1  # ABI 5: through IOCTL_DEV; newer rights are not needed by these jobs
WRITE = FS & ~((1 << 6) | (1 << 11))  # never create device nodes


def restrict(writable: Path, read_only: list[Path], directories: list[Path]) -> None:
    libc = C.CDLL(None, use_errno=True)
    sec = C.CDLL("libseccomp.so.2", use_errno=True)
    sec.seccomp_syscall_resolve_name.argtypes = [C.c_char_p]
    sec.seccomp_syscall_resolve_name.restype = C.c_int

    def syscall(name: str, *args):
        nr = sec.seccomp_syscall_resolve_name(name.encode())
        if nr < 0:
            raise RuntimeError(f"Missing syscall: {name}")
        result = libc.syscall(C.c_long(nr), *args)
        if result < 0:
            raise OSError(C.get_errno(), f"{name}: {os.strerror(C.get_errno())}")
        return result

    abi = syscall("landlock_create_ruleset", C.c_void_p(), C.c_size_t(0), C.c_uint(1))
    if abi < 6:
        raise RuntimeError(f"Landlock ABI 6 required (found {abi}); use bubblewrap on this host")
    attr = Ruleset(FS, 0, 3)  # isolate abstract Unix sockets and signals from outside the job
    rules = syscall("landlock_create_ruleset", C.byref(attr), C.c_size_t(C.sizeof(attr)), C.c_uint(0))
    try:
        def allow(path: Path, access: int):
            path = path.resolve()
            if not path.exists():
                return
            if not path.is_dir():
                access &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14) | (1 << 15)
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, fd)
                syscall("landlock_add_rule", C.c_int(rules), C.c_int(1), C.byref(rule), C.c_uint(0))
            finally:
                os.close(fd)

        for path in read_only:
            allow(path, READ)
        for path in directories:
            allow(path, 1 << 3)
        allow(writable, WRITE)
        for name in ("null", "zero", "random", "urandom"):
            allow(Path("/dev") / name, (1 << 1) | (1 << 2))
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(C.get_errno(), "Cannot set no_new_privs")
        syscall("landlock_restrict_self", C.c_int(rules), C.c_uint(0))
    finally:
        os.close(rules)

    sec.seccomp_init.argtypes = [C.c_uint32]
    sec.seccomp_init.restype = C.c_void_p
    sec.seccomp_rule_add_array.argtypes = [C.c_void_p, C.c_uint32, C.c_int, C.c_uint, C.POINTER(Arg)]
    sec.seccomp_load.argtypes = [C.c_void_p]
    sec.seccomp_release.argtypes = [C.c_void_p]
    ctx = sec.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("Cannot initialize seccomp")
    try:
        def deny(name: str, compare: Arg | None = None, error: int = errno.EPERM):
            nr = sec.seccomp_syscall_resolve_name(name.encode())
            if nr == -1:  # syscall does not exist on this architecture
                return
            result = sec.seccomp_rule_add_array(ctx, 0x00050000 | error, nr,
                                               int(compare is not None), C.byref(compare) if compare else None)
            if result != 0:
                raise RuntimeError(f"Cannot restrict {name}: {result}")

        deny("socket", Arg(0, 1, socket.AF_UNIX, 0))  # SCMP_CMP_NE
        deny("socketpair", Arg(0, 1, socket.AF_UNIX, 0))
        # Also deny pathname Unix clients (Landlock only scopes abstract socket clients).
        deny("sendto", Arg(4, 1, 0, 0))  # deny a destination supplied with a datagram
        # clone3 stores its flags behind a pointer that classic seccomp cannot inspect.
        # ENOSYS lets libc use clone for ordinary threads/processes, whose flags we can filter.
        deny("clone3", error=errno.ENOSYS)
        # Resource and scheduling syscalls can affect the API without sending a
        # signal. Keep descendants in the job's process group for cancellation.
        deny('prlimit64', Arg(0, 1, 0, 0))
        import platform
        flags_arg = 1 if platform.machine() == "s390x" else 0
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000):
            deny("clone", Arg(flags_arg, 7, flag, flag))  # SCMP_CMP_MASKED_EQ: new namespaces
        for name in ("connect", "sendmsg", "sendmmsg", "io_uring_setup", "io_uring_enter", "io_uring_register",
                     "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
                     "mount", "umount2", "pivot_root", "chroot", "setns", "unshare", "bpf",
                     "open_by_handle_at", "name_to_handle_at", "userfaultfd", "perf_event_open",
                     "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat",
                     "setxattr", "lsetxattr", "fsetxattr", "removexattr", "lremovexattr", "fremovexattr",
                     "utime", "utimes", "futimesat", "utimensat",
                     'setpriority', 'sched_setparam', 'sched_setscheduler', 'sched_setattr',
                     'sched_setaffinity', 'ioprio_set', 'setsid', 'setpgid'):
            deny(name)
        result = sec.seccomp_load(ctx)
        if result != 0:
            raise RuntimeError(f"Cannot install seccomp: {result}")
    finally:
        sec.seccomp_release(ctx)


def main() -> None:
    from .sandbox import limit_self
    limit_self()
    config, module, *args = sys.argv[1:]
    config = json.loads(config)
    writable = Path(config["write"]).resolve()
    scratch = writable / ".sandbox"
    scratch.mkdir(exist_ok=True, mode=0o700)
    os.environ.update(HOME=str(scratch), TMPDIR=str(scratch), XDG_CACHE_HOME=str(scratch / "cache"),
                      OSL_SOCKET_PATH=".", CC_JOB_ISOLATED="landlock")
    # Resolve paths before restricting access; the virtualenv itself may be a symlink.
    restrict(writable, [Path(p) for p in config["read"]], [Path(p) for p in config["dirs"]])
    os.execv(sys.executable, [sys.executable, "-m", module, *args])


if __name__ == "__main__":
    main()
