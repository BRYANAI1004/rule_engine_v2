from pathlib import Path
import argparse
from playwright.sync_api import sync_playwright

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--title", default="MCG Capture Preview")
    args = p.parse_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(input_path)

    html = input_path.read_text(encoding="utf-8", errors="replace")

    wrapper = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{args.title}</title>
<style>
  body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    line-height: 1.35;
    margin: 24px;
  }}
  h1, h2, h3 {{
    page-break-after: avoid;
  }}
  a {{
    color: #0b5394;
  }}
  .pdf-title {{
    font-size: 20px;
    font-weight: 700;
    border-bottom: 2px solid #222;
    margin-bottom: 16px;
    padding-bottom: 8px;
  }}
</style>
</head>
<body>
<div class="pdf-title">{args.title}</div>
{html}
</body>
</html>
"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1800})
        page.set_content(wrapper, wait_until="load")
        page.pdf(
            path=str(output_path),
            format="Letter",
            print_background=True,
            margin={"top": "0.4in", "right": "0.35in", "bottom": "0.4in", "left": "0.35in"},
        )
        browser.close()

    print(f"Wrote PDF: {output_path}")

if __name__ == "__main__":
    main()
