#!/usr/bin/env bash
# Run every stage without Pegasus, to check the science before touching a cluster.
set -euo pipefail

YEAR_FROM=${YEAR_FROM:-2015}
YEAR_TO=${YEAR_TO:-2024}
MAX=${MAX:-300}
OUT=${OUT:-manual_output}
SPECIES=("Danaus plexippus" "Vanessa cardui" "Nonexistentus fakus")

mkdir -p "$OUT"
cd "$OUT"
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/bin" && pwd)"

stats=()
for name in "${SPECIES[@]}"; do
    s=$(echo "$name" | tr -c '[:alnum:]' '_' | tr '[:upper:]' '[:lower:]' | sed 's/_*$//')
    echo "=== $name ==="
    "$BIN/fetch_occurrences.py" --species "$name" \
        --year-from "$YEAR_FROM" --year-to "$YEAR_TO" --max-records "$MAX" \
        --output "${s}_occurrences.csv" --match-output "${s}_match.json"
    "$BIN/summarise_species.py" --input "${s}_occurrences.csv" \
        --match "${s}_match.json" --species "$name" \
        --year-from "$YEAR_FROM" --year-to "$YEAR_TO" --max-records "$MAX" \
        --stats-output "${s}_stats.json" --figure-output "${s}_map.png"
    stats+=("${s}_stats.json")
done

"$BIN/merge_species.py" --inputs "${stats[@]}" \
    --table-output comparison.csv --figure-output comparison.png

echo
echo "Results in $OUT/"
cat comparison.csv
