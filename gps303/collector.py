""" TCP server that communicates with terminals """

from configparser import ConfigParser
from importlib import import_module
from logging import getLogger
from os import umask
from socket import (
    socket,
    AF_INET6,
    SOCK_STREAM,
    SOL_SOCKET,
    SO_KEEPALIVE,
    SO_REUSEADDR,
)
from struct import pack
from time import time
from typing import Any, cast, Dict, List, Optional, Tuple, Union
import zmq

from . import common
from .zmsg import Bcast, Resp

log = getLogger("gps303/collector")

MAXBUFFER: int = 4096


class ProtoModule:
    class Stream:
        @staticmethod
        def enframe(buffer: bytes) -> bytes:
            ...

        def recv(self, segment: bytes) -> List[Union[bytes, str]]:
            ...

        def close(self) -> bytes:
            ...

    @staticmethod
    def probe_buffer(buffer: bytes) -> bool:
        ...

    @staticmethod
    def parse_message(packet: bytes, is_incoming: bool = True) -> Any:
        ...

    @staticmethod
    def inline_response(packet: bytes) -> Optional[bytes]:
        ...

    @staticmethod
    def is_goodbye_packet(packet: bytes) -> bool:
        ...

    @staticmethod
    def imei_from_packet(packet: bytes) -> Optional[str]:
        ...

    @staticmethod
    def proto_of_message(packet: bytes) -> str:
        ...

    @staticmethod
    def proto_by_name(name: str) -> int:
        ...


pmods: List[ProtoModule] = []


class Client:
    """Connected socket to the terminal plus buffer and metadata"""

    def __init__(self, sock: socket, addr: Tuple[str, int]) -> None:
        self.sock = sock
        self.addr = addr
        self.pmod: Optional[ProtoModule] = None
        self.stream: Optional[ProtoModule.Stream] = None
        self.imei: Optional[str] = None

    def close(self) -> None:
        log.debug("Closing fd %d (IMEI %s)", self.sock.fileno(), self.imei)
        self.sock.close()
        if self.stream:
            rest = self.stream.close()
        else:
            rest = b""
        if rest:
            log.warning(
                "%d bytes in buffer on close: %s", len(rest), rest[:64].hex()
            )

    def recv(self) -> Optional[List[Tuple[float, Tuple[str, int], bytes]]]:
        """Read from the socket and parse complete messages"""
        try:
            segment = self.sock.recv(MAXBUFFER)
        except OSError as e:
            log.warning(
                "Reading from fd %d (IMEI %s): %s",
                self.sock.fileno(),
                self.imei,
                e,
            )
            return None
        if not segment:  # Terminal has closed connection
            log.info(
                "EOF reading from fd %d (IMEI %s)",
                self.sock.fileno(),
                self.imei,
            )
            return None
        if self.stream is None:
            for pmod in pmods:
                if pmod.probe_buffer(segment):
                    self.pmod = pmod
                    self.stream = pmod.Stream()
                    break
        if self.stream is None:
            log.info(
                "unrecognizable %d bytes of data %s from fd %d",
                len(segment),
                segment[:32].hex(),
                self.sock.fileno(),
            )
            return []
        when = time()
        msgs = []
        for elem in self.stream.recv(segment):
            if isinstance(elem, bytes):
                msgs.append((when, self.addr, elem))
            else:
                log.warning(
                    "%s from fd %d (IMEI %s)",
                    elem,
                    self.sock.fileno(),
                    self.imei,
                )
        return msgs

    def send(self, buffer: bytes) -> None:
        assert self.stream is not None
        try:
            self.sock.send(self.stream.enframe(buffer))
        except OSError as e:
            log.error(
                "Sending to fd %d (IMEI %s): %s",
                self.sock.fileno(),
                self.imei,
                e,
            )


