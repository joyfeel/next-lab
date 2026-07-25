import json
import time

import requests
from pydantic import BaseModel

# Tried in order; a model whose quota is exhausted (429, even after the
# per-request backoff below) falls through to the next one. Excludes
# gemini-2.5-flash / -flash-lite, which 404 ("no longer available to new
# users") for this account rather than rate-limiting — retrying those would
# just waste a round trip on a permanent failure.
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.0-flash"]


def _api_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """\
You extract sports-betting recommendations from PTT SportLottery posts written \
in Traditional Chinese.

Rules:
- Matchups follow the US convention "AwayTeam @ HomeTeam" (客隊 @ 主隊).
- Translate every team name to the official English name used by international \
bookmakers (e.g. 讀賣巨人 -> Yomiuri Giants, 福岡軟銀鷹 -> Fukuoka SoftBank Hawks).
- Normalize each bet's market to one of The Odds API market keys:
  - h2h: 獨贏 / 不讓分 / straight win (selection = the team, or "Draw")
  - spreads: 讓分 / handicap (selection = the team taken, line = the handicap)
  - totals: 大小分 (selection = "Over" or "Under", line = the total)
  - btts: 雙進 / 兩隊都得分 (selection = "Yes" or "No")
  - other: anything else (correct score, first half, player props, parlays...)
- sport is one of: baseball, soccer, basketball, tennis, hockey, other.
- league_hint: the competition if identifiable (e.g. "NPB", "MLB", \
"Peru Liga 1", "J-League"). Infer from team names when not stated.
- odds_claimed: the odds quoted in the post for that selection, if any.
- rank: if the post expresses a preference order between multiple bets on \
the SAME game (e.g. "A＞B＞C", "首選/次選/備選", a numbered list, or "任選其一" \
framing), set rank to the 1-indexed preference (1 = most preferred/primary \
pick). These are alternatives to choose ONE of, not independent bets. If \
the post's bets are independent (different games, or no stated order), \
leave rank null for all of them.
- is_team_total: true if a "totals" bet refers to ONE team's own score \
(e.g. "廣島2.5大" = Hiroshima's own run total), not the two teams' combined \
score. false for the normal whole-match total. Always false for non-totals \
markets.
- If the post is not a betting recommendation (chat, results recap, ads), \
set is_recommendation to false and leave bets empty.
- summary: one short Traditional Chinese sentence describing the tip(s).
"""

_BET_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sport": {"type": "STRING"},
        "league_hint": {"type": "STRING", "nullable": True},
        "away_team_zh": {"type": "STRING", "nullable": True},
        "home_team_zh": {"type": "STRING", "nullable": True},
        "away_team_en": {"type": "STRING", "nullable": True},
        "home_team_en": {"type": "STRING", "nullable": True},
        "market": {"type": "STRING"},
        "market_description": {"type": "STRING"},
        "selection": {"type": "STRING"},
        "line": {"type": "STRING", "nullable": True},
        "odds_claimed": {"type": "STRING", "nullable": True},
        "rank": {"type": "INTEGER", "nullable": True},
        "is_team_total": {"type": "BOOLEAN"},
    },
    "required": [
        "sport",
        "league_hint",
        "away_team_zh",
        "home_team_zh",
        "away_team_en",
        "home_team_en",
        "market",
        "market_description",
        "selection",
        "line",
        "odds_claimed",
        "rank",
        "is_team_total",
    ],
}

_EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_recommendation": {"type": "BOOLEAN"},
        "summary": {"type": "STRING"},
        "bets": {"type": "ARRAY", "items": _BET_SCHEMA},
    },
    "required": ["is_recommendation", "summary", "bets"],
}


class Bet(BaseModel):
    sport: str
    league_hint: str | None
    away_team_zh: str | None
    home_team_zh: str | None
    away_team_en: str | None
    home_team_en: str | None
    market: str
    market_description: str
    selection: str
    line: str | None
    odds_claimed: str | None
    rank: int | None = None
    is_team_total: bool = False


class Extraction(BaseModel):
    is_recommendation: bool
    summary: str
    bets: list[Bet]


def _call_model(model: str, title: str, body: str, api_key: str) -> requests.Response:
    """One model, with its own short retry-with-backoff for transient 429s."""
    resp = None
    for attempt in range(4):
        resp = requests.post(
            _api_url(model),
            headers={"x-goog-api-key": api_key},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [
                    {"role": "user", "parts": [{"text": f"標題: {title}\n\n{body}"}]}
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": _EXTRACTION_SCHEMA,
                },
            },
            timeout=120,
        )
        if resp.status_code == 429 and attempt < 3:
            time.sleep(5 * (attempt + 1))
            continue
        break
    return resp


def extract(title: str, body: str, api_key: str) -> Extraction | None:
    resp = None
    for model in MODELS:
        resp = _call_model(model, title, body, api_key)
        if resp.status_code != 429:
            break
        # This model's quota is exhausted even after backoff — try the next.
        print(f"  {model} rate-limited, trying next model")
    resp.raise_for_status()
    candidates = resp.json().get("candidates", [])
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        return None
    return Extraction.model_validate(json.loads(parts[0]["text"]))
