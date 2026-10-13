"""Lógica pura del spike COST-00: decodificador de certificados, parsers y métricas.

Sin red ni ficheros salvo `Recorder`: los scripts `spike_*.py` hacen las peticiones y
delegan aquí todo lo que se puede testear (tests/test_spikes_common.py).
"""
import json
import struct
import time
import zlib
from pathlib import Path

# asset_properties del inventario de Steam (CS2, 2025+). Se casan por id y, si Valve
# cambia los ids, por nombre: el spike informa de lo que encuentra.
PROP_PATTERN = 1
PROP_WEAR = 2
PROP_CERTIFICATE = 6
PROP_NAMES = {PROP_PATTERN: "Pattern Template", PROP_WEAR: "Wear Rating", PROP_CERTIFICATE: "Item Certificate"}

# CEconItemPreviewDataBlock: número de campo → nombre (solo los escalares que usamos).
_PREVIEW_FIELDS = {1: "accountid", 2: "itemid", 3: "defindex", 4: "paintindex", 5: "rarity",
                   6: "quality", 7: "paintwear", 8: "paintseed", 9: "killeaterscoretype",
                   10: "killeatervalue", 14: "origin", 15: "questid", 16: "dropreason"}


def _varint(buf, i):
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


def _parse_proto(buf):
    """Protobuf mínimo: devuelve {campo: valor} de los varint; salta el resto."""
    out = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
            out[field] = val
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
    return out


def certificate_checksum(proto):
    """Checksum de 4 bytes que cierra un inspect enmascarado (sobre 0x00 + proto)."""
    crc = zlib.crc32(b"\x00" + proto)
    return struct.pack(">I", ((crc & 0xFFFF) ^ (len(proto) * crc)) & 0xFFFFFFFF)


def decode_certificate(hex_str):
    """Decodifica un `Item Certificate` (inspect enmascarado de CS2).

    Formato: byte 0 = clave XOR; tras el XOR, 0x00 + CEconItemPreviewDataBlock + 4 bytes de
    checksum. `paintwear` llega como uint32 con los bits de un float32.
    """
    raw = bytes.fromhex(hex_str.strip())
    if len(raw) < 6:
        raise ValueError("certificado demasiado corto")
    key = raw[0]
    data = bytes(b ^ key for b in raw)
    proto, checksum = data[1:-4], data[-4:]
    fields = _parse_proto(proto)
    out = {name: fields[n] for n, name in _PREVIEW_FIELDS.items() if n in fields}
    if "paintwear" in out:
        out["paintwear"] = struct.unpack("<f", struct.pack("<I", out["paintwear"]))[0]
    out["checksum_ok"] = checksum == certificate_checksum(proto)
    return out


def asset_props(inv_json):
    """{assetid: {nombre_propiedad: valor}} desde `asset_properties` del inventario."""
    out = {}
    for entry in inv_json.get("asset_properties") or []:
        props = {}
        for p in entry.get("asset_properties") or []:
            name = PROP_NAMES.get(p.get("propertyid")) or p.get("name") or f"prop{p.get('propertyid')}"
            for k in ("float_value", "int_value", "string_value"):
                if k in p:
                    props[name] = p[k]
                    break
        out[str(entry.get("assetid"))] = props
    return out


def is_skin(description):
    """Una skin con float es lo que lleva tag `Exterior` (los stickers, cajas y agentes no)."""
    return any(t.get("category") == "Exterior" for t in description.get("tags") or [])


def property_coverage(inv_json):
    """% de skins con cada propiedad, sobre las skins (no sobre todos los assets)."""
    descs = {(d.get("classid"), d.get("instanceid")): d for d in inv_json.get("descriptions") or []}
    props = asset_props(inv_json)
    skins = [a for a in inv_json.get("assets") or []
             if is_skin(descs.get((a.get("classid"), a.get("instanceid")), {}))]
    n = len(skins)
    res = {"assets": len(inv_json.get("assets") or []), "skins": n}
    for name in PROP_NAMES.values():
        hit = sum(1 for a in skins if name in props.get(str(a.get("assetid")), {}))
        res[name] = round(100 * hit / n, 2) if n else None
    return res


