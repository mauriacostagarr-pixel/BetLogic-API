"""
Football Data Microservice - FotMob Edition
-------------------------------------------
Usa la API interna de FotMob (sin bloqueos desde cloud).
No requiere ScraperAPI ni proxies.

Endpoints:
  GET /health
  GET /matches/date/YYYYMMDD          → partidos del día (ej: 20260727)
  GET /match/<id>/details             → stats, corners, tarjetas, lineup, H2H
  GET /match/<id>/stats               → solo estadísticas del partido
  GET /match/<id>/lineups             → alineaciones y formaciones
  GET /match/<id>/h2h                 → historial cara a cara
  GET /team/<id>/info                 → info del equipo + últimos partidos
  GET /league/<id>/table              → tabla de posiciones
"""

import os
import logging

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

FOTMOB_BASE = "https://www.fotmob.com/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fotmob.com/",
    "Origin": "https://www.fotmob.com",
}

API_KEY = os.environ.get("FOOTBALL_API_KEY", "")


def check_api_key():
    if not API_KEY:
        return None
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def fotmob_get(endpoint: str, params: dict = None):
    url = f"{FOTMOB_BASE}{endpoint}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json(), None
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error %s -> %s", e.response.status_code, url)
        return None, f"HTTP {e.response.status_code}"
    except requests.exceptions.RequestException as e:
        logger.error("Request error -> %s: %s", url, str(e))
        return None, str(e)


def parse_result(score_str: str, is_home: bool) -> str:
    """Determina W/D/L desde el scoreStr de FotMob (ej: '2 - 1')."""
    try:
        parts = score_str.replace(" ", "").split("-")
        home = int(parts[0])
        away = int(parts[1])
        if home == away:
            return "D"
        home_wins = home > away
        return "W" if (home_wins == is_home) else "L"
    except Exception:
        return "N/A"


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "football-data-api", "source": "fotmob"})


