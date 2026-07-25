import difflib
import html
import traceback
from datetime import datetime, timezone

import requests

BOOKMAKER_NAMES = {"sportsbet": "Sportsbet", "pointsbetau": "PointsBet"}

# Sportsbet/PointsBet's own tab names differ per sport, not just per market
# type — e.g. baseball's spread market is "Run Line", basketball's is "Line",
# soccer's is "Handicap", and hockey's win market is "Money Line" rather than
# "Head to Head". Sourced from Sportsbet's help centre articles per sport.
MARKET_LABELS_BY_SPORT: dict[str, dict[str, str]] = {
    "baseball": {
        "h2h": "獨贏 (Head to Head)",
        "spreads": "讓分 (Run Line)",
        "totals": "大小分 (Total Runs)",
    },
    "basketball": {
        "h2h": "獨贏 (Head to Head)",
        "spreads": "讓分 (Line)",
        "totals": "大小分 (Total Points)",
    },
    "soccer": {
        "h2h": "獨贏 (Head to Head)",
        "spreads": "讓分 (Handicap)",
        "totals": "大小分 (Total Goals)",
        "btts": "雙方都得分 (Both Teams to Score)",
    },
    "hockey": {
        "h2h": "獨贏 (Money Line)",
        "spreads": "讓分 (Puck Line)",
        "totals": "大小分 (Total Goals)",
    },
    "tennis": {
        "h2h": "獨贏 (Head to Head)",
        "spreads": "讓分 (Game/Set Handicap)",
        "totals": "大小分 (Total Match Games)",
    },
}

# Fallback for a sport not in the table above (or "other").
MARKET_LABELS = {
    "h2h": "獨贏 (Head to Head)",
    "spreads": "讓分 (Line / Handicap)",
    "totals": "大小分 (Total Points / Total Runs — Over/Under)",
    "btts": "雙方都得分 (Both Teams to Score)",
}


def market_label(bet) -> str:
    by_sport = MARKET_LABELS_BY_SPORT.get(bet.sport, {})
    return by_sport.get(bet.market) or MARKET_LABELS.get(bet.market) or bet.market_description


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


def broadcast(
    token: str,
    chat_ids: list[str],
    text: str,
    buttons: list[tuple[str, str]] | None = None,
) -> None:
    """Send to every configured recipient (personal chat, group chats, ...).
    One recipient failing (e.g. bot removed from a group) shouldn't block
    the rest."""
    for chat_id in chat_ids:
        try:
            send_message(token, chat_id, text, buttons)
        except requests.RequestException:
            print(f"  failed to deliver to {chat_id}")
            traceback.print_exc()


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
    """Beginner-friendly rendering of 'what to bet', in plain language."""
    sel = (bet.selection or "").strip()
    if bet.market == "totals":
        side = {"over": "大 (Over)", "under": "小 (Under)"}.get(sel.lower(), sel)
        return f"{side} {bet.line or ''}".strip()
    if bet.market == "spreads":
        return f"{_resolve_team_name(bet, sel)} {bet.line or ''}".strip()
    if bet.market == "btts":
        yn = {"yes": "是 (Yes)", "no": "否 (No)"}.get(sel.lower(), sel)
        return f"雙方都進球：{yn}"
    if bet.market == "h2h":
        if sel.lower() == "draw":
            return "獨贏 和局 (Draw)"
        return f"獨贏 {_resolve_team_name(bet, sel)}"
    return f"{sel} {bet.line or ''}".strip()


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
    lines.append(f"👉 推薦：<b>{esc(_pick_description(bet))}</b>")
    lines.append(f"📖 對應盤口：{esc(market_label(bet))}")

    buttons: list[tuple[str, str]] = []
    if bet.odds_claimed:
        lines.append(f"📰 原文賠率：{esc(bet.odds_claimed)}")

    if matched and bookmakers:
        odds_lines, link_buttons = _odds_lines(bet, bookmakers)
        if odds_lines:
            lines.append("💰 目前賠率：")
            lines.extend(odds_lines)
        # Disambiguate buttons when an article covers more than one match.
        tag = (bet.home_team_en or bet.home_team_zh or "")[:8].strip()
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
        "",
        f"📋 {esc(extraction.summary)}",
    ]

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
    text = "\n".join(
        [
            f"📝 <b>{esc(author)}</b> 發文(非投注推薦)",
            f"<b>{esc(title)}</b>",
            "",
            esc(summary),
        ]
    )
    return text, [("PTT 原文", article_url)]
