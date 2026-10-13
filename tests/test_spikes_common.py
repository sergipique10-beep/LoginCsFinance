"""Spike COST-00: lógica pura (tools/spikes/common.py) y los scripts contra un transporte falso."""
import struct

import httpx
import pytest

from tools.spikes import spike_inventory, spike_priceoverview, spike_sources
from tools.spikes.common import (certificate_checksum, compare_certificates, currency_ratio,
                                 decode_certificate, guess_currency, name_coverage, parse_money,
                                 parse_volume, property_coverage, rate_headers)


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def make_cert(wear, seed=661, defindex=7, paintindex=282, key=0xA3):
    wear_bits = struct.unpack("<I", struct.pack("<f", wear))[0]
    proto = b"".join(_varint(f << 3) + _varint(v) for f, v in
                     ((3, defindex), (4, paintindex), (7, wear_bits), (8, seed)))
    proto += _varint((11 << 3) | 2) + _varint(3) + b"abc"  # customname: campo length-delimited
    plain = b"\x00" + proto + certificate_checksum(proto)
    return bytes([key]) + bytes(b ^ key for b in plain[1:]), proto


def test_decode_certificate_roundtrip():
    raw, _ = make_cert(0.15432)
    dec = decode_certificate(raw.hex().upper())
    assert dec["defindex"] == 7 and dec["paintindex"] == 282 and dec["paintseed"] == 661
    assert abs(dec["paintwear"] - 0.15432) < 1e-6
    assert dec["checksum_ok"] is True


def test_decode_certificate_detecta_checksum_malo():
    raw, _ = make_cert(0.5)
    bad = raw[:-1] + bytes([raw[-1] ^ 0xFF])
    assert decode_certificate(bad.hex())["checksum_ok"] is False


def test_decode_certificate_rechaza_basura():
    with pytest.raises(ValueError):
        decode_certificate("00ff")


def _inventory():
    raw, _ = make_cert(0.07)
    return {
        "assets": [{"assetid": "1", "classid": "a", "instanceid": "0"},
                   {"assetid": "2", "classid": "a", "instanceid": "0"},
                   {"assetid": "3", "classid": "s", "instanceid": "0"}],
        "descriptions": [{"classid": "a", "instanceid": "0", "tags": [{"category": "Exterior"}]},
                         {"classid": "s", "instanceid": "0", "tags": [{"category": "Type"}]}],
        "asset_properties": [{"assetid": "1", "asset_properties": [
            {"propertyid": 2, "float_value": "0.07"}, {"propertyid": 1, "int_value": "12"},
            {"propertyid": 6, "string_value": raw.hex()}]}],
    }


def test_property_coverage_cuenta_solo_skins():
    cov = property_coverage(_inventory())
    assert cov["assets"] == 3 and cov["skins"] == 2
    assert cov["Wear Rating"] == 50.0 and cov["Item Certificate"] == 50.0


def test_compare_certificates():
    rows = compare_certificates(_inventory())
    assert len(rows) == 1 and rows[0]["diff"] < 1e-6 and rows[0]["checksum_ok"]


@pytest.mark.parametrize("s,v", [("$1,234.56", 1234.56), ("1.234,56€", 1234.56), ("12,5", 12.5),
                                 ("1,234", 1234.0), ("¥ 7.10", 7.1), (None, None), ("--", None)])
def test_parse_money(s, v):
    assert parse_money(s) == v


def test_parse_volume():
    assert parse_volume("1,234") == 1234 and parse_volume(None) is None and parse_volume("") is None


def test_name_coverage():
    cov = name_coverage(["A", "b", None], ["a", "B", "c", "d"])
    assert cov["common"] == 2 and cov["catalog_covered_pct"] == 50.0 and cov["jaccard"] == 0.5


def test_currency_guess():
    assert guess_currency(currency_ratio({"x": 70.0}, {"x": 10.0})) == "CNY"
    assert guess_currency(currency_ratio({"x": 8.0}, {"x": 10.0})) == "USD"
    assert guess_currency(None) == "desconocida"


def test_rate_headers():
    h = {"X-RateLimit-Remaining": "5", "Retry-After": "60", "Content-Type": "json"}
    assert rate_headers(h) == {"X-RateLimit-Remaining": "5", "Retry-After": "60"}


class _Rec:
    def __init__(self):
        self.lines = []

    def __call__(self, kind, **data):
        self.lines.append((kind, data))


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_inventory_fetch_full_pagina(monkeypatch):
    monkeypatch.setattr(spike_inventory.time, "sleep", lambda s: None)
    pages = iter([{"assets": [{"assetid": "1"}], "more_items": 1, "last_assetid": "1"},
                  {"assets": [{"assetid": "2"}]}])
    inv = spike_inventory.fetch_full(_client(lambda r: httpx.Response(200, json=next(pages))), "s", _Rec())
    assert [a["assetid"] for a in inv["assets"]] == ["1", "2"]


def test_inventory_run_cadence_para_en_429(monkeypatch):
    monkeypatch.setattr(spike_inventory.time, "sleep", lambda s: None)
    codes = iter([200, 200, 429])
    rec = _Rec()
    at = spike_inventory.run_cadence(_client(lambda r: httpx.Response(next(codes), json={})), ["a"], 0, 30, rec)
    assert at == 2 and len(rec.lines) == 3


def test_priceoverview_run(monkeypatch):
    monkeypatch.setattr(spike_priceoverview.time, "sleep", lambda s: None)
    body = {"success": True, "lowest_price": "$1.00", "volume": "1,000"}
    rec = _Rec()
    assert spike_priceoverview.run(_client(lambda r: httpx.Response(200, json=body)), ["x", "y"], 600, rec)
    assert rec.lines[-1] == ("summary", {"per_min": 600, "n": 2, "ok": 2, "volume_parsed": 2})


def test_sources_marketcsgo():
    items = {"items": [{"market_hash_name": "A"}, {"market_hash_name": "Z"}]}
    rec = _Rec()
    spike_sources.marketcsgo(_client(lambda r: httpx.Response(200, json=items)), rec, {"A", "B"})
    assert rec.lines[0][1]["catalog_covered_pct"] == 50.0


def test_sources_keyed_sin_clave_no_llama(monkeypatch):
    monkeypatch.delenv("CSFLOAT_API_KEY", raising=False)
    monkeypatch.delenv("WAXPEER_API_KEY", raising=False)
    rec = _Rec()
    spike_sources.keyed(_client(lambda r: pytest.fail("no debe llamar")), rec)
    assert [d["skipped"] for _, d in rec.lines] == ["falta CSFLOAT_API_KEY", "falta WAXPEER_API_KEY"]
