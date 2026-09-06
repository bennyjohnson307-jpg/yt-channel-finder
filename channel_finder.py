#!/usr/bin/env python3
"""
Daily-posting channel discovery tool.

Searches YouTube for candidate channels in given niches, then tracks
each candidate's newest Community post age over repeated daily runs
to build a real streak - proving an actual daily-posting pattern
rather than a lucky one-time snapshot. Outputs qualifying channels to
a report file and a summary notification.

NOTE: Comment-count filtering is not yet included - YouTube doesn't
expose comment counts to anonymous/script requests (confirmed via
extensive testing). This will be added once authenticated access
(session cookies) is set up.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

NICHE_KEYWORDS = [
    k.strip() for k in os.environ["NICHE_KEYWORDS"].split(",") if k.strip()
]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "candidates.json")
RESULTS_FILE = os.environ.get("RESULTS_FILE", "qualifying_channels.md")
STREAK_THRESHOLD = int(os.environ.get("STREAK_THRESHOLD", "3"))
MAX_CANDIDATES_PER_KEYWORD = int(os.environ.get("MAX_CANDIDATES_PER_KEYWORD", "10"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="ignore")


def extract_yt_initial_data(html: str) -> dict:
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
    if not m:
        m = re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not m:
        raise RuntimeError("Could not find ytInitialData in the page.")
    return json.loads(m.group(1))


def text_of(node):
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


def find_channels_in_search(data: dict, limit: int):
    """Extracts channel results from a YouTube search filtered to
    'Channel' type results."""
    results = []

    def walk(obj):
        if len(results) >= limit:
            return
        if isinstance(obj, dict):
            ch = obj.get("channelRenderer")
            if ch:
                try:
                    title = text_of(ch.get("title"))
                    handle = (
                        ch.get("navigationEndpoint", {})
                        .get("browseEndpoint", {})
                        .get("canonicalBaseUrl", "")
                    )
                    if handle.startswith("/@") and title:
                        results.append({"title": title, "handle": handle[1:]})
                except Exception:
                    pass
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return results[:limit]


def search_channels(keyword: str, limit: int):
    query = urllib.parse.quote(keyword)
    # sp=EgIQAg%3D%3D filters results to "Channel" type only
    url = f"https://www.youtube.com/results?search_query={query}&sp=EgIQAg%3D%3D"
    try:
        html = fetch_html(url)
        data = extract_yt_initial_data(html)
        return find_channels_in_search(data, limit)
    except Exception as e:
        print(f"Search failed for '{keyword}': {e}")
        return []


def get_newest_post_age(handle: str):
    """Returns the relative-time text of the channel's newest Community
    post (e.g. '2 hours ago'), or None if unavailable."""
    url = f"https://www.youtube.com/@{handle}/posts"
    try:
        html = fetch_html(url)
        data = extract_yt_initial_data(html)
    except Exception as e:
        print(f"[{handle}] fetch failed: {e}")
        return None

    try:
        tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
        for tab in tabs:
            content = tab.get("tabRenderer", {}).get("content", {})
            sections = content.get("sectionListRenderer", {}).get("contents", [])
            for section in sections:
                items = section.get("itemSectionRenderer", {}).get("contents", [])
                for item in items:
                    thread = item.get("backstagePostThreadRenderer")
                    if thread:
                        post = thread.get("post", {}).get("backstagePostRenderer")
                        if post:
                            return text_of(post.get("publishedTimeText"))
    except (KeyError, TypeError, IndexError):
        pass
    return None


def is_fresh(age_text: str) -> bool:
    """Considers a post 'fresh' (posted roughly within the last day) if
    its relative-time label is in seconds/minutes/hours, or exactly
    '1 day ago'. Anything older (2+ days, weeks, months) is not fresh."""
    if not age_text:
        return False
    age_text = age_text.lower()
    if "second" in age_text or "minute" in age_text or "hour" in age_text:
        return True
    if age_text.strip() == "1 day ago":
        return True
    return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(title: str, message: str):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "default", "Tags": "mag"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def write_results_file(state: dict):
    qualifying = {
        h: c for h, c in state.items() if c.get("streak", 0) >= STREAK_THRESHOLD
    }
    lines = [
        "# Qualifying Channels (daily community posters)\n",
        f"Threshold: {STREAK_THRESHOLD}+ consecutive days with a fresh post\n",
        "**Comment-count filtering: not yet available (pending authenticated access).**\n",
        "\n| Channel | Handle | Streak |",
        "|---|---|---|",
    ]
    for handle, c in sorted(qualifying.items(), key=lambda x: -x[1].get("streak", 0)):
        lines.append(f"| {c.get('title', '?')} | @{handle} | {c.get('streak', 0)} |")
    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    return qualifying


def main():
    state = load_state()

    for keyword in NICHE_KEYWORDS:
        found = search_channels(keyword, MAX_CANDIDATES_PER_KEYWORD)
        print(f"'{keyword}': found {len(found)} candidate channels")
        for ch in found:
            if ch["handle"] not in state:
                state[ch["handle"]] = {"title": ch["title"], "streak": 0}

    newly_qualified = []
    for handle, c in state.items():
        age_text = get_newest_post_age(handle)
        fresh = is_fresh(age_text)
        old_streak = c.get("streak", 0)
        c["streak"] = old_streak + 1 if fresh else 0
        c["last_check"] = age_text
        print(f"[{handle}] newest post: {age_text} -> fresh={fresh} streak={c['streak']}")
        if old_streak < STREAK_THRESHOLD <= c["streak"]:
            newly_qualified.append((handle, c["title"]))

    qualifying = write_results_file(state)
    save_state(state)

    if newly_qualified:
        names = ", ".join(f"@{h}" for h, t in newly_qualified)
        notify(
            "New qualifying channel(s) found",
            f"{names}\nTotal qualifying so far: {len(qualifying)}",
        )
    print(f"Total qualifying channels: {len(qualifying)}")


if __name__ == "__main__":
    main()
