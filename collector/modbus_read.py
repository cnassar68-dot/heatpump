"""Read-only Modbus client, using only the Python standard library.

This file can only build read requests (function codes 3 and 4). It has no way to
write, so the collector cannot change a setting on a heat pump.

Two connection types are supported, set per unit in config.json:
  "mbap"    - adapter set to Modbus TCP mode
  "rtu-tcp" - adapter set to plain pass-through (RTU frames over TCP)
"""
import socket
import struct

READ_HOLDING = 3
READ_INPUT = 4
READ_FUNCTIONS = (READ_HOLDING, READ_INPUT)
MAX_REGISTERS = 100  # Larger reads are not reliable on these controllers.


class ModbusError(Exception):
    """The unit did not answer, or its answer could not be used."""


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def read_request(function, address, count):
    """The request body. Refuses anything that is not a read."""
    if function not in READ_FUNCTIONS:
        raise ValueError("Only read functions are allowed")
    if not 1 <= count <= MAX_REGISTERS:
        raise ValueError("Can read 1 to %d registers at a time" % MAX_REGISTERS)
    if not 0 <= address <= 0xFFFF - count + 1:
        raise ValueError("Bad register address")
    return struct.pack(">BHH", function, address, count)


def _receive(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ModbusError("Connection closed by the adapter")
        data += chunk
    return data


def _registers(payload, count):
    if len(payload) != count * 2:
        raise ModbusError("Answer has the wrong length")
    return list(struct.unpack(">%dH" % count, payload))


class ReadOnlyClient:
    def __init__(self, host, port, slave=1, framing="mbap", timeout=5.0):
        if framing not in ("mbap", "rtu-tcp"):
            raise ValueError("framing must be mbap or rtu-tcp")
        self.host, self.port, self.slave, self.framing, self.timeout = host, port, slave, framing, timeout
        self._sock = None
        self._transaction = 0

    def __enter__(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as err:
            raise ModbusError("Cannot connect to %s:%s (%s)" % (self.host, self.port, err))
        return self

    def __exit__(self, *_):
        if self._sock:
            self._sock.close()
            self._sock = None

    def read(self, function, address, count):
        body = read_request(function, address, count)
        try:
            if self.framing == "mbap":
                return self._read_mbap(function, body, count)
            return self._read_rtu(function, body, count)
        except OSError as err:
            raise ModbusError("No answer from %s:%s (%s)" % (self.host, self.port, err))

    def _read_mbap(self, function, body, count):
        self._transaction = (self._transaction + 1) & 0xFFFF
        self._sock.sendall(struct.pack(">HHHB", self._transaction, 0, len(body) + 1, self.slave) + body)
        transaction, protocol, length, slave = struct.unpack(">HHHB", _receive(self._sock, 7))
        if transaction != self._transaction or protocol != 0 or slave != self.slave or not 2 <= length <= 260:
            raise ModbusError("Answer does not match the request")
        pdu = _receive(self._sock, length - 1)
        return self._parse_pdu(function, pdu, count)

    def _read_rtu(self, function, body, count):
        frame = bytes([self.slave]) + body
        self._sock.sendall(frame + crc16(frame))
        head = _receive(self._sock, 3)
        if head[1] & 0x80:
            rest = _receive(self._sock, 2)
        else:
            rest = _receive(self._sock, head[2] + 2)
        whole = head + rest
        if crc16(whole[:-2]) != whole[-2:]:
            raise ModbusError("Answer failed its checksum")
        if head[0] != self.slave:
            raise ModbusError("Answer came from another device")
        return self._parse_pdu(function, whole[1:-2], count)

    @staticmethod
    def _parse_pdu(function, pdu, count):
        if pdu[0] == function | 0x80:
            raise ModbusError("Unit refused the read (code %d)" % pdu[1])
        if pdu[0] != function or pdu[1] != count * 2:
            raise ModbusError("Answer does not match the request")
        return _registers(pdu[2:], count)
