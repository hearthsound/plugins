# Hearthsound official plugin index

This repository builds the official index of Hearthsound plugins. The index
lists every published version of each official plugin, with the download URL,
SHA-256, size, WIT API, changelog, and permission summary of its `.hsplugin`
archive. Servers read the index to install and update plugins.

## Layout

| Path | Purpose |
|---|---|
| `index.toml` | Index name and the WIT APIs the index accepts. |
| `plugins/<id>.toml` | One entry per plugin: `id`, `repo`, and `tag_pattern`. |
| `scripts/build_index.py` | Builds the index from GitHub releases; `--validate` checks its shape. |
| `.github/workflows/ci.yml` | Builds and validates the index on every pull request. |

## Adding a plugin

1. Add `plugins/<id>.toml` with the plugin id, its `owner/name` repository, and
   a `tag_pattern` containing `{version}` (for example `v{version}`).
2. Publish a GitHub release whose tag matches the pattern and whose assets
   include `<id>-<version>.hsplugin`.
3. Build and validate the index locally (set `GITHUB_TOKEN` to avoid the
   anonymous API rate limit):

   ```sh
   python3 scripts/build_index.py --out site/v1/index.json
   python3 scripts/build_index.py --validate site/v1/index.json
   ```

The builder needs only Python 3.11 or newer.

## Index rules

- Each version comes from a release that is neither a draft nor a pre-release.
- The archive must contain `manifest.json` at its root, and its `id` and
  `version` must match the entry and the release tag.
- Releases whose manifest `api` is not in `supportedApis` are skipped.
- A plugin with no compatible release fails the build, so an official entry
  cannot disappear silently.
- Plugin metadata (name, description, provides, homepage, license) is taken
  from the newest compatible version.

## License

MIT. See `LICENSE`.
