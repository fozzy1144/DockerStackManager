"""Generate ``docs/compose-reference.md`` from :mod:`core.snippets`.

The snippet library is the single source of truth for the example
configurations: the editor's browser and the reference document are two views of
it, so they cannot drift apart. Re-run this after editing the library:

    python tools/gen_compose_docs.py

It rewrites the file in place and prints nothing on success beyond a summary.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import snippets  # noqa: E402  (path set up above)

OUTPUT = ROOT / "docs" / "compose-reference.md"

HEADER = """\
# Compose configuration reference

Example service configurations with notes on what to change and why.

> This file is generated from [`core/snippets.py`](../core/snippets.py) by
> `python tools/gen_compose_docs.py`. Edit the library, not this file — the same
> examples are what the app's Compose editor inserts, so they stay in step.

Every example is available inside the application: open a stack, click **Edit**,
and pick it from the **Examples** panel to insert it at the cursor with the right
indentation.

## How to read the examples

Each one says where it belongs:

| Placement | Meaning |
| --- | --- |
| **service** | A complete service block, indented under `services:`. |
| **fragment** | Keys that go *inside* a service block. |
| **root** | A top-level block, at the same level as `services:`. |
"""


def _anchor(title: str) -> str:
    """GitHub-style anchor for a heading."""
    slug = title.lower()
    kept = [char for char in slug if char.isalnum() or char in " -_"]
    return "".join(kept).strip().replace(" ", "-")


def build() -> str:
    grouped = snippets.by_category()
    lines = [HEADER, "", "## Contents", ""]

    for category, items in grouped.items():
        lines.append(f"- **{category}**")
        for snippet in items:
            lines.append(f"  - [{snippet.title}](#{_anchor(snippet.title)})")
    lines.append("")

    for category, items in grouped.items():
        lines += ["---", "", f"## {category}", ""]
        for snippet in items:
            lines += [
                f"### {snippet.title}",
                "",
                f"*{snippet.summary}*",
                "",
                f"Placement: **{snippet.kind}**",
                "",
                "```yaml",
                snippet.body.rstrip(),
                "```",
                "",
                snippet.details.strip(),
                "",
            ]
            if snippet.docs_url:
                lines += [f"Reference: <{snippet.docs_url}>", ""]

    lines += [
        "---",
        "",
        "## Checks the editor runs",
        "",
        "The editor lints as you type, offline. These are the rules and why each",
        "one is worth knowing:",
        "",
        "| Level | Check | Why it matters |",
        "| --- | --- | --- |",
        "| error | YAML syntax, tab indentation | The file will not load at all. |",
        "| error | Service with neither `image` nor `build` | Nothing to run. |",
        "| error | Named volume or network used but not declared | `up` fails; a "
        "missing `./` turns a bind mount into an undeclared volume. |",
        "| error | `depends_on` naming an unknown service | Refers to service keys, "
        "not container names. |",
        "| error | Two services publishing the same host port | The second "
        "container will not start. |",
        "| error | Duplicate `container_name` | Container names are host-wide. |",
        "| warning | `latest` or untagged image | The next pull can cross a major "
        "version. |",
        "| warning | No restart policy | The service will not return after a reboot. |",
        "| warning | `privileged: true` | Disables container isolation. |",
        "| warning | Literal password/token in `environment` | Visible to anyone who "
        "reads the file or runs `docker inspect`. |",
        "| info | Obsolete `version:` key | Ignored by Compose v2. |",
        "| info | No healthcheck | `depends_on: service_healthy` cannot wait, and "
        "the app cannot report a stack as partially up. |",
        "",
        "After the offline pass, **Validate** (or saving) runs",
        "`docker compose config` on the host itself, which additionally resolves",
        "`.env` interpolation, override files, and anchors — the things only the",
        "real project directory can answer.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    content = build()
    OUTPUT.write_text(content, encoding="utf-8")
    print(
        f"Wrote {OUTPUT.relative_to(ROOT)} — "
        f"{len(snippets.SNIPPETS)} examples in {len(snippets.by_category())} categories, "
        f"{len(content.splitlines())} lines."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
