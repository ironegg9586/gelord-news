#!/usr/bin/env python3
"""MyNews: local news page server.

Serves the page on http://localhost:8888 (this device only), keeps your topics in
topics.json, and fetches headlines from the Bing News RSS feed. No AI is used.
"""
import base64
import datetime
import email.utils
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOPICS_FILE = os.path.join(HERE, "topics.json")
INDEX_FILE = os.path.join(HERE, "index.html")
FEEDS_FILE = os.path.join(HERE, "feeds.json")
HOST, PORT = "127.0.0.1", 8888
CACHE_SECONDS = 15 * 60
MAX_ITEMS = 30      # most stories kept per topic (the page shows Top 5 / 10 / 20)
WANT_ITEMS = 20     # try to find at least this many (the biggest "Top" choice)
MAX_TOPICS = 20

# Domains of outlets you trust. Articles from other sites still show, marked "Unverified".
TRUSTED_DOMAINS = {
    "engadget.com", "gsmarena.com", "thurrott.com", "theverge.com", "arstechnica.com",
    "reuters.com", "bbc.com", "bbc.co.uk", "theguardian.com", "apnews.com", "cnn.com",
    "ign.com", "pcgamer.com", "polygon.com", "techradar.com", "tomsguide.com",
    "gamespot.com", "kotaku.com", "rockpapershotgun.com", "eurogamer.net", "gamedev.net",
    "gamepressure.com", "steampowered.com", "valvesoftware.com", "steamdeck.com",
    "bloomberg.com", "wired.com", "pcmag.com", "anandtech.com", "tomshardware.com",
}

# Words that trigger the "Needs attention" highlight.
ATTENTION_WORDS = [
    "recall", "delay", "price increase", "price hike", "warning", "security", "breach",
    "vulnerability", "hack", "bug", "outage", "lawsuit", "ban", "discontinued",
    "shortage", "urgent", "sold out", "data leak", "scam",
]

_cache_lock = threading.Lock()
_topics_lock = threading.Lock()
_cache = {}


