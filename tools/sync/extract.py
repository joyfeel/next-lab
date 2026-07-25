import json
import time

import requests
from pydantic import BaseModel

MODEL = "gemini-flash-latest"
API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL}:generateContent"
)

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


class Extraction(BaseModel):
    is_recommendation: bool
    summary: str
    bets: list[Bet]


def extract(title: str, body: str, api_key: str) -> Extraction | None:
    resp = None
    for attempt in range(4):
        resp = requests.post(
            API_URL,
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
    resp.raise_for_status()
    candidates = resp.json().get("candidates", [])
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        return None
    return Extraction.model_validate(json.loads(parts[0]["text"]))
