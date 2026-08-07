# dnfseek

A TUI package browser for Fedora, built with [Textual](https://github.com/Textualize/textual).
A Python port of the original bash + fzf [`dnfseek`](https://github.com/Sam-Horry/dnfseek) script.

![dnfseek](https://img.shields.io/badge/python-3.12%2B-blue)

## Prerequisites

- Fedora (or another dnf-based distro)
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh` (recommended)

## Test it out

You can test out dnfseek without having to install it, using uvx:

```bash
uvx --from git+https://github.com/Sam-Horry/dnfseek-tui dnfseek
```

## Installation

### From git (recommended)

```bash
uv tool install git+https://github.com/Sam-Horry/dnfseek-tui
```

### From PyPI

```bash
uv tool install dnfseek
```

Either way you get a `dnfseek` command on your PATH. On first run the app
asks for your sudo password (via `sudo -v`) before the interface starts.

## Usage

```bash
dnfseek
```

| Key | Action |
| ----- | -------- |
| `tab` | Switch between search and package list |
| `u` | Upgrade all packages |
| `r` | Refresh package cache |
| `i` / `x` / `e` / `g` | Install / remove / reinstall / update selected package |
| `d` | Show dependencies |
| `space` | Show package info |

Package lists are cached in `~/.cache/dnfseek` and refreshed if older than
24 hours.

Using the built-in command palette (ctrl+p), you can change dnfseek's behaviour by searching all
available packages, or only search packages currently installed on your system.
All the above actions can be performed from the command palette, as well as:

| Command | Action |
| --------- | -------- |
| Search all | Search all available packages |
| Search installed | Search installed packages |
| Theme | Change the app theme |
| Keys | Shows a help widget with a summary of available keys |

## Updating

```bash
uv tool upgrade dnfseek
```

## Notes

- This tool is basically a wrapper for dnf, with only a few commands at the moment.
- This tool is provided as-is, use at your own risk. I am not liable if something goes
  wrong - though in practice, dnf makes it pretty hard to brick your system.
  Most mistakes are recoverable with a rollback or `dnf history undo`
- Installed as a tool, the binary lives in `~/.local/bin/dnfseek`, which
  takes precedence over a `dnfseek` script elsewhere on your PATH.
- Sudo authentication happens once, before the TUI starts. If your sudo
  session expires mid-session, restart the app to re-authenticate.
- Requires Python 3.12+; `uv` downloads a compatible interpreter if your
  system Python is older.
