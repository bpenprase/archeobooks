#!/usr/bin/env python3
"""
make_labels.py
==============

Turns the library's book spreadsheet into a Word document of shelf labels laid
out for Avery 5160 sheets (30 labels per page, 3 across and 10 down, each
2 5/8 inches by 1 inch).

Each label carries the shelf code and the pyramid icon on the left, in a square
about an inch wide, and the author and title on the right.

    python3 make_labels.py archeobooks_to_add_Fall2026_with_codes.csv
    python3 make_labels.py books.csv --out labels.docx --icon chitzen_itza.jpg
    python3 make_labels.py books.csv --start 7        # leave the first 6 labels blank

The spreadsheet needs a Code column, an Author column and a Title column; any
other columns are ignored. --start is for reusing a part-used sheet: give the
position of the first label you want printed, counting across the rows.

    pip install python-docx pillow
"""

import argparse
import csv
import io
import os
import re
import sys

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

# --- Avery 5160 geometry (inches) ------------------------------------------
LABEL_W, LABEL_H = 2.625, 1.0
COLS, ROWS = 3, 10
GUTTER = 0.125            # horizontal space between labels
MARGIN_TOP = 0.5
MARGIN_SIDE = 0.1875
CODE_COL_W = 1.0          # the square region on the left of each label
TEXT_COL_W = LABEL_W - CODE_COL_W
ICON_H = 0.40             # printed height of the icon

# --- Type sizes -------------------------------------------------------------
CODE_PT = 13
AUTHOR_PT = 7.5
TITLE_PT = 7.5
FONT = "Arial"

TITLE_LIMIT = 58          # characters before a title is shortened
AUTHOR_LIMIT = 30


def first_author(field):
    """The first person named, without co-authors, roles or alternate names."""
    s = (field or "").split(";")[0]
    s = re.split(r"\(", s)[0]
    s = re.split(r",\s*(?:with|and)\b|,\s+|\s+and\s+", s)[0]
    return s.strip(" ,;")


def shorten_title(title):
    """Prefer the main title; add as much of the subtitle as will fit."""
    title = (title or "").strip()
    if len(title) <= TITLE_LIMIT:
        return title
    main = re.split(r":\s", title)[0]
    if len(main) <= TITLE_LIMIT:
        return main
    return main[:TITLE_LIMIT - 1].rstrip() + "\u2026"


def shorten_author(author):
    a = first_author(author)
    return a if len(a) <= AUTHOR_LIMIT else a[:AUTHOR_LIMIT - 1].rstrip() + "\u2026"


def prepare_icon(path):
    """Trim the white border off the icon and return it as PNG bytes."""
    from PIL import Image, ImageOps
    img = Image.open(path).convert("RGB")
    grey = img.convert("L").point(lambda p: 255 if p > 200 else 0)
    box = ImageOps.invert(grey).getbbox()
    if box:
        img = img.crop(box)
    img.thumbnail((600, 600))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def set_cell_margins(cell, top=0, start=0, bottom=0, end=0):
    """Cell padding, in twentieths of a point (1440 = one inch)."""
    tcPr = cell._tc.get_or_add_tcPr()
    mar = OxmlElement("w:tcMar")
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = OxmlElement(f"w:{name}")
        node.set(qn("w:w"), str(int(value)))
        node.set(qn("w:type"), "dxa")
        mar.append(node)
    tcPr.append(mar)


def no_borders(table):
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "none")
        node.set(qn("w:sz"), "0")
        borders.append(node)
    tblPr.append(borders)


def fixed_layout(table):
    """Stop Word resizing columns to fit their contents."""
    tblPr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    cantSplit = OxmlElement("w:cantSplit")
    tblPr.append(cantSplit)


def exact_row_height(row, inches):
    trPr = row._tr.get_or_add_trPr()
    h = OxmlElement("w:trHeight")
    h.set(qn("w:val"), str(int(inches * 1440)))
    h.set(qn("w:hRule"), "exact")
    trPr.append(h)


def tight(paragraph, space_after=0, space_before=0):
    pf = paragraph.paragraph_format
    pf.space_after = Pt(space_after)
    pf.space_before = Pt(space_before)
    pf.line_spacing = 1.0
    return paragraph


