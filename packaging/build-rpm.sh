#!/usr/bin/env bash
#
# Build the RPM (and SRPM) locally, mirroring what Copr does.
#
#   ./packaging/build-rpm.sh
#
# The source tarball is created from the working tree (tracked + untracked
# files, minus ignored ones) so uncommitted changes can be tested. Copr
# instead uses the GitHub tag archive referenced by Source0.
#
# rpmbuild runs the spec's %build, which uses uv + PyInstaller to freeze the
# app into a single executable. Requires: uv, rpmbuild (dnf install
# rpm-build), git, tar. Artifacts: build/rpmbuild/{SRPMS,RPMS}/...

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

pyproject_version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
spec_version="$(sed -n 's/^Version:[[:space:]]*//p' packaging/dnfseek.spec)"
if [[ "$pyproject_version" != "$spec_version" ]]; then
    echo "error: pyproject.toml version $pyproject_version != packaging/dnfseek.spec Version $spec_version" >&2
    exit 1
fi

for tool in uv rpmbuild git tar python3; do
    if ! command -v "$tool" >/dev/null; then
        echo "error: $tool is required (uv and rpm-build packages)" >&2
        exit 1
    fi
done

topdir="$repo_root/build/rpmbuild"
rm -rf "$topdir"
mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}

# Working-tree tarball with the same layout as the GitHub tag archive. The
# dnfseek symlink is a local convenience that must not end up in the source.
tarball="$topdir/SOURCES/dnfseek-tui-$pyproject_version.tar.gz"
git ls-files -z --cached --others --exclude-standard \
    | tar --null --exclude=dnfseek \
        --transform "s,^,dnfseek-tui-$pyproject_version/," \
        -C "$repo_root" \
        -czf "$tarball" \
        -T -

cp packaging/dnfseek.spec "$topdir/SPECS/"

echo "==> Building dnfseek $pyproject_version (uv + PyInstaller run inside %build)"
rpmbuild_args=(-ba "$topdir/SPECS/dnfseek.spec" --define "_topdir $topdir")
# Copr and mock install BuildRequires automatically; locally uv usually comes
# from ~/.local/bin (checked above), not from an RPM, so skip the check.
if ! rpm -q uv >/dev/null 2>&1; then
    rpmbuild_args+=(--nodeps)
fi
rpmbuild "${rpmbuild_args[@]}"

echo
echo "==> Built:"
find "$topdir/RPMS" "$topdir/SRPMS" -name '*.rpm' -printf '    %p\n'
