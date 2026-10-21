"""Refresh the bundled OpenAPI snapshot (spec.json) from the API's own /openapi.json.

    python -m buildingactivity.cli.specsync                       (live API, rewrites spec.json)
    python -m buildingactivity.cli.specsync --from openapi.json   (a saved copy)
    python -m buildingactivity.cli.specsync --check               (exit 1 when spec.json is stale)

The CLI builds its commands from spec.json at run time, so this file is the only thing
that has to change when the API gains a route or a parameter. The public snapshot is the
/v1 paths and the schemas they use (cli.main.snapshot), with the recorded response
examples dropped (the CLI never reads them) and a fixed public `info` block. A spec that
still carries internal markers after that is refused, so nothing private ships.
"""
import argparse
import json
import re
import sys
from importlib import resources

from buildingactivity.cli import main as cli

DEFAULT_SOURCE = cli.DEFAULT_URL + "/openapi.json"
TITLE = "BuildingActivity API"
DESCRIPTION = (
    "US building permits, properties, contractors, zoning decisions, early project signals "
    "and big projects. Every record carries source_url, the issuer's own record. Auth: send "
    "your key as X-API-Key. Pagination is keyset: pass next_cursor back as cursor with the "
    "same filters. Reference: https://buildingactivity.com/api"
)
# Markers of build machinery that must never reach the public package.
LEAKS = (r"\.db\b", r"scratch", r"/Volumes[/]", r"127\.0\.0\.1", r"localhost",
         r"permit[ _-]?intel(?!-Signature)",
         r"index\.serve", r"\bunit \d+ scratch", r"examples\.db")
_DOC_REF = re.compile(r"\s*See docs/[\w./-]+\.md\.?")
# Build-unit names that slipped into API descriptions (report them to the API; this only
# keeps them out of the package meanwhile).
_UNIT_REF = re.compile(r"\b[Uu]nit \d+ (?=address ID|permit tags)")


def _strip(node):
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip(x) for x in node]
    if isinstance(node, str):
        return _UNIT_REF.sub("", _DOC_REF.sub("", node))
    return node


def _drop_response_examples(paths):
    """Recorded response bodies (success and error) are documentation for the API site;
    the CLI reads only parameters, so they stay out of the package."""
    for ops in paths.values():
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            for resp in (op.get("responses") or {}).values():
                for media in (resp.get("content") or {}).values():
                    media.pop("example", None)
                    media.pop("examples", None)


def public_spec(raw):
    spec = _strip(cli.snapshot(raw))
    _drop_response_examples(spec["paths"])
    spec["info"] = {"title": TITLE, "version": (raw.get("info") or {}).get("version", "1"),
                    "description": DESCRIPTION}
    return spec


# Known exception: the webhook signature header the API sends today is named
# X-PermitIntel-Signature. It is part of the API contract, so the description keeps it until
# the API renames the header (reported to the API owners); every other spelling is refused.


def leaks(spec):
    text = json.dumps(spec)
    return [p for p in LEAKS if re.search(p, text, re.IGNORECASE)]


def text(spec):
    return json.dumps(spec, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def bundled_path():
    return resources.files("buildingactivity.cli").joinpath(cli.SPEC_FILE)


def load_source(source, timeout=30):
    if re.match(r"https?://", source):
        import requests

        r = requests.get(source, timeout=timeout, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    with open(source) as fh:
        return json.load(fh)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m buildingactivity.cli.specsync",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--from", dest="source", default=DEFAULT_SOURCE,
                   help=f"URL or file of the API's /openapi.json (default {DEFAULT_SOURCE})")
    p.add_argument("--check", action="store_true", help="compare only; exit 1 when stale")
    args = p.parse_args(argv)
    spec = public_spec(load_source(args.source))
    bad = leaks(spec)
    if bad:
        print(f"refused: the spec from {args.source} still matches internal markers {bad}; "
              "fix the API's descriptions first", file=sys.stderr)
        return 2
    new, path = text(spec), bundled_path()
    old = path.read_text() if path.is_file() else ""
    if args.check:
        if new != old:
            print(f"spec.json is stale against {args.source}; run "
                  "`python -m buildingactivity.cli.specsync` and commit", file=sys.stderr)
            return 1
        print(f"spec.json matches {args.source} ({len(spec['paths'])} paths)")
        return 0
    with open(str(path), "w") as fh:
        fh.write(new)
    print(f"wrote {path} ({len(spec['paths'])} paths, {'changed' if new != old else 'same'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
