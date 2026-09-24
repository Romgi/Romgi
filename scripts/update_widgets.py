#!/usr/bin/env python3
"""Render self-contained profile widgets from GitHub's publicly visible data.

Usage: python scripts/update_widgets.py --user Romgi
Offline: --data-dir fixtures --as-of YYYY-MM-DD
Fixtures: repos.json (REST repository list), languages.json (full_name -> bytes),
calendar.html (the unauthenticated /users/USER/contributions response).

The calendar request deliberately never sends credentials: a personal token can
see private activity that visitors cannot. REST uses GITHUB_TOKEN, when present,
only to raise the rate limit; repositories must pass explicit public filters.
All fetching, validation, and rendering finish before any existing SVG changes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


BG = "#0d1117"
BORDER = "#263241"
CYAN = "#67e8f9"
WHITE = "#f0f6fc"
MUTED = "#9baac1"
MINT = "#a7f3d0"
HEAT = ("#172330", "#164e63", "#0e7490", "#22b8cf", CYAN)
LANGUAGE_COLORS = (CYAN, MINT, "#a5b4fc", "#fbbf77", "#f0abfc", "#64748b")


class DataError(RuntimeError):
    """A missing, malformed, or incomplete source must not replace valid assets."""


def request(url: str, *, token: str = "", json_response: bool = True):
    headers = {"User-Agent": "Romgi-public-profile-widgets", "Accept-Language": "en-US"}
    if json_response:
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                body = response.read().decode("utf-8")
            return json.loads(body) if json_response else body
        except HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DataError(f"GitHub returned HTTP {error.code} for {url}") from None
        except (URLError, TimeoutError) as error:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DataError(f"Could not reach GitHub at {url}") from error
        except (ValueError, UnicodeError) as error:
            raise DataError(f"GitHub returned invalid data for {url}") from error
    raise DataError("GitHub request exhausted its retries")


class PublicCalendarParser(HTMLParser):
    """Read dated cells and exact counts from GitHub's public calendar tooltips."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells: dict[str, tuple[str, int]] = {}
        self.tooltips: dict[str, str] = {}
        self.current_id: str | None = None
        self.current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "td" and "data-date" in attributes:
            cell_id = attributes.get("id")
            if not cell_id or cell_id in self.cells:
                raise DataError("Calendar has a missing or repeated cell ID")
            try:
                level = int(attributes["data-level"])
            except (KeyError, ValueError) as error:
                raise DataError("Calendar has an invalid intensity level") from error
            if level not in range(5):
                raise DataError("Calendar intensity level is out of range")
            self.cells[cell_id] = (attributes["data-date"], level)
        if tag == "tool-tip":
            self.current_id = attributes.get("for")
            self.current_text = []

    def handle_data(self, data):
        if self.current_id:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.current_id:
            self.tooltips[self.current_id] = " ".join("".join(self.current_text).split())
            self.current_id = None


def parse_calendar(document: str, as_of: date) -> list[dict]:
    parser = PublicCalendarParser()
    parser.feed(document)
    parser.close()
    start = as_of - timedelta(days=364)
    days = {}
    for cell_id, (raw_date, level) in parser.cells.items():
        try:
            day = date.fromisoformat(raw_date)
        except ValueError as error:
            raise DataError("Calendar contains an invalid date") from error
        if not start <= day <= as_of:
            continue
        text = parser.tooltips.get(cell_id, "")
        match = re.fullmatch(r"(No|[\d,]+) contributions? on .+\.", text)
        if not match:
            raise DataError(f"Missing or unrecognized public contribution count for {day}")
        count = 0 if match[1] == "No" else int(match[1].replace(",", ""))
        if (count == 0) != (level == 0):
            raise DataError(f"Inconsistent public contribution data for {day}")
        if day in days:
            raise DataError(f"Repeated date in the public calendar: {day}")
        days[day] = {"date": day, "count": count, "level": level}
    expected = {start + timedelta(days=offset) for offset in range(365)}
    if set(days) != expected:
        raise DataError(f"Expected 365 public calendar days; received {len(days)}")
    return [days[day] for day in sorted(days)]


