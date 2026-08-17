#!/usr/bin/env python3
"""Generate the profile SVGs and refresh the auto-updated README sections.

Everything is drawn here rather than pulled from a third-party card service, so
the profile never shows a broken image when someone else's Vercel instance is
rate limited. Standard library only -- no install step in CI.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER = "hyeseonko"
BLOG_FEED = "https://hyeseonko.github.io/feed.xml"
LEETCODE_REPO = f"{USER}/LeetCode"

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"

FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

THEMES = {
    "light": {"fg": "#1f2328", "muted": "#59636e", "line": "#d1d9e0"},
    "dark": {"fg": "#e6edf3", "muted": "#9198a1", "line": "#3d444d"},
}

STACK = ["Python", "PyTorch", "LangGraph", "FastAPI", "Docker", "AWS"]

# Minimum number of entries before the activity section is worth showing at all.
ACTIVITY_MIN = 3


# --------------------------------------------------------------------------- io


def token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("no GitHub token: set GITHUB_TOKEN or log in with gh")


def api(path: str, tok: str) -> object:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{USER}-profile-build",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def graphql(query: str, tok: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "User-Agent": f"{USER}-profile-build",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if "errors" in payload:
        sys.exit(f"graphql: {payload['errors']}")
    return payload["data"]


def fetch_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{USER}-profile-build"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return None


# ------------------------------------------------------------------ collectors


def collect_totals(tok: str) -> dict:
    """All-time public contribution counts, plus stars and repo count.

    GitHub's contributions API is scoped to one year per call, so the years are
    queried as aliases in a single round trip and summed.
    """
    years = graphql(
        f'{{ user(login:"{USER}") {{ contributionsCollection {{ contributionYears }} }} }}',
        tok,
    )["user"]["contributionsCollection"]["contributionYears"]

    blocks = "\n".join(
        f'y{y}: contributionsCollection(from:"{y}-01-01T00:00:00Z", to:"{y}-12-31T23:59:59Z") '
        "{ totalCommitContributions totalPullRequestContributions "
        "totalIssueContributions totalPullRequestReviewContributions }"
        for y in years
    )
    data = graphql(
        f"""{{
          user(login:"{USER}") {{
            {blocks}
            repositories(first:100, isFork:false, privacy:PUBLIC, ownerAffiliations:OWNER) {{
              totalCount
              nodes {{
                stargazerCount
              }}
            }}
          }}
        }}""",
        tok,
    )["user"]

    totals = {"commits": 0, "prs": 0, "issues": 0, "reviews": 0}
    for y in years:
        block = data[f"y{y}"]
        totals["commits"] += block["totalCommitContributions"]
        totals["prs"] += block["totalPullRequestContributions"]
        totals["issues"] += block["totalIssueContributions"]
        totals["reviews"] += block["totalPullRequestReviewContributions"]

    repos = data["repositories"]["nodes"]
    totals["stars"] = sum(r["stargazerCount"] for r in repos)
    totals["repos"] = data["repositories"]["totalCount"]

    return totals


def collect_posts(limit: int = 4) -> list[dict]:
    raw = fetch_text(BLOG_FEED)
    if not raw:
        return []
    # ElementTree expands internal entities, so a feed carrying a DTD could blow
    # up memory. No legitimate Atom feed needs one -- refuse instead of parsing.
    if re.search(r"<!DOCTYPE", raw[:4096], re.IGNORECASE):
        print("feed declares a DTD; refusing to parse", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    posts = []
    for entry in root.findall("a:entry", ns):  # Atom (what Chirpy emits)
        title = (entry.findtext("a:title", "", ns) or "").strip()
        link_el = entry.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        stamp = (entry.findtext("a:updated", "", ns) or entry.findtext("a:published", "", ns) or "")
        posts.append({"title": title, "url": link, "date": stamp[:10]})
    if not posts:
        for item in root.iter("item"):  # RSS fallback
            posts.append({
                "title": (item.findtext("title") or "").strip(),
                "url": item.findtext("link") or "",
                "date": (item.findtext("pubDate") or "")[:16],
            })
    return posts[:limit]


def collect_leetcode(tok: str) -> dict:
    # Counting directories overstates the total: that repo carries two naming
    # generations and 40 problems have a directory in each. It computes the real
    # figure itself and publishes stats.json -- read that rather than re-deriving
    # a number that would disagree with the one on the repo's own page.
    try:
        payload = api(f"/repos/{LEETCODE_REPO}/contents/stats.json", tok)
        solved = int(json.loads(base64.b64decode(payload["content"]))["solved"])
    except (urllib.error.HTTPError, KeyError, ValueError, TypeError):
        solved = 0

    return {"solved": solved}


def collect_activity(tok: str, limit: int = 5) -> list[dict]:
    """Recent public activity, one line per repo so a busy day cannot flood it."""
    try:
        events = api(f"/users/{USER}/events/public?per_page=100", tok)
    except urllib.error.HTTPError:
        return []

    seen: dict[str, dict] = {}
    for ev in events:
        repo = ev.get("repo", {}).get("name", "")
        if not repo or repo in seen:
            continue
        kind = ev["type"]
        if kind == "PushEvent":
            n = ev["payload"].get("distinct_size") or ev["payload"].get("size") or 1
            what = f"pushed {n} commit{'s' if n != 1 else ''}"
        elif kind == "PullRequestEvent":
            what = f"{ev['payload'].get('action', 'updated')} a pull request"
        elif kind == "ReleaseEvent":
            what = f"released {ev['payload'].get('release', {}).get('tag_name', '')}".strip()
        elif kind == "CreateEvent" and ev["payload"].get("ref_type") == "repository":
            what = "created the repository"
        elif kind == "IssuesEvent":
            what = f"{ev['payload'].get('action', 'updated')} an issue"
        else:
            continue
        seen[repo] = {"repo": repo, "what": what, "date": ev["created_at"][:10]}
        if len(seen) >= limit:
            break
    return list(seen.values())


# -------------------------------------------------------------------- svg bits


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_width(text: str, size: float, weight: str = "normal") -> float:
    """Rough advance width for the system sans stack, good enough for pills."""
    factor = 0.6 if weight == "600" else 0.55
    return len(text) * size * factor


def svg(width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">\n{body}\n</svg>\n'
    )


def header_svg(theme: dict) -> str:
    w, h = 840, 132
    body = f"""  <line x1="0" y1="0.5" x2="{w}" y2="0.5" stroke="{theme['line']}" stroke-width="1"/>
  <line x1="0" y1="{h - 0.5}" x2="{w}" y2="{h - 0.5}" stroke="{theme['line']}" stroke-width="1"/>
  <text x="40" y="62" font-family="{FONT}" font-size="30" font-weight="600"
        letter-spacing="0.4" fill="{theme['fg']}">Hyeseon Ko</text>
  <text x="41" y="90" font-family="{FONT}" font-size="14"
        letter-spacing="0.3" fill="{theme['muted']}">LLM applications &amp; agents  ·  Seoul, Korea</text>"""
    return svg(w, h, body)


def stack_svg(theme: dict) -> str:
    size, pad, gap, ph = 12.5, 13, 8, 27
    x, pills = 0.5, []
    for label in STACK:
        pw = round(text_width(label, size) + pad * 2)
        pills.append(
            f'  <rect x="{x}" y="0.5" width="{pw}" height="{ph}" rx="{ph / 2}" '
            f'fill="none" stroke="{theme["line"]}" stroke-width="1"/>\n'
            f'  <text x="{x + pw / 2}" y="{ph / 2 + 4.5}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="{size}" fill="{theme["muted"]}">{esc(label)}</text>'
        )
        x += pw + gap
    return svg(round(x + 0.5), ph + 1, "\n".join(pills))


def strip_svg(theme: dict, tiles: list[tuple[str, str]]) -> str:
    """A horizontal band of big-number tiles, sized to sit under the header.

    This replaced a "most used languages" card: with a handful of public repos
    the byte-share of one 2018 notebook outweighed 446 Python solutions, so the
    figure measured file format rather than anything about me.
    """
    w, h = 840, 104
    cell = w / len(tiles)
    body = [
        f'  <line x1="0" y1="{h - 0.5}" x2="{w}" y2="{h - 0.5}" '
        f'stroke="{theme["line"]}" stroke-width="1"/>'
    ]
    for i, (value, label) in enumerate(tiles):
        cx = cell * i + cell / 2
        if i:
            x = round(cell * i, 1)
            body.append(
                f'  <line x1="{x}" y1="26" x2="{x}" y2="{h - 26}" '
                f'stroke="{theme["line"]}" stroke-width="1"/>'
            )
        body.append(
            f'  <text x="{cx:.1f}" y="52" text-anchor="middle" font-family="{MONO}" '
            f'font-size="26" font-weight="600" fill="{theme["fg"]}">{esc(value)}</text>'
        )
        body.append(
            f'  <text x="{cx:.1f}" y="74" text-anchor="middle" font-family="{FONT}" '
            f'font-size="11" letter-spacing="0.8" fill="{theme["muted"]}">{esc(label.upper())}</text>'
        )
    return svg(w, h, "\n".join(body))


def human(n: int) -> str:
    return f"{n:,}"


# ------------------------------------------------------------------ readme bits


def replace_block(text: str, name: str, content: str) -> str:
    pattern = re.compile(
        rf"(<!-- {name}:START -->)(.*?)(<!-- {name}:END -->)", re.DOTALL
    )
    if not pattern.search(text):
        sys.exit(f"README is missing the {name} markers")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text)


def main() -> None:
    tok = token()
    totals = collect_totals(tok)
    posts = collect_posts()
    lc = collect_leetcode(tok)
    activity = collect_activity(tok)

    ASSETS.mkdir(exist_ok=True)
    # A tile reading "0" advertises an absence, so those are dropped rather than shown.
    tiles = [
        (human(value), label)
        for value, label in (
            (totals["commits"], "commits · all time"),
            (lc["solved"], "problems solved"),
            (totals["stars"], "stars earned"),
            (totals["repos"], "public repos"),
        )
        if value
    ]
    for name, theme in THEMES.items():
        (ASSETS / f"header-{name}.svg").write_text(header_svg(theme))
        (ASSETS / f"stack-{name}.svg").write_text(stack_svg(theme))
        (ASSETS / f"stats-{name}.svg").write_text(strip_svg(theme, tiles))

    text = README.read_text()

    if posts:
        blog = "\n".join(f"- [{p['title']}]({p['url']})  <sub>{p['date']}</sub>" for p in posts)
        blog += "\n\n<sub>More at [hyeseonko.github.io](https://hyeseonko.github.io)</sub>"
    else:
        blog = "<sub>Feed unreachable at build time.</sub>"
    text = replace_block(text, "BLOG", blog)

    text = replace_block(text, "LEETCODE", "\n".join([
        f"**{human(lc['solved'])}** problems solved",
        "",
        f"<sub>Solutions live in [{LEETCODE_REPO}](https://github.com/{LEETCODE_REPO}).</sub>",
    ]))

    # A thin activity list reads as neglect, so below the threshold it is dropped
    # entirely rather than shown half empty.
    if len(activity) >= ACTIVITY_MIN:
        rows = "\n".join(
            f"- [{a['repo']}](https://github.com/{a['repo']}) — {a['what']}  <sub>{a['date']}</sub>"
            for a in activity
        )
        block = f"## Recent activity\n\n{rows}"
    else:
        block = ""
    text = replace_block(text, "ACTIVITY", block)

    README.write_text(text)
    print(
        f"commits={totals['commits']} prs={totals['prs']} stars={totals['stars']} "
        f"repos={totals['repos']} "
        f"posts={len(posts)} leetcode={lc['solved']} activity={len(activity)}"
    )


if __name__ == "__main__":
    main()