def build_label(code_cell, text_cell, entry, icon_bytes):
    """Fill one label: the shelf code and icon in the left cell, the author and
    title in the right cell, both vertically centred in the one-inch row."""
    for cell in (code_cell, text_cell):
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    set_cell_margins(code_cell, top=0, start=86, bottom=0, end=29)   # 0.06 in / 0.02 in
    set_cell_margins(text_cell, top=0, start=0, bottom=0, end=86)

    for cell in (code_cell, text_cell):
        p = tight(cell.paragraphs[0])
        p.paragraph_format.line_spacing = Pt(2)

    if entry is None:
        return

    # Left: the shelf code above the icon.
    p = tight(code_cell.paragraphs[0])
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(entry["code"])
    run.font.name, run.font.size, run.font.bold = FONT, Pt(CODE_PT), True
    p = tight(code_cell.add_paragraph(), space_before=1)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run().add_picture(io.BytesIO(icon_bytes), height=Inches(ICON_H))

    # Right: author, then title.
    p = tight(text_cell.paragraphs[0])
    run = p.add_run(shorten_author(entry["author"]))
    run.font.name, run.font.size, run.font.bold = FONT, Pt(AUTHOR_PT), True
    p = tight(text_cell.add_paragraph(), space_before=2)
    run = p.add_run(shorten_title(entry["title"]))
    run.font.name, run.font.size = FONT, Pt(TITLE_PT)
    run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)


def build_document(entries, icon_bytes, out_path, start=1):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name, style.font.size = FONT, Pt(AUTHOR_PT)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.line_spacing = 1.0

    for section in doc.sections:
        section.page_width, section.page_height = Inches(8.5), Inches(11)
        section.top_margin, section.bottom_margin = Inches(MARGIN_TOP), Inches(0.5)
        section.left_margin = section.right_margin = Inches(MARGIN_SIDE)
        section.header_distance = section.footer_distance = Inches(0)

    # Remove any empty paragraph before the first table, so the labels begin
    # exactly at the top margin.
    for para in list(doc.paragraphs):
        if not para.text.strip():
            para._p.getparent().remove(para._p)

    slots = [None] * (start - 1) + list(entries)
    per_page = COLS * ROWS
    pages = [slots[i:i + per_page] for i in range(0, len(slots), per_page)] or [[]]

    # Column pattern: code, text, gutter, code, text, gutter, code, text
    widths = []
    for c in range(COLS):
        widths += [Inches(CODE_COL_W), Inches(TEXT_COL_W)]
        if c < COLS - 1:
            widths.append(Inches(GUTTER))

    # One continuous table: ten one-inch rows fill a page exactly, so the table
    # carries on at the top margin of the next sheet without a page break.
    table = doc.add_table(rows=ROWS * len(pages), cols=len(widths))
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    no_borders(table)
    fixed_layout(table)
    for i, w in enumerate(widths):
        table.columns[i].width = w

    for n, page in enumerate(pages):
        for r in range(ROWS):
            row = table.rows[n * ROWS + r]
            exact_row_height(row, LABEL_H)
            for i, w in enumerate(widths):
                row.cells[i].width = w
            for c in range(COLS):
                index = r * COLS + c
                entry = page[index] if index < len(page) else None
                build_label(row.cells[c * 3], row.cells[c * 3 + 1], entry, icon_bytes)
                if c < COLS - 1:
                    gcell = row.cells[c * 3 + 2]
                    set_cell_margins(gcell, 0, 0, 0, 0)
                    tight(gcell.paragraphs[0]).paragraph_format.line_spacing = Pt(1)

    doc.save(out_path)


def read_entries(csv_path):
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"No rows found in {csv_path}")
    needed = {"Code", "Author", "Title"}
    missing = needed - set(rows[0].keys())
    if missing:
        sys.exit(f"{csv_path} is missing the column(s): {', '.join(sorted(missing))}")
    return [{"code": (r["Code"] or "").strip(), "author": r["Author"] or "", "title": r["Title"] or ""} for r in rows]


def main():
    ap = argparse.ArgumentParser(description="Make Avery 5160 shelf labels from the book spreadsheet.")
    ap.add_argument("csv", help="spreadsheet with Code, Author and Title columns")
    ap.add_argument("--icon", default="chitzen_itza.jpg", help="image printed under each code")
    ap.add_argument("--out", help="output Word file (default: alongside the spreadsheet)")
    ap.add_argument("--start", type=int, default=1, help="first label position to print on the sheet (for part-used sheets)")
    args = ap.parse_args()

    if not os.path.exists(args.icon):
        sys.exit(f"Icon not found: {args.icon}")
    entries = read_entries(args.csv)
    out = args.out or os.path.splitext(args.csv)[0] + "_labels.docx"
    build_document(entries, prepare_icon(args.icon), out, start=args.start)
    sheets = -(-(len(entries) + args.start - 1) // (COLS * ROWS))
    print(f"Wrote {out}: {len(entries)} labels on {sheets} sheet(s) of Avery 5160.")


if __name__ == "__main__":
    main()
