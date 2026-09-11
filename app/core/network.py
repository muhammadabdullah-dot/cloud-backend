"""LAN IP detection for the startup banner — no packet is actually sent; connecting a UDP socket
just makes the OS pick which local interface/address would route to that destination."""
import socket


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
