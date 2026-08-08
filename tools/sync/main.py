import json
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import extract, feed, markets, notify
from .config import STATE_FILE, Config

# Polling every 90s, so this is ~15 minutes of retrying before giving up on an
# article — roughly the wall-clock budget the old 5-minute cadence allowed.
MAX_ATTEMPTS = 10
# Statuses that need no further work; anything else is retried next run.
TERMINAL_STATUSES = ("notified", "seeded", "failed", "skipped")
HEARTBEAT_TZ = ZoneInfo("Australia/Melbourne")
HEARTBEAT_HOUR = 20
# Consecutive empty scans before assuming the listing parser is broken.
ZERO_SCAN_ALERT_AFTER = 10


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def process_article(cfg: Config, author: str, entry: dict) -> bool:
    """Deliver one article. Returns False if it wasn't the watched author's.

    The mirror listing (used when the primary is unreachable) is a whole user
    page, so it also matches other people's posts shown alongside — only the
    article page itself states the real author, hence the check here rather
    than in the listing.
    """
    article = feed.fetch_article(entry["id"])
    actual = article["author"]
    if actual is not None and actual.lower() != author.lower():
        print(f"  skipped: written by {actual}, not {author}")
        return False

    extraction = extract.extract(entry["title"], article["body"], cfg.gemini_api_key)

    if extraction is None or not extraction.is_recommendation or not extraction.bets:
        text, buttons = notify.format_non_bet_message(
            author, entry["title"], entry["url"], extraction
        )
        notify.broadcast(cfg.telegram_bot_token, cfg.telegram_chat_ids, text, buttons)
        print("  delivered (non-recommendation)")
        return True

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
    return True


def _maybe_send_heartbeat(cfg: Config, state: dict) -> None:
    """Once a day at 20:00 Melbourne time, confirm the system is alive —
    otherwise a quiet day with no new posts looks identical to the pipeline
    being broken.

    Keyed on a separate state field from the old UTC-daily heartbeat so the
    stale value can't suppress the first evening send.
    """
    now = datetime.now(HEARTBEAT_TZ)
    if now.hour < HEARTBEAT_HOUR:
        return
    today = now.date().isoformat()
    if state.get("last_heartbeat_local_date") == today:
        return

    # Report what scanning actually returned. Claiming all-clear while the
    # listing parser silently yields nothing is worse than sending nothing.
    health = state.get("scan_health", {})
    stalled = [
        a
        for a, h in health.items()
        if h.get("zero_streak", 0) >= ZERO_SCAN_ALERT_AFTER or h.get("parser_alerted")
    ]
    scans = "、".join(
        f"{a} {h.get('last_count', 0)} 篇" for a, h in health.items()
    )
    text = (
        f"{'⚠️ 系統異常' if stalled else '✅ 系統運作正常'}({today})\n"
        f"追蹤作者：{', '.join(cfg.authors)}\n"
        f"最近掃描：{scans or '尚無資料'}\n"
        f"累計處理文章：{len(state.get('seen', {}))} 篇"
    )
    if stalled:
        text += f"\n⚠️ 掃不到文章：{', '.join(stalled)}"
    try:
        notify.broadcast(cfg.telegram_bot_token, cfg.telegram_chat_ids, text)
        state["last_heartbeat_local_date"] = today
        print(f"heartbeat sent for {today}")
    except Exception:
        print("  heartbeat delivery failed")
        traceback.print_exc()


