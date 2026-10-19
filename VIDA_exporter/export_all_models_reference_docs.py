#!/usr/bin/env python3
"""
export_static_service_docs.py

Export Volvo reference and service documents for all models and years from the
servicerep_en-US SQL Server database into Markdown, HTML, and XML.

What it does
------------
- Exports documents for all models and years.
- Includes i2, sbull, servinfosub, and torqueinfo document types.
- Preserves direct and navigation-context vehicle applicability metadata.
- Decompresses Document.XmlContent ZIP blobs in memory.
- Parses the embedded XML.
- Converts common VIDA XML structures to:
    * Markdown files for RAG ingestion
    * Standalone HTML files for browser preview
- Preserves XML element IDs as anchors.
- Converts VIDA cross-document references into relative links.
- Resolves image references to the previously extracted image folders.
- Adds vehicle applicability from servicerep_en-US and basedata.
- Retains the original extracted XML for inspection.
- Writes a manifest and unresolved-reference logs.

Dependencies
------------
    pip install pyodbc

The Microsoft ODBC Driver for SQL Server must also be installed.

Tested design assumptions
-------------------------
- SQL Server instance is local.
- Database name is servicerep_en-US.
- Images are already extracted under:
      D:\\Volvo DB\\Extracted\\All
- Image references in XML contain exact filenames such as:
      0900c8af84aa37cd_80_60.gif
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
import xml.etree.ElementTree as ET

import pyodbc


# ============================================================
# Configuration
# ============================================================

SERVER = r"localhost"
DATABASE = "servicerep_en-US"

OUTPUT_ROOT = Path(r"D:\Volvo DB\Extracted\All_Models_ServiceExport")
IMAGE_ROOT = Path(r"D:\\Volvo DB\\Extracted\\All")

# No sample limit: export the complete eligible static collection.
# Keep the first version focused on static instructional content.
ALLOWED_DOCUMENT_TYPES = (
    "i2",
    "sbull",
    "servinfosub",
    "torqueinfo",
)

CONNECTION_STRING = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={SERVER};"
    f"DATABASE={DATABASE};"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

MARKDOWN_ROOT = OUTPUT_ROOT / "markdown"
HTML_ROOT = OUTPUT_ROOT / "html"
XML_ROOT = OUTPUT_ROOT / "xml"
ASSET_ROOT = HTML_ROOT / "assets"

MANIFEST_PATH = OUTPUT_ROOT / "manifest.csv"
UNRESOLVED_IMAGES_PATH = OUTPUT_ROOT / "unresolved-images.csv"
UNRESOLVED_LINKS_PATH = OUTPUT_ROOT / "unresolved-links.csv"


# ============================================================
# Data model
# ============================================================

@dataclass
class VehicleApplicability:
    source: str
    profile_id: str
    description: str
    folder_level: Optional[int]
    vehicle_model: str
    model_year: str
    engine: str
    transmission: str
    body_style: str
    steering: str
    suspension: str
    brake_system: str
    chassis_no_from: Optional[int]
    chassis_no_to: Optional[int]
    partner_groups: list[str] = field(default_factory=list)


@dataclass
class DocumentRecord:
    document_id: int
    chronicle_id: str
    project_document_id: str
    vcc_number: str
    title: str
    database_path: str
    document_type: str
    function_group_1: str
    function_group_1_title: str
    function_group_2: str
    function_group_2_title: str
    function_group_3: str
    function_group_3_title: str
    tree_item_title: str
    xml_package: bytes
    vehicle_applicability: list[VehicleApplicability] = field(default_factory=list)


@dataclass
class RenderContext:
    current_doc_key: str
    current_title: str
    unresolved_images: list[dict]
    unresolved_links: list[dict]
    reference_documents: dict[str, dict[str, str]]


# ============================================================
# Helpers
# ============================================================

def clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def safe_filename(value: str, fallback: str) -> str:
    value = clean(value)
    value = re.sub(r'[<>:"/\\|?*]+', "_", value)
    value = value.rstrip(" .")
    return value or fallback


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def element_id(element: ET.Element) -> str:
    return clean(element.attrib.get("id"))


def text_content(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return " ".join(
        part.strip()
        for part in element.itertext()
        if part and part.strip()
    ).strip()


def escape_markdown(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = re.sub(r"([*_`])", r"\\\1", text)
    return text


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def anchor_html(anchor: str) -> str:
    if not anchor:
        return ""
    return f'<a id="{html.escape(anchor, quote=True)}"></a>'


def anchor_markdown(anchor: str) -> str:
    if not anchor:
        return ""
    return f'<a id="{html.escape(anchor, quote=True)}"></a>\n'


def normalize_doc_reference(raw: str) -> tuple[str, str]:
    """
    Convert:
        en-US0900c8af808641e4#KC01111026
    into:
        ("0900c8af808641e4", "KC01111026")
    """
    raw = clean(raw)
    if "#" in raw:
        document_part, anchor = raw.split("#", 1)
    else:
        document_part, anchor = raw, ""

    document_part = re.sub(r"^[a-z]{2}-[A-Z]{2}", "", document_part)
    return document_part, anchor



PROJECT_DOCUMENT_REFERENCE_PATTERN = re.compile(
    r"(?P<raw>(?:[a-z]{2}-[A-Z]{2})?(?P<project>0[0-9a-fA-F]{15})(?:#[A-Za-z0-9_.:-]+)?)"
)

VCC_REFERENCE_PATTERN = re.compile(
    r"(?P<raw>VCC-\d+-\d+)",
    re.IGNORECASE,
)

DOCUMENT_REFERENCE_PATTERN = re.compile(
    r"(?P<raw>"
    r"(?:[a-z]{2}-[A-Z]{2})?0[0-9a-fA-F]{15}(?:#[A-Za-z0-9_.:-]+)?"
    r"|VCC-\d+-\d+"
    r")",
    re.IGNORECASE,
)


def reference_info(raw: str, ctx: RenderContext) -> Optional[dict[str, str]]:
    raw = clean(raw)

    if VCC_REFERENCE_PATTERN.fullmatch(raw):
        lookup_key = raw.upper()
        anchor = ""
        reference_kind = "vcc"
    else:
        document_alias, anchor = normalize_doc_reference(raw)
        lookup_key = document_alias.lower()
        reference_kind = "document_id"

    info = ctx.reference_documents.get(lookup_key)
    if info is None:
        return None

    result = dict(info)
    # Original XML element anchors do not reliably match generated HTML IDs.
    # Link to the document itself instead of creating a broken deep link.
    result["anchor"] = ""
    result["reference_kind"] = reference_kind
    result["matched_identifier"] = lookup_key

    if reference_kind == "vcc":
        result["vcc_number"] = raw.upper()

    return result


def reference_markdown_link(raw: str, ctx: RenderContext) -> str:
    info = reference_info(raw, ctx)
    if info is None:
        ctx.unresolved_links.append(
            {
                "document": ctx.current_doc_key,
                "title": ctx.current_title,
                "link": raw,
            }
        )
        return f"`{escape_markdown(raw)}`"

    title = (
        info.get("title")
        or info.get("vcc_number")
        or info.get("project_document_id")
        or info.get("chronicle_id")
        or info.get("matched_identifier")
        or raw
    )
    output_key = info.get("output_key") or ""
    anchor = info.get("anchor") or ""
    suffix = f"#{anchor}" if anchor else ""
    is_exported = info.get("is_exported") == "1"

    if info.get("reference_kind") == "vcc":
        identifier = info.get("vcc_number") or raw.upper()
        if is_exported and output_key:
            return (
                f"[{escape_markdown(title)}]({output_key}.md{suffix}) "
                f"(`{escape_markdown(identifier)}`)"
            )
        return (
            f"**{escape_markdown(title)}** "
            f"(`{escape_markdown(identifier)}`; target not included in this export)"
        )

    if is_exported and output_key:
        return f"[{escape_markdown(title)}]({output_key}.md{suffix})"

    identifier = (
        info.get("chronicle_id")
        or info.get("project_document_id")
        or info.get("matched_identifier")
        or raw
    )
    return (
        f"**{escape_markdown(title)}** "
        f"(`{escape_markdown(identifier)}`; target not included in this export)"
    )


def reference_html_link(raw: str, ctx: RenderContext) -> str:
    info = reference_info(raw, ctx)
    if info is None:
        ctx.unresolved_links.append(
            {
                "document": ctx.current_doc_key,
                "title": ctx.current_title,
                "link": raw,
            }
        )
        return f"<code>{html.escape(raw)}</code>"

    title = (
        info.get("title")
        or info.get("vcc_number")
        or info.get("project_document_id")
        or info.get("chronicle_id")
        or info.get("matched_identifier")
        or raw
    )
    output_key = info.get("output_key") or ""
    anchor = info.get("anchor") or ""
    suffix = f"#{anchor}" if anchor else ""
    is_exported = info.get("is_exported") == "1"

    if info.get("reference_kind") == "vcc":
        identifier = info.get("vcc_number") or raw.upper()
        if is_exported and output_key:
            href = f"{output_key}.html{suffix}"
            return (
                f'<a class="vcc-reference" '
                f'href="{html.escape(href, quote=True)}">'
                f'{html.escape(title)}</a> '
                f'<code>{html.escape(identifier)}</code>'
            )
        return (
            f'<span class="unavailable-reference">'
            f'<strong>{html.escape(title)}</strong> '
            f'<code>{html.escape(identifier)}</code> '
            f'<small>(target not included in this export)</small>'
            f'</span>'
        )

    if is_exported and output_key:
        href = f"{output_key}.html{suffix}"
        return (
            f'<a href="{html.escape(href, quote=True)}">'
            f'{html.escape(title)}</a>'
        )

    identifier = (
        info.get("chronicle_id")
        or info.get("project_document_id")
        or info.get("matched_identifier")
        or raw
    )
    return (
        f'<span class="unavailable-reference">'
        f'<strong>{html.escape(title)}</strong> '
        f'<code>{html.escape(identifier)}</code> '
        f'<small>(target not included in this export)</small>'
        f'</span>'
    )


def replace_document_references_markdown(text: str, ctx: RenderContext) -> str:
    def repl(match: re.Match[str]) -> str:
        return reference_markdown_link(match.group("raw"), ctx)
    return DOCUMENT_REFERENCE_PATTERN.sub(repl, text)


def replace_document_references_html(text: str, ctx: RenderContext) -> str:
    pieces: list[str] = []
    last = 0
    for match in DOCUMENT_REFERENCE_PATTERN.finditer(text):
        pieces.append(html.escape(text[last:match.start()]))
        pieces.append(reference_html_link(match.group("raw"), ctx))
        last = match.end()
    pieces.append(html.escape(text[last:]))
    return "".join(pieces)


def sole_document_references(text: str) -> list[str]:
    """Return references when a text node consists only of IDs/whitespace."""
    matches = list(DOCUMENT_REFERENCE_PATTERN.finditer(text))
    if not matches:
        return []
    remainder = DOCUMENT_REFERENCE_PATTERN.sub("", text)
    if remainder.strip(" \t\r\n,;|"):
        return []
    return [match.group("raw") for match in matches]

def image_folder_for_extension(extension: str) -> str:
    extension = extension.lower()
    return {
        ".gif": "GIF",
        ".jpeg": "JPEG",
        ".jpg": "JPG",
        ".cgm": "CGM",
        ".svg": "SVG",
    }.get(extension, "Unknown")


def resolve_image(filename: str) -> Optional[Path]:
    filename = Path(filename).name
    folder = IMAGE_ROOT / image_folder_for_extension(Path(filename).suffix)
    exact_path = folder / filename

    if exact_path.exists():
        return exact_path

    # Optional fallback: use the original/full-size 0_0 variant.
    match = re.match(
        r"^(?P<base>[0-9a-fA-F]+)_\d+_\d+(?P<ext>\.[^.]+)$",
        filename,
    )
    if match:
        candidate = folder / (
            f"{match.group('base')}_0_0{match.group('ext')}"
        )
        if candidate.exists():
            return candidate

    return None


def image_url(filename: str) -> str:
    """
    Return a local file URI pointing to the previously extracted
    image repository under D:\\Volvo DB\\Extracted\\All.

    Example:
        file:///D:/Volvo%20DB/Extracted/All/JPEG/example.jpeg
    """
    resolved = resolve_image(filename)

    if resolved is not None:
        return resolved.resolve().as_uri()

    expected_path = (
        IMAGE_ROOT
        / image_folder_for_extension(Path(filename).suffix)
        / Path(filename).name
    )
    return expected_path.resolve().as_uri()


def local_html_doc_link(raw: str) -> str:
    doc_key, anchor = normalize_doc_reference(raw)
    if not doc_key:
        return "#"

    suffix = f"#{anchor}" if anchor else ""
    return f"{doc_key}.html{suffix}"


def local_markdown_doc_link(raw: str) -> str:
    doc_key, anchor = normalize_doc_reference(raw)
    if not doc_key:
        return "#"

    suffix = f"#{anchor}" if anchor else ""
    return f"{doc_key}.md{suffix}"



def optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def parse_applicability_json(raw_json: object) -> list[VehicleApplicability]:
    raw_text = clean(raw_json)
    if not raw_text:
        return []

    rows = json.loads(raw_text)
    applicability: list[VehicleApplicability] = []

    for row in rows:
        partner_groups = [
            clean(value)
            for value in clean(row.get("partner_groups")).split(",")
            if clean(value)
        ]

        applicability.append(
            VehicleApplicability(
                source=clean(row.get("source")),
                profile_id=clean(row.get("profile_id")),
                description=clean(row.get("description")),
                folder_level=optional_int(row.get("folder_level")),
                vehicle_model=clean(row.get("vehicle_model")),
                model_year=clean(row.get("model_year")),
                engine=clean(row.get("engine")),
                transmission=clean(row.get("transmission")),
                body_style=clean(row.get("body_style")),
                steering=clean(row.get("steering")),
                suspension=clean(row.get("suspension")),
                brake_system=clean(row.get("brake_system")),
                chassis_no_from=optional_int(row.get("chassis_no_from")),
                chassis_no_to=optional_int(row.get("chassis_no_to")),
                partner_groups=partner_groups,
            )
        )

    # The tree joins can encounter the same profile through multiple tree paths.
    unique: dict[tuple, VehicleApplicability] = {}
    for item in applicability:
        key = (
            item.source,
            item.profile_id,
            item.description,
            tuple(item.partner_groups),
        )
        unique[key] = item

    return sorted(
        unique.values(),
        key=lambda item: (
            0 if item.source == "DocumentProfile" else 1,
            item.folder_level if item.folder_level is not None else 999,
            item.description,
            item.profile_id,
        ),
    )


def applicability_summary(record: DocumentRecord) -> str:
    descriptions: list[str] = []
    for item in record.vehicle_applicability:
        value = item.description or item.vehicle_model or item.profile_id
        if value and value not in descriptions:
            descriptions.append(value)
    return "; ".join(descriptions)


def chassis_range_text(item: VehicleApplicability) -> str:
    start = item.chassis_no_from
    end = item.chassis_no_to
    if start is not None and end is not None:
        return f"{start}-{end}"
    if start is not None:
        return f"{start}-"
    if end is not None:
        return f"-{end}"
    return ""


def applicability_html(record: DocumentRecord) -> str:
    if not record.vehicle_applicability:
        return (
            '<section class="vehicle-applicability">'
            '<h2>Vehicle applicability</h2>'
            '<p>No vehicle profile was assigned in the database.</p>'
            '</section>'
        )

    groups: dict[str, list[VehicleApplicability]] = {
        "DocumentProfile": [],
        "TreeItemProfile": [],
    }
    for item in record.vehicle_applicability:
        groups.setdefault(item.source, []).append(item)

    parts = ['<section class="vehicle-applicability">', '<h2>Vehicle applicability</h2>']
    labels = {
        "DocumentProfile": "Direct document applicability",
        "TreeItemProfile": "Navigation context applicability",
    }

    for source in ("DocumentProfile", "TreeItemProfile"):
        items = groups.get(source, [])
        if not items:
            continue
        parts.append(f"<h3>{html.escape(labels[source])}</h3><ul>")
        for item in items:
            details = []
            for label, value in (
                ("Model", item.vehicle_model),
                ("Model year", item.model_year),
                ("Engine", item.engine),
                ("Transmission", item.transmission),
                ("Body style", item.body_style),
                ("Steering", item.steering),
                ("Suspension", item.suspension),
                ("Brake system", item.brake_system),
                ("Chassis", chassis_range_text(item)),
                ("Markets", ", ".join(item.partner_groups)),
            ):
                if value:
                    details.append(f"<strong>{html.escape(label)}:</strong> {html.escape(value)}")

            heading = item.description or item.vehicle_model or item.profile_id
            parts.append("<li>")
            parts.append(f"<strong>{html.escape(heading)}</strong>")
            if details:
                parts.append("<br>" + "; ".join(details))
            parts.append(
                f'<br><small>Profile: {html.escape(item.profile_id)}'
                + (
                    f"; folder level: {item.folder_level}"
                    if item.folder_level is not None else ""
                )
                + "</small>"
            )
            parts.append("</li>")
        parts.append("</ul>")

    parts.append("</section>")
    return "".join(parts)


def applicability_markdown(record: DocumentRecord) -> str:
    lines = ["## Vehicle applicability", ""]
    if not record.vehicle_applicability:
        lines.extend(["No vehicle profile was assigned in the database.", ""])
        return "\n".join(lines)

    labels = {
        "DocumentProfile": "Direct document applicability",
        "TreeItemProfile": "Navigation context applicability",
    }

    for source in ("DocumentProfile", "TreeItemProfile"):
        items = [item for item in record.vehicle_applicability if item.source == source]
        if not items:
            continue
        lines.extend([f"### {labels[source]}", ""])
        for item in items:
            heading = item.description or item.vehicle_model or item.profile_id
            lines.append(f"- **{escape_markdown(heading)}**")
            for label, value in (
                ("Model", item.vehicle_model),
                ("Model year", item.model_year),
                ("Engine", item.engine),
                ("Transmission", item.transmission),
                ("Body style", item.body_style),
                ("Steering", item.steering),
                ("Suspension", item.suspension),
                ("Brake system", item.brake_system),
                ("Chassis", chassis_range_text(item)),
                ("Markets", ", ".join(item.partner_groups)),
                ("Profile ID", item.profile_id),
                (
                    "Folder level",
                    str(item.folder_level) if item.folder_level is not None else "",
                ),
            ):
                if value:
                    lines.append(f"  - **{label}:** {escape_markdown(value)}")
        lines.append("")

    return "\n".join(lines)


def yaml_applicability_lines(record: DocumentRecord) -> list[str]:
    lines = ["vehicle_applicability:"]
    if not record.vehicle_applicability:
        lines.append("  []")
        return lines

    for item in record.vehicle_applicability:
        lines.extend(
            [
                f"  - source: {yaml_quote(item.source)}",
                f"    profile_id: {yaml_quote(item.profile_id)}",
                f"    description: {yaml_quote(item.description)}",
                (
                    f"    folder_level: {item.folder_level}"
                    if item.folder_level is not None
                    else "    folder_level: null"
                ),
                f"    vehicle_model: {yaml_quote(item.vehicle_model)}",
                f"    model_year: {yaml_quote(item.model_year)}",
                f"    engine: {yaml_quote(item.engine)}",
                f"    transmission: {yaml_quote(item.transmission)}",
                f"    body_style: {yaml_quote(item.body_style)}",
                f"    steering: {yaml_quote(item.steering)}",
                f"    suspension: {yaml_quote(item.suspension)}",
                f"    brake_system: {yaml_quote(item.brake_system)}",
                (
                    f"    chassis_no_from: {item.chassis_no_from}"
                    if item.chassis_no_from is not None
                    else "    chassis_no_from: null"
                ),
                (
                    f"    chassis_no_to: {item.chassis_no_to}"
                    if item.chassis_no_to is not None
                    else "    chassis_no_to: null"
                ),
                "    partner_groups:",
            ]
        )
        if item.partner_groups:
            lines.extend(
                f"      - {yaml_quote(group)}"
                for group in item.partner_groups
            )
        else:
            lines.append("      []")

    return lines


# ============================================================
# Database
# ============================================================

SELECTION_SETUP_SQL = """
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#SelectedDocuments') IS NOT NULL
    DROP TABLE #SelectedDocuments;

