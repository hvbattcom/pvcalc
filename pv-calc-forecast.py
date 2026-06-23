#!/usr/bin/env python3
# pv-calc-forecast.py — Solar PV clear-sky calculation and weather-based forecast tool.
#
# Two independent modes (may be combined in one run):
#   --calculate   Theoretical DC output via pvlib clear-sky model.
#   --forecast    Real weather forecast (open-meteo / forecast.solar / solcast),
#                 transposed to each string's tilt/azimuth by pvlib.
#
# Multi-string: define [PV1], [PV2], ... sections in config.cfg.
# Each section becomes a separate string= label in Prometheus output,
# plus an aggregated string="total" row when more than one string is present.

import argparse
import configparser
import json
import os
import sys
import time
import requests
import pandas as pd
import pvlib
import pytz
from datetime import datetime, timedelta
from pathlib import Path
from tabulate import tabulate
from tzlocal import get_localzone

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG = SCRIPT_DIR / "config.cfg"
CACHE_DIR      = SCRIPT_DIR / ".cache"
CACHE_TTL      = 3600                              # seconds — shared by all file-based forecast caches
RESERVED_SECTIONS = {'system', 'solar', 'DEFAULT'} # config sections that are NOT PV strings


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_path):
    cfg = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
    cfg.read(config_path)
    if 'system' in cfg:
        return dict(cfg['system']), cfg
    if 'solar' in cfg:
        print("Warning: [solar] config section is deprecated, rename it to [system]", file=sys.stderr)
        return dict(cfg['solar']), cfg
    return {}, cfg


# ──────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────────────

