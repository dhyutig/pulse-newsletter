"""
newsletter_generator.py

Automates the PULSE Institute newsletter end-to-end. This script does NOT
talk to Google Drive directly - a separate `rclone copy` step (run right
before this script, see automate.sh / the GitHub Actions workflow) syncs
the shared Drive folder down to a local ./raw_files directory. This script
just reads whatever's in there.

  1. INGEST   - read files from ./raw_files (synced from Drive by rclone),
                skipping anything already processed in a previous run
  2. EXTRACT  - ask Claude to pull structured fields out of each item
  3. GROUP    - primary grouping by career stage (Postdocs, Grad Students,
                PIs & Faculty, Multiple/General), secondary by type
                (Conferences & Workshops, Academic Positions, Prizes &
                Awards, Funding Opportunities)
  4. RENDER   - build a categorized HTML digest (Attoworld-style cards)
  5. STATE    - record what was processed so next run only sees new items

Usage:
  export ANTHROPIC_API_KEY=sk-...
  rclone copy gdrive:Newsletter ./raw_files --drive-shared-with-me
  python newsletter_generator.py --input ./raw_files --out ./draft_newsletter.html

First run processes everything currently in ./raw_files. Every run after
that only processes files not seen in a previous run (tracked by filename
in ./last_run.json, alongside this script). Use --force-full to reprocess
everything regardless of state.

Requirements:
  pip install anthropic extract_msg --break-system-packages

Notes on extending this:
  - PDF text extraction isn't wired up yet (add pdfplumber if PDFs start
    showing up in the folder).
  - Dedup is intentionally left out of v1 - if the same announcement gets
    dropped in twice, it'll appear twice. Worth revisiting once real
    volume shows how often that happens.
  - Google Docs synced by rclone come down as .docx (or similar export
    format) rather than plain text - the reader below handles .docx via
    a plain zip/XML extraction so no extra dependency is needed. If other
    formats start showing up in the folder, extend load_raw_file().
"""

import argparse
import email
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from email import policy
from pathlib import Path

import anthropic

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "last_run.json"

AUDIENCES = ["Grad Students", "Postdocs", "PIs & Faculty", "Multiple / General"]
CATEGORIES = ["Conferences & Workshops", "Academic Positions", "Prizes & Awards", "Funding Opportunities", "Other"]

EXTRACTION_PROMPT = """You are helping build a newsletter for the Stanford
PULSE Institute, in the style of the Attoworld "Career Opportunities" digest.

Read the raw item below (a forwarded email, doc, or notification) and
extract it into JSON with these exact fields:

{{
  "title": "short descriptive title",
  "audience": "one of: {audiences}  (who this is actually relevant to - if it applies to more than one, or you're unsure, use 'Multiple / General')",
  "category": "one of: {categories}",
  "description": "2-3 sentence plain-language summary",
  "organization": "sponsoring org/institution, or null",
  "deadline": "YYYY-MM-DD if stated, else null",
  "location": "city/country, or null",
  "funding": "funding/award details if stated, else null",
  "links": ["most relevant URL first, additional URLs after"]
}}

If the item is not a genuine opportunity/announcement (e.g. it's spam, a
calendar invite, or unrelated admin email), set "title" to "SKIP".

Only return the JSON object, nothing else.

RAW ITEM:
---
{content}
---
"""


# ---------------------------------------------------------------------------
# State tracking (so each run only processes what's new)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None, "processed_files": []}


def save_state(processed_files: list[str]) -> None:
    state = load_state()
    all_files = list(set(state.get("processed_files", []) + processed_files))
    STATE_FILE.write_text(json.dumps({
        "last_run": datetime.now(timezone.utc).isoformat(),
        "processed_files": all_files,
    }, indent=2))


# ---------------------------------------------------------------------------
# Local file reading (rclone has already synced Drive -> ./raw_files)
# ---------------------------------------------------------------------------

def extract_text_from_eml(path: Path) -> str:
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    parts = [f"Subject: {msg.get('subject', '')}", f"From: {msg.get('from', '')}"]
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            parts.append(part.get_content())
            break
    return "\n".join(parts)


def extract_text_from_msg(path: Path) -> str:
    import extract_msg  # only needed for .msg files

    m = extract_msg.Message(str(path))
    return f"Subject: {m.subject}\nFrom: {m.sender}\n\n{m.body}"


def extract_text_from_docx(path: Path) -> str:
    """Minimal .docx text extraction (no extra dependency) - docx files
    are just zip archives containing XML."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    text = re.sub(r"<[^>]+>", " ", xml)
    return re.sub(r"\s+", " ", text).strip()


def load_raw_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".eml":
        return extract_text_from_eml(path)
    elif suffix == ".msg":
        return extract_text_from_msg(path)
    elif suffix == ".docx":
        return extract_text_from_docx(path)
    else:
        return path.read_text(errors="ignore")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_fields(client: anthropic.Anthropic, raw_text: str) -> dict | None:
    prompt = EXTRACTION_PROMPT.format(
        audiences=", ".join(AUDIENCES), categories=", ".join(CATEGORIES), content=raw_text[:6000]
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        fields = json.loads(text)
    except json.JSONDecodeError:
        return None
    if fields.get("title") == "SKIP":
        return None
    return fields


# ---------------------------------------------------------------------------
# Rendering (Attoworld-style categorized HTML)
# ---------------------------------------------------------------------------

ACCENT = "#F7004B"

CARD_TEMPLATE = """
<div style="border:solid #E5E5E5 1px;border-left:solid {accent} 3px;padding:24px;margin-bottom:24px;border-radius:0 6px 6px 0;">
  <h4 style="margin-top:0;background:#FAFAFA;">{title}</h4>
  <p style="margin-bottom:9pt;background:#FAFAFA;">{description}</p>
  <div style="margin-top:11pt;margin-bottom:11pt;">
    {meta_rows}
  </div>
  <div style="margin-top:13pt;">{link_buttons}</div>
