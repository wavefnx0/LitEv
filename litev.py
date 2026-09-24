import os
import sys
import threading
import subprocess
import queue
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "litev"
APP_VERSION = "1.0.1"
APP_TITLE = f"{APP_NAME} {APP_VERSION} — Literature Evolution Analysis"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_results_root() -> Path:
    home = Path.home()
    docs = home / "Documents"
    return (docs if docs.exists() else home) / "litev"


def engine_command():
    """Return the command prefix used to launch the analysis engine."""
    if getattr(sys, "frozen", False):
        engine = app_dir() / "litev_engine.exe"
        return [str(engine)] if engine.exists() else None
    script = app_dir() / "litev_engine.py"
    return [sys.executable, str(script)] if script.exists() else None


class ExplorerApp(tk.Tk):
    PRESETS = {
        "Standard (balanced)": (2, 1, 70, 20, 1000),
        "Upstream context": (3, 0, 90, 25, 1200),
        "Downstream evolution": (0, 2, 60, 20, 1000),
        "Broader both directions": (3, 2, 90, 25, 1800),
    }

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = max(820, min(980, screen_w - 80))
        win_h = max(680, min(790, screen_h - 90))
        x = max(0, (screen_w - win_w) // 2)
        y = max(0, (screen_h - win_h) // 3)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(min(820, win_w), min(680, win_h))
        self.configure(padx=20, pady=16)
        self.proc = None
        self.messages = queue.Queue()
        self.html_path = None
        self.current_run_dir = None
        self.log_path = None

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(16, 8))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Analyze the literature before and after a seed paper, summarize topic development, and generate a self-contained research dashboard.",
            wraplength=860,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        main = ttk.Frame(self)
        main.grid(row=1, column=0, sticky="ew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        seed_card = ttk.LabelFrame(main, text="Seed literature", padding=14, style="Section.TLabelframe")
        seed_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        seed_card.columnconfigure(1, weight=1)
        ttk.Label(seed_card, text="Identifier", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.identifier = ttk.Entry(seed_card, font=("Segoe UI", 11))
        self.identifier.grid(row=0, column=1, sticky="ew")
        self.identifier.insert(0, "10.1038/nchem.1111")
        ttk.Label(
            seed_card,
            text="DOI, ISBN, PMID, PMCID, arXiv ID, OpenAlex work ID, or bibliographic text. DOI resolution is shown in the analysis log.",
            foreground="#666",
            wraplength=790,
        ).grid(row=1, column=1, sticky="w", pady=(5, 0))

        scope = ttk.LabelFrame(main, text="Analysis scope", padding=14, style="Section.TLabelframe")
        scope.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(0, 10))
        scope.columnconfigure(1, weight=1)

        ttk.Label(scope, text="Preset").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.preset = ttk.Combobox(scope, values=list(self.PRESETS) + ["Custom"], state="readonly", width=24)
        self.preset.grid(row=0, column=1, sticky="ew", pady=4)
        self.preset.set("Standard (balanced)")
        self.preset.bind("<<ComboboxSelected>>", self.apply_preset)

        self.hops = tk.IntVar(value=2)
        self.forward_hops = tk.IntVar(value=1)
        self.per_node_cap = tk.IntVar(value=70)
        self.forward_cap = tk.IntVar(value=20)
        self.max_nodes = tk.IntVar(value=1000)

        ttk.Label(scope, text="Upstream hops").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.up_spin = ttk.Spinbox(scope, from_=0, to=6, width=7, textvariable=self.hops, command=self.scope_changed)
        self.up_spin.grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(scope, text="References cited by the seed, then references of those papers.", foreground="#666", wraplength=360).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))

        ttk.Label(scope, text="Downstream hops").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.down_spin = ttk.Spinbox(scope, from_=0, to=3, width=7, textvariable=self.forward_hops, command=self.scope_changed)
        self.down_spin.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(scope, text="Later papers that cite the seed or the retrieved downstream branch. Their topics, questions and reported findings are summarized automatically without AI.", foreground="#666", wraplength=360).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 5))

        self.scope_summary = ttk.Label(scope, foreground="#40516d", wraplength=360, font=("Segoe UI", 9, "bold"))
        self.scope_summary.grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 3))
        self.advanced_visible = False
        self.advanced_btn = ttk.Button(scope, text="Show advanced limits ▸", command=self.toggle_advanced)
        self.advanced_btn.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 2))

        self.caps = ttk.Frame(scope)
        caps = self.caps
        caps.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for c in range(3):
            caps.columnconfigure(c, weight=1)
        ttk.Label(caps, text="References / paper", foreground="#555").grid(row=0, column=0, sticky="w")
        ttk.Label(caps, text="Citing papers / paper", foreground="#555").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(caps, text="Max papers", foreground="#555").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.ref_cap_spin = ttk.Spinbox(caps, from_=10, to=250, increment=10, width=9, textvariable=self.per_node_cap, command=self.scope_changed)
        self.ref_cap_spin.grid(row=1, column=0, sticky="w")
        self.forward_cap_spin = ttk.Spinbox(caps, from_=5, to=100, increment=5, width=9, textvariable=self.forward_cap, command=self.scope_changed)
        self.forward_cap_spin.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self.max_nodes_spin = ttk.Spinbox(caps, from_=100, to=5000, increment=100, width=9, textvariable=self.max_nodes, command=self.scope_changed)
        self.max_nodes_spin.grid(row=1, column=2, sticky="w", padx=(8, 0))
        self.caps.grid_remove()

        ai = ttk.LabelFrame(main, text="Local AI", padding=14, style="Section.TLabelframe")
        ai.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(0, 10))
        ai.columnconfigure(0, weight=1)

        self.findings_var = tk.BooleanVar(value=True)
        self.future_var = tk.BooleanVar(value=True)
        self.findings_check = ttk.Checkbutton(ai, text="Summarize selected papers' findings", variable=self.findings_var, command=self.update_ai_state)
        self.findings_check.grid(row=0, column=0, sticky="w")
        ttk.Label(ai, text="Uses only each paper's retrieved abstract; unavailable abstracts are not inferred.", foreground="#666", wraplength=360).grid(row=1, column=0, sticky="w", pady=(1, 8))
        self.future_check = ttk.Checkbutton(ai, text="Future directions + open questions", variable=self.future_var, command=self.update_ai_state)
        self.future_check.grid(row=2, column=0, sticky="w")
        ttk.Label(ai, text="Uses upstream context only and is available only when upstream hops are greater than 0.", foreground="#666", wraplength=360).grid(row=3, column=0, sticky="w", pady=(1, 10))

        ai_opts = ttk.Frame(ai)
        ai_opts.grid(row=4, column=0, sticky="ew")
        ai_opts.columnconfigure(1, weight=1)
        ttk.Label(ai_opts, text="Paper summaries").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.findings_max = tk.IntVar(value=8)
        self.findings_max_spin = ttk.Spinbox(ai_opts, from_=1, to=30, width=6, textvariable=self.findings_max)
        self.findings_max_spin.grid(row=0, column=1, sticky="w")

        ttk.Label(ai, text="Ollama model", font=("Segoe UI", 9, "bold")).grid(row=5, column=0, sticky="w", pady=(10, 3))
        model_row = ttk.Frame(ai)
        model_row.grid(row=6, column=0, sticky="ew")
        model_row.columnconfigure(0, weight=1)
        self.model = ttk.Combobox(model_row, font=("Segoe UI", 10), values=("gemma3", "gemma3:12b", "llama3.1", "mistral"))
        self.model.grid(row=0, column=0, sticky="ew")
        self.model.set("gemma3")
        self.refresh_btn = ttk.Button(model_row, text="Refresh models", command=self.refresh_models)
        self.refresh_btn.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(ai, text="Ollama runs locally. No cloud API key is used by litev.", foreground="#666").grid(row=7, column=0, sticky="w", pady=(6, 0))

        output = ttk.LabelFrame(main, text="Results", padding=14, style="Section.TLabelframe")
        output.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="Results folder").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.output_root = tk.StringVar(value=str(default_results_root()))
        self.output_entry = ttk.Entry(output, textvariable=self.output_root)
        self.output_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(output, text="Browse…", command=self.browse_output).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(output, text="Each run gets its own timestamped folder, so earlier reports are not overwritten. AI caches are shared inside this results folder.", foreground="#666", wraplength=780).grid(row=1, column=1, sticky="w", pady=(5, 0))
        self.auto_open = tk.BooleanVar(value=True)
        ttk.Checkbutton(output, text="Open dashboard when finished", variable=self.auto_open).grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(5, 0))
        self.output_entry.bind("<FocusOut>", lambda _e: self.find_last_dashboard())

        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(2, 8))
        actions.columnconfigure(4, weight=1)
        self.run_btn = ttk.Button(actions, text="Run analysis", command=self.start, style="Primary.TButton")
        self.run_btn.grid(row=0, column=0, sticky="w")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(8, 0))
        self.open_btn = ttk.Button(actions, text="Open last dashboard", command=self.open_last, state="disabled")
        self.open_btn.grid(row=0, column=2, padx=(8, 0))
        self.folder_btn = ttk.Button(actions, text="Open results folder", command=self.open_results_folder)
        self.folder_btn.grid(row=0, column=3, padx=(8, 0))
        self.status = ttk.Label(actions, text="Ready", foreground="#555")
        self.status.grid(row=0, column=4, sticky="e")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        log_head = ttk.Frame(self)
        log_head.grid(row=4, column=0, sticky="ew")
        log_head.columnconfigure(0, weight=1)
        ttk.Label(log_head, text="Analysis log", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.copy_diag_btn = ttk.Button(log_head, text="Copy diagnostics", command=self.copy_diagnostics, state="disabled")
        self.copy_diag_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Button(log_head, text="Clear", command=self.clear_log).grid(row=0, column=2, sticky="e")

        log_frame = ttk.Frame(self)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(5, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, wrap="word", font=("Consolas", 9), state="disabled", relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.hops.trace_add("write", lambda *_: self.scope_changed())
        self.forward_hops.trace_add("write", lambda *_: self.scope_changed())
        self.per_node_cap.trace_add("write", lambda *_: self.mark_custom())
        self.forward_cap.trace_add("write", lambda *_: self.mark_custom())
        self.max_nodes.trace_add("write", lambda *_: self.mark_custom())
        self.update_scope_summary()
        self.update_ai_state()
        self.find_last_dashboard()
        self.after(100, self.poll_messages)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def copy_diagnostics(self):
        text = self.log.get("1.0", "end").strip()
        if self.log_path and self.log_path.exists():
            try:
                text = self.log_path.read_text(encoding="utf-8", errors="replace").strip() or text
            except Exception:
                pass
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status.configure(text="Diagnostics copied")

    def _failure_summary(self) -> str:
        lines = []
        if self.log_path and self.log_path.exists():
            try:
                lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                lines = []
        if not lines:
            lines = self.log.get("1.0", "end").splitlines()
        meaningful = [x.strip() for x in lines if x.strip()]
        # Prefer the engine's final ERROR line, then specific FAIL lines.
        for prefix in ("ERROR:", "[Identifier resolution FAIL]", "[Crossref FAIL]", "[OpenAlex"):
            hits = [x for x in meaningful if x.startswith(prefix)]
            if hits:
                return hits[-1][:900]
        return (meaningful[-1][:900] if meaningful else "The analysis engine stopped without a detailed error message.")

    def toggle_advanced(self):
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.caps.grid()
            self.advanced_btn.configure(text="Hide advanced limits ▾")
        else:
            self.caps.grid_remove()
            self.advanced_btn.configure(text="Show advanced limits ▸")

    def _scope_values(self):
        try:
            return (
                int(self.hops.get()), int(self.forward_hops.get()),
                int(self.per_node_cap.get()), int(self.forward_cap.get()), int(self.max_nodes.get())
            )
        except (ValueError, tk.TclError):
            return None

    def update_scope_summary(self):
        values = self._scope_values()
        if not values:
            self.scope_summary.configure(text="Check the numeric scope values.")
            return
        up, down, _refcap, _fcap, maxpapers = values
        if up == 0 and down == 0:
            text = "No literature layers selected — choose at least one upstream or downstream hop."
        else:
            parts = []
            if up:
                parts.append(f"{up} reference layer{'s' if up != 1 else ''}")
            if down:
                parts.append(f"{down} citing generation{'s' if down != 1 else ''}")
            text = "Scope: " + " + ".join(parts) + f" · safety cap {maxpapers:,} papers."
        self.scope_summary.configure(text=text)

    def apply_preset(self, _event=None):
        name = self.preset.get()
        if name not in self.PRESETS:
            return
        up, down, refcap, fcap, maxpapers = self.PRESETS[name]
        self.hops.set(up)
        self.forward_hops.set(down)
        self.per_node_cap.set(refcap)
        self.forward_cap.set(fcap)
        self.max_nodes.set(maxpapers)
        self.preset.set(name)
        self.update_ai_state()

    def mark_custom(self):
        if not hasattr(self, "preset"):
            return
        current = self.preset.get()
        actual = self._scope_values()
        if current in self.PRESETS and actual is not None and actual != self.PRESETS[current]:
            self.preset.set("Custom")

    def scope_changed(self):
        self.mark_custom()
        self.update_scope_summary()
        self.update_ai_state()

    def update_ai_state(self):
        try:
            upstream = int(self.hops.get())
        except Exception:
            upstream = 0
        if upstream <= 0:
            self.future_var.set(False)
            self.future_check.configure(state="disabled")
        else:
            self.future_check.configure(state="normal")
        ai_on = self.findings_var.get() or self.future_var.get()
        state = "normal" if ai_on else "disabled"
        self.model.configure(state=state)
        self.refresh_btn.configure(state=state)
        self.findings_max_spin.configure(state="normal" if self.findings_var.get() else "disabled")

    def browse_output(self):
        initial = self.output_root.get().strip() or str(default_results_root())
        selected = filedialog.askdirectory(title="Choose litev results folder", initialdir=initial if Path(initial).exists() else str(Path.home()))
        if selected:
            self.output_root.set(selected)
            self.find_last_dashboard()

    def find_last_dashboard(self):
        root = Path(self.output_root.get().strip() or default_results_root())
        try:
            dashboards = sorted(root.glob("litev_*/litev_dashboard.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            dashboards = []
        if dashboards:
            self.html_path = dashboards[0]
            self.open_btn.configure(state="normal")
        elif not (self.proc and self.proc.poll() is None):
            self.open_btn.configure(state="disabled")

    def refresh_models(self):
        self.status.configure(text="Checking Ollama…")
        def worker():
            try:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                r = subprocess.run(
                    ["ollama", "list"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", creationflags=creationflags, timeout=12,
                )
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout or "ollama list failed").strip())
                names = []
                for line in r.stdout.splitlines()[1:]:
                    parts = line.split()
                    if parts:
                        names.append(parts[0])
                self.messages.put(("models", names))
            except Exception as exc:
                self.messages.put(("models_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _make_run_dir(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = root / f"litev_{stamp}"
        i = 2
        while run_dir.exists():
            run_dir = root / f"litev_{stamp}_{i}"
            i += 1
        run_dir.mkdir(parents=True)
        return run_dir

    def start(self):
        identifier = self.identifier.get().strip()
        if not identifier:
            messagebox.showwarning(APP_TITLE, "Please enter a literature identifier.")
            return

        ai_on = self.findings_var.get() or self.future_var.get()
        model = self.model.get().strip()
        if ai_on and not model:
            messagebox.showwarning(APP_TITLE, "Choose an Ollama model, or turn off the local AI options.")
            return

        values = self._scope_values()
        if not values:
            messagebox.showwarning(APP_TITLE, "Please check the numeric scope settings.")
            return
        upstream, downstream, refcap, fcap, maxpapers = values
        try:
            findings_max = int(self.findings_max.get())
        except (ValueError, tk.TclError):
            findings_max = 0
        if upstream < 0 or downstream < 0 or refcap < 1 or fcap < 1 or maxpapers < 10:
            messagebox.showwarning(APP_TITLE, "Scope values are outside their allowed range.")
            return
        if self.findings_var.get() and findings_max < 1:
            messagebox.showwarning(APP_TITLE, "Paper summary count must be at least 1 when findings summaries are enabled.")
            return
        if upstream == 0 and downstream == 0:
            messagebox.showwarning(APP_TITLE, "Choose at least one upstream or downstream hop for an evolution analysis.")
            return

        prefix = engine_command()
        if not prefix:
            messagebox.showerror(APP_TITLE, "Could not find the bundled analysis engine.\nRebuild the Windows package with build_windows.bat.")
            return

        root = Path(self.output_root.get().strip() or default_results_root()).expanduser()
        try:
            run_dir = self._make_run_dir(root)
            cache_dir = root / ".cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not create the results folder:\n{exc}")
            return

        report = run_dir / "litev_report.json"
        html = run_dir / "litev_dashboard.html"
        exports = run_dir / "litev_exports"
        cmd = prefix + [
            identifier,
            "--max-hops", str(upstream),
            "--forward-hops", str(downstream),
            "--per-node-cap", str(refcap),
            "--forward-cap", str(fcap),
            "--max-nodes", str(maxpapers),
            "--report", str(report),
            "--html", str(html),
            "--export-dir", str(exports),
            "--paper-findings-cache", str(cache_dir / "paper_findings.json"),
            "--paper-findings-max", str(findings_max),
            "--future-ai-cache", str(cache_dir / "future_ai.json"),
        ]
        if ai_on:
            cmd += ["--ai-model", model]
        if self.findings_var.get():
            cmd.append("--paper-findings-ai")
        if self.future_var.get() and upstream > 0:
            cmd.append("--future-ai")

        self.current_run_dir = run_dir
        self.html_path = html
        self.log_path = run_dir / "litev_run.log"
        self.copy_diag_btn.configure(state="normal")
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.progress.start(10)
        self.status.configure(text="Analyzing…")
        self.write_log(f"\n=== Starting {APP_TITLE} ===\n")
        self.write_log(f"Identifier: {identifier}\n")
        self.write_log(f"Scope: upstream {upstream} · downstream {downstream} · max {maxpapers} papers\n")
        if ai_on:
            self.write_log(f"Local AI model: {model}\n")
        else:
            self.write_log("Local AI: off\n")
        self.write_log(f"Results: {run_dir}\n\n")

        def worker():
            try:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                # The GUI decodes engine output as UTF-8. Force the child to
                # emit UTF-8 even on Windows systems whose active code page is
                # cp1252/cp850; otherwise bibliographic Unicode can crash the
                # engine with UnicodeEncodeError before analysis completes.
                env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
                env["PYTHONUTF8"] = "1"
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(run_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                    env=env,
                    bufsize=1,
                )
                with self.log_path.open("a", encoding="utf-8", errors="replace") as logfile:
                    logfile.write(f"{APP_TITLE}\nIdentifier: {identifier}\nResults: {run_dir}\n\n")
                    logfile.flush()
                    for line in self.proc.stdout:
                        logfile.write(line)
                        logfile.flush()
                        self.messages.put(("log", line))
                code = self.proc.wait()
                self.messages.put(("done", code))
            except Exception as exc:
                self.messages.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.status.configure(text="Stopping…")
            self.write_log("\nStopping analysis…\n")

    def poll_messages(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self.write_log(value)
                elif kind == "models":
                    if value:
                        self.model.configure(values=value)
                        if self.model.get() not in value:
                            self.model.set(value[0])
                        self.write_log(f"Found {len(value)} local Ollama model(s).\n")
                        self.status.configure(text="Ollama ready")
                    else:
                        self.write_log("Ollama is available but no local models were listed.\n")
                        self.status.configure(text="No Ollama models")
                elif kind == "models_error":
                    self.write_log(f"Could not refresh Ollama models: {value}\n")
                    self.status.configure(text="Ollama unavailable")
                elif kind == "done":
                    self.finish(value)
                elif kind == "error":
                    self.finish(None, value)
        except queue.Empty:
            pass
        self.after(100, self.poll_messages)

    def finish(self, code, error=None):
        self.progress.stop()
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.proc = None
        if error:
            self.status.configure(text="Failed")
            self.write_log(f"\nERROR: {error}\n")
            messagebox.showerror(APP_TITLE, error)
            return
        if code == 0 and self.html_path and self.html_path.exists():
            self.open_btn.configure(state="normal")
            self.status.configure(text="Complete")
            if self.auto_open.get():
                self.write_log("\nAnalysis complete. Opening dashboard in your browser…\n")
                webbrowser.open(self.html_path.resolve().as_uri())
            else:
                self.write_log("\nAnalysis complete. Use 'Open last dashboard' when ready.\n")
        else:
            self.status.configure(text="Failed")
            self.write_log(f"\nAnalysis ended with exit code {code}.\n")
            detail = self._failure_summary()
            location = f"\n\nFull diagnostic log:\n{self.log_path}" if self.log_path else ""
            messagebox.showerror(
                APP_TITLE,
                f"Analysis failed (exit code {code}).\n\n{detail}{location}\n\nUse 'Copy diagnostics' if you want to share the full log."
            )

    def open_last(self):
        if self.html_path and self.html_path.exists():
            webbrowser.open(self.html_path.resolve().as_uri())
        else:
            self.find_last_dashboard()
            if self.html_path and self.html_path.exists():
                webbrowser.open(self.html_path.resolve().as_uri())
            else:
                messagebox.showinfo(APP_TITLE, "No dashboard has been generated yet.")

    def open_results_folder(self):
        path = self.current_run_dir or Path(self.output_root.get().strip() or default_results_root())
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))
            else:
                webbrowser.open(path.resolve().as_uri())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open results folder:\n{exc}")

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(APP_TITLE, "An analysis is still running. Stop it and close the program?"):
                return
            self.stop()
        self.destroy()


if __name__ == "__main__":
    ExplorerApp().mainloop()
