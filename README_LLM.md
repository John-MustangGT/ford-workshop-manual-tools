# LLM RAG Guide

This repository can generate a chunked knowledge bundle for use with Open WebUI and a model such as `Qwen:latest`.

The intended flow is:

1. Extract the Ford manual `.arc` files into `extracted/`.
2. Run the RAG exporter to build chunked Markdown files.
3. Import the generated chunk directory into an Open WebUI Knowledge base.
4. Attach that Knowledge base to your Qwen model.

## What The Script Produces

Use `build_rag_knowledge.py` to generate retrieval-friendly chunks from one workshop-manual arc and, by default, its paired wiring arc.

The output goes to:

```text
rag_export/<ARC>/
```

Key outputs:

- `rag_export/<ARC>/chunks/` — one Markdown file per retrieval chunk
- `rag_export/<ARC>/chunks.jsonl` — one JSON record per chunk
- `rag_export/<ARC>/manifest.json` — export metadata

For Open WebUI, the most useful import target is usually the `chunks/` directory.

## Install Dependencies

If you are using the repo-local virtual environment:

```bash
.venv/bin/python -m pip install 'markitdown[pdf]'
```

`markitdown[pdf]` is required so the exporter can convert both HTML and PDF source material.

## Generate A RAG Bundle

Example for the 2014 Mustang workshop manual:

```bash
.venv/bin/python build_rag_knowledge.py --arc SEB
```

That will:

- read `extracted/SEB/`
- include the paired wiring arc automatically when one is known
- write chunked output to `rag_export/SEB/`

## Recommended Chunking For Qwen And Diagnostics

The defaults are chosen for repair procedures, wiring references, and fault isolation steps:

- `--target-words 900`
- `--overlap-words 120`

Why this works well:

- a full removal/installation procedure usually stays together in one chunk
- warnings, torque specs, and numbered steps are less likely to be split apart
- overlap helps preserve context across section boundaries and long diagnostic flows
- chunks are still small enough for focused retrieval in Open WebUI

If you want to adjust the export:

```bash
.venv/bin/python build_rag_knowledge.py \
  --arc SEB \
  --target-words 900 \
  --overlap-words 120
```

Useful options:

- `--no-wiring` — exclude the paired wiring arc
- `--output-root <dir>` — write the export somewhere else
- `--extensions .htm,.html,.pdf` — explicitly control included source types

## Open WebUI Import

Open WebUI Knowledge is the right feature for this content. It uses retrieval instead of injecting the full manual into every prompt.

Basic Knowledge workflow in Open WebUI:

1. Open `Workspace`.
2. Select `Knowledge`.
3. Click `+ New Knowledge`.
4. Give it a name such as `Ford Mustang SEB Workshop Manual`.
5. Add the generated files from `rag_export/SEB/chunks/`.

### Option 1: Upload The Chunk Files

This is the simplest path for a smaller export.

1. Create a new Knowledge base.
2. Upload the Markdown files from `rag_export/SEB/chunks/`.
3. Wait for processing and embedding to finish.

### Option 2: Sync The Chunk Directory

This is the better path for a large export or if you plan to regenerate it often.

1. Create a new Knowledge base.
2. Use `Add Content` -> `Sync Directory`.
3. Choose `rag_export/SEB/chunks/`.
4. Let Open WebUI mirror the directory into the Knowledge base.

This is preferable when you want to rerun the exporter later and update only changed chunks.

## Attach The Knowledge Base To Qwen

After the Knowledge base finishes processing:

1. Open `Workspace` -> `Models`.
2. Edit your `Qwen:latest` model entry.
3. Attach the new Knowledge base.
4. Keep it in `Focused Retrieval` mode for this manual corpus.

`Focused Retrieval` is the correct mode for this use case because the manual is large and only a few chunks should be pulled into a given answer.

## Recommended Prompting Behavior

When you query the model, ask it to stay grounded in the manual and cite the relevant section or procedure title when possible.

Good general prompt pattern:

- `Answer only from the attached Ford workshop-manual knowledge base. If the answer is incomplete, say what information is missing. Cite the procedure title or section when possible.`

Example prompts for diagnostics:

- `How do I diagnose a clutch pedal that stays on the floor on a 2014 Mustang GT? Use the workshop manual knowledge base and list the checks in order.`
- `What is the workshop-manual diagnostic path for a no-crank condition on a 2014 Mustang GT? Start with the first checks and include likely related circuits or components.`
- `My Mustang has a battery drain. Using the manual knowledge base, what charging-system and wiring checks should I perform first?`
- `What are the workshop-manual steps to diagnose an A/C system that does not cool, and what prerequisites should be verified before deeper testing?`

Example prompts for repairs and service procedures:

- `Find the workshop-manual steps for clutch disc and pressure plate removal on the 5.0L car.`
- `Show the removal and installation procedure for the starter motor on a 2014 Mustang GT, including any warnings, special tools, and torque notes.`
- `Summarize the workshop-manual procedure for replacing front brake pads and call out any one-time-use fasteners or caution notes.`
- `Find the manual procedure for removing the transmission and list the major prerequisite steps before the unit is lowered.`

Example prompts for wiring and electrical troubleshooting:

- `What wiring or component checks should I do first for a no-crank condition? Use the workshop manual and wiring knowledge.`
- `Using the attached wiring knowledge, identify the likely power, ground, fuse, and relay path for the fuel pump circuit on a 2014 Mustang.`
- `Help me troubleshoot an inoperative blower motor using the workshop manual and wiring information. Give me the check order and what readings I should expect.`
- `Which connectors, grounds, or splice points are most relevant to diagnosing intermittent power window failure?`

Example prompts for grounded answers instead of guesswork:

- `Answer only from the imported Ford manual knowledge base. Do not guess beyond the retrieved material.`
- `If multiple procedures are relevant, compare them and tell me which one applies to the 5.0L Mustang GT.`
- `Give me the exact workshop-manual sequence first, then add a short plain-English explanation.`
- `List the warnings, cautions, special tools, and torque-sensitive steps separately from the main procedure.`

## Important Open WebUI Behavior

If native function calling is enabled in Open WebUI, attached knowledge is not always auto-injected. In that mode, the model may need to call the Knowledge tools explicitly.

If the model seems to ignore the imported manual:

1. Make sure the Knowledge base is attached to the model.
2. Keep the attachment in `Focused Retrieval` mode.
3. If needed, disable native function calling for that model so normal RAG injection is used.
4. Alternatively, add a system instruction telling the model to use the attached Knowledge base for Ford workshop-manual questions.

## Practical Example End To End

Generate the export:

```bash
.venv/bin/python build_rag_knowledge.py --arc SEB
```

Then import this directory into Open WebUI:

```text
rag_export/SEB/chunks/
```

Attach the Knowledge base to `Qwen:latest`, and ask questions about Mustang repair procedures, diagnostics, and wiring.

## Notes

- The exporter uses the workshop-manual HTML cleanup path for `.HTM` pages, so the chunks are cleaner than raw HTML conversion.
- PDFs are converted through MarkItDown.
- Wiring content is included automatically unless `--no-wiring` is used.
- If you are importing a very large library, Open WebUI directory sync is usually better than manually uploading thousands of files.