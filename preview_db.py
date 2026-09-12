#!/usr/bin/env python3
"""
preview_db.py
=============

Open any archeo database file in the library's own web interface, running
locally on your Mac, so you can browse and search it exactly as students will
before it goes on the website.

    python3 preview_db.py                      # newest archeo2_new_*.db in ingest_work
    python3 preview_db.py latest               # the same
    python3 preview_db.py ingest_work/archeo2_new_20260908_161506.db
    python3 preview_db.py ingest_work/archeo2_current.db      # the published version, for comparison

It fetches the current index.html from the GitHub repository, points it at
the local file instead of the published one, starts a small web server in a
temporary folder, and opens your browser. Press Ctrl-C in Terminal to stop.
"""

import glob
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

WORK_DIR = "ingest_work"
INDEX_URL = "https://raw.githubusercontent.com/bpenprase/archeobooks/main/index.html"
PUBLISHED_DB_URL = "https://raw.githubusercontent.com/bpenprase/archeobooks/main/archeo2.db"


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "latest"
    if arg.lower() == "latest":
        files = sorted(glob.glob(os.path.join(WORK_DIR, "archeo2_new_*.db")), key=os.path.getmtime)
        if not files:
            sys.exit(f"No archeo2_new_*.db files found in {WORK_DIR}; run archeobooks_ingest.py first, "
                     "or give the path of a .db file.")
        db_path = os.path.abspath(files[-1])
    elif os.path.exists(arg):
        db_path = os.path.abspath(arg)
    else:
        sys.exit(f"File not found: {arg}\nUsage: python3 preview_db.py [latest | path/to/file.db]")

    # Use a local index.html if one sits next to this script, otherwise fetch the live one.
    local_index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    if os.path.exists(local_index):
        html = open(local_index, encoding="utf-8").read()
    else:
        with urllib.request.urlopen(INDEX_URL, timeout=30) as resp:
            html = resp.read().decode("utf-8")

    if PUBLISHED_DB_URL not in html:
        sys.exit("Could not find the database address in index.html; the page may have changed.")
    html = html.replace(PUBLISHED_DB_URL, "preview.db")
    html = html.replace("<h1 style=\"margin:0;\">archeo2.db</h1>",
                        f"<h1 style=\"margin:0;\">PREVIEW: {os.path.basename(db_path)}</h1>")

    site = tempfile.mkdtemp(prefix="archeo_preview_")
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    shutil.copyfile(db_path, os.path.join(site, "preview.db"))

    # Pick a free port and serve the folder.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=site, **k)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Previewing {db_path}")
    print(f"Open {url} in your browser (it should open automatically). Press Ctrl-C here to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        shutil.rmtree(site, ignore_errors=True)


if __name__ == "__main__":
    main()
