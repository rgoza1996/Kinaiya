"""Linux syscall network fence, applied before optional/native imports.

This blocks new network channels and send-family/io_uring bypasses. It is not
a general hostile-code, filesystem or host-administrator sandbox.
"""

import ctypes
import errno
import platform
import socket
import sys

BLOCKED = (
    "socket",
    "socketpair",
    "connect",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "socketcall",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "pidfd_getfd",
)
REQUIRED = {
    "socket",
    "socketpair",
    "connect",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "pidfd_getfd",
}


def deny_network():
    if sys.platform != "linux" or platform.machine() != "x86_64" or sys.byteorder != "little":
        raise RuntimeError("Pinned Whistle isolation requires Linux x86_64")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise RuntimeError("Whistle no-new-privileges isolation unavailable")
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    seccomp.seccomp_rule_add_array.restype = ctypes.c_int
    seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    seccomp.seccomp_attr_set.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    policy = seccomp.seccomp_init(0x7FFF0000)
    if not policy:
        raise RuntimeError("Whistle seccomp initialization failed")
    try:
        if seccomp.seccomp_attr_set(policy, 4, 1) != 0:  # SCMP_FLTATR_CTL_TSYNC
            raise RuntimeError("Whistle seccomp thread synchronization unavailable")
        for name in BLOCKED:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                if name in REQUIRED:
                    raise RuntimeError("Required Whistle isolation syscall is unavailable")
                continue
            if (
                seccomp.seccomp_rule_add_array(policy, 0x00050000 | errno.EPERM, number, 0, None)
                != 0
            ):
                raise RuntimeError("Whistle isolation rule failed")
        if seccomp.seccomp_load(policy) != 0:
            raise RuntimeError("Whistle isolation could not be installed")
    finally:
        seccomp.seccomp_release(policy)
    for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
        try:
            connection = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            if exc.errno != errno.EPERM:
                raise RuntimeError("Whistle isolation probe was inconclusive") from exc
        else:
            connection.close()
            raise RuntimeError("Whistle isolation probe failed")
    return "seccomp-network-v1"
