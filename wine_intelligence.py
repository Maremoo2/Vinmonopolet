"""Local catalogue client for the documented Vinmonopolet Open API."""
import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
import gzip
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENDPOINT = "https://apis.vinmonopolet.no/products/v0/details-normal"
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY, name TEXT, changed_date TEXT, changed_time TEXT
);
CREATE TABLE IF NOT EXISTS product_history (
    sequence INTEGER PRIMARY KEY, product_id TEXT NOT NULL, name TEXT,
    changed_date TEXT, changed_time TEXT, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1), last_started TEXT NOT NULL
);
"""


class ImportErrorDetail(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenProducts:
    def __init__(self, key):
        self.key = key
        self.opener = build_opener(NoRedirect())
        self.next_call = 0
        self.calls = 0

    def page(self, **params):
        request = Request(
            ENDPOINT + ("?" + urlencode(params) if params else ""),
            headers={
                "Ocp-Apim-Subscription-Key": self.key,
                "Cache-Control": "no-cache",
                "Accept-Encoding": "gzip",
            },
        )
        for attempt in range(3):
            if self.calls >= 3900:
                raise ImportErrorDetail("Kallbudsjettet for kjøringen er brukt opp.")
            time.sleep(max(0, self.next_call - time.monotonic()))
            self.next_call = time.monotonic() + 1.1
            self.calls += 1
            try:
                with self.opener.open(request, timeout=30) as response:
                    status = response.status
                    content_type = response.headers.get_content_type()
                    encoding = response.headers.get("Content-Encoding", "").strip().lower()
                    body = response.read()
                data = decode_response(body, status, content_type, encoding)
                return parse_page(data)
            except HTTPError as error:
                retry_after = error.headers.get("Retry-After", "")
                status = error.code
                error.close()
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise ImportErrorDetail(f"Vinmonopolet svarte HTTP {status}.") from None
                if retry_after and (not retry_after.isascii() or not retry_after.isdecimal()):
                    raise ImportErrorDetail(f"HTTP {status}. Kjør igjen etter Retry-After.") from None
                delay = int(retry_after) if retry_after else 2 ** (attempt + 1)
                if delay > 120:
                    raise ImportErrorDetail(f"HTTP {status}. Vent minst {delay} sekunder.") from None
                time.sleep(delay)
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise ImportErrorDetail("Nettverksfeil ved oppslag mot Vinmonopolet.") from None
                time.sleep(2 ** (attempt + 1))


def decode_response(body, status, content_type, encoding):
    # Some gateways return compressed responses even for small requests.
    if encoding == "gzip" or body.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            raise ImportErrorDetail(f"HTTP {status}. Ugyldig gzip-respons.") from None
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        response_type = content_type if content_type in {
            "application/json", "text/html", "text/plain", "application/octet-stream"
        } else "ukjent"
        response_encoding = encoding if encoding in {"gzip", "br", "deflate"} else "ukjent" if encoding else "ingen"
        raise ImportErrorDetail(
            f"HTTP {status}. Ugyldig JSON ({response_type}, komprimering: {response_encoding}, "
            f"{len(body)} byte)."
        ) from None


def parse_page(data):
    if not isinstance(data, list):
        raise ImportErrorDetail("Uventet API-svar. Forventet en produktliste.")
    rows = []
    for item in data:
        basic = item.get("basic") if isinstance(item, dict) else None
        changed = item.get("lastChanged") if isinstance(item, dict) else None
        changed = {} if changed is None else changed
        if not isinstance(basic, dict) or not isinstance(changed, dict):
            raise ImportErrorDetail("Uventet produktstruktur i API-svaret.")
        product_id = basic.get("productId")
        fields = (basic.get("productShortName"), changed.get("date"), changed.get("time"))
        if not isinstance(product_id, str) or not product_id.strip() or any(
            value is not None and not isinstance(value, str) for value in fields
        ):
            raise ImportErrorDetail("Uventede produktfelt i API-svaret.")
        rows.append((product_id, *fields))
    return rows


def check_page_ids(rows, seen):
    ids = {row[0] for row in rows}
    if len(ids) != len(rows) or seen.intersection(ids):
        raise ImportErrorDetail("Gjentatte varenummer. Avbrøt for å unngå en ufullstendig import.")
    seen.update(ids)


def probe(client, product_ids):
    seen = set()
    for offset in (0, 5):
        try:
            rows = client.page(maxResults=5, start=offset)
        except ImportErrorDetail as error:
            if offset == 0:
                try:
                    rows = client.page()
                    print(f"HTTP 200 uten parametere, {len(rows)} rader")
                except ImportErrorDetail as baseline_error:
                    print(f"Kontroll uten parametere: {baseline_error}")
            raise ImportErrorDetail(f"Side start={offset}: {error}") from None
        check_page_ids(rows, seen)
        print(f"HTTP 200, start={offset}, {len(rows)} rader")
        if not rows:
            raise ImportErrorDetail("Tom prøveside. Pagineringen kunne ikke bekreftes.")
    for product_id in product_ids:
        rows = client.page(productId=product_id, maxResults=5)
        found = any(row[0] == product_id for row in rows)
        print(f"HTTP 200, varenummer {product_id}: {'treff' if found else 'ingen treff'}")


def sync(db, client, full, page_size):
    # Hold the write lock across the import so overlapping jobs cannot advance the checkpoint.
    db.execute("BEGIN IMMEDIATE")
    started = datetime.now(timezone.utc)
    today = started.astimezone(ZoneInfo("Europe/Oslo")).date()
    previous = db.execute("SELECT last_started FROM sync_state WHERE id = 1").fetchone()
    filters = {}
    if previous and not full:
        filters["changedSince"] = (date.fromisoformat(previous[0]) - timedelta(days=1)).isoformat()

    offset = 0
    revisions = 0
    seen = set()
    while True:
        rows = client.page(start=offset, maxResults=page_size, **filters)
        if not rows:
            break
        check_page_ids(rows, seen)
        for row in rows:
            current = db.execute(
                "SELECT name, changed_date, changed_time FROM products WHERE id = ?", (row[0],)
            ).fetchone()
            if current == row[1:]:
                continue
            db.execute(
                """INSERT INTO products VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                   changed_date=excluded.changed_date, changed_time=excluded.changed_time""",
                row,
            )
            db.execute(
                """INSERT INTO product_history
                   (product_id, name, changed_date, changed_time, observed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (*row, started.isoformat()),
            )
            revisions += 1
        offset += len(rows)

    if not filters and not seen:
        raise ImportErrorDetail("Full import ga null produkter. Synkroniseringspunktet ble beholdt.")
    db.execute("INSERT OR REPLACE INTO sync_state VALUES (1, ?)", (today.isoformat(),))
    db.commit()
    print(f"Import fullført: {offset} produkter lest, {revisions} historikkrader.")


def cellar_report(db, path):
    writer = csv.writer(sys.stdout, lineterminator="\n")
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "varenummer" not in reader.fieldnames:
            raise ImportErrorDetail("Kjellerfilen må ha en kolonne som heter varenummer.")
        writer.writerow(["varenummer", "produktnavn", "treff"])
        for item in reader:
            product_id = (item["varenummer"] or "").strip()
            row = db.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
            writer.writerow([product_id, row[0] if row else "", "ja" if row else "nei"])


def main():
    parser = argparse.ArgumentParser(description="Lokal produktkatalog for Vinmonopolets Open-API")
    parser.add_argument("--database", type=Path, default=Path("data/catalog.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("probe", help="Test to sider og eventuelle varenummer")
    test.add_argument("varenummer", nargs="*")
    ingest = commands.add_parser("sync", help="Importer katalogen eller hent endringer")
    ingest.add_argument("--full", action="store_true")
    ingest.add_argument("--page-size", type=int, default=500)
    cellar = commands.add_parser("cellar", help="Koble kjeller-CSV mot katalogen")
    cellar.add_argument("csv", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "sync" and not 1 <= args.page_size <= 1000:
            parser.error("--page-size må være mellom 1 og 1000")
        if args.command in {"probe", "sync"}:
            key = os.environ.get("VINMONOPOLET_API_KEY", "").strip()
            if not key and sys.stdin.isatty():
                key = getpass("Open API-nøkkel: ").strip()
            if not key:
                raise ImportErrorDetail("Mangler Open-nøkkel. Bruk VINMONOPOLET_API_KEY.")
            if not key.isascii() or any(not 33 <= ord(char) <= 126 for char in key):
                raise ImportErrorDetail(
                    "Open-nøkkelen inneholder mellomrom, linjeskift eller ugyldige tegn. "
                    "Lagre bare selve nøkkelen."
                )
            client = OpenProducts(key)
        if args.command == "probe":
            probe(client, args.varenummer)
            return 0
        if args.command == "cellar" and not args.database.is_file():
            raise ImportErrorDetail("Fant ingen katalog. Kjør sync først.")
        args.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(args.database) as db:
            if args.command == "sync":
                db.executescript(SCHEMA)
                sync(db, client, args.full, args.page_size)
            else:
                cellar_report(db, args.csv)
        return 0
    except ZoneInfoNotFoundError:
        print("Installer tidssonedata med python -m pip install -r requirements.txt.", file=sys.stderr)
    except (ImportErrorDetail, sqlite3.Error, OSError, UnicodeError, csv.Error) as error:
        print(error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