def compare_certificates(inv_json, limit=20):
    """Decodifica hasta `limit` certificados y compara su paintwear con `Wear Rating`."""
    rows = []
    for assetid, p in asset_props(inv_json).items():
        if len(rows) >= limit:
            break
        cert, wear = p.get("Item Certificate"), p.get("Wear Rating")
        if not cert or wear is None:
            continue
        try:
            dec = decode_certificate(cert)
        except ValueError as e:
            rows.append({"assetid": assetid, "error": str(e)})
            continue
        pw = dec.get("paintwear")
        diff = abs(float(wear) - pw) if pw is not None else None
        rows.append({"assetid": assetid, "wear": float(wear), "paintwear": pw, "diff": diff,
                     "paintseed": dec.get("paintseed"), "pattern": p.get("Pattern Template"),
                     "checksum_ok": dec["checksum_ok"]})
    return rows


def parse_money(s):
    """'$1,234.56' / '1.234,56€' / '¥ 12.5' → float; None si no hay número."""
    if s is None:
        return None
    digits = "".join(c for c in str(s) if c.isdigit() or c in ".,")
    if not digits:
        return None
    if "," in digits and "." in digits:
        if digits.rfind(",") > digits.rfind("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif "," in digits:
        head, _, tail = digits.rpartition(",")
        digits = head.replace(",", "") + ("." if len(tail) != 3 else "") + tail
    return float(digits)


def parse_volume(s):
    """`volume` de priceoverview ('1,234') → int; None si no viene o no es número."""
    if s is None:
        return None
    digits = "".join(c for c in str(s) if c.isdigit())
    return int(digits) if digits else None


def name_coverage(source_names, catalog_names):
    """Jaccard y cobertura (qué % del catálogo trae la fuente), con nombres normalizados."""
    a = {n.strip().lower() for n in source_names if n}
    b = {n.strip().lower() for n in catalog_names if n}
    inter = len(a & b)
    union = len(a | b)
    return {"source": len(a), "catalog": len(b), "common": inter,
            "jaccard": round(inter / union, 4) if union else None,
            "catalog_covered_pct": round(100 * inter / len(b), 2) if b else None}


def currency_ratio(source_prices, steam_prices):
    """Mediana de precio_fuente / precio_steam_usd: ~1 → USD, ~7 → CNY, ~0.9 → EUR."""
    ratios = sorted(source_prices[k] / steam_prices[k] for k in source_prices
                    if k in steam_prices and steam_prices[k] and source_prices[k])
    if not ratios:
        return None
    return ratios[len(ratios) // 2]


def guess_currency(ratio):
    if ratio is None:
        return "desconocida"
    # Buff cotiza por debajo de Steam (~0.6-0.9 en USD); en CNY eso queda en ~4-7.
    return "CNY" if ratio > 2.5 else "USD"


def rate_headers(headers):
    """Cabeceras de límite que exponga la fuente (X-RateLimit-*, Retry-After…)."""
    keys = ("ratelimit", "retry-after", "x-rate")
    return {k: v for k, v in headers.items() if any(s in k.lower() for s in keys)}


class Recorder:
    """JSONL en logs/spikes/<nombre>-<ts>.jsonl: una línea por petición o resultado."""

    def __init__(self, name, root=Path("logs/spikes")):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
        self._f = self.path.open("a", encoding="utf-8")

    def __call__(self, kind, **data):
        line = {"t": round(time.time(), 3), "kind": kind, **data}
        self._f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self._f.flush()
        print(json.dumps(line, ensure_ascii=False, default=str)[:300])

    def close(self):
        self._f.close()
