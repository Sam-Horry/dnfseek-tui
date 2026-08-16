"""dnfseek — a Textual TUI wrapper for dnf on Fedora.

The whole app lives in this module: a single ``DnfseekApp`` (plus a thin
``PackageList`` subclass). The companion stylesheet ``main.tcss`` is loaded
via ``App.CSS_PATH`` and styles widgets by their ids (``#options``,
``#input``, ``#left_panel``, ``#right_panel``, ``#spinner``); those ids are
the cross-references noted throughout the comments below.

Run with ``uv run main.py`` (or the installed ``dnfseek`` console script).
A ``sudo -v`` gate runs before the TUI starts (see ``main()``); package
actions later call ``sudo -n``, with no in-app reauthentication.
"""

import asyncio
import heapq
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Iterable

from rapidfuzz import process
from rapidfuzz.distance import Indel

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.screen import Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import (
    Input,
    Static,
    Header,
    Footer,
    OptionList,
    LoadingIndicator,
)
# ``Option`` is NOT exported from ``textual.widgets`` in textual 8.2.8, so it
# must be imported from the private ``_option_list`` module. Used to build the
# entries of the virtualized left-panel list (see ``_populate_options``).
from textual.widgets._option_list import Option

# On-disk cache of package names, one ``name.arch`` per line. Refreshed from
# ``dnf`` when missing or older than ``CACHE_MAX_AGE`` (see ``update_cache``);
# a fresh cache is read instantly with no subprocess spawn (``_load_cache``).
CACHE_DIR = Path.home() / ".cache" / "dnfseek"
INSTALLED_CACHE = CACHE_DIR / "installed"
AVAILABLE_CACHE = CACHE_DIR / "available"
UPGRADABLE_CACHE = CACHE_DIR / "upgradable"
CACHE_MAX_AGE = 24 * 60 * 60  # 24h, the same freshness threshold as the bash original.

# Persisted selection from the command-palette Theme command; ignored if
# ``TEXTUAL_THEME`` is set in the environment (see ``_load_saved_theme``).
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dnfseek"
THEME_FILE = CONFIG_DIR / "theme"

# Status-bar text shown when no action is running (rendered in ``#options_text``).
DEFAULT_HINT = "Search for a package, and press TAB to switch focus"

# Built-in command-palette entries hidden from the palette (see
# ``get_system_commands``); Theme/Quit/Keys still appear.
HIDDEN_SYSTEM_COMMANDS = {"Screenshot", "Maximize", "Minimize"}

# Fuzzy-search tuning: cap displayed matches so the per-keystroke OptionList
# rebuild stays cheap (~50ms), and debounce rapid typing before re-ranking.
MAX_RESULTS = 500
FILTER_DEBOUNCE = 0.06  # seconds of quiet before the list is re-filtered

# Lazy full-list loading: above ``LAZY_THRESHOLD`` options (only ever the
# unfiltered full view — filtered results are capped at ``MAX_RESULTS``),
# ``_populate_options`` shows the first ``LAZY_CHUNK`` immediately and
# streams the rest in a background worker so the UI never blocks on the
# ~1s cold rebuild of 78k options.
LAZY_THRESHOLD = 2000
LAZY_CHUNK = 2000
LAZY_PAUSE = 0.01  # seconds between chunks (let the screen paint)


class PackageList(OptionList):
    """Left-panel package list.

    Subclassed to bind ``space`` to select (triggering the info preview) and
    to make ``get_content_height`` O(1): every option is a single line (a
    package name, never wrapped, no divider rows), so the content height is
    just the option count. The base implementation sums per-option heights on
    every layout pass — quadratic-ish at ~78k options, which showed up in
    profiling as a multi-second stall per rebuild. (Option selection is what
    drives the info preview in ``on_option_list_option_selected``.)
    """

    BINDINGS = [
        Binding("space", "select", "Select", show=False),
    ]

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return self.option_count


