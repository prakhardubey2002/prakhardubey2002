#!/usr/bin/env python3
"""Generate the profile README, JSON snapshot, and animated SVG telemetry card."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "profile.json"
TEMPLATE_PATH = ROOT / "templates" / "profile.svg"
SVG_PATH = ROOT / "assets" / "profile.svg"
README_PATH = ROOT / "README.md"

GITHUB_USER = os.getenv("GITHUB_USERNAME", "prakhardubey2002").strip()
NPM_USER = os.getenv("NPM_USERNAME", "prakhar_dubey").strip()
USER_AGENT = "dynamic-profile-svg/1.0 (+https://github.com/prakhardubey2002/prakhardubey2002)"
CACHE_PATH = ROOT / "data" / "profile.json"
README_START = "<!-- PROFILE_CARD:START -->"
README_END = "<!-- PROFILE_CARD:END -->"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def fetch_json(url: str, headers: dict[str, str] | None = None, attempts: int = 3) -> dict[str, Any]:
    """Fetch JSON with bounded exponential backoff for transient API failures."""
    request_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        request_headers.update(headers)

    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {
                408,
                429,
                500,
                502,
                503,
                504,
            }
            if attempt == attempts - 1 or not retryable:
                raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
            delay = 2**attempt
            retry_after = getattr(exc, "headers", {}).get("Retry-After") if isinstance(exc, urllib.error.HTTPError) else None
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(int(retry_after), 20))
            time.sleep(delay)

    raise AssertionError("unreachable")


def github_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return {}
    return {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_github_data() -> dict[str, Any]:
    profile_url = f"https://api.github.com/users/{urllib.parse.quote(GITHUB_USER)}"
    profile = fetch_json(profile_url, github_headers())

    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "type": "owner",
                "sort": "full_name",
                "direction": "asc",
                "per_page": 100,
                "page": page,
            }
        )
        batch = fetch_json(f"https://api.github.com/users/{urllib.parse.quote(GITHUB_USER)}/repos?{query}", github_headers())
        if not isinstance(batch, list):
            raise RuntimeError("GitHub repositories response was not a list")
        repos.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
        page += 1

    language_counts = Counter(
        repo["language"]
        for repo in repos
        if not repo.get("fork") and isinstance(repo.get("language"), str)
    )
    top_language = language_counts.most_common(1)[0][0] if language_counts else "—"
    stars = sum(int(repo.get("stargazers_count", 0)) for repo in repos)

    return {
        "profile": {
            "name": profile.get("name") or GITHUB_USER,
            "bio": (profile.get("bio") or "Software engineer building useful things for the web.").strip(),
            "company": (profile.get("company") or "Independent").strip().lstrip("@ "),
            "location": profile.get("location") or "Earth",
            "blog": profile.get("blog") or "",
            "avatar_url": profile.get("avatar_url") or "",
        },
        "repos": int(profile.get("public_repos", len(repos))),
        "stars": stars,
        "followers": int(profile.get("followers", 0)),
        "following": int(profile.get("following", 0)),
        "top_language": top_language,
    }


def annual_downloads(package_name: str) -> int:
    encoded_name = urllib.parse.quote(package_name, safe="")
    result = fetch_json(f"https://api.npmjs.org/downloads/point/last-year/{encoded_name}")
    return int(result.get("downloads", 0))


def fetch_npm_data() -> dict[str, Any]:
    query = urllib.parse.urlencode({"text": f"maintainer:{NPM_USER}", "size": 250})
    search = fetch_json(f"https://registry.npmjs.org/-/v1/search?{query}")

    packages: list[dict[str, Any]] = []
    for item in search.get("objects", []):
        package = item.get("package") or {}
        publisher = (package.get("publisher") or {}).get("username")
        maintainers = {
            maintainer.get("username")
            for maintainer in package.get("maintainers", [])
            if isinstance(maintainer, dict)
        }
        if publisher != NPM_USER and NPM_USER not in maintainers:
            continue

        package_name = package.get("name")
        if not isinstance(package_name, str):
            continue
        packages.append(
            {
                "name": package_name,
                "version": package.get("version") or "—",
                "description": package.get("description") or "",
                "monthly_downloads": int((item.get("downloads") or {}).get("monthly", 0)),
                "weekly_downloads": int((item.get("downloads") or {}).get("weekly", 0)),
                "url": (package.get("links") or {}).get("npm") or f"https://www.npmjs.com/package/{package_name}",
            }
        )

    packages.sort(key=lambda item: (-item["monthly_downloads"], item["name"].lower()))
    unique_packages = {item["name"]: item for item in packages}
    packages = list(unique_packages.values())

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(packages)))) as executor:
        yearly_by_name = dict(zip((item["name"] for item in packages), executor.map(annual_downloads, (item["name"] for item in packages))))

    for package in packages:
        package["yearly_downloads"] = yearly_by_name[package["name"]]

    return {
        "username": NPM_USER,
        "profile_url": f"https://www.npmjs.com/~{NPM_USER}",
        "packages": packages,
        "package_count": len(packages),
        "monthly_downloads": sum(item["monthly_downloads"] for item in packages),
        "weekly_downloads": sum(item["weekly_downloads"] for item in packages),
        "yearly_downloads": sum(item["yearly_downloads"] for item in packages),
        "top_package": packages[0] if packages else None,
    }


def load_cache() -> dict[str, Any]:
    try:
        value = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def fetch_or_reuse(
    label: str,
    fetcher: Callable[[], dict[str, Any]],
    cache: dict[str, Any],
) -> dict[str, Any]:
    try:
        return fetcher()
    except Exception as exc:  # Preserve the last good card when a public API is temporarily unavailable.
        previous = cache.get(label)
        if not isinstance(previous, dict):
            raise
        print(f"Warning: {label} refresh failed; reusing last good data. {exc}", file=sys.stderr)
        return previous


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def compact_text(value: str, max_chars: int) -> str:
    value = " ".join(value.split())
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def initials(name: str) -> str:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", name) if word]
    return "".join(word[0] for word in words[:2]).upper() or "PD"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def render_package_pills(packages: list[dict[str, Any]]) -> str:
    visible = packages[:8]
    if not visible:
        return (
            '<g transform="translate(56 544)">'
            '<rect width="260" height="34" rx="8" fill="#081426" stroke="#1f4964"/>'
            '<text x="14" y="22" class="pkg-name">NO PACKAGES DETECTED</text>'
            "</g>"
        )

    pills: list[str] = []
    for index, package in enumerate(visible):
        column = index % 4
        row = index // 4
        x = 56 + column * 276
        y = 544 + row * 40
        name = compact_text(package["name"], 25)
        version = compact_text(package["version"], 12)
        title = compact_text(f"{package['name']} {package['version']}: {package['description']}", 100)
        pills.append(
            f'<g transform="translate({x} {y})">'
            f"<title>{html.escape(title)}</title>"
            '<rect width="260" height="34" rx="8" fill="#081426" stroke="#1c4560"/>'
            '<rect width="3" height="34" rx="1.5" fill="#39f6ff"/>'
            f'<text x="13" y="22" class="pkg-name">{html.escape(name)}</text>'
            f'<text x="247" y="22" text-anchor="end" class="pkg-version">{html.escape(version)}</text>'
            "</g>"
        )
    return "".join(pills)


def render_svg(data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    profile = data["github"]["profile"]
    npm = data["npm"]
    packages = npm.get("packages", [])
    top_package = npm.get("top_package") or {}
    generated_at = data["generated_at"]
    generated_dt = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))

    values = {
        "TITLE": f"Dynamic profile telemetry for {profile['name']}",
        "DESCRIPTION": (
            f"{profile['name']} — {profile['bio']} "
            f"GitHub: {format_number(data['github']['repos'])} public repositories, "
            f"{format_number(data['github']['stars'])} stars and {format_number(data['github']['followers'])} followers. "
            f"npm: {format_number(npm['package_count'])} packages, "
            f"{format_number(npm['yearly_downloads'])} downloads in the last year."
        ),
        "INITIALS": initials(profile["name"]),
        "PROFILE_ID": f"GIT://{GITHUB_USER.upper()}",
        "NAME": html.escape(profile["name"].upper()),
        "BIO": html.escape(compact_text(profile["bio"], 66)),
        "META": html.escape(f"{profile['company']} // {profile['location']}"),
        "REPOS": format_number(data["github"]["repos"]),
        "STARS": format_number(data["github"]["stars"]),
        "FOLLOWERS": format_number(data["github"]["followers"]),
        "TOP_LANGUAGE": html.escape(data["github"]["top_language"]),
        "PACKAGE_COUNT": format_number(npm["package_count"]),
        "YEARLY_DOWNLOADS": format_number(npm["yearly_downloads"]),
        "MONTHLY_DOWNLOADS": format_number(npm["monthly_downloads"]),
        "WEEKLY_DOWNLOADS": format_number(npm["weekly_downloads"]),
        "TOP_PACKAGE": html.escape(compact_text(top_package.get("name", "—"), 22)),
        "TOP_PACKAGE_NOTE": html.escape(
            f"{format_number(top_package.get('monthly_downloads', 0))}/MO · {compact_text(top_package.get('version', '—'), 12)}"
        ),
        "PACKAGE_PROFILE": html.escape(NPM_USER),
        "GENERATED_DISPLAY": html.escape(generated_dt.strftime("%d %b %Y · %H:%M UTC").upper()),
        "GENERATED_ISO": html.escape(generated_at),
        "PACKAGE_PILLS": render_package_pills(packages),
    }

    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    leftovers = sorted(set(re.findall(r"{{([A-Z0-9_]+)}}", rendered)))
    if leftovers:
        raise RuntimeError(f"Unresolved SVG template values: {', '.join(leftovers)}")
    return rendered.rstrip() + "\n"


def render_readme(data: dict[str, Any]) -> str:
    profile = data["github"]["profile"]
    npm = data["npm"]
    generated_at = data["generated_at"]
    cache_key = re.sub(r"[^0-9A-Za-z]", "", generated_at)
    raw_svg = (
        f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_USER}/main/assets/profile.svg"
        f"?v={cache_key}"
    )
    npm_url = npm.get("profile_url", f"https://www.npmjs.com/~{NPM_USER}")
    blog = profile.get("blog") or f"https://linktr.ee/{GITHUB_USER}"
    section = f"""{README_START}
