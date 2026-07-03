"""Verification script for _weapon_category helper and _map_item integration.

Run with:
    python verify_weapon_category.py

All assertions must pass and the script must exit 0.
"""

import sys
import os

# Ensure the repo root is on the path so steam.mappers is importable
sys.path.insert(0, os.path.dirname(__file__))

from steam.mappers import _weapon_category, _map_item, _category_rank, _CATEGORY_PRIORITY

# ── Unit tests for _weapon_category ──────────────────────────────────────────

assert _weapon_category("ak-47") == "Rifle",           f"Expected 'Rifle', got {_weapon_category('ak-47')!r}"
assert _weapon_category("awp") == "Sniper Rifle",      f"Expected 'Sniper Rifle', got {_weapon_category('awp')!r}"
assert _weapon_category("karambit") == "Knife",        f"Expected 'Knife', got {_weapon_category('karambit')!r}"
assert _weapon_category("glock-18") == "Pistol",       f"Expected 'Pistol', got {_weapon_category('glock-18')!r}"
assert _weapon_category("mac-10") == "SMG",            f"Expected 'SMG', got {_weapon_category('mac-10')!r}"
assert _weapon_category("nova") == "Heavy",            f"Expected 'Heavy', got {_weapon_category('nova')!r}"
assert _weapon_category(None) is None,                 f"Expected None, got {_weapon_category(None)!r}"

# Substring fallback: "sport gloves" → "Gloves"
assert _weapon_category("sport gloves") == "Gloves",   f"Expected 'Gloves', got {_weapon_category('sport gloves')!r}"

# title-case fallback for totally unknown itemtype
assert _weapon_category("music kit") == "Music Kit",   f"Expected 'Music Kit', got {_weapon_category('music kit')!r}"

print("_weapon_category unit tests: ALL PASSED")

# ── Integration test: _map_item picks up weaponType via _weapon_category ─────

raw = {
    "itemtype": "ak-47",
    "marketname": "AK-47 | Redline",
    "pricelatestsell": 100,
}

result = _map_item(raw)

assert result["weaponType"] == "Rifle", (
    f"Integration: expected weaponType='Rifle', got {result['weaponType']!r}"
)
assert result["itemType"] == "ak-47", (
    f"Integration: expected itemType='ak-47', got {result['itemType']!r}"
)

print("_map_item integration test: ALL PASSED")
print()
# ── Unit tests for _category_rank ────────────────────────────────────────────

assert _category_rank("Rifle") < _category_rank("Case"), (
    f"Expected Rifle before Case, got Rifle={_category_rank('Rifle')}, Case={_category_rank('Case')}"
)
assert _category_rank("Music Kit") < _category_rank("Desconocido"), (
    f"Expected known category before unknown, got Music Kit={_category_rank('Music Kit')}, Desconocido={_category_rank('Desconocido')}"
)
assert _category_rank(None) == len(_CATEGORY_PRIORITY), (
    f"Expected None to rank last ({len(_CATEGORY_PRIORITY)}), got {_category_rank(None)}"
)

print("_category_rank unit tests: ALL PASSED")

# ── Integration test: sort prioritizes weapons over cases ─────────────────────

_test_items = [
    {"weaponType": "Case",     "sold24h": 1000, "name": "case-1000"},
    {"weaponType": "Rifle",    "sold24h":   10, "name": "rifle-10"},
    {"weaponType": "Pistol",   "sold24h":  500, "name": "pistol-500"},
    {"weaponType": "Case",     "sold24h":  200, "name": "case-200"},
    {"weaponType": "Rifle",    "sold24h":  300, "name": "rifle-300"},
]
_sorted = sorted(
    _test_items,
    key=lambda x: (_category_rank(x.get("weaponType")), -(x.get("sold24h") or 0)),
)
_first_weapon = next(i for i, item in enumerate(_sorted) if item["weaponType"] == "Rifle")
_first_case   = next(i for i, item in enumerate(_sorted) if item["weaponType"] == "Case")
assert _first_weapon < _first_case, (
    f"Expected all Rifles before Cases; Rifle first at index {_first_weapon}, Case first at {_first_case}"
)
# Within Rifle: highest sold24h first
assert _sorted[0]["name"] == "rifle-300", (
    f"Expected highest-sold Rifle first; got {_sorted[0]['name']!r}"
)
assert _sorted[1]["name"] == "rifle-10", (
    f"Expected lowest-sold Rifle second; got {_sorted[1]['name']!r}"
)

print("_category_rank sort integration test: ALL PASSED")

# ── All done ─────────────────────────────────────────────────────────────────

print()
print("ALL CHECKS PASSED — verify_weapon_category.py exiting 0")
