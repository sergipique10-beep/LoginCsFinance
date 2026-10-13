---
feature: spikes
files:
  - tools/spikes/common.py
  - tools/spikes/spike_inventory.py
  - tools/spikes/spike_priceoverview.py
  - tools/spikes/spike_sources.py
---

# Spikes de medición (COST-00)

Scripts de un solo uso para medir, **desde la IP del VPS**, las cifras que dimensionan la
migración fuera de steamwebapi (gate GCRA, presupuesto diario, DDL). No los importa la
aplicación; los resultados viven en el issue COST-00 de Plane, no aquí.

> ⚠️ Nunca desde Render ni desde un portátil: los límites de Steam son por IP, y una medida
> desde otra IP no dice nada de la del worker. Tampoco desde una sesión cloud del agente: su
> proxy de salida bloquea Steam, Skinport, market.csgo, csgotrader, CSFloat y Hetzner.

## Cómo se ejecuta (en el VPS)

```bash
git clone … && cd LoginCsFinance && python3 -m venv venv
venv/bin/pip install httpx brotli          # brotli: Skinport exige Accept-Encoding: br
# 1) fuentes, claves e infra (~2 min). Las claves solo se leen del entorno.
export STEAM_WEB_API_KEY=…  CSFLOAT_API_KEY=…  WAXPEER_API_KEY=…  HCLOUD_TOKEN=…
venv/bin/python -m tools.spikes.spike_sources --ids ids.txt      # ids.txt: hasta 100 SteamID64
# 2) inventario: cobertura + ritmo (puede tardar horas si hay bloqueo; usar tmux)
venv/bin/python -m tools.spikes.spike_inventory <sid1> <sid2> <sid_con_2000+_items>
# 3) priceoverview: 100 nombres a 6/min, 10 min de pausa, 100 a 10/min (~40 min)
venv/bin/python -m tools.spikes.spike_priceoverview
```

Cada script escribe `logs/spikes/<nombre>-<ts>.jsonl` (una línea por petición y una de
resumen por bloque). Para el issue basta con las líneas `coverage`, `certificates`, `blocked`,
`summary`, `first_429`, `skinport`, `marketcsgo`, `csgotrader`, `players`, `keyed`, `hetzner`
y `aaaa`: `grep -E '"kind": "(coverage|certificates|blocked|summary|first_429)"' logs/spikes/*.jsonl`.

## Qué decide cada cifra

| Cifra | Criterio (diseño § 6.1) | Alimenta |
|---|---|---|
| % skins con `Wear Rating` | ≥ 95 % | COST-07 (float sin inspección) |
| \|paintwear − Wear Rating\| en 20 certificados | < 1e-6 | COST-07 (decodificador) |
| 1.er 429 del inventario por cadencia + duración del bloqueo | — | COST-02 (GCRA), COST-07 (cola) |
| 1.er 429 de priceoverview a 6/min y 10/min; `volume` parseable | — | COST-04 (presupuesto top-N) |
| Cobertura de nombres de Skinport / market.csgo / csgotrader | ≥ 90 % del catálogo | COST-03 |
| Moneda de `buff163.json` | resuelta (USD o CNY) | COST-03 |
| Cabeceras de límite CSFloat / Waxpeer | — | COST-03 |
| `pg_trgm`, tamaño de la BD, `db-max-rows` | — | COST-01 (DDL) |
| Precio IPv4 primaria Hetzner; AAAA de Steam | — | COST-01 (segunda IP) |

## Notas de implementación

- `common.decode_certificate`: el `Item Certificate` es un inspect enmascarado de CS2 —
  byte 0 = clave XOR; tras el XOR, `0x00` + `CEconItemPreviewDataBlock` + 4 bytes de checksum
  (CRC32 plegado con la longitud). `paintwear` llega como uint32 con los bits de un float32.
  `checksum_ok` se registra pero no bloquea: si Valve cambia el plegado, el spike lo dirá.
- Los ids de `asset_properties` (1 Pattern Template, 2 Wear Rating, 6 Item Certificate) se
  casan por id y, si no, por el `name` que venga: el spike informa de lo que encuentra.
- Si el inventario da 429 en la primera cadencia (4 s), el script mide el bloqueo y repite a
  8 s antes de terminar (nota del issue).