def parse_args_and_config():
    # First pass: resolve --config path before loading defaults
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config', default=str(DEFAULT_CONFIG))
    pre_args, _ = pre.parse_known_args()
    cfg, full_cfg = load_config(pre_args.config)

    parser = argparse.ArgumentParser(
        description='PV clear-sky calculation and solar forecast tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f'Configuration loaded from {pre_args.config} (if present).\n'
            'Forecast sources: forecast-solar (default), open-meteo, solcast\n'
            'Both --calculate and --forecast may be used together in one invocation.'
        )
    )

    parser.add_argument('--calculate', nargs='?', const='now', default=None,
                        metavar='TIMESPEC',
                        help=(
                            'Clear-sky calculation. TIMESPEC: now (default), HH:MM, '
                            'HH:MM-HH:MM, YYYY-MM-DD, YYYY-MM-DD-HH:MM, '
                            'YYYY-MM-DD-HH:MM-HH:MM, YYYY-MM-DD-HH:MM-YYYY-MM-DD-HH:MM'
                        ))
    parser.add_argument('--forecast', nargs='?', const='forecast-solar',
                        choices=['forecast-solar', 'open-meteo', 'solcast'],
                        default=None,
                        metavar='SOURCE',
                        help='Forecast source: forecast-solar, open-meteo, or solcast')

    parser.add_argument('--config', default=str(DEFAULT_CONFIG), help='Path to config file')

    # Site-level parameters (always required)
    parser.add_argument('--latitude', type=float,
                        default=float(cfg['latitude']) if 'latitude' in cfg else None,
                        help='Location latitude')
    parser.add_argument('--longitude', type=float,
                        default=float(cfg['longitude']) if 'longitude' in cfg else None,
                        help='Location longitude')
    parser.add_argument('--timezone', default=cfg.get('timezone'),
                        help='Override timezone (auto-detected from coordinates if omitted)')
    parser.add_argument('--format', choices=['human', 'json', 'prometheus'],
                        default=cfg.get('format', 'human'),
                        help='Output format (default: human)')

    # Single-string mode parameters (used when no [PV*] sections exist in config)
    parser.add_argument('--system-capacity', type=float,
                        default=float(cfg['system-capacity']) if 'system-capacity' in cfg else None,
                        help='System capacity in kWp (single-string mode)')
    parser.add_argument('--panel-tilt', type=float,
                        default=float(cfg['panel-tilt']) if 'panel-tilt' in cfg else None,
                        help='Panel tilt in degrees (single-string mode)')
    parser.add_argument('--panel-azimuth', type=float,
                        default=float(cfg['panel-azimuth']) if 'panel-azimuth' in cfg else None,
                        help='Panel azimuth in degrees, 180=South (single-string mode)')
    parser.add_argument('--shortname', default=cfg.get('shortname'),
                        help='Short identifier used as string name in single-string mode')
    parser.add_argument('--string', action='append', dest='string_args',
                        metavar='NAME:CAPACITY:TILT:AZIMUTH',
                        help='Define a PV string (repeatable). Example: --string PV1:15:30:205')

    parser.add_argument('--resolution', choices=['1min', '10min', '20min', '30min', '1H'], default='1H',
                        help='Time resolution for range calculations (default: 1H)')

    # Forecast-only options — current-hour slice is always included
    parser.add_argument('--hourly-window', default=cfg.get('hourly-window', '0-59'),
                        help='Minutes within each hour to emit full hourly data (default: 0-59)')
    parser.add_argument('--show-days', type=int, default=int(cfg.get('show-days', 3)), metavar='N',
                        help='Number of days to include in forecast output (default: 3)')
    parser.add_argument('--solcast-api-key',
                        default=cfg.get('solcast-api-key'),
                        help='Solcast API key (or set solcast-api-key in config.cfg)')
    parser.add_argument('--solcast-resource-id',
                        default=cfg.get('solcast-resource-id'),
                        help='Solcast rooftop site resource ID (auto-detected if only one site exists)')

    args = parser.parse_args()

    # Config forecast default applies only when --calculate is not explicitly passed
    if args.calculate is None and args.forecast is None:
        args.forecast = cfg.get('forecast', 'open-meteo')

    if args.calculate is None and args.forecast is None:
        parser.error("at least one of --calculate or --forecast is required")

    if args.forecast == 'solcast' and not args.solcast_api_key:
        parser.error("--forecast=solcast requires --solcast-api-key or solcast_api_key in config.cfg")

    # Site-level params always required
    missing_site = [f'--{k}' for k, v in [
        ('latitude', args.latitude), ('longitude', args.longitude)
    ] if v is None]
    if missing_site:
        parser.error(f'required (via CLI or config.cfg): {", ".join(missing_site)}')

    # Build string list: --string flags > [PV*] config sections > single-string CLI params
    pv_sections = [s for s in full_cfg.sections() if s not in RESERVED_SECTIONS]
    if args.string_args:
        args.strings = []
        for s in args.string_args:
            try:
                name, capacity, tilt, azimuth = s.split(':')
                args.strings.append({
                    'name': name,
                    'capacity': float(capacity),
                    'tilt': float(tilt),
                    'azimuth': float(azimuth),
                    'solcast_resource_id': None,
                })
            except (ValueError, TypeError):
                parser.error(f"--string format must be NAME:CAPACITY:TILT:AZIMUTH, got: {s!r}")
    elif pv_sections:
        args.strings = []
        for name in pv_sections:
            sec = full_cfg[name]
            try:
                args.strings.append({
                    'name': name,
                    'capacity': float(sec['capacity']),
                    'tilt': float(sec['tilt']),
                    'azimuth': float(sec['azimuth']),
                    'solcast_resource_id': sec.get('solcast-resource-id'),
                })
            except KeyError as e:
                parser.error(f"[{name}] section missing required key: {e}")
    else:
        missing = [f'--{k.replace("_", "-")}' for k, v in [
            ('system_capacity', args.system_capacity),
            ('panel_tilt', args.panel_tilt),
            ('panel_azimuth', args.panel_azimuth),
        ] if v is None]
        if missing:
            parser.error(f'required (via CLI or config.cfg): {", ".join(missing)}')
        args.strings = [{
            'name': args.shortname or 'pv',
            'capacity': args.system_capacity,
            'tilt': args.panel_tilt,
            'azimuth': args.panel_azimuth,
            'solcast_resource_id': args.solcast_resource_id,
        }]

    # Resolve timezone: explicit/config > derive from coordinates > system default
    # TimezoneFinder is only imported when no timezone is configured — it loads a
    # 20 MB database and costs ~4s on ARM, so we skip it whenever possible.
    if args.timezone:
        args.timezone = pytz.timezone(args.timezone)
    else:
        from timezonefinder import TimezoneFinder
        tz_name = TimezoneFinder().timezone_at(lat=args.latitude, lng=args.longitude)
        args.timezone = pytz.timezone(tz_name) if tz_name else get_localzone()

    # Parse hourly window tuple
    try:
        s, e = map(int, args.hourly_window.split('-'))
        args.hourly_window = (s, e)
    except (ValueError, AttributeError):
        parser.error("--hourly-window must be 'start-end' (e.g. '0-5')")

    return args


# ──────────────────────────────────────────────────────────────────────────────
# Calculate mode — pvlib clear-sky
# ──────────────────────────────────────────────────────────────────────────────

# Maps --resolution CLI values to pandas freq strings used in pd.date_range()
_RESOLUTION_MAP = {
    '1min': 'min', '10min': '10min', '20min': '20min', '30min': '30min', '1H': 'h'
}


