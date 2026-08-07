# dnfseek

A TUI package browser for Fedora, built with [Textual](https://github.com/Textualize/textual).
A Python port of the original bash + fzf [`dnfseek`](https://github.com/Sam-Horry/dnfseek) script.

![dnfseek](https://img.shields.io/badge/python-3.12%2B-blue)

## Prerequisites

- Fedora (or another dnf-based distro)
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh` (recommended)

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

## Updating

```bash
uv tool upgrade dnfseek
```

## Notes

- Installed as a tool, the binary lives in `~/.local/bin/dnfseek`, which
  takes precedence over a `dnfseek` script elsewhere on your PATH.
- Sudo authentication happens once, before the TUI starts. If your sudo
  session expires mid-session, restart the app to re-authenticate.
- Requires Python 3.12+; `uv` downloads a compatible interpreter if your
  system Python is older.
