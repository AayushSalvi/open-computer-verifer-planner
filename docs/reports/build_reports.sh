#!/usr/bin/env bash
# Build every report in this folder to .docx.
#
#   ./docs/reports/build_reports.sh              # build all reports
#   ./docs/reports/build_reports.sh 2026-07-20   # build ones matching a filter
#
# Convention: one markdown file per report, named
#   YYYY-MM-DD_<slug>.md   ->   YYYY-MM-DD_<slug>.docx
# The .md is the source of truth; the .docx is generated and gitignored.
# Close the .docx in Word before rebuilding — Word holds an exclusive lock and
# pandoc will fail with "permission denied".
set -euo pipefail

cd "$(dirname "$0")"
filter="${1:-}"
built=0

for md in *.md; do
    [ -e "$md" ] || continue
    case "$md" in README.md) continue ;; esac
    if [ -n "$filter" ] && [[ "$md" != *"$filter"* ]]; then continue; fi

    docx="${md%.md}.docx"
    # First "# " heading becomes the document title.
    title="$(grep -m1 '^# ' "$md" | sed 's/^# //')"

    if pandoc "$md" -o "$docx" --toc --toc-depth=2 \
         -M title="$title" \
         -M subtitle="Process-Checking Verifier for GUI Agent Tasks"; then
        echo "  built  $docx"
        built=$((built + 1))
    else
        echo "  FAILED $docx (is it open in Word?)" >&2
    fi
done

echo "$built report(s) built."