CREATE TABLE #SelectedDocuments
(
    DocumentId int NOT NULL PRIMARY KEY,
    ProjectDocumentId char(16) NULL
);
"""

SELECT_DOCUMENTS_SQL = f"""
INSERT INTO #SelectedDocuments (DocumentId, ProjectDocumentId)
SELECT
    d.id,
    d.projectDocumentId
FROM dbo.Document AS d
INNER JOIN dbo.DocumentType AS dt
    ON dt.id = d.fkDocumentType
WHERE
    d.XmlContent IS NOT NULL
    AND dt.name IN ({','.join('?' for _ in ALLOWED_DOCUMENT_TYPES)});
"""

SELECTION_COUNTS_SQL = """
SELECT COUNT(*) AS SelectedDocumentCount
FROM #SelectedDocuments;
"""


DOCUMENT_QUERY = f"""
WITH StaticDocuments AS
(
    SELECT
        d.id AS DocumentId,
        RTRIM(d.chronicleId) AS ChronicleId,
        RTRIM(d.projectDocumentId) AS ProjectDocumentId,
        d.vccNumber,
        d.title,
        d.path AS DatabasePath,
        dt.name AS DocumentType,
        CONVERT(varbinary(max), d.XmlContent) AS XmlPackage,
        ROW_NUMBER() OVER
        (
            PARTITION BY RTRIM(d.projectDocumentId)
            ORDER BY d.id
        ) AS ProjectDocumentRow
    FROM dbo.Document AS d
    INNER JOIN #SelectedDocuments AS selected
        ON selected.DocumentId = d.id
    INNER JOIN dbo.DocumentType AS dt
        ON dt.id = d.fkDocumentType
    WHERE
        d.XmlContent IS NOT NULL
        AND dt.name IN ({",".join("?" for _ in ALLOWED_DOCUMENT_TYPES)})
)
SELECT
    sd.DocumentId,
    sd.ChronicleId,
    sd.ProjectDocumentId,
    sd.vccNumber,
    sd.title,
    sd.DatabasePath,
    sd.DocumentType,
    COALESCE(ti.functionGroup1, '') AS FunctionGroup1,
    COALESCE(fg1.title, '') AS FunctionGroup1Title,
    COALESCE(ti.functionGroup2, '') AS FunctionGroup2,
    COALESCE(fg2.title, '') AS FunctionGroup2Title,
    COALESCE(ti.functionGroup3, '') AS FunctionGroup3,
    COALESCE(fg3.title, '') AS FunctionGroup3Title,
    COALESCE(ti.title, '') AS TreeItemTitle,
    sd.XmlPackage
