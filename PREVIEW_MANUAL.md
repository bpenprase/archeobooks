# preview_db: a short manual

*Browsing a database file in the library's own web interface before it is published.*

## What it does

The library website reads its data from `archeo2.db` in the GitHub repository, and cannot show a database file that is still on your Mac. `preview_db.py` closes that gap. It fetches a copy of the website's page, points it at a local database file instead of the published one, starts a small web server on your Mac, and opens your browser. What you see is the real library interface, headed "PREVIEW:" and the name of the file, with the same search boxes, the same table and the same CSV download, but showing the file you chose. Nothing is uploaded and nothing on the website changes.

## Running it

The usual three lines start a session:

```
cd /path/to/archeobooks_ingest
source venv/bin/activate
export ANTHROPIC_API_KEY="sk-ant-..."
```

(`preview_db.py` itself needs neither the virtual environment nor the API key, since it uses only what comes with Python, but you will normally be running it in the same window as the ingestion program, so the habit costs nothing.) Then one of:

```
python3 preview_db.py                                        # the newest archeo2_new file in ingest_work
python3 preview_db.py latest                                 # the same, spelled out
python3 preview_db.py ingest_work/archeo2_new_20260908_161506.db    # a particular file
python3 preview_db.py ingest_work/archeo2_current.db         # the published version, for comparison
```

With no argument the program looks in `ingest_work` for the most recently written `archeo2_new_...db`, which is the output of your last ingestion run, so the ordinary sequence after a run is simply `python3 preview_db.py`. Terminal prints

```
Previewing /Users/you/archeobooks_ingest/ingest_work/archeo2_new_20260908_161506.db
Open http://127.0.0.1:52667/ in your browser (it should open automatically). Press Ctrl-C here to stop.
```

and your browser opens to the preview. If it does not open by itself, copy the address into any browser. The port number changes from run to run, which is harmless.

## Using the preview

The interface is the website's own, so it behaves exactly as students will find it: the Author, Title and Keywords boxes search their columns, the Any box searches everything, searches are case-insensitive, and a comma between terms means "or". To find the entries you have just added, search the Author box for a name from the batch, or sort by id if the table offers it, since new entries take the next free ids after the existing ones. The "Download all results (CSV)" button works in the preview too, and is a convenient way to get a spreadsheet of the new entries for reading offline.

Comparing two files is a matter of running the previewer twice in two Terminal windows, once on `archeo2_current.db` (the published version) and once on the new file, and switching between the browser tabs.

## Stopping

Return to the Terminal window and press Ctrl-C. The server stops, the temporary folder it was serving from is deleted, and the browser tab will show a connection error if you reload it, which is expected. Your database file is untouched throughout: the previewer works on a temporary copy.

## Troubleshooting

*"No archeo2_new_*.db files found in ingest_work"*: there has been no ingestion run yet in this folder, or you are not in the working folder; `pwd` shows where you are. Give the path of a file explicitly if it is somewhere else.

*"File not found"*: check the spelling of the path; `ls ingest_work` lists the files available.

*The page opens but stays at "Loading..."*: the page fetches its table-reading library from a public web address, so this usually means the Mac is offline. The database itself is served locally and needs no connection.

*The page shows the old data*: you may have two previews open at once; check the "PREVIEW:" heading for the filename, and that you are looking at the tab the latest run opened.

*You want to keep a preview running while doing other things*: open a second Terminal window for the other work; the preview keeps running until its own window receives Ctrl-C.
