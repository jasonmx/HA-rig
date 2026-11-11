#!/usr/bin/env bash
# Check out the deviceside ref the rig's tests run against, into <clone> (cloned there if it isn't a clone yet).
#
#   ci_deviceside.sh <clone> <rig-pr>     <rig-pr> is empty when CI isn't running for a PR
#
# A rig PR whose body has a `Tests-with: DeviceSide/DeviceSide#N` line tests against that deviceside PR: its head while
# it's open, its base branch once merged. A closed, unmerged PR, or a Tests-with line in any other shape, fails.
# Without one, deviceside's newest line of work: whichever of main and the release branches had the latest commit,
# as deviceside's PRs pick their base. The choice and its reason are always printed, so a fallback can't pass as
# the pairing it replaced.
#
# Needs gh, authenticated to read the rig PR (GH_TOKEN in CI), and GITHUB_REPOSITORY when <rig-pr> is set.
set -euo pipefail

usage='usage: ci_deviceside.sh <clone> <rig-pr>'
clone=${1:?$usage}
rig_pr=${2-}
deviceside=DeviceSide/DeviceSide

fail() {
    echo "ci_deviceside: $*" >&2
    exit 1
}

# Prints N from the body's Tests-with line, or nothing when there's none. Read fresh rather than from the event
# payload, which a re-run replays as it was.
tests_with() {
    local body line found='' looks_like='^[[:space:]`*>-]*tests-with[[:space:]]*:'
    body=$(gh pr view "$rig_pr" -R "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}" --json body --jq .body)
    while IFS= read -r line; do
        line=${line%$'\r'}
        # Any line that starts like Tests-with must be one exactly: a typo shouldn't quietly test the default.
        [[ ${line,,} =~ $looks_like ]] || continue
        [[ $line =~ ^Tests-with:[[:space:]]*DeviceSide/DeviceSide#([0-9]+)[[:space:]]*$ ]] ||
            fail "rig PR #$rig_pr: '$line' isn't 'Tests-with: $deviceside#N'"
        [[ -z $found ]] || fail "rig PR #$rig_pr names more than one Tests-with PR"
        found=${BASH_REMATCH[1]}
    done <<<"$body"
    echo "$found"
}

if [[ ! -d $clone/.git ]]; then
    git clone --quiet --filter=blob:none --no-checkout "https://github.com/$deviceside" "$clone"
fi

n=
[[ -z $rig_pr ]] || n=$(tests_with)

if [[ -n $n ]]; then
    pr=$(gh pr view "$n" -R "$deviceside" --json state,baseRefName --jq '"\(.state) \(.baseRefName)"')
    read -r state base <<<"$pr"
    case $state in
        OPEN)
            ref=refs/pull/$n/head
            git -C "$clone" fetch --quiet origin "+$ref:$ref"
            why="Tests-with #$n, open"
            label=$ref
            ;;
        MERGED)
            ref=refs/remotes/origin/$base
            git -C "$clone" fetch --quiet origin "+refs/heads/$base:$ref"
            why="Tests-with #$n, merged"
            label=$base
            ;;
        *)
            fail "Tests-with $deviceside#$n is $state, not open or merged: name another PR, or drop the line"
            ;;
    esac
else
    git -C "$clone" fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main' \
        '+refs/heads/release/*:refs/remotes/origin/release/*'
    ref=$(git -C "$clone" for-each-ref --sort=-committerdate --count=1 --format='%(refname)' \
        refs/remotes/origin/main 'refs/remotes/origin/release/*')
    label=${ref#refs/remotes/origin/}
    why="newest of main and release/*"
    [[ -z $rig_pr ]] || why="rig PR #$rig_pr has no Tests-with; $why"
fi

git -C "$clone" checkout --quiet --detach "$ref"
echo "deviceside at $label ($why), $(git -C "$clone" rev-parse --short HEAD)"