@app.route("/matches/date/<date>")
def matches_by_date(date: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/matches", params={"date": date})
    if error:
        return jsonify({"error": error}), 502

    league_filter = request.args.get("league", "").lower()
    matches = []

    for league in data.get("leagues", []):
        league_name = league.get("name", "")
        if league_filter and league_filter not in league_name.lower():
            continue
        for m in league.get("matches", []):
            home = m.get("home", {})
            away = m.get("away", {})
            matches.append({
                "match_id": m.get("id"),
                "league": league_name,
                "league_id": league.get("id"),
                "home_team": home.get("name"),
                "home_team_id": home.get("id"),
                "away_team": away.get("name"),
                "away_team_id": away.get("id"),
                "score": m.get("status", {}).get("scoreStr"),
                "status": m.get("status", {}).get("liveTime", {}).get("short") or m.get("status", {}).get("reason", {}).get("short"),
                "started": m.get("status", {}).get("started"),
                "finished": m.get("status", {}).get("finished"),
            })

    return jsonify({"date": date, "total": len(matches), "matches": matches})


@app.route("/match/<match_id>/details")
def match_details(match_id: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/matchDetails", params={"matchId": match_id})
    if error:
        return jsonify({"error": error}), 502

    general = data.get("general", {})
    content = data.get("content", {})
    header = data.get("header", {})

    stats_raw = content.get("stats", {}).get("Periods", {}).get("All", {}).get("stats", [])
    stats = {}
    for group in stats_raw:
        for item in group.get("stats", []):
            key = item.get("key", item.get("title", "")).lower().replace(" ", "_")
            stats[key] = {
                "name": item.get("title"),
                "home": item.get("stats", [None, None])[0],
                "away": item.get("stats", [None, None])[1],
            }

    lineup_raw = content.get("lineup", {})

    def parse_lineup(team_key):
        team = lineup_raw.get(team_key, {})
        players = []
        for p in team.get("players", []):
            for player in (p if isinstance(p, list) else [p]):
                players.append({
                    "name": player.get("name", {}).get("fullName") or player.get("name", {}).get("lastName"),
                    "position": player.get("position"),
                    "jersey": player.get("shirt"),
                    "substitute": player.get("isSub", False),
                    "rating": player.get("rating", {}).get("num") if player.get("rating") else None,
                })
        return {"formation": team.get("lineup"), "players": players}

    h2h_raw = content.get("h2h", {})
    h2h_matches = []
    for m in h2h_raw.get("matches", []):
        h2h_matches.append({
            "match_id": m.get("id"),
            "date": m.get("date"),
            "home_team": m.get("home", {}).get("name"),
            "away_team": m.get("away", {}).get("name"),
            "score": m.get("status", {}).get("scoreStr"),
        })

    events = []
    for e in content.get("matchFacts", {}).get("events", {}).get("events", []):
        events.append({
            "type": e.get("type"),
            "minute": e.get("time"),
            "team": e.get("teamId"),
            "player": e.get("firstName", "") + " " + e.get("lastName", ""),
        })

    return jsonify({
        "match_id": match_id,
        "tournament": general.get("leagueName"),
        "home_team": {"id": general.get("homeTeam", {}).get("id"), "name": general.get("homeTeam", {}).get("name")},
        "away_team": {"id": general.get("awayTeam", {}).get("id"), "name": general.get("awayTeam", {}).get("name")},
        "score": header.get("status", {}).get("scoreStr"),
        "status": header.get("status", {}).get("liveTime", {}).get("short"),
        "stats": stats,
        "lineups": {
            "home": parse_lineup("homeTeam"),
            "away": parse_lineup("awayTeam"),
        },
        "h2h": {"total": len(h2h_matches), "matches": h2h_matches},
        "events": events,
    })


@app.route("/match/<match_id>/stats")
def match_stats(match_id: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/matchDetails", params={"matchId": match_id})
    if error:
        return jsonify({"error": error}), 502

    periods = data.get("content", {}).get("stats", {}).get("Periods", {})
    result = {"match_id": match_id, "periods": {}}

    for period_name, period_data in periods.items():
        period_stats = {}
        for group in period_data.get("stats", []):
            for item in group.get("stats", []):
                key = item.get("key", item.get("title", "")).lower().replace(" ", "_")
                vals = item.get("stats", [None, None])
                period_stats[key] = {
                    "name": item.get("title"),
                    "home": vals[0] if len(vals) > 0 else None,
                    "away": vals[1] if len(vals) > 1 else None,
                }
        result["periods"][period_name] = period_stats

    return jsonify(result)


@app.route("/match/<match_id>/lineups")
def match_lineups(match_id: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/matchDetails", params={"matchId": match_id})
    if error:
        return jsonify({"error": error}), 502

    lineup_raw = data.get("content", {}).get("lineup", {})

    def parse_lineup(team_key):
        team = lineup_raw.get(team_key, {})
        players = []
        for p in team.get("players", []):
            for player in (p if isinstance(p, list) else [p]):
                players.append({
                    "name": player.get("name", {}).get("fullName") or player.get("name", {}).get("lastName"),
                    "position": player.get("position"),
                    "jersey": player.get("shirt"),
                    "substitute": player.get("isSub", False),
                    "rating": player.get("rating", {}).get("num") if player.get("rating") else None,
                })
        return {"formation": team.get("lineup"), "players": players}

    return jsonify({
        "match_id": match_id,
        "home": parse_lineup("homeTeam"),
        "away": parse_lineup("awayTeam"),
    })


@app.route("/match/<match_id>/h2h")
def match_h2h(match_id: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/matchDetails", params={"matchId": match_id})
    if error:
        return jsonify({"error": error}), 502

    h2h_raw = data.get("content", {}).get("h2h", {})
    matches = []
    for m in h2h_raw.get("matches", []):
        matches.append({
            "match_id": m.get("id"),
            "date": m.get("date"),
            "home_team": m.get("home", {}).get("name"),
            "away_team": m.get("away", {}).get("name"),
            "score": m.get("status", {}).get("scoreStr"),
        })

    return jsonify({"match_id": match_id, "total": len(matches), "matches": matches})


@app.route("/team/<team_id>/info")
def team_info(team_id: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/teams", params={"id": team_id})
    if error:
        return jsonify({"error": error}), 502

    details = data.get("details", {})
    recent_matches = []
    for m in data.get("recentResults", {}).get("matches", []):
        score_str = m.get("status", {}).get("scoreStr", "")
        is_home = m.get("home", {}).get("id") == int(team_id)
        recent_matches.append({
            "match_id": m.get("id"),
            "date": m.get("status", {}).get("utcTime"),
            "home_team": m.get("home", {}).get("name"),
            "away_team": m.get("away", {}).get("name"),
            "score": score_str,
            "result": parse_result(score_str, is_home),
        })

    return jsonify({
        "team_id": team_id,
        "name": details.get("name"),
        "short_name": details.get("shortName"),
        "country": details.get("country"),
        "league": details.get("primaryLeague", {}).get("name"),
        "recent_matches": recent_matches,
    })


@app.route("/league/<league_id>/table")
def league_table(league_id: str):
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    data, error = fotmob_get("/leagues", params={"id": league_id})
    if error:
        return jsonify({"error": error}), 502

    table_raw = data.get("table", [{}])[0].get("data", {}).get("table", {}).get("all", [])
    table = []
    for row in table_raw:
        table.append({
            "position": row.get("idx"),
            "team": row.get("name"),
            "team_id": row.get("id"),
            "played": row.get("played"),
            "wins": row.get("wins"),
            "draws": row.get("draws"),
            "losses": row.get("losses"),
            "goals_for": row.get("scoresStr", "0-0").split("-")[0],
            "goals_against": row.get("scoresStr", "0-0").split("-")[1],
            "points": row.get("pts"),
        })

    return jsonify({
        "league_id": league_id,
        "name": data.get("details", {}).get("name"),
        "table": table,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