class Clients:
    def __init__(self) -> None:
        self.by_fd: Dict[int, Client] = {}
        self.by_imei: Dict[str, Client] = {}

    def add(self, clntsock: socket, clntaddr: Tuple[str, int]) -> int:
        fd = clntsock.fileno()
        log.info("Start serving fd %d from %s", fd, clntaddr)
        self.by_fd[fd] = Client(clntsock, clntaddr)
        return fd

    def stop(self, fd: int) -> None:
        clnt = self.by_fd[fd]
        log.info("Stop serving fd %d (IMEI %s)", clnt.sock.fileno(), clnt.imei)
        clnt.close()
        if clnt.imei:
            del self.by_imei[clnt.imei]
        del self.by_fd[fd]

    def recv(
        self, fd: int
    ) -> Optional[
        List[Tuple[ProtoModule, Optional[str], float, Tuple[str, int], bytes]]
    ]:
        clnt = self.by_fd[fd]
        msgs = clnt.recv()
        if msgs is None:
            return None
        result = []
        for when, peeraddr, packet in msgs:
            assert clnt.pmod is not None
            if clnt.imei is None:
                imei = clnt.pmod.imei_from_packet(packet)
                if imei is not None:
                    log.info("LOGIN from fd %d (IMEI %s)", fd, imei)
                    clnt.imei = imei
                    oldclnt = self.by_imei.get(clnt.imei)
                    if oldclnt is not None:
                        log.info(
                            "Orphaning fd %d with the same IMEI",
                            oldclnt.sock.fileno(),
                        )
                        oldclnt.imei = None
                    self.by_imei[clnt.imei] = clnt
                else:
                    log.warning(
                        "Login message from %s: %s, but client imei unfilled",
                        peeraddr,
                        packet,
                    )
            result.append((clnt.pmod, clnt.imei, when, peeraddr, packet))
            log.debug(
                "Received from %s (IMEI %s): %s",
                peeraddr,
                clnt.imei,
                packet.hex(),
            )
        return result

    def response(self, resp: Resp) -> Optional[ProtoModule]:
        if resp.imei in self.by_imei:
            clnt = self.by_imei[resp.imei]
            clnt.send(resp.packet)
            return clnt.pmod
        else:
            log.info("Not connected (IMEI %s)", resp.imei)
            return None


def runserver(conf: ConfigParser, handle_hibernate: bool = True) -> None:
    global pmods
    pmods = [
        cast(ProtoModule, import_module("." + modnm, __package__))
        for modnm in conf.get("collector", "protocols").split(",")
    ]
    # Is this https://github.com/zeromq/pyzmq/issues/1627 still not fixed?!
    zctx = zmq.Context()  # type: ignore
    zpub = zctx.socket(zmq.PUB)  # type: ignore
    zpull = zctx.socket(zmq.PULL)  # type: ignore
    oldmask = umask(0o117)
    zpub.bind(conf.get("collector", "publishurl"))
    zpull.bind(conf.get("collector", "listenurl"))
    umask(oldmask)
    tcpl = socket(AF_INET6, SOCK_STREAM)
    tcpl.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    tcpl.bind(("", conf.getint("collector", "port")))
    tcpl.listen(5)
    tcpfd = tcpl.fileno()
    poller = zmq.Poller()  # type: ignore
    poller.register(zpull, flags=zmq.POLLIN)
    poller.register(tcpfd, flags=zmq.POLLIN)
    clients = Clients()
    try:
        while True:
            tosend = []
            topoll = []
            tostop = []
            events = poller.poll(1000)
            for sk, fl in events:
                if sk is zpull:
                    while True:
                        try:
                            msg = zpull.recv(zmq.NOBLOCK)
                            zmsg = Resp(msg)
                            tosend.append(zmsg)
                        except zmq.Again:
                            break
                elif sk == tcpfd:
                    clntsock, clntaddr = tcpl.accept()
                    clntsock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
                    topoll.append((clntsock, clntaddr))
                elif fl & zmq.POLLIN:
                    received = clients.recv(sk)
                    if received is None:
                        log.debug("Terminal gone from fd %d", sk)
                        tostop.append(sk)
                    else:
                        for pmod, imei, when, peeraddr, packet in received:
                            proto = pmod.proto_of_message(packet)
                            zpub.send(
                                Bcast(
                                    proto=proto,
                                    imei=imei,
                                    when=when,
                                    peeraddr=peeraddr,
                                    packet=packet,
                                ).packed
                            )
                            if (
                                pmod.is_goodbye_packet(packet)
                                and handle_hibernate
                            ):
                                log.debug(
                                    "Goodbye from fd %d (IMEI %s)",
                                    sk,
                                    imei,
                                )
                                tostop.append(sk)
                            respmsg = pmod.inline_response(packet)
                            if respmsg is not None:
                                tosend.append(
                                    Resp(imei=imei, when=when, packet=respmsg)
                                )
                else:
                    log.debug("Stray event: %s on socket %s", fl, sk)
            # poll queue consumed, make changes now
            for zmsg in tosend:
                log.debug("Sending to the client: %s", zmsg)
                rpmod = clients.response(zmsg)
                if rpmod is not None:
                    zpub.send(
                        Bcast(
                            is_incoming=False,
                            proto=rpmod.proto_of_message(zmsg.packet),
                            when=zmsg.when,
                            imei=zmsg.imei,
                            packet=zmsg.packet,
                        ).packed
                    )
            for fd in tostop:
                poller.unregister(fd)  # type: ignore
                clients.stop(fd)
            for clntsock, clntaddr in topoll:
                fd = clients.add(clntsock, clntaddr)
                poller.register(fd, flags=zmq.POLLIN)
    except KeyboardInterrupt:
        zpub.close()
        zpull.close()
        zctx.destroy()  # type: ignore
        tcpl.close()


if __name__.endswith("__main__"):
    runserver(common.init(log))
