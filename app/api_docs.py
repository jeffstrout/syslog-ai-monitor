"""The shared API reference page, served at ``/api/docs``.

The fleet contract: every appliance serves this page, at this path, in this
shape (jeffstrout/homelab-standards ``docs/style-guide.md``). The header's
``API docs`` link points here rather than at Swagger, because swagger-ui loads
from ``cdn.jsdelivr.net`` and renders an empty shell on a LAN with no route out
— which is exactly when you reach for API docs (homelab-standards#7).

**Generated, never authored.** The content comes from ``app.openapi()``, the
same schema Swagger reads, so this page cannot drift from the routes. Adding an
endpoint with a ``summary=`` and a ``tags=`` is all it takes to appear here
correctly. That is the whole reason the FastAPI appliances render from the
schema while split-flap declares a list: Express has nothing to introspect.

Styling is ``/static/tokens.css`` + ``/static/components.css`` — both vendored
into this repo and served from this process, so the page has no external
dependency of any kind. Do not add one.

This module is duplicated near-verbatim in ``ac-monitor``. It belongs in the
shared ``homelab_appliance`` package once that exists
(jeffstrout/homelab-standards#1); until then the duplication is deliberate and
the two copies should be kept in step.
"""

from __future__ import annotations

import html
import re

# Consequence, not the GET-green/POST-blue convention. A blue POST badge is a
# label, not something you can act on, and blue is reserved for things that are
# (homelab-standards docs/style-guide.md).
_METHOD_MOD = {
    "get": "get",        # safe
    "head": "get",
    "options": "get",
    "post": "post",      # mutates
    "put": "post",
    "patch": "post",
    "delete": "delete",  # destroys
}

# Methods in a stable, meaningful order rather than dict order, so a path that
# supports several reads the same way on every appliance.
_METHOD_ORDER = ["get", "head", "options", "post", "put", "patch", "delete"]


def _first_line(text: str) -> str:
    """First prose line of a docstring — the summary fallback.

    Route docstrings here open with a one-line summary and then go on to code
    fences and examples, none of which belong in a scannable table.
    """
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if line and not line.startswith("```"):
            return line
    return ""


def _describe(op: dict) -> str:
    return op.get("summary") or _first_line(op.get("description", "")) or ""


def _inline_md(text: str) -> str:
    """Escape, then honour the inline Markdown FastAPI descriptions actually use.

    The OpenAPI ``description`` is Markdown — Swagger renders it. Escaping and
    stopping there leaves literal ``**findings**`` on the page, so the two marks
    that appear in practice are converted. Escaping happens FIRST, so nothing in
    the source text can inject markup; the patterns below only ever match the
    asterisks and backticks that survived escaping.
    """
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r'<code class="hl-code">\1</code>', out)
    return out


def render(app, *, version: dict | None = None, back: tuple[str, str] | None = None) -> str:
    """Render the reference page from ``app``'s own OpenAPI schema."""
    schema = app.openapi()
    info = schema.get("info", {})
    title = info.get("title", "API")
    description = info.get("description", "")

    # Tag order and prose come from openapi_tags when declared, so the page
    # groups the way the author intended rather than alphabetically.
    tag_meta = {t["name"]: t.get("description", "") for t in schema.get("tags", [])}
    order = list(tag_meta) or []

    groups: dict[str, list[tuple[str, str, str]]] = {}
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method in _METHOD_ORDER:
            op = methods.get(method)
            if not op:
                continue
            tags = op.get("tags") or ["Endpoints"]
            tag = tags[0]
            groups.setdefault(tag, []).append((method.upper(), path, _describe(op)))

    for tag in groups:
        if tag not in order:
            order.append(tag)

    total = sum(len(v) for v in groups.values())

    sections = []
    for tag in order:
        rows = groups.get(tag)
        if not rows:
            continue
        blurb = tag_meta.get(tag, "")
        body = "".join(
            f'<tr><td><span class="hl-method hl-method--{_METHOD_MOD.get(m.lower(), "get")}">'
            f"{html.escape(m)}</span></td>"
            f'<td><code class="hl-code">{html.escape(p)}</code></td>'
            f"<td>{html.escape(d)}</td></tr>"
            for m, p, d in rows
        )
        sections.append(
            f'<section class="hl-section">'
            f'<h2 class="hl-section-title">{html.escape(tag)}</h2>'
            + (f'<p class="hl-note">{html.escape(blurb)}</p>' if blurb else "")
            + f'<div class="hl-table-wrap"><table class="hl-table"><tbody>{body}</tbody></table></div>'
            f"</section>"
        )

    v = version or {}
    build = " · ".join(x for x in (v.get("commit"), (v.get("built_at") or "")[:10]) if x)
    back_href, back_label = back or ("/", "Dashboard")

    lead = "".join(
        f'<p class="hl-note">{_inline_md(para.strip())}</p>'
        for para in description.split("\n\n")
        if para.strip()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — API</title>
<!-- Vendored and served by this process. No CDN: this page has to render on a
     LAN with no route to anywhere, which is when it is needed most. -->
<link rel="stylesheet" href="/static/tokens.css">
<link rel="stylesheet" href="/static/components.css">
<style>
  body {{
    margin: 0; background: var(--hl-canvas); color: var(--hl-fg);
    font: var(--hl-text-base)/var(--hl-leading) var(--hl-font-sans);
  }}
  .hl-table td {{ vertical-align: top; }}
  .hl-table td:nth-child(3) {{ color: var(--hl-fg-muted); }}
</style>
</head>
<body>
<header class="hl-header">
  <h1 class="hl-header-name">{html.escape(title)} — API</h1>
  <span class="hl-header-spacer"></span>
  <nav class="hl-header-nav">
    <a class="hl-header-link" href="{html.escape(back_href)}">&larr; {html.escape(back_label)}</a>
    <a class="hl-header-link" href="/api/health" target="_blank" rel="noopener">Health</a>
    <a class="hl-header-link" href="/openapi.json" target="_blank" rel="noopener">OpenAPI&nbsp;JSON</a>
    <a class="hl-header-link" href="/docs" target="_blank" rel="noopener">Swagger</a>
  </nav>
</header>
<main class="hl-page">
{lead}
{"".join(sections)}
</main>
<footer class="hl-footer">
  <span class="hl-num">{total}</span>&nbsp;endpoints
  <span class="hl-footer-spacer"></span>
  <span class="hl-footer-meta">{html.escape(build or "dev")}</span>
</footer>
</body>
</html>"""
