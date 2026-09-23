"""Wake-on-LAN: magic-пакеты из сети, где крутится бот."""

from __future__ import annotations

import socket

WOL_PORT = 9


def build_magic_packet(mac: str) -> bytes:
    clean = mac.lower().replace('-', '').replace(':', '').strip()
    if len(clean) != 12:
        raise ValueError(f'«{mac}» не похоже на MAC-адрес')
    try:
        mac_bytes = bytes.fromhex(clean)
    except ValueError as exc:
        raise ValueError(f'«{mac}» не похоже на MAC-адрес') from exc
    if mac_bytes == b'\x00' * 6:
        raise ValueError('Нулевой MAC — пробуждать некого')
    return b'\xff' * 6 + mac_bytes * 16


def send_wol(mac: str, broadcast: str = '255.255.255.255', port: int = WOL_PORT) -> None:
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
