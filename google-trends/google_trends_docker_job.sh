#!/bin/sh
set -eu

cd /app
now="$(date +%Y%m%d_%H%M%S)"
cache_file="/app/google_trends_wedding_formal_single_keyword_cache.jsonl"

echo "Google Trends job started at $(date -Iseconds)"

if [ -f "$cache_file" ]; then
  mv "$cache_file" "/cache_archive/google_trends_wedding_formal_single_keyword_cache_${now}.jsonl"
  echo "Archived cache for fresh crawl"
fi

python trends_wedding_formal_report.py

latest_workbook="$(ls -t /app/google_trends_wedding_formal_*.xlsx | head -n 1)"
if [ -z "$latest_workbook" ]; then
  echo "No generated workbook found"
  exit 1
fi

echo "Importing ${latest_workbook}"
python import_google_trends_to_db.py "$latest_workbook"

echo "Google Trends job finished at $(date -Iseconds)"
