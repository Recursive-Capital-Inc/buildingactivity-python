# Releasing

This package is **not published to PyPI** (owner decision, 2026-10-28). `pyproject.toml`
carries the `Private :: Do Not Upload` classifier, which PyPI refuses on upload, so an
accidental `twine upload` fails. Do not remove it or add a PyPI publish job without a new
owner decision.

What a release does: `release.yml` runs when a GitHub release is published, checks the tag
against the version, builds the wheel and sdist (kept as a workflow artifact), and pushes
the hosted MCP image to GHCR as
`ghcr.io/recursive-capital-inc/buildingactivity-mcp:<version>` and `:latest`.

Production does not pull that image today: the prod droplet builds the image from
`/srv/mcp/src` (see [hosted.md](hosted.md)).

## Each release

1. Bump `version` in `pyproject.toml`, merge.
2. GitHub > Releases > Draft a new release, tag `v<version>` on main, publish.
3. `release.yml` checks the tag against the version, builds, `twine check`s and pushes the
   image.