FROM StaticDocuments AS sd
OUTER APPLY
(
    SELECT TOP (1)
        ti2.*
    FROM dbo.TreeItemDocument AS tid
    INNER JOIN dbo.TreeItem AS ti2
        ON ti2.id = tid.fkTreeItem
    WHERE RTRIM(tid.projectDocumentTo) = sd.ProjectDocumentId
    ORDER BY
        ti2.functionGroup1,
        ti2.functionGroup2,
        ti2.functionGroup3,
        ti2.id
) AS ti
LEFT JOIN dbo.FunctionGroupText AS fg1
    ON fg1.functionGroup = ti.functionGroup1
LEFT JOIN dbo.FunctionGroupText AS fg2
    ON fg2.functionGroup = ti.functionGroup2
LEFT JOIN dbo.FunctionGroupText AS fg3
    ON fg3.functionGroup = ti.functionGroup3
WHERE sd.ProjectDocumentRow = 1
ORDER BY
    sd.DocumentType,
    sd.title,
    sd.DocumentId;
"""

PROFILE_COLUMNS = """
    vp.Id AS ProfileId,
    vp.Description AS ApplicabilityDescription,
    vp.FolderLevel,
    vm.Description AS VehicleModel,
    my.Description AS ModelYear,
    en.Description AS Engine,
    tr.Description AS Transmission,
    bs.Description AS BodyStyle,
    st.Description AS Steering,
    su.Description AS Suspension,
    br.Description AS BrakeSystem,
    vp.ChassisNoFrom,
    vp.ChassisNoTo
"""

PROFILE_JOINS = """
LEFT JOIN [basedata].dbo.VehicleProfile AS vp
    ON vp.Id = PROFILE_ID_EXPRESSION
LEFT JOIN [basedata].dbo.VehicleModel AS vm
    ON vm.Id = vp.fkVehicleModel
LEFT JOIN [basedata].dbo.ModelYear AS my
    ON my.Id = vp.fkModelYear
LEFT JOIN [basedata].dbo.Engine AS en
    ON en.Id = vp.fkEngine
LEFT JOIN [basedata].dbo.Transmission AS tr
    ON tr.Id = vp.fkTransmission
LEFT JOIN [basedata].dbo.BodyStyle AS bs
    ON bs.Id = vp.fkBodyStyle
LEFT JOIN [basedata].dbo.Steering AS st
    ON st.Id = vp.fkSteering
LEFT JOIN [basedata].dbo.Suspension AS su
    ON su.Id = vp.fkSuspension
LEFT JOIN [basedata].dbo.BrakeSystem AS br
    ON br.Id = vp.fkBrakeSystem
"""

DOCUMENT_APPLICABILITY_QUERY = f"""
WITH EligibleDocuments AS
(
    SELECT
        d.id AS DocumentId,
        ROW_NUMBER() OVER
        (
            PARTITION BY RTRIM(d.projectDocumentId)
            ORDER BY d.id
        ) AS ProjectDocumentRow
    FROM dbo.Document AS d
    INNER JOIN #SelectedDocuments AS selected
        ON selected.DocumentId = d.id
    INNER JOIN dbo.DocumentType AS dt
        ON dt.id = d.fkDocumentType
    WHERE
        d.XmlContent IS NOT NULL
        AND dt.name IN ({",".join("?" for _ in ALLOWED_DOCUMENT_TYPES)})
)
SELECT
    ed.DocumentId,
    {PROFILE_COLUMNS}
