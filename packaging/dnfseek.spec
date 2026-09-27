# Build dnfseek from source into a self-contained PyInstaller executable.
#
# The Copr build runs in a clean chroot: uv (packaged in Fedora) creates the
# project's locked environment from uv.lock and PyInstaller (fetched from
# PyPI) freezes main.py together with CPython, textual and rapidfuzz. The
# resulting RPM therefore has no Python runtime dependencies. PyInstaller is
# not packaged in Fedora, so the Copr project must have internet access
# enabled for builds.
#
# Local test build (mirrors the Copr build):
#   ./packaging/build-rpm.sh
#
# Automation: .github/workflows/copr.yml builds the SRPM on a version tag and
# submits it to Copr. See packaging/README.md for the one-time setup.

Name:           dnfseek
Version:        0.1.10
Release:        1%{?dist}
Summary:        A TUI package browser for Fedora, built with Textual

License:        MIT
URL:            https://github.com/Sam-Horry/dnfseek-tui

# The bundle contains an x86-64 CPython interpreter.
BuildArch:      x86_64

# The payload is a prebuilt, already-stripped bundle: there is no useful
# debuginfo to extract.
%global debug_package %{nil}

# Pinned PyInstaller (fetched from PyPI; Fedora does not package it).
%global pyinstaller_version 6.22.3

# Tag archive of this repository. The extra path component makes GitHub serve
# the download under a filename matching the entry's basename.
Source0:        %{url}/archive/v%{version}/%{name}-tui-%{version}.tar.gz

# uv builds the locked environment and runs PyInstaller.
BuildRequires:  uv
BuildRequires:  ca-certificates

%description
dnfseek is a Textual terminal UI for browsing Fedora packages: search all,
installed or upgradable packages with fuzzy client-side filtering, preview
`dnf info` and dependencies, and run install/remove/reinstall/update actions
through sudo. CPython, Textual and rapidfuzz are bundled inside the
executable, so no system Python packages are required.

%prep
%autosetup -n %{name}-tui-%{version}

%build
# Freeze the app exactly like packaging/build-rpm.sh does locally. uv uses a
# managed CPython 3.14 (python-build-standalone) plus the locked dependencies
# from uv.lock, so the bundle does not depend on the chroot's interpreter or
# Python packages. PyInstaller comes from PyPI (pinned below).
export UV_PYTHON_PREFERENCE=only-managed
uv run --frozen --python "3.14" \
    --with pyinstaller==%{pyinstaller_version} -- \
    pyinstaller --onefile --noconfirm --name dnfseek \
    --add-data "main.tcss:." main.py

%install
install -Dpm 0755 dist/dnfseek %{buildroot}%{_bindir}/%{name}

%files
%license LICENSE
%{_bindir}/%{name}

%changelog
* Sun Sep 27 2026 Samuel Horry <211251918+Sam-Horry@users.noreply.github.com> - 0.1.9-1
- Initial RPM: PyInstaller-bundled dnfseek with no Python runtime deps
