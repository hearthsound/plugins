#!/usr/bin/env python3
"""Build the official Hearthsound plugin index.

The index is assembled from the entry files under ``plugins/`` and the GitHub
releases they name. Each release asset named ``<id>-<version>.hsplugin`` is
downloaded and hashed. Plugin metadata is read from the ``manifest.json`` inside
the archive, and releases whose manifest ``api`` is not listed in
``supportedApis`` (``index.toml``) are skipped.

Usage::

    python3 scripts/build_index.py --out site/v1/index.json
    python3 scripts/build_index.py --validate site/v1/index.json

Set ``GITHUB_TOKEN`` to avoid the anonymous GitHub API rate limit. Only the
Python standard library is used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
API_ROOT = "https://api.github.com"
USER_AGENT = "hearthsound-plugin-index"
FORMAT_VERSION = 1
HTTP_TIMEOUT_SECS = 60
MAX_API_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
RELEASES_PER_PAGE = 100
PERMISSION_KEYS = ("http", "kv", "files", "process")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

ID_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9][a-z0-9-]*)+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_IDENT = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({SEMVER_IDENT}(?:\.{SEMVER_IDENT})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class BuildError(Exception):
    """A condition that makes the index impossible to build or unsafe to publish."""


@dataclass(frozen=True)
class Config:
    """Settings read from ``index.toml``."""

    name: str
    supported_apis: tuple[str, ...]


@dataclass(frozen=True)
class Entry:
    """One plugin listed in ``plugins/<id>.toml``."""

    id: str
    repo: str
    tag_pattern: str


def log(message: str) -> None:
    """Write a progress or warning line to standard error."""
    print(message, file=sys.stderr)


def semver_key(version: str) -> tuple[Any, ...] | None:
    """Return a sort key for a semantic version, or ``None`` if it is not one.

    Releases sort above their own pre-releases, and pre-release identifiers
    compare as the specification describes (numeric below alphanumeric).
    """
    match = SEMVER_RE.match(version)
    if match is None:
        return None
    core = tuple(int(match.group(i)) for i in (1, 2, 3))
    pre = match.group(4)
    if pre is None:
        return (core, 1, ())
    idents = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in pre.split(".")
    )
    return (core, 0, idents)


def tag_regex(pattern: str) -> re.Pattern[str]:
    """Compile a ``tag_pattern`` such as ``v{version}`` into a matcher.

    The pattern must contain ``{version}`` exactly once. The returned regex
    captures the version in the group ``version``.

    Raises ``BuildError`` when the pattern does not contain one placeholder.
    """
    if pattern.count("{version}") != 1:
        raise BuildError(f"tag_pattern must contain {{version}} once: {pattern!r}")
    head, tail = pattern.split("{version}")
    return re.compile(
        "^" + re.escape(head) + "(?P<version>[^/]+?)" + re.escape(tail) + "$"
    )


def load_config(path: Path) -> Config:
    """Read ``index.toml`` and return the index name and supported APIs.

    Raises ``BuildError`` when a field is missing or has the wrong type.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    name = raw.get("name")
    apis = raw.get("supportedApis")
    if not isinstance(name, str) or not name:
        raise BuildError(f"{path.name}: name must be a non-empty string")
    if not isinstance(apis, list) or not apis or not all(
        isinstance(api, str) and api for api in apis
    ):
        raise BuildError(f"{path.name}: supportedApis must be a non-empty string list")
    return Config(name=name, supported_apis=tuple(apis))


