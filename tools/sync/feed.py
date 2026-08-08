import html
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.ptt.cc"
MIRROR_URL = "https://www.pttweb.cc"
BOARD = "SportLottery"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
COOKIES = {"over18": "1"}

# How many board pages to read per scan. One page is ~20 posts, which on this
# board is well over an hour — ample at a 75s poll. The second is only there so
# a burst of activity can't push a post off the newest page between polls.
INDEX_PAGES = 2

# A board/search listing row is a `div.r-ent` block: the title link plus,
# lower down, the poster. Deleted posts keep the block but lose the link.
# Parsing goes through BeautifulSoup (class-token matching) rather than exact
# string regexes, so extra classes, reordered attributes, or whitespace from a
# PTT layout tweak don't blind it — only a wholesale restructure would, and
# that is what the mirror fallback and the canary in search_author cover.
_BOARD_HREF_ID_RE = re.compile(r"/bbs/" + BOARD + r"/(M\.\d+\.A\.[0-9A-Fa-f]+)\.html")
_MIRROR_HREF_ID_RE = re.compile(r"/bbs/" + BOARD + r"/(M\.\d+\.A\.[0-9A-Fa-f]+)")
# A real board page is tens of KB; an error stub or over-18 gate is tiny. Zero
# rows on a page above this size means the parser went silent, not the board.
_MIN_BOARD_PAGE_BYTES = 2000

# Article-page (single post) extraction still uses regexes: on a mismatch it
# degrades to "unknown author" / whole-page body rather than dropping posts.
_BODY_RE = re.compile(
    r'<div id="main-content"[^>]*>(?P<body>.*?)<span class="f2">', re.S
)
_MIRROR_BODY_RE = re.compile(
    r'itemprop="articleBody"[^>]*>(?P<body>.*?)</div>', re.S
)
# The primary's meta line is "account (nickname)"; the mirror splits the two,
# with only the account inside itemprop="name". Both appear exactly once per
# article page — push comments link to users without either marker.
_AUTHOR_RE = re.compile(
    r'article-meta-tag">作者</span><span class="article-meta-value">'
    r"(?P<author>[^\s<]+)"
)
_MIRROR_AUTHOR_RE = re.compile(r'itemprop="name"[^>]*>(?P<author>[^<]+)</span>')
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Scan:
    """Result of one author scan.

    articles: [{id, title, url}], newest first.
    source: which source(s) produced them — "board" / "search" / "mirror",
        with fallbacks combined (e.g. "search+mirror"), or "none".
    primary_broken: the board page loaded but parsed to zero rows for anyone,
        i.e. the board-listing markup changed. A canary read by main.py.
    """

    articles: list[dict]
    source: str
    primary_broken: bool


def _get(url: str, **kwargs) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                url, headers=HEADERS, cookies=COOKIES, timeout=30, **kwargs
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as err:
            last_err = err
            time.sleep(2**attempt)
    raise last_err


def article_url(article_id: str) -> str:
    return f"{BASE_URL}/bbs/{BOARD}/{article_id}.html"


def _posted_at(article_id: str) -> int:
    return int(article_id.split(".")[1])


def _parse_board_rows(page: str) -> list[dict]:
    """Every listing row on one board/search page that still has an article
    link: [{id, title, url, author}].

    The board index and PTT's own search results share the r-ent row markup,
    so both go through here. BeautifulSoup matches classes as tokens, so extra
    classes or reordered attributes on a row don't break it.
    """
    soup = BeautifulSoup(page, "html.parser")
    rows: list[dict] = []
    for ent in soup.select("div.r-ent"):
        link = ent.select_one("div.title a[href]")
        if link is None:
            continue  # deleted post: the row survives, the link doesn't
        m = _BOARD_HREF_ID_RE.search(link.get("href", ""))
        if not m:
            continue
        author_el = ent.select_one("div.author")
        rows.append(
            {
                "id": m.group(1),
                "title": link.get_text(strip=True),
                "url": article_url(m.group(1)),
                "author": author_el.get_text(strip=True) if author_el else "",
            }
        )
    return rows


def _rows_for_author(page: str, author: str) -> list[dict]:
    """`_parse_board_rows` filtered to one poster; id/title/url only."""
    want = author.lower()
    return [
        {"id": r["id"], "title": r["title"], "url": r["url"]}
        for r in _parse_board_rows(page)
        if r["author"].lower() == want
    ]


