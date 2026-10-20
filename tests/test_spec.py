"""The bundled OpenAPI snapshot and the package contents.

spec.json must be exactly what specsync renders (so the CLI stays generated from the API),
carry nothing private, and the package must hold only the two thin HTTP clients.
Set BUILDINGACTIVITY_SPEC_SOURCE to a URL or file of the API's /openapi.json to also check
that spec.json is current against it (the scheduled spec-drift workflow does this).
"""
import json
import os
import pathlib
import re

import pytest

from buildingactivity.cli import main as cli
from buildingactivity.cli import specsync

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "buildingactivity"


def test_bundled_spec_is_a_specsync_render():
    have = specsync.bundled_path().read_text()
    assert specsync.text(specsync.public_spec(json.loads(have))) == have, (
        "spec.json was edited by hand; run python -m buildingactivity.cli.specsync")


def test_bundled_spec_is_public():
    spec = cli.load_spec()
    assert spec["info"]["title"] == specsync.TITLE
    assert specsync.leaks(spec) == []
    for ops in spec["paths"].values():
        for op in ops.values():
            for resp in (op.get("responses") or {}).values():
                for media in (resp.get("content") or {}).values():
                    assert "example" not in media and "examples" not in media
    assert all(p.startswith("/v1/") for p in spec["paths"])


@pytest.mark.skipif(not os.environ.get("BUILDINGACTIVITY_SPEC_SOURCE"),
                    reason="set BUILDINGACTIVITY_SPEC_SOURCE to check against the live API")
def test_bundled_spec_matches_the_api():
    source = os.environ["BUILDINGACTIVITY_SPEC_SOURCE"]
    want = specsync.text(specsync.public_spec(specsync.load_source(source)))
    assert specsync.bundled_path().read_text() == want, (
        f"spec.json is stale against {source}; run "
        f"python -m buildingactivity.cli.specsync --from {source}")


def test_specsync_refuses_internal_markers(tmp_path, monkeypatch):
    raw = json.loads(specsync.bundled_path().read_text())
    raw["paths"]["/v1/usage"]["get"]["description"] = "built from scratch index.serve.db"
    src = tmp_path / "openapi.json"
    src.write_text(json.dumps(raw))
    monkeypatch.setattr(specsync, "bundled_path", lambda: tmp_path / "spec.json")
    assert specsync.main(["--from", str(src)]) == 2
    assert not (tmp_path / "spec.json").exists()


# ---- package contents: only the CLI, the MCP server and the spec -------------------------

ALLOWED = {"__init__.py", "cli/__init__.py", "cli/__main__.py", "cli/main.py",
           "cli/specsync.py", "cli/spec.json", "mcp/__init__.py", "mcp/__main__.py",
           "mcp/server.py", "mcp/tools.py"}
# Words that would mean crawler, connector or storage code slipped into the client.
FORBIDDEN = (r"\bimport sqlite3\b", r"\bpsycopg\b", r"\bconnectors?\.", r"\bplaywright\b",
             r"\bselenium\b", r"\bbeautifulsoup|\bbs4\b", r"permit_intel", r"/Volumes/",
             r"\bscrap(e|er|ing)\b")


def test_package_holds_only_the_two_clients():
    files = {p.relative_to(PKG).as_posix() for p in PKG.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts}
    assert files == ALLOWED


def test_no_crawler_or_storage_code():
    for rel in sorted(ALLOWED - {"cli/spec.json"}):
        text = (PKG / rel).read_text()
        for pat in FORBIDDEN:
            assert not re.search(pat, text, re.IGNORECASE), (rel, pat)

