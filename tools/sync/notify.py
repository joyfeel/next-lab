import html

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


def _pick_description(bet) -> str:
    """Beginner-friendly rendering of 'what to bet', in plain language."""
    sel = (bet.selection or "").strip()
    if bet.market == "totals":
        side = {"over": "大 (Over)", "under": "小 (Under)"}.get(sel.lower(), sel)
        return f"{side} {bet.line or ''}".strip()
    if bet.market == "spreads":
        return f"{sel} {bet.line or ''}".strip()
    if bet.market == "btts":
        yn = {"yes": "是 (Yes)", "no": "否 (No)"}.get(sel.lower(), sel)
        return f"雙方都進球：{yn}"
    if bet.market == "h2h":
        return f"獨贏 {sel}"
    return f"{sel} {bet.line or ''}".strip()


def _bookmaker_deep_link(bookmaker: dict) -> str | None:
    """Event-level link. Sportsbet/PointsBet only expose this at the
    bookmaker level, not per-market or per-outcome — in practice
    market/outcome `link` is consistently null."""
    return bookmaker.get("link")


def _odds_lines(bet, bookmakers: list[dict]) -> tuple[list[str], list[tuple[str, str]]]:
    """Current-odds text per bookmaker + deep-link buttons (event page)."""
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
            suffix = "" if shown == bet.market else f"(僅提供 {shown})"
            lines.append(f"• {esc(name)} {suffix}: {esc(prices)}")
        link = _bookmaker_deep_link(bm)
        if link:
            buttons.append((f"開啟 {name}", link))
    return lines, buttons


def _bet_block(bet, matched, bookmakers: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    matchup = " @ ".join(
        x
        for x in [
            bet.away_team_en or bet.away_team_zh,
            bet.home_team_en or bet.home_team_zh,
        ]
        if x
    )
    lines: list[str] = []
    if matchup:
        lines.append(f"⚔️ <b>{esc(matchup)}</b>")
    lines.append(f"👉 推薦：<b>{esc(_pick_description(bet))}</b>")
    lines.append(f"📖 對應盤口：{esc(market_label(bet))}")
    if bet.odds_claimed:
        lines.append(f"📰 文中賠率：{esc(bet.odds_claimed)}")

    buttons: list[tuple[str, str]] = []
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
