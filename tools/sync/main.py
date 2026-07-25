import json
import sys
import traceback

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
        notify.send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text, buttons)
        print("  delivered (non-recommendation)")
        return

    bet_results = []
    for bet in extraction.bets:
        matched = None
        bookmakers: list[dict] = []
        try:
            matched = markets.find_event(bet, cfg.data_api_key)
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
    notify.send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text, buttons)
    print("  delivered")


def run() -> int:
    cfg = Config.from_env()
    missing = cfg.missing_keys()
    if missing:
        print(f"Missing secrets, doing nothing: {', '.join(missing)}")
        return 0

    state = load_state()
    seen = state["seen"]
    first_run = not seen

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
            except Exception:
                traceback.print_exc()
                if attempts >= MAX_ATTEMPTS:
                    seen[key] = {"status": "failed"}
                    try:
                        notify.send_message(
                            cfg.telegram_bot_token,
                            cfg.telegram_chat_id,
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
