# Volvo VIDA Local RAG Assistant

This project is intended to turn an exported Volvo VIDA service-information library into a **local, offline retrieval-augmented generation (RAG) assistant** that mechanics can query in natural language.

The goal is not to build a generic automotive chatbot.

The goal is to build a system that answers questions from the **factory service information that was exported from VIDA**, so that a mechanic can ask a question such as:

```text
How do I remove the cylinder head on a 2005 Volvo S40?
```

and receive an answer grounded in the exported factory documentation rather than relying primarily on forum posts, blog articles, social media, or general automotive knowledge.

## Project goal

The intended end state is:

```text
Volvo VIDA SQL databases
        ↓
Python exporter
        ↓
HTML / Markdown / XML service documents
        ↓
RAG ingestion and chunking
        ↓
Embedding model
        ↓
Local vector database
        ↓
Ollama-hosted language model
        ↓
Mechanic asks a question
        ↓
Relevant factory-manual sections are retrieved
        ↓
Answer is generated from those retrieved sources
```

The user should be able to query the system conversationally while still retaining traceability back to the underlying factory documents.

## Why use RAG?

A normal language model answers from its training data and general knowledge.

That is useful for broad questions, but it is not ideal when the objective is:

- factory-specific repair instructions
- exact component names
- Volvo-specific tool references
- torque specifications
- model-year applicability
- engine-specific procedures
- service bulletins
- connector and component identifiers
- chassis-range restrictions

A RAG system changes the workflow.

Instead of asking the model to answer entirely from memory, the system first searches the local VIDA knowledge base and retrieves the most relevant document sections.

Those sections are then supplied to the model as context for the answer.

Conceptually:

```text
Question
   ↓
Retrieve relevant VIDA chunks
   ↓
Send question + retrieved VIDA text to local LLM
   ↓
Generate answer grounded in VIDA
```

This makes the factory documentation the primary source of truth.

## Primary design principle

The assistant should prefer:

```text
Factory VIDA documentation
```

over:

```text
general internet knowledge
forum posts
blog posts
YouTube descriptions
community repair guides
uncited model knowledge
```

The system should not silently fill in missing factory information from unrelated sources.

When the retrieved VIDA documents do not contain the requested information, the assistant should say so clearly.

For example:

```text
I did not find a torque specification for this fastener in the retrieved VIDA documents.
```

That is preferable to inventing or guessing a specification.

## Intended users

This system is aimed at:

- professional mechanics
- independent repair shops
- Volvo enthusiasts
- technicians working on older Volvo vehicles
- owners performing factory-guided repairs
- researchers examining VIDA service-information structure

The interface should allow normal workshop-style questions rather than requiring database knowledge.

Examples:

```text
How do I replace the thermostat on a 2005 S40?
```

```text
What special tools are required to remove the cylinder head?
```

```text
What does VIDA say about removing the fuel pump?
```

```text
Does this procedure apply to the B5254T3 engine?
```

```text
What torque values are specified for the camshaft bearing caps?
```

```text
Which service bulletins apply to the steering system?
```

## Local / offline architecture

The intended stack is fully local.

A representative architecture is:

```text
Markdown VIDA export
        ↓
Open WebUI Knowledge / local RAG layer
        ↓
EmbeddingGemma
        ↓
Local vector database
        ↓
Ollama
        ↓
Gemma-family or other local instruction model
        ↓
Open WebUI chat interface
```

No cloud LLM is required for the core workflow.

This is useful for:

- privacy
- offline workshop use
- predictable access
- avoiding external API costs
- keeping proprietary or licensed service data local
- controlling exactly which documents are indexed

## Ollama's role

Ollama provides the local model runtime.

It can host both:

- the embedding model
- the language model used to answer questions

Example embedding model:

```text
embeddinggemma
```

Example answering model:

```text
Gemma-family instruction model
```

The exact answering model can change over time without rebuilding the entire document export.

The embedding model should remain consistent for a given vector index unless the documents are re-embedded.

## EmbeddingGemma's role

`embeddinggemma` converts text into numerical vectors that represent semantic meaning.

For example:

```text
"remove cylinder head"
```

and:

```text
"cylinder head removal procedure"
```

should produce vectors that are relatively close in semantic space.

This allows the retrieval system to find relevant VIDA content even when the user's wording differs from the exact wording in the manual.

Embedding generation happens during two stages:

### Document ingestion

Each document chunk is embedded and stored in the vector database.

```text
VIDA chunk
    ↓
embeddinggemma
    ↓
vector
```

### User query

The mechanic's question is embedded using the same model.

```text
Question
    ↓
embeddinggemma
    ↓
query vector
```

The vector database then finds the closest matching document chunks.

## Why document structure matters

The exporter preserves information that is important for accurate retrieval:

- document title
- document type
- function group
- direct vehicle applicability
- navigation-context applicability
- model year
- engine
- transmission
- chassis range
- market / partner group
- document IDs
- related tool references
- related service subdocuments

