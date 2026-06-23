#!/usr/bin/env python3
# pvcalc-api.py — Flask HTTP API server for pvcalc.
#
# Loads pv-calc-forecast.py once at startup (importlib, because of the hyphen in the filename).
# All endpoints call its calculation/forecast functions directly — no subprocess overhead.
#
# Primary use case: Prometheus scrape via GET /metrics (theoretical + forecast together).
# Secondary: ad-hoc JSON queries via /forecast and /calculate.
#
# Start: python3 pvcalc-api.py

import copy
import importlib.util
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytz
from flask import Flask, Response, jsonify, request
from tzlocal import get_localzone

# ──────────────────────────────────────────────────────────────────────────────
# Load pv-calc-forecast.py
# importlib is required because the hyphen in the filename makes it non-importable directly.
# ──────────────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent.resolve()
_spec = importlib.util.spec_from_file_location(
    "pvcalcforecast", _HERE / "pv-calc-forecast.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_config             = _mod.load_config
CACHE_TTL               = _mod.CACHE_TTL
parse_calculate_timespec = _mod.parse_calculate_timespec
calculate_production    = _mod.calculate_production
calculate_range_production = _mod.calculate_range_production
run_forecast_open_meteo = _mod.run_forecast_open_meteo
run_forecast_solar      = _mod.run_forecast_solar
run_forecast_solcast    = _mod.run_forecast_solcast
format_prometheus       = _mod.format_prometheus
format_json             = _mod.format_json
RESERVED_SECTIONS       = _mod.RESERVED_SECTIONS

# ──────────────────────────────────────────────────────────────────────────────
# Startup — runs once when the process starts
# Builds base_args (a SimpleNamespace that mirrors what argparse would produce)
# so all downstream pvlib functions work without modification.
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = _HERE / "config.cfg"
_START_TIME = time.time()


def _parse_window(s):
    try:
        a, b = map(int, s.split('-'))
        return (a, b)
    except Exception:
        return (0, 59)


def _parse_pv_sections(full_cfg):
    strings = []
    for name in full_cfg.sections():
        if name in RESERVED_SECTIONS:
            continue
        sec = full_cfg[name]
        strings.append({
            'name': name,
            'capacity': float(sec['capacity']),
            'tilt': float(sec['tilt']),
            'azimuth': float(sec['azimuth']),
            'solcast_resource_id': sec.get('solcast-resource-id'),
        })
    return strings


def _build_base_args(cfg, full_cfg):
    tz_str = cfg.get('timezone')
    if tz_str:
        timezone = pytz.timezone(tz_str)
    else:
        from timezonefinder import TimezoneFinder
        tz_name = TimezoneFinder().timezone_at(
            lat=float(cfg['latitude']), lng=float(cfg['longitude'])
        )
        timezone = pytz.timezone(tz_name) if tz_name else get_localzone()

    return SimpleNamespace(
        latitude=float(cfg['latitude']),
        longitude=float(cfg['longitude']),
        timezone=timezone,
        strings=_parse_pv_sections(full_cfg),
        show_days=int(cfg.get('show-days', 3)),
        hourly_window=_parse_window(cfg.get('hourly-window', '0-59')),
        resolution=cfg.get('resolution', '1H'),
        solcast_api_key=cfg.get('solcast-api-key'),
        solcast_resource_id=cfg.get('solcast-resource-id'),
        format='prometheus',
    )


_cfg, _full_cfg = load_config(DEFAULT_CONFIG)
base_args       = _build_base_args(_cfg, _full_cfg)
_api_host       = _cfg.get('host', '0.0.0.0')
_api_port       = int(_cfg.get('port', 5001))
_forecast_source = _cfg.get('forecast', 'open-meteo')


# Pre-fetch the forecast in a background thread so the first /metrics scrape
# returns immediately instead of waiting on a network call.
def _warm_cache():
    try:
        _run_forecast(_forecast_source, base_args)
        print("# Forecast cache warmed at startup", file=sys.stderr)
    except Exception as e:
        print(f"# Cache warm failed: {e}", file=sys.stderr)

threading.Thread(target=_warm_cache, daemon=True).start()


def _make_args(**overrides):
    args = copy.copy(base_args)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _parse_strings(raw):
    """Validate and normalise strings list from a request body."""
    result = []
    for s in raw:
        result.append({
            'name': str(s['name']),
            'capacity': float(s['capacity']),
            'tilt': float(s['tilt']),
            'azimuth': float(s['azimuth']),
            'solcast_resource_id': s.get('solcast_resource_id'),
        })
    return result


# In-memory cache sits on top of the file-based ForecastCache inside pv-calc-forecast.py.
# Prometheus scrapes every 15-30 s; without this layer every scrape would read and
# parse a JSON file from disk. The TTL matches the file cache (1 hour).
_mem_cache = {}


def _forecast_cache_key(source, args):
    strings_key = tuple(
        (s['name'], s['capacity'], s['tilt'], s['azimuth']) for s in args.strings
    )
    return (source, args.latitude, args.longitude, args.show_days, strings_key)


def _run_forecast(source, args):
    key = _forecast_cache_key(source, args)
    entry = _mem_cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    if source == 'open-meteo':
        results = run_forecast_open_meteo(args)
    elif source == 'solcast':
        results = run_forecast_solcast(args)
    else:
        results = run_forecast_solar(args)
    _mem_cache[key] = (time.time(), results)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Flask endpoints
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route('/')
def index():
    uptime_s = int(time.time() - _START_TIME)
    h, rem = divmod(uptime_s, 3600)
    m, s   = divmod(rem, 60)

    strings_rows = ''.join(
        f'<tr><td>{st["name"]}</td><td>{st["capacity"]} kWp</td>'
        f'<td>{st["tilt"]}°</td><td>{st["azimuth"]}°</td></tr>'
        for st in base_args.strings
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>pvcalc-api</title>
<style>
  body  {{ font-family: monospace; max-width: 820px; margin: 40px auto; padding: 0 20px; color: #222; }}
  h1   {{ font-size: 1.3em; margin-bottom: 0.2em; }}
  h2   {{ font-size: 0.95em; margin-top: 1.8em; padding-bottom: 3px;
          border-bottom: 1px solid #ddd; color: #555; text-transform: uppercase; letter-spacing: .05em; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 0.4em; }}
  th, td {{ text-align: left; padding: 3px 16px 3px 0; }}
  th   {{ color: #777; font-weight: normal; }}
  code {{ background: #f3f3f3; padding: 1px 5px; border-radius: 3px; font-size: 0.95em; }}
  a    {{ color: #0055cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .ep  {{ margin: 5px 0; }}
  .dim {{ color: #999; }}
</style>
</head>
<body>
<h1>pvcalc-api</h1>
<p class="dim">Uptime: {h}h {m}m {s}s &nbsp;&mdash;&nbsp;
   {datetime.now(base_args.timezone).strftime('%Y-%m-%d %H:%M %Z')}</p>

<h2>Site</h2>
<table>
  <tr><th>Location</th><td>{base_args.latitude}, {base_args.longitude}</td></tr>
  <tr><th>Timezone</th><td>{base_args.timezone}</td></tr>
  <tr><th>Forecast source</th><td>{_forecast_source}</td></tr>
  <tr><th>Forecast days</th><td>{base_args.show_days}</td></tr>
  <tr><th>Hourly window</th><td>{base_args.hourly_window[0]}&ndash;{base_args.hourly_window[1]} min</td></tr>
  <tr><th>Theoretical (/metrics)</th><td>yes</td></tr>
</table>

<h2>PV Strings</h2>
<table>
  <tr><th>Name</th><th>Capacity</th><th>Tilt</th><th>Azimuth</th></tr>
  {strings_rows}
</table>

<h2>Endpoints</h2>
<div class="ep"><a href="/metrics"><code>GET /metrics</code></a>
  &mdash; Prometheus scrape endpoint</div>
<div class="ep"><a href="/forecast"><code>GET /forecast</code></a>
  &mdash; Forecast &nbsp;<span class="dim">?source=open-meteo &nbsp;?days=3 &nbsp;?format=json|prometheus</span></div>
<div class="ep"><code>POST /forecast</code>
  &mdash; Forecast with custom strings
  &nbsp;<span class="dim">body: <code>{{"source":"open-meteo","strings":[{{"name":"PV1","capacity":15,"tilt":30,"azimuth":205}}]}}</code></span></div>
<div class="ep"><a href="/calculate"><code>GET /calculate</code></a>
  &mdash; Clear-sky now &nbsp;<span class="dim">?at=TIMESPEC &nbsp;?resolution=1H &nbsp;?format=json|prometheus</span></div>
<div class="ep"><code>POST /calculate</code>
  &mdash; Calculate with custom strings
  &nbsp;<span class="dim">body: <code>{{"at":"14:00","strings":[...]}}</code></span></div>
</body>
</html>"""


@app.route('/metrics')
def metrics():
    args = _make_args(format='prometheus')
    parts = []

    ts = datetime.now(args.timezone)
    string_results = [
        {'name': st['name'], 'capacity': st['capacity'],
         'result': calculate_production(args, ts, st)}
        for st in args.strings
    ]
    parts.append(format_prometheus(string_results, 'calculate', args))

    results = _run_forecast(_forecast_source, args)
    parts.append(format_prometheus(results, 'forecast', args))

    return Response(
        '\n\n'.join(parts),
        mimetype='text/plain; version=0.0.4; charset=utf-8'
    )


@app.route('/forecast', methods=['GET', 'POST'])
def forecast():
    if request.method == 'POST':
        body   = request.get_json(silent=True) or {}
        source = body.get('source', _forecast_source)
        days   = int(body.get('days', base_args.show_days))
        fmt    = body.get('format', 'json')
        strings_override = None
        if 'strings' in body:
            try:
                strings_override = _parse_strings(body['strings'])
            except (KeyError, ValueError) as e:
                return jsonify(error=f"Invalid strings: {e}"), 400
    else:
        source = request.args.get('source', _forecast_source)
        days   = int(request.args.get('days', base_args.show_days))
        fmt    = request.args.get('format', 'json')
        strings_override = None

    overrides = {'show_days': days, 'format': fmt}
    if strings_override is not None:
        overrides['strings'] = strings_override
    args = _make_args(**overrides)

    try:
        results = _run_forecast(source, args)
    except Exception as e:
        return jsonify(error=str(e)), 500

    if fmt == 'prometheus':
        return Response(
            format_prometheus(results, 'forecast', args),
            mimetype='text/plain; version=0.0.4; charset=utf-8'
        )
    return Response(format_json(results, 'forecast', args), mimetype='application/json')


@app.route('/calculate', methods=['GET', 'POST'])
def calculate():
    if request.method == 'POST':
        body       = request.get_json(silent=True) or {}
        timespec   = body.get('at', 'now')
        resolution = body.get('resolution', base_args.resolution)
        fmt        = body.get('format', 'json')
        strings_override = None
        if 'strings' in body:
            try:
                strings_override = _parse_strings(body['strings'])
            except (KeyError, ValueError) as e:
                return jsonify(error=f"Invalid strings: {e}"), 400
    else:
        timespec   = request.args.get('at', 'now')
        resolution = request.args.get('resolution', base_args.resolution)
        fmt        = request.args.get('format', 'json')
        strings_override = None

    try:
        mode, time_val = parse_calculate_timespec(timespec, base_args.timezone)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    overrides = {'resolution': resolution, 'format': fmt}
    if strings_override is not None:
        overrides['strings'] = strings_override
    args = _make_args(**overrides)

    try:
        string_results = []
        for st in args.strings:
            if mode == 'now':
                result = calculate_production(args, datetime.now(args.timezone), st)
            elif mode == 'point':
                result = calculate_production(args, time_val, st)
            else:
                start, end = time_val
                result = calculate_range_production(args, start, end, st)
            string_results.append({
                'name': st['name'],
                'capacity': st['capacity'],
                'result': result,
            })
    except Exception as e:
        return jsonify(error=str(e)), 500

    if fmt == 'prometheus':
        return Response(
            format_prometheus(string_results, 'calculate', args),
            mimetype='text/plain; version=0.0.4; charset=utf-8'
        )
    return Response(format_json(string_results, 'calculate', args), mimetype='application/json')


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"pvcalc-api listening on {_api_host}:{_api_port}", file=sys.stderr)
    print(f"  location : {base_args.latitude}, {base_args.longitude} ({base_args.timezone})", file=sys.stderr)
    print(f"  strings  : {', '.join(st['name'] for st in base_args.strings)}", file=sys.stderr)
    print(f"  forecast : {_forecast_source}", file=sys.stderr)
    app.run(host=_api_host, port=_api_port)
