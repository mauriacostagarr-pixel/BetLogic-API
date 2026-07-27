"""
Football Data Microservice
--------------------------
Proxy para la API interna de SofaScore.
Endpoints pensados para análisis pre-partido y predicciones.

Uso:
  GET /matches/date/<YYYY-MM-DD>     → partidos del día
  GET /match/<id>/stats              → corners, tarjetas, tiros, posesión
  GET /match/<id>/lineups            → alineaciones y formaciones
  GET /match/<id>/h2h                → historial cara a cara
  GET /team/<id>/form                → últimos 5 resultados del equipo
  GET /match/<id>/prematch           → resumen completo: H2H + forma local + forma visitante
  GET /health                        → estado del servicio
"""

import time
import os
import logging

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SOFASCORE_BASE = "https://www.sofascore.com/api/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

API_KEY = os.environ.get("FOOTBALL_API_KEY", "")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")


def check_api_key():
    if not API_KEY:
        return None
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def sofascore_get(endpoint: str):
    target_url = f"{SOFASCORE_BASE}{endpoint}"

    if SCRAPER_API_KEY:
        params = {
            "api_key": SCRAPER_API_KEY,
            "url": target_url,
            "keep_headers": "true",
        }
        try:
            resp = requests.get(
                "http://api.scraperapi.com",
                params=params,
                headers=HEADERS,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json(), None
        except requests.exceptions.HTTPError as e:
            logger.error("ScraperAPI HTTP error %s -> %s", e.response.status_code, target_url)
            return None, f"HTTP {e.response.status_code}"
        except requests.exceptions.RequestException as e:
            logger.error("ScraperAPI error -> %s: %s", target_url, str(e))
            return None, str(e)
    else:
        try:
            resp = requests.get(target_url, headers=HEADERS, timeout=12)
            resp.raise_for_status()
            return resp.json(), None
        except requests.exceptions.HTTPError as e:
            logger.error("HTTP error %s -> %s", e.response.status_code, target_url)
            return None, f"HTTP {e.response.status_code}"
        except requests.exceptions.RequestException as e:
            logger.error("Request error -> %s: %s", target_url, str(e))
            return None, str(e)


def parse_result(home_score, away_score, team_is_home: bool) -> str:
    if home_score is None or away_score is None:
        return "N/A"
    if home_score == away_score:
        return "D"
    home_wins = home_score > away_score
    return "W" if (home_wins == team_is_home) else "L"


def format_event(event: dict, team_id=None) -> dict:
    hs = event.get("homeScore", {}).get("current")
    as_ = event.get("awayScore", {}).get("current")
    home_id = event.get("homeTeam", {}).get("id")
    is_home = home_id == team_id if team_id else None

    base = {
        "match_id": event.get("id"),
        "date_ts": event.get("startTimestamp"),
        "tournament": event.get("tournament", {}).get("name"),
        "country": event.get("tournament", {}).get("category", {}).get("name"),
        "home_team": event.get("homeTeam", {}).get("name"),
        "home_team_id": home_id,
        "away_team": event.get("awayTeam", {}).get("name"),
        "away_team_id": event.get("awayTeam", {}).get("id"),
        "home_score": hs,
        "away_score": as_,
        "status": event.get("status", {}).get("description"),
        "winner_code": event.get("winnerCode"),
    }

    if team_id is not None:
        base["venue"] = "home" if is_home else "away"
        base["result"] = parse_result(hs, as_, is_home)

    return base


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "football-data-api", "scraper": bool(SCRAPER_API_KEY)})


