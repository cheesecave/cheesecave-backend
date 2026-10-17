#!/bin/bash
# Who depends on the form of a commit id? READ-ONLY: it only reads the gateway's log and prints counts.
# No IP address, token, request body or user name is printed.
#
# Before the default `commit_id_format = hub` (40 hex) is set on a site that has run for a while, run it there:
#     ssh <host> bash -s < scripts/commit_id_consumers.sh
# A: who sends a 64 hex id in a URL (an id they stored); B: on which routes;
# C: who reads commit ids from responses (they see 40 hex where they saw 64: see docs/api/commits.md);
# D: the callers that are neither a browser nor huggingface_hub (the ones to look at, and to tell).
# If D is not empty and it compares what a response says with ids it stored, set CHEESE_CAVE_COMMIT_ID_FORMAT=full
# for a while. The gateway container is the one whose name has gateway or nginx in it.
GW=$(docker ps --format '{{.Names}}' < /dev/null | grep -i -E 'gateway|nginx' | head -1)
echo "gateway container: $GW"
LOG=$(mktemp)
docker logs "$GW" 2>&1 < /dev/null | sed 's/\x1b\[[0-9;]*m//g' > "$LOG"
echo "log lines: $(wc -l < "$LOG")   first: $(head -1 "$LOG" | grep -o '\[[^]]*\]' | head -1)   last: $(tail -1 "$LOG" | grep -o '\[[^]]*\]' | head -1)"

fam() {  # the family of the user agent of each log line, counted
  sed -E 's/.*" ([0-9]{3}) [0-9]+ "[^"]*" "([^"]*)".*/\2/' | awk '{
    ua=$0
    if (ua ~ /hf_hub|huggingface_hub/) f="huggingface_hub client";
    else if (ua ~ /Mozilla/) f="browser";
    else if (ua ~ /curl/) f="curl";
    else if (ua ~ /python-requests/) f="python-requests";
    else if (ua ~ /python-httpx/) f="python-httpx";
    else if (ua ~ /Go-http|okhttp|node|axios|Java/) f="other http library";
    else if (ua == "-" || ua == "") f="(no user agent)";
    else { split(ua, a, /[\/ ]/); f="other: " a[1] }
    print f }' | sort | uniq -c | sort -rn | head -8
}

echo; echo "## A. who sends a 64-hex id IN THE URL (any route)"
grep -E '"(GET|POST|HEAD|PUT|DELETE) [^ ]*/[0-9a-f]{64}([/ ?]|")' "$LOG" | fam
echo; echo "## B. which routes carry it"
grep -E '"(GET|POST|HEAD|PUT|DELETE) [^ ]*/[0-9a-f]{64}([/ ?]|")' "$LOG" \
  | sed -E 's/.*"(GET|POST|HEAD|PUT|DELETE) ([^ ?]*)[^"]*" ([0-9]{3}).*/\1 \2 \3/' \
  | sed -E 's#/api/(models|datasets|spaces)/[^/]+/[^/]+/#/api/T/N/#; s#^(GET|POST|HEAD) /(models|datasets|spaces)/[^/]+/[^/]+/#\1 /T/N/#; s#^(GET|POST|HEAD) /[^/]+/[^/]+/resolve/#\1 /N/resolve/#; s#[0-9a-f]{64}#<id64>#g' \
  | sed -E 's#(<id64>)/.*#\1/...#' | sort | uniq -c | sort -rn | head -12
echo; echo "## C. who READS commit ids from responses (these see 40 hex where they saw 64)"
for route in 'resolve/' '/revision/' '/commits/' '/refs' '/tree/' '/paths-info/'; do
  n=$(grep -E "\"(GET|HEAD|POST) [^ ]*${route}" "$LOG" | grep -c .)
  echo "-- $route : $n requests; by caller:"
  grep -E "\"(GET|HEAD|POST) [^ ]*${route}" "$LOG" | fam | head -4
done
echo; echo "## D. neither a browser nor huggingface_hub, on the routes that say a commit id"
grep -E '"(GET|HEAD|POST) [^ ]*(/revision/|/commits/|/refs|/commit/|resolve/)' "$LOG" | grep -v -E 'Mozilla|hf_hub|huggingface_hub' | fam | head -8
rm -f "$LOG"
