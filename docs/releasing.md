# Releasing

Releases go to PyPI as `buildingactivity` through trusted publishing (GitHub's OIDC
token; no PyPI token is stored anywhere), and the hosted MCP image goes to GHCR.

## One-time setup (owner)

Steps 1, 2 and 4 were done on 2026-11-22 (owner decision of that date, which replaces the
2026-10-28 decision not to publish): the repository is public, the PyPI account
`recursivecapital` holds the publisher, and the `pypi` environment requires an owner review.

1. Make the repository public: GitHub > Recursive-Capital-Inc/buildingactivity-python >
   Settings > General > Danger Zone > Change visibility > Public. Check first that the
   history holds only this package.
2. PyPI: sign in at pypi.org (an account with 2FA, ideally an organization account for
   Recursive Capital), then Your account > Publishing > Add a new pending publisher >
   GitHub, with: PyPI project name `buildingactivity`, owner `Recursive-Capital-Inc`,
   repository `buildingactivity-python`, workflow `release.yml`, environment `pypi`.
   The project is created by the first successful upload.
3. Optional dry run: the same at test.pypi.org with environment `testpypi`.
4. GitHub environments: Settings > Environments > New environment `pypi` (and
   `testpypi`); on `pypi` add yourself as a required reviewer so every publish waits for
   one click.
5. After the first image push, GHCR package `buildingactivity-mcp`: Package settings >
   Change visibility (public, or keep private and give the production host a read token).

## Each release

1. Bump `version` in `pyproject.toml`, merge.
2. GitHub > Releases > Draft a new release, tag `v<version>` on main, publish.
3. `release.yml` checks the tag against the version, builds, `twine check`s, waits for the
   `pypi` environment approval, publishes, and pushes the image.
4. `pipx install buildingactivity==<version>` from a clean machine and run
   `buildingactivity --version`.
