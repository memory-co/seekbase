#!/usr/bin/env bash
# Build seekbase and upload to PyPI. (Mirrors memory.talk/scripts/release.sh.)
#
# Prereqs:
#   - `python3 -m pip install build twine` (one-time; auto-used from .venv if present)
#   - PyPI token configured in ~/.pypirc, e.g.
#       [pypi]
#         username = __token__
#         password = pypi-...
#   - Git working tree is clean (script will refuse if dirty unless --force).
#
# Usage:
#   scripts/release.sh                  # build + upload to real PyPI
#   scripts/release.sh --test           # build + upload to TestPyPI
#   scripts/release.sh --build          # build only, no upload
#   scripts/release.sh --force          # skip the clean-tree check
#
# After upload:
#   pip install seekbase==<version>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prefer the project venv (where build/twine were installed), fall back to python3.
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

TARGET="pypi"
BUILD_ONLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --test)   TARGET="testpypi" ;;
    --build)  BUILD_ONLY=1 ;;
    --force)  FORCE=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0 ;;
    *)
      echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# --- preflight ---

if [ "$FORCE" = "0" ] && [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree has uncommitted changes." >&2
  echo "       commit or stash first, or pass --force to override." >&2
  git status --short >&2
  exit 1
fi

VERSION=$("$PYTHON" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
NAME=$("$PYTHON" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['name'])")

echo ">>> package: $NAME"
echo ">>> version: $VERSION"
echo ">>> target:  $TARGET"
echo

# --- clean ---

rm -rf dist/ build/ *.egg-info "${NAME//-/_}.egg-info"

# --- build ---

"$PYTHON" -m build
echo
echo ">>> built artifacts:"
ls -lh dist/

# --- verify metadata ---

"$PYTHON" -m twine check dist/*

if [ "$BUILD_ONLY" = "1" ]; then
  echo ">>> --build passed; skipping upload."
  exit 0
fi

# --- upload ---

if [ "$TARGET" = "testpypi" ]; then
  "$PYTHON" -m twine upload --repository testpypi dist/*
  echo
  echo ">>> uploaded to TestPyPI. Try:"
  echo "    pip install --index-url https://test.pypi.org/simple/ \\"
  echo "                --extra-index-url https://pypi.org/simple/ \\"
  echo "                $NAME==$VERSION"
else
  "$PYTHON" -m twine upload dist/*
  echo
  echo ">>> uploaded to PyPI. Try:"
  echo "    pip install $NAME==$VERSION"
fi
