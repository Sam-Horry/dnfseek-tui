from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, Static, Button, Input, Header, Footer, ListView, ListItem


class HeaderAndFooterExample(App):
    CSS_PATH = "main.tcss"
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Options", id="options")

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
        self.title = "dnfseek"
        self.sub_title = "a TUI dnf wrapper"
        self.styles.scrollbar_visibility = "hidden"

if __name__ == "__main__":
    app = HeaderAndFooterExample()
    app.run()
