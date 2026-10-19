# Volvo VIDA Service Document Exporter

This Python script exports static service/reference content from a locally attached Volvo VIDA SQL Server database into formats that are easier to browse, inspect, and use with local RAG/LLM tools.

The exporter is intended for research, archival, and personal analysis of an existing VIDA database installation. It does **not** include Volvo service data itself.

## What the script exports

The script reads all available documents for all vehicle models and years for these VIDA document types:

- `i2` — component/system reference information
- `sbull` — service bulletins
- `servinfosub` — reusable service subdocuments, component references, special tools, etc.
- `torqueinfo` — torque/specification information

Documents are exported into three parallel formats:

- **Markdown (`.md`)** — useful for RAG ingestion, text search, and LLM workflows
- **HTML (`.html`)** — useful for normal browser-based viewing
- **Original XML (`.xml`)** — preserved for reverse engineering and troubleshooting

The script also generates an HTML index, a CSV manifest, and reports for unresolved images and document references.

## Key features

### Vehicle applicability

The exporter preserves both forms of VIDA vehicle applicability:

- **Direct document applicability** from `DocumentProfile`
- **Navigational context applicability** from `TreeItemProfile`

Vehicle profile IDs are resolved through the `basedata` database into readable values such as:

- vehicle model
- model year
- engine
- transmission
- body style
- steering
- suspension
- brake system
- chassis range
- partner/market groups

This allows exported documents to retain the vehicle context that VIDA normally applies dynamically.

### Function-group navigation

The exporter reads VIDA's function-group hierarchy and adds it to each document.

The generated `index.html` uses readable link labels such as:

```text
3 Electrical system : 39 Media, communication and navigation : Loudspeaker, rear side door
```

Index entries are sorted numerically by Function Group 1 and Function Group 2, with Function Group 3 and title used as additional tie-breakers.

### Cross-document references

VIDA uses several identifier formats when one document refers to another. The exporter resolves references using:

```text
0800...          Document.projectDocumentId
0900...          Document.chronicleId
VCC-370333-13    Document.vccNumber
```

These identifiers are resolved to the appropriate exported `servinfosub` document when possible.

Examples include:

- related component documents
- removal/installation prerequisites
- special-tool references
- reusable service-information subdocuments

Generated HTML and Markdown links point to the corresponding local exported document.

Original XML fragment anchors such as:

```text
#nev13713390n1
```

are intentionally not carried into generated local links because VIDA's XML element IDs do not always map reliably to the generated HTML anchors.

### Special-tool references

Some VIDA documents contain raw references such as:

```text
en-US0800c8af80a2e629
```

The exporter resolves these references against the database so that a raw identifier can become something readable such as:

```text
999 7103 Lifting beam
```

### Images

The script resolves VIDA image references against a previously extracted image repository.

It handles image references found both in normal XML content and inside tables, including filenames such as:

```text
0900c8af8162067b_34_34.gif
```

HTML output renders these as actual `<img>` elements rather than plain filenames.

Unresolved image references are written to:

```text
unresolved-images.csv
```

## Database requirements

The script currently expects Microsoft SQL Server with these databases attached:

```text
servicerep_en-US
basedata
```

`servicerep_en-US` contains the service documents, navigation structure, XML packages, document identifiers, and profile assignments.

`basedata` contains the vehicle-profile definitions and lookup tables used to decode vehicle applicability.

Important tables used by the exporter include:

```text
servicerep_en-US.dbo.Document
servicerep_en-US.dbo.DocumentType
servicerep_en-US.dbo.DocumentProfile
servicerep_en-US.dbo.TreeItem
servicerep_en-US.dbo.TreeItemDocument
servicerep_en-US.dbo.TreeItemProfile
servicerep_en-US.dbo.FunctionGroupText

basedata.dbo.VehicleProfile
basedata.dbo.VehicleProfilePartnerGroup
basedata.dbo.VehicleModel
basedata.dbo.ModelYear
basedata.dbo.Engine
basedata.dbo.Transmission
basedata.dbo.BodyStyle
basedata.dbo.Steering
basedata.dbo.Suspension
basedata.dbo.BrakeSystem
```

## Requirements

### Python

Python 3 is required.

Install the Python dependency:

```powershell
pip install pyodbc
```

### SQL Server ODBC driver

The script is configured for:

```text
ODBC Driver 18 for SQL Server
```

The Microsoft ODBC Driver for SQL Server must therefore be installed on the machine running the exporter.

### SQL Server authentication

The default connection uses Windows integrated authentication:

```python
SERVER = r"localhost"
DATABASE = "servicerep_en-US"
```

with:

```text
Trusted_Connection=yes
TrustServerCertificate=yes
```

Modify the connection settings near the top of the script if your SQL Server instance or authentication method is different.

## Configuration

The main configuration values are near the top of the script.

### SQL Server

```python
SERVER = r"localhost"
DATABASE = "servicerep_en-US"
```

### Output directory

```python
OUTPUT_ROOT = Path(r"D:\Volvo DB\Extracted\All_Models_ServiceExport")
```

### Extracted image repository

```python
IMAGE_ROOT = Path(r"D:\Volvo DB\Extracted\All")
```

Change these paths to match your environment.

### Document types

The current export is limited to:

