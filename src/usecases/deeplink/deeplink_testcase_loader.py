"""Deeplink test-case model and Excel loader (use-case-specific).

The Excel workbook is the source of truth for the deeplink suite: each data row
becomes a ``DeeplinkTestCase``. The parser is intentionally tolerant of layout -
it recognises a header row by name and maps columns accordingly, falling back to
a fixed positional layout when no header is present. This schema (Deep Link,
Expected Result, Installed, ...) is the Deeplink use case's own contract; the
model never decides any of it.
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# Test cases (Excel is the source of truth; keep it simple: 4 columns)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeeplinkTestCase:
    """One row of the Excel: the four core columns plus the deterministic
    INSTALLED scenario selector (optional, defaults to installed=True)."""

    test_id: str
    deep_link: str
    user_type: str
    expected_result: str
    # Deterministic scenario selector from the Excel INSTALLED column. Absent or
    # blank preserves the legacy contract (installed=True). The model NEVER
    # decides this - it comes straight from the workbook.
    installed: bool = True


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# Relationship namespaces used to resolve the workbook's first worksheet part:
# the r:id attribute on <sheet> and the package-relationship <Relationship>.
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# Fallback worksheet member, used only when the workbook relationships cannot be
# resolved (e.g. a minimal package without workbook.xml). This preserves the
# historical behavior for such inputs.
_FALLBACK_SHEET = "xl/worksheets/sheet1.xml"
# Positional fallback used only when no header row is recognised; otherwise
# columns are mapped by header name (see _map_header).
_COLUMNS = {
    "A": "test_id",
    "B": "deep_link",
    "C": "user_type",
    "D": "expected_result",
    "E": "installed",
}
# Header-name synonyms per logical field (casefolded, whitespace-collapsed).
# Matching is substring-based and tolerant so real-world headers like
# "Launch URL", "Expected Screen" or "License" map without exact-string coupling.
_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "test_id": ("test case id", "test id", "testid", "test case", "testcase", "case id"),
    "deep_link": ("launch url", "deep link", "deeplink", "launch link", "url", "link", "launch"),
    "user_type": ("license", "licence", "account", "user type", "user", "persona", "plan", "subscription"),
    "expected_result": ("expected screen", "expected result", "expected", "result", "screen"),
    "installed": ("installed", "install state", "app installed", "fresh", "uninstalled"),
}
# Only explicit negatives mean a fresh/uninstalled first-open scenario; anything
# else (including blank/unknown, "yes", "true") means the app is installed.
_INSTALLED_FALSE = {"false", "f", "no", "n", "0", "uninstalled", "not installed", "fresh"}
# Deterministic signal (not model-driven) that a deeplink targets the genuine
# first-open-after-install experience: the URL asks the app store to open on
# load, which only makes sense when the app is not yet installed.
_APP_STORE_ON_LOAD = "openappstoreonload=true"


def _parse_installed(raw: str) -> bool:
    return (raw or "").strip().casefold() not in _INSTALLED_FALSE


def _derive_installed(deep_link: str) -> bool:
    """Deterministically infer the INSTALLED scenario from the deeplink when the
    workbook has no explicit INSTALLED column. A deeplink that routes to the app
    store on load is a first-open-after-install (uninstalled) case."""
    return _APP_STORE_ON_LOAD not in (deep_link or "").casefold()


def _normalize_label(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _match_field(label: str) -> str | None:
    """Map a header cell label to a logical field, preferring the most specific
    (longest) synonym match so e.g. 'Expected Screen' maps to expected_result
    rather than the shorter 'screen'."""
    norm = _normalize_label(label)
    if not norm:
        return None
    best_field: str | None = None
    best_len = 0
    for logical_field, synonyms in _FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if synonym == norm or synonym in norm:
                if len(synonym) > best_len:
                    best_field, best_len = logical_field, len(synonym)
    return best_field


def _column_letter(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def _first_worksheet_member(archive: zipfile.ZipFile) -> str:
    """Resolve the archive member holding the workbook's FIRST worksheet.

    A valid ``.xlsx`` does not have to store its first sheet as
    ``xl/worksheets/sheet1.xml``: the first ``<sheet>`` in ``xl/workbook.xml``
    references a relationship id, which ``xl/_rels/workbook.xml.rels`` maps to
    the actual worksheet part (which may be ``sheet2.xml`` etc.). Resolve it
    through that metadata and fall back to ``sheet1.xml`` when the relationship
    cannot be determined, so minimal/legacy packages keep working unchanged.
    """
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return _FALLBACK_SHEET
    sheets = workbook.find(f"{_SHEET_NS}sheets")
    first_sheet = sheets.find(f"{_SHEET_NS}sheet") if sheets is not None else None
    rel_id = first_sheet.get(f"{_REL_NS}id") if first_sheet is not None else None
    if not rel_id:
        return _FALLBACK_SHEET
    target = None
    for rel in rels.findall(f"{_PKG_REL_NS}Relationship"):
        if rel.get("Id") == rel_id:
            target = rel.get("Target")
            break
    if not target:
        return _FALLBACK_SHEET
    # Relationship targets are relative to the workbook part (in ``xl/``); an
    # absolute "/xl/..." target is relative to the package root.
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    strings: list[str] = []
    for si in root.findall(f"{_SHEET_NS}si"):
        strings.append("".join(t.text or "" for t in si.iter(f"{_SHEET_NS}t")))
    return strings


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        node = cell.find(f"{_SHEET_NS}is")
        return "".join(t.text or "" for t in node.iter(f"{_SHEET_NS}t")) if node is not None else ""
    value = cell.find(f"{_SHEET_NS}v")
    text = value.text if value is not None else ""
    if text is None:
        return ""
    if cell_type == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return ""
    return text


def _row_cells(row: ET.Element, shared: list[str]) -> dict[str, str]:
    """Read ALL populated cells of a row as {column_letter: stripped_value}."""
    cells: dict[str, str] = {}
    for cell in row.findall(f"{_SHEET_NS}c"):
        letter = _column_letter(cell.get("r", ""))
        if letter:
            cells[letter] = _cell_value(cell, shared).strip()
    return cells


def _looks_like_data(cells: dict[str, str]) -> bool:
    # A data row carries an actual deeplink (has a URL scheme); header/title rows
    # do not. This cleanly separates the first data row from any leading
    # title/header rows without depending on exact positions.
    return any("://" in value for value in cells.values())


def _map_header(cells: dict[str, str]) -> dict[str, str]:
    """Map header cells to logical fields: {column_letter: field}. Each field is
    assigned at most once (first, most-specific match wins)."""
    mapping: dict[str, str] = {}
    assigned: set[str] = set()
    # Resolve per-cell best field, then assign in column order avoiding clashes.
    scored = [
        (letter, _match_field(label)) for letter, label in cells.items()
    ]
    for letter, logical_field in sorted(scored, key=lambda item: item[0]):
        if logical_field and logical_field not in assigned:
            mapping[letter] = logical_field
            assigned.add(logical_field)
    return mapping


def _is_usable_header(mapping: dict[str, str]) -> bool:
    fields = set(mapping.values())
    return "deep_link" in fields and "expected_result" in fields


def load_deeplink_cases(path: str | Path) -> list[DeeplinkTestCase]:
    """Load deeplink test cases from the Excel workbook (stdlib only).

    The parser is intentionally tolerant of layout. It recognises a header row by
    name (e.g. "Launch URL", "Expected Screen", "License") and maps columns
    accordingly, so leading title rows, renamed/reordered columns and optional
    extra columns are all handled. When no header is present it falls back to the
    positional layout A=Test ID, B=Deep Link, C=User Type, D=Expected Result.

    Every data row must provide a Test ID, a Deep Link and an Expected Result.
    The INSTALLED scenario is taken from an explicit INSTALLED column when
    present, otherwise derived deterministically from the deeplink.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        member = _first_worksheet_member(archive)
        try:
            sheet_bytes = archive.read(member)
        except KeyError as error:
            # A valid ZIP/XLSX that lacks the resolved worksheet member (e.g. a
            # non-workbook zip, or a workbook whose relationships point at a
            # missing part). Surface a stable domain-level error instead of
            # leaking zip-internal KeyError, so the CLI reports it cleanly
            # (exit 2) rather than a traceback.
            raise ValueError(
                f"{path} is not a readable deeplink workbook: expected worksheet "
                f"'{member}' was not found"
            ) from error
        sheet = ET.fromstring(sheet_bytes)

    rows = [
        cells
        for cells in (_row_cells(row, shared) for row in sheet.iter(f"{_SHEET_NS}row"))
        if any(cells.values())
    ]

    # Recognise a header among the leading non-data rows (title rows are simply
    # ignored: they map too few fields to be a usable header).
    column_map = _COLUMNS
    data_start = 0
    for index, cells in enumerate(rows):
        if _looks_like_data(cells):
            data_start = index
            break
        candidate = _map_header(cells)
        if _is_usable_header(candidate):
            column_map = candidate
    else:
        # No data row found (only title/header rows, or empty sheet).
        data_start = len(rows)

    cases: list[DeeplinkTestCase] = []
    for cells in rows[data_start:]:
        values = {
            logical_field: cells.get(letter, "")
            for letter, logical_field in column_map.items()
        }
        test_id = values.get("test_id", "")
        deep_link = values.get("deep_link", "")
        expected = values.get("expected_result", "")
        missing = [
            name
            for name, present in (
                ("Test ID", test_id),
                ("Deep Link", deep_link),
                ("Expected Result", expected),
            )
            if not present
        ]
        if missing:
            raise ValueError(
                f"Deeplink test row is missing required column(s): {', '.join(missing)}"
            )
        installed_raw = values.get("installed", "")
        installed = (
            _parse_installed(installed_raw)
            if installed_raw
            else _derive_installed(deep_link)
        )
        cases.append(
            DeeplinkTestCase(
                test_id=test_id,
                deep_link=deep_link,
                user_type=values.get("user_type", ""),
                expected_result=expected,
                installed=installed,
            )
        )
    if not cases:
        raise ValueError(f"No deeplink test cases found in {path}")
    return cases
