#!/bin/bash
# Fetch data artifacts only; never install or execute downloaded packages.
set -euo pipefail
MANIFEST=validation/step03b/evidence-axe-step03b-5c3b9969-06d6-4960-afeb-c2e6db8d9f97/wheels.tsv
[ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ]
OUT=validation/step03b/wheels-$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')
/bin/mkdir "$OUT"
printf 'artifact_directory=%s\n' "$OUT"
COUNT=0
while IFS=$'\t' read -r filename digest size url; do
    [[ "$filename" =~ ^[A-Za-z0-9_.+-]+\.whl$ ]]
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]]
    [[ "$size" =~ ^[0-9]+$ ]]
    [[ "$url" == https://files.pythonhosted.org/packages/*/"$filename" ]]
    [[ "$url" != *'?'* && "$url" != *'#'* && "$url" != *'..'* ]]
    [ ! -e "$OUT/$filename" ]
    status=$(/usr/bin/curl -q --silent --show-error --fail --noproxy '*' \
        --proto '=https' --connect-timeout 10 --max-time 120 \
        --max-filesize "$size" --output "$OUT/$filename" --write-out '%{http_code}' "$url")
    [ "$status" = 200 ]
    actual_size=$(/usr/bin/stat -f '%z' "$OUT/$filename")
    [ "$actual_size" = "$size" ]
    actual_hash=$(/usr/bin/shasum -a 256 "$OUT/$filename")
    [ "${actual_hash%% *}" = "$digest" ]
    printf '%s\t%s\t%s\n' "$filename" "$digest" "$size" >> "$OUT/verified.tsv"
    COUNT=$((COUNT + 1))
    printf 'verified=%s file=%s\n' "$COUNT" "$filename"
done < "$MANIFEST"
printf 'verified_count=%s\nartifact_directory=%s\n' "$COUNT" "$OUT"
