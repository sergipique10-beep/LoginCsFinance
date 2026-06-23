"""Verification script for _weapon_category helper and _map_item integration.

Run with:
    python verify_weapon_category.py

All assertions must pass and the script must exit 0.
"""

import sys
import os

# Ensure the repo root is on the path so steam.mappers is importable
sys.path.insert(0, os.path.dirname(__file__))

from steam.mappers import _weapon_category, _map_item

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
print("ALL CHECKS PASSED — verify_weapon_category.py exiting 0")