def load_entries(directory: Path) -> list[Entry]:
    """Read every ``plugins/<id>.toml`` entry, sorted by file name.

    Each entry's ``id`` must match its file name. Raises ``BuildError`` on any
    malformed entry or when no entries exist.
    """
    entries: list[Entry] = []
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        plugin_id = raw.get("id")
        repo = raw.get("repo")
        tag_pattern = raw.get("tag_pattern")
        if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
            raise BuildError(f"{path.name}: id is missing or malformed")
        if plugin_id != path.stem:
            raise BuildError(f"{path.name}: id {plugin_id!r} must match the file name")
        if not isinstance(repo, str) or not REPO_RE.match(repo):
            raise BuildError(f"{path.name}: repo must be 'owner/name'")
        if not isinstance(tag_pattern, str):
            raise BuildError(f"{path.name}: tag_pattern must be a string")
        tag_regex(tag_pattern)
        entries.append(Entry(id=plugin_id, repo=repo, tag_pattern=tag_pattern))
    if not entries:
        raise BuildError(f"no plugin entries found in {directory}")
    return entries


def fetch(url: str, *, token: str | None, api: bool, limit: int) -> bytes:
    """Download ``url`` and return its body, refusing bodies over ``limit`` bytes.

    The token is sent only to the GitHub API. Raises ``BuildError`` on transport
    failures, HTTP errors, or oversize bodies.
    """
    headers = {"User-Agent": USER_AGENT}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if token:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS) as response:
            body = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BuildError(f"GET {url} failed: {exc}") from exc
    if len(body) > limit:
        raise BuildError(f"GET {url} returned more than {limit} bytes")
    return body


def list_releases(repo: str, token: str | None) -> list[dict[str, Any]]:
    """Return every release of ``repo``, following pagination.

    Raises ``BuildError`` when the API response is not a JSON array.
    """
    releases: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{API_ROOT}/repos/{repo}/releases?per_page={RELEASES_PER_PAGE}&page={page}"
        batch = json.loads(fetch(url, token=token, api=True, limit=MAX_API_BYTES))
        if not isinstance(batch, list):
            raise BuildError(f"{url} did not return a list")
        releases.extend(batch)
        if len(batch) < RELEASES_PER_PAGE:
            return releases
        page += 1


def read_manifest(archive: bytes) -> dict[str, Any]:
    """Return the parsed ``manifest.json`` stored at the root of a ``.hsplugin``.

    Raises ``BuildError`` when the archive is not a zip, lacks a manifest, or
    the manifest is not a JSON object.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            with bundle.open("manifest.json") as handle:
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except KeyError as exc:
        raise BuildError("archive has no manifest.json at its root") from exc
    except zipfile.BadZipFile as exc:
        raise BuildError(f"asset is not a zip archive: {exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BuildError("manifest.json is larger than 1 MiB")
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise BuildError(f"manifest.json is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BuildError("manifest.json is not a JSON object")
    return manifest


def permissions_of(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the index permission summary from a manifest.

    Only the keys listed in ``PERMISSION_KEYS`` are kept. Raises ``BuildError``
    when the manifest's ``permissions`` value is not an object.
    """
    raw = manifest.get("permissions", {})
    if not isinstance(raw, dict):
        raise BuildError("manifest permissions must be an object")
    return {key: raw[key] for key in PERMISSION_KEYS if key in raw}


