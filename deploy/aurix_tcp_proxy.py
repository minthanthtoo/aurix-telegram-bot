#!/usr/bin/env python3
"""Small TCP/UDP relay for Outline management and data-plane access."""

from __future__ import annotations

import argparse
import socket
import threading


def _pipe(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                return
            destination.sendall(chunk)
    except OSError:
        return
    finally:
        for sock in (source, destination):
            try:
                sock.close()
            except OSError:
                pass


def _udp_relay(
    listen_host: str, listen_port: int, target_host: str, target_port: int
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((listen_host, listen_port))

    clients: dict[tuple[str, int], socket.socket] = {}
    clients_lock = threading.Lock()

    def forward_replies(
        client_addr: tuple[str, int], upstream: socket.socket
    ) -> None:
        try:
            while True:
                packet = upstream.recv(65536)
                listener.sendto(packet, client_addr)
        except OSError:
            return
        finally:
            with clients_lock:
                if clients.get(client_addr) is upstream:
                    clients.pop(client_addr, None)
            try:
                upstream.close()
            except OSError:
                pass

    while True:
        packet, client_addr = listener.recvfrom(65536)
        with clients_lock:
            upstream = clients.get(client_addr)
            if upstream is None:
                upstream = None
                try:
                    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    upstream.settimeout(60)
                    upstream.connect((target_host, target_port))
                except OSError:
                    if upstream is not None:
                        upstream.close()
                    continue
                clients[client_addr] = upstream
                threading.Thread(
                    target=forward_replies,
                    args=(client_addr, upstream),
                    daemon=True,
                ).start()
        try:
            upstream.send(packet)
        except OSError:
            with clients_lock:
                clients.pop(client_addr, None)
            upstream.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument(
        "--udp",
        action="store_true",
        help="relay UDP datagrams instead of TCP streams",
    )
    args = parser.parse_args()

    if args.udp:
        _udp_relay(
            args.listen_host,
            args.listen_port,
            args.target_host,
            args.target_port,
        )
        return

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.listen_host, args.listen_port))
    listener.listen(128)

    while True:
        client, _ = listener.accept()
        try:
            upstream = socket.create_connection(
                (args.target_host, args.target_port), timeout=8
            )
        except OSError:
            client.close()
            continue

        threading.Thread(
            target=_pipe, args=(client, upstream), daemon=True
        ).start()
        threading.Thread(
            target=_pipe, args=(upstream, client), daemon=True
        ).start()


if __name__ == "__main__":
    main()
