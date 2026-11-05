#!/bin/sh
# SessionStart: when this checkout uses a compact applied with --apply-local and
# the committed CLAUDE.md has changed since (a pull), tell the session the slim
# copy may be missing rules. Plain sh so it never installs anything; silent otherwise.
DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
STATE="$DIR/.claude/context-lens-local.json"
[ -f "$STATE" ] || exit 0
ORIG=$(sed -n 's/.*"original": *"\([^"]*\)".*/\1/p' "$STATE")
WANT=$(sed -n 's/.*"sha256": *"\([0-9a-f]*\)".*/\1/p' "$STATE")
[ -n "$ORIG" ] || exit 0
if [ -f "$DIR/$ORIG" ]; then
  HAVE=$( (shasum -a 256 "$DIR/$ORIG" 2>/dev/null || sha256sum "$DIR/$ORIG") | cut -d' ' -f1)
fi
[ "$HAVE" = "$WANT" ] && exit 0
echo "context-lens local mode: $ORIG changed since the slim CLAUDE.local.md was made, so it may be missing new rules. Read $ORIG for anything newer than CLAUDE.local.md before you start, and tell the user to re-run /context-lens:compact (local) or \`context-lens local off\`."
