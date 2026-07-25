import html

import requests

BOOKMAKER_NAMES = {"sportsbet": "Sportsbet", "pointsbetau": "PointsBet"}


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


def _market_lines(bet, bookmakers: list[dict]) -> tuple[list[str], list[tuple[str, str]]]:
    """Current odds text per bookmaker + deep-link buttons."""
    lines: list[str] = []
    buttons: list[tuple[str, str]] = []
    for bm in bookmakers:
        name = BOOKMAKER_NAMES.get(bm.get("key", ""), bm.get("title", "?"))
        market = next(
            (m for m in bm.get("markets", []) if m.get("key") == bet.market), None
        ) or next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
        if market:
            shown = market["key"]
            prices = " / ".join(
                f"{o.get('name')}"
                + (f" {o.get('point')}" if o.get("point") is not None else "")
                + f" @ {o.get('price')}"
                for o in market.get("outcomes", [])
            )
            suffix = "" if shown == bet.market else f"(僅 {shown})"
            lines.append(f"• {esc(name)} {suffix}: {esc(prices)}")
        link = (
            (market or {}).get("link")
            or bm.get("link")
            or next(
                (
                    o.get("link")
                    for o in (market or {}).get("outcomes", [])
                    if o.get("link")
                ),
                None,
            )
        )
        if link:
            buttons.append((f"開啟 {name}", link))
    return lines, buttons


def format_bet_message(
    author: str, title: str, article_url: str, extraction, bet, matched, bookmakers
) -> tuple[str, list[tuple[str, str]]]:
    from . import markets

    parts = [
        f"🏟 <b>{esc(author)}</b> 新推薦",
        f"<b>{esc(title)}</b>",
        "",
        f"📋 {esc(extraction.summary)}",
        "",
        f"🎯 {esc(bet.market_description)}: <b>{esc(bet.selection)}</b>"
        + (f" {esc(bet.line)}" if bet.line else "")
        + (f"(文中賠率 {esc(bet.odds_claimed)})" if bet.odds_claimed else ""),
    ]
    matchup = " @ ".join(
        x for x in [bet.away_team_en or bet.away_team_zh, bet.home_team_en or bet.home_team_zh] if x
    )
    if matchup:
        parts.append(f"⚔️ {esc(matchup)}")

    buttons: list[tuple[str, str]] = [("PTT 原文", article_url)]
    if matched and bookmakers:
        lines, link_buttons = _market_lines(bet, bookmakers)
        if lines:
            parts += ["", "💰 目前賠率:"] + lines
        buttons += link_buttons
    else:
        parts += ["", "⚠️ 未在 Sportsbet / PointsBet 找到對應賽事或市場"]
    if matchup:
        buttons.append(("Sportsbet 搜尋", markets.sportsbet_search_url(matchup.split(" @ ")[-1])))
    return "\n".join(parts), buttons
