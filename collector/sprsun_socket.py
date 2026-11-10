#!/usr/bin/env python3
"""SPRSUN live socket reader (standard library only, read-only).

The app receives your device list and live readings over a websocket. This connects the
same way, signs in with your uid + token, and listens. It sends only:
  - Authenticate            (sign in)
  - DeviceAttrStatusQuery   (ask for current readings)
  - Pong                    (keep-alive reply)
It never sends DeviceSendCommand or any control message, so it cannot change a unit.

wss://m2m.sprsun.com:447  (set in config under cloud.socket_url)
"""
import base64
import json
import os
import socket
import ssl
import struct
import time

APP_SECRET_DEFAULT = "1995995f02d056277d88c59e346e86e0"


class SocketError(Exception):
    pass


def _parse_wss(url):
    # wss://host:port/path
    rest = url.split("://", 1)[-1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return host, int(port or 443), "/" + path


class WSClient:
    """Minimal RFC6455 client over TLS. Text frames only, plus ping/pong/close."""

    def __init__(self, url, timeout=15, verify=True):
        self.host, self.port, self.path = _parse_wss(url)
        self.timeout = timeout
        self.verify = verify
        self.sock = None
        self.buf = b""

    def connect(self):
        raw = socket.create_connection((self.host, self.port), self.timeout)
        ctx = ssl.create_default_context()
        if not self.verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (self.path, self.host, self.port, key)
        self.sock.sendall(handshake.encode())
        # Read until end of HTTP headers.
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise SocketError("Websocket handshake failed: %s" % head[:120])

    def _fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise SocketError("Connection closed by server")
        self.buf += chunk

    def send_text(self, text):
        data = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        """Return the next text message, or None on ping/other handled control frames."""
        b0, b1 = self._read_exact(2)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        payload = self._read_exact(length) if length else b""
        if opcode == 0x8:  # close
            raise SocketError("Server closed the websocket")
        if opcode == 0x9:  # ping -> pong
            self._send_control(0xA, payload)
            return None
        if opcode == 0xA:  # pong
            return None
        if opcode in (0x1, 0x0):  # text / continuation
            return payload.decode("utf-8", "replace")
        return None

    def _send_control(self, opcode, payload=b""):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked)

    def close(self):
        try:
            if self.sock:
                self._send_control(0x8)
                self.sock.close()
        except OSError:
            pass
        finally:
            self.sock = None


def authenticate_message(app_key, app_secret, uid, token):
    return json.dumps({
        "cmd": "Authenticate", "sn": 1,
        "app_info": {"app_key": app_key, "app_secret": app_secret},
        "user_info": {"uid": uid, "token": token},
        "log_clear": False, "lan_disable": False, "diff_report": False,
        "targeting_push": True, "no_devStatus_after_DevList": True,
    })


def collect(url, app_key, app_secret, uid, token, seconds=15, verify=True, on_message=None):
    """Sign in and listen. Returns (messages, devices, readings).
    devices: list of {device_key, device_mac, product_key, ...} seen in any message.
    readings: {device_key: {attr: value}} from attr_status payloads."""
    ws = WSClient(url, timeout=min(seconds, 20), verify=verify)
    ws.connect()
    ws.send_text(authenticate_message(app_key, app_secret, uid, token))

    messages, devices, readings = [], {}, {}
    queried = set()
    deadline = time.time() + seconds

    def note_device(d):
        key = d.get("device_key")
        if key and key not in devices:
            devices[key] = {k: d.get(k) for k in ("device_key", "device_mac", "product_key", "device_name", "status")}

    def scan(obj):
        if isinstance(obj, dict):
            if "device_key" in obj:
                note_device(obj)
            for v in obj.values():
                scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)

    while time.time() < deadline:
        try:
            text = ws.recv()
        except socket.timeout:
            break
        if text is None:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            continue
        messages.append(msg)
        if on_message:
            on_message(msg)
        scan(msg)
        # Capture attribute values from any message that carries them
        # (PushDeviceAttrStatus = changed values, DeviceAttrStatusQueryAck = full current state).
        if "attr_status" in msg:
            key = msg.get("device_key")
            data = (msg.get("attr_status") or {}).get("data") or {}
            if key and isinstance(data, dict):
                readings.setdefault(key, {}).update(data)
        # Once we know devices, ask each for its full current readings (read-only query).
        for key, dev in list(devices.items()):
            if key not in queried:
                ws.send_text(json.dumps({
                    "cmd": "DeviceAttrStatusQuery", "sn": 1, "device_key": key,
                    "device_mac": dev.get("device_mac"), "product_key": dev.get("product_key"),
                }))
                queried.add(key)
    ws.close()
    return messages, devices, readings