FROM EligibleDocuments AS ed
INNER JOIN dbo.DocumentProfile AS dp
    ON dp.fkDocument = ed.DocumentId
{PROFILE_JOINS.replace("PROFILE_ID_EXPRESSION", "dp.profileId")}
WHERE ed.ProjectDocumentRow = 1
ORDER BY ed.DocumentId, vp.FolderLevel, vp.Description, vp.Id;
"""

TREE_APPLICABILITY_QUERY = f"""
WITH EligibleDocuments AS
(
    SELECT
        d.id AS DocumentId,
        RTRIM(d.projectDocumentId) AS ProjectDocumentId,
        ROW_NUMBER() OVER
        (
            PARTITION BY RTRIM(d.projectDocumentId)
            ORDER BY d.id
        ) AS ProjectDocumentRow
    FROM dbo.Document AS d
    INNER JOIN #SelectedDocuments AS selected
        ON selected.DocumentId = d.id
    INNER JOIN dbo.DocumentType AS dt
        ON dt.id = d.fkDocumentType
    WHERE
        d.XmlContent IS NOT NULL
        AND dt.name IN ({",".join("?" for _ in ALLOWED_DOCUMENT_TYPES)})
)
SELECT DISTINCT
    ed.DocumentId,
    {PROFILE_COLUMNS}
FROM EligibleDocuments AS ed
INNER JOIN dbo.TreeItemDocument AS tid
    ON RTRIM(tid.projectDocumentTo) = ed.ProjectDocumentId
INNER JOIN dbo.TreeItemProfile AS tip
    ON tip.fkTreeItem = tid.fkTreeItem
{PROFILE_JOINS.replace("PROFILE_ID_EXPRESSION", "tip.profileId")}
WHERE ed.ProjectDocumentRow = 1
ORDER BY ed.DocumentId, vp.FolderLevel, vp.Description, vp.Id;
"""

REFERENCE_DOCUMENT_QUERY = """
SELECT
    RTRIM(d.projectDocumentId) AS ProjectDocumentId,
    RTRIM(d.chronicleId) AS ChronicleId,
    RTRIM(d.vccNumber) AS VccNumber,
    d.title AS DocumentTitle,
    dt.name AS DocumentType
FROM dbo.Document AS d
INNER JOIN dbo.DocumentType AS dt
    ON dt.id = d.fkDocumentType
WHERE dt.name = 'servinfosub'
  AND
  (
      d.projectDocumentId IS NOT NULL
      OR d.vccNumber IS NOT NULL
  );
"""


DEPENDENCY_TABLE_SETUP_SQL = """
IF OBJECT_ID('tempdb..#ReferencedAliases') IS NOT NULL
    DROP TABLE #ReferencedAliases;

CREATE TABLE #ReferencedAliases
(
    AliasValue varchar(100) NOT NULL PRIMARY KEY
);
"""

DEPENDENCY_DOCUMENT_QUERY = """
IF OBJECT_ID('tempdb..#NewDependencyDocuments') IS NOT NULL
    DROP TABLE #NewDependencyDocuments;

SELECT DISTINCT
    d.id AS DocumentId
INTO #NewDependencyDocuments
FROM dbo.Document AS d
INNER JOIN dbo.DocumentType AS dt
    ON dt.id = d.fkDocumentType
WHERE
    dt.name = 'servinfosub'
    AND d.XmlContent IS NOT NULL
    AND
    (
        EXISTS
        (
            SELECT 1
            FROM #ReferencedAliases AS a
            WHERE LOWER(a.AliasValue) = LOWER(RTRIM(d.projectDocumentId))
        )
        OR EXISTS
        (
            SELECT 1
            FROM #ReferencedAliases AS a
            WHERE LOWER(a.AliasValue) = LOWER(RTRIM(d.chronicleId))
        )
        OR EXISTS
        (
            SELECT 1
            FROM #ReferencedAliases AS a
            WHERE UPPER(a.AliasValue) = UPPER(RTRIM(d.vccNumber))
        )
    )
    AND NOT EXISTS
    (
        SELECT 1
        FROM #SelectedDocuments AS selected
        WHERE selected.DocumentId = d.id
    );

INSERT INTO #SelectedDocuments (DocumentId, ProjectDocumentId)
SELECT
    d.id,
    d.projectDocumentId
FROM dbo.Document AS d
INNER JOIN #NewDependencyDocuments AS nd
    ON nd.DocumentId = d.id;

SELECT
    d.id AS DocumentId,
    RTRIM(d.chronicleId) AS ChronicleId,
    RTRIM(d.projectDocumentId) AS ProjectDocumentId,
    RTRIM(d.vccNumber) AS vccNumber,
    d.title,
    d.path AS DatabasePath,
    dt.name AS DocumentType,
    COALESCE(ti.functionGroup1, '') AS FunctionGroup1,
    COALESCE(fg1.title, '') AS FunctionGroup1Title,
    COALESCE(ti.functionGroup2, '') AS FunctionGroup2,
    COALESCE(fg2.title, '') AS FunctionGroup2Title,
    COALESCE(ti.functionGroup3, '') AS FunctionGroup3,
    COALESCE(fg3.title, '') AS FunctionGroup3Title,
    COALESCE(ti.title, '') AS TreeItemTitle,
    CONVERT(varbinary(max), d.XmlContent) AS XmlPackage
FROM dbo.Document AS d
INNER JOIN #NewDependencyDocuments AS nd
    ON nd.DocumentId = d.id
INNER JOIN dbo.DocumentType AS dt
    ON dt.id = d.fkDocumentType
OUTER APPLY
(
    SELECT TOP (1)
        ti2.*
    FROM dbo.TreeItemDocument AS tid
    INNER JOIN dbo.TreeItem AS ti2
        ON ti2.id = tid.fkTreeItem
    WHERE RTRIM(tid.projectDocumentTo) = RTRIM(d.projectDocumentId)
    ORDER BY
        ti2.functionGroup1,
        ti2.functionGroup2,
        ti2.functionGroup3,
        ti2.id
) AS ti
LEFT JOIN dbo.FunctionGroupText AS fg1
    ON fg1.functionGroup = ti.functionGroup1
LEFT JOIN dbo.FunctionGroupText AS fg2
    ON fg2.functionGroup = ti.functionGroup2
LEFT JOIN dbo.FunctionGroupText AS fg3
    ON fg3.functionGroup = ti.functionGroup3
ORDER BY d.id;
"""


PARTNER_GROUP_QUERY = """
SELECT
    RTRIM(fkVehicleProfile) AS ProfileId,
    RTRIM(PartnerGroupCID) AS PartnerGroupCID