<div align="center">
  <a href="https://github.com/{GITHUB_USER}">
    <img src="{raw_svg}" width="100%" alt="Dynamic sci-fi profile card for {html.escape(profile['name'])}" />
  </a>
</div>

<div align="center">

| GitHub telemetry | npm telemetry |
|:---:|---:|
| **Public repositories:** {format_number(data['github']['repos'])} | **Packages:** {format_number(npm['package_count'])} |
| **Total stars:** {format_number(data['github']['stars'])} | **Downloads / 12 months:** {format_number(npm['yearly_downloads'])} |
| **Followers:** {format_number(data['github']['followers'])} | **Monthly downloads:** {format_number(npm['monthly_downloads'])} |

[GitHub](https://github.com/{GITHUB_USER}) · [npm]({npm_url}) · [Links]({blog}) · Updated `{generated_at}`

</div>
{README_END}"""

    current = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""
    if README_START in current and README_END in current:
        pattern = re.compile(
            rf"{re.escape(README_START)}.*?{re.escape(README_END)}",
            flags=re.DOTALL,
        )
        return pattern.sub(section, current, count=1).rstrip() + "\n"
    return current.rstrip() + "\n\n" + section + "\n"


def main() -> None:
    cache = load_cache()
    github = fetch_or_reuse("github", fetch_github_data, cache)
    npm = fetch_or_reuse("npm", fetch_npm_data, cache)

    unchanged = cache.get("github") == github and cache.get("npm") == npm
    generated_at = cache.get("generated_at") if unchanged else utc_now().isoformat().replace("+00:00", "Z")
    if not isinstance(generated_at, str):
        generated_at = utc_now().isoformat().replace("+00:00", "Z")

    snapshot = {
        "generated_at": generated_at,
        "github": github,
        "npm": npm,
    }
    atomic_write(DATA_PATH, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    atomic_write(SVG_PATH, render_svg(snapshot))
    atomic_write(README_PATH, render_readme(snapshot))

    print(
        "Profile updated: "
        f"{format_number(github['repos'])} repos, "
        f"{format_number(github['stars'])} stars, "
        f"{format_number(npm['package_count'])} npm packages, "
        f"{format_number(npm['yearly_downloads'])} yearly downloads."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Profile update failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
