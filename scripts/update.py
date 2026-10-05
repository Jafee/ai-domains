#!/usr/bin/env python3
"""Convert v2fly's category-ai-!cn to an AdGuard DNS subscription.

Only Python's standard library is used. Upstream files are parsed as data;
no upstream code, workflows, or shell commands are executed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


UPSTREAM = "v2fly/domain-list-community"
ROOT_LIST = "category-ai-!cn"
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILES = ("adguard.txt", "sources.json", "metadata.json")
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
LIST_NAME = re.compile(r"[a-z0-9!-]+\Z")
ATTRIBUTE = re.compile(r"[a-z0-9!]+\Z")
DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class UpdateError(Exception):
    """An incomplete or unsupported input must not replace the subscription."""


@dataclass(frozen=True)
class Entry:
    kind: str
    value: str
    attrs: frozenset[str]
    source: str


@dataclass(frozen=True)
class Include:
    name: str
    required: frozenset[str]
    excluded: frozenset[str]


def validate_name(name: str) -> str:
    name = name.lower()
    if not LIST_NAME.fullmatch(name):
        raise UpdateError(f"Invalid list name: {name!r}")
    return name


def canonical_source(text: str) -> str:
    """Keep rule data, without upstream comments or blank lines."""
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line) + "\n"


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "ai-domains-updater", "Accept": "application/vnd.github+json"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=45) as response:
                data = response.read(MAX_DOWNLOAD + 1)
            if len(data) > MAX_DOWNLOAD:
                raise UpdateError("Upstream response exceeds the download limit")
            return data
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == 2:
                raise UpdateError(f"Upstream request failed: HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == 2:
                raise UpdateError(f"Upstream request failed: {error}") from error
        time.sleep(2 ** attempt)
    raise UpdateError("Upstream request failed")


def archive_sources(data: bytes) -> dict[str, str]:
    """Read only regular data files in memory; never extract the archive."""
    files = {}
    total_size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                parts = member.name.split("/")
                if len(parts) != 3 or parts[1] != "data":
                    continue
                if member.isdir():
                    continue
                if not member.isfile():
                    raise UpdateError("Non-regular upstream data file")
                name = validate_name(parts[2])
                total_size += member.size
                if member.size > 1024 * 1024 or total_size > MAX_SOURCE_BYTES:
                    raise UpdateError("Upstream data exceeds the size limit")
                stream = archive.extractfile(member)
                if stream is None or name in files:
                    raise UpdateError("Missing or duplicate upstream data file")
                files[name] = canonical_source(stream.read().decode("utf-8"))
    except (tarfile.TarError, UnicodeError, OSError) as error:
        raise UpdateError(f"Invalid upstream archive: {error}") from error
    if ROOT_LIST not in files:
        raise UpdateError(f"Upstream archive is missing {ROOT_LIST}")
    return files


def latest_snapshot() -> dict:
    try:
        commit = json.loads(fetch_bytes(f"https://api.github.com/repos/{UPSTREAM}/commits/master"))
        sha = commit["sha"]
        commit_date = commit["commit"]["committer"]["date"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise UpdateError("Invalid upstream commit SHA")
        if not isinstance(commit_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", commit_date):
            raise UpdateError("Invalid upstream commit date")
    except (ValueError, KeyError, TypeError) as error:
        raise UpdateError("Invalid GitHub commit response") from error
    files = archive_sources(fetch_bytes(f"https://codeload.github.com/{UPSTREAM}/tar.gz/{sha}"))
    return {
        "root": ROOT_LIST,
        "upstream": f"https://github.com/{UPSTREAM}",
        "upstream_ref": "master",
        "upstream_commit": sha,
        "upstream_commit_date": commit_date,
        "snapshot_date": commit_date[:10],
        "acquisition": "GitHub archive at the exact upstream commit; only rule data retained.",
        "files": files,
    }


def parse_sources(files: dict[str, str]) -> tuple[dict, dict]:
    parsed = {}
    affiliated = {}
    for name, text in sorted(files.items()):
        validate_name(name)
        items = []
        for number, line in enumerate(text.splitlines(), 1):
            fields = line.split()
            if not fields:
                continue
            spec, *annotations = fields
            kind, value = spec.split(":", 1) if ":" in spec else ("domain", spec)
            kind = kind.lower()
            attrs, excluded, affiliations = set(), set(), set()
            for annotation in annotations:
                if annotation.startswith("&") and kind != "include":
                    affiliations.add(validate_name(annotation[1:]))
                    continue
                if not annotation.startswith("@"):
                    raise UpdateError(f"{name}:{number}: Invalid annotation")
                attr = annotation[1:].lower()
                negative = kind == "include" and attr.startswith("-")
                if negative:
                    attr = attr[1:]
                if not ATTRIBUTE.fullmatch(attr):
                    raise UpdateError(f"{name}:{number}: Invalid attribute")
                (excluded if negative else attrs).add(attr)
            if not value:
                raise UpdateError(f"{name}:{number}: Empty rule")
            if kind == "include":
                items.append(Include(validate_name(value), frozenset(attrs), frozenset(excluded)))
            else:
                if kind not in {"domain", "full", "keyword", "regexp"}:
                    raise UpdateError(f"{name}:{number}: Unsupported rule type {kind!r}")
                entry = Entry(kind, value, frozenset(attrs), name)
                items.append(entry)
                for target in affiliations:
                    affiliated.setdefault(target, []).append(entry)
        parsed[name] = items
    return parsed, affiliated


def expand_sources(files: dict[str, str]) -> tuple[list[Entry], set[str]]:
    parsed, affiliated = parse_sources(files)
    cache = {}
    used = set()

    def expand(name: str, stack: tuple[str, ...] = ()) -> list[Entry]:
        if name in stack:
            raise UpdateError("Include cycle: " + " -> ".join((*stack, name)))
        if name in cache:
            return cache[name]
        if name not in parsed and name not in affiliated:
            raise UpdateError(f"Missing included list: {name}")
        if name in parsed:
            used.add(name)
        entries = list(affiliated.get(name, []))
        for item in parsed.get(name, []):
            if isinstance(item, Include):
                entries.extend(entry for entry in expand(item.name, (*stack, name))
                               if item.required <= entry.attrs and not item.excluded & entry.attrs)
            else:
                entries.append(item)
        used.update(entry.source for entry in entries)
        cache[name] = entries
        return entries

    return expand(ROOT_LIST), used


def domain_rule(value: str) -> str:
    value = value.lower()
    if len(value) > 253 or not all(DOMAIN_LABEL.fullmatch(label) for label in value.split(".")):
        raise UpdateError(f"Invalid domain: {value!r}")
    return f"||{value}^"


def regex_rule(value: str) -> str:
    """Relax hostname-start anchors to also match domain label boundaries."""
    if "/" in value or "[[:" in value:
        raise UpdateError("Regex with slash or POSIX character class needs manual conversion")
    converted = []
    in_class = False
    class_position = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            pair = value[index:index + 2]
            converted.append(r"(?:^|\.)" if pair == r"\A" and not in_class else pair)
            index += 2
            class_position = max(0, class_position) + 1
            continue
        if char == "[" and not in_class:
            in_class, class_position = True, 0
        elif char == "]" and in_class and class_position > 0:
            in_class = False
        if char == "^" and not in_class:
            converted.append(r"(?:^|\.)")
        else:
            converted.append(char)
        if in_class and char != "[":
            # A leading ^ negates the class; a following ] can be literal.
            if class_position == 0 and char == "^":
                class_position = -1
            else:
                class_position = max(0, class_position) + 1
        index += 1
    pattern = "".join(converted)
    try:
        re.compile(value)
        re.compile(pattern)
    except re.error as error:
        raise UpdateError(f"Regex needs manual conversion: {error}") from error
    return f"/{pattern}/"


def entry_rule(entry: Entry) -> str:
    if entry.kind in {"domain", "full"}:
        # full: is deliberately extended to subdomains, per the requested scope.
        return domain_rule(entry.value)
    if entry.kind == "regexp":
        return regex_rule(entry.value)
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", entry.value):
        raise UpdateError("Invalid domain keyword")
    return "/" + re.escape(entry.value.lower()) + "/"


def json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def build_artifacts(snapshot: dict) -> dict[str, str]:
    if snapshot.get("root") != ROOT_LIST or not isinstance(snapshot.get("files"), dict):
        raise UpdateError("Invalid source snapshot")
    files = {validate_name(name): canonical_source(text) for name, text in snapshot["files"].items()}
    entries, used = expand_sources(files)
    rules = {entry_rule(entry) for entry in entries}
    if not rules:
        raise UpdateError("Refusing to publish an empty subscription")
    domain_rules = sorted((rule for rule in rules if rule.startswith("||")), key=lambda rule: rule[2:-1])
    regex_rules = sorted(rule for rule in rules if rule.startswith("/"))
    retained = {key: value for key, value in snapshot.items() if key != "files"}
    retained["files"] = {name: files[name] for name in sorted(used)}
    revision = snapshot.get("upstream_commit") or "unpinned bootstrap snapshot"
    source_date = snapshot.get("upstream_commit_date") or snapshot.get("snapshot_date", "unknown")
    header = [
        "! Title: ai-domains - AI outside mainland China",
        "! Description: Expanded v2fly category-ai-!cn; listed hosts and all subdomains.",
        f"! Source: https://github.com/{UPSTREAM}/blob/master/data/{ROOT_LIST}",
        f"! Source revision: {revision}",
        f"! Source date: {source_date}",
        f"! Rules: {len(rules)} ({len(domain_rules)} domain + {len(regex_rules)} regex)",
        "! Updates: weekly via GitHub Actions",
        "! Full-host rules intentionally include subdomains.",
        "!",
    ]
    blocklist = "\n".join(header + domain_rules + regex_rules) + "\n"
    metadata = {
        "root": ROOT_LIST,
        "upstream_commit": snapshot.get("upstream_commit"),
        "upstream_commit_date": snapshot.get("upstream_commit_date"),
        "source_file_count": len(used),
        "expanded_entry_count": len(entries),
        "source_entry_types": dict(Counter(entry.kind for entry in entries)),
        "domain_rule_count": len(domain_rules),
        "regex_rule_count": len(regex_rules),
        "total_rule_count": len(rules),
        "blocklist_sha256": hashlib.sha256(blocklist.encode()).hexdigest(),
        "source_sha256": {name: hashlib.sha256(files[name].encode()).hexdigest() for name in sorted(used)},
    }
    return {"adguard.txt": blocklist, "sources.json": json_text(retained), "metadata.json": json_text(metadata)}


def write_artifacts(output: Path, artifacts: dict[str, str]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    # Render/validate everything first. Prepare every file before replacing any.
    with tempfile.TemporaryDirectory(prefix=".ai-domains-", dir=output) as temporary:
        staged = Path(temporary)
        for name, text in artifacts.items():
            (staged / name).write_text(text, encoding="utf-8", newline="\n")
            (staged / name).chmod(0o644)
        for name in artifacts:
            os.replace(staged / name, output / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Regenerate from a saved sources.json without network access")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--check", action="store_true", help="Check generated files instead of writing them")
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else latest_snapshot()
        artifacts = build_artifacts(snapshot)
        if args.check:
            for name, text in artifacts.items():
                path = args.output_dir / name
                if not path.exists() or path.read_text(encoding="utf-8") != text:
                    raise UpdateError(f"Generated file is out of date: {name}")
        else:
            write_artifacts(args.output_dir, artifacts)
        stats = json.loads(artifacts["metadata.json"])
        print(f"{'Verified' if args.check else 'Generated'} {stats['total_rule_count']} rules from {stats['source_file_count']} source files.")
        return 0
    except (UpdateError, OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