FROM [basedata].dbo.VehicleProfilePartnerGroup
ORDER BY fkVehicleProfile, PartnerGroupCID;
"""


def prepare_document_selection(cursor: pyodbc.Cursor) -> int:
    """Create a connection-scoped list of all eligible document rows."""
    cursor.execute(SELECTION_SETUP_SQL)
    cursor.execute(SELECT_DOCUMENTS_SQL, *ALLOWED_DOCUMENT_TYPES)
    cursor.execute(SELECTION_COUNTS_SQL)
    row = cursor.fetchone()
    return int(row.SelectedDocumentCount)


def make_applicability(
    row: object,
    source: str,
    partner_groups: dict[str, list[str]],
) -> VehicleApplicability:
    profile_id = clean(row.ProfileId)
    return VehicleApplicability(
        source=source,
        profile_id=profile_id,
        description=clean(row.ApplicabilityDescription),
        folder_level=(
            int(row.FolderLevel)
            if row.FolderLevel is not None
            else None
        ),
        vehicle_model=clean(row.VehicleModel),
        model_year=clean(row.ModelYear),
        engine=clean(row.Engine),
        transmission=clean(row.Transmission),
        body_style=clean(row.BodyStyle),
        steering=clean(row.Steering),
        suspension=clean(row.Suspension),
        brake_system=clean(row.BrakeSystem),
        chassis_no_from=(
            int(row.ChassisNoFrom)
            if row.ChassisNoFrom is not None
            else None
        ),
        chassis_no_to=(
            int(row.ChassisNoTo)
            if row.ChassisNoTo is not None
            else None
        ),
        partner_groups=list(partner_groups.get(profile_id, [])),
    )



def row_to_document_record(row: object) -> DocumentRecord:
    return DocumentRecord(
        document_id=int(row.DocumentId),
        chronicle_id=clean(row.ChronicleId),
        project_document_id=clean(row.ProjectDocumentId),
        vcc_number=clean(row.vccNumber),
        title=clean(row.title),
        database_path=clean(row.DatabasePath),
        document_type=clean(row.DocumentType),
        function_group_1=clean(row.FunctionGroup1),
        function_group_1_title=clean(row.FunctionGroup1Title),
        function_group_2=clean(row.FunctionGroup2),
        function_group_2_title=clean(row.FunctionGroup2Title),
        function_group_3=clean(row.FunctionGroup3),
        function_group_3_title=clean(row.FunctionGroup3Title),
        tree_item_title=clean(row.TreeItemTitle),
        xml_package=bytes(row.XmlPackage),
    )


def referenced_document_aliases(record: DocumentRecord) -> set[str]:
    """Return project, chronicle, and VCC aliases referenced by one XML package."""
    aliases: set[str] = set()

    try:
        xml_bytes = extract_xml_from_package(record.xml_package)
        xml_text = xml_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return aliases

    for match in DOCUMENT_REFERENCE_PATTERN.finditer(xml_text):
        raw = clean(match.group("raw"))
        if not raw:
            continue

        if VCC_REFERENCE_PATTERN.fullmatch(raw):
            aliases.add(raw.upper())
        else:
            document_alias, _anchor = normalize_doc_reference(raw)
            if document_alias:
                aliases.add(document_alias.lower())

    return aliases


def include_referenced_servinfosub_documents(
    cursor: pyodbc.Cursor,
    records: list[DocumentRecord],
    records_by_id: dict[int, DocumentRecord],
    max_passes: int = 10,
) -> int:
    """
    Recursively add referenced servinfosub documents that were omitted by the
    vehicle-profile filter. This keeps local links usable without broadening
    the selected top-level document collection.
    """
    cursor.execute(DEPENDENCY_TABLE_SETUP_SQL)

    scanned_document_ids: set[int] = set()
    total_added = 0

    for pass_number in range(1, max_passes + 1):
        aliases: set[str] = set()

        for record in records:
            if record.document_id in scanned_document_ids:
                continue
            scanned_document_ids.add(record.document_id)
            aliases.update(referenced_document_aliases(record))

        if not aliases:
            break

        cursor.execute("DELETE FROM #ReferencedAliases;")
        cursor.fast_executemany = True
        cursor.executemany(
            "INSERT INTO #ReferencedAliases (AliasValue) VALUES (?)",
            [(alias,) for alias in sorted(aliases)],
        )
        cursor.fast_executemany = False

        cursor.execute(DEPENDENCY_DOCUMENT_QUERY)
        new_records: list[DocumentRecord] = []

        for row in cursor:
            record = row_to_document_record(row)
            if record.document_id in records_by_id:
                continue
            records_by_id[record.document_id] = record
            records.append(record)
            new_records.append(record)

        if not new_records:
            break

        total_added += len(new_records)
        print(
            f"    dependency pass {pass_number}: added "
            f"{len(new_records):,} referenced servinfosub documents.",
            flush=True,
        )

    return total_added


def fetch_documents() -> tuple[list[DocumentRecord], dict[str, dict[str, str]]]:
    records: list[DocumentRecord] = []
    records_by_id: dict[int, DocumentRecord] = {}
    reference_documents: dict[str, dict[str, str]] = {}

    with pyodbc.connect(CONNECTION_STRING) as connection:
        cursor = connection.cursor()

        print(
            "  Preparing all-model, all-year document selection...",
            flush=True,
        )
        selected_count = prepare_document_selection(cursor)
        print(
            f"    selected {selected_count:,} source document rows.",
            flush=True,
        )

        print("  Stage 1/4: loading documents and XML packages...", flush=True)
        cursor.execute(DOCUMENT_QUERY, *ALLOWED_DOCUMENT_TYPES)

        for row in cursor:
            record = row_to_document_record(row)
            records.append(record)
            records_by_id[record.document_id] = record

            if len(records) % 10000 == 0:
                print(f"    loaded {len(records):,} documents...", flush=True)

        print(f"    loaded {len(records):,} documents.", flush=True)

        print("  Stage 2/4: loading vehicle-profile market groups...", flush=True)
        partner_groups: dict[str, list[str]] = {}
        cursor.execute(PARTNER_GROUP_QUERY)
        group_rows = 0
        for row in cursor:
            profile_id = clean(row.ProfileId)
            group = clean(row.PartnerGroupCID)
            if profile_id and group:
                partner_groups.setdefault(profile_id, []).append(group)
            group_rows += 1
        print(f"    loaded {group_rows:,} profile/market rows.", flush=True)

        print("  Stage 3/4: loading direct document applicability...", flush=True)
        cursor.execute(
            DOCUMENT_APPLICABILITY_QUERY,
            *ALLOWED_DOCUMENT_TYPES,
        )
        direct_rows = 0
        for row in cursor:
            record = records_by_id.get(int(row.DocumentId))
            if record is not None:
                record.vehicle_applicability.append(
                    make_applicability(row, "DocumentProfile", partner_groups)
                )
            direct_rows += 1
            if direct_rows % 100000 == 0:
                print(f"    processed {direct_rows:,} direct profile rows...", flush=True)
        print(f"    processed {direct_rows:,} direct profile rows.", flush=True)

        print("  Stage 4/4: loading navigation-tree applicability...", flush=True)
        cursor.execute(
            TREE_APPLICABILITY_QUERY,
            *ALLOWED_DOCUMENT_TYPES,
        )
        tree_rows = 0
        for row in cursor:
            record = records_by_id.get(int(row.DocumentId))
            if record is not None:
                record.vehicle_applicability.append(
                    make_applicability(row, "TreeItemProfile", partner_groups)
                )
            tree_rows += 1
            if tree_rows % 100000 == 0:
                print(f"    processed {tree_rows:,} tree profile rows...", flush=True)
        print(f"    processed {tree_rows:,} tree profile rows.", flush=True)

        print(
            "  Loading servinfosub project/chronicle/VCC reference titles...",
            flush=True,
        )

        exported_project_ids = {
            record.project_document_id.lower()
            for record in records
            if record.project_document_id
        }
        exported_chronicle_ids = {
            record.chronicle_id.lower()
            for record in records
            if record.chronicle_id
        }
        exported_vcc_numbers = {
            record.vcc_number.upper()
            for record in records
            if record.vcc_number
        }

        cursor.execute(REFERENCE_DOCUMENT_QUERY)
        reference_rows = 0
        project_aliases = 0
        chronicle_aliases = 0
        vcc_aliases = 0

        for row in cursor:
            project_id = clean(row.ProjectDocumentId)
            chronicle_id = clean(row.ChronicleId)
            vcc_number = clean(row.VccNumber).upper()
            fallback_key = (
                chronicle_id
                or project_id
                or vcc_number
                or str(reference_rows)
            )
            output_key = safe_filename(
                chronicle_id or project_id or vcc_number,
                fallback_key,
            )
            is_exported = (
                (project_id and project_id.lower() in exported_project_ids)
                or (
                    chronicle_id
                    and chronicle_id.lower() in exported_chronicle_ids
                )
                or (vcc_number and vcc_number in exported_vcc_numbers)
            )

            info = {
                "title": clean(row.DocumentTitle),
                "document_type": clean(row.DocumentType),
                "output_key": output_key,
                "project_document_id": project_id,
                "chronicle_id": chronicle_id,
                "vcc_number": vcc_number,
                "is_exported": "1" if is_exported else "0",
            }

            if project_id:
                reference_documents[project_id.lower()] = dict(info)
                project_aliases += 1

            if chronicle_id:
                reference_documents[chronicle_id.lower()] = dict(info)
                chronicle_aliases += 1

            if vcc_number:
                reference_documents[vcc_number] = dict(info)
                vcc_aliases += 1

            reference_rows += 1

        print(
            f"    loaded {reference_rows:,} servinfosub records with "
            f"{project_aliases:,} project-ID aliases, "
            f"{chronicle_aliases:,} chronicle-ID aliases, and "
            f"{vcc_aliases:,} VCC aliases.",
            flush=True,
        )

    return records, reference_documents

def extract_xml_from_package(package: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
        xml_names = [
            name for name in archive.namelist()
            if name.lower().endswith(".xml")
        ]

        if not xml_names:
            raise ValueError("ZIP package contains no XML file.")

        return archive.read(xml_names[0])


# ============================================================
# Rendering
# ============================================================

BLOCK_TAGS = {
    "frontm",
    "body",
    "servinfosub",
    "i2",
    "torqueinfo",
    "sbull",
    "procedure",
    "stepgrp",
    "procstep",
    "preface",
    "materialtable",
    "toolpics",
    "kitpics",
}


def heading_level(element: ET.Element) -> int:
    parent_tag = ""
    # ElementTree does not track parents, so callers can override where useful.
    tag = strip_namespace(element.tag)
    if tag == "title":
        return 2
    return 3


def render_inline_markdown(element: ET.Element, ctx: RenderContext) -> str:
    tag = strip_namespace(element.tag)
    anchor = element_id(element)
    output: list[str] = []

    if element.text:
        output.append(
            replace_document_references_markdown(element.text.strip(), ctx)
        )

    for child in element:
        child_tag = strip_namespace(child.tag)

        if child_tag == "emph":
            value = render_inline_markdown(child, ctx).strip()
            output.append(f"**{value}**" if value else "")
        elif child_tag in {"xref", "ref"}:
            output.append(render_inline_markdown(child, ctx))
        elif child_tag == "href":
            raw = text_content(child)
            title = clean(child.attrib.get("title")) or raw
            if re.search(r"\.(gif|jpe?g|cgm|svg)$", raw, re.I):
                output.append(title)
            elif DOCUMENT_REFERENCE_PATTERN.fullmatch(raw):
                output.append(reference_markdown_link(raw, ctx))
            else:
                link = local_markdown_doc_link(raw)
                output.append(f"[{escape_markdown(title)}]({link})")
        elif child_tag == "equipment":
            name = clean(child.attrib.get("equipmentname"))
            output.append(f"**Tool:** {escape_markdown(name)}" if name else "")
        elif child_tag == "material":
            name = clean(child.attrib.get("materialname"))
            output.append(
                f"**Material:** {escape_markdown(name)}" if name else ""
            )
        else:
            output.append(render_inline_markdown(child, ctx))

        if child.tail:
            output.append(
                replace_document_references_markdown(child.tail.strip(), ctx)
            )

    value = " ".join(part for part in output if part).strip()
    if anchor:
        return anchor_markdown(anchor) + value
    return value


def render_inline_html(element: ET.Element, ctx: RenderContext) -> str:
    anchor = element_id(element)
    output: list[str] = [anchor_html(anchor)]

    if element.text:
        output.append(
            replace_document_references_html(element.text.strip(), ctx)
        )

    for child in element:
        child_tag = strip_namespace(child.tag)

        if child_tag == "emph":
            output.append(
                f"<strong>{render_inline_html(child, ctx)}</strong>"
            )
        elif child_tag in {"xref", "ref"}:
            output.append(render_inline_html(child, ctx))
        elif child_tag == "href":
            raw = text_content(child)
            title = clean(child.attrib.get("title")) or raw
            if re.search(r"\.(gif|jpe?g|cgm|svg)$", raw, re.I):
                output.append(html.escape(title))
            elif DOCUMENT_REFERENCE_PATTERN.fullmatch(raw):
                output.append(reference_html_link(raw, ctx))
            else:
                link = local_html_doc_link(raw)
                output.append(
                    f'<a href="{html.escape(link, quote=True)}">'
                    f"{html.escape(title)}</a>"
                )
        elif child_tag == "equipment":
            name = clean(child.attrib.get("equipmentname"))
            if name:
                output.append(
                    f'<span class="equipment"><strong>Tool:</strong> '
                    f"{html.escape(name)}</span>"
                )
        elif child_tag == "material":
            name = clean(child.attrib.get("materialname"))
            if name:
                output.append(
                    f'<span class="material"><strong>Material:</strong> '
                    f"{html.escape(name)}</span>"
                )
        else:
            output.append(render_inline_html(child, ctx))

        if child.tail:
            output.append(
                replace_document_references_html(child.tail.strip(), ctx)
            )

    return " ".join(part for part in output if part).strip()



IMAGE_FILENAME_PATTERN = re.compile(
    r"(?P<filename>[A-Za-z0-9_.-]+\.(?:gif|jpe?g|svg|cgm))",
    re.IGNORECASE,
)


def table_text_markdown(value: str, ctx: RenderContext) -> str:
    """Render plain table-cell text, converting bare image filenames to images."""
    output: list[str] = []
    last = 0

    for match in IMAGE_FILENAME_PATTERN.finditer(value):
        output.append(escape_markdown(value[last:match.start()]))
        filename = match.group("filename")

        if resolve_image(filename) is None:
            ctx.unresolved_images.append(
                {
                    "document": ctx.current_doc_key,
                    "title": ctx.current_title,
                    "image": filename,
                }
            )

        output.append(
            f"![{escape_markdown(Path(filename).stem)}]"
            f"({image_url(filename)})"
            f"<br><code>{escape_markdown(filename)}</code>"
        )
        last = match.end()

    output.append(escape_markdown(value[last:]))
    return "".join(output)


def table_text_html(value: str, ctx: RenderContext) -> str:
    """Render plain table-cell text, converting bare image filenames to img tags."""
    output: list[str] = []
    last = 0

    for match in IMAGE_FILENAME_PATTERN.finditer(value):
        output.append(html.escape(value[last:match.start()]))
        filename = match.group("filename")

        if resolve_image(filename) is None:
            ctx.unresolved_images.append(
                {
                    "document": ctx.current_doc_key,
                    "title": ctx.current_title,
                    "image": filename,
                }
            )

        output.append(
            '<span class="table-image-reference">'
            f'<img class="table-image" '
            f'src="{html.escape(image_url(filename), quote=True)}" '
            f'alt="{html.escape(Path(filename).stem, quote=True)}" '
            f'loading="lazy">'
            f'<code>{html.escape(filename)}</code>'
            '</span>'
        )
        last = match.end()

    output.append(html.escape(value[last:]))
    return "".join(output)


def render_table_cell_markdown(entry: ET.Element, ctx: RenderContext) -> str:
    """Render a table entry without flattening nested graphics."""
    parts: list[str] = []

    if entry.text:
        parts.append(table_text_markdown(entry.text, ctx))

    for child in entry:
        child_tag = strip_namespace(child.tag)

        if child_tag in {"graphic", "i2graphic"}:
            rendered = render_markdown(child, ctx).strip()
        elif child_tag in {"para", "ptxt", "phrase"}:
            rendered = render_inline_markdown(child, ctx).strip()
        else:
            rendered = render_markdown(child, ctx).strip()
            if not rendered:
                rendered = render_inline_markdown(child, ctx).strip()

        if rendered:
            parts.append(rendered)

        if child.tail:
            parts.append(table_text_markdown(child.tail, ctx))

    compact = " ".join(part.strip() for part in parts if part.strip())
    compact = re.sub(r"\s*\n+\s*", "<br>", compact)
    return compact


def render_table_cell_html(entry: ET.Element, ctx: RenderContext) -> str:
    """Render a table entry without flattening nested graphics."""
    parts: list[str] = []

    if entry.text:
        parts.append(table_text_html(entry.text, ctx))

    for child in entry:
        child_tag = strip_namespace(child.tag)

        if child_tag in {"graphic", "i2graphic"}:
            rendered = render_html(child, ctx)
        elif child_tag in {"para", "ptxt", "phrase"}:
            inline = render_inline_html(child, ctx).strip()
            rendered = f"<p>{inline}</p>" if inline else ""
        else:
            rendered = render_html(child, ctx)
            if not rendered:
                rendered = render_inline_html(child, ctx)

        if rendered:
            parts.append(rendered)

        if child.tail:
            parts.append(table_text_html(child.tail, ctx))

    return "".join(parts).strip()


def render_markdown(element: ET.Element, ctx: RenderContext, depth: int = 0) -> str:
    tag = strip_namespace(element.tag)
    anchor = element_id(element)

    if tag == "title":
        level = min(2 + depth, 6)
        return (
            anchor_markdown(anchor)
            + f"{'#' * level} {escape_markdown(text_content(element))}\n\n"
        )

    if tag in {"para", "ptxt", "phrase"}:
        value = render_inline_markdown(element, ctx).strip()
        return f"{value}\n\n" if value else ""

    if tag in {"warning", "caution", "note"}:
        label = tag.capitalize()
        body = "".join(
            render_markdown(child, ctx, depth + 1)
            for child in element
        ).strip()
        if not body:
            body = escape_markdown(text_content(element))
        quoted = "\n".join(f"> {line}" for line in body.splitlines())
        return f"> **{label}:**\n{quoted}\n\n"

    if tag in {"list1", "list2"}:
        ordered = element.attrib.get("type") == "ordered"
        lines: list[str] = []
        number = 1
        for child in element:
            if strip_namespace(child.tag) != "item":
                continue
            body = "".join(
                render_markdown(grandchild, ctx, depth + 1)
                for grandchild in child
            ).strip()
            if not body:
                body = escape_markdown(text_content(child))
            prefix = f"{number}." if ordered else "-"
            lines.append(f"{prefix} {body}")
            number += 1
        return "\n".join(lines) + "\n\n"

    if tag == "graphic" or tag == "i2graphic":
        href = next(
            (
                text_content(child)
                for child in element
                if strip_namespace(child.tag) == "href"
            ),
            "",
        )
        title = next(
            (
                clean(child.attrib.get("title"))
                for child in element
                if strip_namespace(child.tag) == "href"
            ),
            "",
        )

        if not href:
            return ""

        resolved = resolve_image(href)
        if resolved is None:
            ctx.unresolved_images.append(
                {
                    "document": ctx.current_doc_key,
                    "title": ctx.current_title,
                    "image": href,
                }
            )

        alt = title or Path(href).stem
        return (
            anchor_markdown(anchor)
            + f"![{escape_markdown(alt)}]({image_url(href)})\n\n"
        )

    if tag in {"table", "tgroup", "thead", "tbody"}:
        # Render at the first table-like container only.
        if tag != "table":
            return "".join(
                render_markdown(child, ctx, depth)
                for child in element
            )

        rows: list[list[str]] = []
        for row in element.iter():
            if strip_namespace(row.tag) != "row":
                continue
            cells = [
                render_table_cell_markdown(entry, ctx)
                for entry in row
                if strip_namespace(entry.tag) == "entry"
            ]
            if cells:
                rows.append(cells)

        if not rows:
            return ""

        width = max(len(row) for row in rows)
        normalized = [
            row + [""] * (width - len(row))
            for row in rows
        ]

        output = [
            "| " + " | ".join(escape_markdown(v) for v in normalized[0]) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
        ]
        for row in normalized[1:]:
            output.append(
                "| " + " | ".join(escape_markdown(v) for v in row) + " |"
            )
        return "\n".join(output) + "\n\n"

    if tag in {"stepgrp", "procstep"}:
        css_label = "Procedure group" if tag == "stepgrp" else "Step"
        content = "".join(
            render_markdown(child, ctx, depth + 1)
            for child in element
        ).strip()
        if not content:
            return ""
        return (
            anchor_markdown(anchor)
            + f"<!-- {css_label} -->\n\n{content}\n\n"
        )

    if tag == "grate":
        # Preserve the XML order: image and associated graph text.
        return "".join(
            render_markdown(child, ctx, depth + 1)
            for child in element
        )

    if tag in {"xref", "ref", "href", "emph", "equipment", "material"}:
        value = render_inline_markdown(element, ctx)
        return f"{value}\n\n" if value else ""

    # Generic container fallback.
    children = "".join(
        render_markdown(child, ctx, depth)
        for child in element
    )

    if children.strip():
        return anchor_markdown(anchor) + children

    value = text_content(element)
    if value:
        references = sole_document_references(value)
        if references:
            lines = []
            for raw in references:
                info = reference_info(raw, ctx)
                project_id, _ = normalize_doc_reference(raw)
                label = reference_markdown_link(raw, ctx)
                kind = (info or {}).get("document_type", "")
                prefix = "Special tool/reference" if kind == "servinfosub" else "Related document"
                lines.append(
                    f"- **{prefix}:** {label} "
                    f"(`{escape_markdown(project_id)}`)"
                )
            return anchor_markdown(anchor) + "\n".join(lines) + "\n\n"
        return (
            anchor_markdown(anchor)
            + replace_document_references_markdown(value, ctx)
            + "\n\n"
        )

    return ""


def render_html(element: ET.Element, ctx: RenderContext, depth: int = 0) -> str:
    tag = strip_namespace(element.tag)
    anchor = element_id(element)

    if tag == "title":
        level = min(2 + depth, 6)
        return (
            f"{anchor_html(anchor)}"
            f"<h{level}>{html.escape(text_content(element))}</h{level}>"
        )

    if tag in {"para", "ptxt", "phrase"}:
        value = render_inline_html(element, ctx).strip()
        return f"<p>{value}</p>" if value else ""

    if tag in {"warning", "caution", "note"}:
        body = "".join(
            render_html(child, ctx, depth + 1)
            for child in element
        )
        if not body:
            body = f"<p>{html.escape(text_content(element))}</p>"
        return (
            f'{anchor_html(anchor)}'
            f'<aside class="{tag}">'
            f"<strong>{tag.capitalize()}</strong>{body}</aside>"
        )

    if tag in {"list1", "list2"}:
        list_tag = (
            "ol" if element.attrib.get("type") == "ordered" else "ul"
        )
        items = []
        for child in element:
            if strip_namespace(child.tag) != "item":
                continue
            body = "".join(
                render_html(grandchild, ctx, depth + 1)
                for grandchild in child
            )
            if not body:
                body = html.escape(text_content(child))
            items.append(f"<li>{body}</li>")
        return (
            f"{anchor_html(anchor)}"
            f"<{list_tag}>{''.join(items)}</{list_tag}>"
        )

    if tag in {"graphic", "i2graphic"}:
        href_element = next(
            (
                child for child in element
                if strip_namespace(child.tag) == "href"
            ),
            None,
        )
        if href_element is None:
            return ""

        filename = text_content(href_element)
        title = clean(href_element.attrib.get("title"))
        resolved = resolve_image(filename)

        if resolved is None:
            ctx.unresolved_images.append(
                {
                    "document": ctx.current_doc_key,
                    "title": ctx.current_title,
                    "image": filename,
                }
            )

        src = image_url(filename)
        alt = title or Path(filename).stem
        return (
            f"{anchor_html(anchor)}"
            f'<figure class="graphic">'
            f'<img src="{html.escape(src, quote=True)}" '
            f'alt="{html.escape(alt, quote=True)}" loading="lazy">'
            f"<figcaption>{html.escape(alt)}</figcaption>"
            f"</figure>"
        )

    if tag == "table":
        rows = []
        for row in element.iter():
            if strip_namespace(row.tag) != "row":
                continue
            cells = [
                render_table_cell_html(entry, ctx)
                for entry in row
                if strip_namespace(entry.tag) == "entry"
            ]
            if cells:
                rows.append(cells)

        if not rows:
            return ""

        header = "".join(f"<th>{v}</th>" for v in rows[0])
        body = "".join(
            "<tr>"
            + "".join(f"<td>{v}</td>" for v in row)
            + "</tr>"
            for row in rows[1:]
        )
        return (
            f"{anchor_html(anchor)}"
            f"<table><thead><tr>{header}</tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    if tag in {"tgroup", "thead", "tbody"}:
        return "".join(
            render_html(child, ctx, depth)
            for child in element
        )

    if tag in {"stepgrp", "procstep"}:
        class_name = "step-group" if tag == "stepgrp" else "procedure-step"
        content = "".join(
            render_html(child, ctx, depth + 1)
            for child in element
        )
        if not content.strip():
            return ""
        return (
            f'{anchor_html(anchor)}'
            f'<section class="{class_name}">{content}</section>'
        )

    if tag == "grate":
        return (
            f'{anchor_html(anchor)}'
            f'<div class="graphic-and-text">'
            + "".join(
                render_html(child, ctx, depth + 1)
                for child in element
            )
            + "</div>"
        )

    if tag in {"xref", "ref", "href", "emph", "equipment", "material"}:
        return render_inline_html(element, ctx)

    children = "".join(
        render_html(child, ctx, depth)
        for child in element
    )

    if children.strip():
        class_name = safe_filename(tag, "xml-container").lower()
        return (
            f'{anchor_html(anchor)}'
            f'<div class="{html.escape(class_name, quote=True)}">'
            f"{children}</div>"
        )

    value = text_content(element)
    if value:
        references = sole_document_references(value)
        if references:
            items = []
            for raw in references:
                info = reference_info(raw, ctx)
                project_id, _ = normalize_doc_reference(raw)
                label = reference_html_link(raw, ctx)
                kind = (info or {}).get("document_type", "")
                prefix = "Special tool/reference" if kind == "servinfosub" else "Related document"
                items.append(
                    '<li class="special-tool-reference">'
                    f'<strong>{html.escape(prefix)}:</strong> {label} '
                    f'<code>{html.escape(project_id)}</code></li>'
                )
            return (
                f"{anchor_html(anchor)}"
                '<ul class="special-tool-references">'
                + "".join(items)
                + "</ul>"
            )
        return (
            f"{anchor_html(anchor)}"
            f"<p>{replace_document_references_html(value, ctx)}</p>"
        )

    return ""


# ============================================================
# Output
# ============================================================

HTML_STYLE = """
:root {
    color-scheme: light dark;
    font-family: Arial, Helvetica, sans-serif;
}
body {
    max-width: 1100px;
    margin: 0 auto;
    padding: 2rem;
    line-height: 1.55;
}
header {
    border-bottom: 1px solid #888;
    margin-bottom: 2rem;
}
.metadata {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: .35rem 1rem;
    font-size: .95rem;
}
.step-group {
    margin: 2rem 0;
}
.procedure-step,
.graphic-and-text {
    border: 1px solid #9995;
    border-radius: .5rem;
    padding: 1rem;
    margin: 1rem 0;
}
.graphic img {
    display: block;
    max-width: 100%;
    height: auto;
    margin: 0 auto;
}
.graphic figcaption {
    text-align: center;
    font-size: .9rem;
    opacity: .8;
}
.warning,
.caution,
.note {
    border-left: .4rem solid currentColor;
    padding: .75rem 1rem;
    margin: 1rem 0;
}
.warning { background: #ff000012; }
.caution { background: #ffa50018; }
.note { background: #0080ff12; }
.vehicle-applicability {
    border: 1px solid #aaa;
    border-radius: .4rem;
    padding: .8rem 1rem;
    margin: 1rem 0 1.5rem;
    background: #f7f7f7;
}
.vehicle-applicability h2 { margin-top: 0; }
.vehicle-applicability li { margin-bottom: .65rem; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 1rem 0;
}
th, td {
    border: 1px solid #888;
    padding: .5rem;
    vertical-align: top;
}
.table-image-reference {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: .25rem;
}
.table-image {
    display: block;
    width: auto;
    max-width: 100%;
    height: auto;
}
.table-image-reference code {
    font-size: .75rem;
    opacity: .7;
}
td .graphic {
    margin: 0;
}
td .graphic img {
    margin: 0;
}
a {
    overflow-wrap: anywhere;
}
.special-tool-references {
    border-left: .3rem solid #666;
    padding: .65rem 1rem .65rem 2rem;
    margin: 1rem 0;
    background: #7771;
}
.special-tool-reference {
    margin: .35rem 0;
}
.special-tool-reference code {
    margin-left: .4rem;
    font-size: .85em;
    opacity: .75;
}
.vcc-reference + code {
    margin-left: .35rem;
    font-size: .85em;
    opacity: .75;
}
.unavailable-reference small {
    opacity: .75;
}
"""


def write_original_xml(record: DocumentRecord, xml_bytes: bytes) -> Path:
    """Write the XML bytes exactly as extracted from the document ZIP blob."""
    doc_key = record.chronicle_id or record.project_document_id
    output_path = XML_ROOT / f"{safe_filename(doc_key, str(record.document_id))}.xml"
    output_path.write_bytes(xml_bytes)
    return output_path


def write_html_page(record: DocumentRecord, body: str) -> Path:
    doc_key = record.chronicle_id or record.project_document_id
    output_path = HTML_ROOT / f"{safe_filename(doc_key, str(record.document_id))}.html"

    metadata = [
        ("Document type", record.document_type),
        ("VCC number", record.vcc_number),
        ("Project document", record.project_document_id),
        ("Chronicle", record.chronicle_id),
        ("Function group 1", f"{record.function_group_1} {record.function_group_1_title}".strip()),
        ("Function group 2", f"{record.function_group_2} {record.function_group_2_title}".strip()),
        ("Function group 3", f"{record.function_group_3} {record.function_group_3_title}".strip()),
        ("Tree topic", record.tree_item_title),
    ]

    metadata_html = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
        for label, value in metadata
        if value
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(record.title or doc_key)}</title>
<style>{HTML_STYLE}</style>
</head>
<body>
<header>
<h1>{html.escape(record.title or doc_key)}</h1>
<dl class="metadata">{metadata_html}</dl>
</header>
<main>
{applicability_html(record)}
{body}
</main>
</body>
</html>
"""

    output_path.write_text(page, encoding="utf-8")
    return output_path


def write_markdown_page(record: DocumentRecord, body: str) -> Path:
    doc_key = record.chronicle_id or record.project_document_id
    output_path = MARKDOWN_ROOT / f"{safe_filename(doc_key, str(record.document_id))}.md"

    front_matter = [
        "---",
        f"title: {yaml_quote(record.title or doc_key)}",
        f"document_type: {yaml_quote(record.document_type)}",
        f"document_id: {record.document_id}",
        f"project_document_id: {yaml_quote(record.project_document_id)}",
        f"chronicle_id: {yaml_quote(record.chronicle_id)}",
        f"vcc_number: {yaml_quote(record.vcc_number)}",
        f"tree_item_title: {yaml_quote(record.tree_item_title)}",
        f"function_group_1: {yaml_quote(record.function_group_1)}",
        f"function_group_1_title: {yaml_quote(record.function_group_1_title)}",
        f"function_group_2: {yaml_quote(record.function_group_2)}",
        f"function_group_2_title: {yaml_quote(record.function_group_2_title)}",
        f"function_group_3: {yaml_quote(record.function_group_3)}",
        f"function_group_3_title: {yaml_quote(record.function_group_3_title)}",
    ]
    front_matter.extend(yaml_applicability_lines(record))
    front_matter.extend(
        [
            "---",
            "",
            f"# {record.title or doc_key}",
            "",
            applicability_markdown(record),
        ]
    )

    output_path.write_text(
        "\n".join(front_matter) + "\n" + body,
        encoding="utf-8",
    )
    return output_path


def function_group_sort_key(value: str) -> tuple[int, tuple[int, ...], str]:
    """Sort function-group codes numerically, with blanks/non-numeric values last."""
    cleaned = clean(value)
    number_parts = tuple(int(part) for part in re.findall(r"\\d+", cleaned))

    if number_parts:
        return (0, number_parts, cleaned.casefold())

    return (1, (), cleaned.casefold())


def index_link_text(record: DocumentRecord) -> str:
    """Build a human-readable Function Group 1 : Function Group 2 : Tree Topic label."""
    parts = [
        f"{record.function_group_1} {record.function_group_1_title}".strip(),
        f"{record.function_group_2} {record.function_group_2_title}".strip(),
        record.tree_item_title.strip(),
    ]
    parts = [part for part in parts if part]

    # Fall back to the document title or identifier if no tree classification exists.
    if not parts:
        return record.title or record.chronicle_id or record.project_document_id

    return " : ".join(parts)


def write_index(records: list[tuple[DocumentRecord, Path]]) -> None:
    sorted_records = sorted(
        records,
        key=lambda item: (
            function_group_sort_key(item[0].function_group_1),
            function_group_sort_key(item[0].function_group_2),
            function_group_sort_key(item[0].function_group_3),
            clean(item[0].tree_item_title).casefold(),
            clean(item[0].title).casefold(),
            clean(item[0].project_document_id).casefold(),
        ),
    )

    items = []
    for record, html_path in sorted_records:
        link_text = index_link_text(record)
        items.append(
            "<li>"
            f'<a href="{html.escape(html_path.name, quote=True)}">'
            f"{html.escape(link_text)}"
            "</a>"
            f" <small>({html.escape(record.document_type)})</small>"
            "</li>"
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volvo Service Export — All Models and Years</title>
<style>{HTML_STYLE}</style>
</head>
<body>
<h1>Volvo Service Export — All Models and Years</h1>
<p>{len(records)} documents exported. Sorted numerically by function groups 1 and 2.</p>
<ul>
{''.join(items)}
</ul>
</body>
</html>
"""

    (HTML_ROOT / "index.html").write_text(page, encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# Main
# ============================================================

def main() -> int:
    MARKDOWN_ROOT.mkdir(parents=True, exist_ok=True)
    HTML_ROOT.mkdir(parents=True, exist_ok=True)
    XML_ROOT.mkdir(parents=True, exist_ok=True)

    unresolved_images: list[dict] = []
    unresolved_links: list[dict] = []
    manifest_rows: list[dict] = []
    index_records: list[tuple[DocumentRecord, Path]] = []

    print("Querying SQL Server for all models and years...", flush=True)
    records, reference_documents = fetch_documents()
    total_records = len(records)
    print(f"Retrieved {total_records} eligible documents.")

    for number, record in enumerate(records, start=1):
        doc_key = record.chronicle_id or record.project_document_id
        if number == 1 or number % 100 == 0 or number == total_records:
            print(f"[{number}/{total_records}] {record.title or doc_key}")

        try:
            xml_bytes = extract_xml_from_package(record.xml_package)
            root = ET.fromstring(xml_bytes)

            ctx = RenderContext(
                current_doc_key=doc_key,
                current_title=record.title,
                unresolved_images=unresolved_images,
                unresolved_links=unresolved_links,
                reference_documents=reference_documents,
            )

            markdown_body = render_markdown(root, ctx).strip() + "\n"
            html_body = render_html(root, ctx)

            markdown_path = write_markdown_page(record, markdown_body)
            html_path = write_html_page(record, html_body)
            xml_path = write_original_xml(record, xml_bytes)

            index_records.append((record, html_path))

            manifest_rows.append(
                {
                    "document_id": record.document_id,
                    "title": record.title,
                    "document_type": record.document_type,
                    "project_document_id": record.project_document_id,
                    "chronicle_id": record.chronicle_id,
                    "vcc_number": record.vcc_number,
                    "tree_item_title": record.tree_item_title,
                    "vehicle_applicability": applicability_summary(record),
                    "vehicle_profile_count": len(record.vehicle_applicability),
                    "markdown_path": str(markdown_path),
                    "html_path": str(html_path),
                    "xml_path": str(xml_path),
                    "status": "exported",
                }
            )
        except Exception as exc:
            manifest_rows.append(
                {
                    "document_id": record.document_id,
                    "title": record.title,
                    "document_type": record.document_type,
                    "project_document_id": record.project_document_id,
                    "chronicle_id": record.chronicle_id,
                    "vcc_number": record.vcc_number,
                    "tree_item_title": record.tree_item_title,
                    "vehicle_applicability": applicability_summary(record),
                    "vehicle_profile_count": len(record.vehicle_applicability),
                    "markdown_path": "",
                    "html_path": "",
                    "xml_path": "",
                    "status": f"error: {exc}",
                }
            )
            print(f"  ERROR: {exc}", file=sys.stderr)

    write_index(index_records)

    write_csv(
        MANIFEST_PATH,
        manifest_rows,
        [
            "document_id",
            "title",
            "document_type",
            "project_document_id",
            "chronicle_id",
            "vcc_number",
            "tree_item_title",
            "vehicle_applicability",
            "vehicle_profile_count",
            "markdown_path",
            "html_path",
            "xml_path",
            "status",
        ],
    )

    write_csv(
        UNRESOLVED_IMAGES_PATH,
        unresolved_images,
        ["document", "title", "image"],
    )

    write_csv(
        UNRESOLVED_LINKS_PATH,
        unresolved_links,
        ["document", "title", "link"],
    )

    print()
    print("Export complete.")
    print(f"Markdown: {MARKDOWN_ROOT}")
    print(f"HTML:     {HTML_ROOT}")
    print(f"XML:      {XML_ROOT}")
    print(f"Preview:  {HTML_ROOT / 'index.html'}")
    print(f"Manifest: {MANIFEST_PATH}")
    print(f"Unresolved images: {len(unresolved_images)}")
    print(f"Unresolved links:  {len(unresolved_links)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
