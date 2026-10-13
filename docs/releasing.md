# Releasing

Versions follow [Semantic Versioning](https://semver.org/): a breaking change of the CLI, the config file, the MQTT/CSV output or the socket protocol is a major version; new features are minor; fixes are patch.

A release is made by a git tag; GitHub Actions (`.github/workflows/release.yml`) does the rest.

1. Set `__version__` in `src/smartsolar/__init__.py` (the only place; `pyproject.toml` reads it).
2. In `CHANGELOG.md` move the entries to a new section `## [X.Y.Z] - YYYY-MM-DD` (Added / Changed / Deprecated / Removed / Fixed / Security) and add the link at the bottom. The section becomes the release notes.
3. Commit, push, wait for the tests to pass.
4. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin vX.Y.Z
   ```

The workflow runs the tests on Python 3.12 to 3.14, checks that the tag equals `__version__` and that the changelog has a section for it, builds the sdist and the wheel (`python -m build`) and creates the GitHub release with both files and the changelog section as notes. A tag with a suffix (`v1.1.0-rc.1`) makes a pre-release. If a check fails nothing is released; delete the tag (`git push --delete origin vX.Y.Z`), fix and tag again.

Check the build locally with `pip install build && python -m build`.
