# dnfseek

A TUI package browser for Fedora, built with [Textual](https://github.com/Textualize/textual).
A Python port of my bash + fzf [`dnfseek`](https://github.com/Sam-Horry/dnfseek) script,
which is a fork of this [`project`](https://github.com/OmarHesham2356/dnfseek) of the same name.

![dnfseek](https://img.shields.io/badge/python-3.12%2B-blue)

## Prerequisites

- Fedora (or another dnf-based distro)

If installing it as a uv tool:

- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Test it out

You can test out dnfseek without having to install it, using uvx:

```bash
uvx dnfseek
```

## Installation

### From Copr (Fedora)

```bash
sudo dnf copr enable sam-horry/dnfseek
sudo dnf install dnfseek
```

It is currently built for Fedora 44 and Rawhide on x86_64,
and updates arrive through `dnf upgrade`. If you would like dnfseek
for other Fedora versions let me know!

If you previously installed dnfseek with `uv tool`, remove that copy
(`uv tool uninstall dnfseek`) so it doesn't shadow `/usr/bin/dnfseek` on your
`PATH`.

### As a uv tool

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
| Search upgradeable | Search packages that have pending upgrades |
| Search installed | Search installed packages |
| Theme | Change the app theme |
| Keys | Shows a help widget with a summary of available keys |

## Updating

If installed via the Copr repository, you can upgrade dnfseek using dnf
(and you can technically upgrade dnfseek using dnfseek!)

If installed as a uv tool, you will have to manually update
dnfseek by running:

```bash
uv tool upgrade dnfseek
```

## Notes

- This tool is basically a wrapper for dnf, with only a few commands at the moment.
- I have confirmed that this tool works on my personal Fedora laptop, running Fedora 44.
  It may work on other dnf-based systems, but they are untested,
  and I can't guarantee they will work.
- This tool is provided as-is, use at your own risk. I am not liable if something goes
  wrong - though in practice, dnf makes it pretty hard to brick your system.
  Most mistakes are recoverable with a rollback or `dnf history undo`
- Sudo authentication happens once, before the TUI starts. If your sudo
  session expires mid-session, restart the app to re-authenticate.