def parse_calculate_timespec(spec, timezone):
    """
    Parse --calculate TIMESPEC into ('now', None), ('point', ts), or ('range', (start, end)).

    Supported formats:
        now                                 current moment
        HH:MM                               today at HH:MM
        HH:MM-HH:MM                         today, time range
        YYYY-MM-DD                          whole day
        YYYY-MM-DD-HH:MM                    specific datetime
        YYYY-MM-DD-HH:MM-HH:MM             specific date, time range
        YYYY-MM-DD-HH:MM-YYYY-MM-DD-HH:MM  cross-date range
    """
    import re

    def _localize(dt_str):
        return pd.Timestamp(dt_str).tz_localize(timezone)

    def _today_at(time_str):
        h, m = map(int, time_str.split(':'))
        return datetime.now(timezone).replace(hour=h, minute=m, second=0, microsecond=0)

    spec = spec.strip()

    if spec == 'now':
        return ('now', None)

    # HH:MM
    if re.fullmatch(r'\d{2}:\d{2}', spec):
        return ('point', _today_at(spec))

    # HH:MM-HH:MM
    m = re.fullmatch(r'(\d{2}:\d{2})-(\d{2}:\d{2})', spec)
    if m:
        return ('range', (_today_at(m.group(1)), _today_at(m.group(2))))

    # YYYY-MM-DD (whole day)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', spec):
        return ('range', (_localize(f"{spec} 00:00"), _localize(f"{spec} 23:59")))

    # YYYY-MM-DD-HH:MM
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})-(\d{2}:\d{2})', spec)
    if m:
        return ('point', _localize(f"{m.group(1)} {m.group(2)}"))

    # YYYY-MM-DD-HH:MM-HH:MM
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})-(\d{2}:\d{2})-(\d{2}:\d{2})', spec)
    if m:
        return ('range', (
            _localize(f"{m.group(1)} {m.group(2)}"),
            _localize(f"{m.group(1)} {m.group(3)}"),
        ))

    # YYYY-MM-DD-HH:MM-YYYY-MM-DD-HH:MM
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})-(\d{2}:\d{2})-(\d{4}-\d{2}-\d{2})-(\d{2}:\d{2})', spec)
    if m:
        return ('range', (
            _localize(f"{m.group(1)} {m.group(2)}"),
            _localize(f"{m.group(3)} {m.group(4)}"),
        ))

    raise ValueError(
        f"Unrecognized TIMESPEC {spec!r}. "
        "Use: now, HH:MM, HH:MM-HH:MM, YYYY-MM-DD, YYYY-MM-DD-HH:MM, "
        "YYYY-MM-DD-HH:MM-HH:MM, or YYYY-MM-DD-HH:MM-YYYY-MM-DD-HH:MM"
    )


def calculate_production(args, timestamp, string):
    location = pvlib.location.Location(
        latitude=args.latitude, longitude=args.longitude, tz=str(args.timezone)
    )
    if isinstance(timestamp, datetime):
        timestamp = pd.Timestamp(timestamp)
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize(args.timezone)

    times = pd.DatetimeIndex([timestamp])
    solar_pos = location.get_solarposition(times)
    clearsky = location.get_clearsky(times)
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=string['tilt'],
        surface_azimuth=string['azimuth'],
        dni=clearsky['dni'],
        ghi=clearsky['ghi'],
        dhi=clearsky['dhi'],
        solar_zenith=solar_pos['apparent_zenith'],
        solar_azimuth=solar_pos['azimuth']
    )
    dc_power = poa['poa_global'] * string['capacity'] / 1000
    return {
        'timestamp': timestamp,
        'ghi': float(clearsky['ghi'].iloc[0]),
        'poa_irradiance': float(poa['poa_global'].iloc[0]),
        'dc_power_kw': float(dc_power.iloc[0])
    }


def calculate_range_production(args, start, end, string):
    # Vectorized: pass the full DatetimeIndex to pvlib in one call instead of looping.
    times = pd.date_range(start=start, end=end, freq=_RESOLUTION_MAP[args.resolution])
    location = pvlib.location.Location(
        latitude=args.latitude, longitude=args.longitude, tz=str(args.timezone)
    )
    solar_pos = location.get_solarposition(times)
    clearsky   = location.get_clearsky(times)
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=string['tilt'],
        surface_azimuth=string['azimuth'],
        dni=clearsky['dni'], ghi=clearsky['ghi'], dhi=clearsky['dhi'],
        solar_zenith=solar_pos['apparent_zenith'],
        solar_azimuth=solar_pos['azimuth'],
    )
    dc_power = poa['poa_global'] * string['capacity'] / 1000
    results = []
    for i, ts in enumerate(times):
        kw = float(dc_power.iloc[i])
        if kw > 0.001:
            results.append({
                'timestamp': ts,
                'ghi': float(clearsky['ghi'].iloc[i]),
                'poa_irradiance': float(poa['poa_global'].iloc[i]),
                'dc_power_kw': kw,
            })
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Shared forecast helpers
# ──────────────────────────────────────────────────────────────────────────────