def load_topics():
    try:
        with open(TOPICS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [t for t in data if isinstance(t, str)]
    except (OSError, ValueError):
        return []


def save_topics(topics):
    tmp = TOPICS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(topics, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TOPICS_FILE)


def strip_html(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def domain_of(url):
    host = urllib.parse.urlparse(url or "").hostname or ""
    return host[4:] if host.startswith("www.") else host


def is_trusted(domain):
    return any(domain == d or domain.endswith("." + d) for d in TRUSTED_DOMAINS)


def attention_hits(text):
    low = text.lower()
    return [w for w in ATTENTION_WORDS if w in low]


BING_URL = "https://www.bing.com/news/search"
BING_HOSTS = ("bing.com",)
FEED_MAX_BYTES = 1024 * 1024
# After Bing refuses us, wait this long before asking again (no hammering).
BING_BLOCK_WAIT = 15 * 60
_bing_blocked_until = 0.0


class NewsError(Exception):
    """A news-source problem with a plain message that is safe to show on the page."""


def is_bing(host):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in BING_HOSTS)


def real_link(href):
    """Turn a Bing wrapper link (bing.com/news/apiclick.aspx?...&url=...) into the real URL.

    Only reads the url= value; never makes a request.
    """
    href = (href or "").strip()
    p = urllib.parse.urlparse(href)
    if is_bing(p.hostname):
        return urllib.parse.parse_qs(p.query).get("url", [""])[0].strip()
    return href


def iso_date(pub):
    """RSS pubDate -> ISO 8601 (UTC) the browser can read; "" if missing or odd."""
    try:
        dt = email.utils.parsedate_to_datetime(pub)
    except (TypeError, ValueError, IndexError):
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(data):
    """Read Bing News RSS. Returns the items, or None if it is not an RSS feed."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.tag != "rss":
        return None
    items, seen = [], set()
    for it in root.iter("item"):
        title = strip_html(it.findtext("title"))
        link = real_link(it.findtext("link"))
        if not title or not link.startswith("https://") or link in seen:
            continue
        domain = domain_of(link)
        if not domain or is_bing(domain):
            continue
        seen.add(link)
        snippet = strip_html(it.findtext("description"))
        source = ""
        for child in it:
            if child.tag.endswith("}Source") or child.tag == "Source":
                source = strip_html(child.text)
                break
        items.append({
            "title": title,
            "link": link,
            "source": source or domain,
            "domain": domain,
            "published": iso_date(it.findtext("pubDate")),
            "snippet": snippet[:280],
            "trusted": is_trusted(domain),
            "attention": attention_hits(f"{title} {snippet}"),
        })
    return items[:MAX_ITEMS]


def fetch_bing(topic, force=False):
    """Bing News items, cached 15 min. At most 2 Bing requests per topic per cache period:
    page 1, and page 2 only when page 1 had fewer than WANT_ITEMS stories."""
    now = time.time()
    with _cache_lock:
        cached = _cache.get(topic)
        if cached and not force and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        if now < _bing_blocked_until:
            mins = int((_bing_blocked_until - now) // 60) + 1
            raise NewsError(f"Bing News is not answering automatic requests right now. "
                            f"Try again in {mins} min.")
    items, raw = _bing_page(topic, now)
    if len(items) < WANT_ITEMS and raw:
        # Page 2 is a bonus: if it fails, keep page 1 (a refusal still starts the back-off).
        try:
            more, _ = _bing_page(topic, now, first=raw + 1)
        except NewsError:
            more = []
        seen = {it["link"] for it in items}
        for it in more:
            if it["link"] not in seen and len(items) < MAX_ITEMS:
                seen.add(it["link"])
                items.append(it)
    with _cache_lock:
        _cache[topic] = (now, items)
    return items


def _bing_page(topic, now, first=0):
    """One Bing News RSS request. Returns (items, number of <item>s Bing sent)."""
    global _bing_blocked_until
    params = {"q": topic, "format": "rss", "count": 30}
    if first:
        params["first"] = first
    url = BING_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(FEED_MAX_BYTES)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            with _cache_lock:
                _bing_blocked_until = now + BING_BLOCK_WAIT
            raise NewsError(f"Bing News refused the request ({e.code}). Try again in 15 min.")
        raise NewsError(f"Bing News answered with an error ({e.code}).")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        raise NewsError("Could not reach Bing News. Check your internet and try again.")
    items = parse_feed(data)
    if items is None:
        # Not a feed (for example a "prove you are human" page): back off, do not retry.
        with _cache_lock:
            _bing_blocked_until = now + BING_BLOCK_WAIT
        raise NewsError("Bing News sent a page instead of news (maybe a human check). "
                        "Try again in 15 min.")
    return items, data.count(b"<item>")


# ---------------------------------------------------------------------------
# RSS sites (feeds.json): one feed per site, filtered by the topic's words.
# ---------------------------------------------------------------------------
STOP_WORDS = {"news", "and", "the", "for", "with", "from", "that", "this", "new", "latest",
              "about", "are", "was", "has", "have", "you", "your", "all", "any"}
_feed_cache = {}  # url -> (time, items or None when it failed)


def load_feeds():
    try:
        with open(FEEDS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and isinstance(e.get("url"), str) and e["url"].startswith("https://"):
            out.append((str(e.get("name") or domain_of(e["url"]))[:60], e["url"]))
    return out


def topic_words(topic):
    words = re.findall(r"[a-z0-9]+", topic.lower())
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in STOP_WORDS]


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c.text or ""
    return ""


def any_date(text):
    text = (text or "").strip()
    if not text:
        return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        try:
            dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso_date(text)


def parse_site_feed(data, name):
    """Parse RSS 2.0 or Atom into item dicts. Returns None if it is neither."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    kind = _local(root.tag)
    if kind == "rss":
        entries = [e for e in root.iter() if _local(e.tag) == "item"]
    elif kind == "feed":
        entries = [e for e in root.iter() if _local(e.tag) == "entry"]
    else:
        return None
    items = []
    for e in entries:
        title = strip_html(_child_text(e, "title"))
        link = ""
        if kind == "rss":
            link = _child_text(e, "link").strip()
        else:
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = (c.get("href") or "").strip()
                    break
        if not title or not link.startswith("https://"):
            continue
        domain = domain_of(link)
        if not domain:
            continue
        snippet = strip_html(_child_text(e, "description") or _child_text(e, "summary")
                             or _child_text(e, "content"))
        pub = (_child_text(e, "pubDate") or _child_text(e, "published")
               or _child_text(e, "updated") or _child_text(e, "date"))
        # Category/tag words (RSS <category>text</category>, Atom <category term=".."/>).
        cats = " ".join((c.text or c.get("term") or "") for c in e
                        if _local(c.tag) in ("category", "subject"))
        items.append({
            "title": title,
            "link": link,
            "source": name,
            "domain": domain,
            "published": any_date(pub),
            "snippet": snippet[:280],
            "trusted": is_trusted(domain),
            "attention": attention_hits(f"{title} {snippet}"),
            "_cats": strip_html(cats)[:300],
        })
    return items


def fetch_site_feed(name, url):
    """One site's items, cached 15 min. Returns [] if the site fails (never raises)."""
    now = time.time()
    with _cache_lock:
        c = _feed_cache.get(url)
        if c and now - c[0] < CACHE_SECONDS:
            return c[1]
    items = []
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(FEED_MAX_BYTES)
        items = parse_site_feed(data, name) or []
    except Exception:
        items = []
    with _cache_lock:
        _feed_cache[url] = (now, items)
    return items


def fetch_rss(topic):
    words = topic_words(topic)
    if not words:
        return [], []
    pats = [re.compile(r"\b" + re.escape(w) + r"(?:s|es)?\b") for w in words]
    feeds = load_feeds()
    results = [[] for _ in feeds]

    def work(i, name, url):
        results[i] = fetch_site_feed(name, url)
    threads = [threading.Thread(target=work, args=(i, n, u)) for i, (n, u) in enumerate(feeds)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    # Loose match (only used to fill up to WANT_ITEMS): the topic words in the link or
    # category tags. To stay on topic it needs 2 different topic words (or the only one).
    need = min(2, len(words))
    strict, loose = [], []
    for items in results:
        for it in items:
            text = f"{it['title']} {it['snippet']}".lower()
            if any(p.search(text) for p in pats):
                strict.append(it)
                continue
            extra = re.sub(r"[^a-z0-9]+", " ", f"{it['link']} {it.get('_cats', '')}".lower())
            if sum(1 for p in pats if p.search(extra)) >= need:
                loose.append(it)
    newest = lambda i: i["published"]
    strict.sort(key=newest, reverse=True)
    loose.sort(key=newest, reverse=True)
    return strict[:MAX_ITEMS], loose


def fetch_news(topic, force=False):
    """Bing results + RSS-site results, no duplicate links, newest first."""
    bing, error = [], None
    try:
        bing = fetch_bing(topic, force)
    except NewsError as e:
        error = e
    rss, loose = fetch_rss(topic)
    merged, seen = [], set()
    for it in sorted(bing + rss, key=lambda i: i["published"], reverse=True):
        if it["link"] not in seen:
            seen.add(it["link"])
            merged.append(it)
    # Still short? Add the looser RSS matches, only up to WANT_ITEMS.
    for it in loose:
        if len(merged) >= WANT_ITEMS:
            break
        if it["link"] not in seen:
            seen.add(it["link"])
            merged.append(it)
    if error and not merged:
        raise error
    merged.sort(key=lambda i: i["published"], reverse=True)
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in merged[:MAX_ITEMS]]


# ---------------------------------------------------------------------------
# Reader view: fetch one article and return its readable text (plain text only).
# ---------------------------------------------------------------------------
ARTICLE_CACHE_SECONDS = 30 * 60
ARTICLE_CACHE_MAX = 200
ARTICLE_TIMEOUT = 15
ARTICLE_MAX_BYTES = 2 * 1024 * 1024
MAX_PARAGRAPHS = 60
MAX_ARTICLE_CHARS = 12000
SHORT_TEXT_CHARS = 300
SHORT_PARA_PENALTY = 100
USER_AGENT = "MyNews-local/1.0"
GOOGLE_HOSTS = ("google.com", "googleusercontent.com", "gstatic.com")

_article_lock = threading.Lock()
_article_cache = {}


class ArticleError(Exception):
    """An error with a plain message that is safe to show on the page."""

    def __init__(self, message, code=502):
        super().__init__(message)
        self.code = code


def is_google(host):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in GOOGLE_HOSTS)


