import difflib
import html
import re
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

_ARTICLE_ID_RE = re.compile(r"M\.(\d+)\.A\.[0-9A-Fa-f]+")
_DISPLAY_TZ = ZoneInfo("Australia/Melbourne")

BOOKMAKER_NAMES = {"sportsbet": "Sportsbet", "pointsbetau": "PointsBet"}

# Sportsbet/PointsBet's own tab names differ per sport, not just per market
# type — e.g. baseball's spread market is "Run Line", basketball's is "Line",
# soccer's is "Handicap", and hockey's win market is "Money Line" rather than
# "Head to Head". Sourced from Sportsbet's help centre articles per sport.
# English only: the Chinese market name is already stated by the pick itself
# (rank_label + _pick_description) — repeating it here just says the same
# thing twice. This line's only job is "which tab to click on the site".
MARKET_LABELS_BY_SPORT: dict[str, dict[str, str]] = {
    "baseball": {"h2h": "Head to Head", "spreads": "Run Line", "totals": "Total Runs"},
    "basketball": {"h2h": "Head to Head", "spreads": "Line", "totals": "Total Points"},
    "soccer": {
        "h2h": "Head to Head",
        "spreads": "Handicap",
        "totals": "Total Goals",
        "btts": "Both Teams to Score",
    },
    "hockey": {"h2h": "Money Line", "spreads": "Puck Line", "totals": "Total Goals"},
    "tennis": {
        "h2h": "Head to Head",
        "spreads": "Game/Set Handicap",
        "totals": "Total Match Games",
    },
}

# Fallback for a sport not in the table above (or "other").
MARKET_LABELS = {
    "h2h": "Head to Head",
    "spreads": "Line / Handicap",
    "totals": "Total Points / Total Runs (Over/Under)",
    "btts": "Both Teams to Score",
}


def market_label(bet) -> str:
    """The bookmaker's own English tab name — the piece of information the
    Chinese pick description below doesn't already carry."""
    by_sport = MARKET_LABELS_BY_SPORT.get(bet.sport, {})
    return by_sport.get(bet.market) or MARKET_LABELS.get(bet.market) or bet.market_description


_MARKET_PHRASE = {
    "h2h": "獨贏",
    "spreads": "讓分",
    "totals": "大小分",
    "btts": "雙方都進球",
}


_RANK_LABELS = {1: "🥇 首選", 2: "🥈 次選", 3: "🥉 第三選擇"}


def rank_label(rank: int | None) -> str:
    if rank is None:
        return "推薦"
    return _RANK_LABELS.get(rank, f"第 {rank} 選擇")


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def send_message(
    token: str, chat_id: str, text: str, buttons: list[tuple[str, str]] | None = None
) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]
        }
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=30
    )
    resp.raise_for_status()


class DeliveryError(RuntimeError):
    """Not one configured recipient accepted the message."""


def broadcast(
    token: str,
    chat_ids: list[str],
    text: str,
    buttons: list[tuple[str, str]] | None = None,
) -> int:
    """Send to every configured recipient (personal chat, group chats, ...).

    One recipient failing (e.g. bot removed from a group) shouldn't block the
    rest, so returns how many accepted it. But a total failure must not be
    mistaken for delivery — the caller would record the article as notified
    and never retry it — hence DeliveryError when nobody got it.
    """
    delivered = 0
    for chat_id in chat_ids:
        try:
            send_message(token, chat_id, text, buttons)
            delivered += 1
        except requests.RequestException:
            print(f"  failed to deliver to {chat_id}")
            traceback.print_exc()
    if chat_ids and not delivered:
        raise DeliveryError(f"no recipient accepted the message ({len(chat_ids)} tried)")
    return delivered


def _team_bilingual(en: str | None, zh: str | None) -> str:
    """'中文名 (English Name)' — the English half is what's actually shown
    on the Sportsbet/PointsBet site, so keep both together everywhere a
    team name appears."""
    if zh and en:
        return f"{zh} ({en})"
    return en or zh or ""


def _matchup(bet) -> str:
    away = _team_bilingual(bet.away_team_en, bet.away_team_zh)
    home = _team_bilingual(bet.home_team_en, bet.home_team_zh)
    return " @ ".join(x for x in [away, home] if x)


def _resolve_team_name(bet, english_name: str) -> str:
    """If `english_name` is one of the bet's two teams, render it as
    '中文名 (English Name)'; otherwise return it unchanged (e.g. "Draw")."""
    for en, zh in ((bet.home_team_en, bet.home_team_zh), (bet.away_team_en, bet.away_team_zh)):
        if en and english_name and en.strip().lower() == english_name.strip().lower():
            return _team_bilingual(en, zh)
    return english_name


