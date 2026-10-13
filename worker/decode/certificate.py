"""Decodificador del `Item Certificate` de CS2 (COST-07). Puro, sin dependencias.

El certificado es un inspect enmascarado: byte 0 = clave XOR; tras el XOR queda
`0x00` + `CEconItemPreviewDataBlock` (protobuf) + 4 bytes de checksum. `paintwear` llega como
uint32 con los bits de un float32.

Desviación consciente de la tarjeta: en vez de `econ.proto` + `_pb2.py` generado con protoc, un
lector protobuf mínimo (solo varint y saltos). Los campos que usamos son escalares; así no hay
código generado que acoplar a la versión de protobuf de la imagen.
"""
import struct
import zlib

# CEconItemPreviewDataBlock: número de campo → nombre (solo escalares).
FIELDS = {1: "accountid", 2: "itemid", 3: "defindex", 4: "paintindex", 5: "rarity", 6: "quality",
          7: "paintwear", 8: "paintseed", 9: "killeaterscoretype", 10: "killeatervalue",
          14: "origin", 15: "questid", 16: "dropreason"}


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint truncado")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def parse_proto(buf: bytes) -> dict[int, int]:
    """{campo: valor} de los varint y fixed32; salta length-delimited y fixed64."""
    out: dict[int, int] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            out[field], i = _varint(buf, i)
        elif wire == 2:
            ln, i = _varint(buf, i)
            i += ln
        elif wire == 5:
            out[field] = struct.unpack("<I", buf[i:i + 4])[0]
            i += 4
        elif wire == 1:
            i += 8
        else:
            raise ValueError(f"wire type {wire} no soportado")
        if i > len(buf):
            raise ValueError("campo truncado")
    return out


def checksum(proto: bytes) -> bytes:
    """Checksum de 4 bytes que cierra un inspect enmascarado (sobre 0x00 + proto)."""
    crc = zlib.crc32(b"\x00" + proto)
    return struct.pack(">I", ((crc & 0xFFFF) ^ (len(proto) * crc)) & 0xFFFFFFFF)


def decode(hex_str: str) -> dict[str, int | float | bool]:
    raw = bytes.fromhex(hex_str.strip())
    if len(raw) < 6:
        raise ValueError("certificado demasiado corto")
    key = raw[0]
    data = bytes(b ^ key for b in raw)
    proto, tail = data[1:-4], data[-4:]
    fields = parse_proto(proto)
    out: dict[str, int | float | bool] = {name: fields[n] for n, name in FIELDS.items() if n in fields}
    if "paintwear" in out:
        out["paintwear"] = struct.unpack("<f", struct.pack("<I", int(out["paintwear"])))[0]
    out["checksum_ok"] = tail == checksum(proto)
    return out