This metadata is critical because automotive service information is highly contextual.

A repair procedure for one model or engine may be incorrect for another.

For example, a question about:

```text
2005 S40 2.5T
```

should not accidentally retrieve a procedure specific to:

```text
1999 S80
```

even if the two documents contain similar language.

## Vehicle applicability

The exported documents preserve two important VIDA applicability concepts.

### Direct document applicability

This comes from the document itself.

Example:

```text
Direct document applicability
S40, 2005
```

or:

```text
Direct document applicability
All Models*
```

### Navigational context applicability

This comes from the VIDA navigation tree in which the document appears.

Example:

```text
Navigational context applicability
S40, 2005
```

A generic document may be reusable across many vehicles but appear in a specific vehicle context in VIDA.

Both forms should remain available to the retrieval system.

## Document types

The current export focuses on:

```text
i2
sbull
servinfosub
torqueinfo
```

These serve different purposes.

### `i2`

These appear to be component or system reference documents.

Typical content includes:

- component designators
- part numbers
- connectors
- system descriptions
- supporting reference information

They generally do not contain full removal or repair procedures.

### `sbull`

Service bulletin documents.

These may contain:

- known issues
- updated procedures
- product changes
- service recommendations
- corrective actions

### `servinfosub`

Reusable service-information subdocuments.

These often include:

- component-specific procedures
- related procedures
- special-tool information
- reusable service blocks
- supporting instructions

### `torqueinfo`

Torque and specification information.

These documents are particularly valuable for exact-value queries.

## Cross-document references

VIDA frequently references related documents.

The exporter resolves several identifier formats:

```text
0800...          projectDocumentId
0900...          chronicleId
VCC-370333-13    vccNumber
```

This allows related documents to remain connected after export.

Example:

```text
Air cleaner housing
VCC-370333-13
```

can resolve to the local exported service subdocument for that component.

This is important because a parent repair procedure may not contain every prerequisite step inline.

A mechanic's question may therefore require retrieval from more than one related document.

## Special tools

Some procedures contain references to Volvo special tools.

Examples:

```text
999 7103 Lifting beam
999 7170 Guide pin
999 7355 Installation tools
```

The exporter resolves those references so that the RAG system can retrieve meaningful tool names instead of opaque internal IDs.

This enables questions such as:

```text
What special tools are required for this job?
```

## Images

Some exported documents contain factory illustrations.

The exporter resolves image references and renders them in the HTML output.

The RAG system is initially text-focused, but image filenames and surrounding descriptions remain useful metadata.

A future multimodal version of the project could allow a vision-capable local model to inspect the associated illustrations.

## Recommended chunking strategy

The Markdown documents should not simply be split into arbitrary fixed-size blocks without considering structure.

Better chunk boundaries include:

- major headings
- procedure groups
- removal sections
- installation sections
- individual steps
- warnings
- notes
- torque/specification tables
- special-tool sections

A practical starting target is:

```text
300–800 tokens per chunk
```

with modest overlap when needed.

The important principle is:

> Keep a complete procedural thought together whenever possible.

For example, do not split a warning away from the step it applies to.

## Metadata that should accompany each chunk

Each chunk should retain enough context to stand on its own.

Recommended metadata includes:

```yaml
document_id: ...
project_document_id: ...
chronicle_id: ...
vcc_number: ...
title: ...
document_type: ...
function_group_1: ...
function_group_2: ...
function_group_3: ...
vehicle_models: ...
model_years: ...
engines: ...
transmissions: ...
markets: ...
source_file: ...
section: ...
```

This allows retrieval to combine semantic similarity with exact metadata filtering.

## Retrieval strategy

A strong retrieval process should use both semantic and exact matching.

For example, for:

```text
2005 S40 B5254T3 thermostat removal
```

the system should consider:

- semantic similarity to "thermostat removal"
- exact model match: S40
- exact year match: 2005
- exact engine match: B5254T3
- applicable generic documents: All Models
- related service subdocuments
- torque documents
- relevant bulletins

The system should down-rank documents that are explicitly tied to unrelated vehicles.

## Exact identifiers matter

Automotive service data includes many terms that semantic search alone may not handle perfectly.

Examples:

```text
B5254T3
VCC-370346-17
999 7103
P0171
CMP
ECM
```

These should remain visible in the chunk text and metadata.

A robust implementation may eventually combine:

```text
vector search
+
keyword search
+
metadata filtering
```

This is often called hybrid retrieval.

## Answer-generation rules

The answering model should be instructed to behave conservatively.

A suitable system prompt might include rules such as:

```text
Use the retrieved Volvo VIDA documents as the primary source.

Prefer documents whose vehicle applicability matches the requested
model, year, engine, transmission, market, and chassis range.

Do not invent missing procedures, torque values, specifications,
part numbers, or special tools.

When the factory documents do not contain the requested information,
say so clearly.

Preserve warnings, cautions, procedural order, and numerical values.

Cite the source document title and identifier used for the answer.

Distinguish between direct applicability and navigational applicability
when relevant.
```

