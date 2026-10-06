#!/usr/bin/env python3
"""Forward the bHaptics Player loopback ports to a daemon on another machine.

Games and VRChat OSC hardcode 127.0.0.1 for the bHaptics Player, so they cannot
see a daemon running on a different device -- for example the vest's host device
while the game is streamed from this PC over Steam Remote Play. Run this on the
gaming PC: it listens on the usual loopback ports and forwards them to the
remote daemon, so unmodified games just work.

With no --host the daemon is found automatically over UDP broadcast, provided it
was started with Remote Play enabled (non-loopback bind) -- see the README. Use
--list to see what is on the LAN. VRChat can skip the relay entirely by pointing
OSC straight at the daemon with its launch option `--osc=9000:<daemon-ip>:9001`.
Standard library only.
"""
import argparse
import asyncio
import contextlib
import logging
import secrets
import socket
import sys

log = logging.getLogger("remote-relay")

DEFAULT_TCP_PORTS = (15881, 15882)
DEFAULT_UDP_PORTS = (9001,)
DISCOVERY_PORT = 15880
DISCOVERY_PREFIX = "BHAPTICS-LINUX/1"


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _tcp_client(reader, writer, host, port):
    peer = writer.get_extra_info("peername")
    try:
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except OSError as e:
        log.warning("tcp %s -> %s:%d failed: %s", peer, host, port, e)
        writer.close()
        return
    log.info("tcp %s -> %s:%d", peer, host, port)
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


async def _serve_tcp(bind, host, port):
    server = await asyncio.start_server(
        lambda r, w: _tcp_client(r, w, host, port), bind, port)
    log.info("tcp %s:%d -> %s:%d", bind, port, host, port)
    return server


class _DiscoveryClient(asyncio.DatagramProtocol):
    def __init__(self, nonce):
        self.nonce = nonce
        self.offers = {}

    def datagram_received(self, data, addr):
        parts = data.decode(errors="replace").split()
        if len(parts) != 7 or parts[0] != DISCOVERY_PREFIX or parts[1] != "OFFER":
            return
        if parts[2] != self.nonce or addr[0] in ("127.0.0.1", "::1"):
            return
        self.offers[addr[0]] = (parts[3], parts[4:])


async def _discover(timeout, targets=("255.255.255.255",)):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    nonce = secrets.token_hex(8)
    proto = _DiscoveryClient(nonce)
    transport, _ = await loop.create_datagram_endpoint(lambda: proto, sock=sock)
    probe = f"{DISCOVERY_PREFIX} DISCOVER {nonce}".encode()
    try:
        for target in targets:
            with contextlib.suppress(OSError):
                transport.sendto(probe, (target, DISCOVERY_PORT))
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return proto.offers


class _UdpReply(asyncio.DatagramProtocol):
    def __init__(self, forwarder):
        self.forwarder = forwarder

    def datagram_received(self, data, _addr):
        self.forwarder.reply(data)


class _UdpForwarder(asyncio.DatagramProtocol):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.transport = None
        self.upstream = None
        self.last_client = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.last_client = addr
        if self.upstream is not None:
            self.upstream.sendto(data, (self.host, self.port))

    def reply(self, data):
        if self.last_client is not None and self.transport is not None:
            self.transport.sendto(data, self.last_client)


async def _serve_udp(bind, host, port):
    loop = asyncio.get_running_loop()
    forwarder = _UdpForwarder(host, port)
    upstream, _ = await loop.create_datagram_endpoint(
        lambda: _UdpReply(forwarder), remote_addr=(host, port))
    forwarder.upstream = upstream
    listen, _ = await loop.create_datagram_endpoint(
        lambda: forwarder, local_addr=(bind, port))
    log.info("udp %s:%d -> %s:%d", bind, port, host, port)
    return listen, upstream


def _ports(text):
    return [int(p) for p in text.split(",") if p.strip()]


async def _resolve_host(args):
    if args.host:
        return args.host
    log.info("discovering bhaptics-linux on the LAN (%.0fs)...", args.discover_timeout)
    offers = await _discover(args.discover_timeout)
    if not offers:
        log.error("no daemon found. Is Remote Play enabled on the device "
                  "(network.json bind)? Otherwise pass --host <ip>.")
        return None
    if len(offers) > 1:
        for ip, offer in sorted(offers.items()):
            log.info("found %s at %s", offer[0], ip)
        log.error("multiple daemons found; pick one with --host <ip>")
        return None
    ip = next(iter(offers))
    log.info("found %s at %s", offers[ip][0], ip)
    return ip


async def _list_daemons(args):
    offers = await _discover(args.discover_timeout)
    if not offers:
        log.error("no bhaptics-linux daemon found on the LAN")
        return 1
    for ip, (name, ports) in sorted(offers.items()):
        log.info("found %s at %s (ports %s)", name, ip, "/".join(ports))
    return 0


async def run(args):
    if args.list:
        return await _list_daemons(args)
    host = await _resolve_host(args)
    if host is None:
        return 1
    tcp = [await _serve_tcp(args.bind, host, p) for p in args.tcp_ports]
    udp = [await _serve_udp(args.bind, host, p) for p in args.udp_ports]
    log.info("relaying to %s — keep this running while you play", host)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*(s.serve_forever() for s in tcp))
    for transport in udp:
        transport.close()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Forward bHaptics loopback ports to a daemon on another machine.")
    parser.add_argument("--host", help="IP of the device running bhaptics-linux "
                                       "(omit to auto-discover on the LAN)")
    parser.add_argument("--list", action="store_true", help="discover daemons and exit")
    parser.add_argument("--discover-timeout", type=float, default=2.0,
                        help="seconds to wait for discovery replies (default 2)")
    parser.add_argument("--bind", default="127.0.0.1", help="local address to listen on")
    parser.add_argument("--tcp-ports", type=_ports, default=None,
                        help="comma-separated SDK ports (default 15881,15882)")
    parser.add_argument("--udp-ports", type=_ports, default=None,
                        help="comma-separated OSC ports (default 9001)")
    parser.add_argument("--no-udp", action="store_true", help="relay TCP only")
    args = parser.parse_args()
    if args.tcp_ports is None:
        args.tcp_ports = list(DEFAULT_TCP_PORTS)
    if args.no_udp:
        args.udp_ports = []
    elif args.udp_ports is None:
        args.udp_ports = list(DEFAULT_UDP_PORTS)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