class ForecastCache:
    # File-based JSON cache under .cache/. Each source uses its own prefix so
    # entries never collide. An optional suffix distinguishes per-resource keys (Solcast).

    def __init__(self, prefix, ttl=CACHE_TTL):
        self.prefix = prefix
        self.ttl = ttl
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _path(self, lat, lon, suffix=None):
        name = f"{self.prefix}_{lat}_{lon}"
        if suffix is not None:
            name += f"_{suffix}"
        return CACHE_DIR / f"{name}.json"

    def get(self, lat, lon, suffix=None, ignore_age=False):
        path = self._path(lat, lon, suffix)
        try:
            if path.exists():
                cached = json.loads(path.read_text())
                age = time.time() - cached['cache_timestamp']
                if ignore_age:
                    print(f"# Using cached data ({int(age / 60)} min old) due to rate limit", file=sys.stderr)
                    return cached['data'], True
                if age < self.ttl:
                    return cached['data'], True
        except Exception as e:
            print(f"Cache read error: {e}", file=sys.stderr)
        return None, False

    def set(self, data, lat, lon, suffix=None):
        path = self._path(lat, lon, suffix)
        try:
            path.write_text(json.dumps({'cache_timestamp': time.time(), 'data': data}))
        except Exception as e:
            print(f"Cache write error: {e}", file=sys.stderr)


def _daily_totals_from_hourly(watts_tilted):
    """Sum round-hour watts by local date to get watt-hours/day."""
    daily = {}
    for ts_str, watts in watts_tilted.items():
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if dt.minute != 0 or dt.second != 0:
            continue
        date = dt.strftime('%Y-%m-%d')
        daily[date] = daily.get(date, 0.0) + watts
    return daily


def _next_hour_power(watts_tilted):
    next_hour = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return watts_tilted.get(next_hour.strftime("%Y-%m-%d %H:%M:%S"), 0)


def _build_forecast_result(args, watts_tilted, name):
    today = datetime.now().date()
    cutoff = today + timedelta(days=args.show_days)

    filtered = {}
    for ts_str, w in watts_tilted.items():
        try:
            dt_date = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").date()
        except ValueError:
            continue
        if today <= dt_date < cutoff:
            filtered[ts_str] = w

    return {
        'name': name,
        'watts_tilted': filtered,
        'watt_hours_day': _daily_totals_from_hourly(filtered),
        'current_hour_watts': _next_hour_power(watts_tilted),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Forecast source: forecast.solar
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_forecast_solar_normalized(lat, lon, cache):
    # Fetch at tilt=0, kwp=1 so the cache is independent of string params.
    # Each string is scaled and transposed locally in _transpose_forecast_solar_string.
    cached, is_cached = cache.get(lat, lon)
    if is_cached:
        return cached, True

    url = f"https://api.forecast.solar/estimate/{lat}/{lon}/0/0/1"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        if response.status_code == 429 or (
            isinstance(data.get('message'), dict) and
            data['message'].get('ratelimit', {}).get('remaining') == 0
        ):
            raise requests.exceptions.RequestException("Rate limit exceeded")

        cache.set(data, lat, lon)
        return data, False

    except requests.exceptions.RequestException as e:
        if "Rate limit exceeded" in str(e):
            print("# Rate limit exceeded, attempting to use expired cache", file=sys.stderr)
            cached, is_cached = cache.get(lat, lon, ignore_age=True)
            if is_cached:
                return cached, True
        print(f"Error fetching forecast.solar data: {e}", file=sys.stderr)
        sys.exit(1)


def _transpose_forecast_solar_string(watts_horizontal, string, solar_pos, clearsky, ts_keys):
    # forecast.solar only returns total watts — no DNI/DHI components.
    # Workaround: compute a ratio = POA_tilted / GHI_clearsky from pvlib's clear-sky model,
    # then multiply the API watts by that ratio to approximate the tilted output.
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=string['tilt'],
        surface_azimuth=string['azimuth'],
        solar_zenith=solar_pos['apparent_zenith'],
        solar_azimuth=solar_pos['azimuth'],
        dni=clearsky['dni'],
        ghi=clearsky['ghi'],
        dhi=clearsky['dhi'],
    )
    ghi_cs    = clearsky['ghi']
    poa_tilted = poa['poa_global']

    ratio = pd.Series(0.0, index=solar_pos.index)
    mask = ghi_cs >= 1.0
    ratio[mask] = poa_tilted[mask] / ghi_cs[mask]

    watts_series = pd.Series(
        [float(watts_horizontal[k]) for k in ts_keys], index=solar_pos.index
    )
    adjusted = (watts_series * ratio * string['capacity']).clip(lower=0.0).fillna(0.0)
    return {k: float(adjusted.iloc[i]) for i, k in enumerate(ts_keys)}