def _pick_description(bet) -> str:
    """'{中文盤口} ({Sportsbet 英文分類})：{實際下法}' — one line covering both
    what to bet and which tab to find it under, instead of stating the
    market twice (once here, once in a separate line)."""
    sel = (bet.selection or "").strip()
    market_zh = _MARKET_PHRASE.get(bet.market, bet.market_description)
    prefix = f"{market_zh} ({market_label(bet)})："
    if bet.is_team_total:
        prefix = f"{market_zh} ({market_label(bet)} · 單隊得分)："

    if bet.market == "totals":
        side = {"over": "大 (Over)", "under": "小 (Under)"}.get(sel.lower(), sel)
        return f"{prefix}{side} {bet.line or ''}".strip()
    if bet.market == "spreads":
        return f"{prefix}{_resolve_team_name(bet, sel)} {bet.line or ''}".strip()
    if bet.market == "btts":
        yn = {"yes": "是 (Yes)", "no": "否 (No)"}.get(sel.lower(), sel)
        return f"{prefix}{yn}"
    if bet.market == "h2h":
        if sel.lower() == "draw":
            return f"{prefix}和局 (Draw)"
        return f"{prefix}{_resolve_team_name(bet, sel)}"
    return f"{prefix}{sel} {bet.line or ''}".strip()


def _bookmaker_deep_link(bookmaker: dict) -> str | None:
    """Event-level link. Sportsbet/PointsBet only expose this at the
    bookmaker level, not per-market or per-outcome — in practice
    market/outcome `link` is consistently null."""
    return bookmaker.get("link")


def _parse_float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _kickoff_countdown(commence_time: str | None) -> str | None:
    if not commence_time:
        return None
    try:
        kickoff = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta_min = int((kickoff - datetime.now(timezone.utc)).total_seconds() // 60)
    if delta_min <= 0:
        return "⚠️ 已開賽或即將開賽,把握剩餘時間"
    hours, mins = divmod(delta_min, 60)
    if hours > 0:
        return f"⏰ 距離開賽還有 {hours} 小時 {mins} 分"
    return f"⏰ 距離開賽還有 {mins} 分鐘"


def _post_time_note(article_url: str) -> str | None:
    """PTT article IDs embed a Unix timestamp ('M.<ts>.A.<hash>') — no need
    to parse the page's own date text, and it's precise to the second."""
    m = _ARTICLE_ID_RE.search(article_url)
    if not m:
        return None
    try:
        posted_utc = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    except (ValueError, OSError):
        return None
    posted_local = posted_utc.astimezone(_DISPLAY_TZ)
    return f"📅 發文於 {posted_local.strftime('%m/%d %H:%M')}(墨爾本時間)"


def _matching_outcome(bet, market: dict) -> dict | None:
    """Find the outcome in this market that matches the tipster's pick."""
    sel = (bet.selection or "").strip().lower()
    outcomes = market.get("outcomes", [])
    if not outcomes:
        return None

    for o in outcomes:
        if (o.get("name") or "").strip().lower() == sel:
            return o

    aliases = {"大": "over", "小": "under", "是": "yes", "否": "no"}
    alias_sel = aliases.get(sel, sel)
    for o in outcomes:
        if (o.get("name") or "").strip().lower() == alias_sel:
            return o

    # Team-name spelling can drift between our translation and the
    # bookmaker's own naming — fall back to fuzzy matching.
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, sel, (o.get("name") or "").lower()).ratio(), o)
            for o in outcomes
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best = scored[0]
    return best if best_score >= 0.6 else None


def _odds_lines(bet, bookmakers: list[dict]) -> tuple[list[str], list[tuple[str, str]]]:
    """Current price for the tipster's exact pick per bookmaker, with a
    comparison against the odds quoted in the post and a note if the line
    itself has moved since — the two things that matter for deciding
    whether to bet now."""
    claimed = _parse_float(bet.odds_claimed)
    bet_line = _parse_float(bet.line)
    lines: list[str] = []
    buttons: list[tuple[str, str]] = []
    line_shift_noted = False

    for bm in bookmakers:
        name = BOOKMAKER_NAMES.get(bm.get("key", ""), bm.get("title", "?"))
        market = next(
            (m for m in bm.get("markets", []) if m.get("key") == bet.market), None
        ) or next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)

        if market:
            outcome = _matching_outcome(bet, market)
            if outcome:
                price = outcome.get("price")
                point = outcome.get("point")
                arrow = ""
                if claimed is not None and price is not None:
                    if price > claimed + 0.005:
                        arrow = " 📈比原文好"
                    elif price < claimed - 0.005:
                        arrow = " 📉比原文差"
                point_txt = f" {point}" if point is not None else ""
                lines.append(f"• {esc(name)}：{esc(outcome.get('name'))}{point_txt} @ {price}{arrow}")
                if (
                    not line_shift_noted
                    and point is not None
                    and bet_line is not None
                    and abs(point - bet_line) >= 0.5
                ):
                    lines.append(f"  ⚠️ 盤口已從原文的 {bet.line} 變動到 {point}")
                    line_shift_noted = True
            else:
                # Couldn't match the exact pick (e.g. an "other" market
                # shown via its h2h fallback) — display as context only.
                prices = " / ".join(
                    f"{o.get('name')} @ {o.get('price')}" for o in market.get("outcomes", [])
                )
                lines.append(f"• {esc(name)} (參考 {esc(market['key'])}): {esc(prices)}")

        link = _bookmaker_deep_link(bm)
        if link:
            buttons.append((f"開啟 {name}", link))
    return lines, buttons


