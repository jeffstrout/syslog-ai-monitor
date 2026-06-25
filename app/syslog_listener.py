"""Asyncio syslog receiver — listens on UDP and TCP and stores parsed lines.

Handles the two common framings the UDM Pro / UniFi devices emit:
  * RFC 3164 (BSD):   <PRI>Mmm dd HH:MM:SS host tag: message
  * RFC 5424:         <PRI>1 TIMESTAMP host app procid msgid ... message
We only need PRI (facility/severity), host, and the message body; the rest is
forwarded verbatim as the message text so nothing is lost before evaluation.
"""
from __future__ import annotations

import asyncio
import logging
import re

from . import db

log = logging.getLogger("syslog")

# <PRI> is facility*8 + severity. Capture it plus everything after.
_PRI_RE = re.compile(r"^<(\d{1,3})>(.*)$", re.DOTALL)
# RFC 3164 header:  Mmm dd HH:MM:SS host ...
_RFC3164_RE = re.compile(
    r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+(\S+)\s+(.*)$", re.DOTALL
)
# RFC 5424 header:  1 TIMESTAMP host app ...
_RFC5424_RE = re.compile(r"^1\s+\S+\s+(\S+)\s+(.*)$", re.DOTALL)


def parse_line(raw: str) -> tuple[str | None, int | None, int | None, str]:
    """Return (host, facility, severity, message)."""
    line = raw.strip()
    if not line:
        return None, None, None, ""

    facility = severity = None
    body = line

    m = _PRI_RE.match(line)
    if m:
        pri = int(m.group(1))
        facility, severity = pri // 8, pri % 8
        body = m.group(2).strip()

    host = None
    m3 = _RFC3164_RE.match(body)
    m5 = _RFC5424_RE.match(body)
    if m5:
        host, body = m5.group(1), m5.group(2).strip()
    elif m3:
        host, body = m3.group(1), m3.group(2).strip()

    if host in ("-", ""):
        host = None
    return host, facility, severity, body


def _store(raw: str) -> None:
    for piece in raw.replace("\r", "\n").split("\n"):
        if piece.strip():
            host, facility, severity, message = parse_line(piece)
            if message:
                db.insert_log(host, facility, severity, message)


class _UDPProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:
        try:
            _store(data.decode("utf-8", "replace"))
        except Exception:  # never let one bad packet kill the listener
            log.exception("failed to handle UDP datagram from %s", addr)


async def _handle_tcp(reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                _store(line.decode("utf-8", "replace"))
            except Exception:
                log.exception("failed to handle TCP line")
    finally:
        writer.close()


async def start(port: int) -> None:
    """Start UDP + TCP syslog servers; runs for the lifetime of the loop."""
    loop = asyncio.get_running_loop()

    await loop.create_datagram_endpoint(
        _UDPProtocol, local_addr=("0.0.0.0", port)
    )
    log.info("syslog UDP listening on 0.0.0.0:%d", port)

    server = await asyncio.start_server(_handle_tcp, "0.0.0.0", port)
    log.info("syslog TCP listening on 0.0.0.0:%d", port)

    # Keep the TCP server serving forever.
    async with server:
        await server.serve_forever()