def run_forecast_solar(args):
    cache = ForecastCache('forecast_solar')
    data, is_cached = _fetch_forecast_solar_normalized(args.latitude, args.longitude, cache)
    if is_cached:
        print("# Using cached forecast.solar data (less than 1 hour old)", file=sys.stderr)

    location = pvlib.location.Location(
        latitude=args.latitude, longitude=args.longitude, tz=str(args.timezone)
    )
    # solar_pos and clearsky are identical for all strings — compute once, reuse per string.
    watts_horizontal = data['result']['watts']
    ts_keys  = list(watts_horizontal.keys())
    times    = pd.to_datetime(ts_keys, format="%Y-%m-%d %H:%M:%S").tz_localize(args.timezone)
    clearsky  = location.get_clearsky(times, model='ineichen')
    solar_pos = location.get_solarposition(times)

    results = []
    for string in args.strings:
        watts_tilted = _transpose_forecast_solar_string(
            watts_horizontal, string, solar_pos, clearsky, ts_keys
        )
        results.append(_build_forecast_result(args, watts_tilted, string['name']))
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Forecast source: Open-Meteo
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_open_meteo(lat, lon, cache):
    # Returns raw DNI/GHI/DHI series. Cache key is lat/lon only — string params
    # don't affect the weather data, only the pvlib transposition step does.
    cached, is_cached = cache.get(lat, lon)
    if is_cached:
        return cached, True

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=shortwave_radiation,direct_normal_irradiance,diffuse_radiation"
        f"&forecast_days=7&timezone=UTC"
    )
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        cache.set(data, lat, lon)
        return data, False
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Open-Meteo data: {e}", file=sys.stderr)
        sys.exit(1)


def _calc_open_meteo_string(string, solar_pos, ghi, dni, dhi):
    # Real DNI/DHI from Open-Meteo's NWP model → exact pvlib transposition, no approximation.
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=string['tilt'],
        surface_azimuth=string['azimuth'],
        solar_zenith=solar_pos['apparent_zenith'],
        solar_azimuth=solar_pos['azimuth'],
        dni=dni, ghi=ghi, dhi=dhi,
    )
    watts = (poa['poa_global'] * string['capacity']).clip(lower=0).fillna(0)
    return {ts.strftime("%Y-%m-%d %H:%M:%S"): float(w) for ts, w in watts.items()}


def run_forecast_open_meteo(args):
    cache = ForecastCache('open_meteo')
    raw, is_cached = _fetch_open_meteo(args.latitude, args.longitude, cache)
    if is_cached:
        print("# Using cached Open-Meteo data (less than 1 hour old)", file=sys.stderr)

    location = pvlib.location.Location(
        latitude=args.latitude, longitude=args.longitude, tz=str(args.timezone)
    )
    # solar_pos is location/time-dependent only — compute once and reuse for every string.
    hourly = raw['hourly']
    times = (pd.to_datetime(hourly['time'])
             .tz_localize('UTC')
             .tz_convert(args.timezone))
    ghi = pd.Series(hourly['shortwave_radiation'],      index=times, dtype=float).fillna(0).clip(lower=0)
    dni = pd.Series(hourly['direct_normal_irradiance'], index=times, dtype=float).fillna(0).clip(lower=0)
    dhi = pd.Series(hourly['diffuse_radiation'],        index=times, dtype=float).fillna(0).clip(lower=0)
    solar_pos = location.get_solarposition(times)

    results = []
    for string in args.strings:
        watts_tilted = _calc_open_meteo_string(string, solar_pos, ghi, dni, dhi)
        results.append(_build_forecast_result(args, watts_tilted, string['name']))
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Forecast source: Solcast
# ──────────────────────────────────────────────────────────────────────────────

SOLCAST_TTL = 14400  # 4 h — free tier allows only 10 API calls/day; expired cache used on rate-limit