def _bet_block(bet, matched, bookmakers: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    matchup = _matchup(bet)

    lines: list[str] = []
    if matchup:
        lines.append(f"⚔️ <b>{esc(matchup)}</b>")
    if matched:
        countdown = _kickoff_countdown(matched[1].get("commence_time"))
        if countdown:
            lines.append(countdown)
    lines.append(f"👉 {rank_label(bet.rank)}：<b>{esc(_pick_description(bet))}</b>")

    buttons: list[tuple[str, str]] = []
    if bet.odds_claimed:
        lines.append(f"📰 原文賠率：{esc(bet.odds_claimed)}")

    tag = (bet.home_team_en or bet.home_team_zh or "")[:8].strip()

    if bet.is_team_total:
        # No standalone team-total market on Sportsbet/PointsBet — the
        # whole-match total is a different bet with unrelated odds, so
        # don't show it as if it matched.
        if matched and bookmakers:
            lines.append("⚠️ 單隊得分盤,Sportsbet / PointsBet 無此獨立市場,請自行至下方連結查看")
            for bm in bookmakers:
                link = _bookmaker_deep_link(bm)
                if link:
                    name = BOOKMAKER_NAMES.get(bm.get("key", ""), bm.get("title", "?"))
                    label = f"開啟 {name}" + (f" · {tag}" if tag else "")
                    buttons.append((label, link))
        else:
            lines.append("⚠️ 單隊得分盤,Sportsbet / PointsBet 無此獨立市場")
    elif matched and bookmakers:
        odds_lines, link_buttons = _odds_lines(bet, bookmakers)
        if odds_lines:
            lines.append("💰 目前賠率：")
            lines.extend(odds_lines)
        # Disambiguate buttons when an article covers more than one match.
        buttons = [(f"{label} · {tag}" if tag else label, url) for label, url in link_buttons]
    else:
        lines.append("⚠️ Sportsbet / PointsBet 目前未開盤,暫無連結")

    return "\n".join(lines), buttons


def format_article_message(
    author: str,
    title: str,
    article_url: str,
    extraction,
    bet_results: list[tuple],
) -> tuple[str, list[tuple[str, str]]]:
    """One Telegram message per article, covering every bet inside it.

    bet_results: list of (bet, matched, bookmakers) — one entry per bet.
    """
    parts = [
        f"🏟 <b>{esc(author)}</b> 新推薦",
        f"<b>{esc(title)}</b>",
    ]
    post_time = _post_time_note(article_url)
    if post_time:
        parts.append(post_time)
    parts += [
        "",
        f"📋 {esc(extraction.summary)}",
    ]

    # Ranked bets are alternatives to pick ONE of (e.g. "A＞B＞C"), not
    # independent picks — surface them in that order and say so up front.
    if any(bet.rank is not None for bet, _, _ in bet_results):
        parts.append("💡 以下為同一場比賽的排序選項,依風險承受度擇一下注即可")
        bet_results = sorted(bet_results, key=lambda t: (t[0].rank is None, t[0].rank or 0))

    buttons: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for bet, matched, bookmakers in bet_results:
        block, block_buttons = _bet_block(bet, matched, bookmakers)
        parts += ["", "――――――――――", block]
        for label, url in block_buttons:
            # Same match, different bet -> same event-level deep link;
            # skip the duplicate button rather than repeating it.
            if url in seen_urls:
                continue
            seen_urls.add(url)
            buttons.append((label, url))

    buttons.append(("PTT 原文", article_url))
    return "\n".join(parts), buttons


def format_non_bet_message(
    author: str, title: str, article_url: str, extraction
) -> tuple[str, list[tuple[str, str]]]:
    """A post that isn't a betting recommendation (recap, chat, event
    thread, etc.) — still surfaced, clearly labeled as non-actionable."""
    summary = extraction.summary if extraction else "(內容無法解析)"
    header = [
        f"📝 <b>{esc(author)}</b> 發文(非投注推薦)",
        f"<b>{esc(title)}</b>",
    ]
    post_time = _post_time_note(article_url)
    if post_time:
        header.append(post_time)
    text = "\n".join(
        header
        + [
            "",
            esc(summary),
        ]
    )
    return text, [("PTT 原文", article_url)]
