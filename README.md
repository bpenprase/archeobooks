# Archeobooks: the archaeoastronomy library database

The archaeoastronomy library at Soka University of America is catalogued in a single SQLite file, `archeo2.db`, which is served to students through a small browser page at **https://bpenprase.github.io/archeobooks/**. Each row holds one book: a shelf code, title, author, publisher, publication year, keywords, page count, and a summary of several paragraphs describing what the book contains and why it matters to the study of cultural astronomy.

This repository holds the database, the website page that displays it, and the Python tools that maintain it.

## What the tools are for

The catalogue was originally built by feeding titles to early language models and storing whatever came back. That produced a usable library of some nine hundred books, but with three recurring faults: entries whose opening paragraphs had been lost, so that they began mid-stream inside an invented chapter-by-chapter walkthrough; confident page citations and quotations for books the model had never seen; and duplicated rows. Adding new arrivals was also laborious, since each title had to be typed in and prompted for individually.

The current system addresses both problems. New books are catalogued from photographs: you photograph the cover or copyright page of each new arrival with a phone, point the program at the folder, and it reads the bibliographic details off the pages, checks them against the existing database, and writes verified entries. Accuracy comes from splitting the work in two. A research call, with web search enabled, establishes the facts from publisher pages, library catalogues, Google Books and reviews, and reports plainly what it could not confirm. A second call, with no tools and strict instructions against inventing quotations, page numbers or chapter structures, composes the entry from those verified notes. Every entry is filed with a confidence rating, verification notes and the list of sources it rests on, and the whole run is written up in a report for a human to read before anything is published.

The same machinery repairs the old entries, either by database id or by selecting the ones that show the known failure patterns, and removes duplicate rows.

## The files

### The library itself

| File | What it is |
|---|---|
| `archeo2.db` | The catalogue: one SQLite table, `books`, with the columns id, Code, Title, Author, Publisher, Publication Year, Keywords, NumPages and Summary. This is the file the website reads. |
| `index.html` | The website page. It downloads `archeo2.db` in the browser, reads it with sql.js, and provides search by author, title, keyword or anything, with CSV export. Changing which columns appear means editing the `DISPLAY_SPEC` list near the top of its script. |

### The programs

| File | What it does |
|---|---|
| `archeobooks_ingest.py` | The main program. Reads photographs of books, verifies and writes entries, regenerates suspect or duplicated entries, assigns shelf codes, and produces a new database file plus a report. Never modifies the file it starts from. |
| `preview_db.py` | Opens any database file in the library's own web interface, running locally on your machine, so that a new file can be browsed exactly as students will see it before it is published. |
| `compare_entries.py` | Puts the same books from two or three database files side by side in one Markdown document, for judging whether a regenerated entry is an improvement. |
| `make_labels.py` | Turns a spreadsheet of shelf codes into a Word document of spine labels laid out for Avery 5160 sheets, with the code and an icon on the left of each label and the author and title on the right. |

### The documentation

| File | What it covers |
|---|---|
| `MANUAL.md` | The full manual for `archeobooks_ingest.py`: setup, every command-line option, five worked examples, how the suspect filter decides what to regenerate, how to read a report, previewing, publishing, troubleshooting and costs. |
| `MANUAL.pdf` | The same manual as a PDF, for printing or circulating. |
| `PREVIEW_MANUAL.md` | A short manual for `preview_db.py`. |
| `README.md` | This file. |

### Supporting material

| File | What it is |
|---|---|
| `chitzen_itza.jpg` | The stepped-pyramid line drawing printed on each shelf label. |
| `sample_report_from_mock_run.md` | An example of the report a run produces, for reference. |

## The shelf code

Every entry carries a code of the form `FRA.001`: the first three letters of the first author's surname, then a sequence number that distinguishes books sharing a surname. Codes are assigned in database-id order and never change once given, since they are printed on labels and stuck to the spines of physical books. New entries receive the next free code as they are written, and any run that adds or replaces entries also writes a spreadsheet of just those books for `make_labels.py` to turn into labels.

## Typical work

Starting a session, in Terminal, from the working folder:

```
source venv/bin/activate
export ANTHROPIC_API_KEY="sk-ant-..."
```

Cataloguing a batch of new arrivals photographed into a folder:

```
python3 archeobooks_ingest.py --images new_books --db latest
python3 preview_db.py
```

Repairing the entries that show signs of the old pipeline's failures:

```
python3 archeobooks_ingest.py --dedupe --refresh suspect --db latest --workers 4 --research-model claude-haiku-4-5
```

Printing labels for whatever the last run added:

```
python3 make_labels.py ingest_work/labels_20260911_172159.csv --icon chitzen_itza.jpg
```

Publishing means replacing `archeo2.db` in this repository with the new file, renamed, through GitHub Desktop or the browser; the website reads it afresh on every visit. Full instructions for all of this are in `MANUAL.md`.

## Requirements

Python 3.9 or later, and two libraries installed into a virtual environment:

```
python3 -m venv venv
source venv/bin/activate
python3 -m pip install anthropic pillow
```

`make_labels.py` additionally needs `python-docx`. An Anthropic API key is required for anything that generates entries; the shelf-code pass, the previewer, the comparison tool and the label maker need no key and cost nothing. Keys belong in the `ANTHROPIC_API_KEY` environment variable and should never be written into a file in this repository.

## A note on trust

The entries in this database are written by a language model and read by students. The safeguards are the two-stage process, the prohibitions in the writing prompt, and the confidence rating, verification notes and sources recorded for every regenerated entry. None of that removes the need for a human to read the report before publishing, which is why the program writes to a new file every time and never touches the live one.