def _list_solcast_sites(api_key):
    """List registered Solcast rooftop sites; cached for 24 hours."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / "solcast_sites.json"
    try:
        if path.exists():
            cached = json.loads(path.read_text())
            if time.time() - cached['cache_timestamp'] < 86400:
                return cached['data']
    except Exception:
        pass

    try:
        response = requests.get(
            "https://api.solcast.com.au/rooftop_sites?format=json",
            headers={'Authorization': f'Bearer {api_key}'}
        )
        if response.status_code in (401, 403):
            print("Error: Solcast API key rejected. Check your key at solcast.com.", file=sys.stderr)
            sys.exit(1)
        response.raise_for_status()
        sites = response.json().get('sites', [])
        try:
            path.write_text(json.dumps({'cache_timestamp': time.time(), 'data': sites}))
        except Exception as e:
            print(f"Cache write error: {e}", file=sys.stderr)
        return sites
    except requests.exceptions.RequestException as e:
        print(f"Error listing Solcast sites: {e}", file=sys.stderr)
        sys.exit(1)


def _fetch_solcast_rooftop(resource_id, api_key):
    """Fetch rooftop forecast for a Solcast resource_id; 4-hour cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"solcast_{resource_id}.json"

    try:
        if path.exists():
            cached = json.loads(path.read_text())
            if time.time() - cached['cache_timestamp'] < SOLCAST_TTL:
                return cached['data'], True
    except Exception as e:
        print(f"Cache read error: {e}", file=sys.stderr)

    url = f"https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts?hours=168&format=json"
    try:
        response = requests.get(url, headers={'Authorization': f'Bearer {api_key}'})
        if response.status_code in (401, 403):
            print("Error: Solcast API key rejected (401/403). Check your key or account plan.", file=sys.stderr)
            sys.exit(1)
        if response.status_code == 429:
            if path.exists():
                try:
                    cached = json.loads(path.read_text())
                    age = time.time() - cached['cache_timestamp']
                    print(f"# Rate limit hit — using cached Solcast data ({int(age / 60)} min old)", file=sys.stderr)
                    return cached['data'], True
                except Exception:
                    pass
            print("Error: Solcast daily API limit reached (10 calls/day on free tier)", file=sys.stderr)
            sys.exit(1)
        response.raise_for_status()
        data = response.json()
        try:
            path.write_text(json.dumps({'cache_timestamp': time.time(), 'data': data}))
        except Exception as e:
            print(f"Cache write error: {e}", file=sys.stderr)
        return data, False
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Solcast data: {e}", file=sys.stderr)
        sys.exit(1)


def _process_solcast_rooftop(timezone, raw):
    """
    Convert Solcast pv_estimate (kW per 30-min period) to hourly watts.
    pv_estimate is already computed for the site's tilt/azimuth as registered on
    Solcast — no pvlib transposition needed. Sub-hourly periods are averaged per hour.
    """
    half_hours = []
    for entry in raw['forecasts']:
        period_end = pd.Timestamp(entry['period_end']).tz_convert(timezone)
        minutes = 60 if entry.get('period') == 'PT60M' else 30
        period_start = period_end - pd.Timedelta(minutes=minutes)
        half_hours.append((period_start, float(entry.get('pv_estimate') or 0)))

    hourly = {}
    for start, kw in half_hours:
        hour_ts = start.replace(minute=0, second=0, microsecond=0)
        hourly.setdefault(hour_ts, []).append(kw)

    return {
        ts.strftime("%Y-%m-%d %H:%M:%S"): sum(kw_list) / len(kw_list) * 1000
        for ts, kw_list in sorted(hourly.items())
    }


def run_forecast_solcast(args):
    results = []
    for string in args.strings:
        resource_id = string.get('solcast_resource_id') or args.solcast_resource_id
        if not resource_id:
            sites = _list_solcast_sites(args.solcast_api_key)
            if not sites:
                print("Error: No Solcast rooftop sites found. Register one at solcast.com.", file=sys.stderr)
                sys.exit(1)
            if len(sites) > 1:
                site_list = "\n".join(
                    f"  {s['resource_id']}: {s.get('name', '')} ({s.get('location', '')})"
                    for s in sites
                )
                print(
                    f"Error: Multiple Solcast sites found. Specify solcast_resource_id in "
                    f"[{string['name']}] config section or via --solcast-resource-id:\n{site_list}",
                    file=sys.stderr
                )
                sys.exit(1)
            resource_id = sites[0]['resource_id']

        data, is_cached = _fetch_solcast_rooftop(resource_id, args.solcast_api_key)
        if is_cached:
            print(f"# Using cached Solcast data for {string['name']} (less than 4 hours old)", file=sys.stderr)

        watts_tilted = _process_solcast_rooftop(args.timezone, data)
        results.append(_build_forecast_result(args, watts_tilted, string['name']))
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Output formatters
# ──────────────────────────────────────────────────────────────────────────────