def _next_index_url(page: str) -> str | None:
    """Absolute URL of the '‹ 上頁' (older page) link, or None at the end."""
    soup = BeautifulSoup(page, "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "上頁" in a.get_text() and re.search(r"/index\d+\.html$", href):
            return f"{BASE_URL}{href}" if href.startswith("/") else href
    return None


def _from_board_index(author: str) -> tuple[list[dict], bool]:
    """(articles by author newest-first, primary_broken).

    primary_broken is the canary: the first board page came back substantial
    (a real page, not an error stub) yet parsed to zero rows for *any* author.
    A live board always shows other people's posts, so zero-rows-on-a-big-page
    means the parser went silent — the board-listing markup changed.
    """
    articles: dict[str, dict] = {}
    primary_broken = False
    want = author.lower()
    url = f"{BASE_URL}/bbs/{BOARD}/index.html"
    for i in range(INDEX_PAGES):
        page = _get(url).text
        all_rows = _parse_board_rows(page)
        if i == 0 and not all_rows and len(page) > _MIN_BOARD_PAGE_BYTES:
            primary_broken = True
        for r in all_rows:
            if r["author"].lower() == want:
                articles.setdefault(
                    r["id"], {"id": r["id"], "title": r["title"], "url": r["url"]}
                )
        nxt = _next_index_url(page)
        if not nxt:
            break
        url = nxt
    ordered = sorted(articles.values(), key=lambda a: _posted_at(a["id"]), reverse=True)
    return ordered, primary_broken


def _from_search(author: str) -> list[dict]:
    """PTT's own author search on the primary host. Same r-ent markup as the
    board, so it reuses the board parser, but it finds the author's posts even
    after they've scrolled off the shallow index window. Its index lags the
    board by minutes, so it is a coverage fallback, never the primary path.
    """
    resp = _get(f"{BASE_URL}/bbs/{BOARD}/search", params={"q": f"author:{author}"})
    articles: dict[str, dict] = {}
    for r in _rows_for_author(resp.text, author):
        articles.setdefault(r["id"], r)
    return sorted(articles.values(), key=lambda a: _posted_at(a["id"]), reverse=True)


def _from_mirror(author: str) -> list[dict]:
    """Whole-user page on the mirror — a different host and markup, so it
    survives a primary outage and a primary layout change alike. It also lists
    posts by other people shown alongside, which fetch_article's author check
    filters out.
    """
    resp = _get(f"{MIRROR_URL}/user/{author}")
    soup = BeautifulSoup(resp.text, "html.parser")
    out: dict[str, dict] = {}
    for a in soup.select("a[href]"):
        m = _MIRROR_HREF_ID_RE.search(a.get("href", ""))
        if not m:
            continue
        title_el = a.find(class_="thread-title")
        if title_el is None:
            continue
        out.setdefault(
            m.group(1),
            {
                "id": m.group(1),
                "title": title_el.get_text(strip=True),
                "url": article_url(m.group(1)),
            },
        )
    return list(out.values())


def search_author(author: str) -> Scan:
    """Latest articles by the author (newest first) plus scan health, as a Scan.

    Source chain, freshest first:
      1. board index (www.ptt.cc) — carries a post the instant it appears;
         the listing states each poster, so filtering happens before any
         article is fetched.
      2. PTT author search (www.ptt.cc) — same host, finds posts that have
         scrolled off the index window; its index lags the board by minutes.
      3. mirror /user page (pttweb.cc) — a different host and markup, so it
         survives both a primary outage and a primary layout change.

    The board is primary; the other two are consulted only when it yields
    nothing — whether the author is quiet, has scrolled off, the host is
    unreachable, or the markup changed (Scan.primary_broken). Fallback results
    are merged and de-duplicated so one broken or stale source can't hide
    posts another still sees. Only when every source comes back empty is the
    scan genuinely empty, which is what the health check must then see.
    """
    primary_broken = False
    try:
        rows, primary_broken = _from_board_index(author)
        if rows:
            return Scan(rows, "board", primary_broken)
    except requests.RequestException:
        print("primary board index unreachable")

    if primary_broken:
        print("board index parsed no rows on a live page — trying fallbacks")
    # Consulted only when the board yields nothing. search is same-host but
    # fresher than the mirror; mirror is a different host/markup and the last
    # line of defence against a primary change.
    merged: dict[str, dict] = {}
    used: list[str] = []
    for name, fn in (("search", _from_search), ("mirror", _from_mirror)):
        try:
            found = fn(author)
        except requests.RequestException:
            print(f"{name} source unreachable")
            continue
        if found:
            used.append(name)
        for a in found:
            merged.setdefault(a["id"], a)

    ordered = sorted(merged.values(), key=lambda a: _posted_at(a["id"]), reverse=True)
    return Scan(ordered, "+".join(used) if used else "none", primary_broken)


def fetch_article(article_id: str) -> dict:
    """Fetch one article: plain-text body (no push comments) plus its author.

    `author` is None when the page's markup no longer matches — callers treat
    that as "unknown", not as a mismatch, so a layout change degrades to the
    old behaviour instead of silently dropping every post.
    """
    url = article_url(article_id)
    try:
        resp = _get(url)
        body_re, author_re = _BODY_RE, _AUTHOR_RE
    except requests.RequestException:
        print("primary unreachable, using mirror")
        resp = _get(f"{MIRROR_URL}/bbs/{BOARD}/{article_id}")
        body_re, author_re = _MIRROR_BODY_RE, _MIRROR_AUTHOR_RE
    m = body_re.search(resp.text)
    raw = m.group("body") if m else resp.text
    text = html.unescape(_TAG_RE.sub("", raw))
    # Drop the signature separator onward if present
    text = text.split("\n--\n")[0]
    a = author_re.search(resp.text)
    author = html.unescape(a.group("author")).strip() if a else None
    return {"id": article_id, "url": url, "body": text.strip(), "author": author}
