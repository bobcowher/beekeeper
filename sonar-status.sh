#!/bin/bash
# sonar-status.sh — Pull a SonarCloud quality summary for any public project.
#
# Usage:
#   ./sonar-status.sh                           # default: bobcowher_beekeeper
#   ./sonar-status.sh <project-key>             # any SonarCloud project key
#   ./sonar-status.sh bobcowher_beekeeper main  # specific branch
#
# Auth: set SONAR_TOKEN env var to authenticate. Falls back to unauthenticated
# for public projects.
#
# Output: plain-text summary suitable for pasting into Claude or a PR comment.
# Requires: curl, python3 (stdlib only)

set -euo pipefail

PROJECT="${1:-bobcowher_beekeeper}"
BRANCH="${2:-}"
BASE="https://sonarcloud.io/api"

branch_param=""
if [ -n "$BRANCH" ]; then
    branch_param="&branch=${BRANCH}"
fi

TOKEN="${SONAR_TOKEN:-}"

fetch() {
    if [ -n "${TOKEN:-}" ]; then
        curl -s -u "${TOKEN}:" "$BASE/$1"
    else
        curl -s "$BASE/$1"
    fi
}

echo "=== SonarCloud: ${PROJECT} ==="
[ -n "$BRANCH" ] && echo "Branch: ${BRANCH}"
[ -n "${TOKEN:-}" ] && echo "Auth: token" || echo "Auth: none (public)"
echo ""

# Quality gate
QG=$(fetch "qualitygates/project_status?projectKey=${PROJECT}${branch_param}")
QG_STATUS=$(echo "$QG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['projectStatus']['status'])" 2>/dev/null || echo "UNKNOWN")
echo "Quality Gate: ${QG_STATUS}"

# Failed conditions
FAILED=$(echo "$QG" | python3 -c "
import sys, json
d = json.load(sys.stdin)
conditions = d['projectStatus'].get('conditions', [])
failed = [c for c in conditions if c['status'] == 'ERROR']
for c in failed:
    print(f\"  FAIL  {c['metricKey']}: actual={c['actualValue']} threshold={c['errorThreshold']}\")
" 2>/dev/null)
[ -n "$FAILED" ] && echo "$FAILED"

echo ""

# Key metrics
METRICS=$(fetch "measures/component?component=${PROJECT}${branch_param}&metricKeys=bugs,vulnerabilities,code_smells,coverage,duplicated_lines_density,ncloc,security_hotspots")
echo "$METRICS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
measures = {m['metric']: m.get('value', 'n/a') for m in d['component']['measures']}
print('Metrics:')
print(f\"  Lines of code:       {measures.get('ncloc', 'n/a')}\")
print(f\"  Bugs:                {measures.get('bugs', 'n/a')}\")
print(f\"  Vulnerabilities:     {measures.get('vulnerabilities', 'n/a')}\")
print(f\"  Security hotspots:   {measures.get('security_hotspots', 'n/a')}\")
print(f\"  Code smells:         {measures.get('code_smells', 'n/a')}\")
print(f\"  Coverage:            {measures.get('coverage', 'n/a')}%\")
print(f\"  Duplication:         {measures.get('duplicated_lines_density', 'n/a')}%\")
" 2>/dev/null

echo ""

# Open issues by severity
ISSUES=$(fetch "issues/search?componentKeys=${PROJECT}${branch_param}&statuses=OPEN&ps=1&facets=severities,types")
echo "$ISSUES" | python3 -c "
import sys, json
d = json.load(sys.stdin)
total = d.get('paging', {}).get('total', d.get('total', 0))
print(f'Open issues: {total}')
facets = {f['property']: f['values'] for f in d.get('facets', [])}
if 'severities' in facets:
    print('  By severity:')
    for sv in ['BLOCKER','CRITICAL','MAJOR','MINOR','INFO']:
        count = next((v['count'] for v in facets['severities'] if v['val'] == sv), 0)
        if count:
            print(f'    {sv:<10} {count}')
if 'types' in facets:
    print('  By type:')
    for t in ['BUG','VULNERABILITY','CODE_SMELL','SECURITY_HOTSPOT']:
        count = next((v['count'] for v in facets['types'] if v['val'] == t), 0)
        if count:
            print(f'    {t:<20} {count}')
" 2>/dev/null

echo ""
echo "Dashboard: https://sonarcloud.io/project/overview?id=${PROJECT}"