def eligible_repositories(repositories: list, user: str) -> list[dict]:
    if not isinstance(repositories, list):
        raise DataError("Expected a repository list from GitHub")
    selected = []
    for repo in repositories:
        if not isinstance(repo, dict):
            raise DataError("Repository data is malformed")
        if (repo.get("private") is False and repo.get("fork") is False
                and repo.get("archived") is False
                and repo.get("owner", {}).get("login", "").casefold() == user.casefold()
                and repo.get("name", "").casefold() != user.casefold()):
            if not isinstance(repo.get("full_name"), str) or not repo["full_name"]:
                raise DataError("Eligible repository is missing its name")
            selected.append(repo)
    return sorted(selected, key=lambda repo: repo["full_name"].casefold())


def language_totals(repositories: list[dict], languages: dict) -> Counter:
    totals = Counter()
    for repo in repositories:
        name = repo["full_name"]
        if name not in languages or not isinstance(languages[name], dict):
            raise DataError(f"Missing language data for {name}")
        for language, count in languages[name].items():
            if not isinstance(language, str) or not language or type(count) is not int or count < 0:
                raise DataError(f"Invalid language byte count for {name}")
            if count:
                totals[language] += count
    return totals


def load_data(user: str, data_dir: Path | None, as_of: date):
    if data_dir:
        repositories = json.loads((data_dir / "repos.json").read_text(encoding="utf-8"))
        languages = json.loads((data_dir / "languages.json").read_text(encoding="utf-8"))
        calendar = (data_dir / "calendar.html").read_text(encoding="utf-8")
        repositories = eligible_repositories(repositories, user)
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        repositories = []
        for page in range(1, 1001):
            batch = request(f"https://api.github.com/users/{quote(user)}/repos?type=owner&per_page=100&page={page}", token=token)
            if not isinstance(batch, list):
                raise DataError("GitHub did not return a repository list")
            repositories.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise DataError("Repository pagination limit reached")
        repositories = eligible_repositories(repositories, user)

        def fetch_language(repo):
            name = repo["full_name"]
            return name, request(f"https://api.github.com/repos/{quote(name, safe='/')}/languages", token=token)

        with ThreadPoolExecutor(max_workers=4) as pool:
            languages = dict(pool.map(fetch_language, repositories))
        # No token, cookies, or authenticated browser session reaches this request.
        calendar = request(f"https://github.com/users/{quote(user)}/contributions", json_response=False)
    return parse_calendar(calendar, as_of), language_totals(repositories, languages), len(repositories)


def svg_start(height: int, title: str, description: str, width: int = 960) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="desc">{escape(description)}</desc>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}</style>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="{BG}" stroke="{BORDER}"/>',
    ]


def text(x, y, value, *, size=13, color=MUTED, weight=400, extra="") -> str:
    return f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}" {extra}>{escape(str(value))}</text>'


def contribution_summary(days: list[dict]) -> tuple[int, int, int]:
    total = sum(day["count"] for day in days)
    active = sum(day["count"] > 0 for day in days)
    best = run = 0
    for day in days:
        run = run + 1 if day["count"] else 0
        best = max(best, run)
    return total, active, best