def _track_scan_health(cfg: Config, state: dict, author: str, scan: "feed.Scan") -> None:
    """Watch for a listing that parses to nothing.

    Two independent alerts:

    * canary — the board page loaded but parsed to zero rows for anyone
      (scan.primary_broken). A live board always shows other people's posts,
      so that is a markup change: alert on the first occurrence rather than
      waiting out the zero-streak, and say so even when a fallback source
      still delivered, because the primary is silently degrading.
    * blackout — every source came back empty for a sustained run of polls.
      An empty result alone is indistinguishable from a quiet day, so this
      one waits ZERO_SCAN_ALERT_AFTER polls. Alert once per outage.
    """
    count = len(scan.articles)
    record = state.setdefault("scan_health", {}).setdefault(
        author,
        {"zero_streak": 0, "alerted": False, "last_count": 0, "parser_alerted": False},
    )

    if scan.primary_broken:
        if not record.get("parser_alerted"):
            if count > 0:
                msg = (
                    "⚠️ PTT 主站列表解析失效(canary)\n"
                    f"主站板面掃不到任何文章,已自動改用備援來源({scan.source})"
                    f"送出 {count} 篇。\nPTT 板面可能已改版,請盡快修復主站解析。"
                )
            else:
                msg = (
                    "⚠️ PTT 主站列表解析失效(canary)\n"
                    "主站板面掃不到任何文章,且所有備援來源也都掃不到。\n"
                    "PTT 可能已改版,文章列表解析失效。"
                )
            try:
                notify.broadcast(cfg.telegram_bot_token, cfg.telegram_chat_ids, msg)
                record["parser_alerted"] = True
            except Exception:
                traceback.print_exc()
    elif count > 0:
        # Primary healthy again — re-arm the canary for the next change.
        record["parser_alerted"] = False

    if count > 0:
        record.update(zero_streak=0, alerted=False, last_count=count)
        return

    record["zero_streak"] += 1
    if record["zero_streak"] < ZERO_SCAN_ALERT_AFTER or record["alerted"]:
        return
    record["alerted"] = True
    try:
        notify.broadcast(
            cfg.telegram_bot_token,
            cfg.telegram_chat_ids,
            f"⚠️ 已連續 {record['zero_streak']} 次掃不到 {author} 的任何文章\n"
            f"(上次正常掃到 {record['last_count']} 篇)\n"
            "PTT 可能已改版,文章列表解析失效",
        )
    except Exception:
        # Un-latch so the next poll tries the alert again.
        record["alerted"] = False
        traceback.print_exc()


def _scan_author(cfg: Config, state: dict, author: str, first_run: bool) -> None:
    seen = state["seen"]
    scan = feed.search_author(author)
    entries = scan.articles
    print(f"scan: {len(entries)} items (source={scan.source})")
    _track_scan_health(cfg, state, author, scan)

    for entry in entries:
        key = entry["id"]
        record = seen.get(key)
        if record and record["status"] in TERMINAL_STATUSES:
            continue

        if first_run:
            seen[key] = {"status": "seeded"}
            continue

        attempts = (record or {}).get("attempts", 0) + 1
        try:
            print(f"processing {key}")
            delivered = process_article(cfg, author, entry)
            seen[key] = {"status": "notified" if delivered else "skipped"}
        except Exception as err:
            traceback.print_exc()
            upstream_busy = (
                isinstance(err, requests.HTTPError)
                and err.response is not None
                and err.response.status_code in (429, 500, 502, 503, 504)
            )
            if upstream_busy:
                # Quota exhausted across every fallback model, or the
                # provider is briefly down (Gemini 503s do happen). Neither
                # is a bug — they clear on their own — so keep retrying
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
        # Persist per article: the poll loop restarts this process every 90s,
        # and a crash between here and the end of the scan would otherwise
        # replay every delivery made above.
        save_state(state)


def run() -> int:
    cfg = Config.from_env()
    missing = cfg.missing_keys()
    if missing:
        print(f"Missing secrets, doing nothing: {', '.join(missing)}")
        return 0

    state = load_state()
    first_run = not state["seen"]

    _maybe_send_heartbeat(cfg, state)
    # Before the scan, which may raise — otherwise a PTT outage re-sends the
    # heartbeat on every poll.
    save_state(state)

    try:
        for author in cfg.authors:
            try:
                _scan_author(cfg, state, author, first_run)
            except Exception:
                # One author's listing failing shouldn't skip the others. Count
                # it like an empty scan: unreachable and unparseable are the
                # same outage as far as "am I still seeing posts" goes.
                print(f"scan failed for {author}")
                traceback.print_exc()
                _track_scan_health(cfg, state, author, feed.Scan([], "error", False))
    finally:
        save_state(state)

    if first_run:
        print(f"first run: seeded {len(state['seen'])} existing items")
    return 0


if __name__ == "__main__":
    sys.exit(run())
