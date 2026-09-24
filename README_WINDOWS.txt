litev 1.0.1 - Windows

Build on Windows
----------------
1. Install 64-bit Python 3.11 or newer and make sure the `py` launcher works.
2. Install Ollama separately if you want to use the local AI features, and pull at least one model such as `gemma3`.
3. Extract this source folder.
4. Double-click `build_windows.bat`.
5. Run `litev\litev.exe` from the build output folder.

The Windows build creates two single-file executables:
- `litev.exe` — graphical interface
- `litev_engine.exe` — analysis engine launched by the GUI

The GUI accepts DOI, ISBN, PMID, PMCID, arXiv ID, OpenAlex work ID, or bibliographic text as the seed literature identifier. It also lets you select a local Ollama model and set upstream/downstream analysis depth.

After a successful analysis, `litev_dashboard.html` is opened automatically in the default browser. The dashboard does not display a citation graph or lineage visualization; citation relationships are used internally for scope and analysis.

Release: litev 1.0.1

Results are stored in a timestamped folder under Documents\litev by default, so previous analyses are not overwritten. The results location can be changed in the GUI.


## Failure diagnostics

Each analysis run writes `litev_run.log` inside its timestamped results folder. If an analysis fails, the GUI shows the most specific engine error it can find and enables **Copy diagnostics**. The log is the first place to check for network/API, identifier-resolution, Ollama, or packaging errors.


Windows text encoding:
The GUI forces the bundled analysis engine to use UTF-8 output so Unicode characters in bibliographic metadata cannot crash a run on legacy Windows code pages.
