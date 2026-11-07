"""Dependency-free table readers used by the mock executor.

The mock executor has to turn a real spreadsheet into a real CSV without
pulling in a heavyweight dependency, so XLSX is parsed directly from the OOXML
package with the standard library only.  ``openpyxl`` is used when it happens
to be installed, because it understands more of the format.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

from xml.etree import ElementTree

_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}

_CELL_REF = re.compile(r"^([A-Z]+)\d+$")


class TableReadError(Exception):
    """Raised when a table file cannot be read as a table."""


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref.upper())
    if not match:
        return -1
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(payload)
    strings: list[str] = []
    for item in root.findall("main:si", _NS):
        parts = [node.text or "" for node in item.iter("{%s}t" % _NS["main"])]
        strings.append("".join(parts))
    return strings


def _read_xlsx(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        sheets = sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise TableReadError(f"{path} contains no worksheet")
        payload = archive.read(sheets[0])
    root = ElementTree.fromstring(payload)
    rows: list[list[str]] = []
    for row in root.iter("{%s}row" % _NS["main"]):
        cells: dict[int, str] = {}
        for cell in row.findall("main:c", _NS):
            index = _column_index(cell.get("r", "")) if cell.get("r") else len(cells)
            if index < 0:
                index = len(cells)
            kind = cell.get("t", "n")
            if kind == "s":
                node = cell.find("main:v", _NS)
                value = ""
                if node is not None and node.text is not None:
                    try:
                        value = shared[int(node.text)]
                    except (ValueError, IndexError):
                        value = ""
            elif kind == "inlineStr":
                node = cell.find("main:is", _NS)
                value = "".join(t.text or "" for t in node.iter("{%s}t" % _NS["main"])) if node is not None else ""
            else:
                node = cell.find("main:v", _NS)
                value = node.text or "" if node is not None else ""
            cells[index] = value
        if not cells:
            rows.append([])
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _read_csv(path: Path, delimiter: str | None = None) -> list[list[str]]:
    payload = path.read_bytes()
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise TableReadError(f"{path} could not be decoded as text")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _read_json(path: Path) -> list[list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        records = [data]
    elif isinstance(data, list):
        records = data
    else:
        raise TableReadError(f"{path} does not contain a table")
    if not records:
        return []
    if all(isinstance(r, dict) for r in records):
        headers: list[str] = []
        for record in records:
            for key in record:
                if key not in headers:
                    headers.append(str(key))
        rows = [headers]
        for record in records:
            rows.append(["" if record.get(h) is None else str(record.get(h)) for h in headers])
        return rows
    rows = []
    for record in records:
        if isinstance(record, (list, tuple)):
            rows.append(["" if v is None else str(v) for v in record])
        else:
            rows.append([str(record)])
    return rows


def read_table(path: Path, delimiter: str | None = None) -> list[list[str]]:
    """Read a CSV/TSV/JSON/XLSX file into a list of rows."""
    path = Path(path)
    suffix = path.suffix.casefold()
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook  # type: ignore
        except ImportError:
            return _read_xlsx(path)
        workbook = load_workbook(filename=path, read_only=True, data_only=True)
        try:
            sheet = workbook[workbook.sheetnames[0]]
            return [["" if cell is None else str(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
        finally:
            workbook.close()
    if suffix == ".json":
        return _read_json(path)
    if suffix in (".csv", ".tsv", ".txt", ""):
        if suffix == ".tsv" and delimiter is None:
            delimiter = "\t"
        return _read_csv(path, delimiter)
    raise TableReadError(f"unsupported table format {suffix!r}")


def write_csv(rows: list[list[str]], path: Path, delimiter: str = ",", encoding: str = "utf-8-sig") -> None:
    """Write rows as a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=encoding) as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        for row in rows:
            writer.writerow(row)
