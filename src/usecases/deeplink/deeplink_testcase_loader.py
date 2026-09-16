"""Deeplink Excel testcase loading."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .supported_links import SUPPORTED_LINK_DOMAINS


@dataclass(frozen=True)
class DeeplinkTestCase:
    """One deeplink testcase row."""

    test_id: str
    deep_link: str
    user_type: str
    expected_result: str
    installed: bool = True

    @property
    def license(self) -> str:
        """Return the workbook's License value without changing its spelling."""
        return self.user_type


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_FALLBACK_SHEET = "xl/worksheets/sheet1.xml"
_COLUMNS = {
    "A": "test_id",
    "B": "deep_link",
    "C": "user_type",
    "D": "expected_result",
    "E": "installed",
}
_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "test_id": ("test case id", "test id", "testid", "test case", "testcase", "case id"),
    "deep_link": ("launch url", "deep link", "deeplink", "launch link", "url", "link", "launch"),
    "user_type": ("license", "licence", "account", "user type", "user", "persona", "plan", "subscription"),
    "expected_result": ("expected screen", "expected result", "expected", "result", "screen"),
    "installed": ("installed", "install state", "app installed", "fresh", "uninstalled"),
}
_SPECIFIC_DEEP_LINK_SYNONYMS = {
    "launch url",
    "deep link",
    "deeplink",
    "launch link",
}
_INSTALLED_TRUE = {"true", "t", "yes", "y", "1", "installed"}
_INSTALLED_FALSE = {"false", "f", "no", "n", "0", "uninstalled", "not installed", "fresh"}
_REQUIRED_FIELDS = ("test_id", "deep_link", "user_type", "expected_result")
_FIELD_DISPLAY = {
    "test_id": "Test ID",
    "deep_link": "Deep Link",
    "user_type": "License",
    "expected_result": "Expected Result",
    "installed": "Installed",
}
_ALLOWED_SCHEMES = {"https"}
_APP_STORE_ON_LOAD = "openappstoreonload=true"


def _parse_installed(raw: str) -> bool:
    value = (raw or "").strip().casefold()
    if value in _INSTALLED_TRUE:
        return True
    if value in _INSTALLED_FALSE:
        return False
    raise ValueError(
        "Installed must be one of: "
        + ", ".join(sorted(_INSTALLED_TRUE | _INSTALLED_FALSE))
    )


def _derive_installed(deep_link: str) -> bool:
    """Infer the installed scenario from the deeplink."""
    return _APP_STORE_ON_LOAD not in (deep_link or "").casefold()


