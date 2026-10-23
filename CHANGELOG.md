# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The official plugin index builder (`scripts/build_index.py`) and the entries
  for the Deezer, MusicBrainz, and slskd plugins.
- Signed publishing: pushes to `main` and the daily schedule build the index,
  sign it with the official minisign key, verify the signature against
  `public.minisign`, and publish `v1/index.json` and `v1/index.json.minisig`
  through GitHub Pages.

### Changed

- The index accepts releases built for the `hearthsound:plugin@0.3.0` API next
  to those built for `hearthsound:plugin@0.2.0`, so servers on either API find
  compatible versions.
