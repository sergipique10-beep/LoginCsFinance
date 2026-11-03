# Rankings de Home: cajas más vendidas, skins que suben y que bajan — diseño (BORRADOR)

> Estado: **aprobado** (2026-11-03). Plan: `docs/superpowers/plans/2026-11-03-home-rankings.md`.
> Sustituye a HOT/COLD (`/market/movers`) en Home.

## Por qué

HOT/COLD no enseña lo que de verdad se mueve en el mercado. Medido el 2026-11-02/03:

1. **Solo puntúa 20 skins.** `compute_movers` coge las 20 de más facturación 24h
   (`rules.turnover`) y las reparte 10/10 entre HOT y COLD por delta 7d. Con 20 candidatas
   para 20 huecos sale todo, se haya movido o no: la Galil AR | Grey Smoke (FT) aparecía en
   COLD con 0,0 % en 24h, 7d y 30d.
2. **Las skins caras no existen para el back.** `/items` se pide ordenado por unidades
   (`soldZa`, 5.000 items): en ese universo hay 1 skin ≥ 100 $ y ninguna ≥ 500 $. Cuchillos,
   guantes, AWP Fade… nunca entran.
3. **El precio de Steam que usamos es una venta suelta.** `priceLatest` = `pricelatestsell`,
   la última venta registrada. Una compra rara se convierte en el precio de la skin:
   - PP-Bizon | Wood Block Camo (FT): 342,12 $ en nuestra tabla, que es su `pricemax` (una venta
     real a ese precio que el gráfico de Steam, de medianas, no muestra). Vale 0,03 $.
   - 58 de las 333 filas de `market_trending` tenían el precio de Steam > 3× el precio real.
   - Ahora mismo el nº 1 de HOT es P250 | Sedimentary (FN): 200 $ Steam frente a 0,12 $ real.

Lo que el usuario (experto en el mercado) quiere ver: dónde está la actividad real y qué
skins —sobre todo las caras, que son las que la gente mira— han subido o bajado de verdad,
en dinero.

## Qué se construye

Tres listas en Home, en lugar de HOT/COLD:

| Lista | Qué entra | Orden |
|---|---|---|
| **Cajas más vendidas** | solo cajas | unidades vendidas 24h, de más a menos |
| **Skins que más suben** | todo excepto cajas | subida en **$** a 30 días, de más a menos |
| **Skins que más bajan** | todo excepto cajas | bajada en **$** a 30 días, de más a menos |

La tarjeta de suben/bajan muestra los dólares en grande y el % pequeño al lado.

## Reglas decididas

- **"Skin" = todo lo que no es caja**: armas, cuchillos, guantes, agentes, stickers, charms…
  (decisión del usuario).
- **Precio fiable, nunca la última venta.** Para filtrar y para calcular: `pricereal`
  (mercados de terceros) o la mediana de Steam. Nunca `pricelatestsell`.
- **Periodo: 30 días** (`pricereal` frente a `pricereal30d`). 7 días traía más basura en el
  precio de referencia. Un selector 24h/7d/30d, al estilo Pricempire, queda para más adelante.
- **Plausibilidad**: se descarta la skin si el precio de hace 30 días y el actual se separan
  más de 10× (`rules.MAX_PLAUSIBLE_RATIO`, la regla que ya existe).
- **Mínimo 5 ventas en 7 días** para suben/bajan. Con 1 se colaban movimientos falsos
  (Souvenir AWP Printstream WW +792 $ con una venta); con 7 se perdían skins caras reales
  (★ Sport Gloves | Vice MW, −208 $).
- **Cajas sin mínimo de precio**: la Kilowatt Case (0,12 $) es la 2ª más vendida y es real.
- **Sin relleno**: si una lista tiene menos skins que cumplan las reglas, se muestran menos.
  Nunca un 0 % para completar.

## De dónde salen los datos

Dos peticiones a `/items` por captura (no una por skin; la cuota es por petición):

1. La actual: `sort_by=soldZa`, `max=5000` → actividad, cajas y skins baratas/medias.
2. Nueva: `sort_by=soldZa`, `price_real_min=50`, `max=5000` → skins caras.

Se unen por `markethashname`. Ambas traen `pricereal`, `pricereal30d`, `sold24h`, `sold7d`
en el mismo payload, así que suben/bajan se calcula sobre ~10.000 items sin llamar a CSFloat.
La petición 2 llegó al tope de 5.000 en la prueba: hay más skins ≥ 50 $ que no entran.

`/items` admite además `sort_by=winner|loser`, `priceRealZa` y `price_real_max` (documentación
de steamwebapi). No está comprobado sobre qué periodo calcula `winner/loser`.

## Resultado esperado con datos del 2026-11-03

- Cajas: Dreams & Nightmares (110.497 / 24h), Kilowatt (93.744), Revolution (72.410), Fever…
- Suben 30d: ★ Moto Gloves | 3rd Commando Company MW (+34,16 $, +54,5 %), FAMAS | Styx FN
  (+29,51 $), ★ Shadow Daggers | Forest DDPAT WW (+21,14 $), M4A4 | Hellish FN (+18,37 $).
- Bajan 30d: ★ Sport Gloves | Vice MW (−208,33 $), ★ Butterfly Knife | Gamma Doppler FN
  (−132,27 $), ★ Butterfly Knife | Lore MW (−112,11 $), ★ Karambit | Doppler FN (−102,18 $).

## Decisiones cerradas (antes «Abierto»)

1. **Variedad: sin tope por tipo** (usuario). Se ordena solo por dólares; cuchillos y guantes
   caros copan las listas y es lo que la gente mira.
2. **Encaje en la app** (usuario): las tres listas sustituyen a HOT/COLD en Home; el **ticker**
   de la barra superior pasa a usar suben/bajan; el trending de Market no se toca.
   `/market/movers` conserva la ruta y la tabla `market_movers`, pero su contenido cambia.
3. **Ramas** (usuario): `feat/home-rankings` en los dos repos, encima de `feat/ux46-data-status`.

Decisiones del constructor (no las pidió el usuario; revisables):

- **Sin respaldo de topmovers** para estas listas: topmovers da % en 24h, no encaja. Si
  `/items` falla, el tick conserva el snapshot anterior (CAL-10) y la API avisa con UX-46.
  Si solo falla la petición de las caras, se guarda lo demás como `partial`.
- **Sin CSFloat history**: los deltas salen de `pricereal`/`pricereal30d` del propio payload.
  El movers-tick deja de gastar ~20 llamadas cada 15 min.
- **Precio mostrado** (`priceLatest`) = mediana de Steam (`pricemedian`), no la última venta.
  Lo leen también las alertas (`alerts/service._cached_prices`), que ignoran el 0.
- **Dólares sin columna nueva**: `priceChange30d` se deriva al leer de `price_real` y
  `price_delta_30d` (el % se guarda sin redondear). Sin migración de Supabase.
- **10 skins por lista**; Home enseña 5 y un «Ver más» despliega el resto.
- **Caja** = nombre que termina en « Case» (39 en el payload medido).

## Fuera de alcance

- Arreglar `priceLatest` en el resto de la app (inventario, búsqueda): mismo problema de
  venta suelta, pero es otro cambio.
- Lista de «más vendidas» de skins: se habló y se sustituyó por suben/bajan.
