#!/usr/bin/env python3
"""Warn when a newer patch release exists for high-risk pinned software.

Covers what pip-audit does not: Docker base images (Dockerfiles, compose files,
GitLab CI, GitHub Actions workflows, the legacy CircleCI config), *_VERSION build args and
CI variables, the Python version in .python-version, .tool-versions and
setup-python, GitHub Actions `uses:` pins, `pip install x==1.2.3` /
`pipx install x==1.2.3` / `npm install -g x@1.2.3` lines, and the requirements
files in REQUIREMENTS_FILES. requirements.txt and test-requirements.txt are
audited by pip-audit and deliberately skipped here.

Pins are discovered, not listed, so a new FROM line or workflow step is checked
without touching this file. A new *_VERSION arg is the exception: it needs an
entry in VERSION_VARS to say where its releases live, and is reported as
unchecked until it has one.

Every pin is rated by RISK_RULES: high when the software handles internet
traffic, low for internal services, build stages, local tooling and CI. Exit
status is WARN_EXIT when a high-risk pin has a patch upgrade or could not be
checked, and 0 otherwise; minor and major releases and all low-risk findings
are listed but never warn, since moving to a new minor is a planned upgrade,
not a fix to pick up. Stdlib only (Python >= 3.9), so the CI job installs
nothing.

Keep this file in step with the files it reads: a new service or change in
exposure needs a RISK_RULES entry, a new *_VERSION arg a VERSION_VARS entry, a
file in a new place a glob here. Checklist in CLAUDE.md, "Pinned versions and
CI audits".
"""

import functools
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

WARN_EXIT = 3
TIMEOUT = 30
USER_AGENT = "edgemanage-version-check"

# Never searched: virtualenvs, installed packages, caches, build output, VCS data.
SKIP_DIRS = {
    ".git", ".venv", "venv", "venv3", "node_modules", "__pycache__", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".cache", "build", "dist", "htmlcov",
    "edgemanage.egg-info",
}

DOCKER_FILES = [
    "**/Dockerfile*",
    "**/docker-compose*.yml",
    "**/compose*.yaml",
    ".gitlab-ci.yml",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".circleci/config.yml",
]
# The Python version for pyenv, asdf and mise.
TOOLCHAIN_FILES = ["**/.python-version", "**/.tool-versions"]
# Requirements files this script owns. Both locks (requirements.txt and
# test-requirements.txt) belong to pip-audit, so each pin is watched by one job.
REQUIREMENTS_FILES = []

# *_VERSION build args and CI variables -> (source, name, tag prefix).
# Sources: docker (Docker Hub image), pypi, npm, git (repo URL).
VERSION_VARS = {
    # .gitlab-ci.yml: image: python:${PROD_PYTHON_VERSION}-bookworm.
    "PROD_PYTHON_VERSION": ("docker", "python", ""),
}

# .tool-versions (asdf/mise) tool -> (source, name); other tools are listed as not checked.
TOOL_SOURCES = {
    "python": ("docker", "python"),
}

HIGH, LOW = "high", "low"
# (file glob, software glob, risk, why); first match wins, and `*` also matches
# "/". A pin no rule matches is rated high, so a new public service is not
# quietly downgraded. Nothing in this repo is deployed: docker/ is a local
# harness. The Python pins rated high are the ones kept in step with
# production, where edge_manage fetches from and parses responses of internet
# edges. In .gitlab-ci.yml that is PROD_PYTHON_VERSION.
RISK_RULES = [
    (".github/workflows/python-app.yml", "python", HIGH, "tracks production Python"),
    (".gitlab-ci.yml", "python", HIGH, "tox job tracks production Python"),
    ("docker/edgemanage/Dockerfile", "python", HIGH, "tracks production Python"),
    ("docker/bind/Dockerfile", "*", LOW, "local harness; port 5354 on the dev host"),
    ("docker/origin/Dockerfile", "*", LOW, "local harness; fake edge"),
    ("docker/test/Dockerfile", "*", LOW, "test runner only"),
    (".github/workflows/*", "*", LOW, "CI only"),
    (".gitlab-ci.yml", "*", LOW, "CI only"),
    (".circleci/*", "*", LOW, "legacy CI, not live"),
    ("**/.python-version", "*", LOW, "local development toolchain"),
    ("**/.tool-versions", "*", LOW, "local development toolchain"),
]