def _label_str(labels):
    return '{' + ','.join(f'{k}="{v}"' for k, v in labels.items()) + '}'


def _sum_by_key(dicts):
    """Sum values across a list of dicts with the same keys."""
    total = {}
    for d in dicts:
        for k, v in d.items():
            total[k] = total.get(k, 0.0) + v
    return total


def format_human(data, mode, args):
    if mode == 'calculate':
        lines = []
        for sr in data:
            if len(data) > 1:
                lines.append(f"=== {sr['name']} ===")
            result = sr['result']
            points = result if isinstance(result, list) else [result]
            if len(points) == 1:
                p = points[0]
                lines.append(tabulate([
                    ["Time", p['timestamp'].strftime('%Y-%m-%d %H:%M %Z')],
                    ["DC Power", f"{p['dc_power_kw']:.2f} kW"],
                    ["POA Irradiance", f"{p['poa_irradiance']:.2f} W/m²"],
                    ["GHI", f"{p['ghi']:.2f} W/m²"],
                ], tablefmt="simple"))
            else:
                rows = [
                    [p['timestamp'].strftime('%Y-%m-%d %H:%M %Z'),
                     f"{p['dc_power_kw']:.2f}",
                     f"{p['poa_irradiance']:.2f}",
                     f"{p['ghi']:.2f}"]
                    for p in points
                ]
                lines.append(tabulate(rows,
                                      headers=["Time", "DC Power (kW)", "POA Irr (W/m²)", "GHI (W/m²)"],
                                      tablefmt="simple"))
        return "\n".join(lines)

    # Forecast mode
    lines = []
    for sr in data:
        if len(data) > 1:
            lines.append(f"=== {sr['name']} ===")
        daily_rows = [[date, f"{int(wh):,}"]
                      for date, wh in sorted(sr['watt_hours_day'].items())]
        lines.append(tabulate(daily_rows, headers=["Date", "Energy (Wh)"], tablefmt="simple"))

        hourly_rows = []
        for ts_str, watts in sorted(sr['watts_tilted'].items()):
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if dt.minute != 0 or dt.second != 0:
                continue
            hourly_rows.append([dt.strftime('%Y-%m-%d %H:00'), f"{int(watts):,}"])
        if hourly_rows:
            lines.append("")
            lines.append(tabulate(hourly_rows, headers=["Hour", "Power (W)"], tablefmt="simple"))

        lines.append(f"\nNext hour: {int(sr['current_hour_watts']):,} W")

    return "\n".join(lines)


def format_json(data, mode, args):
    def _serialize_calc(sr):
        result = sr['result']
        points = result if isinstance(result, list) else [result]
        serialised = [{
            'timestamp': p['timestamp'].strftime('%Y-%m-%d %H:%M %Z'),
            'dc_power_watts': round(p['dc_power_kw'] * 1000, 2),
            'poa_irradiance': round(p['poa_irradiance'], 2),
            'ghi': round(p['ghi'], 2),
        } for p in points]
        return serialised[0] if len(serialised) == 1 else serialised

    if mode == 'calculate':
        if len(data) == 1:
            return json.dumps(_serialize_calc(data[0]), indent=2)
        return json.dumps({sr['name']: _serialize_calc(sr) for sr in data}, indent=2)

    def _serialize_forecast(sr):
        return {
            'watt_hours_day': {k: round(v) for k, v in sorted(sr['watt_hours_day'].items())},
            'watts_tilted': {k: round(v) for k, v in sorted(sr['watts_tilted'].items())},
            'current_hour_watts': round(sr['current_hour_watts']),
        }

    if len(data) == 1:
        return json.dumps(_serialize_forecast(data[0]), indent=2)
    return json.dumps({sr['name']: _serialize_forecast(sr) for sr in data}, indent=2)


