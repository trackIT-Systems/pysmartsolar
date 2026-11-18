# Releasing

Versions follow [Semantic Versioning](https://semver.org/). The public interface is the CLI, the config file, the MQTT and CSV outputs and the control socket. Within a major version, what exists is kept and new things are added:

- **Kept:** commands and options of the CLI, config keys and the values they take, output columns with their position and unit, event names and `detail` keys, socket commands, arguments and result fields. None is removed or renamed, and none changes its meaning.
- **Added in a minor version:** new commands, options, config keys and values, new columns (appended: `TRAILING` in `src/smartsolar/columns.py`, see [outputs.md](outputs.md#compatibility)), events, `detail` keys, socket commands and result fields. Consumers ignore what they do not know.
- **Patch:** fixes. A value that was decoded wrongly is a fix, even though consumers see a different number.
- **Major:** removing, renaming or changing the meaning of anything above.

The outputs are covered from 1.3.0 on, the rest since 1.0.0. 1.3.0 replaced the MQTT and CSV outputs of 1.2 as a whole; tsOS ships pysmartsolar pinned and announced that change with the tsOS release that brought it.

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