@app.route("/matches/date/<date>")
def matches_by_date(date: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = sofascore_get(f"/sport/football/scheduled-events/{date}")
    if error:
        return jsonify({"error": error}), 502

    league_filter = request.args.get("league", "").lower()
    matches = []

    for event in data.get("events", []):
        m = format_event(event)
        if league_filter and league_filter not in (m.get("tournament") or "").lower():
            continue
        matches.append(m)

    return jsonify({"date": date, "total": len(matches), "matches": matches})


@app.route("/match/<int:match_id>/stats")
def match_stats(match_id: int):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = sofascore_get(f"/event/{match_id}/statistics")
    if error:
        return jsonify({"error": error}), 502

    result = {"match_id": match_id, "periods": {}}

    for period_block in data.get("statistics", []):
        period = period_block.get("period", "ALL")
        period_stats = {}
        for group in period_block.get("groups", []):
            for item in group.get("statisticsItems", []):
                key = item.get("key") or item.get("name", "").lower().replace(" ", "_")
                period_stats[key] = {
                    "name": item.get("name"),
                    "home": item.get("home"),
                    "away": item.get("away"),
                }
        result["periods"][period] = period_stats

    return jsonify(result)


@app.route("/match/<int:match_id>/lineups")
def match_lineups(match_id: int):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = sofascore_get(f"/event/{match_id}/lineups")
    if error:
        return jsonify({"error": error}), 502

    def parse_lineup(team_data: dict) -> dict:
        players = []
        for p in team_data.get("players", []):
            info = p.get("player", {})
            players.append({
                "name": info.get("name"),
                "position": p.get("position"),
                "jersey": p.get("jerseyNumber"),
                "substitute": p.get("substitute", False),
                "rating": p.get("statistics", {}).get("rating"),
            })
        return {"formation": team_data.get("formation"), "players": players}

    return jsonify({
        "match_id": match_id,
        "home": parse_lineup(data.get("home", {})),
        "away": parse_lineup(data.get("away", {})),
    })


@app.route("/match/<int:match_id>/h2h")
def match_h2h(match_id: int):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = sofascore_get(f"/event/{match_id}/h2h/events")
    if error:
        return jsonify({"error": error}), 502

    h2h = [format_event(e) for e in data.get("events", [])]
    return jsonify({"match_id": match_id, "total": len(h2h), "h2h": h2h})


@app.route("/team/<int:team_id>/form")
def team_form(team_id: int):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    limit = min(int(request.args.get("limit", 5)), 20)
    data, error = sofascore_get(f"/team/{team_id}/events/last/0")
    if error:
        return jsonify({"error": error}), 502

    events = data.get("events", [])[-limit:]
    form = [format_event(e, team_id) for e in events]
    results = [m["result"] for m in form if m["result"] != "N/A"]

    return jsonify({
        "team_id": team_id,
        "summary": {
            "W": results.count("W"),
            "D": results.count("D"),
            "L": results.count("L"),
            "string": "".join(results),
        },
        "matches": form,
    })


@app.route("/match/<int:match_id>/prematch")
def match_prematch(match_id: int):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    form_limit = min(int(request.args.get("form_limit", 5)), 10)

    event_data, error = sofascore_get(f"/event/{match_id}")
    if error:
        return jsonify({"error": error}), 502

    event = event_data.get("event", {})
    home_id = event.get("homeTeam", {}).get("id")
    away_id = event.get("awayTeam", {}).get("id")
    home_name = event.get("homeTeam", {}).get("name")
    away_name = event.get("awayTeam", {}).get("name")

    h2h_data, _ = sofascore_get(f"/event/{match_id}/h2h/events")
    h2h = [format_event(e) for e in (h2h_data or {}).get("events", [])]

    time.sleep(0.3)

    home_data, _ = sofascore_get(f"/team/{home_id}/events/last/0")
    home_events = (home_data or {}).get("events", [])[-form_limit:]
    home_form = [format_event(e, home_id) for e in home_events]
    home_results = [m["result"] for m in home_form if m["result"] != "N/A"]

    time.sleep(0.3)

    away_data, _ = sofascore_get(f"/team/{away_id}/events/last/0")
    away_events = (away_data or {}).get("events", [])[-form_limit:]
    away_form = [format_event(e, away_id) for e in away_events]
    away_results = [m["result"] for m in away_form if m["result"] != "N/A"]

    return jsonify({
        "match_id": match_id,
        "tournament": event.get("tournament", {}).get("name"),
        "date_ts": event.get("startTimestamp"),
        "home_team": {"id": home_id, "name": home_name},
        "away_team": {"id": away_id, "name": away_name},
        "h2h": {"total": len(h2h), "matches": h2h},
        "home_form": {
            "summary": {"W": home_results.count("W"), "D": home_results.count("D"), "L": home_results.count("L"), "string": "".join(home_results)},
            "matches": home_form,
        },
        "away_form": {
            "summary": {"W": away_results.count("W"), "D": away_results.count("D"), "L": away_results.count("L"), "string": "".join(away_results)},
            "matches": away_form,
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
