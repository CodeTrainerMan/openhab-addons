#!/usr/bin/env python3
"""Minimal UPnP device simulator used to verify openHAB pioneeravr discovery.

It advertises a fake UPnP MediaRenderer whose manufacturer is PIONEER and whose
modelName is configurable (default: VSX-924). openHAB's
PioneerAvrDiscoveryParticipant only looks at those two fields plus the device
type, so no real hardware is needed to exercise the model -> thing type mapping.

Usage:
    python pioneer_upnp_sim.py --model VSX-924 --http-port 9000
"""

import argparse
import http.server
import socket
import socketserver
import struct
import threading
import time
import uuid

SSDP_MULTICAST = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
UDN = "uuid:8f5c2e10-0000-4000-8000-pioneersim0001"


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def device_xml(model: str, base: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>{base}</URLBase>
  <device>
    <deviceType>{DEVICE_TYPE}</deviceType>
    <friendlyName>Pioneer {model} (simulator)</friendlyName>
    <manufacturer>PIONEER</manufacturer>
    <manufacturerURL>http://www.pioneer.com</manufacturerURL>
    <modelDescription>Pioneer AVR simulator</modelDescription>
    <modelName>{model}</modelName>
    <modelNumber>{model}</modelNumber>
    <serialNumber>SIM0000001</serialNumber>
    <UDN>{UDN}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/RenderingControl.xml</SCPDURL>
        <controlURL>/RenderingControl/control</controlURL>
        <eventSubURL>/RenderingControl/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
""".encode("utf-8")


class DescriptionHandler(http.server.BaseHTTPRequestHandler):
    model = "VSX-924"
    base_url = "http://127.0.0.1:9000"

    def do_GET(self):
        body = device_xml(self.model, self.base_url)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("SERVER", "Linux/3.14.0 UPnP/1.0 PioneerAVR/1.0")
        self.send_header("EXT", "")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}")


def build_notify(ip: str, http_port: int) -> bytes:
    location = f"http://{ip}:{http_port}/device.xml"
    server = "Linux/3.14.0 UPnP/1.0 PioneerAVR/1.0"
    nts = [
        ("upnp:rootdevice", f"{UDN}::upnp:rootdevice"),
        (UDN, UDN),
        (DEVICE_TYPE, f"{UDN}::{DEVICE_TYPE}"),
    ]
    msg = ""
    for nt, usn in nts:
        msg += (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_MULTICAST}:{SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            f"NT: {nt}\r\n"
            "NTS: ssdp:alive\r\n"
            f"SERVER: {server}\r\n"
            f"USN: {usn}\r\n\r\n"
        )
    return msg.encode("utf-8")


def notify_loop(ip: str, http_port: int, interval: int, stop: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 4))
    payload = build_notify(ip, http_port)
    while not stop.is_set():
        try:
            sock.sendto(payload, (SSDP_MULTICAST, SSDP_PORT))
            print(f"[ssdp] sent ssdp:alive for http://{ip}:{http_port}/device.xml")
        except OSError as exc:
            print(f"[ssdp] send failed: {exc}")
        stop.wait(interval)
    sock.close()


def search_responder(ip: str, http_port: int, stop: threading.Event):
    """Answer SSDP M-SEARCH queries (best effort; may fail if port 1900 is busy)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", SSDP_PORT))
        mreq = struct.pack("4sL", socket.inet_aton(SSDP_MULTICAST), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        print(f"[ssdp] cannot bind 1900 ({exc}); relying on NOTIFY only")
        return

    location = f"http://{ip}:{http_port}/device.xml"
    server = "Linux/3.14.0 UPnP/1.0 PioneerAVR/1.0"
    while not stop.is_set():
        try:
            sock.settimeout(1.0)
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode("utf-8", errors="ignore")
        if "M-SEARCH" not in text:
            continue
        st = ""
        for line in text.split("\r\n"):
            if line.upper().startswith("ST:"):
                st = line[3:].strip()
        if st not in ("ssdp:all", "upnp:rootdevice", DEVICE_TYPE, UDN):
            continue
        reply = (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            f"SERVER: {server}\r\n"
            f"ST: {st if st else DEVICE_TYPE}\r\n"
            f"USN: {UDN}::{st if st else DEVICE_TYPE}\r\n"
            "EXT:\r\n\r\n"
        ).encode("utf-8")
        try:
            sock.sendto(reply, addr)
            print(f"[ssdp] answered M-SEARCH ST={st} from {addr[0]}")
        except OSError as exc:
            print(f"[ssdp] reply failed: {exc}")
    sock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="VSX-924")
    parser.add_argument("--http-port", type=int, default=9000)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    ip = local_ip()
    base_url = f"http://{ip}:{args.http_port}"
    handler = type("BoundHandler", (DescriptionHandler,), {"model": args.model, "base_url": base_url})
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("0.0.0.0", args.http_port), handler)

    stop = threading.Event()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=search_responder, args=(ip, args.http_port, stop), daemon=True).start()

    print(f"[sim] model={args.model} ip={ip} description={base_url}/device.xml")
    try:
        notify_loop(ip, args.http_port, args.interval, stop)
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()
