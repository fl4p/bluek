"""Async L2CAP-LE socket on the ATT channel (CID 0x0004).

CPython's ``socket`` cannot express the extended ``sockaddr_l2`` (it has no
``l2_cid`` / ``l2_bdaddr_type``), so we build the struct by hand and call libc
``bind``/``connect`` on the socket fd via ctypes. The kernel performs the actual
LE connection — we never take exclusive control of the controller, so this
coexists with bluetoothd. This is the ``gatttool``/``btgatt-client`` model.

Note: ``l2_psm``/``l2_cid`` are ``__le16`` in BlueZ; on little-endian hosts
(arm/x86, i.e. every realistic target) the native ``c_ushort`` layout matches.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import socket
from typing import Callable, Optional

from ._util import str_to_bdaddr

AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", 31)
BTPROTO_L2CAP = 0
ATT_CID = 0x0004

# bdaddr types (match the kernel mgmt DEVICE_FOUND address_type for LE).
BDADDR_LE_PUBLIC = 1
BDADDR_LE_RANDOM = 2

_libc = ctypes.CDLL(None, use_errno=True)


class _sockaddr_l2(ctypes.Structure):
    _fields_ = [
        ("l2_family", ctypes.c_ushort),
        ("l2_psm", ctypes.c_ushort),
        ("l2_bdaddr", ctypes.c_ubyte * 6),
        ("l2_cid", ctypes.c_ushort),
        ("l2_bdaddr_type", ctypes.c_ubyte),
    ]


def _make_addr(bdaddr: bytes, bdaddr_type: int) -> _sockaddr_l2:
    a = _sockaddr_l2()
    a.l2_family = AF_BLUETOOTH
    a.l2_psm = 0
    a.l2_cid = ATT_CID
    a.l2_bdaddr = (ctypes.c_ubyte * 6)(*bdaddr)
    a.l2_bdaddr_type = bdaddr_type
    return a


class L2CAPSocket:
    """A connected L2CAP ATT socket with asyncio-driven read/write."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._loop = asyncio.get_event_loop()
        self._on_data: Optional[Callable[[bytes], None]] = None
        self._on_close: Optional[Callable[[Exception | None], None]] = None
        self._closed = False

    @classmethod
    async def connect(
        cls,
        *,
        dst: str,
        dst_type: int,
        src: Optional[str] = None,
        timeout: float = 10.0,
    ) -> "L2CAPSocket":
        """Open and connect an ATT L2CAP socket to ``dst`` (a ``"AA:BB:.."`` MAC).

        ``src`` selects the local adapter by its BD_ADDR; ``None`` binds to
        BDADDR_ANY and lets the kernel pick the default controller.
        """
        loop = asyncio.get_event_loop()
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        s.setblocking(False)
        try:
            src_bytes = str_to_bdaddr(src) if src else bytes(6)
            src_addr = _make_addr(src_bytes, BDADDR_LE_PUBLIC)
            if _libc.bind(s.fileno(), ctypes.byref(src_addr), ctypes.sizeof(src_addr)) != 0:
                e = ctypes.get_errno()
                raise OSError(e, os.strerror(e), f"l2cap bind to {src}")

            dst_addr = _make_addr(str_to_bdaddr(dst), dst_type)
            rc = _libc.connect(s.fileno(), ctypes.byref(dst_addr), ctypes.sizeof(dst_addr))
            if rc != 0:
                e = ctypes.get_errno()
                if e not in (errno.EINPROGRESS, errno.EALREADY, errno.EAGAIN):
                    raise OSError(e, os.strerror(e), f"l2cap connect to {dst}")
                await cls._wait_connected(loop, s, timeout)
        except BaseException:
            s.close()
            raise
        return cls(s)

    @staticmethod
    async def _wait_connected(loop, s, timeout):
        fut = loop.create_future()

        def _writable():
            if not fut.done():
                fut.set_result(None)

        loop.add_writer(s.fileno(), _writable)
        try:
            await asyncio.wait_for(fut, timeout)
        finally:
            loop.remove_writer(s.fileno())
        err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            raise OSError(err, os.strerror(err), "l2cap connect")

    # -- I/O ---------------------------------------------------------------
    def start_reader(
        self,
        on_data: Callable[[bytes], None],
        on_close: Optional[Callable[[Exception | None], None]] = None,
    ) -> None:
        self._on_data = on_data
        self._on_close = on_close
        self._loop.add_reader(self._sock.fileno(), self._read_ready)

    def _read_ready(self) -> None:
        try:
            data = self._sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            self._fail(e)
            return
        if not data:  # peer disconnected
            self._fail(None)
            return
        if self._on_data is not None:
            self._on_data(data)

    async def send(self, data: bytes) -> None:
        """Send one ATT PDU as a single L2CAP SDU (preserves message boundary)."""
        while True:
            try:
                self._sock.send(data)
                return
            except (BlockingIOError, InterruptedError):
                fut = self._loop.create_future()
                self._loop.add_writer(self._sock.fileno(), lambda: fut.done() or fut.set_result(None))
                try:
                    await fut
                finally:
                    self._loop.remove_writer(self._sock.fileno())

    def _fail(self, exc: Optional[Exception]) -> None:
        if self._closed:
            return
        cb = self._on_close
        self.close()
        if cb is not None:
            cb(exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self._sock.fileno())
        except (OSError, ValueError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed
