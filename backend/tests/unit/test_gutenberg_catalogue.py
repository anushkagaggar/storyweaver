"""data_pipeline/flows/gutenberg_catalogue.py: picking children's tales out of Gutenberg's catalogue CSV, and
fetching that CSV politely. The network is replaced by httpx's mock transport: nothing contacts Gutenberg."""

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[3] / "data_pipeline" / "flows" / "gutenberg_catalogue.py"
_spec = importlib.util.spec_from_file_location("gutenberg_catalogue", _PATH)
assert _spec is not None and _spec.loader is not None
cat = importlib.util.module_from_spec(_spec)
sys.modules["gutenberg_catalogue"] = cat
_spec.loader.exec_module(cat)

HEADER = ["Text#", "Type", "Issued", "Title", "Language", "Authors", "Subjects", "LoCC", "Bookshelves"]


def _row(
    number: int,
    title: str,
    *,
    kind: str = "Text",
    language: str = "en",
    subjects: str = "",
    shelves: str = "",
) -> list[str]:
    return [
        str(number),
        kind,
        "2008-06-27",
        title,
        language,
        "Someone, Anne, 1800-1880",
        subjects,
        "PZ",
        shelves,
    ]


ROWS = [
    _row(
        11,
        "Alice's Adventures in Wonderland",
        subjects="Fantasy fiction; Children's stories; Alice (Fictitious character) -- Juvenile fiction",
        shelves="Children's Literature; Movie Books",
    ),
    _row(2591, "Grimms' Fairy Tales", subjects="Fairy tales -- Germany"),
    _row(21, "Aesop's Fables", subjects="Fables, Greek -- Translations into English"),
    _row(900, "Adventure on the Hill", subjects="Adventure stories", shelves="Children's Fiction"),
    _row(1234, "Contes de fées", language="fr", subjects="Fairy tales"),
    _row(5678, "A Bilingual Reader", language="en; fr", subjects="Fairy tales"),
    _row(31, "Fairy Tales, Read Aloud", kind="Sound", subjects="Fairy tales"),
    _row(41, "A Study of Fairy Tales", subjects="Fairy tales -- History and criticism"),
    _row(2701, "Moby Dick; Or, The Whale", subjects="Whaling -- Fiction; Adventure stories"),
    _row(51, "The Tale of\nthe  Lost Mitten", subjects="Folklore -- Russia"),
]


def _csv_text(rows: list[list[str]], header: list[str] | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header or HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def _write_catalogue(path: Path, rows: list[list[str]] | None = None, *, bom: bool = False) -> Path:
    text = _csv_text(rows if rows is not None else ROWS)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


# ------------------------------------------------------------------------------------------ selection


def test_children_tales_are_picked_and_everything_else_is_left(tmp_path: Path) -> None:
    books = list(cat.read_catalogue(_write_catalogue(tmp_path / "pg_catalog.csv")))

    picked = [book.gutenberg_id for book in cat.select_tales(iter(books))]

    # Alice (subject), Grimm (fairy tales), Aesop (fables), 900 (bookshelf only), 51 (folklore).
    assert picked == [11, 21, 51, 900, 2591]


@pytest.mark.parametrize(
    ("number", "reason"),
    [
        (1234, "French"),
        (5678, "English and French together"),
        (31, "an audiobook, not a text"),
        (41, "a book about fairy tales"),
        (2701, "no tale genre"),
    ],
)
def test_each_exclusion_has_its_own_reason(tmp_path: Path, number: int, reason: str) -> None:
    books = {book.gutenberg_id: book for book in cat.read_catalogue(_write_catalogue(tmp_path / "c.csv"))}

    assert not cat.is_children_tale(books[number]), reason


def test_a_byte_order_mark_and_line_breaks_in_titles_are_handled(tmp_path: Path) -> None:
    books = {
        book.gutenberg_id: book for book in cat.read_catalogue(_write_catalogue(tmp_path / "c.csv", bom=True))
    }

    assert books[11].title == "Alice's Adventures in Wonderland"  # no stray BOM glued to the first column
    assert books[51].title == "The Tale of the Lost Mitten"
    assert books[11].bookshelves == ("Children's Literature", "Movie Books")
    assert books[11].authors == ("Someone, Anne, 1800-1880",)


def test_damaged_rows_are_skipped_not_fatal(tmp_path: Path) -> None:
    rows = [
        *ROWS,
        ["", "", "", "", "", "", "", "", ""],
        ["not a number", "Text", "", "Odd", "en", "", "", "", ""],
    ]
    path = _write_catalogue(tmp_path / "c.csv", rows)

    assert len(list(cat.read_catalogue(path))) == len(ROWS)


def test_a_catalogue_with_a_renamed_column_stops_with_a_clear_message(tmp_path: Path) -> None:
    renamed = [("Subject" if name == "Subjects" else name) for name in HEADER]
    path = tmp_path / "c.csv"
    path.write_text(_csv_text([], header=renamed), encoding="utf-8")

    with pytest.raises(cat.CatalogueFormatError, match="Subjects"):
        list(cat.read_catalogue(path))


def test_columns_may_come_in_any_order(tmp_path: Path) -> None:
    order = list(reversed(range(len(HEADER))))
    header = [HEADER[i] for i in order]
    rows = [[row[i] for i in order] for row in ROWS]
    path = tmp_path / "c.csv"
    path.write_text(_csv_text(rows, header=header), encoding="utf-8")

    assert [b.gutenberg_id for b in cat.select_tales(cat.read_catalogue(path))] == [11, 21, 51, 900, 2591]


def test_the_selection_is_written_with_its_lineage(tmp_path: Path) -> None:
    catalogue = _write_catalogue(tmp_path / "pg_catalog.csv")

    assert cat.run(tmp_path / "out", catalogue=catalogue, refresh=False) == 0

    lines = (tmp_path / "out" / "tales.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["tale_id"] == "gutenberg:11"
    assert first["licence"] == "public-domain-us"
    assert first["source"] == "gutenberg"
    meta = json.loads((tmp_path / "out" / "tales.meta.json").read_text(encoding="utf-8"))
    assert meta["selected"] == 5
    assert meta["catalogue_rows"] == len(ROWS)
    assert meta["catalogue_sha256"] == cat.sha256_of(catalogue)
    assert meta["selection_rules_version"] == cat.SELECTION_RULES_VERSION


# ----------------------------------------------------------------------------------------------- fetch


class _Site:
    """A fake Gutenberg: answers like the scripted response and records the requests it got."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.response

        return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_fresh_copy_means_no_request_at_all(tmp_path: Path) -> None:
    dest = _write_catalogue(tmp_path / "pg_catalog.csv")
    site = _Site(httpx.Response(200, content=b"never used"))

    path = cat.fetch_catalogue(dest, client=site.client(), now=lambda: dest.stat().st_mtime + 3600)

    assert path == dest
    assert site.requests == []


def test_a_first_download_is_saved_and_identifies_itself(tmp_path: Path) -> None:
    dest = tmp_path / "pg_catalog.csv"
    site = _Site(httpx.Response(200, content=b"Text#,Type\n", headers={"ETag": '"abc"'}))

    cat.fetch_catalogue(dest, client=site.client())

    assert dest.read_bytes() == b"Text#,Type\n"
    assert dest.with_suffix(".etag").read_text(encoding="utf-8") == '"abc"'
    assert not dest.with_suffix(".part").exists()
    (request,) = site.requests
    assert str(request.url) == cat.CATALOGUE_URL
    assert request.headers["user-agent"] == cat.USER_AGENT
    assert "if-none-match" not in request.headers


def test_a_stale_copy_is_rechecked_and_an_unchanged_one_is_not_downloaded_again(tmp_path: Path) -> None:
    dest = _write_catalogue(tmp_path / "pg_catalog.csv")
    dest.with_suffix(".etag").write_text('"abc"', encoding="utf-8")
    now = dest.stat().st_mtime + 30 * 86_400
    original = dest.read_bytes()
    site = _Site(httpx.Response(304))

    path = cat.fetch_catalogue(dest, client=site.client(), now=lambda: now)

    assert path.read_bytes() == original
    (request,) = site.requests
    assert request.headers["if-none-match"] == '"abc"'


def test_a_changed_catalogue_replaces_the_old_copy(tmp_path: Path) -> None:
    dest = _write_catalogue(tmp_path / "pg_catalog.csv")
    now = dest.stat().st_mtime + 30 * 86_400
    site = _Site(httpx.Response(200, content=b"new,file\n", headers={"ETag": '"def"'}))

    cat.fetch_catalogue(dest, client=site.client(), now=lambda: now)

    assert dest.read_bytes() == b"new,file\n"
    assert dest.with_suffix(".etag").read_text(encoding="utf-8") == '"def"'


def test_refresh_asks_even_when_the_copy_is_fresh(tmp_path: Path) -> None:
    dest = _write_catalogue(tmp_path / "pg_catalog.csv")
    site = _Site(httpx.Response(304))

    cat.fetch_catalogue(dest, refresh=True, client=site.client())

    assert len(site.requests) == 1


def test_a_busy_site_stops_the_run_with_its_retry_time_and_keeps_the_old_copy(tmp_path: Path) -> None:
    dest = _write_catalogue(tmp_path / "pg_catalog.csv")
    now = dest.stat().st_mtime + 30 * 86_400
    original = dest.read_bytes()
    site = _Site(httpx.Response(503, headers={"Retry-After": "21600"}, text="Down for maintenance"))

    with pytest.raises(cat.SiteBusyError, match=r"6\.0 h") as caught:
        cat.fetch_catalogue(dest, client=site.client(), now=lambda: now)

    assert caught.value.retry_after_seconds == 21600
    assert len(site.requests) == 1  # asked once, did not retry
    assert dest.read_bytes() == original
    assert not dest.with_suffix(".part").exists()


def test_a_busy_site_without_a_retry_time_is_still_a_clean_stop(tmp_path: Path) -> None:
    site = _Site(httpx.Response(429))

    with pytest.raises(cat.SiteBusyError, match="later"):
        cat.fetch_catalogue(tmp_path / "pg_catalog.csv", client=site.client())


def test_any_other_error_is_not_saved_as_a_catalogue(tmp_path: Path) -> None:
    dest = tmp_path / "pg_catalog.csv"
    site = _Site(httpx.Response(500, text="oops"))

    with pytest.raises(httpx.HTTPStatusError):
        cat.fetch_catalogue(dest, client=site.client())

    assert list(tmp_path.iterdir()) == []


def test_main_reports_a_busy_site_with_a_distinct_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_args: object, **_kwargs: object) -> Path:
        raise cat.SiteBusyError(503, 21600)

    monkeypatch.setattr(cat, "fetch_catalogue", busy)

    assert cat.main(["--out", str(tmp_path)]) == 2


def test_main_reports_a_changed_format_with_a_distinct_exit_code(tmp_path: Path) -> None:
    broken = tmp_path / "broken.csv"
    broken.write_text("Text#,Title\n1,Hi\n", encoding="utf-8")

    assert cat.main(["--out", str(tmp_path / "out"), "--catalogue", str(broken)]) == 3