def build_version(
    entry: Entry,
    version: str,
    release: dict[str, Any],
    asset: dict[str, Any],
    config: Config,
    token: str | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Download one release asset and describe it as an index version.

    Returns ``(version entry, manifest)``, or ``None`` when the manifest's
    ``api`` is not in ``supportedApis``. Raises ``BuildError`` when the archive
    contradicts the entry (wrong plugin id or version) or fails to download.
    """
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BuildError(f"{entry.repo} {version}: asset has no https download URL")
    data = fetch(url, token=None, api=False, limit=MAX_ARCHIVE_BYTES)
    hinted = asset.get("size")
    if isinstance(hinted, int) and hinted != len(data):
        raise BuildError(f"{entry.repo} {version}: downloaded size differs from release metadata")
    manifest = read_manifest(data)
    if manifest.get("id") != entry.id:
        raise BuildError(f"{entry.repo} {version}: manifest id {manifest.get('id')!r} != {entry.id!r}")
    if manifest.get("version") != version:
        raise BuildError(f"{entry.repo} {version}: manifest version {manifest.get('version')!r} differs")
    api = manifest.get("api")
    if api not in config.supported_apis:
        log(f"skip {entry.repo} {version}: api {api!r} is not in supportedApis")
        return None
    published = release.get("published_at") or release.get("created_at")
    version_entry = {
        "version": version,
        "api": api,
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "publishedAt": published,
        "changelog": (release.get("body") or "").strip(),
        "permissions": permissions_of(manifest),
    }
    return version_entry, manifest


def versions_for(
    entry: Entry, config: Config, token: str | None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Collect every compatible published version of one plugin.

    Drafts and GitHub pre-releases are skipped, as are releases without the
    expected ``<id>-<version>.hsplugin`` asset. Raises ``BuildError`` when two
    releases claim the same version.
    """
    pattern = tag_regex(entry.tag_pattern)
    seen: set[str] = set()
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for release in list_releases(entry.repo, token):
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "")
        match = pattern.match(tag)
        if match is None:
            continue
        version = match.group("version")
        if semver_key(version) is None:
            log(f"skip {entry.repo} {tag}: {version!r} is not a semantic version")
            continue
        if version in seen:
            raise BuildError(f"{entry.repo} has two releases for version {version}")
        seen.add(version)
        expected = f"{entry.id}-{version}.hsplugin"
        asset = next(
            (a for a in release.get("assets", []) if a.get("name") == expected), None
        )
        if asset is None:
            log(f"skip {entry.repo} {tag}: no asset named {expected}")
            continue
        built = build_version(entry, version, release, asset, config, token)
        if built is not None:
            found.append(built)
    return found


def build_plugin(entry: Entry, config: Config, token: str | None) -> dict[str, Any]:
    """Describe one plugin using its newest compatible version for metadata.

    Raises ``BuildError`` when no release of the plugin targets a supported API,
    so an official entry can never silently disappear from the index.
    """
    found = versions_for(entry, config, token)
    if not found:
        raise BuildError(f"{entry.id}: no release targets a supported api")
    found.sort(key=lambda pair: semver_key(pair[0]["version"]), reverse=True)
    manifest = found[0][1]
    homepage = manifest.get("homepage", "")
    return {
        "id": entry.id,
        "name": manifest.get("name"),
        "description": manifest.get("description"),
        "provides": manifest.get("provides"),
        "homepage": homepage,
        "license": manifest.get("license"),
        "official": True,
        "versions": [version for version, _ in found],
    }


def build_index(config: Config, entries: list[Entry], token: str | None) -> dict[str, Any]:
    """Assemble the complete index document for every entry.

    Raises ``BuildError`` when any entry cannot be described.
    """
    plugins = [build_plugin(entry, config, token) for entry in entries]
    return {
        "formatVersion": FORMAT_VERSION,
        "name": config.name,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime(TIMESTAMP_FORMAT),
        "plugins": plugins,
    }


def expect(condition: bool, message: str) -> None:
    """Raise ``BuildError(message)`` unless ``condition`` holds."""
    if not condition:
        raise BuildError(message)