```python
ALLOWED_DOCUMENT_TYPES = (
    "i2",
    "sbull",
    "servinfosub",
    "torqueinfo",
)
```

Other VIDA document types can be added if their XML structures are compatible with the renderer.

## Running the exporter

From PowerShell or Command Prompt:

```powershell
python .\export_all_models_reference_docs.py
```

The script reports progress while querying SQL Server and while exporting documents.

Typical startup output resembles:

```text
Querying SQL Server for all models and years...
  Preparing all-model, all-year document selection...
  Stage 1/4: loading documents and XML packages...
  Stage 2/4: loading vehicle-profile market groups...
  Stage 3/4: loading direct document applicability...
  Stage 4/4: loading navigation-tree applicability...
  Loading servinfosub project/chronicle/VCC reference titles...
```

It then reports export progress periodically.

## Output structure

By default, the script creates:

```text
D:\Volvo DB\Extracted\All_Models_ServiceExport\
│
├── html\
│   ├── index.html
│   ├── 0900c8af....html
│   └── ...
│
├── markdown\
│   ├── 0900c8af....md
│   └── ...
│
├── xml\
│   ├── 0900c8af....xml
│   └── ...
│
├── manifest.csv
├── unresolved-images.csv
└── unresolved-links.csv
```

### `html/index.html`

Provides a browser-friendly index of the exported documents.

Entries are organized by VIDA function groups and show the document type.

### `manifest.csv`

Contains one row per processed document, including fields such as:

```text
document_id
title
document_type
project_document_id
chronicle_id
vcc_number
tree_item_title
vehicle_applicability
vehicle_profile_count
markdown_path
html_path
xml_path
status
```

The manifest is useful for auditing, RAG preprocessing, scripting, and troubleshooting.

### `unresolved-images.csv`

Lists image references that could not be found under the configured `IMAGE_ROOT`.

### `unresolved-links.csv`

Lists document references the exporter could not resolve.

## How VIDA XML is stored

The VIDA `Document.XmlContent` column does not contain plain XML directly.

Each value is a ZIP package stored as a SQL Server binary/BLOB. The script:

1. reads the BLOB from SQL Server
2. opens the ZIP package in memory
3. locates the embedded XML file
4. preserves the original XML
5. parses the XML
6. renders HTML and Markdown versions

No temporary ZIP extraction is required.

## Vehicle applicability model

VIDA can attach applicability in two different places.

### Direct applicability

```text
Document
  -> DocumentProfile
  -> basedata.dbo.VehicleProfile
```

This represents applicability assigned directly to the document.

### Navigation-context applicability

```text
Document
  -> TreeItemDocument
  -> TreeItem
  -> TreeItemProfile
  -> basedata.dbo.VehicleProfile
```

This represents the vehicle context in which VIDA exposes the document through its navigation tree.

Both are retained because a generic document may have broad direct applicability while being presented in a more specific model/year navigation context.

## Market / partner-group codes

The database defines the following partner groups:

```text
AME  Volvo Cars North America
CHI  Partner Group China
EUR  Volvo Cars Europe
INT  International Misc
NOR  Volvo Cars Nordic Countries
```

The script currently preserves the database codes in the vehicle-applicability metadata.

## Notes about filenames

Generated document filenames are primarily based on the document's `chronicleId` (`0900...`) when available.

A document may also have:

```text
projectDocumentId  0800...
chronicleId         0900...
vccNumber           VCC-...
```

These are separate VIDA identifiers for the same underlying document record or document family.

## Intended use with local RAG systems

The Markdown output is suitable as a source collection for local retrieval-augmented generation systems such as Open WebUI with Ollama.

Because the exporter preserves:

- vehicle applicability
- function groups
- document type
- titles
- document identifiers
- procedure structure
- links to related documents

the Markdown files provide substantially more retrieval context than exporting only the visible body text.

For large RAG collections, additional semantic chunking and metadata-aware retrieval may still improve search quality.

## Limitations

This project is based on reverse engineering of the database structure rather than official Volvo schema documentation.

Known limitations include:

- not every VIDA XML element has a custom renderer
- some rare document-reference types may remain unresolved
- image lookup depends on the external extracted image repository
- XML-to-Markdown conversion may simplify complex VIDA layouts
- generated HTML is intended for local reference rather than exact reproduction of the original VIDA UI
- only the configured document types are exported
- applicability and navigation relationships reflect the database records available in the attached VIDA data set

The original XML export is retained specifically so questionable or incomplete conversions can be inspected against the source.

## Project status

The exporter currently supports:

- all models and model years
- `i2`, `sbull`, `servinfosub`, and `torqueinfo`
- HTML, Markdown, and original XML output
- vehicle applicability decoding
- function-group hierarchy
- human-readable HTML index
- special-tool references
- `projectDocumentId`, `chronicleId`, and `VCC` cross-references
- inline/table images
- export manifest
- unresolved-reference reporting

## Disclaimer

Volvo, VIDA, and related names are trademarks of their respective owners.

This repository should contain only the exporter/source code and documentation. It should not include proprietary Volvo VIDA databases, service documents, images, or other copyrighted source data unless you have the right to distribute them.

The script is an independent reverse-engineering and data-conversion utility and is not affiliated with or endorsed by Volvo Cars.