class DnfseekApp(App):
    """The whole dnfseek TUI: search/browse packages and run dnf actions.

    Layout (ids match ``main.tcss`` rules):
      * ``#options`` — status bar with a hidden ``#spinner`` (``.hidden`` in
        ``main.tcss`` sets ``display: none``) and ``#options_text``.
      * ``#input`` — client-side filter over the in-memory list.
      * ``#left_panel`` — this ``PackageList`` (virtualized OptionList).
      * ``#right_panel`` — info/deps/output preview ``Static``.
    """

    CSS_PATH = "main.tcss"
    TITLE = "dnfseek"
    SUB_TITLE = "a TUI dnf wrapper"

    # Key → action_* method (textual convention). Listed in the Footer and in
    # the command palette (see ``get_system_commands``).
    BINDINGS = [
        ("u", "upgrade_all", "Upgrade all packages"),
        ("r", "refresh_cache", "Refresh cache"),
        ("i", "install", "Install package"),
        ("x", "remove", "Remove package"),
        ("e", "reinstall", "Reinstall package"),
        ("g", "update_package", "Update package"),
        ("d", "deps", "Show dependencies"),
    ]

    def __init__(self) -> None:
        super().__init__()
        if (saved_theme := self._load_saved_theme()) is not None:
            self.theme = saved_theme
        # Package-name sets (one ``name.arch`` per entry). Populated from the
        # on-disk cache (``_load_cache``) and refreshed via ``update_cache``.
        self._installed: set[str] = set()
        self._available: set[str] = set()
        self._upgradable: set[str] = set()
        # Per-package in-memory caches of lazy-fetched dnf output.
        self._info_cache: dict[str, str] = {}
        self._deps_cache: dict[str, str] = {}
        # Names with a fetch worker already in flight; prevents duplicate
        # ``dnf info`` / deps spawns when the user re-selects quickly.
        self._pending_fetches: set[str] = set()
        # Currently-selected package (whose info is shown in ``#right_panel``).
        self._active_package: str | None = None
        # Names backing the current view (installed-only OR installed|available),
        # before the live ``#input`` filter is applied (see ``_populate_options``).
        self._view_names: list[str] = []
        # Casefolded twin of ``_view_names``, built once per view change in
        # ``_rebuild_view_names`` — the per-keystroke fuzzy ranker never casefolds.
        self._view_cf: list[str] = []
        # The view mode last requested (see ``_rebuild_view_names``).
        self._installed_only = False
        self._filter = ""
        # Pending debounce timer for ``on_input_changed`` re-ranking.
        self._filter_timer: Timer | None = None
        # True while a dnf action's status owns ``#options_text`` — keeps
        # ``_populate_options``'s match-count hint from clobbering it.
        self._status_active = False
        # Bumped on every ``_populate_options``; a lazy full-list stream aborts
        # when its captured generation goes stale (see ``_populate_options_lazy``).
        self._populate_generation = 0

    def compose(self) -> ComposeResult:
        """Build the widget tree. Widget ids are targeted by ``main.tcss``."""
        yield Header()
        with Horizontal(id="options"):  # styled by the ``#options`` rule (height: 3)
            yield LoadingIndicator(id="spinner", classes="hidden")  # toggled by ``_show_status``/``_hide_status``
            yield Static(DEFAULT_HINT, id="options_text")

        with Horizontal():
            with Vertical():
                yield Input(placeholder="Type Package Name", id="input")
                yield PackageList(id="left_panel", markup=False)
            yield Static("Select a package (enter/space) to view its information", id="right_panel", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        """Boot straight into "Search all" so the list is never blank."""
        self.theme_changed_signal.subscribe(self, self._save_theme)
        self.styles.scrollbar_visibility = "hidden"
        # Runs ``_show_packages(installed_only=False)`` in a worker; the
        # ``exclusive=True`` group cancels any prior search worker mid-flight.
        self.run_worker(
            self._show_packages(installed_only=False),
            name="search-all",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    def _load_saved_theme(self) -> str | None:
        """Return the persisted theme name, or None to fall back to defaults.

        The ``TEXTUAL_THEME`` env var takes precedence (we then stay hands-off);
        missing/empty file or unknown theme name also yield None.
        """
        if "TEXTUAL_THEME" in os.environ:
            return None
        try:
            saved = THEME_FILE.read_text().strip()
        except OSError:
            return None
        if not saved or self.get_theme(saved) is None:
            return None
        return saved

    def _save_theme(self, theme: Theme) -> None:
        """Persist theme name on change (best-effort: silent on OSError)."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            THEME_FILE.write_text(theme.name)
        except OSError:
            pass

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Populate the command palette (ctrl+p).

        Filters the built-in commands via ``HIDDEN_SYSTEM_COMMANDS`` (note the
        display-name field is ``command.title``, not ``.name`` — SystemCommand
        is a NamedTuple), then appends the dnfseek actions.
        """
        # keep built-in commands, minus the ones we don't need
        for command in super().get_system_commands(screen):
            if command.title not in HIDDEN_SYSTEM_COMMANDS:
                yield command
        # custom commands
        yield SystemCommand(
            "Upgrade all", "Upgrade all packages with dnf", self.upgrade_all
        )
        yield SystemCommand(
            "Refresh cache", "Refresh the dnf package cache", self.refresh_cache
        )
        yield SystemCommand(
            "Search all", "Search all available packages", self.search_all
        )
        yield SystemCommand(
            "Search installed", "Search installed packages", self.search_installed
        )
        yield SystemCommand(
            "Install package", "Install the highlighted package with dnf",
            self.action_install,
        )
        yield SystemCommand(
            "Remove package", "Remove the highlighted package with dnf",
            self.action_remove,
        )
        yield SystemCommand(
            "Reinstall package", "Reinstall the highlighted package with dnf",
            self.action_reinstall,
        )
        yield SystemCommand(
            "Update package", "Update the highlighted package with dnf",
            self.action_update_package,
        )
        yield SystemCommand(
            "Show dependencies", "Show dependencies of the highlighted package",
            self.action_deps,
        )

    async def _dnf_list(self, args: list[str]) -> str | None:
        """Run an unprivileged ``dnf -q`` and return its stdout, or None on failure.

        Used to populate the cache (installed/available/upgradable). stdout/stderr
        are always PIPed — children must never write to the TUI terminal.
        """
        process = await asyncio.create_subprocess_exec(
            "dnf", "-q", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return stdout.decode()

    @staticmethod
    def _parse_dnf_list(output: str) -> list[str]:
        """Extract package names from ``dnf list`` output.

        Each data row is ``<name>.<arch>  <version>  <repo>``. The
        ``re.search(r"[0-9.-]", parts[1])`` check discriminates real data rows
        (whose 2nd field is a version with digits/dots/dashes) from the two
        header rows ``Installed Packages`` / ``Available Packages``.
        """
        names = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 3 and re.search(r"[0-9.-]", parts[1]):
                names.append(parts[0])
        return names

    @staticmethod
    def _parse_repoquery_set(output: str) -> list[str]:
        """Extract ``name.arch`` tokens from ``dnf repoquery`` output.

        Keeps only single tokens containing a ``.`` (arch separator) — this
        discards empty lines and any unexpected multi-word rows.
        """
        names = []
        for line in output.splitlines():
            token = line.strip()
            if token and "." in token and " " not in token:
                names.append(token)
        return names

    async def _write_cache(self, path: Path, names: list[str]) -> None:
        """Atomically write the cache file: mkstemp → os.replace.

        Writing to a temp file in ``CACHE_DIR`` and renaming avoids a
        half-written cache being read by a concurrent ``_load_cache``.
        """
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(names))
                f.write("\n")
            os.replace(tmp, path)
        except OSError:
            os.unlink(tmp)

    def _load_cache(self) -> None:
        """Read any existing cache files into the in-memory sets (instant)."""
        if INSTALLED_CACHE.exists():
            self._installed = set(INSTALLED_CACHE.read_text().split())
        if AVAILABLE_CACHE.exists():
            self._available = set(AVAILABLE_CACHE.read_text().split())
        if UPGRADABLE_CACHE.exists():
            self._upgradable = set(UPGRADABLE_CACHE.read_text().split())

    def _cache_is_stale(self) -> bool:
        """True if any cache file is missing or older than ``CACHE_MAX_AGE``."""
        for path in (INSTALLED_CACHE, AVAILABLE_CACHE, UPGRADABLE_CACHE):
            if not path.exists():
                return True
            if time.time() - path.stat().st_mtime > CACHE_MAX_AGE:
                return True
        return False

    async def update_cache(self) -> None:
        """Refresh all three cache files from dnf and repopulate the list.

        Runs the three fetches concurrently with ``asyncio.gather``. The
        available-list fetch carries ``--setopt=gpgcheck=1 --setopt=repo_gpgcheck=1``
        (mirrors a plain ``dnf install``). Upgradable names come from
        ``dnf repoquery --upgrades`` (parsed by ``_parse_repoquery_set``) rather
        than a separate ``dnf list``, giving a clean ``name.arch`` set directly.
        """
        self.notify("Refreshing package lists...", timeout=2)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        installed, available, upgradable = await asyncio.gather(
            self._dnf_list(["list", "--installed"]),
            self._dnf_list(
                ["-y", "--setopt=gpgcheck=1", "--setopt=repo_gpgcheck=1",
                 "list", "--available"]
            ),
            self._dnf_list(
                ["repoquery", "--upgrades", "--queryformat", "%{name}.%{arch}\n"]
            ),
        )
        if installed is None or available is None:
            self.notify("Failed to refresh package lists", severity="error")
            return
        installed_names = self._parse_dnf_list(installed)
        available_names = self._parse_dnf_list(available)
        upgradable_names = self._parse_repoquery_set(upgradable) if upgradable else []
        await asyncio.gather(
            self._write_cache(INSTALLED_CACHE, installed_names),
            self._write_cache(AVAILABLE_CACHE, available_names),
            self._write_cache(UPGRADABLE_CACHE, upgradable_names),
        )
        self._installed = set(installed_names)
        self._available = set(available_names)
        self._upgradable = set(upgradable_names)
        self._rebuild_view_names(self._installed_only)
        self._populate_options()
        self.notify("Package lists refreshed", severity="information")

    async def _ensure_cache(self) -> None:
        """Load the disk cache if needed, then refresh if it's stale."""
        if not self._installed and not self._available:
            self._load_cache()
        if self._cache_is_stale():
            await self.update_cache()

    async def refresh_cache(self) -> None:
        """Command-palette entry point for "Refresh cache"."""
        self.run_worker(
            self.update_cache(),
            name="refresh-cache",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    def action_refresh_cache(self) -> None:
        """``r`` key handler — same worker as the palette entry."""
        self.run_worker(
            self.update_cache(),
            name="refresh-cache",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    def _rebuild_view_names(self, installed_only: bool) -> None:
        """Set ``_view_names`` (and its casefolded twin ``_view_cf``) from the
        current package sets. Called on view switches and after install/remove
        mutations so the ranked list always reflects ``_installed``/``_available``.
        """
        self._installed_only = installed_only
        names = (
            sorted(self._installed)
            if installed_only
            else sorted(self._installed | self._available)
        )
        self._view_names = names
        self._view_cf = [name.casefold() for name in names]

    def _match_names(self, query: str) -> tuple[list[str], int]:
        """Rank ``_view_names`` against ``query``, best first, capped at ``MAX_RESULTS``.

        Case-insensitive, over the precomputed ``_view_cf`` (no per-keystroke
        casefolding). Matches are tiered fzf-style: prefix matches first, then
        word-boundary matches (previous char not alphanumeric), then plain
        substrings; within a tier, shorter names win (alphabetical tiebreak),
        with a bonus when the match ends on a boundary — so "zlib" ranks
        ``zlib-ng`` above ``zlibrary``. When the tiers exhaust the budget,
        the remainder is filled with fuzzy subsequence matches (rapidfuzz
        Indel/LCS scorer, then verified to actually contain the query in
        order), so out-of-order queries like "frefx" still surface
        ``firefox``.

        Returns ``(names, match_count)`` — the substring-match count drives
        the "top N of M" status hint and can exceed ``len(names)`` when capped.
        """
        if not query:
            return self._view_names, len(self._view_names)
        needle = query.casefold()
        names, candidates = self._view_names, self._view_cf
        prefix, boundary, substring = [], [], []
        for i, candidate in enumerate(candidates):
            pos = candidate.find(needle)
            if pos < 0:
                continue
            if pos == 0:
                prefix.append(i)
            elif not candidate[pos - 1].isalnum():
                boundary.append(i)
            else:
                substring.append(i)
        match_count = len(prefix) + len(boundary) + len(substring)

        def prefix_key(i: int) -> tuple[int, int, str]:
            rest = candidates[i][len(needle):]
            ends_on_boundary = not rest or not rest[0].isalnum()
            return (0 if ends_on_boundary else 1, len(candidates[i]), candidates[i])

        def tier_key(i: int) -> tuple[int, str]:
            return (len(candidates[i]), candidates[i])

        matched: list[int] = []
        budget = MAX_RESULTS
        for tier, key in ((prefix, prefix_key), (boundary, tier_key), (substring, tier_key)):
            if not tier or not budget:
                continue
            take = heapq.nsmallest(budget, tier, key=key)
            matched.extend(take)
            budget -= len(take)
        if budget:
            already = set(matched)
            results = process.extract(
                needle,
                candidates,
                scorer=Indel.normalized_similarity,
                score_cutoff=0.3,
                limit=max(budget * 10, 5000),
            )
            for _, _, i in results:
                if i in already:
                    continue
                candidate = candidates[i]
                pos = -1
                for char in needle:
                    pos = candidate.find(char, pos + 1)
                    if pos == -1:
                        break
                else:
                    matched.append(i)
                    if len(matched) == MAX_RESULTS:
                        break
        return [names[i] for i in matched], match_count

    def _populate_options(self) -> None:
        """Rebuild ``#left_panel`` from ``_view_names`` ranked against the filter.

        Ranking is purely client-side fuzzy matching (``_match_names``) over the
        in-memory list — no dnf calls — capped at ``MAX_RESULTS`` so the
        per-keystroke OptionList rebuild stays cheap. Glyph precedence is ⬆️
        (upgradable) over ✅ (installed) — a package can be both installed and
        have an upgrade available. When filtered, ``#options_text`` shows a
        match-count hint unless a dnf action's status owns it
        (``_status_active``).

        Oversized result sets (the unfiltered full view) are lazy: the first
        ``LAZY_CHUNK`` renders synchronously, the rest streams in via
        ``_populate_options_lazy``. Every call bumps ``_populate_generation``,
        which aborts any stream still in flight.
        """
        names, match_count = self._match_names(self._filter)
        options = [
            Option(
                f"⬆️ {name}" if name in self._upgradable
                else (f"✅ {name}" if name in self._installed else name),
                id=name,
            )
            for name in names
        ]
        self._populate_generation += 1
        left = self.query_one("#left_panel", OptionList)
        if len(options) > LAZY_THRESHOLD:
            left.set_options(options[:LAZY_CHUNK])
            left.highlighted = 0
            self.run_worker(
                self._populate_options_lazy(options, self._populate_generation),
                name="populate",
                group="populate",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            left.set_options(options)
            if options:
                left.highlighted = 0
        if not self._status_active:
            hint = DEFAULT_HINT
            if self._filter:
                if not names:
                    hint = f'No matches for "{self._filter}"'
                elif match_count > len(names):
                    hint = (
                        f"Top {len(names)} of {match_count} matches — "
                        "keep typing to narrow"
                    )
                else:
                    hint = f"{len(names)} matches"
            self.query_one("#options_text", Static).update(hint)

    async def _populate_options_lazy(
        self, options: list[Option], generation: int
    ) -> None:
        """Stream the full view into ``#left_panel`` in background chunks.

        The first ``LAZY_CHUNK`` was already set by ``_populate_options``;
        this appends the rest ``LAZY_CHUNK`` at a time, pausing between chunks
        so the screen paints. A stale ``generation`` (any newer
        ``_populate_options`` — a keystroke filter, view switch, or cache
        refresh) aborts the stream; that newer populate has already replaced
        the list.
        """
        left = self.query_one("#left_panel", OptionList)
        for start in range(LAZY_CHUNK, len(options), LAZY_CHUNK):
            if generation != self._populate_generation:
                return
            await asyncio.sleep(LAZY_PAUSE)
            if generation != self._populate_generation:
                return
            left.add_options(options[start:start + LAZY_CHUNK])

    async def search_all(self) -> None:
        """Command-palette "Search all" — installed | available."""
        self.run_worker(
            self._show_packages(installed_only=False),
            name="search-all",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    async def search_installed(self) -> None:
        """Command-palette "Search installed" — installed only."""
        self.run_worker(
            self._show_packages(installed_only=True),
            name="show-installed",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    async def _show_packages(self, installed_only: bool) -> None:
        """Ensure cache freshness, set the view's names, reset the filter, render."""
        await self._ensure_cache()
        self._rebuild_view_names(installed_only)
        self._filter = ""
        self.query_one("#input", Input).value = ""
        self._populate_options()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Debounced re-rank for ``#input``.

        The ``event.value == self._filter`` guard skips redundant work (textual
        re-emits Changed on focus even when the value didn't change). The rank
        + OptionList rebuild takes ~100ms, so rapid typing is debounced via
        ``FILTER_DEBOUNCE`` rather than re-ranking on every keystroke.
        """
        if event.input.id != "input":
            return
        if event.value == self._filter:
            return
        self._filter = event.value
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(FILTER_DEBOUNCE, self._populate_options)

    def _selected_package(self) -> str | None:
        """Return the highlighted package's id (its ``name.arch``), or None."""
        option = self.query_one("#left_panel", OptionList).highlighted_option
        return option.id if option is not None else None

    def _format_info(self, name: str, content: str) -> str:
        """Append the contextual Actions footer to a block of info/deps text.

        The footer varies by installed state and gains a ⬆️ line when an
        upgrade is available. (Footer will be replaced by tab labels per PLAN.md.)
        """
        if name in self._installed:
            actions = "x Remove | e Reinstall | g Update | d Dependencies"
        else:
            actions = "i Install | d Dependencies"
        suffix = "\n⬆️ Update available" if name in self._upgradable else ""
        return f"{content}\n\n── Actions ──\n{actions}{suffix}"

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Show info for the selected package in ``#right_panel`` (lazy + cached).

        Cache hit → instant render. In-flight fetch → no-op until the worker
        resolves and updates the panel. Otherwise kick a worker via
        ``_fetch_package_info`` and mark the name as pending.
        """
        name = event.option_id
        if name is None:
            return
        self._active_package = name
        right_panel = self.query_one("#right_panel", Static)
        if name in self._info_cache:
            right_panel.update(self._format_info(name, self._info_cache[name]))
            return
        if name in self._pending_fetches:
            return
        self._pending_fetches.add(name)
        right_panel.update(f"Fetching info for {name}...")
        self.run_worker(
            self._fetch_package_info(name),
            name=f"info-{name}",
            group="info",
            exit_on_error=False,
        )

    async def _fetch_package_info(self, name: str) -> None:
        """Lazy ``dnf info`` → ``_info_cache``, rendered only if still active.

        ``dnf info`` is unprivileged (no sudo). We filter to 7 keys
        (Name/Summary/Version/Release/Size/URL/Description) rather than render
        the full output — see PLAN.md bundle 3 for the proposed full-output tab.
        The ``name == self._active_package`` guard avoids clobbering the panel
        if the user selected a different package while this fetch ran.
        """
        process = await asyncio.create_subprocess_exec(
            "dnf", "info", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        self._pending_fetches.discard(name)
        if process.returncode != 0:
            return
        lines = [
            line.strip()
            for line in stdout.decode().splitlines()
            if re.match(r"^(Name|Summary|Version|Release|Size|URL|Description)", line.strip())
        ]
        info = "\n".join(lines)
        self._info_cache[name] = info
        if name == self._active_package:
            self.query_one("#right_panel", Static).update(
                self._format_info(name, info)
            )

    def action_install(self) -> None:
        """``i`` — install the highlighted package via ``dnf install``.

        The four package actions share a shape: guard (no selection / wrong
        state) → notify → spawn ``_run_dnf`` in a ``group="dnf"`` exclusive
        worker. ``installed="add"|"remove"|None`` tells ``_run_dnf`` which
        ``_mark_*`` helper to run on success so the in-memory + disk caches
        stay consistent.
        """
        name = self._selected_package()
        if name is None:
            self.notify("No package selected", severity="warning")
            return
        if name in self._installed:
            self.notify(f"{name} is already installed", severity="warning")
            return
        self.notify(f"Installing {name}...", timeout=2)
        self.run_worker(
            self._run_dnf(
                ["dnf", "install", "-y", name],
                success_msg=f"Installed: {name}",
                name=name,
                installed="add",
            ),
            name=f"install-{name}",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

    def action_remove(self) -> None:
        """``x`` — remove an installed package via ``dnf remove``."""
        name = self._selected_package()
        if name is None:
            self.notify("No package selected", severity="warning")
            return
        if name not in self._installed:
            self.notify(f"{name} is not installed", severity="warning")
            return
        self.notify(f"Removing {name}...", timeout=2)
        self.run_worker(
            self._run_dnf(
                ["dnf", "remove", "-y", name],
                success_msg=f"Removed: {name}",
                name=name,
                installed="remove",
            ),
            name=f"remove-{name}",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

    def action_reinstall(self) -> None:
        """``e`` — reinstall an installed package (no cache-set change → installed=None)."""
        name = self._selected_package()
        if name is None:
            self.notify("No package selected", severity="warning")
            return
        if name not in self._installed:
            self.notify(f"{name} is not installed", severity="warning")
            return
        self.notify(f"Reinstalling {name}...", timeout=2)
        self.run_worker(
            self._run_dnf(
                ["dnf", "reinstall", "-y", name],
                success_msg=f"Reinstalled: {name}",
                name=name,
            ),
            name=f"reinstall-{name}",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

    def action_update_package(self) -> None:
        """``g`` — upgrade one installed package via ``dnf upgrade``.

        Guards on ``self._upgradable`` (loaded by ``update_cache``) to refuse
        packages with no pending upgrade. ``_upgradable`` may be empty if the
        cache wasn't refreshed yet, in which case the guard is skipped.
        """
        name = self._selected_package()
        if name is None:
            self.notify("No package selected", severity="warning")
            return
        if name not in self._installed:
            self.notify(f"{name} is not installed", severity="warning")
            return
        if self._upgradable and name not in self._upgradable:
            self.notify(f"{name} is already up to date", severity="warning")
            return
        self.notify(f"Updating {name}...", timeout=2)
        self.run_worker(
            self._run_dnf(
                ["dnf", "upgrade", "-y", name],
                success_msg=f"Updated: {name}",
                name=name,
            ),
            name=f"update-{name}",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

    def action_deps(self) -> None:
        """``d`` — show ``dnf repoquery --requires`` for the highlighted package.

        Cache-first into ``_deps_cache``; otherwise spawn ``_fetch_deps`` in
        the ``"info"`` worker group. Output is rendered through
        ``_format_info`` to reuse the Actions footer.
        """
        name = self._selected_package()
        if name is None:
            self.notify("No package selected", severity="warning")
            return
        right_panel = self.query_one("#right_panel", Static)
        if name in self._deps_cache:
            right_panel.update(self._format_info(name, self._deps_cache[name]))
            return
        right_panel.update(f"Fetching dependencies for {name}...")
        self.run_worker(
            self._fetch_deps(name),
            name=f"deps-{name}",
            group="info",
            exit_on_error=False,
        )

    async def _fetch_deps(self, name: str) -> None:
        """Fetch and cache a package's requirements (unprivileged repoquery)."""
        process = await asyncio.create_subprocess_exec(
            "dnf", "repoquery", "--requires", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        right_panel = self.query_one("#right_panel", Static)
        if process.returncode != 0:
            right_panel.update(f"Could not fetch dependencies for {name}")
            self.notify(f"Could not fetch dependencies for {name}", severity="error")
            return
        deps = [line.strip() for line in stdout.decode().splitlines() if line.strip()]
        text = "\n".join(deps) if deps else "No dependencies"
        self._deps_cache[name] = text
        right_panel.update(self._format_info(name, text))

    def _restore_package_info(self, name: str) -> None:
        """Re-show info for a package after a dnf action mutated state.

        Cache hit → render immediately; otherwise kick a fresh ``dnf info``
        fetch (the cached entry was just invalidated by ``_mark_*``).
        """
        self._active_package = name
        if name in self._info_cache:
            self.query_one("#right_panel", Static).update(
                self._format_info(name, self._info_cache[name])
            )
        else:
            self.run_worker(
                self._fetch_package_info(name),
                name=f"info-{name}",
                group="info",
                exit_on_error=False,
            )

    def _mark_installed(self, name: str) -> None:
        """Move ``name`` from available→installed, drop stale caches, persist.

        Also drops the entry from ``_upgradable`` (a just-installed package is
        current by definition) and re-syncs all three disk caches in a worker.
        """
        self._installed.add(name)
        self._available.discard(name)
        self._upgradable.discard(name)
        self._info_cache.pop(name, None)
        self._deps_cache.pop(name, None)
        self._rebuild_view_names(self._installed_only)
        self._populate_options()
        self.run_worker(
            self._sync_cache_files(),
            name="cache-sync",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    def _mark_removed(self, name: str) -> None:
        """Move ``name`` from installed→available; mirror ``_mark_installed``."""
        self._installed.discard(name)
        self._available.add(name)
        self._upgradable.discard(name)
        self._info_cache.pop(name, None)
        self._deps_cache.pop(name, None)
        self._rebuild_view_names(self._installed_only)
        self._populate_options()
        self.run_worker(
            self._sync_cache_files(),
            name="cache-sync",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    async def _sync_cache_files(self) -> None:
        """Rewrite all three cache files from the current in-memory sets."""
        await asyncio.gather(
            self._write_cache(INSTALLED_CACHE, sorted(self._installed)),
            self._write_cache(AVAILABLE_CACHE, sorted(self._available)),
            self._write_cache(UPGRADABLE_CACHE, sorted(self._upgradable)),
        )

    def _start_upgrade(self) -> None:
        """Shared body of upgrade_all / action_upgrade_all — kicks ``dnf upgrade -y``."""
        self.notify("Upgrading all packages...", timeout=2)
        self.run_worker(
            self._run_dnf(
                ["dnf", "upgrade", "-y"],
                success_msg="All packages upgraded!",
            ),
            name="upgrade_all",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

    async def upgrade_all(self) -> None:
        """Command-palette "Upgrade all" (routes through ``_start_upgrade``)."""
        self._start_upgrade()

    def action_upgrade_all(self) -> None:
        """``u`` key handler (routes through ``_start_upgrade``)."""
        self._start_upgrade()

    async def _run_dnf(
        self,
        args: list[str],
        success_msg: str = "Command completed successfully",
        name: str | None = None,
        installed: str | None = None,
    ) -> None:
        """Drive a privileged dnf action end-to-end: status → sudo → result.

        Three branches after ``_sudo_once`` returns:
          * sudo expired (``sudo -n`` reports "a password is required" /
            "no password was provided") → notify + ``self.exit(result=...)``,
            which ``main()`` prints to stderr after the TUI restores the
            terminal. There is no in-app reauth path.
          * returncode 0 → notify success, apply the cache-set mutation
            (``installed="add"|"remove"``), refresh the panel.
          * non-zero → surface the last 10 stderr lines in ``#right_panel``
            and classify the common dnf messages (already installed /
            already latest / nothing-to-do) into friendlier notifications.
        """
        right_panel = self.query_one("#right_panel", Static)
        self._show_status(self._action_status(args, name))
        try:
            right_panel.update(f"Running: {' '.join(args)}")
            returncode, stderr = await self._sudo_once(args)
            if returncode != 0 and (
                b"a password is required" in stderr.lower()
                or b"no password was provided" in stderr.lower()
            ):
                self.notify(
                    "Sudo session expired — please restart dnfseek",
                    severity="error",
                )
                self.exit(
                    result="dnfseek: sudo session expired. "
                    "Re-run the app to reauthenticate."
                )
                return
            if returncode == 0:
                self.notify(success_msg, timeout=2, severity="information")
                if installed == "add" and name is not None:
                    self._mark_installed(name)
                    self._restore_package_info(name)
                elif installed == "remove" and name is not None:
                    self._mark_removed(name)
                    self._restore_package_info(name)
                elif name is not None:
                    self._restore_package_info(name)
            else:
                stderr_text = stderr.decode(errors="replace")
                error_text = stderr_text.lower()
                if stderr:
                    error_lines = stderr_text.splitlines()[-10:]
                    right_panel.update("\n".join(error_lines))
                if "already installed" in error_text:
                    self.notify(
                        f"{name or 'Package'} is already installed", severity="warning"
                    )
                elif "already the latest" in error_text or "nothing to do" in error_text:
                    self.notify(
                        f"{name or 'Packages'} are already up to date",
                        severity="warning",
                    )
                else:
                    self.notify(
                        f"Command failed (error code {returncode})",
                        severity="error",
                    )
        finally:
            self._hide_status()

    async def _sudo_once(self, args: list[str]) -> tuple[int, bytes]:
        """Run ``sudo -n <cmd> ...`` once, streaming stdout to ``#right_panel``.

        Non-interactive sudo (validated up front by ``main()``'s ``sudo -v``).
        stdout is streamed line-by-line into the panel showing only the last
        15 lines (a scrollback-preserving RichLog is planned per PLAN.md).
        stderr is read in parallel (``stderr_task``) so stderr buffering can't
        deadlock the streaming stdout. ``--requires``-style messages are
        matched case-insensitively against the raw **bytes** (``stderr.lower()``)
        rather than decoded text, which is why the literals are byte strings.
        Returns ``(returncode, stderr_bytes)``; ``returncode or 0`` coerces the
        ``None`` that ``create_subprocess_exec`` can leave behind before wait().
        """
        right_panel = self.query_one("#right_panel", Static)
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        lines: list[str] = []
        async for chunk in process.stdout:
            for line in chunk.decode(errors="replace").replace("\r", "\n").splitlines():
                if line.strip():
                    lines.append(line.strip())
            right_panel.update("\n".join(lines[-15:]))
        stderr = await stderr_task
        await process.wait()
        return process.returncode or 0, stderr

    @staticmethod
    def _action_status(args: list[str], name: str | None) -> str:
        """Map a dnf argv to the status-bar message shown during the run."""
        action = args[1]
        if action == "install":
            return f"Installing {name}..."
        if action == "remove":
            return f"Removing {name}..."
        if action == "reinstall":
            return f"Reinstalling {name}..."
        if action == "upgrade":
            if name is not None:
                return f"Upgrading {name}..."
            return "Upgrading all packages..."
        return f"Running: {' '.join(args)}"

    def _show_status(self, message: str) -> None:
        """Un-hide ``#spinner`` (the ``.hidden`` rule in main.tcss sets display:none) and set ``#options_text``.

        Sets ``_status_active`` so ``_populate_options``'s match-count hint
        doesn't clobber the action message.
        """
        self._status_active = True
        self.query_one("#spinner", LoadingIndicator).remove_class("hidden")
        self.query_one("#options_text", Static).update(message)

    def _hide_status(self) -> None:
        """Re-hide ``#spinner`` and restore ``#options_text`` to ``DEFAULT_HINT``."""
        self._status_active = False
        self.query_one("#spinner", LoadingIndicator).add_class("hidden")
        self.query_one("#options_text", Static).update(DEFAULT_HINT)


def main() -> None:
    """Entry point — authenticate, run the TUI, print any exit result.

    ``sudo -v`` runs on the *real terminal* before the TUI starts (the wrong
    password, or a fingerprint tap, is handled here — the app itself never
    holds a password). If ``app.run()`` returns a string — i.e. a
    ``self.exit(result=<message>)`` such as the sudo-expiry path — it is
    printed to stderr after the TUI restores the terminal.
    """
    import subprocess
    import sys

    if subprocess.run(["sudo", "-v"]).returncode != 0:
        raise SystemExit("Authentication failed - dnfseek requires sudo access")
    app = DnfseekApp()
    result = app.run()
    if result is not None:
        print(result, file=sys.stderr)


if __name__ == "__main__":
    main()