## Example end-user interaction

A mechanic asks:

```text
What do I need to remove before removing the cylinder head on a 2005 S40?
```

The RAG layer retrieves:

- cylinder head procedure
- related removal prerequisites
- special-tool references
- component subdocuments
- relevant torque/specification documents

The model then answers using those retrieved factory sources.

An ideal response might look conceptually like:

```text
According to the retrieved VIDA cylinder-head procedure, the following
components must be removed before cylinder-head removal:

- air cleaner housing
- exhaust manifold
- intake manifold
- glow plugs
- brake-booster vacuum pump
- thermostat
- fuel pump
- camshaft position sensor
- camshaft seal

VIDA also references the following special tools:
- 999 7103 Lifting beam
- 999 7170 Guide pin
...

Sources:
- Cylinder head — [document ID]
- Air cleaner housing — VCC-370333-13
- Thermostat — VCC-370346-17
```

The exact answer should come from the retrieved factory text rather than this example.

## Traceability

Every answer should make it possible to trace information back to the source.

Useful citation fields include:

```text
Document title
Document type
Chronicle ID
Project document ID
VCC number
Function group
Vehicle applicability
```

The user should be able to open the exported HTML document and verify the answer manually.

## Avoiding hallucinations

The most important safety mechanism is retrieval discipline.

The model should not be encouraged to "fill in the blanks."

For technical service information, a confident but unsupported answer can be worse than no answer.

The desired behavior is:

```text
Factory documentation found
    → answer from factory documentation

Factory documentation incomplete
    → explain what is missing

No relevant factory documentation found
    → say no relevant VIDA source was retrieved
```

## Open WebUI

Open WebUI is a convenient interface for this project because it can provide:

- chat UI
- Ollama integration
- local knowledge bases
- document ingestion
- embedding configuration
- vector retrieval
- source references

A possible Knowledge Base name is:

```text
Volvo VIDA Service Information
```

The exported Markdown files can be ingested into that knowledge base.

## Initial test process

Before indexing the full exported collection, begin with a smaller representative sample.

Recommended test set:

```text
100–300 documents
```

Include:

- one known vehicle/model-year combination
- generic "All Models" documents
- `i2` reference documents
- `servinfosub` procedures
- torque information
- service bulletins
- several documents with cross-references

Then test questions where the expected answer is already known from the source documents.

Review:

- which chunks were retrieved
- whether vehicle applicability was respected
- whether unrelated models were retrieved
- whether exact identifiers were found
- whether source references were shown
- whether the final answer remained faithful to the manuals

## Full-scale collection

The exported VIDA library can contain tens of thousands of Markdown documents.

This is still a practical size for a local RAG system.

The main scaling concern is not the number of files itself, but:

```text
number of chunks
embedding size
index size
retrieval quality
metadata design
```

A collection of tens or hundreds of thousands of chunks is reasonable for a local vector database.

## Incremental indexing

The system should eventually track:

```text
source filename
content hash
document ID
chunk ID
embedding status
```

This allows changed documents to be re-indexed without rebuilding the entire knowledge base.

## Future improvements

Potential future enhancements include:

- custom semantic chunking based on VIDA XML structure
- JSONL chunk export
- hybrid keyword + vector retrieval
- deterministic vehicle applicability filtering
- direct model/year/engine selectors in the UI
- automatic source citation formatting
- related-document graph traversal
- retrieval of neighboring procedural steps
- image-aware RAG using a multimodal model
- separate indexes for torque/specification lookups
- query expansion for Volvo abbreviations and component names
- better ranking of exact engine codes and VCC identifiers

## Current project status

At this stage:

- VIDA database content can be exported
- HTML output is browser-readable
- Markdown output is available for RAG ingestion
- original XML is preserved
- vehicle applicability is decoded
- function groups are preserved
- special-tool references are resolved
- VCC / project / chronicle document references are resolved
- images are rendered in exported HTML
- Ollama is running locally
- `embeddinggemma` has been installed and tested
- Open WebUI can reach Ollama's embedding endpoint

The next phase is document ingestion, chunking, retrieval testing, and answer-quality validation.

## Repository scope

A GitHub repository for this project should contain:

```text
README files
Python exporter
RAG ingestion scripts
configuration examples
sample schemas
documentation
```

It should **not** contain proprietary Volvo VIDA database files, exported factory manuals, image repositories, or other copyrighted service data unless redistribution rights are available.

## Disclaimer

Volvo, VIDA, and related names are trademarks of their respective owners.

This project is an independent reverse-engineering, archival, and local-search utility. It is not affiliated with or endorsed by Volvo Cars.

Users are responsible for ensuring they have the legal right to access, process, and use the service information supplied to the system.
