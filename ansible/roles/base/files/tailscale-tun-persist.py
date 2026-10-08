#!/usr/bin/python3
# Managed by Ansible (roles/base).
"""Mark tailscaled's TUN interface persistent, without restarting tailscaled.

tailscaled creates tailscale0 as an ordinary TUN device, which the kernel
destroys as soon as tailscaled closes it: on every restart, including the one a
Tailscale auto-update does. Destroying it also destroys flannel.1, the VXLAN
device k3s stacks on top of it, and k3s does not recover from that on its own.

A persistent TUN device outlives the process that holds it open. The next
tailscaled attaches to the same interface (same name, same ifindex) instead of
creating a new one, so flannel.1 is never touched.

Only a file descriptor attached to the device can mark it persistent
(TUNSETPERSIST), and the only one is tailscaled's. pidfd_getfd(2) duplicates it
into this process, the ioctl is made on the duplicate, and the duplicate is
closed again; tailscaled itself is not disturbed.

Usage: tailscale-tun-persist [--check] [IFACE]   (IFACE defaults to tailscale0)
  --check  change nothing; report whether IFACE could be marked persistent.
Exits 0 when a process (tailscaled) holds IFACE open and IFACE is (or, with
--check, could be made) persistent; else 1. A persistent IFACE that nothing
holds open also exits 1: the systemd drop-in relies on that to tell a
tailscaled that attached to the kept device from one that failed to.
"""
import ctypes
import fcntl
import os
import sys
import time

IFF_PERSIST = 0x0800
TUNSETPERSIST = 0x400454CB  # _IOW('T', 203, int)
SYS_PIDFD_GETFD = 438       # the same on x86_64 and arm64
WAIT_SECONDS = 10           # tailscaled reports ready before it opens the device


def tun_flags(iface):
    try:
        with open(f"/sys/class/net/{iface}/tun_flags") as f:
            return int(f.read(), 16)
    except FileNotFoundError:
        return None


def find_owner(iface):
    """Return (pid, fd) of a descriptor attached to iface, or None."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") != "/dev/net/tun":
                    continue
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    if f"iff:\t{iface}\n" in f.read():
                        return int(pid), int(fd)
            except OSError:
                continue
    return None


def dup_from(pid, target_fd):
    """Duplicate another process's descriptor into this one (pidfd_getfd)."""
    libc = ctypes.CDLL(None, use_errno=True)
    pidfd = os.pidfd_open(pid)
    try:
        fd = libc.syscall(SYS_PIDFD_GETFD, pidfd, target_fd, 0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), f"pidfd_getfd(pid {pid}, fd {target_fd})")
        return fd
    finally:
        os.close(pidfd)


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    iface = args[0] if args else "tailscale0"

    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        flags = tun_flags(iface)
        owner = find_owner(iface) if flags is not None else None
        if owner or time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    if flags is None:
        print(f"{iface}: no such TUN interface", file=sys.stderr)
        return 1
    if owner is None:
        print(f"{iface}: no process holds it open", file=sys.stderr)
        return 1
    pid, target_fd = owner
    if flags & IFF_PERSIST:
        print(f"{iface}: already persistent (owner pid {pid}, fd {target_fd})")
        return 0

    try:
        fd = dup_from(pid, target_fd)
    except OSError as e:
        print(f"{iface}: {e}", file=sys.stderr)
        return 1
    try:
        if check:
            print(f"{iface}: not persistent; can be marked (owner pid {pid}, fd {target_fd})")
            return 0
        fcntl.ioctl(fd, TUNSETPERSIST, 1)
    finally:
        os.close(fd)

    if not (tun_flags(iface) or 0) & IFF_PERSIST:
        print(f"{iface}: TUNSETPERSIST did not take effect", file=sys.stderr)
        return 1
    print(f"{iface}: marked persistent (owner pid {pid}, fd {target_fd})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
