#!/usr/bin/env python3
"""
archeobooks_ingest.py
=====================

Adds new books to the archaeoastronomy library database (archeo2.db) from a
folder of photographs of book covers and title/copyright pages.

What it does, in order:

  1. Downloads the current archeo2.db from the GitHub site (or uses a local copy).
  2. Reads every photo in the folder you give it and asks Claude (vision) to
     read the title, author, publisher, year and ISBN off the page.
  3. Merges photos that show the same book (e.g. a cover shot plus a copyright
     page shot) into one record, and writes candidates.json so you can check it.
  4. Checks each candidate against the existing database to catch duplicates.
  5. For each new book, asks Claude (with web search switched on) to verify the
     bibliographic details and write a summary in the house style, with strict
     instructions against invented quotes, page numbers and chapter structures.
  6. Writes a NEW database file (the original is never modified) plus a
     Markdown report of what was added, replaced, or skipped and why.
  7. Optionally (phase 2) uploads the new database to GitHub.

Typical use
-----------
    python archeobooks_ingest.py --images ./new_books
    python archeobooks_ingest.py --images ./new_books --dry-run      # no API calls
    python archeobooks_ingest.py --images ./new_books --replace      # overwrite duplicates
    python archeobooks_ingest.py --refresh 167,878                   # regenerate old entries
    python archeobooks_ingest.py --refresh suspect                   # regenerate the entries that look broken
    python archeobooks_ingest.py --dedupe --refresh all --workers 4  # clean the whole database
    python archeobooks_ingest.py --resume                            # carry on an interrupted run
    python archeobooks_ingest.py --manifest candidates.json          # re-use edited candidates
    python archeobooks_ingest.py --images ./new_books --upload       # phase 2

Long runs write each finished entry into the new database immediately and
keep a checkpoint, so you can stop with Ctrl-C at any time and pick up where
you left off with --resume.

Requirements
------------
    pip install anthropic pillow
    (optional, for iPhone .heic photos)  pip install pillow-heif

    Set your API key as an environment variable rather than typing it into
    the script:
        macOS / Linux:   export ANTHROPIC_API_KEY="sk-ant-..."
        Windows (cmd):   set ANTHROPIC_API_KEY=sk-ant-...
"""

import argparse
import base64
import datetime as dt
import difflib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Settings you may want to change
# ---------------------------------------------------------------------------

DB_URL = "https://raw.githubusercontent.com/bpenprase/archeobooks/main/archeo2.db"
GITHUB_OWNER = "bpenprase"
GITHUB_REPO = "archeobooks"
GITHUB_BRANCH = "main"
GITHUB_DB_PATH = "archeo2.db"

EXTRACT_MODEL = "claude-sonnet-5"    # reads the photographs (cheap and accurate)
RESEARCH_MODEL = "claude-sonnet-5"   # searches the web and gathers verified notes (most of the cost)
SUMMARY_MODEL = "claude-opus-5"      # writes the entry from those notes (cheap: little text in, a page out)

# Published rates, dollars per million tokens, for the cost estimate only.
RATES = {"claude-fable-5-1": (10, 50), "claude-opus-5": (5, 25), "claude-sonnet-5": (3, 15),
         "claude-haiku-4-5": (1, 5)}
# Rough token sizes of one entry, measured on typical runs.
RESEARCH_IN, RESEARCH_OUT = 80_000, 1_500    # search results accumulate across turns
COMPOSE_IN, COMPOSE_OUT = 2_500, 1_300
SEARCH_COST = 0.01                           # per web search
MAX_WEB_SEARCHES = 6                # web searches allowed per book summary
IMAGE_MAX_EDGE = 1600               # photos are shrunk to this many pixels before upload

TITLE_MATCH_THRESHOLD = 0.88        # how similar two titles must be to count as the same book
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}

# The library's subject focus, used to steer keywords and summary emphasis.
LIBRARY_FOCUS = (
    "an archaeoastronomy and cultural astronomy library used by undergraduate "
    "astronomy students, covering the astronomy, cosmology, sky lore, calendars, "
    "monuments, art and worldviews of Indigenous and ancient cultures, as well as "
    "the history and philosophy of astronomy and science"
)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def log(msg=""):
    print(msg, flush=True)


def normalize_title(title, keep_subtitle=False):
    """Lower-case, strip punctuation and leading articles, and (unless asked to
    keep it) drop the subtitle after a colon or dash, so that 'The Stars Above
    Us: Or the Conquest...' and 'Stars Above Us' compare as the same book."""
    t = (title or "").lower()
    if not keep_subtitle:
        t = re.split(r"[:\u2013\u2014]| - ", t)[0]
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def normalize_isbn(isbn):
    return re.sub(r"[^0-9Xx]", "", isbn or "").upper()


def titles_match(a, b, threshold=TITLE_MATCH_THRESHOLD):
    """True if two titles look like the same book. Compares the short form
    (main title only) and the long form (with subtitle) of each, and also
    accepts a short title that is the opening of the other's long title, as
    happens when a cover shows 'Star Trails' and the title page shows
    'Star Trails Navajo: A Different Way to Look at the Night Sky'."""
    forms_a = {normalize_title(a), normalize_title(a, keep_subtitle=True)} - {""}
    forms_b = {normalize_title(b), normalize_title(b, keep_subtitle=True)} - {""}
    if not forms_a or not forms_b:
        return False
    for fa in forms_a:
        for fb in forms_b:
            if fa == fb or difflib.SequenceMatcher(None, fa, fb).ratio() >= threshold:
                return True
            short, long_ = (fa, fb) if len(fa) <= len(fb) else (fb, fa)
            if len(short) >= 10 and (long_ == short or long_.startswith(short + " ")):
                return True
    return False


def first_nonempty(*values):
    for v in values:
        if v not in (None, "", [], "unknown", "Unknown"):
            return v
    return ""


def parse_json_from_text(text):
    """Claude usually returns clean JSON, but this tolerates code fences and
    surrounding prose by grabbing the outermost {...} block."""
    if not text:
        raise ValueError("empty response")
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in response")
    return json.loads(cleaned[start:end + 1])


CITE_TAG = re.compile(r"[<(]/?\s*cite\b[^>)]*[>)]|</?\s*cite\s*>", re.IGNORECASE)


