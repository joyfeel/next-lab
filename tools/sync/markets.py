import difflib
from urllib.parse import quote_plus

import requests

API_BASE = "https://api.the-odds-api.com/v4"
BOOKMAKERS = "sportsbet,pointsbetau"
MATCH_THRESHOLD = 0.62
MAX_SPORTS_SCANNED = 30

SPORT_GROUPS = {
    "baseball": "Baseball",
    "soccer": "Soccer",
    "basketball": "Basketball",
    "tennis": "Tennis",
    "hockey": "Ice Hockey",
}

_sports_cache: list[dict] | None = None


def sportsbet_search_url(query: str) -> str:
    return f"https://www.sportsbet.com.au/search?query={quote_plus(query)}"


def _get(path: str, api_key: str, **params) -> requests.Response:
    params["apiKey"] = api_key
    resp = requests.get(f"{API_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp


def list_sports(api_key: str) -> list[dict]:
    global _sports_cache
    if _sports_cache is None:
        _sports_cache = _get("/sports", api_key).json()
    return _sports_cache


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _event_score(bet, event: dict) -> float:
    home, away = event.get("home_team", ""), event.get("away_team", "")
    scores = []
    if bet.home_team_en:
        scores.append(_similarity(bet.home_team_en, home))
    if bet.away_team_en:
        scores.append(_similarity(bet.away_team_en, away))
    return sum(scores) / len(scores) if scores else 0.0


def _candidate_sports(bet, api_key: str) -> list[dict]:
    group = SPORT_GROUPS.get(bet.sport)
    if group is None:
        return []
    candidates = [
        s for s in list_sports(api_key) if s.get("group") == group and s.get("active")
    ]
    hint = (bet.league_hint or "").lower()

    def rank(sport: dict) -> float:
        if not hint:
            return 0.0
        title = f"{sport.get('title', '')} {sport.get('description', '')}".lower()
        if hint in title:
            return 1.0
        return _similarity(hint, sport.get("title", ""))

    candidates.sort(key=rank, reverse=True)
    return candidates[:MAX_SPORTS_SCANNED]


def find_event(bet, api_key: str) -> tuple[str, dict] | None:
    """Find the (sport_key, event) best matching the bet's teams.

    The /events endpoint is quota-free, so scanning candidate leagues is cheap.
    """
    best: tuple[float, str, dict] | None = None
    for sport in _candidate_sports(bet, api_key):
        try:
            events = _get(f"/sports/{sport['key']}/events", api_key).json()
        except requests.RequestException:
            continue
        for event in events:
            score = _event_score(bet, event)
            if best is None or score > best[0]:
                best = (score, sport["key"], event)
        # League hints are ranked first; stop early on a confident match
        if best and best[0] >= 0.85:
            break
    if best and best[0] >= MATCH_THRESHOLD:
        return best[1], best[2]
    return None


def get_event_odds(
    api_key: str, sport_key: str, event_id: str, markets: list[str]
) -> list[dict]:
    """Return bookmaker odds (with deep links when available) for the event."""
    wanted = ",".join(dict.fromkeys(markets + ["h2h"]))
    try:
        resp = _get(
            f"/sports/{sport_key}/events/{event_id}/odds",
            api_key,
            bookmakers=BOOKMAKERS,
            markets=wanted,
            oddsFormat="decimal",
            includeLinks="true",
        )
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 422:
            # Requested market not offered for this sport; retry with h2h only
            resp = _get(
                f"/sports/{sport_key}/events/{event_id}/odds",
                api_key,
                bookmakers=BOOKMAKERS,
                markets="h2h",
                oddsFormat="decimal",
                includeLinks="true",
            )
        else:
            raise
    return resp.json().get("bookmakers", [])