# Release versions only: rc/alpha/beta/dev builds never match.
NUMERIC = r"(\d+(?:\.\d+)*)"


@dataclass(frozen=True)
class Pin:
    where: str  # file:line
    source: str  # docker | pypi | npm | git
    name: str  # image repository, package name, or git URL
    version: str  # numeric part as written, e.g. "3.14.8"
    prefix: str = ""  # tag text before the version ("v", "openssl-")
    suffix: str = ""  # tag text after it ("-slim-bookworm")
    # actions/checkout@v4, python-version: 3.12: compared at their own precision
    floating: bool = False

    @property
    def label(self):
        return self.name.removeprefix("https://github.com/")

    def tag(self, version):
        if self.source in ("docker", "git"):
            return f"{self.prefix}{version}{self.suffix}"
        return version


@dataclass
class Note:
    where: str
    message: str


# -- discovery ----------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def glob_regex(pattern, star):
    """`**` crosses directories; `star` is what a single `*` may match."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + star, i + 1
        elif pattern[i] == "?":
            out, i = out + ("." if star == ".*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def match_path(path, pattern):
    return glob_regex(pattern, "[^/]*").fullmatch(path)


def fnmatch(name, pattern):
    return glob_regex(pattern, ".*").fullmatch(name)


def walk():
    files = []
    for root, dirs, names in os.walk("."):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files += [
            os.path.relpath(os.path.join(root, name)).replace(os.sep, "/") for name in names
        ]
    return files


def lines(patterns, files):
    seen = set()
    for pattern in patterns:
        for path in sorted(file for file in files if match_path(file, pattern)):
            if path in seen:
                continue
            seen.add(path)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for number, text in enumerate(fh, 1):
                    text = text.rstrip("\r\n")
                    if not text.strip().startswith("#"):
                        yield f"{path}:{number}", path, text


def image_pin(where, ref, notes):
    """A Pin for a Docker image reference, or None (with a note when it is worth one)."""
    # docker.io/library/python:3 is python:3, so it matches the same RISK_RULES.
    ref = re.sub(r"^(?:(?:index\.)?docker\.io/)?(?:library/)?", "", ref.split("@", 1)[0])
    repo, _, tag = ref.rpartition(":")
    if not repo or not re.fullmatch(r"\w[\w.-]*", tag) or "$" in ref:
        return None
    first = repo.split("/", 1)[0]
    if "/" in repo and ("." in first or ":" in first or first == "localhost"):
        notes.append(Note(where, f"{ref}: not a Docker Hub image, not checked"))
        return None
    match = re.fullmatch(rf"(v?){NUMERIC}(.*)", tag)
    if not match:
        notes.append(Note(where, f"{ref}: tag is not a version, not checked"))
        return None
    return Pin(where, "docker", repo, match[2], match[1], match[3])


def discover():
    files = walk()
    pins, unchecked, notes = [], [], []
    stages, stages_path = set(), None

    def add_image(where, ref):
        pin = image_pin(where, ref, notes)
        pins.extend([pin] if pin else [])

    for where, path, text in lines(DOCKER_FILES, files):
        if re.search(r"(^|/)Dockerfile[^/]*$", path):
            if path != stages_path:  # stage names are per file
                stages, stages_path = set(), path
            match = re.match(
                r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+as\s+(\S+))?", text, re.I
            )
            if match:
                if match[2]:
                    stages.add(match[2].lower())
                if match[1].lower() not in stages:
                    add_image(where, match[1])
            var = re.match(r"\s*(?:ARG|ENV)\s+([A-Z0-9_]*VERSION)=[\"']?([^\s\"']+)", text)
        else:
            match = re.match(r"\s*(?:-\s*)?(?:image|name|container):\s*[\"']?([^\s\"'#]+)", text)
            if match and ":" in match[1]:
                add_image(where, match[1])
            # GitLab `services:` entries can be bare list items: `- postgres:16.4`.
            if path == ".gitlab-ci.yml":
                match = re.fullmatch(r"\s*-\s+[\"']?([a-z][\w./-]*:[\w.-]+)[\"']?\s*", text)
                if match:
                    add_image(where, match[1])
            var = re.match(r"\s*([A-Z0-9_]*VERSION):\s*[\"']?([^\s\"']+)", text)

            match = re.match(
                r"\s*(?:-\s*)?uses:\s*[\"']?([\w.-]+/[\w.-]+)(?:/[^@\s\"']*)?@([^\s\"'#]+)", text
            )
            if match:
                version = re.fullmatch(rf"(v?){NUMERIC}", match[2])
                if version:
                    url = f"https://github.com/{match[1]}"
                    pins.append(Pin(where, "git", url, version[2], version[1], floating=True))
                else:
                    if re.fullmatch(r"[0-9a-f]{40}", match[2]):
                        why = "pinned to a commit SHA"
                    else:
                        why = "ref is not a version"
                    notes.append(Note(where, f"{match[1]}@{match[2]}: {why}, not checked"))
            match = re.fullmatch(
                r"\s*(?:-\s*)?python-version:\s*[\"']?(\d+(?:\.\d+)*)(?:\.x)?[\"']?\s*(?:#.*)?",
                text,
            )
            if match:
                pins.append(Pin(where, "docker", "python", match[1], floating=True))

        if var:
            name, value = var[1], var[2]
            if name not in VERSION_VARS:
                unchecked.append(Note(where, f"{name}={value}: no release source in VERSION_VARS"))
            else:
                source, target, prefix = VERSION_VARS[name]
                number = re.fullmatch(rf"(?:{re.escape(prefix)})?{NUMERIC}", value)
                if number:
                    pins.append(Pin(where, source, target, number[1], prefix))
                else:
                    unchecked.append(Note(where, f"{name}={value}: not a version number"))

        if re.search(r"\b(?:npm\s+(?:install|i)|npx|corepack\s+(?:prepare|install|use))\b", text):
            for token in text.split():
                match = re.fullmatch(rf"(@?[^@\s]+)@{NUMERIC}", token)
                if match:
                    pins.append(Pin(where, "npm", match[1], match[2]))
        if re.search(r"\b(?:pip3?|pipx|uv\s+pip|uv\s+tool)\s+install\b", text):
            for token in text.split():
                match = re.fullmatch(rf"([A-Za-z0-9][\w.\-]*)(?:\[[^\]]*\])?=={NUMERIC}", token)
                if match:
                    pins.append(Pin(where, "pypi", match[1], match[2]))

    for where, path, text in lines(TOOLCHAIN_FILES, files):
        value = text.strip()
        if not value:
            continue
        if path.rsplit("/", 1)[-1] == ".python-version":
            match = re.fullmatch(NUMERIC, value)
            if match:
                pins.append(Pin(where, "docker", "python", match[1], floating=True))
            else:
                notes.append(Note(where, f"{value}: not a CPython version, not checked"))
        else:
            tool, _, version = value.partition(" ")
            version = version.split()[0] if version.split() else ""
            match = re.fullmatch(rf"v?{NUMERIC}", version)
            if tool in TOOL_SOURCES and match:
                source, name = TOOL_SOURCES[tool]
                pins.append(Pin(where, source, name, match[1], floating=source == "docker"))
            else:
                if tool in TOOL_SOURCES:
                    why = "not a version"
                else:
                    why = "no release source in TOOL_SOURCES"
                notes.append(Note(where, f"{tool} {version}: {why}, not checked"))

    for where, _, text in lines(REQUIREMENTS_FILES, files):
        match = re.match(
            rf"\s*([A-Za-z0-9][\w.\-]*)(?:\[[^\]]*\])?\s*==\s*{NUMERIC}\s*(?:[;#\\]|$)", text
        )
        if match:
            pins.append(Pin(where, "pypi", match[1], match[2]))

    return pins, unchecked, notes


# -- release lookup -------------------------------------------------------------


def get_json(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response), response.headers


def docker_tags(repo):
    if "/" not in repo:
        repo = f"library/{repo}"
    token = get_json(
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    )[0]["token"]
    url, tags = f"https://registry-1.docker.io/v2/{repo}/tags/list?n=10000", []
    while url:
        body, headers = get_json(url, {"Authorization": f"Bearer {token}"})
        tags += body.get("tags") or []
        link = re.match(r"<([^>]+)>", headers.get("Link", ""))
        url = f"https://registry-1.docker.io{link[1]}" if link else None
    return tags


def pypi_versions(name):
    releases = get_json(f"https://pypi.org/pypi/{name}/json")[0]["releases"]
    return [
        v for v, files in releases.items() if files and not all(f.get("yanked") for f in files)
    ]


def npm_versions(name):
    # The abbreviated document: the full one for a popular package is tens of MB.
    body = get_json(
        f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}",
        {"Accept": "application/vnd.npm.install-v1+json"},
    )[0]
    return [v for v, release in body["versions"].items() if not release.get("deprecated")]


def git_tags(url):
    # ls-remote rather than the GitHub API, which allows 60 anonymous calls an
    # hour per IP and shared runners exhaust that. No prompt: a private repo fails, not hangs.
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", url],
        capture_output=True, text=True, timeout=120, check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout
    return [line.split("refs/tags/", 1)[1] for line in out.splitlines() if "refs/tags/" in line]


FETCHERS = {"docker": docker_tags, "pypi": pypi_versions, "npm": npm_versions, "git": git_tags}


def fetch(key):
    source, name = key
    for attempt in range(3):
        try:
            return FETCHERS[source](name)
        except Exception as exc:  # noqa: BLE001 -- any failure is reported per pin, never fatal
            error = exc
    return error


# -- comparison -----------------------------------------------------------------


def sort_key(version):
    # Zero-padded, so 2.11 and 2.11.0 compare equal.
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (8 - len(parts))


def newer(pin, available):
    """{"patch"|"minor"|"major": newest version string} of the releases newer than the pin."""
    pattern = re.compile(rf"{re.escape(pin.prefix)}{NUMERIC}{re.escape(pin.suffix)}")
    current = sort_key(pin.version)
    precision = pin.version.count(".") + 1
    best = {}
    for item in available:
        match = pattern.fullmatch(item)
        if not match:
            continue
        version = match[1]
        # A floating pin (python:3.12-slim, actions/checkout@v4) is only ever
        # replaced at its own precision; 3.12.4 is the same line, not an upgrade.
        if pin.source == "docker" or pin.floating:
            parts = version.split(".")
            if len(parts) < precision:
                continue
            version = ".".join(parts[:precision])
        key = sort_key(version)
        if key <= current:
            continue
        if key[0] != current[0]:
            kind = "major"
        elif key[1] != current[1]:
            kind = "minor"
        else:
            kind = "patch"
        if kind not in best or key > sort_key(best[kind]):
            best[kind] = version
    return best


# -- report ---------------------------------------------------------------------


def table(title, headers, rows):
    """Print rows as a left-aligned plain-text table, so it reads the same in a job log."""
    if not rows:
        return
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]

    def line(cells):
        return "  ".join(str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip()

    print(f"\n{title}\n")
    print(line(headers))
    print(line("-" * width for width in widths))
    for row in rows:
        print(line(row))


def location_key(where):
    path, _, number = where.rpartition(":")
    return path, int(number)


def risk(where, software=""):
    path = where.rpartition(":")[0]
    for pattern, name, level, why in RISK_RULES:
        if fnmatch(path, pattern) and fnmatch(software, name):
            return level, why
    return HIGH, "no RISK_RULES entry, assumed exposed"


def annotate(where, message):
    # On GitHub Actions a continue-on-error job shows green in the PR checks, so
    # also annotate the lines that warn.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        path, number = location_key(where)
        print(f"::warning file={path},line={number}::{message}")


# Actionable rows first, high risk before low, then by file.
STATUS_ORDER = {"PATCH": 0, "ERROR": 1, "MINOR": 2, "OK": 3}
RISK_ORDER = {HIGH: 0, LOW: 1}


def main():
    pins, unchecked, notes = discover()
    keys = sorted({(pin.source, pin.name) for pin in pins})
    with ThreadPoolExecutor(max_workers=8) as pool:
        releases = dict(zip(keys, pool.map(fetch, keys)))

    rows, errors = [], []
    for pin in pins:
        level, why = risk(pin.where, pin.label)
        available = releases[(pin.source, pin.name)]
        if isinstance(available, Exception):
            status, best = "ERROR", {}
            errors.append((level, pin.where, pin.label, str(available)))
        else:
            best = newer(pin, available)
            status = "PATCH" if "patch" in best else "MINOR" if "minor" in best else "OK"
        upgrades = [
            pin.tag(best[kind]) if kind in best else "-" for kind in ("patch", "minor", "major")
        ]
        rows.append((status, level, pin.where, pin.label, pin.tag(pin.version), *upgrades, why))
    rows.sort(key=lambda row: (STATUS_ORDER[row[0]], RISK_ORDER[row[1]], location_key(row[2])))

    not_checked = [("UNCHECKED", risk(n.where)[0], n.where, n.message) for n in unchecked]
    not_checked += [("SKIPPED", risk(n.where)[0], n.where, n.message) for n in notes]

    print(f"Checked {len(pins)} pinned versions across {len(keys)} release sources.")
    table(
        "Pinned versions (only a HIGH risk PATCH warns; MINOR and MAJOR are information only)",
        ("STATUS", "RISK", "LOCATION", "SOFTWARE", "CURRENT", "PATCH", "MINOR", "MAJOR", "WHY"),
        rows,
    )
    table("Lookup errors", ("RISK", "LOCATION", "SOFTWARE", "ERROR"), errors)
    table("Not checked", ("STATUS", "RISK", "LOCATION", "REASON"), not_checked)

    def count(status, level):
        return sum(row[0] == status and row[1] == level for row in rows)

    high_unchecked = [row for row in not_checked if row[0] == "UNCHECKED" and row[1] == HIGH]
    print(
        f"\nHigh risk: {count('PATCH', HIGH)} patch(es), {count('ERROR', HIGH)} lookup error(s),"
        f" {len(high_unchecked)} unchecked, {count('MINOR', HIGH)} minor"
        " (listed only, never warns)."
        f"\nLow risk:  {count('PATCH', LOW)} patch(es), {count('MINOR', LOW)} minor,"
        f" {count('ERROR', LOW)} lookup error(s) (listed only, never warn)."
    )

    for status, level, where, software, current, patch, *_ in rows:
        if level == HIGH and status == "PATCH":
            annotate(where, f"{software} {current} has a patch release: {patch}")
        elif level == HIGH and status == "ERROR":
            annotate(where, f"{software}: release lookup failed")
    for _, _, where, message in high_unchecked:
        annotate(where, message)

    if count("PATCH", HIGH) or count("ERROR", HIGH) or high_unchecked:
        print("A high-risk pin needs attention, so this job ends with a warning.")
        return WARN_EXIT
    print("No high-risk pin needs attention.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
