#!/usr/bin/env bash
# Environment names for the dev scripts: CHEESE_CAVE_<NAME> first, KOHAKU_HUB_<NAME> as the fallback.
# The same rule as kohakuhub.envnames (an empty value counts as set). Source this file, then:
#   cheese_cave_default NAME DEFAULT
# leaves CHEESE_CAVE_<NAME> exported and holding the setting: its own value when set, else the
# old KOHAKU_HUB_<NAME> when that is set, else DEFAULT. The scripts then use only the new name.

cheese_cave_default() {
  local new="CHEESE_CAVE_$1" old="KOHAKU_HUB_$1"
  if [[ -z "${!new+x}" ]]; then
    if [[ -n "${!old+x}" ]]; then
      printf -v "${new}" '%s' "${!old}"
    else
      printf -v "${new}" '%s' "$2"
    fi
  fi
  export "${new}"
}
