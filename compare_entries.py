#!/usr/bin/env python3
"""
compare_entries.py
==================

Puts the same books from two or three database files side by side in one
Markdown document, so you can judge which version of an entry is better.

    python3 compare_entries.py --ids 42,117,388 \\
        --labels "published,opus,haiku" \\
        ingest_work/archeo2_current.db \\
        ingest_work/archeo2_new_20260911_103526.db \\
        haiku_test/archeo2_new_20260911_150204.db

If --ids is left out, it compares every entry whose summary differs between
the files. The result is written to comparison_<date>.md unless --out says
otherwise.

Research notes are not stored in the database; they are in each run's
ingest_report_<date>.md, under the "Research notes" heading for each book.
"""

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys


def load(db_path, ids=None):
    con = sqlite3.connect(db_path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    code = "Code" if "Code" in cols else "NULL"
    sql = f'SELECT id, {code}, Title, Author, Publisher, "Publication Year", NumPages, Keywords, Summary FROM books'
    if ids:
        sql += " WHERE id IN (" + ",".join("?" * len(ids)) + ")"
    rows = con.execute(sql, ids or ()).fetchall()
    con.close()
    keys = ("id", "code", "title", "author", "publisher", "year", "pages", "keywords", "summary")
    return {r[0]: dict(zip(keys, r)) for r in rows}


def words(text):
    return len(re.findall(r"\w+", text or ""))


def main():
    ap = argparse.ArgumentParser(description="Compare the same entries across two or three database files.")
    ap.add_argument("databases", nargs="+", help="two or three .db files, oldest first")
    ap.add_argument("--ids", help="comma-separated database ids to compare (default: every entry that differs)")
    ap.add_argument("--labels", help="comma-separated names for the columns, e.g. published,opus,haiku")
    ap.add_argument("--out", help="output Markdown file")
    args = ap.parse_args()

    if not 2 <= len(args.databases) <= 3:
        sys.exit("Give two or three database files.")
    for path in args.databases:
        if not os.path.exists(path):
            sys.exit(f"File not found: {path}")

    labels = (args.labels or "").split(",") if args.labels else []
    while len(labels) < len(args.databases):
        labels.append(os.path.basename(args.databases[len(labels)]))

    ids = [int(x) for x in re.split(r"[,\s]+", args.ids.strip()) if x] if args.ids else None
    versions = [load(p, ids) for p in args.databases]

    if ids is None:
        ids = sorted(i for i in versions[0]
                     if any(i in v and v[i]["summary"] != versions[0][i]["summary"] for v in versions[1:]))
        if not ids:
            print("No differing entries found.")
            return

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out or f"comparison_{stamp}.md"
    lines = ["# Entry comparison", "",
             "| Version | File | Entries |", "|---|---|---|"]
    for label, path, v in zip(labels, args.databases, versions):
        lines.append(f"| {label} | `{path}` | {len(v)} |")
    lines += ["", f"Comparing {len(ids)} book(s): {', '.join(str(i) for i in ids)}", ""]

    for i in ids:
        first = next((v[i] for v in versions if i in v), None)
        if not first:
            continue
        lines += ["---", "", f"## {i}. {first['title']}", ""]
        lines.append("| | " + " | ".join(labels) + " |")
        lines.append("|---|" + "---|" * len(labels))
        for field in ("code", "author", "publisher", "year", "pages"):
            cells = [str(v.get(i, {}).get(field) or "") for v in versions]
            lines.append(f"| {field} | " + " | ".join(c.replace("|", "/") for c in cells) + " |")
        cells = [str(words(v.get(i, {}).get("summary"))) for v in versions]
        lines.append("| words | " + " | ".join(cells) + " |")
        lines.append("")
        for label, v in zip(labels, versions):
            entry = v.get(i)
            if not entry:
                lines += [f"### {label}: (not present)", ""]
                continue
            lines += [f"### {label}", "", f"*Keywords: {entry['keywords']}*", "", entry["summary"] or "(empty)", ""]

    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"Wrote {out}: {len(ids)} book(s) across {len(args.databases)} versions.")


if __name__ == "__main__":
    main()
