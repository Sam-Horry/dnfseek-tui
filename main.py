import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Iterable

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.screen import Screen, ModalScreen
from textual.widgets import Input, Static, Header, Footer, OptionList
from textual.widgets._option_list import Option

CACHE_DIR = Path.home() / ".cache" / "dnfseek"
INSTALLED_CACHE = CACHE_DIR / "installed"
AVAILABLE_CACHE = CACHE_DIR / "available"
CACHE_MAX_AGE = 24 * 60 * 60


class PasswordScreen(ModalScreen[str]):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="sudo password", password=True, id="password")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class DnfseekApp(App):
    CSS_PATH = "main.tcss"
    TITLE = "dnfseek"
    SUB_TITLE = "a TUI dnf wrapper"

    BINDINGS = [
        ("u", "upgrade_all", "Upgrade all packages"),
        ("r", "refresh_cache", "Refresh cache"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sudo_password: str | None = None
        self._installed: set[str] = set()
        self._available: set[str] = set()
        self._info_cache: dict[str, str] = {}
        self._pending_fetches: set[str] = set()
        self._view_names: list[str] = []
        self._filter = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Press ctrl + p to view commands", id="options")

        with Horizontal():
            with Vertical():
                yield Input(placeholder="Type Package Name", id="input")
                yield OptionList(id="left_panel")
            yield Static("Right panel", id="right_panel")
        yield Footer()

    def on_mount(self) -> None:
        self.styles.scrollbar_visibility = "hidden"
        self.run_worker(
            self._show_packages(installed_only=False),
            name="search-all",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        # keep built-in commands
        yield from super().get_system_commands(screen)
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
            "Show installed", "Show installed packages", self.show_installed
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

    def _cache_is_stale(self) -> bool:
        for path in (INSTALLED_CACHE, AVAILABLE_CACHE):
            if not path.exists():
                return True
            if time.time() - path.stat().st_mtime > CACHE_MAX_AGE:
                return True
        return False

    async def update_cache(self) -> None:
        self.notify("Refreshing package lists...", timeout=2)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        installed, available = await asyncio.gather(
            self._dnf_list(["list", "--installed"]),
            self._dnf_list(
                ["-y", "--setopt=gpgcheck=1", "--setopt=repo_gpgcheck=1",
                 "list", "--available"]
            ),
        )
        if installed is None or available is None:
            self.notify("Failed to refresh package lists", severity="error")
            return
        installed_names = self._parse_dnf_list(installed)
        available_names = self._parse_dnf_list(available)
        await asyncio.gather(
            self._write_cache(INSTALLED_CACHE, installed_names),
            self._write_cache(AVAILABLE_CACHE, available_names),
        )
        self._installed = set(installed_names)
        self._available = set(available_names)
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
            Option(f"✅ {name}" if name in self._installed else name, id=name)
            for name in names
        ]
        left = self.query_one("#left_panel", OptionList)
        left.clear_options()
        left.add_options(options)

    async def search_all(self) -> None:
        self.run_worker(
            self._show_packages(installed_only=False),
            name="search-all",
            group="search",
            exclusive=True,
            exit_on_error=False,
        )

    async def show_installed(self) -> None:
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
        if event.value == self._filter:
            return
        self._filter = event.value
        self._populate_options()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        name = event.option_id
        if name is None:
            return
        right_panel = self.query_one("#right_panel", Static)
        if name in self._info_cache:
            right_panel.update(self._info_cache[name])
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
        highlighted = self.query_one("#left_panel", OptionList).highlighted_option
        if highlighted is not None and highlighted.id == name:
            self.query_one("#right_panel", Static).update(info)

    def _start_upgrade(self) -> None:
        self.notify("Upgrading all packages...", timeout=2)
        self.run_worker(
            self._run_dnf(["dnf", "upgrade", "-y"]),
            name="upgrade_all",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

    async def upgrade_all(self) -> None:
        self._start_upgrade()

    def action_upgrade_all(self) -> None:
        self._start_upgrade()

    async def _run_dnf(self, args: list[str]) -> None:
        if not self._sudo_password:
            self._sudo_password = await self.push_screen_wait(PasswordScreen())
            if not self._sudo_password:
                return
        process = await asyncio.create_subprocess_exec(
            "sudo", "-S", *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(f"{self._sudo_password}\n".encode())
        if process.returncode == 0:
            self.notify("All packages upgraded!", timeout=2, severity="information")
        else:
            if b"incorrect password" in stderr.lower() or b"Sorry" in stderr:
                self._sudo_password = None
            self.notify(f"Upgrade failed (error code {process.returncode})", severity="error")


if __name__ == "__main__":
    app = DnfseekApp()
    app.run()
