# Packaging

`dnfseek.spec` builds a self-contained executable with PyInstaller: `uv`
creates the project's locked environment from `uv.lock`, and PyInstaller
freezes `main.py` together with CPython, textual and rapidfuzz. The RPM has
no Python runtime dependencies; it only needs glibc.

## Local test build

```bash
./packaging/build-rpm.sh
```

This builds an SRPM and an RPM from the working tree under
`build/rpmbuild/`. It needs `uv`, `rpm-build`, `git` and `tar`.

## Copr setup (one time)

1. Create a FAS account and log in at https://copr.fedorainfracloud.org/,
   then copy the API token from https://copr.fedorainfracloud.org/api/ into
   `~/.config/copr` and install `copr-cli`.

2. Create the project. Internet access during builds is required because
   PyInstaller is not packaged in Fedora and both it and the locked PyPI
   dependencies are downloaded during `%build`:

   ```bash
   copr-cli create dnfseek \
       --chroot fedora-44-x86_64 --chroot fedora-rawhide-x86_64 \
       --enable-net on \
       --description "A TUI package browser for Fedora, built with Textual"
   ```

   Only x86-64 chroots work: the bundle ships an x86-64 interpreter.

3. In the GitHub repository settings, add:

   - secret `COPR_CONFIG` — the contents of `~/.config/copr`
   - variable `COPR_PROJECT` — e.g. `myusername/dnfseek`

## Releasing

Bump the version in `pyproject.toml` and `packaging/dnfseek.spec` (the local
script fails if they differ), commit, then push a matching tag:

```bash
git tag v0.1.10
git push origin main v0.1.10
```

`.github/workflows/copr.yml` builds an SRPM from the tag and submits it to
Copr, which builds every enabled chroot. Users install with:

```bash
sudo dnf copr enable myusername/dnfseek
sudo dnf install dnfseek
```

If a build fails and you need to retry without cutting a new release, push
the fix and run the workflow manually:

```bash
gh workflow run copr.yml
```