def scrub(text):
    """Remove any citation markup that leaked into prose, and tidy the spaces."""
    text = CITE_TAG.sub("", text or "")
    text = re.sub(r"[ \t]+([.,;:)])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def scrub_database(db_path):
    """Clean citation markup out of every summary in a database file, in place."""
    con = sqlite3.connect(db_path)
    n = 0
    for row_id, summary in con.execute("SELECT id, Summary FROM books").fetchall():
        if summary and CITE_TAG.search(summary):
            con.execute("UPDATE books SET Summary=? WHERE id=?", (scrub(summary), row_id))
            n += 1
    con.commit()
    con.close()
    return n


# ---------------------------------------------------------------------------
# Shelf codes: three letters of the first author's surname, plus a sequence
# number, e.g. FRA.001. Once a code is assigned to a row it never changes,
# because it is printed on a label and stuck to the physical book.
# ---------------------------------------------------------------------------

NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "ph.d.", "md"}
SURNAME_PARTICLES = {"st", "st.", "van", "von", "de", "del", "della", "la", "le", "da", "di", "mac", "mc"}
CORPORATE_CODES = {"artscience museum": "ART"}


def strip_accents(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def first_author(field):
    """The first person named, without co-authors, roles or alternate names."""
    s = (field or "").split(";")[0]
    s = re.split(r"\(", s)[0]
    s = re.split(r",\s*(?:with|and)\b|,\s+|\s+and\s+", s)[0]
    return s.strip(" ,;")


def author_prefix(author_field):
    """The three-letter part of the shelf code."""
    name = first_author(author_field)
    for corp, code in CORPORATE_CODES.items():
        if name.lower().startswith(corp):
            return code
    words = [w for w in strip_accents(name).split() if w]
    while words and words[-1].lower().strip(".,") in NAME_SUFFIXES:
        words.pop()
    if not words:
        return "ZZZ"
    last = words[-1]
    if len(words) >= 2 and words[-2].lower() in SURNAME_PARTICLES:
        last = words[-2] + words[-1]
    letters = re.sub(r"[^A-Za-z]", "", last).upper()
    if len(letters) < 3:   # initials or a one-letter surname: use the whole name
        letters = (letters + re.sub(r"[^A-Za-z]", "", strip_accents(name)).upper())
    return (letters[:3] or "ZZZ")


def next_code(prefix, taken):
    """The first free PREFIX.nnn, given the set of codes already in use."""
    n = 1
    while f"{prefix}.{n:03d}" in taken:
        n += 1
    return f"{prefix}.{n:03d}"


def has_code_column(db_path):
    con = sqlite3.connect(db_path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    con.close()
    return "Code" in cols


def ensure_code_column(db_path):
    """Rebuild the table with Code as the first field after id, preserving
    every row and its id. Does nothing if the column is already there."""
    if has_code_column(db_path):
        return False
    con = sqlite3.connect(db_path)
    con.executescript('''
        CREATE TABLE books_new (
            id INTEGER PRIMARY KEY,
            Code TEXT,
            Title TEXT,
            Author TEXT,
            Publisher TEXT,
            "Publication Year" TEXT,
            Keywords TEXT,
            NumPages TEXT,
            Summary TEXT
        );
        INSERT INTO books_new (id, Code, Title, Author, Publisher, "Publication Year", Keywords, NumPages, Summary)
            SELECT id, NULL, Title, Author, Publisher, "Publication Year", Keywords, NumPages, Summary FROM books;
        DROP TABLE books;
        ALTER TABLE books_new RENAME TO books;
    ''')
    con.commit()
    con.close()
    return True


def assign_codes(db_path):
    """Give a shelf code to every row that lacks one, in id order, keeping any
    code already assigned. Returns (number assigned, number already present)."""
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT id, Code, Author FROM books ORDER BY id").fetchall()
    taken = {r[1] for r in rows if r[1]}
    assigned = 0
    for row_id, code, author in rows:
        if code:
            continue
        new = next_code(author_prefix(author), taken)
        taken.add(new)
        con.execute("UPDATE books SET Code=? WHERE id=?", (new, row_id))
        assigned += 1
    con.commit()
    con.close()
    return assigned, len(rows) - assigned


def codes_in_use(db_path):
    con = sqlite3.connect(db_path)
    codes = {r[0] for r in con.execute("SELECT Code FROM books WHERE Code IS NOT NULL")}
    con.close()
    return codes


# ---------------------------------------------------------------------------
# Step 1: get the current database
# ---------------------------------------------------------------------------


def fetch_database(local_db, work_dir):
    """Return a path to a copy of the current database. Downloads from GitHub
    unless the user supplied a local file."""
    if local_db and local_db.strip().lower() == "latest":
        import glob
        files = sorted(glob.glob(os.path.join(work_dir, "archeo2_new_*.db")), key=os.path.getmtime)
        if not files:
            sys.exit(f"--db latest: no archeo2_new_*.db found in {work_dir}")
        local_db = files[-1]
    if local_db:
        if not os.path.exists(local_db):
            sys.exit(f"Database file not found: {local_db}")
        log(f"Using local database: {local_db}")
        return local_db

    target = os.path.join(work_dir, "archeo2_current.db")
    log(f"Downloading current database from GitHub ...")
    req = urllib.request.Request(DB_URL, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    log(f"  saved to {target} ({os.path.getsize(target)/1e6:.1f} MB)")
    return target


def load_existing_books(db_path):
    con = sqlite3.connect(db_path)
    code_col = "Code" if has_code_column(db_path) else "NULL"
    rows = con.execute(f'SELECT id, {code_col}, Title, Author, Publisher, "Publication Year", NumPages, Summary FROM books').fetchall()
    con.close()
    return [
        {"id": r[0], "code": r[1] or "", "title": r[2] or "", "author": r[3] or "", "publisher": r[4] or "",
         "year": r[5] or "", "pages": r[6] or "", "summary_len": len(r[7] or "")}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Step 2: read the photographs
# ---------------------------------------------------------------------------


def list_images(folder):
    if not os.path.isdir(folder):
        sys.exit(f"Image folder not found: {folder}")
    files = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS and not f.startswith(".")
    )
    if not files:
        sys.exit(f"No images found in {folder} (looked for {', '.join(sorted(IMAGE_EXTENSIONS))})")
    return files


def prepare_image(path):
    """Open the photo, fix phone-camera rotation, shrink it, and return JPEG bytes."""
    from PIL import Image, ImageOps
    if path.lower().endswith((".heic", ".heif")):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise RuntimeError("HEIC photo found; run  pip install pillow-heif  or convert it to JPG")
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


EXTRACT_PROMPT = """You are cataloguing books for a university library. This photograph shows a page or cover of a physical book (it may be the front cover, the title page, or the copyright/imprint page; it may be rotated or partly shadowed).

Read the text carefully and return ONLY a JSON object with these keys:
  "page_type": one of "front cover", "title page", "copyright page", "back cover", "other", "not a book"
  "title": the main title exactly as printed (no subtitle)
  "subtitle": the subtitle if printed, else ""
  "authors": list of author names as printed (include editors/translators/illustrators with their role in parentheses, e.g. "Barton Wright (editor and illustrator)")
  "publisher": publisher name if printed, else ""
  "place": place of publication if printed, else ""
  "year": publication or copyright year if printed, else ""
  "isbn": ISBN if printed (digits only), else ""
  "edition_notes": anything about edition, printing, or series, else ""
  "confidence": "high", "medium", or "low" for how legible the key fields were
  "notes": anything ambiguous, e.g. handwriting, multiple books visible, uncertain letters

Transcribe only what you can actually read in the image. Do not fill in details from memory of the book; leave a field "" if it is not visible. If several books are visible, describe the most prominent one and mention the others in "notes"."""


def extract_from_image(client, path, model):
    data = prepare_image(path)
    b64 = base64.standard_b64encode(data).decode("ascii")
    response = client.messages.create(
        model=model,
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    rec = parse_json_from_text(text)
    rec["source_image"] = os.path.basename(path)
    return rec


def merge_extractions(extractions):
    """Group per-photo readings into one record per book.

    Photos are matched on ISBN first, then on title similarity. For each field
    the copyright page wins over the title page, which wins over the cover,
    because that is the order in which the details are most reliably printed."""
    priority = {"copyright page": 3, "title page": 2, "front cover": 1, "back cover": 0, "other": 0}
    books = []
    for rec in extractions:
        if rec.get("page_type") == "not a book" or not (rec.get("title") or rec.get("isbn")):
            log(f"  skipping {rec.get('source_image')}: {rec.get('page_type')} / no title read")
            continue
        home = None
        for b in books:
            if rec.get("isbn") and b.get("isbn") and normalize_isbn(rec["isbn"]) == normalize_isbn(b["isbn"]):
                home = b
                break
            if titles_match(rec.get("title", ""), b.get("title", "")):
                home = b
                break
        if home is None:
            books.append({
                "title": rec.get("title", ""), "subtitle": rec.get("subtitle", ""),
                "authors": rec.get("authors", []) or [], "publisher": rec.get("publisher", ""),
                "place": rec.get("place", ""), "year": rec.get("year", ""), "isbn": rec.get("isbn", ""),
                "edition_notes": rec.get("edition_notes", ""), "confidence": rec.get("confidence", "medium"),
                "notes": rec.get("notes", ""), "source_images": [rec["source_image"]],
                "_priority": priority.get(rec.get("page_type"), 0),
            })
            continue
        # Merge into the existing record, preferring the more authoritative page.
        newer_wins = priority.get(rec.get("page_type"), 0) >= home["_priority"]
        for key in ("title", "subtitle", "publisher", "place", "year", "isbn", "edition_notes"):
            if newer_wins:
                home[key] = first_nonempty(rec.get(key), home.get(key))
            else:
                home[key] = first_nonempty(home.get(key), rec.get(key))
        if newer_wins and rec.get("authors"):
            home["authors"] = rec["authors"]
        elif not home["authors"]:
            home["authors"] = rec.get("authors", []) or []
        if rec.get("notes"):
            home["notes"] = (home["notes"] + " | " + rec["notes"]).strip(" |")
        home["source_images"].append(rec["source_image"])
        home["_priority"] = max(home["_priority"], priority.get(rec.get("page_type"), 0))
    for b in books:
        b.pop("_priority", None)
        b["author"] = ", ".join(b["authors"]) if isinstance(b["authors"], list) else str(b["authors"])
        b["full_title"] = b["title"] + (f": {b['subtitle']}" if b.get("subtitle") else "")
    return books


# ---------------------------------------------------------------------------
# Step 4: duplicate check against the existing database
# ---------------------------------------------------------------------------


def find_existing(candidate, existing):
    """Return the existing database row that looks like the same book, or None."""
    best, best_score = None, 0.0
    for row in existing:
        if not titles_match(candidate["title"], row["title"]):
            continue
        score = difflib.SequenceMatcher(None, normalize_title(candidate["title"]), normalize_title(row["title"])).ratio()
        # A matching author surname makes the match more convincing.
        cand_surnames = {w.lower() for w in re.findall(r"[A-Za-z\u00C0-\u017F']{3,}", candidate.get("author", ""))}
        row_surnames = {w.lower() for w in re.findall(r"[A-Za-z\u00C0-\u017F']{3,}", row["author"])}
        if cand_surnames and row_surnames and cand_surnames & row_surnames:
            score += 0.1
        if score > best_score:
            best, best_score = row, score
    return best


# ---------------------------------------------------------------------------
# Step 5: verify and summarize
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM = f"""You are a meticulous bibliographer researching a book for {LIBRARY_FOCUS}. Use the web search tool to establish the facts about the book named below. Prefer publisher sites, WorldCat, Google Books, JSTOR and other scholarly reviews, and university library records over retail listings, and do not confuse the book with another of a similar title; the bibliographic details supplied are authoritative for identifying the copy in hand.

Report, in plain prose with no markdown and no citation markup:
1. The confirmed full title, author(s) with roles, publisher, place, year of this edition and of first publication, page count, and ISBN, noting any discrepancy with the details supplied.
2. What the book actually contains: its purpose, organisation and, if you found a table of contents, its actual chapter structure; its main arguments or themes; who the author is and why the book matters.
3. What reviewers or scholars have said about it, if anything.
4. Which facts you could confirm from sources and which you could not; be explicit about gaps rather than filling them from memory."""

RESEARCH_USER = """Research this book. {provenance}

{details}"""

COMPOSE_SYSTEM = f"""You are a scholarly writer preparing catalogue entries for {LIBRARY_FOCUS}. Students rely on these entries, so accuracy matters more than completeness. You will be given verified research notes on a book; write only from those notes and from reliable knowledge of the book.

Absolute rules:
- Never invent quotations, page numbers, chapter titles, chapter numbers, or specific anecdotes. If the notes do not give the chapter structure, describe the book's themes and organisation in general terms.
- Never attribute to the book claims, arguments or examples the notes do not support. Do not pad.
- If the notes say little could be verified, say so plainly in the summary and keep it shorter; a short honest entry is far better than a long fabricated one.
- No citation markup, footnotes, URLs, markdown or tags anywhere in the summary text.

Style of the summary (it must match the rest of the database):
- Several well-crafted paragraphs of complete sentences, separated by blank lines; no bullet points, no headings.
- Begin with a paragraph that identifies the book, its author and its purpose, then discuss its main content and structure, then its unique takeaways or original insights, then its particular relevance to archaeoastronomy, cultural astronomy or the history of science, and end with a short assessment of who will find it most useful.
- Roughly 500 to 900 words when the book is well documented, fewer when it is not.
- Written for intelligent undergraduates: substantive, precise, and free of promotional language."""

COMPOSE_USER = """Bibliographic details of the copy in hand:
{details}

Research notes:
{notes}

Return ONLY a JSON object with exactly these keys:
  "title": full title including subtitle, in normal title case
  "author": author(s) as they should appear in a catalogue, including editor/translator/illustrator roles in parentheses where relevant
  "publisher": publisher of this edition
  "publication_year": four-digit year of this edition (or first publication if the edition year is unknown), as a string
  "num_pages": page count as a string of digits, or "" if it was not verified
  "keywords": a comma-separated string of 8 to 12 specific keywords (peoples, places, monuments, topics, methods), most specific first
  "summary": the multi-paragraph prose summary described in your instructions
  "confidence": "high" if the content of the book was well confirmed by sources, "medium" if partly, "low" if the notes relied on general knowledge
  "verification_notes": one or two sentences on what was and was not confirmed, and any discrepancy between the copy in hand and published records
  "sources": list of the URLs the research relied on (may be empty)"""


def candidate_details(candidate):
    return "\n".join(f"  {k}: {v}" for k, v in [
        ("Title", candidate.get("full_title") or candidate.get("title")),
        ("Author(s)", candidate.get("author")),
        ("Publisher", candidate.get("publisher")),
        ("Place", candidate.get("place")),
        ("Year", candidate.get("year")),
        ("ISBN", candidate.get("isbn")),
        ("Page count in existing record", candidate.get("pages")),
        ("Edition notes", candidate.get("edition_notes")),
        ("Cataloguer's notes", candidate.get("notes")),
    ] if v)


def estimate_cost(research_model, summary_model, searches=MAX_WEB_SEARCHES):
    """Rough dollars per entry for the chosen pair of models."""
    def rate(name):
        for key, value in RATES.items():
            if name.startswith(key) or key.startswith(name):
                return value
        return RATES["claude-opus-5"]
    r_in, r_out = rate(research_model)
    c_in, c_out = rate(summary_model)
    return (RESEARCH_IN * r_in + RESEARCH_OUT * r_out + COMPOSE_IN * c_in + COMPOSE_OUT * c_out) / 1e6 + searches * SEARCH_COST


def research_book(client, candidate, model, max_searches=MAX_WEB_SEARCHES):
    """Stage one: gather verified notes with web search. Returns (notes, urls)."""
    if candidate.get("_refresh_id"):
        provenance = ("These details come from the library's existing catalogue record, which was generated by an "
                      "older AI system and may contain errors; treat the title and author as reliable and verify the rest.")
    else:
        provenance = "These details were read from photographs of the physical copy in the library:"
    messages = [{"role": "user", "content": RESEARCH_USER.format(provenance=provenance, details=candidate_details(candidate))}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}]
    notes, urls = [], []
    for _ in range(6):   # web search can pause a long turn; keep going until it finishes
        response = client.messages.create(model=model, max_tokens=4000, system=RESEARCH_SYSTEM, messages=messages, tools=tools)
        for b in response.content:
            if b.type == "text":
                notes.append(b.text)
                for c in (getattr(b, "citations", None) or []):
                    u = getattr(c, "url", None)
                    if u and u not in urls:
                        urls.append(u)
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        break
    return scrub("\n".join(notes)), urls


def compose_entry(client, candidate, notes, urls, model):
    """Stage two: write the entry from the notes, with no tools, as clean JSON."""
    user = COMPOSE_USER.format(details=candidate_details(candidate), notes=notes or "(no research notes were produced)")
    if urls:
        user += "\n\nURLs consulted during research:\n" + "\n".join(urls)
    response = client.messages.create(model=model, max_tokens=5000, system=COMPOSE_SYSTEM,
                                      messages=[{"role": "user", "content": user}])
    return parse_json_from_text("\n".join(b.text for b in response.content if b.type == "text"))


def summarize_book(client, candidate, model, max_searches=MAX_WEB_SEARCHES, research_model=None):
    notes, urls = research_book(client, candidate, research_model or model, max_searches)
    result = compose_entry(client, candidate, notes, urls, model)
    sources = [u for u in (result.get("sources") or []) if isinstance(u, str) and u.startswith("http")] or urls
    # Keep the field names tidy and the types the database expects (all TEXT).
    return {
        "title": scrub(str(result.get("title") or candidate.get("full_title") or candidate.get("title"))),
        "author": scrub(str(result.get("author") or candidate.get("author"))),
        "publisher": scrub(str(result.get("publisher") or candidate.get("publisher"))),
        "year": re.sub(r"[^0-9]", "", str(result.get("publication_year") or candidate.get("year") or ""))[:4],
        "pages": re.sub(r"[^0-9]", "", str(result.get("num_pages") or "")),
        "keywords": scrub(str(result.get("keywords") or "")),
        "summary": scrub(str(result.get("summary") or "")),
        "confidence": str(result.get("confidence") or "unknown"),
        "verification_notes": scrub(str(result.get("verification_notes") or "")),
        "sources": sources,
        "research_notes": notes,
    }


class AuthError(Exception):
    """The API key was rejected: retrying cannot help."""


def call_with_retries(fn, what, attempts=3):
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # network hiccups, rate limits, malformed JSON
            text = str(exc)
            if "authentication_error" in text or "401" in text[:40]:
                raise AuthError(text) from exc
            log(f"    attempt {i} failed for {what}: {exc}")
            if i == attempts:
                raise
            time.sleep(5 * i)


# ---------------------------------------------------------------------------
# Step 6: write the new database and the report
# ---------------------------------------------------------------------------


def write_entry(db_path, action, row_id, e):
    """Write one finished entry straight into the new database (so an
    interrupted run loses nothing already generated). An existing row keeps
    the shelf code it already has; a new row is given the next free one."""
    con = sqlite3.connect(db_path)
    values = (e["title"], e["author"], e["publisher"], e["year"], e["keywords"], e["pages"], e["summary"])
    if action == "replace":
        con.execute('UPDATE books SET Title=?, Author=?, Publisher=?, "Publication Year"=?, Keywords=?, NumPages=?, Summary=? WHERE id=?',
                    values + (row_id,))
        code = con.execute("SELECT Code FROM books WHERE id=?", (row_id,)).fetchone()[0]
        if not code:
            taken = {r[0] for r in con.execute("SELECT Code FROM books WHERE Code IS NOT NULL")}
            code = next_code(author_prefix(e["author"]), taken)
            con.execute("UPDATE books SET Code=? WHERE id=?", (code, row_id))
    else:
        taken = {r[0] for r in con.execute("SELECT Code FROM books WHERE Code IS NOT NULL")}
        code = next_code(author_prefix(e["author"]), taken)
        con.execute('INSERT INTO books (id, Code, Title, Author, Publisher, "Publication Year", Keywords, NumPages, Summary) VALUES (?,?,?,?,?,?,?,?,?)',
                    (row_id, code) + values)
    con.commit()
    con.close()
    e["code"] = code
    return code


def find_duplicate_groups(existing):
    """Rows whose full titles are identical after normalisation. Returns a
    list of groups, each a list of rows, with the row to keep first: the one
    with the longest summary, since it carries the most information."""
    groups = {}
    for row in existing:
        key = normalize_title(row["title"], keep_subtitle=True)
        if key:
            groups.setdefault(key, []).append(row)
    out = []
    for rows in groups.values():
        if len(rows) > 1:
            out.append(sorted(rows, key=lambda r: (-r["summary_len"], r["id"])))
    return sorted(out, key=lambda g: g[0]["id"])


def remove_duplicates(db_path, groups):
    """Delete every row in each group except the first. Returns the deleted ids."""
    deleted = []
    con = sqlite3.connect(db_path)
    for g in groups:
        for row in g[1:]:
            con.execute("DELETE FROM books WHERE id=?", (row["id"],))
            deleted.append(row["id"])
    con.commit()
    con.close()
    return deleted


def is_suspect(db_path):
    """Ids of entries that show the known failure modes of the old pipeline:
    empty, beginning mid-stream with 'Chapter ...', or quoting page numbers."""
    con = sqlite3.connect(db_path)
    ids = []
    for row_id, summary in con.execute("SELECT id, Summary FROM books"):
        s = summary or ""
        if (not s.strip()
                or re.match(r"\s*(In )?Chapter", s)
                or re.search(r"\(pp?\.\s*\d+", s)
                or re.search(r"\bon page \d+", s, re.I)
                or len(s) < 400):
            ids.append(row_id)
    con.close()
    return ids


def parse_id_list(text, all_ids):
    """'all', 'suspect', or '1-50,167,878'."""
    ids = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            ids.extend(i for i in all_ids if int(a) <= i <= int(b))
        else:
            ids.append(int(part))
    return ids


def export_label_csv(db_path, path, ids=None):
    """Write Code, Title, Author, Publisher, Year for the given ids (or every
    row), in the column order make_labels.py expects."""
    con = sqlite3.connect(db_path)
    sql = 'SELECT Code, Title, Author, Publisher, "Publication Year" FROM books'
    if ids:
        sql += " WHERE id IN (" + ",".join("?" * len(ids)) + ")"
    sql += " ORDER BY Code"
    rows = con.execute(sql, ids or ()).fetchall()
    con.close()
    import csv as _csv
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.writer(fh)
        w.writerow(["Code", "Title", "Author", "Publisher", "Year"])
        w.writerows(rows)
    return len(rows)


def write_report(path, run_info, results):
    lines = [f"# archeobooks ingest report", "",
             f"Run: {run_info['timestamp']}  ",
             f"Source database: {run_info['source_db']}  ",
             f"New database: {run_info['new_db']}  ",
             f"Images: {run_info['image_folder'] or '(none, refresh/manifest run)'}  ", "",
             "| # | Action | DB id | Code | Title | Author | Publisher | Year | Pages | Confidence | Notes |",
             "|---|--------|-------|------|-------|--------|-----------|------|-------|------------|-------|"]
    for i, r in enumerate(results, 1):
        e = r.get("entry") or {}
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i, r["action"], r.get("db_id", ""), e.get("code", ""),
            (e.get("title") or r["candidate"].get("full_title") or "").replace("|", "/"),
            (e.get("author") or r["candidate"].get("author") or "").replace("|", "/"),
            (e.get("publisher") or "").replace("|", "/"),
            e.get("year", ""), e.get("pages", ""), e.get("confidence", ""),
            (r.get("reason") or e.get("verification_notes") or "").replace("|", "/").replace("\n", " ")))
    if run_info.get("dedupe"):
        lines += ["", "## Duplicates removed", ""]
        for g in run_info["dedupe"]:
            keep, rest = g[0], g[1:]
            lines.append(f"- Kept id {keep['id']} ({keep['title']} / {keep['author']}); removed " +
                         ", ".join(f"id {r['id']} ({r['author']})" for r in rest))
    lines += ["", "## Details", ""]
    for r in results:
        e = r.get("entry") or {}
        c = r["candidate"]
        lines.append(f"### {e.get('title') or c.get('full_title') or c.get('title')}")
        lines.append(f"- Action: **{r['action']}**" + (f" (database id {r['db_id']})" if r.get("db_id") else ""))
        if r.get("reason"):
            lines.append(f"- Reason: {r['reason']}")
        if c.get("source_images"):
            lines.append(f"- Photographs: {', '.join(c['source_images'])}")
        lines.append(f"- Read from photographs: {c.get('full_title') or c.get('title')} / {c.get('author')} / {c.get('publisher')} / {c.get('year')} / ISBN {c.get('isbn') or '-'}")
        if e:
            lines.append(f"- Confidence: {e.get('confidence')}")
            lines.append(f"- Verification notes: {e.get('verification_notes')}")
            if e.get("sources"):
                lines.append("- Sources: " + ", ".join(e["sources"]))
            lines.append(f"- Keywords: {e.get('keywords')}")
            lines.append("")
            lines.append(e.get("summary", ""))
            if e.get("research_notes"):
                lines.append("")
                lines.append("<details><summary>Research notes used to write this entry</summary>")
                lines.append("")
                lines.append(e["research_notes"])
                lines.append("")
                lines.append("</details>")
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Step 7 (phase 2): upload to GitHub
# ---------------------------------------------------------------------------


def upload_to_github(db_path, message):
    """Replace archeo2.db in the repository using the GitHub REST API.
    Needs a fine-grained personal access token with 'Contents: read and write'
    on the repo, supplied as the GITHUB_TOKEN environment variable."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GITHUB_TOKEN to a GitHub personal access token before using --upload.")
    api = "https://api.github.com"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "archeobooks-ingest"}

    # Find the current blob sha for the file (the tree listing works for any file size).
    req = urllib.request.Request(f"{api}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        tree = json.load(resp)
    sha = next((t["sha"] for t in tree.get("tree", []) if t["path"] == GITHUB_DB_PATH), None)

    with open(db_path, "rb") as fh:
        content = base64.b64encode(fh.read()).decode("ascii")
    body = {"message": message, "content": content, "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(
        f"{api}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_DB_PATH}",
        data=json.dumps(body).encode("utf-8"), headers={**headers, "Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        info = json.load(resp)
    log(f"Uploaded. Commit: {info['commit']['html_url']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Add books to archeo2.db from photographs of covers and title pages, or regenerate existing entries.")
    ap.add_argument("--images", help="folder of JPG/PNG/HEIC photographs of the new books")
    ap.add_argument("--manifest", help="re-use (possibly hand-edited) candidates.json instead of reading photographs")
    ap.add_argument("--refresh", help="regenerate existing entries: 'all', 'suspect', or ids and ranges like 167,878,1-50")
    ap.add_argument("--dedupe", action="store_true", help="remove exact duplicate titles from the database (keeps the longest entry)")
    ap.add_argument("--resume", action="store_true", help="continue the last interrupted run from its checkpoint")
    ap.add_argument("--db", help="use this local archeo2.db instead of downloading the current one; 'latest' means the newest archeo2_new file in the work folder")
    ap.add_argument("--out", help="name of the new database file (default archeo2_new_<date>.db)")
    ap.add_argument("--workdir", default="ingest_work", help="folder for downloads, candidates.json, checkpoints and reports")
    ap.add_argument("--replace", action="store_true", help="overwrite existing entries for photographed books already in the database (default: skip them)")
    ap.add_argument("--dry-run", action="store_true", help="call no AI: just list the images, duplicates and plan")
    ap.add_argument("--yes", action="store_true", help="do not pause for confirmation before generating summaries")
    ap.add_argument("--limit", type=int, help="only generate the first N entries of the plan (a trial batch)")
    ap.add_argument("--workers", type=int, default=3, help="how many books to process at once (default 3)")
    ap.add_argument("--extract-model", default=EXTRACT_MODEL)
    ap.add_argument("--summary-model", default=SUMMARY_MODEL, help="model that writes the entries (default %(default)s)")
    ap.add_argument("--research-model", default=RESEARCH_MODEL, help="model that searches the web; this is where most of the cost is (default %(default)s)")
    ap.add_argument("--upload", action="store_true", help="phase 2: push the new database to GitHub when finished")
    ap.add_argument("--add-codes", action="store_true", help="add the Code column and give every entry a shelf code; no AI calls")
    ap.add_argument("--scrub", metavar="DBFILE", help="only clean citation markup out of the summaries in this database file, in place")
    args = ap.parse_args()

    if args.scrub:
        n = scrub_database(args.scrub)
        log(f"Cleaned citation markup out of {n} entries in {args.scrub}.")
        return

    if not (args.images or args.manifest or args.refresh or args.dedupe or args.resume or args.add_codes):
        ap.error("give --images FOLDER, --manifest candidates.json, --refresh IDS, --dedupe, --add-codes, or --resume")

    os.makedirs(args.workdir, exist_ok=True)
    checkpoint_path = os.path.join(args.workdir, "checkpoint.json")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    client = None
    needs_api = bool(args.images or args.manifest or args.refresh or args.resume)
    if needs_api and not args.dry_run:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set. See the top of this file for how to set it.")
        import anthropic
        client = anthropic.Anthropic(max_retries=4)
        try:
            client.messages.create(model=args.extract_model, max_tokens=1,
                                   messages=[{"role": "user", "content": "hi"}])
        except Exception as exc:
            text = str(exc)
            if "authentication_error" in text or "401" in text[:40]:
                sys.exit("The API key was rejected by Anthropic (401).\n"
                         "Check it with:  echo \"[$ANTHROPIC_API_KEY]\"\n"
                         "It should begin sk-ant-api03- with no spaces inside the brackets. If it looks\n"
                         "right, the key may have been revoked, or its workspace may have no credit;\n"
                         "create a new one at console.anthropic.com and export it again.")
            log(f"Warning: could not verify the API key ({exc}); carrying on.")

    # ---- Resume an interrupted run -------------------------------------
    if args.resume:
        if not os.path.exists(checkpoint_path):
            sys.exit(f"No checkpoint found at {checkpoint_path}; nothing to resume.")
        with open(checkpoint_path, encoding="utf-8") as fh:
            ck = json.load(fh)
        out_db, source_db = ck["out_db"], ck["source_db"]
        plan = [tuple(p) for p in ck["plan"]]
        results = ck["results"]
        done_ids = {r["db_id"] for r in results if r.get("db_id") is not None}
        dedupe_groups = ck.get("dedupe_groups", [])
        args.summary_model = ck.get("summary_model", args.summary_model)
        args.research_model = ck.get("research_model", args.research_model)
        log(f"Resuming: {len(done_ids)} of {len(plan)} entries already finished; writing into {out_db}")
    else:
        out_db = args.out or os.path.join(args.workdir, f"archeo2_new_{stamp}.db")
        source_db = fetch_database(args.db, args.workdir)
        shutil.copyfile(source_db, out_db)
        plan, results, dedupe_groups, done_ids = [], [], [], set()
    db_changed = args.resume   # becomes True once the new file differs from the source

    if not args.resume:
        if ensure_code_column(out_db):
            log("Added the Code column to the database.")
            db_changed = True

    existing = load_existing_books(out_db)
    log(f"Database has {len(existing)} books.")

    if not args.resume:
        # ---- Duplicates ------------------------------------------------
        if args.dedupe:
            groups = find_duplicate_groups(existing)
            log(f"Found {len(groups)} duplicated titles ({sum(len(g) - 1 for g in groups)} rows to remove):")
            for g in groups:
                log(f"  keep id {g[0]['id']:>4} ({g[0]['summary_len']} chars), remove " +
                    ", ".join(f"id {r['id']} ({r['summary_len']} chars)" for r in g[1:]) + f"   {g[0]['title'][:60]}")
            if not args.dry_run:
                remove_duplicates(out_db, groups)
                db_changed = True
                existing = load_existing_books(out_db)
                log(f"Removed; database now has {len(existing)} books.")
            dedupe_groups = groups

        # ---- Shelf codes -----------------------------------------------
        if not args.dry_run:
            assigned, kept = assign_codes(out_db)
            if assigned:
                log(f"Assigned shelf codes to {assigned} entries ({kept} already had one).")
                db_changed = True
            existing = load_existing_books(out_db)
        elif not has_code_column(source_db):
            log(f"Would add the Code column and assign a shelf code to all {len(existing)} entries.")

        # ---- Candidates from photographs or manifest -------------------
        candidates = []
        if args.manifest:
            with open(args.manifest, encoding="utf-8") as fh:
                candidates = json.load(fh)
            for c in candidates:
                c.setdefault("full_title", c.get("title", ""))
                c.setdefault("author", ", ".join(c.get("authors", [])) if isinstance(c.get("authors"), list) else c.get("authors", ""))
            log(f"Loaded {len(candidates)} candidates from {args.manifest}.")
        elif args.images:
            images = list_images(args.images)
            log(f"Found {len(images)} photographs in {args.images}.")
            if args.dry_run:
                log("Dry run: photographs are not sent to the API. Listing them only:")
                for p in images:
                    log(f"  {os.path.basename(p)}")
            else:
                log("Reading photographs ...")
                cache_path = os.path.join(args.workdir, "extractions_cache.json")
                cache = {}
                if os.path.exists(cache_path):
                    with open(cache_path, encoding="utf-8") as fh:
                        cache = json.load(fh)
                extractions = []
                for p in images:
                    key = f"{os.path.basename(p)}|{os.path.getsize(p)}"
                    if key in cache:
                        rec = cache[key]
                        tag = "cached"
                    else:
                        rec = call_with_retries(lambda: extract_from_image(client, p, args.extract_model), os.path.basename(p))
                        cache[key] = rec
                        with open(cache_path, "w", encoding="utf-8") as fh:
                            json.dump(cache, fh, indent=1, ensure_ascii=False)
                        tag = rec.get("confidence")
                    extractions.append(rec)
                    log(f"  {os.path.basename(p)}: [{rec.get('page_type')}] {rec.get('title')!r} by {', '.join(rec.get('authors') or [])} ({tag})")
                with open(os.path.join(args.workdir, f"extractions_{stamp}.json"), "w", encoding="utf-8") as fh:
                    json.dump(extractions, fh, indent=2, ensure_ascii=False)
                candidates = merge_extractions(extractions)
                manifest_path = os.path.join(args.workdir, "candidates.json")
                with open(manifest_path, "w", encoding="utf-8") as fh:
                    json.dump(candidates, fh, indent=2, ensure_ascii=False)
                log(f"Merged into {len(candidates)} books; written to {manifest_path} (edit and re-run with --manifest if anything is wrong).")

        # ---- Existing entries to regenerate ----------------------------
        if args.refresh:
            all_ids = [r["id"] for r in existing]
            if args.refresh.strip().lower() == "all":
                ids = all_ids
            elif args.refresh.strip().lower() == "suspect":
                ids = is_suspect(out_db)
            else:
                ids = parse_id_list(args.refresh, all_ids)
            by_id = {r["id"]: r for r in existing}
            for i in ids:
                row = by_id.get(i)
                if not row:
                    log(f"  no database row with id {i}; skipping")
                    continue
                candidates.append({"title": row["title"], "full_title": row["title"], "author": row["author"],
                                   "publisher": row["publisher"], "year": row["year"], "pages": row["pages"], "isbn": "",
                                   "notes": "", "source_images": [], "confidence": "existing entry", "_refresh_id": i})
            log(f"Queued {len(ids)} existing entries for regeneration ({args.refresh}).")

        # ---- Plan ------------------------------------------------------
        next_id = (max(r["id"] for r in existing) if existing else 0) + 1
        log("")
        log("Plan:")
        for c in candidates:
            if c.get("_refresh_id"):
                plan.append(("replace", c["_refresh_id"], c, "regenerating existing entry"))
                if len(candidates) <= 40:
                    log(f"  REPLACE  id {c['_refresh_id']:>4}  {c['full_title']}")
                continue
            match = find_existing(c, existing)
            if match and args.replace:
                plan.append(("replace", match["id"], c, f"already in database as id {match['id']} ({match['title']} / {match['author']}); replacing"))
                log(f"  REPLACE  id {match['id']:>4}  {c['full_title']}  (was: {match['title']})")
            elif match:
                plan.append(("skip", match["id"], c, f"already in database as id {match['id']} ({match['title']} / {match['author']}); use --replace to regenerate"))
                log(f"  SKIP     id {match['id']:>4}  {c['full_title']}  (already present; --replace to overwrite)")
            else:
                plan.append(("add", next_id, c, ""))
                log(f"  ADD      id {next_id:>4}  {c['full_title']}  by {c.get('author')}")
                next_id += 1
        if len(candidates) > 40:
            log(f"  ({sum(1 for p in plan if p[0] == 'replace')} entries to regenerate; list omitted)")
        for action, row_id, c, reason in plan:
            if action == "skip":
                results.append({"action": "skipped", "db_id": row_id, "candidate": c, "reason": reason})

    if args.add_codes and not (args.images or args.manifest or args.refresh):
        export_label_csv(out_db, os.path.join(args.workdir, f"labels_all_{stamp}.csv"))
        log("")
        log(f"New database: {out_db}")
        log(f"Label spreadsheet: {os.path.join(args.workdir, f'labels_all_{stamp}.csv')}")
        return

    # ---- Confirm ---------------------------------------------------------
    todo = [p for p in plan if p[0] != "skip" and p[1] not in done_ids]
    if args.limit:
        todo = todo[:args.limit]
    if args.dry_run:
        log("")
        log(f"Dry run complete; {len(todo)} entries would be generated. Nothing was changed.")
        os.remove(out_db)
        return
    if not todo:
        log("Nothing to generate.")
        if not db_changed:
            os.remove(out_db)
            return
    else:
        per_entry = estimate_cost(args.research_model, args.summary_model)
        est_cost = len(todo) * per_entry
        est_hours = len(todo) * 1.5 / 60 / max(1, args.workers)
        log("")
        log(f"{len(todo)} entries, {args.workers} at a time: research with {args.research_model}, "
            f"written by {args.summary_model}.")
        log(f"Rough estimate: {est_hours:.1f} hours and ${est_cost * 0.6:.0f} to ${est_cost * 1.6:.0f} "
            f"(about ${per_entry:.2f} per entry; actual spending shows at console.anthropic.com).")
        if not args.yes:
            answer = input("Proceed? [Y/n] ").strip().lower()
            if answer not in ("", "y", "yes"):
                log("Stopped. Nothing was generated.")
                if not db_changed:
                    os.remove(out_db)
                return

    def save_checkpoint():
        with open(checkpoint_path, "w", encoding="utf-8") as fh:
            json.dump({"out_db": out_db, "source_db": source_db, "plan": plan, "results": results,
                       "dedupe_groups": dedupe_groups, "summary_model": args.summary_model,
                       "research_model": args.research_model}, fh, indent=1, ensure_ascii=False)

    # ---- Generate, several at a time, writing each entry as it finishes --
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading
        lock = threading.Lock()
        save_checkpoint()
        finished = 0

        def work(item):
            action, row_id, c, reason = item
            entry = call_with_retries(lambda: summarize_book(client, c, args.summary_model, research_model=args.research_model), c["full_title"])
            return item, entry

        try:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(work, item): item for item in todo}
                for fut in as_completed(futures):
                    action, row_id, c, reason = futures[fut]
                    try:
                        _, entry = fut.result()
                    except AuthError as exc:
                        log(f"\nThe API key was rejected mid-run (401): {str(exc)[:120]}")
                        log("Stopping. Fix the key, then run again with --resume.")
                        for f in futures:
                            f.cancel()
                        save_checkpoint()
                        return
                    except Exception as exc:
                        with lock:
                            results.append({"action": "failed", "db_id": None, "candidate": c, "reason": f"summary generation failed: {exc}"})
                            log(f"  FAILED  {c['full_title']}: {exc}")
                            save_checkpoint()
                        continue
                    with lock:
                        write_entry(out_db, action, row_id, entry)
                        results.append({"action": "replaced" if action == "replace" else "added", "db_id": row_id,
                                        "candidate": c, "entry": entry, "reason": reason})
                        finished += 1
                        log(f"  [{finished}/{len(todo)}] {'replaced' if action == 'replace' else 'added':8} id {row_id:>4}  {entry['title'][:60]}  "
                            f"({entry['year']}, {entry['pages'] or '?'} pp, confidence {entry['confidence']})")
                        if entry["verification_notes"]:
                            log(f"           note: {entry['verification_notes'][:160]}")
                        save_checkpoint()
        except KeyboardInterrupt:
            log("\nInterrupted. Everything finished so far is saved; run again with --resume to continue.")
            save_checkpoint()
            return

    # ---- Report --------------------------------------------------------
    report_path = os.path.join(args.workdir, f"ingest_report_{stamp}.md")
    ordered = sorted(results, key=lambda r: (r.get("db_id") is None, r.get("db_id") or 0))
    touched = [r["db_id"] for r in results if r["action"] in ("added", "replaced") and r.get("db_id")]
    labels_csv = os.path.join(args.workdir, f"labels_{stamp}.csv")
    if touched:
        export_label_csv(out_db, labels_csv, ids=touched)
    write_report(report_path, {"timestamp": stamp, "source_db": source_db, "new_db": out_db,
                               "image_folder": args.images, "dedupe": dedupe_groups}, ordered)
    with open(os.path.join(args.workdir, f"ingest_results_{stamp}.json"), "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=2, ensure_ascii=False)

    n_added = sum(r["action"] == "added" for r in results)
    n_rep = sum(r["action"] == "replaced" for r in results)
    n_skip = sum(r["action"] == "skipped" for r in results)
    n_fail = sum(r["action"] == "failed" for r in results)
    remaining = len([p for p in plan if p[0] != "skip"]) - n_added - n_rep
    log("")
    log(f"Done. Added {n_added}, replaced {n_rep}, skipped {n_skip}, failed {n_fail}"
        + (f", {remaining} still to do (run again with --resume)." if remaining > 0 else "."))
    if dedupe_groups:
        log(f"Duplicates removed: {sum(len(g) - 1 for g in dedupe_groups)} rows.")
    log(f"New database: {out_db}")
    log(f"Report:       {report_path}")
    if touched:
        log(f"Labels:       {labels_csv}  (feed to make_labels.py)")

    # ---- Upload (phase 2) ----------------------------------------------
    if args.upload and (n_added or n_rep or dedupe_groups) and remaining <= 0:
        upload_to_github(out_db, f"Add {n_added}, update {n_rep}, remove {sum(len(g) - 1 for g in dedupe_groups)} book entries ({stamp})")


if __name__ == "__main__":
    main()