def format_prometheus(data, mode, args):
    lines = []
    multi = len(data) > 1

    if mode == 'calculate':
        lines.append("# HELP theoretical_pv_w Theoretical DC power output under clear-sky conditions in watts")
        lines.append("# TYPE theoretical_pv_w gauge")
        total_watts = 0.0
        total_capacity = 0.0
        for sr in data:
            result = sr['result']
            points = result if isinstance(result, list) else [result]
            watts = points[-1]['dc_power_kw'] * 1000
            total_watts += watts
            total_capacity += sr['capacity']
            lbls = {'string': sr['name'], 'plant': 'theoretical', 'capacity': str(sr['capacity'])}
            lines.append(f'theoretical_pv_w{_label_str(lbls)} {watts:.2f}')
        if multi:
            lbls = {'string': 'total', 'plant': 'theoretical', 'capacity': str(total_capacity)}
            lines.append(f'theoretical_pv_w{_label_str(lbls)} {total_watts:.2f}')
        return "\n".join(lines)

    # Forecast mode
    total_watt_hours_day = _sum_by_key([sr['watt_hours_day'] for sr in data])
    total_watts_tilted = _sum_by_key([sr['watts_tilted'] for sr in data])
    total_current_hour = sum(sr['current_hour_watts'] for sr in data)

    lines.append("# HELP solar_forecast_day_wh Forecasted solar energy production in watt-hours per day")
    lines.append("# TYPE solar_forecast_day_wh gauge")
    for sr in data:
        for date, wh in sorted(sr['watt_hours_day'].items()):
            lbls = {'string': sr['name'], 'date': date}
            lines.append(f'solar_forecast_day_wh{_label_str(lbls)} {round(wh)}')
    if multi:
        for date, wh in sorted(total_watt_hours_day.items()):
            lbls = {'string': 'total', 'date': date}
            lines.append(f'solar_forecast_day_wh{_label_str(lbls)} {round(wh)}')

    current_minute = datetime.now().minute
    hw = args.hourly_window
    if hw[0] <= current_minute <= hw[1]:
        lines.append("\n# HELP solar_forecast_hour_w Forecasted solar power output per hour in watts")
        lines.append("# TYPE solar_forecast_hour_w gauge")
        for sr in data:
            for ts_str, watts in sorted(sr['watts_tilted'].items()):
                try:
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if dt.minute != 0 or dt.second != 0:
                    continue
                lbls = {'string': sr['name'], 'date': dt.strftime('%Y-%m-%d'), 'hour': dt.strftime('%H:00')}
                lines.append(f'solar_forecast_hour_w{_label_str(lbls)} {round(watts)}')
        if multi:
            for ts_str, watts in sorted(total_watts_tilted.items()):
                try:
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if dt.minute != 0 or dt.second != 0:
                    continue
                lbls = {'string': 'total', 'date': dt.strftime('%Y-%m-%d'), 'hour': dt.strftime('%H:00')}
                lines.append(f'solar_forecast_hour_w{_label_str(lbls)} {round(watts)}')

    lines.append("\n# HELP solar_forecast_current_hour_w Forecasted solar power output for the current hour in watts")
    lines.append("# TYPE solar_forecast_current_hour_w gauge")
    for sr in data:
        lbls = {'string': sr['name']}
        lines.append(f'solar_forecast_current_hour_w{_label_str(lbls)} {round(sr["current_hour_watts"])}')
    if multi:
        lbls = {'string': 'total'}
        lines.append(f'solar_forecast_current_hour_w{_label_str(lbls)} {round(total_current_hour)}')

    return "\n".join(lines)


def _render(result, mode, args):
    if args.format == 'human':
        return format_human(result, mode, args)
    elif args.format == 'json':
        return format_json(result, mode, args)
    elif args.format == 'prometheus':
        return format_prometheus(result, mode, args)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args_and_config()

    try:
        outputs = []

        if args.calculate is not None:
            mode, time_val = parse_calculate_timespec(args.calculate, args.timezone)
            string_results = []
            for string in args.strings:
                if mode == 'now':
                    result = calculate_production(args, datetime.now(args.timezone), string)
                elif mode == 'point':
                    result = calculate_production(args, time_val, string)
                else:
                    start, end = time_val
                    result = calculate_range_production(args, start, end, string)
                    if not result:
                        print(f"No significant production for {string['name']} in the specified range.",
                              file=sys.stderr)
                        continue
                string_results.append({
                    'name': string['name'],
                    'capacity': string['capacity'],
                    'result': result,
                })
            if string_results:
                outputs.append(_render(string_results, 'calculate', args))
            elif mode == 'range':
                sys.exit(0)

        if args.forecast is not None:
            if args.forecast == 'open-meteo':
                results = run_forecast_open_meteo(args)
            elif args.forecast == 'solcast':
                results = run_forecast_solcast(args)
            else:
                results = run_forecast_solar(args)
            outputs.append(_render(results, 'forecast', args))

        print("\n".join(outputs))

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