def is_timestamp(value: Any) -> bool:
    """Return whether ``value`` is an RFC 3339 UTC timestamp such as ``2026-10-16T19:28:44Z``."""
    if not isinstance(value, str):
        return False
    try:
        dt.datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def validate_index(doc: Any, config: Config) -> None:
    """Check that ``doc`` has the shape of index format v1.

    Every version's ``api`` must be listed in ``config.supported_apis``. Raises
    ``BuildError`` describing the first violation found.
    """
    expect(isinstance(doc, dict), "index must be a JSON object")
    expect(doc.get("formatVersion") == FORMAT_VERSION, "formatVersion must be 1")
    expect(isinstance(doc.get("name"), str) and doc["name"] != "", "name must be a string")
    expect(is_timestamp(doc.get("generatedAt")), "generatedAt must be an RFC 3339 UTC timestamp")
    plugins = doc.get("plugins")
    expect(isinstance(plugins, list) and plugins, "plugins must be a non-empty list")
    seen_ids: set[str] = set()
    for plugin in plugins:
        expect(isinstance(plugin, dict), "each plugin must be an object")
        plugin_id = plugin.get("id")
        expect(isinstance(plugin_id, str) and ID_RE.match(plugin_id) is not None,
               f"plugin id {plugin_id!r} is malformed")
        expect(plugin_id not in seen_ids, f"plugin id {plugin_id} appears twice")
        seen_ids.add(plugin_id)
        for key in ("name", "description", "license"):
            expect(isinstance(plugin.get(key), str) and plugin[key] != "",
                   f"{plugin_id}: {key} must be a non-empty string")
        expect(isinstance(plugin.get("homepage"), str), f"{plugin_id}: homepage must be a string")
        provides = plugin.get("provides")
        expect(isinstance(provides, list) and provides and all(
            isinstance(layer, str) and layer for layer in provides),
            f"{plugin_id}: provides must be a non-empty string list")
        expect(plugin.get("official") is True, f"{plugin_id}: official must be true")
        versions = plugin.get("versions")
        expect(isinstance(versions, list) and versions,
               f"{plugin_id}: versions must be a non-empty list")
        seen_versions: set[str] = set()
        for version in versions:
            expect(isinstance(version, dict), f"{plugin_id}: each version must be an object")
            number = version.get("version")
            expect(isinstance(number, str) and semver_key(number) is not None,
                   f"{plugin_id}: version {number!r} is not semantic")
            expect(number not in seen_versions, f"{plugin_id}: version {number} appears twice")
            seen_versions.add(number)
            api = version.get("api")
            expect(api in config.supported_apis, f"{plugin_id} {number}: api {api!r} is not supported")
            url = version.get("url")
            expect(isinstance(url, str) and url.startswith("https://"),
                   f"{plugin_id} {number}: url must be https")
            expect(isinstance(version.get("sha256"), str) and SHA256_RE.match(version["sha256"]),
                   f"{plugin_id} {number}: sha256 must be 64 lowercase hex digits")
            size = version.get("size")
            expect(type(size) is int and size > 0, f"{plugin_id} {number}: size must be a positive integer")
            expect(is_timestamp(version.get("publishedAt")),
                   f"{plugin_id} {number}: publishedAt must be an RFC 3339 UTC timestamp")
            expect(isinstance(version.get("changelog"), str),
                   f"{plugin_id} {number}: changelog must be a string")
            permissions = version.get("permissions")
            expect(isinstance(permissions, dict), f"{plugin_id} {number}: permissions must be an object")
            for key, value in permissions.items():
                expect(key in PERMISSION_KEYS and isinstance(value, dict),
                       f"{plugin_id} {number}: unknown permission block {key!r}")


def main(argv: list[str] | None = None) -> int:
    """Run the builder or the validator and return the process exit status."""
    parser = argparse.ArgumentParser(description="Build or validate the plugin index.")
    parser.add_argument("--out", type=Path, default=ROOT / "site" / "v1" / "index.json",
                        help="where to write the index (default: site/v1/index.json)")
    parser.add_argument("--validate", type=Path, metavar="PATH",
                        help="validate an existing index instead of building one")
    args = parser.parse_args(argv)
    try:
        config = load_config(ROOT / "index.toml")
        if args.validate is not None:
            doc = json.loads(args.validate.read_bytes())
            validate_index(doc, config)
            print(f"ok: {args.validate} is a valid index with {len(doc['plugins'])} plugins")
            return 0
        token = os.environ.get("GITHUB_TOKEN") or None
        entries = load_entries(ROOT / "plugins")
        doc = build_index(config, entries, token)
        validate_index(doc, config)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="ascii")
    except (BuildError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"ok: wrote {args.out} with {len(doc['plugins'])} plugins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
