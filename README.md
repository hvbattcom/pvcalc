# pv-calc-forecast

A single CLI tool for solar PV **clear-sky calculation** and **weather-based forecasting**, powered by [pvlib](https://pvlib-python.readthedocs.io) and the [forecast.solar](https://forecast.solar) API.

Both modes share the same system parameters (location, capacity, tilt, azimuth). Timezone is **auto-detected from coordinates** — no need to specify it manually.

## Features

- **Calculate mode** — theoretical DC output under clear-sky conditions via pvlib
- **Forecast mode** — real weather forecast from forecast.solar API, with local pvlib transposition to your actual tilt/azimuth
- Forecast caching (1 hour TTL) — one cache entry per location regardless of tilt/azimuth
- Output formats: human-readable table, JSON, Prometheus metrics
- `config.cfg` for storing default system parameters

## Requirements

```bash
pip install -r requirements.txt
```

## Configuration

Store your system parameters in `config.cfg` (see `config.cfg.example`) so you don't need to pass them on every invocation. CLI arguments always take precedence over config values.

```ini
[system]
latitude        = 41.000
longitude       = 22.000
system_capacity = 10.0
panel_tilt      = 30.0
panel_azimuth   = 180.0
shortname       = mysystem
```

A custom config path can be specified with `--config /path/to/config.cfg`.

## Usage

```
pv-calc-forecast.py (--calculate | --forecast) [options]
```

### Calculate mode

Uses pvlib to compute theoretical DC output under clear-sky conditions.

```bash
# Current moment
./pv-calc-forecast.py --calculate --now

# Specific time
./pv-calc-forecast.py --calculate --time "2024-06-15 12:00"

# Date range (hourly by default)
./pv-calc-forecast.py --calculate --timeframe "2024-06-15:2024-06-16"

# Date range at 30-minute resolution
./pv-calc-forecast.py --calculate --timeframe "2024-06-15:2024-06-16" --resolution 30min
```

**Calculate-only options:**

| Option | Description |
|--------|-------------|
| `--now` | Use current time |
| `--time YYYY-MM-DD HH:MM` | Specific timestamp |
| `--timeframe YYYY-MM-DD:YYYY-MM-DD` | Date range |
| `--resolution` | `1min`, `10min`, `20min`, `30min`, `1H` (default: `1H`) |

### Forecast mode

Fetches a weather-based solar forecast and transposes it to your actual panel tilt/azimuth using pvlib. Two sources are available:

| Source | Flag | Horizon | Rate limit | Transposition |
|--------|------|---------|------------|---------------|
| forecast.solar | `--forecast` or `--forecast=forecast-solar` | ~2 days | 12 req/h (free) | ratio method via clear-sky |
| Open-Meteo | `--forecast=open-meteo` | 7 days | none | exact (real DNI/DHI) |

Both sources share the same cache TTL (1 hour) with separate files per source. The cache key excludes tilt/azimuth, so changing those values never requires a new API call.

```bash
# forecast.solar (default)
./pv-calc-forecast.py --forecast

# Open-Meteo (7-day horizon, no rate limit, exact transposition)
./pv-calc-forecast.py --forecast=open-meteo

# Also include next-hour power
./pv-calc-forecast.py --forecast=open-meteo --show-current-hour
```

**Forecast-only options:**

| Option | Description |
|--------|-------------|
| `--show-current-hour` | Also emit next-hour power metric |
| `--hourly-window START-END` | Minutes within each hour to emit full hourly data (default: `0-5`) |

### Shared options

| Option | Description |
|--------|-------------|
| `--latitude` | Location latitude |
| `--longitude` | Location longitude |
| `--system-capacity` | System capacity in kWp |
| `--panel-tilt` | Panel tilt in degrees |
| `--panel-azimuth` | Panel azimuth in degrees (180 = South, 90 = East, 270 = West) |
| `--shortname` | Short label for the system |
| `--timezone` | Override timezone (auto-detected from coordinates if omitted) |
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

Calculate mode — timeframe:
```
Time                   DC Power (kW)    POA Irr (W/m²)    GHI (W/m²)
---------------------  ---------------  ----------------  ------------
2024-06-15 08:00 EEST             1.64            163.74        235.82
2024-06-15 09:00 EEST             3.60            359.59        423.89
...
```

Forecast mode:
```
Date          Energy (Wh)
----------  -------------
2024-06-15        56,848
2024-06-16        54,711

Hour              Power (W)
----------------  -----------
2024-06-15 08:00       1,462
2024-06-15 09:00       3,175
...
```

### JSON

Calculate mode:
```json
{
  "timestamp": "2024-06-15 12:00 EEST",
  "dc_power_watts": 8588.15,
  "poa_irradiance": 858.82,
  "ghi": 849.22
}
```

Forecast mode:
```json
{
  "watt_hours_day": {
    "2024-06-15": 56848,
    "2024-06-16": 54711
  },
  "watts_tilted": {
    "2024-06-15 08:00:00": 1462,
    "2024-06-15 09:00:00": 3175
  }
}
```

### Prometheus

Calculate mode:
```
theoretical_pv_watts{shortname="mysystem",plant="theoretical",capacity="10.0"} 8588.15
```

Forecast mode:
```
# HELP solar_forecast_watt_hours_day Forecasted solar energy production in watt-hours per day
# TYPE solar_forecast_watt_hours_day gauge
solar_forecast_watt_hours_day{forecast="solar",shortname="mysystem",date="2024-06-15"} 56848

# HELP solar_forecast_hour_watts Forecasted solar power output per hour in watts, labelled by date and hour
# TYPE solar_forecast_hour_watts gauge
solar_forecast_hour_watts{shortname="mysystem",date="2024-06-15",hour="08:00"} 1462
solar_forecast_hour_watts{shortname="mysystem",date="2024-06-15",hour="09:00"} 3175

# HELP solar_forecast_current_hour_watts Forecasted solar power output for the current hour in watts
# TYPE solar_forecast_current_hour_watts gauge
solar_forecast_current_hour_watts{forecast="hourly",shortname="mysystem"} 3175
```

## Notes

- Timezone is auto-detected from latitude/longitude using `timezonefinder`; use `--timezone` only to override
- Panel azimuth: 180° = South, 90° = East, 270° = West
- Calculate mode filters out negligible production values (< 0.001 kW)
- Calculate mode assumes clear-sky conditions
- Forecast mode daily totals are summed from the transposed hourly values, not taken from the raw API response
- Open-Meteo uses actual DNI/DHI from its NWP model for an exact pvlib transposition; forecast.solar uses a clear-sky ratio approximation (the API only exposes total watts, not irradiance components)
- The `--hourly-window` gate limits when the full hourly data block is emitted (useful for Prometheus scrapers to avoid redundant data)

## License

MIT
