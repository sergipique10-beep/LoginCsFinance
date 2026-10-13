"""Respuesta de steamcommunity.com/inventory → assets sin precio (COST-07). Puro.

Fuente primaria del float y del patrón: `asset_properties` (Wear Rating, Pattern Template). El
`Item Certificate` se decodifica para lo que no viene ahí (paintindex → fase Doppler, defindex);
`decode_certs` recibe una caché {assetid: decodificado} para no repetir trabajo.

Nunca se usa `actions[].link`: lleva un placeholder, no un inspect válido. Un ítem recién
tradeado puede venir sin Wear Rating: `float_value=None` y se reintenta en el siguiente refresco.
"""
from typing import Any

from worker.decode import certificate

PROP_PATTERN, PROP_WEAR, PROP_CERT = 1, 2, 6
PROP_NAMES = {PROP_PATTERN: "pattern", PROP_WEAR: "wear", PROP_CERT: "cert",
              "Pattern Template": "pattern", "Wear Rating": "wear", "Item Certificate": "cert"}

# paintindex → fase de los cuchillos Doppler / Gamma Doppler (catálogo público de CS2).
DOPPLER_PHASES = {415: "Ruby", 416: "Sapphire", 417: "Black Pearl", 418: "Phase 1", 419: "Phase 2",
                  420: "Phase 3", 421: "Phase 4", 568: "Emerald", 569: "Phase 1", 570: "Phase 2",
                  571: "Phase 3", 572: "Phase 4", 617: "Black Pearl", 618: "Phase 2", 619: "Sapphire",
                  852: "Phase 1", 853: "Phase 2", 854: "Phase 3", 855: "Phase 4"}


def _props(inv: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in inv.get("asset_properties") or []:
        props: dict[str, Any] = {}
        for p in entry.get("asset_properties") or []:
            key = PROP_NAMES.get(p.get("propertyid")) or PROP_NAMES.get(p.get("name"))
            if key is None:
                continue
            for field in ("float_value", "int_value", "string_value"):
                if field in p:
                    props[key] = p[field]
                    break
        out[str(entry.get("assetid"))] = props
    return out


def _tag(desc: dict[str, Any], category: str) -> dict[str, Any]:
    return next((t for t in desc.get("tags") or [] if t.get("category") == category), {})


def _num(v: Any, cast: type) -> Any:
    try:
        return cast(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def extract(inv: dict[str, Any], decoded: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Un dict por asset, listo para `inventory_snapshots_shadow.items` (sin precios)."""
    descs = {(d.get("classid"), d.get("instanceid")): d for d in inv.get("descriptions") or []}
    props = _props(inv)
    decoded = decoded or {}
    out = []
    for a in inv.get("assets") or []:
        aid = str(a.get("assetid"))
        d = descs.get((a.get("classid"), a.get("instanceid")))
        if not d or not d.get("market_hash_name"):
            continue
        p = props.get(aid, {})
        dec = decoded.get(aid, {})
        rarity = _tag(d, "Rarity")
        quality = _tag(d, "Quality").get("localized_tag_name")
        paint_index = _num(dec.get("paintindex"), int)
        name = d["market_hash_name"]
        out.append({
            "assetid": aid, "classid": str(a.get("classid")), "instanceid": str(a.get("instanceid")),
            "amount": _num(a.get("amount"), int) or 1,
            "market_hash_name": name, "name": d.get("name"),
            "icon_url": d.get("icon_url"), "type": d.get("type"),
            "item_type": _tag(d, "Type").get("localized_tag_name"),
            "weapon": _tag(d, "Weapon").get("localized_tag_name"),
            "exterior": _tag(d, "Exterior").get("localized_tag_name"),
            "rarity": rarity.get("localized_tag_name"), "rarity_color": rarity.get("color"),
            "quality": quality,
            "is_stattrak": "StatTrak™" in name, "is_souvenir": name.startswith("Souvenir "),
            "is_star": name.startswith("★"),
            "tradable": bool(d.get("tradable", 1)), "marketable": bool(d.get("marketable", 1)),
            "float_value": _num(p.get("wear"), float), "paint_seed": _num(p.get("pattern"), int),
            "paint_index": paint_index, "phase": DOPPLER_PHASES.get(paint_index) if paint_index else None,
            "defindex": _num(dec.get("defindex"), int),
        })
    return out


def certificates(inv: dict[str, Any]) -> dict[str, str]:
    """{assetid: hex} de los assets que traen Item Certificate."""
    return {aid: p["cert"] for aid, p in _props(inv).items() if isinstance(p.get("cert"), str) and p["cert"]}


def decode_certs(certs: dict[str, str], cache: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """Decodifica lo que no esté en `cache`. Devuelve (todos los decodificados, nº de fallos)."""
    out = dict(cache)
    failed = 0
    for aid, hex_str in certs.items():
        if aid in out:
            continue
        try:
            out[aid] = certificate.decode(hex_str)
        except ValueError:
            failed += 1
    return out, failed


def decode_missing_ratio(items: list[dict[str, Any]]) -> float:
    """% de skins (con desgaste) sin float: la métrica que vigila COST-07/08."""
    skins = [i for i in items if i.get("exterior")]
    return round(sum(1 for i in skins if i["float_value"] is None) / len(skins), 4) if skins else 0.0
