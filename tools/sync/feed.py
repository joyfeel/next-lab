import html
import re
import time

import requests

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

# Each listing row is one `r-ent` block holding the title link and, further
# down, the poster. Deleted posts keep the block but lose the link.
_ROW_MARKER = '<div class="r-ent">'
_ROW_LINK_RE = re.compile(
    r'<a href="/bbs/' + BOARD + r'/(?P<id>M\.\d+\.A\.[0-9A-F]+)\.html">'
    r"(?P<title>[^<]*)</a>"
)
_ROW_AUTHOR_RE = re.compile(r'<div class="author">(?P<author>[^<]*)</div>')
_PREV_PAGE_RE = re.compile(
    r'href="(?P<path>/bbs/' + BOARD + r'/index\d+\.html)">&lsaquo;'
)
_BODY_RE = re.compile(
    r'<div id="main-content"[^>]*>(?P<body>.*?)<span class="f2">', re.S
)
_MIRROR_ENTRY_RE = re.compile(
    r'<a href="/bbs/' + BOARD + r'/(?P<id>M\.\d+\.A\.[0-9A-F]+)"[^>]*>\s*'
    r'<span class="thread-title"[^>]*>(?P<title>[^<]+)</span>',
    re.S,
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


def _rows_for_author(page: str, author: str) -> list[dict]:
    """Listing rows on one board page written by `author`."""
    rows = []
    for block in page.split(_ROW_MARKER)[1:]:
        who = _ROW_AUTHOR_RE.search(block)
        if not who or who.group("author").strip().lower() != author.lower():
            continue
        # Bounded to before the poster: the title always precedes the meta
        # block, so an unbounded search would run past a row whose link is
        # missing and pick up the next one's — or the page footer's.
        link = _ROW_LINK_RE.search(block, 0, who.start())
        if not link:
            continue  # deleted post: the row survives, the link doesn't
        rows.append(
            {
                "id": link.group("id"),
                "title": html.unescape(link.group("title")).strip(),
                "url": article_url(link.group("id")),
            }
        )
    return rows


def _from_board_index(author: str) -> list[dict]:
    articles: dict[str, dict] = {}
    url = f"{BASE_URL}/bbs/{BOARD}/index.html"
    for _ in range(INDEX_PAGES):
        page = _get(url).text
        for row in _rows_for_author(page, author):
            articles.setdefault(row["id"], row)
        prev = _PREV_PAGE_RE.search(page)
        if not prev:
            break
        url = f"{BASE_URL}{prev.group('path')}"
    return sorted(articles.values(), key=lambda a: _posted_at(a["id"]), reverse=True)


def _from_mirror(author: str) -> list[dict]:
    """Whole-user page on the mirror — it also matches posts by other people
    shown alongside, which fetch_article's author check filters out."""
    resp = _get(f"{MIRROR_URL}/user/{author}")
    return [
        {
            "id": m.group("id"),
            "title": html.unescape(m.group("title")).strip(),
            "url": article_url(m.group("id")),
        }
        for m in _MIRROR_ENTRY_RE.finditer(resp.text)
    ]


def search_author(author: str) -> list[dict]:
    """Latest articles by the author (newest first): [{id, title, url}].

    Reads the board listing rather than PTT's author search. The listing is
    the board itself and carries a post the instant it appears; the search
    index is rebuilt on its own schedule, measured lagging between 6 and 98
    minutes, which dominated end-to-end delivery time. The listing also
    states each poster, so the author filter applies before any article is
    fetched.

    The primary host intermittently resets connections from datacenter IPs,
    so fall back to the mirror (same article IDs) when it's unreachable.
    """
    try:
        return _from_board_index(author)
    except requests.RequestException:
        print("primary unreachable, using mirror")
        return _from_mirror(author)


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
