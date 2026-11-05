#!/bin/sh
# Run the context-lens CLI with whatever is available: an installed copy, uv,
# pipx, or (needing only python3) a private virtualenv created once in the
# user's cache directory. Set CONTEXT_LENS_REFRESH=1 to reinstall that copy.
SRC="git+https://github.com/AaryanJ45/context-lens"
if command -v context-lens >/dev/null 2>&1; then exec context-lens "$@"; fi
if command -v uvx >/dev/null 2>&1; then exec uvx --quiet --from "$SRC" context-lens "$@"; fi
if command -v pipx >/dev/null 2>&1; then exec pipx run --spec "$SRC" context-lens "$@"; fi
if command -v python3 >/dev/null 2>&1; then
  VENV="${XDG_CACHE_HOME:-$HOME/.cache}/context-lens/venv"
  if [ -n "$CONTEXT_LENS_REFRESH" ] || [ ! -x "$VENV/bin/context-lens" ]; then
    echo "context-lens: installing into $VENV (one time)…" >&2
    python3 -m venv "$VENV" >&2 && "$VENV/bin/pip" install --quiet --disable-pip-version-check --upgrade "$SRC" >&2 || {
      echo "context-lens: install failed; try: pipx install $SRC" >&2; exit 127; }
  fi
  exec "$VENV/bin/context-lens" "$@"
fi
echo "context-lens needs Python 3.10+. Install it, then: pipx install $SRC" >&2
exit 127