</div>
"""

META_ROW = '<div style="border:solid #E5E5E5 1px;padding:5pt 11pt;border-radius:4px;"><p style="background:white;"><b>{label}:</b> {value}</p></div>'
LINK_BUTTON = '<p style="background:{accent};"><a href="{url}">{label}</a></p>'


def render_item(item: dict) -> str:
    meta_rows = ""
    for label, key in [("Organization", "organization"), ("Deadline", "deadline"),
                        ("Location", "location"), ("Funding", "funding")]:
        if item.get(key):
            meta_rows += META_ROW.format(label=label, value=item[key])

    link_buttons = ""
    for i, url in enumerate(item.get("links") or []):
        label = "Learn more" if i == 0 else f"Additional link{'' if i == 1 else ' ' + str(i)}"
        link_buttons += LINK_BUTTON.format(accent=ACCENT, url=url, label=label)
    if not link_buttons:
        link_buttons = "<p><i>No direct link available.</i></p>"

    return CARD_TEMPLATE.format(
        accent=ACCENT, title=item.get("title", "Untitled"),
        description=item.get("description", ""), meta_rows=meta_rows, link_buttons=link_buttons,
    )


def render_html(items: list[dict], newsletter_title: str = "PULSE Newsletter") -> str:
    if not items:
        return f"<html><body><h1>{newsletter_title}</h1><p>No new opportunities this period.</p></body></html>"

    by_audience: dict[str, list[dict]] = {}
    for item in items:
        by_audience.setdefault(item.get("audience", "Multiple / General"), []).append(item)

    summary_line = " • ".join(f"{aud}: {len(entries)}" for aud, entries in by_audience.items())
    total = len(items)

    body = f"""
    <div style="border:solid #E5E5E5 1px;padding:15pt 19pt;margin-bottom:22.5pt;border-radius:8px;">
      <p style="background:#F9F9F9;">This digest contains <b>{total} opportunities</b> across {len(by_audience)} sections.</p>
      <p style="background:#F9F9F9;">{summary_line}</p>
    </div>
    """

    for audience, entries in by_audience.items():
        by_category: dict[str, list[dict]] = {}
        for e in entries:
            by_category.setdefault(e.get("category", "Other"), []).append(e)

        body += f"""
        <div style="border:solid #E5E5E5 1px;padding:23pt;margin-bottom:22.5pt;border-radius:8px;">
          <div style="border-bottom:solid {ACCENT} 2.25pt;padding-bottom:9pt;">
            <h2 style="background:white;">{audience} ({len(entries)} opportunities)</h2>
          </div>
        """
        for category, cat_items in by_category.items():
            body += f'<div style="margin-bottom:26.25pt;"><h3 style="background:white;">{category}</h3>'
            for item in cat_items:
                body += render_item(item)
            body += "</div>"
        body += "</div>"

    footer = f"""
    <div style="border-top:solid #E5E5E5 1px;padding-top:19pt;margin-top:37.5pt;">
      <p style="text-align:center;">This digest contains {total} opportunities curated for PULSE researchers.</p>
      <p style="text-align:center;">Generated automatically on {datetime.now().date().isoformat()}</p>
    </div>
    """

    return f"<html><body><h1>{newsletter_title}</h1>{body}{footer}</body></html>"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="./raw_files", help="Local folder synced from Drive by rclone")
    parser.add_argument("--out", default="draft_newsletter.html")
    parser.add_argument("--title", default="PULSE Newsletter")
    parser.add_argument("--force-full", action="store_true",
                         help="Ignore state and reprocess every file currently in the folder")
    args = parser.parse_args()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    input_dir = Path(args.input)

    state = load_state()
    already_processed = set() if args.force_full else set(state.get("processed_files", []))

    all_files = sorted(p for p in input_dir.glob("*") if p.is_file())
    new_files = [p for p in all_files if p.name not in already_processed]
    print(f"Found {len(new_files)} new file(s) out of {len(all_files)} total in {input_dir}.")

    items = []
    processed_files = []
    for path in new_files:
        print(f"Processing {path.name}...")
        try:
            raw_text = load_raw_file(path)
            fields = extract_fields(client, raw_text)
            if fields:
                items.append(fields)
            processed_files.append(path.name)
        except Exception as e:
            print(f"  Skipped ({e})")

    draft = render_html(items, newsletter_title=args.title)
    Path(args.out).write_text(draft)
    save_state(processed_files)
    print(f"\nDraft written to {args.out} ({len(items)} items included).")


if __name__ == "__main__":
    main()
