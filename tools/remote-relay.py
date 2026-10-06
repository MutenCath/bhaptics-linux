#!/usr/bin/env python3
"""Forward the bHaptics Player loopback ports to a daemon on another machine.

Games and VRChat OSC hardcode 127.0.0.1 for the bHaptics Player, so they cannot
see a daemon running on a different device -- for example the vest's host device
while the game is streamed from this PC over Steam Remote Play. Run this on the
gaming PC: it listens on the usual loopback ports and forwards them to the
remote daemon, so unmodified games just work.

The daemon must accept LAN connections (it is loopback-only by default). On the
host device:

    BHAPTICS_BIND=0.0.0.0 ./install.sh
    # or ~/.config/bhaptics-linux/network.json  {"bind": "0.0.0.0"}

Those endpoints are unauthenticated, so only use this on a trusted LAN. VRChat
can skip the relay entirely by pointing OSC straight at the daemon with its
launch option `--osc=9000:<daemon-ip>:9001`. Standard library only.
"""
import argparse
import asyncio
import contextlib
import logging

log = logging.getLogger("remote-relay")

DEFAULT_TCP_PORTS = (15881, 15882)
DEFAULT_UDP_PORTS = (9001,)


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


async def run(args):
    tcp = [await _serve_tcp(args.bind, args.host, p) for p in args.tcp_ports]
    udp = [await _serve_udp(args.bind, args.host, p) for p in args.udp_ports]
    log.info("relaying to %s — keep this running while you play", args.host)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*(s.serve_forever() for s in tcp))
    for transport in udp:
        transport.close()


def main():
    parser = argparse.ArgumentParser(
        description="Forward bHaptics loopback ports to a daemon on another machine.")
    parser.add_argument("--host", required=True,
                        help="IP of the device running bhaptics-linux")
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
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
