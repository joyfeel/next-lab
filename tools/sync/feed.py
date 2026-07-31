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

_ENTRY_RE = re.compile(
    r'<div class="title">\s*'
    r'<a href="/bbs/' + BOARD + r'/(?P<id>M\.\d+\.A\.[0-9A-F]+)\.html">'
    r"(?P<title>[^<]+)</a>",
    re.S,
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


def search_author(author: str) -> list[dict]:
    """Latest articles by the author (newest first): [{id, title, url}].

    The primary host intermittently resets connections from datacenter IPs,
    so fall back to the mirror (same article IDs) when it's unreachable.
    """
    try:
        resp = _get(
            f"{BASE_URL}/bbs/{BOARD}/search", params={"q": f"author:{author}"}
        )
        entry_re = _ENTRY_RE
    except requests.RequestException:
        print("primary unreachable, using mirror")
        resp = _get(f"{MIRROR_URL}/user/{author}")
        entry_re = _MIRROR_ENTRY_RE
    articles = []
    for m in entry_re.finditer(resp.text):
        articles.append(
            {
                "id": m.group("id"),
                "title": html.unescape(m.group("title")).strip(),
                "url": article_url(m.group("id")),
            }
        )
    return articles


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
