from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def md_to_html_lines(md: str) -> str:
    out = []
    in_table = False

    def close_table():
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for raw in md.splitlines():
        line = raw.rstrip()

        if not line.strip():
            close_table()
            out.append("<div class='blank'></div>")
            continue

        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells):
                continue
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
            continue

        close_table()

        if line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.startswith("### "):
            out.append(f"<h3>{html.escape(line[4:].strip())}</h3>")
        elif line.strip() == "---":
            out.append("<hr>")
        else:
            escaped = html.escape(line)
            escaped = re.sub(
                r"\b(OR|AND|CHECKLIST|OPTIONS|EXAMPLE_SET|IS_TRUE|linked shared definition)\b",
                r"<span class='op'>\1</span>",
                escaped,
            )
            out.append(f"<div class='tree-line'>{escaped}</div>")

    close_table()
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", default="M083 Full Rule Hierarchy Tree")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    md = input_path.read_text(encoding="utf-8")
    body = md_to_html_lines(md)

    html_doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(args.title)}</title>
<style>
@page {{
  size: A4;
  margin: 9mm 7mm 10mm 7mm;
}}

body {{
  font-family: Arial, sans-serif;
  font-size: 8.2px;
  line-height: 1.24;
  color: #111;
}}

h1 {{
  font-size: 17px;
  margin: 0 0 8px 0;
  padding-bottom: 5px;
  border-bottom: 2px solid #222;
  page-break-after: avoid;
}}

h2 {{
  font-size: 13px;
  margin: 12px 0 5px 0;
  padding: 4px 6px;
  background: #eef2f7;
  border-left: 4px solid #1f5f99;
  page-break-after: avoid;
}}

h3 {{
  font-size: 11px;
  margin: 8px 0 4px 0;
  color: #1f4e79;
  page-break-after: avoid;
}}

hr {{
  border: none;
  border-top: 1px solid #999;
  margin: 12px 0;
}}

.tree-line {{
  font-family: Menlo, Monaco, Consolas, "Courier New", monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 0.7px 0;
}}

.op {{
  font-weight: 800;
  color: #5b2aa0;
}}

.blank {{
  height: 4px;
}}

table {{
  border-collapse: collapse;
  width: 100%;
  margin: 6px 0 9px 0;
  page-break-inside: avoid;
}}

td {{
  border: 1px solid #bbb;
  padding: 2px 3px;
  vertical-align: top;
  font-size: 7px;
  overflow-wrap: anywhere;
}}
</style>
</head>
<body>
<h1>{html.escape(args.title)}</h1>
{body}
</body>
</html>
"""

    html_path = output_path.with_suffix(".html")
    html_path.write_text(html_doc, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1240, "height": 1754})
        page.set_content(html_doc, wait_until="load")
        page.pdf(
            path=str(output_path),
            format="A4",
            print_background=True,
            margin={
                "top": "8mm",
                "right": "6mm",
                "bottom": "8mm",
                "left": "6mm",
            },
        )
        browser.close()

    print(f"Wrote PDF: {output_path}")
    print(f"Wrote HTML preview: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