def _normalize_label(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _field_match(label: str) -> tuple[str, int] | None:
    """Map a header cell label to a logical field."""
    norm = _normalize_label(label)
    if not norm:
        return None
    best_field: str | None = None
    best_synonym: str | None = None
    best_len = 0
    for logical_field, synonyms in _FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if synonym == norm or synonym in norm:
                if len(synonym) > best_len:
                    best_field = logical_field
                    best_synonym = synonym
                    best_len = len(synonym)
    if best_field is None:
        return None
    if best_field == "deep_link":
        return (
            best_field,
            2 if best_synonym in _SPECIFIC_DEEP_LINK_SYNONYMS else 1,
        )
    return best_field, best_len


def _match_field(label: str) -> str | None:
    match = _field_match(label)
    return match[0] if match is not None else None


def _column_letter(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def _first_worksheet_member(archive: zipfile.ZipFile) -> str:
    """Resolve the archive member for the first worksheet."""
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
    return any("://" in value for value in cells.values())


def _map_header(
    cells: dict[str, str],
    *,
    row_number: int | None = None,
) -> dict[str, str]:
    """Map header cells to logical fields."""
    mapping: dict[str, str] = {}
    assigned: set[str] = set()
    deep_link_candidates: list[tuple[int, str]] = []
    for letter, label in sorted(cells.items()):
        match = _field_match(label)
        if match is None:
            continue
        logical_field, score = match
        if logical_field == "deep_link":
            deep_link_candidates.append((score, letter))
        elif logical_field not in assigned:
            mapping[letter] = logical_field
            assigned.add(logical_field)

    ambiguous_letters: list[str] = []
    if deep_link_candidates:
        best_score = max(score for score, _letter in deep_link_candidates)
        best_letters = sorted(
            letter
            for score, letter in deep_link_candidates
            if score == best_score
        )
        mapping[best_letters[0]] = "deep_link"
        if len(best_letters) > 1:
            ambiguous_letters = best_letters

    recognized_required = set(mapping.values()) & set(_REQUIRED_FIELDS)
    if ambiguous_letters and len(recognized_required) >= 2:
        location = (
            f" row {row_number}" if row_number is not None else ""
        )
        raise ValueError(
            f"Workbook header{location} has ambiguous columns: "
            f"{_FIELD_DISPLAY['deep_link']} "
            f"({', '.join(ambiguous_letters)})"
        )
    return mapping


def load_deeplink_cases(path: str | Path) -> list[DeeplinkTestCase]:
    """Load deeplink test cases from an Excel workbook."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        member = _first_worksheet_member(archive)
        try:
            sheet_bytes = archive.read(member)
        except KeyError as error:
            raise ValueError(
                f"{path} is not a readable deeplink workbook: expected worksheet "
                f"'{member}' was not found"
            ) from error
        sheet = ET.fromstring(sheet_bytes)

    rows = []
    for fallback_number, row in enumerate(
        sheet.iter(f"{_SHEET_NS}row"), start=1
    ):
        cells = _row_cells(row, shared)
        if any(cells.values()):
            raw_number = row.get("r", "")
            row_number = int(raw_number) if raw_number.isdigit() else fallback_number
            rows.append((row_number, cells))

    column_map = _COLUMNS
    data_start = 0
    header_row: int | None = None
    for index, (row_number, cells) in enumerate(rows):
        if _looks_like_data(cells):
            data_start = index
            break
        candidate = _map_header(cells, row_number=row_number)
        if len(set(candidate.values()) & set(_REQUIRED_FIELDS)) >= 2:
            column_map = candidate
            header_row = row_number
    else:
        data_start = len(rows)

    if header_row is not None:
        missing_headers = [
            _FIELD_DISPLAY[field]
            for field in _REQUIRED_FIELDS
            if field not in column_map.values()
        ]
        if missing_headers:
            raise ValueError(
                f"Workbook header row {header_row} is missing required column(s): "
                + ", ".join(missing_headers)
            )

    cases: list[DeeplinkTestCase] = []
    seen_ids: dict[str, int] = {}
    field_columns = {
        logical_field: letter for letter, logical_field in column_map.items()
    }
    for row_number, cells in rows[data_start:]:
        values = {
            logical_field: cells.get(letter, "")
            for letter, logical_field in column_map.items()
        }
        test_id = values.get("test_id", "")
        deep_link = values.get("deep_link", "")
        license_name = values.get("user_type", "")
        expected = values.get("expected_result", "")
        missing = [
            name
            for name, present in (
                ("Test ID", test_id),
                ("Deep Link", deep_link),
                ("License", license_name),
                ("Expected Result", expected),
            )
            if not present
        ]
        if missing:
            contextual = [
                f"{name} (column {field_columns.get(field, '?')})"
                for field, name, present in (
                    ("test_id", "Test ID", test_id),
                    ("deep_link", "Deep Link", deep_link),
                    ("user_type", "License", license_name),
                    ("expected_result", "Expected Result", expected),
                )
                if not present
            ]
            raise ValueError(
                f"Workbook row {row_number} is missing required value(s): "
                + ", ".join(contextual)
            )
        normalized_id = test_id.casefold()
        if normalized_id in seen_ids:
            raise ValueError(
                f"Workbook row {row_number}, column "
                f"{field_columns.get('test_id', '?')} has duplicate Test ID "
                f"{test_id!r}; first defined on row {seen_ids[normalized_id]}"
            )
        seen_ids[normalized_id] = row_number

        parsed_link = urlsplit(deep_link)
        deep_link_column = field_columns.get("deep_link", "?")
        if parsed_link.scheme.casefold() not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"Workbook row {row_number}, column {deep_link_column} has "
                f"unsupported deeplink scheme {parsed_link.scheme!r}; "
                "only https is allowed"
            )
        hostname = (parsed_link.hostname or "").casefold()
        if hostname not in {
            domain.casefold() for domain in SUPPORTED_LINK_DOMAINS
        }:
            raise ValueError(
                f"Workbook row {row_number}, column {deep_link_column} has "
                f"unsupported deeplink host {hostname!r}; allowed hosts: "
                + ", ".join(SUPPORTED_LINK_DOMAINS)
            )
        installed_raw = values.get("installed", "")
        if installed_raw:
            try:
                installed = _parse_installed(installed_raw)
            except ValueError as error:
                raise ValueError(
                    f"Workbook row {row_number}, column "
                    f"{field_columns.get('installed', '?')} is invalid: {error}"
                ) from error
        else:
            installed = _derive_installed(deep_link)
        cases.append(
            DeeplinkTestCase(
                test_id=test_id,
                deep_link=deep_link,
                user_type=license_name,
                expected_result=expected,
                installed=installed,
            )
        )
    if not cases:
        raise ValueError(f"No deeplink test cases found in {path}")
    return cases