def check_public_https(url):
    """Allow only https URLs whose host resolves to public internet addresses."""
    try:
        p = urllib.parse.urlparse(url)
        port = p.port
    except ValueError:
        raise ArticleError("That link is not valid.", 400)
    if p.scheme != "https" or not p.hostname:
        raise ArticleError("Only secure (https) links can be opened in the reader.", 400)
    if p.username or p.password or port not in (None, 443):
        raise ArticleError("That link is not allowed.", 400)
    host = p.hostname
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise ArticleError("Could not find that website.", 502)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise ArticleError("That address is private or local, so it is blocked.", 400)
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but check every new address the same way."""
    max_redirections = 6

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(SafeRedirect)


def fetch_html(url):
    """Download a page (https, public, text/html, max ~2 MB). Returns (final_url, text)."""
    check_public_https(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with _opener.open(req, timeout=ARTICLE_TIMEOUT) as r:
            final_url = r.geturl()
            ctype = r.headers.get("Content-Type", "").lower()
            if "text/html" not in ctype and "application/xhtml" not in ctype:
                raise ArticleError("That link is not a web page the reader can show.", 415)
            raw = r.read(ARTICLE_MAX_BYTES)
            charset = r.headers.get_content_charset()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 451):
            raise ArticleError("This site blocks reader apps. Use Open original.", 502)
        if e.code == 404:
            raise ArticleError("The article was not found on the site (404).", 502)
        raise ArticleError(f"The site answered with an error ({e.code}).", 502)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        if isinstance(e, ArticleError):
            raise
        raise ArticleError("Could not reach the site (timeout or no connection).", 502)
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_\-]+)', raw[:4096], re.I)
        charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return final_url, text


def resolve_google_link(url):
    """Turn a news.google.com/rss/articles/... link into the real article address.

    Returns (real_url, page_text_or_None, method). Raises ArticleError if not found.
    """
    p = urllib.parse.urlparse(url)
    article_id = p.path.rstrip("/").split("/")[-1]
    # 1. Older Google links carry the real address inside the (base64) id.
    try:
        raw = base64.urlsafe_b64decode(article_id + "=" * (-len(article_id) % 4))
        m = re.search(rb"https://[\x21-\x7e]+", raw)
        if m and not is_google(urllib.parse.urlparse(m.group(0).decode()).hostname):
            return m.group(0).decode(), None, "decoded id"
    except (ValueError, UnicodeDecodeError):
        pass
    # 2. Follow redirects.
    final_url, page = fetch_html(url)
    if not is_google(urllib.parse.urlparse(final_url).hostname):
        return final_url, page, "redirect"
    # 3. Look for the real address inside Google's page.
    patterns = [
        r'data-n-au="([^"]+)"',
        r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"',
        r'<link[^>]+href="([^"]+)"[^>]+rel="canonical"',
        r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"',
    ]
    for pat in patterns:
        for found in re.findall(pat, page, re.I):
            cand = html.unescape(found)
            host = urllib.parse.urlparse(cand).hostname
            if cand.startswith("https://") and host and not is_google(host):
                return cand, None, "found in page"
    raise ArticleError(
        "Google News hides the real address of this story, so it cannot be shown here. "
        "Use Open original.", 502)


class ReadableParser(HTMLParser):
    """Collects paragraphs of plain text and the page title, skipping menus and scripts."""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form",
            "figcaption", "figure", "svg", "iframe", "template", "button", "select",
            "textarea", "dialog", "menu"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "ul", "ol", "li", "table", "h1", "h2", "h3",
             "h4", "h5", "h6", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # (tag, element_id)
        self.next_id = 0
        self.skip_depth = 0
        self.article_depth = 0
        self.in_title = False
        self.title = ""
        self.meta = {}
        self.cur = None          # current paragraph: dict(parent, grand, in_article, parts)
        self.paras = []

    def _close_p(self):
        if self.cur is not None:
            text = re.sub(r"\s+", " ", "".join(self.cur["parts"])).strip()
            if text:
                self.cur["text"] = text
                self.paras.append(self.cur)
            self.cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:site_name", "twitter:title", "application-name"):
                self.meta.setdefault(key, (a.get("content") or "").strip())
            return
        if tag == "title" and not self.stack_has_skip():
            self.in_title = True
        if tag in self.VOID:
            return
        if self.cur is not None and tag in self.BLOCK:
            self._close_p()
        hidden = "hidden" in a or a.get("aria-hidden") == "true"
        self.next_id += 1
        self.stack.append((tag, self.next_id, tag in self.SKIP or hidden))
        if tag in self.SKIP or hidden:
            self.skip_depth += 1
        if tag == "article":
            self.article_depth += 1
        if tag == "p" and not self.skip_depth:
            self.cur = {"anc": [eid for _, eid, _ in self.stack[:-1]],
                        "in_article": self.article_depth > 0, "parts": []}

    def stack_has_skip(self):
        return self.skip_depth > 0

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.VOID:
            return
        if not any(t == tag for t, _, _ in self.stack):
            return
        while self.stack:
            t, _, skipped = self.stack.pop()
            if skipped:
                self.skip_depth -= 1
            if t == "article":
                self.article_depth -= 1
            if t == "p":
                self._close_p()
            if t == tag:
                break

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif self.cur is not None and not self.skip_depth:
            self.cur["parts"].append(data)

    def close(self):
        super().close()
        self._close_p()


def pick_paragraphs(paras):
    """Use <article> text if it has enough, else the container with the most <p> text."""
    def total(ps):
        return sum(len(p["text"]) for p in ps)

    # Pool: the <article> paragraphs if there is enough text there, else all of them.
    in_article = [p for p in paras if p["in_article"]]
    pool = in_article if total(in_article) >= SHORT_TEXT_CHARS else paras
    # Score every box that holds paragraphs: long paragraphs add points, short
    # labels ("Share", "5 days ago", teaser cards) take points away. The best box
    # is the story; this drops related-story lists, author boxes and labels.
    scores, depth = {}, {}
    for p in pool:
        n = len(p["text"])
        points = n if n >= 80 else n - SHORT_PARA_PENALTY
        for d, eid in enumerate(p["anc"]):
            scores[eid] = scores.get(eid, 0) + points
            depth[eid] = d
    chosen = []
    if scores:
        box = max(scores, key=lambda eid: (scores[eid], depth[eid]))
        chosen = [p for p in pool if box in p["anc"]]
    if total(chosen) < SHORT_TEXT_CHARS:
        longer = [p for p in pool if len(p["text"]) >= 40]
        if total(longer) > total(chosen):
            chosen = longer
    out, size, seen = [], 0, set()
    for p in chosen:
        text = p["text"]
        if text in seen:
            continue
        seen.add(text)
        if size + len(text) > MAX_ARTICLE_CHARS:
            if MAX_ARTICLE_CHARS - size < 80:
                break
            text =text[: max(0, MAX_ARTICLE_CHARS - size)].rsplit(" ", 1)[0] + " …"
        if text.strip(" …"):
            out.append(text)
        size += len(text)
        if size >= MAX_ARTICLE_CHARS or len(out) >= MAX_PARAGRAPHS:
            break
    return out


def fetch_article(url):
    now = time.time()
    with _article_lock:
        cached = _article_cache.get(url)
        if cached and now - cached[0] < ARTICLE_CACHE_SECONDS:
            return cached[1]
    check_public_https(url)
    method = "direct"
    page = None
    target = url
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "news.google.com" or is_google(host) and "/articles/" in url:
        target, page, method = resolve_google_link(url)
    final_url = target
    if page is None:
        final_url, page = fetch_html(target)
    if is_google(urllib.parse.urlparse(final_url).hostname):
        raise ArticleError("The reader landed on a Google page, not the article. Use Open original.")
    parser = ReadableParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        raise ArticleError("Could not read this page's text. Use Open original.")
    paragraphs = pick_paragraphs(parser.paras)
    title = (parser.meta.get("og:title") or parser.meta.get("twitter:title")
             or re.sub(r"\s+", " ", parser.title).strip())
    site = parser.meta.get("og:site_name") or parser.meta.get("application-name") or domain_of(final_url)
    chars = sum(len(p) for p in paragraphs)
    result = {
        "title": title[:300],
        "site": site[:100],
        "paragraphs": paragraphs,
        "url": final_url,
        "chars": chars,
        "too_short": chars < SHORT_TEXT_CHARS,
        "resolved_by": method,
    }
    with _article_lock:
        if len(_article_cache) >= ARTICLE_CACHE_MAX:
            oldest = min(_article_cache, key=lambda k: _article_cache[k][0])
            _article_cache.pop(oldest, None)
        _article_cache[url] = (now, result)
    return result

# ---------------------------------------------------------------------------
# Free key-point summary: plain code, no AI. Picks the best 3 sentences.
# ---------------------------------------------------------------------------
SUMMARY_CACHE_SECONDS = 30 * 60
SUMMARY_CACHE_MAX = 300
SUMMARY_POINTS = 3
POINT_CHARS = 160
MIN_SENTENCE = 40
BOILERPLATE = ("subscribe", "sign up", "signup", "cookie", "advertisement", "read more",
               "click here", "newsletter", "all rights reserved", "privacy policy",
               "terms of use", "follow us", "affiliate", "commission", "related:",
               "share this", "log in", "sign in", "photo:", "image:", "credit:")
_summary_lock = threading.Lock()
_summary_cache = {}


def split_sentences(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(])", text)
    return [p.strip() for p in parts if p.strip()]


def trim_point(s, limit=POINT_CHARS):
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def clean_text(s):
    """Plain text only: no tags, no control characters."""
    s = re.sub(r"<[^>]*>", " ", s or "")
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def pick_key_points(paragraphs, words, n=SUMMARY_POINTS):
    """Score sentences by topic words and position; return the top n in original order."""
    sents = []
    for para in paragraphs[:25]:
        sents.extend(split_sentences(para))
    seen, cands = set(), []
    for idx, s in enumerate(sents):
        low = s.lower()
        if len(s) < MIN_SENTENCE or len(s) > 600 or low in seen:
            continue
        if any(b in low for b in BOILERPLATE):
            continue
        if sum(c.isalpha() for c in s) < len(s) * 0.6:
            continue
        seen.add(low)
        cands.append((idx, s))
    if not cands:
        return []
    total = max(len(sents), 1)
    scored = []
    for idx, s in cands:
        low = s.lower()
        hits = sum(1 for w in words if w in low)
        position = 1.0 - idx / total              # earlier is better
        length_ok = 0.3 if 60 <= len(s) <= 260 else 0.0
        has_fact = 0.3 if re.search(r"\d", s) else 0.0
        scored.append((hits * 1.5 + position * 2.0 + length_ok + has_fact, idx, s))
    top = sorted(scored, key=lambda t: -t[0])[:n]
    top.sort(key=lambda t: t[1])
    return [trim_point(s) for _, _, s in top]


def snippet_points(snippet, title):
    sents = [s for s in split_sentences(clean_text(snippet)) if len(s) >= 15]
    if sents:
        return [trim_point(s) for s in sents[:SUMMARY_POINTS]]
    title = clean_text(title)
    return [trim_point(title)] if title else []


def make_summary(url, title="", snippet="", topic=""):
    now = time.time()
    with _summary_lock:
        c = _summary_cache.get(url)
        if c and now - c[0] < SUMMARY_CACHE_SECONDS:
            return c[1]
    check_public_https(url)          # same SSRF protection as the reader
    words = topic_words(topic) + [w for w in topic_words(title) if w not in topic_words(topic)]
    points, origin = [], "snippet"
    try:
        art = fetch_article(url)
        if not art["too_short"]:
            points = pick_key_points(art["paragraphs"], words)
            if points:
                origin = "article"
    except ArticleError:
        pass
    except Exception:
        pass
    if not points:
        points, origin = snippet_points(snippet, title), "snippet"
    result = {"points": points, "from": origin}
    with _summary_lock:
        if len(_summary_cache) >= SUMMARY_CACHE_MAX:
            oldest = min(_summary_cache, key=lambda k: _summary_cache[k][0])
            _summary_cache.pop(oldest, None)
        _summary_cache[url] = (now, result)
    return result


# ---------------------------------------------------------------------------
# Settings (settings.json), background refresh and ntfy alerts. No AI is used.
# ---------------------------------------------------------------------------
SETTINGS_FILE = os.path.join(HERE, "settings.json")
SEEN_FILE = os.path.join(HERE, "seen.json")
INTERVALS = (15, 30, 60)            # minutes between background refreshes
PER_PUSH = (1, 3, 5, 10)            # alerts per topic per refresh cycle
TOPIC_RE = re.compile(r"^gelordnews-[a-z0-9]{12}$")
NTFY_BASE = "https://ntfy.sh/"
NTFY_TIMEOUT = 10
SEEN_MAX = 500
MAX_ATTEMPTS = 10
PENDING_MAX_AGE = 24 * 3600         # drop queued alerts after 24 h
OLD_STORY = 24 * 3600               # held-back stories older than this are skipped
RETRY_EVERY = 120                   # retry failed alerts every 2 min
BACKOFF_CAP = 30 * 60
FIRST_REFRESH_DELAY = 60            # first background refresh 1 min after start
DEFAULT_SETTINGS = {"interval": 15, "per_push": 3, "notify": False}

_settings_lock = threading.Lock()
_alert_lock = threading.RLock()
_wake = threading.Event()
_bg = {"last": 0.0, "next": 0.0, "retryable": 0, "pending": 0}


def log(msg):
    """One short line to server.log (counts only, never story text)."""
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def new_ntfy_topic():
    abc = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "gelordnews-" + "".join(secrets.choice(abc) for _ in range(12))


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _clean_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    s = dict(DEFAULT_SETTINGS)
    if raw.get("interval") in INTERVALS and not isinstance(raw.get("interval"), bool):
        s["interval"] = raw["interval"]
    if raw.get("per_push") in PER_PUSH and not isinstance(raw.get("per_push"), bool):
        s["per_push"] = raw["per_push"]
    if isinstance(raw.get("notify"), bool):
        s["notify"] = raw["notify"]
    topic = raw.get("ntfy_topic")
    s["ntfy_topic"] = topic if isinstance(topic, str) and TOPIC_RE.match(topic) else ""
    s["baseline_done"] = raw.get("baseline_done") is True
    return s


def load_settings():
    """Read settings.json; make it (with a new ntfy topic) if missing or broken."""
    with _settings_lock:
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        s = _clean_settings(raw)
        if not s["ntfy_topic"] or s != raw:
            if not s["ntfy_topic"]:
                s["ntfy_topic"] = new_ntfy_topic()
            write_json_atomic(SETTINGS_FILE, s)
        return s


def update_settings(change):
    """Re-read, apply change(settings) and save, all under the lock. Returns (old, new)."""
    load_settings()
    with _settings_lock:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            old = _clean_settings(json.load(f))
        new = dict(old)
        change(new)
        new = _clean_settings(new)
        if not new["ntfy_topic"]:
            new["ntfy_topic"] = old["ntfy_topic"] or new_ntfy_topic()
        write_json_atomic(SETTINGS_FILE, new)
        return old, new


def validate_settings_post(body):
    """Check a POST /api/settings body. Returns a change dict or raises ValueError."""
    if not isinstance(body, dict) or not body:
        raise ValueError("Nothing to save.")
    allowed = {"interval", "per_push", "notify", "ntfy_topic", "regenerate"}
    if set(body) - allowed:
        raise ValueError("Unknown setting.")
    ch = {}
    if "interval" in body:
        v = body["interval"]
        if isinstance(v, bool) or v not in INTERVALS:
            raise ValueError("Refresh must be 15, 30 or 60 minutes.")
        ch["interval"] = v
    if "per_push" in body:
        v = body["per_push"]
        if isinstance(v, bool) or v not in PER_PUSH:
            raise ValueError("Alerts per push must be 1, 3, 5 or 10.")
        ch["per_push"] = v
    if "notify" in body:
        if not isinstance(body["notify"], bool):
            raise ValueError("Notifications must be on or off.")
        ch["notify"] = body["notify"]
    if "ntfy_topic" in body:
        v = body["ntfy_topic"]
        if not isinstance(v, str) or not TOPIC_RE.match(v):
            raise ValueError("The ntfy topic must look like gelordnews- plus 12 letters/digits.")
        ch["ntfy_topic"] = v
    if "regenerate" in body:
        if body["regenerate"] is not True:
            raise ValueError("Bad request")
        ch["ntfy_topic"] = new_ntfy_topic()
    return ch


def iso_utc(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_settings(s):
    return {
        "interval": s["interval"], "per_push": s["per_push"], "notify": s["notify"],
        "ntfy_topic": s["ntfy_topic"], "intervals": list(INTERVALS), "per_push_choices": list(PER_PUSH),
        "last_refresh": iso_utc(_bg["last"]), "next_refresh": iso_utc(_bg["next"]),
        "pending": _bg["pending"],
    }


# ---- seen.json: {"seen": {link: first_seen_time}, "pending": [queued alerts]} ----
def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    seen = raw.get("seen") if isinstance(raw, dict) else None
    pending = raw.get("pending") if isinstance(raw, dict) else None
    seen = {k: v for k, v in (seen or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float))} if isinstance(seen, dict) else {}
    keep = []
    for p in pending if isinstance(pending, list) else []:
        if isinstance(p, dict) and isinstance(p.get("link"), str) and p["link"].startswith("https://"):
            keep.append({
                "topic": str(p.get("topic", ""))[:100], "link": p["link"][:2000],
                "title": str(p.get("title", ""))[:300], "source": str(p.get("source", ""))[:80],
                "published": str(p.get("published", ""))[:30],
                "attempts": int(p.get("attempts", 0) or 0), "next_try": float(p.get("next_try", 0) or 0),
                "queued": float(p.get("queued", 0) or 0),
            })
    return {"seen": seen, "pending": keep}


def save_seen(state):
    seen = state["seen"]
    if len(seen) > SEEN_MAX:
        newest = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:SEEN_MAX]
        state["seen"] = seen = dict(newest)
    write_json_atomic(SEEN_FILE, state)
    _bg["pending"] = len(state["pending"])
    _bg["retryable"] = sum(1 for p in state["pending"] if p["attempts"] > 0)


def _mark_seen(state, link, when):
    state["seen"].setdefault(link, when)


def _pub_ts(p):
    try:
        return datetime.datetime.strptime(p.get("published") or "", "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def time_ago_text(ts, now):
    if not ts:
        return "time unknown"
    mins = max(0, int((now - ts) // 60))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    hrs = round(mins / 60)
    return f"{hrs} h ago" if hrs < 48 else f"{round(hrs / 24)} days ago"


def header_text(text):
    """ntfy reads UTF-8 headers in RFC 2047 form; plain ASCII is sent as is."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def alert_message(p, now):
    tail = f". Source: {clean_text(p['source'])[:60] or 'unknown'}, {time_ago_text(_pub_ts(p), now)}."
    head = clean_text(p["title"]).rstrip(" .")
    room = 300 - len(tail) - 1
    if len(head) > room:
        head = head[:room - 1].rsplit(" ", 1)[0].rstrip(" ,;:-.") + "…"
    return head + tail