def activity_svg(user: str, days: list[dict], as_of: date) -> str:
    total, active, best = contribution_summary(days)
    title = f"{user}'s publicly visible GitHub contribution activity"
    desc = (f"{total:,} contributions across {active} active days in the 365 days ending {as_of}. "
            f"Longest streak in this period: {best} days. Counts match the unauthenticated public GitHub calendar; "
            "they may include anonymized private activity if the account publicly shares it. Each square is one day.")
    svg = svg_start(322, title, desc)
    svg += [text(32, 36, "Contribution activity", size=19, color=WHITE, weight=600),
            text(928, 35, "LAST 365 DAYS", size=11, color=CYAN, extra='text-anchor="end" class="mono" letter-spacing="1.4"'),
            f'<path d="M32 56H928" stroke="{BORDER}"/>']
    for x, value, label in ((32, f"{total:,}", "CONTRIBUTIONS"), (336, active, "ACTIVE DAYS"), (640, f"{best} days", "LONGEST STREAK")):
        svg += [text(x, 100, value, size=32, color=WHITE, weight=650),
                text(x, 123, label, size=10, extra='class="mono" letter-spacing="1.1"')]
    first = days[0]["date"]
    first_sunday = first - timedelta(days=(first.weekday() + 1) % 7)
    for day in days:
        offset = (day["date"] - first_sunday).days
        column, row = divmod(offset, 7)
        x, y = 64 + column * 16, 162 + row * 16
        month = day["date"].strftime("%b")
        # Skip a short initial partial month so labels cannot overlap.
        if (day["date"].day == 1 or (day["date"] == first and first.day <= 7)) and column < 50:
            svg.append(text(x, 149, month, size=10))
        count = day["count"]
        svg.append(f'<rect x="{x}" y="{y}" width="12" height="12" rx="2.5" fill="{HEAT[day["level"]]}"><title>{day["date"]}: {count} contribution{"s" if count != 1 else ""}</title></rect>')
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        svg.append(text(32, 171 + row * 16, label, size=10))
    svg += [text(32, 300, f"Public profile calendar · updated {as_of.isoformat()}", size=11),
            text(780, 300, "Less", size=10)]
    for index, color in enumerate(HEAT):
        svg.append(f'<rect x="{808 + index * 16}" y="290" width="11" height="11" rx="2" fill="{color}"/>')
    svg.append(text(902, 300, "More", size=10))
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def languages_svg(user: str, totals: Counter, repository_count: int, as_of: date) -> str:
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    entries = ranked if len(ranked) <= 6 else ranked[:5] + [("Other", sum(count for _, count in ranked[5:]))]
    desc = (f"Language distribution of {total:,} code bytes across {repository_count} public, owned, non-fork, "
            f"non-archived repositories, excluding {user}/{user}. This measures repository code bytes, not proficiency. "
            + "; ".join(f"{name}: {count:,} bytes" for name, count in ranked))
    svg = svg_start(238, f"{user}'s public repository languages", desc)
    svg += [text(32, 36, "Languages in the code", size=19, color=WHITE, weight=600),
            text(928, 35, "PUBLIC REPOSITORIES", size=11, color=CYAN, extra='text-anchor="end" class="mono" letter-spacing="1.4"'),
            text(32, 63, "Actual code bytes across public repositories", size=12),
            f'<defs><clipPath id="bar"><rect x="32" y="86" width="896" height="14" rx="7"/></clipPath></defs>',
            f'<rect x="32" y="86" width="896" height="14" rx="7" fill="{BORDER}"/>']
    position = 32.0
    for index, (language, count) in enumerate(entries):
        width = 896 * count / total
        color = LANGUAGE_COLORS[index]
        svg.append(f'<rect x="{position:.3f}" y="86" width="{width:.3f}" height="14" fill="{color}" clip-path="url(#bar)"/>')
        position += width
        x = 32 + (index % 3) * 304
        y = 132 + (index // 3) * 38
        share = count / total * 100
        percentage = "<0.1%" if share < 0.1 else f"{share:.1f}%"
        svg += [f'<circle cx="{x + 5}" cy="{y - 4}" r="4" fill="{color}"/>',
                text(x + 19, y, language, size=13, color=WHITE, weight=500),
                text(x + 268, y, percentage, size=12, extra='text-anchor="end" class="mono"')]
    if not entries:
        svg.append(text(32, 138, "GitHub reports no code bytes for the eligible public repositories.", size=13))
    svg += [f'<path d="M32 196H928" stroke="{BORDER}"/>',
            text(32, 218, f"{repository_count} owned repos · forks, archives & profile repo excluded · bytes ≠ proficiency", size=11),
            text(928, 218, as_of.isoformat(), size=11, extra='text-anchor="end" class="mono"'),
            "</svg>"]
    return "\n".join(svg) + "\n"


def activity_mobile_svg(user: str, days: list[dict], as_of: date) -> str:
    total, active, best = contribution_summary(days)
    months = Counter()
    for day in days:
        months[day["date"].replace(day=1)] += day["count"]
    monthly = sorted(months.items())
    peak = max(months.values(), default=0) or 1
    desc = (f"{user}: {total:,} publicly visible contributions across {active} active days in the 365 days ending {as_of}. "
            f"Longest streak: {best} days. Bars group this same period by calendar month, with partial first and last months. "
            + "; ".join(f"{month:%B %Y}: {count}" for month, count in monthly))
    svg = svg_start(414, "Public contribution activity by month", desc, width=480)
    svg += [text(24, 35, "Contribution activity", size=22, color=WHITE, weight=600),
            text(24, 63, f"Last 365 days · {as_of.isoformat()}", size=16),
            f'<path d="M24 80H456" stroke="{BORDER}"/>']
    for x, value, label in ((24, f"{total:,}", "Contributions"), (180, active, "Active days"), (336, f"{best}d", "Best streak")):
        svg += [text(x, 119, value, size=30, color=WHITE, weight=650),
                text(x, 146, label, size=16)]
    svg += [text(24, 190, "Monthly contribution totals", size=17, color=WHITE, weight=500),
            f'<path d="M24 342H456" stroke="{BORDER}"/>']
    step = 432 / len(monthly)
    for index, (month, count) in enumerate(monthly):
        center = 24 + (index + 0.5) * step
        height = max(2, 126 * count / peak)
        color = MINT if index == len(monthly) - 1 else CYAN
        svg.append(f'<rect x="{center - 11:.2f}" y="{342 - height:.2f}" width="22" height="{height:.2f}" rx="4" fill="{color if count else BORDER}"><title>{month:%B %Y}: {count} contributions in this period</title></rect>')
        if index % 2 == 0:
            svg.append(text(f"{center:.2f}", 368, f"{month:%b}", size=16, extra='text-anchor="middle"'))
    svg += [text(24, 398, "First and last months cover part of a month.", size=16), "</svg>"]
    return "\n".join(svg) + "\n"


def languages_mobile_svg(user: str, totals: Counter, repository_count: int, as_of: date) -> str:
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    entries = ranked if len(ranked) <= 6 else ranked[:5] + [("Other", sum(count for _, count in ranked[5:]))]
    desc = (f"Language distribution across {repository_count} public owned repositories, excluding forks, archives, and the profile repo. "
            "This measures code bytes, not proficiency. "
            + "; ".join(f"{name}: {count:,} bytes" for name, count in ranked))
    svg = svg_start(482, f"{user}'s public repository languages", desc, width=480)
    svg += [text(24, 35, "Languages in the code", size=22, color=WHITE, weight=600),
            text(24, 63, "Share of public repository code bytes", size=16)]
    for index, (language, count) in enumerate(entries):
        y = 100 + index * 50
        share = 100 * count / total
        percentage = "<0.1%" if share < 0.1 else f"{share:.1f}%"
        svg += [text(24, y, language, size=17, color=WHITE, weight=500),
                text(456, y, percentage, size=16, extra='text-anchor="end" class="mono"'),
                f'<rect x="24" y="{y + 11}" width="432" height="9" rx="4.5" fill="{BORDER}"/>',
                f'<rect x="24" y="{y + 11}" width="{432 * count / total:.3f}" height="9" rx="4.5" fill="{LANGUAGE_COLORS[index]}"/>']
    if not entries:
        svg.append(text(24, 108, "GitHub reports no eligible code bytes.", size=16))
    svg += [f'<path d="M24 389H456" stroke="{BORDER}"/>',
            text(24, 414, f"{repository_count} owned public repos · {as_of.isoformat()}", size=16),
            text(24, 440, "Code bytes, not proficiency", size=16),
            text(24, 466, "Excludes forks, archives & profile repo", size=16), "</svg>"]
    return "\n".join(svg) + "\n"


def data_markdown(user: str, days: list[dict], totals: Counter, repository_count: int, as_of: date) -> str:
    total, active, best = contribution_summary(days)
    code_bytes = sum(totals.values())
    lines = ["# Public profile data", "", f"Updated {as_of.isoformat()} (UTC).", "", "## Contribution activity", "",
             f"The calendar covers exactly 365 days: **{days[0]['date']} through {as_of}**, inclusive.", "",
             "| Metric | Value |", "| --- | ---: |", f"| Contributions | {total:,} |",
             f"| Active days | {active} |", f"| Longest consecutive streak within this period | {best} days |", "",
             f"Source: [GitHub's public contribution calendar](https://github.com/users/{quote(user)}/contributions), fetched without authentication. "
             "These are the counts visible to any visitor. They can include anonymized private activity if the account publicly shares it. "
             "GitHub's native calendar sometimes includes additional days to complete its first week; this widget uses exactly 365 days.", "",
             "## Languages", "", f"**{code_bytes:,} code bytes** across **{repository_count} public, owned repositories**. "
             "Forks, archived repositories, and this profile repository are excluded. "
             "These percentages describe repository code bytes, not proficiency or time spent.", "",
             "| Language | Code bytes | Share |", "| --- | ---: | ---: |"]
    for language, count in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        name = escape(language).replace("|", "&#124;").replace("\n", " ")
        lines.append(f"| {name} | {count:,} | {100 * count / code_bytes:.2f}% |")
    if not totals:
        lines.append("| No eligible language data | 0 | — |")
    lines += ["", f"Source: [GitHub's public repository API](https://api.github.com/users/{quote(user)}/repos?type=owner&per_page=100) "
              "and each eligible repository's `/languages` endpoint. The graphical widget groups smaller languages as Other when needed; this table includes every language.", "",
              "<details>", "<summary>Daily contribution counts (365 days)</summary>", "", "| Date | Contributions |", "| --- | ---: |"]
    lines += [f"| {day['date']} | {day['count']} |" for day in days]
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def write_assets(output: Path, assets: dict[str, str]):
    # Parse every document before touching the output. Stage in the same directory
    # so each replacement is atomic on both Windows and the Actions Linux runner.
    for name, document in assets.items():
        if name.endswith(".svg"):
            ET.fromstring(document)
    output.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for name, document in assets.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", prefix=".widget-", suffix=".tmp", dir=output, delete=False) as file:
                file.write(document)
                staged.append((Path(file.name), output / name))
        for source, destination in staged:
            os.replace(source, destination)
    finally:
        for source, _ in staged:
            source.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="Romgi")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "assets")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", args.user):
        parser.error("Invalid GitHub username")
    if args.as_of != datetime.now(timezone.utc).date() and not args.data_dir:
        parser.error("--as-of is only supported with --data-dir")
    try:
        days, totals, count = load_data(args.user, args.data_dir, args.as_of)
        assets = {"activity.svg": activity_svg(args.user, days, args.as_of),
                  "languages.svg": languages_svg(args.user, totals, count, args.as_of),
                  "activity-mobile.svg": activity_mobile_svg(args.user, days, args.as_of),
                  "languages-mobile.svg": languages_mobile_svg(args.user, totals, count, args.as_of),
                  "data.md": data_markdown(args.user, days, totals, count, args.as_of)}
        write_assets(args.output, assets)
    except (DataError, OSError, ValueError, ET.ParseError) as error:
        print(f"Widget refresh failed: {error}", file=sys.stderr)
        return 1
    print(f"Updated four SVG widgets and data.md from public GitHub data ({len(days)} days, {count} repositories).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
