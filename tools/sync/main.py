import json
import sys
import traceback
from datetime import datetime, timezone

import requests

from . import extract, feed, markets, notify
from .config import STATE_FILE, Config

MAX_ATTEMPTS = 3


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def process_article(cfg: Config, author: str, entry: dict) -> None:
    article = feed.fetch_article(entry["id"])
    extraction = extract.extract(entry["title"], article["body"], cfg.gemini_api_key)

    if extraction is None or not extraction.is_recommendation or not extraction.bets:
        text, buttons = notify.format_non_bet_message(
            author, entry["title"], entry["url"], extraction
        )
        notify.broadcast(cfg.telegram_bot_token, cfg.telegram_chat_ids, text, buttons)
        print("  delivered (non-recommendation)")
        return

    bet_results = []
    # Multiple bets in one article are often the same match (e.g. h2h +
    # spreads + totals on one game) — cache the event lookup per matchup
    # instead of re-scanning candidate leagues for each bet.
    event_cache: dict[tuple[str | None, str | None], tuple | None] = {}
    for bet in extraction.bets:
        matched = None
        bookmakers: list[dict] = []
        cache_key = (bet.away_team_en or bet.away_team_zh, bet.home_team_en or bet.home_team_zh)
        try:
            if cache_key in event_cache:
                matched = event_cache[cache_key]
            else:
                matched = markets.find_event(bet, cfg.data_api_key)
                event_cache[cache_key] = matched
            if matched:
                sport_key, event = matched
                wanted = [bet.market] if bet.market != "other" else ["h2h"]
                bookmakers = markets.get_event_odds(
                    cfg.data_api_key, sport_key, event["id"], wanted
                )
        except Exception:
            print("  market lookup failed for one bet, continuing without market data")
            traceback.print_exc()
        bet_results.append((bet, matched, bookmakers))

    text, buttons = notify.format_article_message(
        author, entry["title"], entry["url"], extraction, bet_results
    )
    notify.broadcast(cfg.telegram_bot_token, cfg.telegram_chat_ids, text, buttons)
    print("  delivered")


def _maybe_send_heartbeat(cfg: Config, state: dict) -> None:
    """Once per (UTC) day, confirm the system is alive — otherwise a quiet
    day with no new posts looks identical to the pipeline being broken."""
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_heartbeat_date") == today:
        return
    text = (
        f"✅ 系統運作正常({today})\n"
        f"追蹤作者：{', '.join(cfg.authors)}\n"
        f"累計處理文章：{len(state.get('seen', {}))} 篇"
    )
    try:
        notify.broadcast(cfg.telegram_bot_token, cfg.telegram_chat_ids, text)
        state["last_heartbeat_date"] = today
        print(f"heartbeat sent for {today}")
    except Exception:
        print("  heartbeat delivery failed")
        traceback.print_exc()


def run() -> int:
    cfg = Config.from_env()
    missing = cfg.missing_keys()
    if missing:
        print(f"Missing secrets, doing nothing: {', '.join(missing)}")
        return 0

    state = load_state()
    seen = state["seen"]
    first_run = not seen

    _maybe_send_heartbeat(cfg, state)

    for author in cfg.authors:
        entries = feed.search_author(author)
        print(f"scan: {len(entries)} items")
        for entry in entries:
            key = entry["id"]
            record = seen.get(key)
            if record and record["status"] in ("notified", "seeded", "failed"):
                continue

            if first_run:
                seen[key] = {"status": "seeded"}
                continue

            attempts = (record or {}).get("attempts", 0) + 1
            try:
                print(f"processing {key}")
                process_article(cfg, author, entry)
                seen[key] = {"status": "notified"}
            except Exception as err:
                traceback.print_exc()
                rate_limited = (
                    isinstance(err, requests.HTTPError)
                    and err.response is not None
                    and err.response.status_code == 429
                )
                if rate_limited:
                    # Every fallback model is exhausted right now. This
                    # isn't a bug — it clears on its own — so keep retrying
                    # every run without burning down attempts or alerting.
                    seen[key] = {"status": "pending", "attempts": 0}
                elif attempts >= MAX_ATTEMPTS:
                    seen[key] = {"status": "failed"}
                    try:
                        notify.broadcast(
                            cfg.telegram_bot_token,
                            cfg.telegram_chat_ids,
                            f"⚠️ 處理文章失敗(已重試 {MAX_ATTEMPTS} 次):\n{entry['title']}\n{entry['url']}",
                        )
                    except Exception:
                        traceback.print_exc()
                else:
                    seen[key] = {"status": "pending", "attempts": attempts}

    if first_run:
        print(f"first run: seeded {len(seen)} existing items")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
