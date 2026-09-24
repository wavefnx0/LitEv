# LitEv

LitEv is a seed-based literature evolution analysis tool based on a workflow I used for a PhD-thesis. Starting from a publication, it explores literature before and after the seed paper, summarizes topic development, highlights possible transitions, and creates an interactive HTML research dashboard.

litev uses Crossref and OpenAlex for bibliographic metadata. Optional AI features run locally through Ollama.

## Features

- Start from a DOI, ISBN, PMID, PMCID, arXiv ID, OpenAlex work ID, or bibliographic text.
- Explore upstream literature: references cited by the seed and, optionally, references of those papers.
- Explore downstream literature: later papers citing the seed and, optionally, later citing generations.
- Summarize topics represented in the seed paper's references.
- Track topic development and contextual similarity to the seed over time.
- Highlight exploratory transition literature.
- Summarize topics, questions, and reported findings in downstream papers.
- Optionally summarize selected papers' findings with a local Ollama model.
- Optionally generate structured future directions and open research questions using Ollama.
- Export results as HTML, JSON, CSV, Excel, BibTeX, RIS, and Markdown.

## Upstream and downstream hops

The hop settings define how far litev expands around the seed paper.

- **Upstream hops** move backward through references.
  - `1` = papers directly cited by the seed.
  - `2` = also follow references of those papers.
- **Downstream hops** move forward through citing literature.
  - `1` = papers that cite the seed.
  - `2` = also retrieve papers citing those first-generation citing papers.

Higher values can increase runtime and API usage substantially. The GUI provides presets and safety limits for this reason.

## Requirements

- Windows 10/11 for the provided Windows build workflow
- Python 3.11 or newer
- Internet connection for Crossref and OpenAlex
- [Ollama](https://ollama.com/) only if local AI features are enabled

The default Ollama model is `gemma3`, but another locally installed model can be selected in the GUI.

## Quick start from source

Clone or download the repository, open a terminal in the project folder, and install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Start the graphical application:

```bash
python litev.py
```

Then:

1. Enter the seed publication identifier.
2. Choose an analysis preset or set upstream/downstream hops manually.
3. Enable or disable the local AI options.
4. Choose a results folder if desired.
5. Click **Run analysis**.

Each run is saved in its own timestamped results folder. By default, litev uses:

```text
Documents/litev/
```

The HTML dashboard opens automatically when the run finishes unless that option is disabled.

## Optional local AI

AI is not required for the core Crossref/OpenAlex analysis.

To use the AI features, install Ollama and pull a model, for example:

```bash
ollama pull gemma3
```

The GUI can refresh the list of locally available Ollama models.

Two AI-assisted features are available:

- **Paper findings summaries** — concise summaries based on retrieved abstracts.
- **Future directions + open questions** — structured future-oriented analysis using upstream literature context. This option is available only when upstream hops are greater than zero.

AI output should only be treated as model-assisted interpretation, always check the results. The underlying papers and metadata remain the evidential basis of the report.

## Dashboard and outputs

A normal run produces an interactive dashboard plus exports in different formats. Depending on the selected scope and available metadata, the dashboard includes:

- seed-paper overview;
- topics cited by the seed paper;
- potential transition papers;
- contextual similarity to the seed over time;
- citation-impact evidence over time;
- topic development;
- downstream topics, questions, and reported findings;
- optional AI paper summaries;
- optional future directions and open questions;
- methods and data-quality information.

Research exports include:

```text
litev_dashboard.html
litev_report.json
litev_exports/
    litev_papers.csv
    litev_papers.xlsx
    litev_references.bib
    litev_references.ris
    litev_research_brief.md
```

A `litev_run.log` file is also written for diagnostics.

## Windows executable build

The repository includes `build_windows.bat`.

On Windows:

1. Install 64-bit Python 3.11 or newer.
2. Extract or clone the repository.
3. Double-click `build_windows.bat`.
4. After a successful build, open:

```text
litev/litev.exe
```

The build creates two executables:

- `litev.exe` — graphical application
- `litev_engine.exe` — analysis engine used by the GUI

Ollama is still installed separately and is only needed for the optional AI features.

## Command-line use

The engine can also be run directly:

```bash
python litev_engine.py 10.1038/nchem.1111 --max-hops 2 --forward-hops 1
```

Show all options with:

```bash
python litev_engine.py --help
```

Useful options include:

```text
--max-hops             upstream/reference-side depth
--forward-hops         downstream/citing-paper depth
--per-node-cap         maximum references followed per upstream paper
--forward-cap          maximum citing papers retrieved per downstream paper
--max-nodes            overall paper safety cap
--paper-findings-ai    enable local AI findings summaries
--future-ai            enable future directions and open questions
--ai-model             local Ollama model name
--report               JSON output path
--html                 dashboard output path
--export-dir           researcher export directory
```

## Data sources and interpretation

litev combines metadata from Crossref and OpenAlex. Coverage therefore depends on what those services provide for a particular publication and its references/citations.

Important interpretation points:

- A citation relationship is an observed bibliographic link, not proof of intellectual influence or causation.
- Topic labels and citation counts are metadata signals, not measures of scientific quality.
- Contextual similarity compares available titles/abstracts and topic metadata with the seed; missing metadata can reduce coverage.
- Transition indicators are exploratory signals intended to guide reading, not rank papers by importance.
- DOI-bearing references are generally the strongest path for upstream traversal; incomplete reference metadata can limit coverage.
- Downstream question/finding extraction is conservative and depends on available titles and abstracts.
- AI-generated material can be incomplete or incorrect and should be checked against the cited literature.

## Troubleshooting

If a run fails, check the analysis log in the GUI or open the run's `litev_run.log` file. The Copy diagnostics button copies the current log for easier reporting.

Common causes include:

- temporary Crossref/OpenAlex network errors;
- an identifier that cannot be resolved to a work with a DOI;
- incomplete bibliographic metadata;
- Ollama not running when AI is enabled;
- the selected Ollama model not being installed locally.

For Ollama, confirm that it is running and that the model appears in:

```bash
ollama list
```

## Repository files

```text
litev.py                 GUI application
litev_engine.py          literature-analysis engine
requirements.txt         Python dependencies
build_windows.bat        Windows/PyInstaller build script
version_info_*.txt       Windows executable version metadata
README_WINDOWS.txt       short Windows build notes
README.md                this file
```