def send_ntfy(topic, p, now=None):
    """POST one alert to ntfy.sh. Returns "ok", "retry", "offline" or "drop"."""
    if not TOPIC_RE.match(topic or ""):
        return "drop"
    now = now or time.time()
    headers = {
        "Title": header_text(f"{p['topic'][:80]}: needs attention"),
        "Priority": "high",
        "Tags": "warning",
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    link = p.get("link", "")
    if link.startswith("https://") and link.isascii() and not re.search(r"[\s\x00-\x1f\x7f]", link):
        headers["Click"] = link
    req = urllib.request.Request(NTFY_BASE + topic, data=alert_message(p, now).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT) as r:
            r.read(2048)
        return "ok"
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            return "retry"
        return "drop" if 400 <= e.code < 500 else "retry"
    except Exception:
        return "offline"   # no internet, DNS, timeout, refused...


_sender = send_ntfy   # tests swap this for a fake

TEST_BODY = "MyNews test: if you can read this, your phone is subscribed to this topic."
TEST_EVERY = 10                     # at most one manual test per 10 seconds
_test_lock = threading.Lock()
_test_last = [0.0]


def send_ntfy_test(topic):
    """POST one manual test message. Returns None if sent, else a short plain reason."""
    if not TOPIC_RE.match(topic or ""):
        return "The saved ntfy topic is not valid."
    headers = {
        "Title": "MyNews test",
        "Priority": "default",
        "Tags": "white_check_mark",
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(NTFY_BASE + topic, data=TEST_BODY.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT) as r:
            r.read(2048)
        return None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "ntfy is busy (too many messages). Try again in a minute."
        return f"ntfy refused the message (error {e.code})."
    except (socket.timeout, TimeoutError):
        return "ntfy did not answer in time. Check your internet and try again."
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return "ntfy did not answer in time. Check your internet and try again."
        return "Could not reach ntfy. Check your internet connection."
    except Exception:
        return "Could not reach ntfy. Check your internet connection."


_test_sender = send_ntfy_test   # tests swap this for a fake


def run_ntfy_test(now=None):
    """Send one test to the saved topic. Returns (http_code, json_obj). Writes nothing."""
    now = now or time.time()
    with _test_lock:
        if now - _test_last[0] < TEST_EVERY:
            return 429, {"ok": False, "error": "Please wait a few seconds before sending another test."}
        _test_last[0] = now
    try:
        topic = load_settings()["ntfy_topic"]
    except Exception:
        return 500, {"ok": False, "error": "Could not read settings."}
    if not isinstance(topic, str) or not TOPIC_RE.match(topic):
        return 400, {"ok": False, "error": "The saved ntfy topic is not valid."}
    err = _test_sender(topic)
    if err:
        return 502, {"ok": False, "error": err}
    return 200, {"ok": True, "topic": topic}


def _backoff(attempts):
    return min(RETRY_EVERY * 2 ** max(attempts - 1, 0), BACKOFF_CAP)


def expire_pending(state, now):
    """Drop queued alerts that are too old or failed too often. Returns the count."""
    keep, gone = [], 0
    for p in state["pending"]:
        if p["attempts"] >= MAX_ATTEMPTS or (p["queued"] and now - p["queued"] > PENDING_MAX_AGE):
            _mark_seen(state, p["link"], p["queued"] or now)
            gone += 1
        else:
            keep.append(p)
    state["pending"] = keep
    return gone


def send_pending(state, settings, now, retry_only=False, sender=None):
    """Send due alerts, at most per_push per topic. Returns counts dict."""
    sender = sender or _sender
    c = {"sent": 0, "failed": 0, "dropped": 0, "skipped_old": 0}
    topic = settings["ntfy_topic"]
    if not settings["notify"] or not topic:
        return c
    limit = settings["per_push"]
    offline = False
    by_topic = {}
    for p in state["pending"]:
        by_topic.setdefault(p["topic"], []).append(p)
    remove = set()
    for items in by_topic.values():
        due = [p for p in items if p["next_try"] <= now and (p["attempts"] > 0 or not retry_only)]
        due.sort(key=_pub_ts, reverse=True)           # newest first
        for p in due[limit:]:                          # over the limit: wait, unless old
            if _pub_ts(p) and now - _pub_ts(p) > OLD_STORY:
                _mark_seen(state, p["link"], p["queued"] or now)
                remove.add(id(p))
                c["skipped_old"] += 1
        for p in due[:limit]:
            if offline:
                break
            result = sender(topic, p)
            if result == "ok":
                _mark_seen(state, p["link"], p["queued"] or now)
                remove.add(id(p))
                c["sent"] += 1
            elif result == "drop":
                _mark_seen(state, p["link"], p["queued"] or now)
                remove.add(id(p))
                c["dropped"] += 1
            else:
                p["attempts"] += 1
                p["next_try"] = now + _backoff(p["attempts"])
                c["failed"] += 1
                offline = offline or result == "offline"
    state["pending"] = [p for p in state["pending"] if id(p) not in remove]
    return c


def process_alerts(results, settings, now=None, complete=True, sender=None):
    """After a refresh: queue new "needs attention" stories and send them.

    results = {topic: items}. complete = every topic loaded (needed for first setup).
    """
    now = now or time.time()
    with _alert_lock:
        state = load_seen()
        expired = expire_pending(state, now)
        if expired:
            log(f"alerts: {expired} dropped (too many tries or older than 24 h)")
        if not settings["notify"] or not settings["ntfy_topic"]:
            save_seen(state)
            return {"sent": 0, "queued": 0}
        attn = [(t, it) for t, items in results.items() for it in items
                if it.get("attention") and str(it.get("link", "")).startswith("https://")]
        if not settings["baseline_done"]:
            # First time on: everything already there counts as seen; send nothing.
            for _, it in attn:
                _mark_seen(state, it["link"], now)
            save_seen(state)
            if complete:
                update_settings(lambda s: s.update(baseline_done=True))
            log(f"alerts: first setup, {len(attn)} current alerts marked seen, 0 sent")
            return {"sent": 0, "queued": 0, "baseline": len(attn)}
        queued = {p["link"] for p in state["pending"]}
        new = 0
        for t, it in attn:
            if it["link"] in state["seen"] or it["link"] in queued:
                continue
            queued.add(it["link"])
            state["pending"].append({
                "topic": t[:100], "link": it["link"][:2000], "title": str(it.get("title", ""))[:300],
                "source": str(it.get("source", ""))[:80], "published": str(it.get("published", ""))[:30],
                "attempts": 0, "next_try": 0.0, "queued": now,
            })
            new += 1
        c = send_pending(state, settings, now, sender=sender)
        save_seen(state)
        if new or any(c.values()):
            log(f"alerts: {new} new, {c['sent']} sent, {c['failed']} failed, "
                f"{c['dropped']} refused, {c['skipped_old']} skipped (old), {len(state['pending'])} waiting")
        c["queued"] = new
        return c


def retry_alerts(settings, now=None, sender=None):
    now = now or time.time()
    with _alert_lock:
        state = load_seen()
        expired = expire_pending(state, now)
        c = send_pending(state, settings, now, retry_only=True, sender=sender)
        save_seen(state)
        if expired or c["sent"] or c["failed"] or c["dropped"]:
            log(f"alerts retry: {c['sent']} sent, {c['failed']} failed, {c['dropped']} refused, "
                f"{expired} dropped, {len(state['pending'])} waiting")
        return c


def refresh_all(force=True):
    topics = load_topics()
    results, stories = {}, 0
    for t in topics:
        try:
            results[t] = fetch_news(t, force)
            stories += len(results[t])
        except Exception:
            pass
    log(f"refresh: {len(results)}/{len(topics)} topics, {stories} stories")
    return results, len(results) == len(topics)


def background_loop():
    """Refresh every topic every <interval> min (read again each round) and send alerts."""
    _bg["next"] = time.time() + FIRST_REFRESH_DELAY
    last_retry = 0.0
    while True:
        try:
            s = load_settings()
            now = time.time()
            if _bg["last"]:
                _bg["next"] = _bg["last"] + s["interval"] * 60
            if now >= _bg["next"]:
                _bg["last"] = now
                _bg["next"] = now + s["interval"] * 60
                results, complete = refresh_all(force=True)
                process_alerts(results, load_settings(), complete=complete)
            elif s["notify"] and not s["baseline_done"]:
                # Just turned on: note what is there now (cached news, no extra Bing calls).
                results, complete = refresh_all(force=False)
                process_alerts(results, load_settings(), complete=complete)
            elif _bg["retryable"] and now - last_retry >= RETRY_EVERY:
                last_retry = now
                retry_alerts(s)
        except Exception as e:
            try:
                log(f"background: error ({type(e).__name__}), will try again")
            except Exception:
                pass
        try:
            wait = max(1.0, _bg["next"] - time.time())
            if _bg["retryable"]:
                wait = min(wait, RETRY_EVERY)
            _wake.wait(min(wait, 3600))
            _wake.clear()
        except Exception:
            time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    server_version = "MyNews/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the log quiet

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10000:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            with open(INDEX_FILE, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif parsed.path == "/api/topics":
            self._json(200, load_topics())
        elif parsed.path == "/api/settings":
            try:
                self._json(200, public_settings(load_settings()))
            except Exception:
                self._json(500, {"error": "Could not read settings."})
        elif parsed.path == "/api/news":
            q = urllib.parse.parse_qs(parsed.query)
            topic = q.get("topic", [""])[0].strip()
            force = q.get("refresh", ["0"])[0] == "1"
            if topic not in load_topics():
                return self._json(404, {"error": "Unknown topic"})
            try:
                self._json(200, {"topic": topic, "items": fetch_news(topic, force)})
            except NewsError as e:
                self._json(502, {"error": str(e)})
            except Exception:
                self._json(502, {"error": "Could not load news from Bing News. Try again later."})
        elif parsed.path == "/api/article":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0].strip()
            if not url or len(url) > 2000:
                return self._json(400, {"error": "No article link was given."})
            try:
                self._json(200, fetch_article(url))
            except ArticleError as e:
                self._json(e.code, {"error": str(e)})
            except Exception:
                self._json(502, {"error": "Could not read this article. Use Open original."})
        elif parsed.path == "/api/summary":
            q = urllib.parse.parse_qs(parsed.query)
            url = q.get("url", [""])[0].strip()
            if not url or len(url) > 2000:
                return self._json(400, {"error": "No article link was given."})
            try:
                self._json(200, make_summary(
                    url, q.get("title", [""])[0][:300], q.get("snippet", [""])[0][:600],
                    q.get("topic", [""])[0][:100]))
            except ArticleError as e:
                self._json(e.code, {"error": str(e)})
            except Exception:
                self._json(502, {"error": "Could not make key points for this story."})
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "Bad request"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "Bad request"})
        if parsed.path == "/api/settings":
            try:
                change = validate_settings_post(body)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                _, new = update_settings(lambda s: s.update(change))
            except Exception:
                return self._json(500, {"error": "Could not save settings."})
            if new["notify"] and not new["baseline_done"]:
                log("settings: notifications turned on (first setup pending)")
            if _bg["last"]:
                _bg["next"] = _bg["last"] + new["interval"] * 60
            _wake.set()   # let the background loop pick up the change now
            return self._json(200, public_settings(new))
        if parsed.path == "/api/ntfy/test":
            # The body is ignored: the test always goes to the topic saved in settings.
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
                return self._json(403, {"ok": False, "error": "Not allowed."})
            code, obj = run_ntfy_test()
            return self._json(code, obj)
        name =str(body.get("name", "")).strip()
        with _topics_lock:
            topics = load_topics()
            if parsed.path == "/api/topics/add":
                if not 1 <= len(name) <= 60:
                    return self._json(400, {"error": "Topic must be 1 to 60 characters."})
                if any(t.lower() == name.lower() for t in topics):
                    return self._json(400, {"error": "That topic is already on your page."})
                if len(topics) >= MAX_TOPICS:
                    return self._json(400, {"error": f"You can have up to {MAX_TOPICS} topics."})
                topics.append(name)
            elif parsed.path == "/api/topics/remove":
                if name not in topics:
                    return self._json(404, {"error": "Topic not found."})
                topics.remove(name)
                with _cache_lock:
                    _cache.pop(name, None)
            else:
                return self._json(404, {"error": "Not found"})
            save_topics(topics)
        self._json(200, topics)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MyNews running at http://localhost:{PORT} (Ctrl+C to stop)", flush=True)
    try:
        load_settings()   # makes settings.json (with a new ntfy topic) the first time
    except Exception:
        log("settings: could not create settings.json")
    threading.Thread(target=background_loop, name="mynews-refresh", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
