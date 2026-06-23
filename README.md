# pv-calc-forecast

A CLI tool and HTTP API for solar PV **clear-sky calculation** and **weather-based forecasting**, powered by [pvlib](https://pvlib-python.readthedocs.io) and multiple forecast sources.

Supports **multiple PV strings** (panel arrays with different orientations) in a single run — designed to be used as a long-running Prometheus exporter service. Timezone is **auto-detected from coordinates** — no need to specify it manually.

## Features

- **Calculate mode** — theoretical DC output under clear-sky conditions via pvlib
- **Forecast mode** — real weather forecast from three sources, with local pvlib transposition to your actual tilt/azimuth
- **Multi-string support** — define multiple panel arrays (PV1, PV2, PV3…) via config or CLI; per-string and total metrics emitted
- **Flask API server** (`pvcalc-api.py`) — libraries loaded once at startup; Prometheus scrape endpoint + JSON REST endpoints
- Forecast caching (1 hour TTL) — weather data fetched once per location, transposed per string
- Output formats: human-readable table, JSON, Prometheus metrics
- `config.cfg` for storing all parameters — config keys match CLI flags exactly (strip the `--`)

## Requirements

```bash
pip install -r requirements.txt
```

## Configuration

### Multi-string (recommended)

Define each panel array as a named section in `config.cfg`. Section names become the `string=` label in Prometheus output.

```ini
[system]
latitude  = 41.000
longitude = 22.000
timezone  = Europe/Athens
forecast  = open-meteo
format    = prometheus

[PV1]
capacity = 15     # kWp
tilt     = 30     # degrees from horizontal
azimuth  = 205    # degrees clockwise from North (180=South, 90=East, 270=West)

[PV2]
capacity = 10
tilt     = 25
azimuth  = 90

[PV3]
capacity = 5
tilt     = 25
azimuth  = 270
```

### Single-string (legacy / one-off runs)

```ini
[system]
latitude        = 41.000
longitude       = 22.000
system-capacity = 10.0
panel-tilt      = 30.0
panel-azimuth   = 180.0
shortname       = mysystem
```

A custom config path can be specified with `--config /path/to/config.cfg`.

**Config key naming** — every config key is the CLI flag without the `--` prefix. Example: `--hourly-window` → `hourly-window = 0-5` in config.

## Usage

```
pv-calc-forecast.py (--calculate | --forecast) [options]
```

### Multi-string via CLI

Strings can be defined directly on the command line with repeatable `--string` flags (overrides config sections):

```bash
./pv-calc-forecast.py --forecast=open-meteo --format=prometheus \
  --latitude=41.000 --longitude=22.000 --timezone=Europe/Athens \
  --string PV1:15:30:205 \
  --string PV2:10:25:90 \
  --string PV3:5:25:270
```

Format: `NAME:CAPACITY_kWp:TILT_deg:AZIMUTH_deg`

### Calculate mode

Uses pvlib to compute theoretical DC output under clear-sky conditions.
Runs independently — does **not** trigger forecast output.

```bash
# Current moment (default)
./pv-calc-forecast.py --calculate

# Today at a specific hour
./pv-calc-forecast.py --calculate=14:00

# Today, time range
./pv-calc-forecast.py --calculate=14:00-16:00

# Whole day
./pv-calc-forecast.py --calculate=2024-06-15

# Specific datetime
./pv-calc-forecast.py --calculate=2024-06-15-12:00

# Specific date, time range
./pv-calc-forecast.py --calculate=2024-06-15-10:00-14:00

# Cross-date range
./pv-calc-forecast.py --calculate=2024-06-15-10:00-2024-06-16-14:00

# Higher resolution
./pv-calc-forecast.py --calculate=2024-06-15-10:00-14:00 --resolution=30min
```

**Calculate options:**

| Option | Description |
|--------|-------------|
| `--calculate[=TIMESPEC]` | `now` (default), `HH:MM`, `HH:MM-HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DD-HH:MM`, `YYYY-MM-DD-HH:MM-HH:MM`, `YYYY-MM-DD-HH:MM-YYYY-MM-DD-HH:MM` |
| `--resolution` | `1min`, `10min`, `20min`, `30min`, `1H` (default: `1H`) |

### Forecast mode

Fetches a weather-based solar forecast. Three sources are available:

| Source | Flag | Horizon | Rate limit | Auth | Transposition |
|--------|------|---------|------------|------|---------------|
| Open-Meteo | `--forecast=open-meteo` **(default)** | 7 days | none | none | exact (real DNI/DHI) |
| forecast.solar | `--forecast=forecast-solar` | ~2 days | 12 req/h (free) | none | ratio method via clear-sky |
| Solcast | `--forecast=solcast` | 7 days | 10 calls/day (free) | API key | pre-computed by Solcast |

forecast.solar and Open-Meteo transpose the forecast to your actual panel tilt/azimuth using pvlib. Solcast uses the tilt/azimuth and capacity you registered in your [Solcast account](https://solcast.com) — no local transposition is performed.

All sources cache results locally. Cache TTL: 1 hour for forecast.solar and Open-Meteo, 4 hours for Solcast.

```bash
# Open-Meteo (default — 7-day horizon, no rate limit, exact transposition)
./pv-calc-forecast.py --forecast

# forecast.solar (~2-day horizon, clear-sky ratio method)
./pv-calc-forecast.py --forecast=forecast-solar

# Solcast (requires a registered rooftop site and API key)
./pv-calc-forecast.py --forecast=solcast --solcast-api-key=YOUR_KEY
```

**Forecast-only options:**

| Option | Description |
|--------|-------------|
| `--hourly-window START-END` | Minutes within each hour to emit the hourly block in Prometheus format (default: `0-59`); `solar_forecast_current_hour_w` is always emitted |
| `--show-days N` | Number of forecast days to include in output (default: `3`; config key: `show-days`) |
| `--solcast-api-key KEY` | Solcast API key (or `solcast-api-key` in config.cfg) |
| `--solcast-resource-id ID` | Solcast rooftop site resource ID (auto-detected if only one site is registered) |

### Shared options

| Option | Description |
|--------|-------------|
| `--latitude` | Location latitude |
| `--longitude` | Location longitude |
| `--timezone` | Override timezone (auto-detected from coordinates if omitted) |
| `--calculate[=TIMESPEC]` | Clear-sky calculation (see Calculate mode above) |
| `--string NAME:CAPACITY:TILT:AZIMUTH` | Define a PV string (repeatable; overrides config sections) |
| `--system-capacity` | System capacity in kWp (single-string mode only) |
| `--panel-tilt` | Panel tilt in degrees (single-string mode only) |
| `--panel-azimuth` | Panel azimuth in degrees (single-string mode only) |
| `--shortname` | String name to use in single-string mode |
| `--format` | `human` (default), `json`, `prometheus` |
| `--config` | Path to config file |

## Output formats

### Human (default)

Calculate mode — single point:
```
--------------  ---------------------
Time            2024-06-15 12:00 EEST
DC Power        8.59 kW
POA Irradiance  858.82 W/m²
GHI             849.22 W/m²
--------------  ---------------------
```

Forecast mode — multi-string:
```
=== PV1 ===
Date          Energy (Wh)
----------  -------------
2024-06-15        110,618

=== PV2 ===
Date          Energy (Wh)
----------  -------------
2024-06-15         67,693
```

### JSON

Multi-string forecast:
```json
{
  "PV1": {
    "watt_hours_day": { "2024-06-15": 110618 },
    "watts_tilted":   { "2024-06-15 08:00:00": 1462 }
  },
  "PV2": {
    "watt_hours_day": { "2024-06-15": 67693 },
    "watts_tilted":   { "2024-06-15 08:00:00": 890 }
  }
}
```

### Prometheus

All metrics use a `string=` label. When multiple strings are configured, a `string="total"` row is also emitted.

Calculate mode:
```
theoretical_pv_w{string="PV1",plant="theoretical",capacity="15.0"} 8250.00
theoretical_pv_w{string="PV2",plant="theoretical",capacity="10.0"} 4100.00
theoretical_pv_w{string="total",plant="theoretical",capacity="25.0"} 12350.00
```

Forecast mode:
```
# HELP solar_forecast_day_wh Forecasted solar energy production in watt-hours per day
# TYPE solar_forecast_day_wh gauge
solar_forecast_day_wh{string="PV1",date="2024-06-15"} 110618
solar_forecast_day_wh{string="PV2",date="2024-06-15"} 67693
solar_forecast_day_wh{string="total",date="2024-06-15"} 178311

# HELP solar_forecast_hour_w Forecasted solar power output per hour in watts
# TYPE solar_forecast_hour_w gauge
solar_forecast_hour_w{string="PV1",date="2024-06-15",hour="14:00"} 18500
solar_forecast_hour_w{string="PV2",date="2024-06-15",hour="14:00"} 9200
solar_forecast_hour_w{string="total",date="2024-06-15",hour="14:00"} 27700

# HELP solar_forecast_current_hour_w Forecasted solar power output for the current hour in watts
# TYPE solar_forecast_current_hour_w gauge
solar_forecast_current_hour_w{string="PV1"} 18500
solar_forecast_current_hour_w{string="PV2"} 9200
solar_forecast_current_hour_w{string="total"} 27700
```

## API server

`pvcalc-api.py` is a Flask HTTP server that loads all libraries once at startup and serves requests by calling the same calculation/forecast functions directly — no subprocess overhead.

```bash
python3 pvcalc-api.py
```

Startup output shows the bound address, location, configured strings, and forecast source. The forecast is fetched in the background at startup so the first scrape returns immediately.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Landing page — uptime, site config, PV strings, endpoint list |
| `/metrics` | GET | Prometheus scrape endpoint |
| `/forecast` | GET | Forecast; params: `?source=open-meteo`, `?days=3`, `?format=json\|prometheus` |
| `/forecast` | POST | Forecast with custom strings (JSON body) |
| `/calculate` | GET | Clear-sky calculation; params: `?at=TIMESPEC`, `?resolution=1H`, `?format=json\|prometheus` |
| `/calculate` | POST | Calculate with custom strings (JSON body) |

### API config keys (in `[system]`)

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `0.0.0.0` | Bind address |
| `port` | `5001` | Listening port |
| `forecast` | `open-meteo` | Default forecast source |

### Custom strings via POST

```bash
curl -X POST http://localhost:5001/forecast \
  -H 'Content-Type: application/json' \
  -d '{"source":"open-meteo","strings":[{"name":"PV1","capacity":15,"tilt":30,"azimuth":205}]}'

curl http://localhost:5001/calculate?at=14:00&format=json
```

## Notes

- Timezone is auto-detected from latitude/longitude using `timezonefinder`; set `timezone` in `[system]` to avoid the ~4s startup cost on ARM boards
- Panel azimuth: 180° = South, 90° = East, 270° = West
- `--string` flags take priority over `[PV*]` config sections, which take priority over single-string CLI params
- Weather data is fetched once per location and transposed independently per string — no extra API calls for additional strings
- Calculate mode filters out negligible production values (< 0.001 kW) and assumes clear-sky conditions
- Forecast mode daily totals are summed from the transposed hourly values, not taken from the raw API response
- Open-Meteo uses actual DNI/DHI from its NWP model for an exact pvlib transposition; forecast.solar uses a clear-sky ratio approximation (the API only exposes total watts, not irradiance components)
- Solcast rooftop forecasts use the tilt, azimuth and capacity configured in your Solcast account dashboard; for multi-string Solcast use, add `solcast-resource-id` to each `[PV*]` section
- Solcast free tier: 10 API calls/day — results are cached for 4 hours; expired cache is used on rate-limit errors
- `--hourly-window` gates the `solar_forecast_hour_w` block in Prometheus format; `solar_forecast_current_hour_w` is always emitted regardless; human and JSON output always include the full hourly table

## Solcast setup

1. Create a free account at [solcast.com](https://solcast.com)
2. Register your rooftop site with its actual tilt, azimuth and capacity
3. Copy your API key from the account dashboard
4. Add to `config.cfg`:
   ```ini
   solcast-api-key = your-key-here
   # Per-string resource IDs in each [PV*] section:
   # [PV1]
   # solcast-resource-id = 1111-aaaa-2222-bbbb
   ```

## License

MIT
