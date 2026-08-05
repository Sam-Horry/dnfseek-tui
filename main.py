from typing import Iterable
import asyncio
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.screen import Screen, ModalScreen
from textual.widgets import Label, Static, Button, Input, Header, Footer, ListView, ListItem, Tabs, Tab

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

    # cache the password
    def __init__(self) -> None:
        super().__init__()
        self._sudo_password: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Press ctrl + p to view commands", id="options")

        with Horizontal():
            with Vertical():
                yield Input(placeholder="Type Package Name", id="input")
                yield ListView(
                    ListItem(Label("Package 1")),
                    ListItem(Label("Package 2")),
                    ListItem(Label("Package 3")),
                    id="left_panel")
            yield Static("Right panel", id="right_panel")
        yield Footer()

    def on_mount(self) -> None:
        self.styles.scrollbar_visibility = "hidden"

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

    async def upgrade_all(self) -> None:
        # upgrade all packages
        self.notify("Upgrading all packages...", timeout=2)
        self.run_worker(
            self._run_dnf(["dnf", "upgrade", "-y"]),
            name="upgrade_all",
            group="dnf",
            exclusive=True,
            exit_on_error=False,
        )

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
