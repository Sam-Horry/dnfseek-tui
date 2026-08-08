import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Iterable

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import (
    Input,
    Static,
    Header,
    Footer,
    OptionList,
    LoadingIndicator,
)
from textual.widgets._option_list import Option

CACHE_DIR = Path.home() / ".cache" / "dnfseek"
INSTALLED_CACHE = CACHE_DIR / "installed"
AVAILABLE_CACHE = CACHE_DIR / "available"
UPGRADABLE_CACHE = CACHE_DIR / "upgradable"
CACHE_MAX_AGE = 24 * 60 * 60
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dnfseek"
THEME_FILE = CONFIG_DIR / "theme"
DEFAULT_HINT = "Search for a package, and press TAB to switch focus"
HIDDEN_SYSTEM_COMMANDS = {"Screenshot", "Maximize", "Minimize"}


class PackageList(OptionList):
    BINDINGS = [
        Binding("space", "select", "Select", show=False),
    ]


class DnfseekApp(App):
    CSS_PATH = "main.tcss"
    TITLE = "dnfseek"
    SUB_TITLE = "a TUI dnf wrapper"

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
        self._installed: set[str] = set()
        self._available: set[str] = set()
        self._upgradable: set[str] = set()
        self._info_cache: dict[str, str] = {}
        self._deps_cache: dict[str, str] = {}
        self._pending_fetches: set[str] = set()
        self._active_package: str | None = None
        self._view_names: list[str] = []
        self._filter = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="options"):
            yield LoadingIndicator(id="spinner", classes="hidden")
            yield Static(DEFAULT_HINT, id="options_text")

        with Horizontal():
            with Vertical():
                yield Input(placeholder="Type Package Name", id="input")
                yield PackageList(id="left_panel")
            yield Static("Select a package (enter/space) to view its information", id="right_panel", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.theme_changed_signal.subscribe(self, self._save_theme)
        self.styles.scrollbar_visibility = "hidden"
        self.run_worker(
            self._show_packages(installed_only=False),
            name="search-all",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    def _load_saved_theme(self) -> str | None:
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
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            THEME_FILE.write_text(theme.name)
        except OSError:
            pass

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
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
        names = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 3 and re.search(r"[0-9.-]", parts[1]):
                names.append(parts[0])
        return names

    @staticmethod
    def _parse_repoquery_set(output: str) -> list[str]:
        names = []
        for line in output.splitlines():
            token = line.strip()
            if token and "." in token and " " not in token:
                names.append(token)
        return names

    async def _write_cache(self, path: Path, names: list[str]) -> None:
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(names))
                f.write("\n")
            os.replace(tmp, path)
        except OSError:
            os.unlink(tmp)

    def _load_cache(self) -> None:
        if INSTALLED_CACHE.exists():
            self._installed = set(INSTALLED_CACHE.read_text().split())
        if AVAILABLE_CACHE.exists():
            self._available = set(AVAILABLE_CACHE.read_text().split())
        if UPGRADABLE_CACHE.exists():
            self._upgradable = set(UPGRADABLE_CACHE.read_text().split())

    def _cache_is_stale(self) -> bool:
        for path in (INSTALLED_CACHE, AVAILABLE_CACHE, UPGRADABLE_CACHE):
            if not path.exists():
                return True
            if time.time() - path.stat().st_mtime > CACHE_MAX_AGE:
                return True
        return False

    async def update_cache(self) -> None:
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
        self._populate_options()
        self.notify("Package lists refreshed", severity="information")

    async def _ensure_cache(self) -> None:
        if not self._installed and not self._available:
            self._load_cache()
        if self._cache_is_stale():
            await self.update_cache()

    async def refresh_cache(self) -> None:
        self.run_worker(
            self.update_cache(),
            name="refresh-cache",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    def action_refresh_cache(self) -> None:
        self.run_worker(
            self.update_cache(),
            name="refresh-cache",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    def _populate_options(self) -> None:
        names = self._view_names
        if self._filter:
            needle = self._filter.casefold()
            names = [name for name in names if needle in name.casefold()]
        options = [
            Option(
                f"⬆️ {name}" if name in self._upgradable
                else (f"✅ {name}" if name in self._installed else name),
                id=name,
            )
            for name in names
        ]
        left = self.query_one("#left_panel", OptionList)
        left.clear_options()
        left.add_options(options)
        if options:
            left.highlighted = 0

    async def search_all(self) -> None:
        self.run_worker(
            self._show_packages(installed_only=False),
            name="search-all",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    async def search_installed(self) -> None:
        self.run_worker(
            self._show_packages(installed_only=True),
            name="show-installed",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    async def _show_packages(self, installed_only: bool) -> None:
        await self._ensure_cache()
        if installed_only:
            names = sorted(self._installed)
        else:
            names = sorted(self._installed | self._available)
        self._view_names = names
        self._filter = ""
        self.query_one("#input", Input).value = ""
        self._populate_options()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        if event.value == self._filter:
            return
        self._filter = event.value
        self._populate_options()

    def _selected_package(self) -> str | None:
        option = self.query_one("#left_panel", OptionList).highlighted_option
        return option.id if option is not None else None

    def _format_info(self, name: str, content: str) -> str:
        if name in self._installed:
            actions = "x Remove | e Reinstall | g Update | d Dependencies"
        else:
            actions = "i Install | d Dependencies"
        suffix = "\n⬆️ Update available" if name in self._upgradable else ""
        return f"{content}\n\n── Actions ──\n{actions}{suffix}"

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
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
        self._installed.add(name)
        self._available.discard(name)
        self._upgradable.discard(name)
        self._info_cache.pop(name, None)
        self._deps_cache.pop(name, None)
        self._populate_options()
        self.run_worker(
            self._sync_cache_files(),
            name="cache-sync",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    def _mark_removed(self, name: str) -> None:
        self._installed.discard(name)
        self._available.add(name)
        self._upgradable.discard(name)
        self._info_cache.pop(name, None)
        self._deps_cache.pop(name, None)
        self._populate_options()
        self.run_worker(
            self._sync_cache_files(),
            name="cache-sync",
            group="cache",
            exclusive=True,
            exit_on_error=False,
        )

    async def _sync_cache_files(self) -> None:
        await asyncio.gather(
            self._write_cache(INSTALLED_CACHE, sorted(self._installed)),
            self._write_cache(AVAILABLE_CACHE, sorted(self._available)),
            self._write_cache(UPGRADABLE_CACHE, sorted(self._upgradable)),
        )

    def _start_upgrade(self) -> None:
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
        self._start_upgrade()

    def action_upgrade_all(self) -> None:
        self._start_upgrade()

    async def _run_dnf(
        self,
        args: list[str],
        success_msg: str = "Command completed successfully",
        name: str | None = None,
        installed: str | None = None,
    ) -> None:
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
        """Run `sudo -n <cmd> ...` once, streaming stdout to the right panel."""
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
        self.query_one("#spinner", LoadingIndicator).remove_class("hidden")
        self.query_one("#options_text", Static).update(message)

    def _hide_status(self) -> None:
        self.query_one("#spinner", LoadingIndicator).add_class("hidden")
        self.query_one("#options_text", Static).update(DEFAULT_HINT)


def main() -> None:
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
