#!/usr/bin/env bash
#           ---   Makefile like bash script for development    ---
#  --- Source this script in your terminal to set up the dev environment ---
#      --- Execute the script to see a list of available commands ---
# ------------------------------------------------------------------------------
[[ -n "${MKWD:-}" ]] || export MKWD="$(command cd . && pwd)"

# export BUILDKIT_PROGRESS=plain

if [[ $(basename "${0}" 2> /dev/null) != "make.sh" ]]; then
    # Script sourced, setup the environment.
    alias cdmk="cd $MKWD/"
    alias mk="$MKWD/make.sh"

    # Setup shell completion for the mk command
    _mk_completion() {
        local cur prev opts
        COMPREPLY=()
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"

        if [[ ${COMP_CWORD} == 1 ]]; then
            # Extract public function names directly from the script file
            opts=$(grep -E '^function [^_]' "$MKWD/make.sh" | sed 's/^function \([^{[:space:]]*\).*/\1/' | tr '\n' ' ')
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            return 0
        fi
    }
    if command -v complete >/dev/null 2>&1; then
        complete -F _mk_completion mk
    fi

    if [[ -f "$MKWD/.venv/bin/activate" ]]; then
        source "$MKWD/.venv/bin/activate"
        export PYTHONPATH=$MKWD
    fi

    echo "Environment setup complete. You can now run 'mk' to execute this script."
    return 0
fi

set -euo pipefail
# -----------------------------------------------------------------------------

VERSION_BUMP_FILES=(pyproject.toml mca/__init__.py uv.lock)

function _bump_check_versions {
    local expected_version="$1"
    local mismatch_message="$2"
    local pyproject_version init_version lock_version

    pyproject_version=$(sed -nE 's/^version = "([^"]+)"$/\1/p' pyproject.toml | head -n 1)
    init_version=$(sed -nE 's/^__version__ = "([^"]+)"$/\1/p' mca/__init__.py | head -n 1)
    lock_version=$(awk '
        /^name = "model-context-api"$/ { found = 1; next }
        found && /^version = / { gsub(/[" ]/, "", $3); print $3; exit }
        found && /^\[\[package\]\]$/ { exit }
    ' uv.lock)

    if [[ "$pyproject_version" != "$expected_version" || \
        "$init_version" != "$expected_version" || \
        "$lock_version" != "$expected_version" ]]; then
        echo "$mismatch_message" >&2
        echo "  pyproject.toml: $pyproject_version" >&2
        echo "  mca/__init__.py: $init_version" >&2
        echo "  uv.lock: $lock_version" >&2
        return 1
    fi
}


function bump {
    : "Read the current git tag, synchronize the package version, commit, and tag the release."
    : "If 'minor' is specified in args, bump minor version and reset patch to 0."
    : "If 'push' is specified in args, push the version commit and tag to origin."
    command cd "$MKWD"

    local current_tag current_version new_version new_tag branch
    local major minor patch
    local minor_bump=false push_release=false

    for arg in "$@"; do
        case "$arg" in
            minor) minor_bump=true ;;
            push) push_release=true ;;
            *)
                echo "Unknown bump argument: $arg" >&2
                return 1
                ;;
        esac
    done

    if ! git diff --quiet -- "${VERSION_BUMP_FILES[@]}" || \
        ! git diff --cached --quiet -- "${VERSION_BUMP_FILES[@]}"; then
        echo "Version files have uncommitted changes; refusing to bump." >&2
        return 1
    fi

    current_tag=$(git describe --tags --abbrev=0 --match 'v[0-9]*')
    if [[ ! "$current_tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        echo "Latest version tag must match vMAJOR.MINOR.PATCH: $current_tag" >&2
        return 1
    fi

    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    patch=${BASH_REMATCH[3]}
    current_version="$major.$minor.$patch"

    _bump_check_versions \
        "$current_version" \
        "Version files do not match the latest tag $current_tag."

    if [[ "$minor_bump" == true ]]; then
        minor=$((minor + 1))
        patch=0
    else
        patch=$((patch + 1))
    fi

    new_version="$major.$minor.$patch"
    new_tag="v$new_version"

    if git rev-parse --verify --quiet "refs/tags/$new_tag" >/dev/null; then
        echo "Tag already exists: $new_tag" >&2
        return 1
    fi

    (
        build_dir=""

        cleanup_build() {
            if [[ -n "$build_dir" && -d "$build_dir" ]]; then
                rm -rf -- "$build_dir"
            fi
            build_dir=""
        }

        rollback_version_files() {
            cleanup_build
            if { ! git diff --quiet -- "${VERSION_BUMP_FILES[@]}" || \
                ! git diff --cached --quiet -- "${VERSION_BUMP_FILES[@]}"; }; then
                git restore --source=HEAD --staged --worktree -- "${VERSION_BUMP_FILES[@]}" || true
            fi
        }

        trap rollback_version_files EXIT

        # pyproject.toml remains the canonical version source for uv_build.
        uv version "$new_version" --frozen
        sed -i -E "s/^__version__ = \"[0-9]+\.[0-9]+\.[0-9]+\"$/__version__ = \"$new_version\"/" mca/__init__.py
        uv lock
        _bump_check_versions \
            "$new_version" \
            "Version synchronization failed for $new_version."

        build_dir=$(mktemp -d)
        uv build --clear --no-sources --out-dir "$build_dir"
        if ! find "$build_dir" -maxdepth 1 -type f -name "*-$new_version-*" -print -quit | grep -q .; then
            echo "Built distributions do not contain version $new_version." >&2
            exit 1
        fi
        cleanup_build

        git add -- "${VERSION_BUMP_FILES[@]}"
        git commit --only -m "Bump version to $new_version" -- "${VERSION_BUMP_FILES[@]}"
        trap cleanup_build EXIT
        git tag --annotate "$new_tag" --message "Release $new_tag"
        trap - EXIT
    )

    echo "Version bumped to $new_version and tagged as $new_tag"

    if [[ "$push_release" == true ]]; then
        branch=$(git branch --show-current)
        if [[ -z "$branch" ]]; then
            echo "Cannot push from a detached HEAD." >&2
            return 1
        fi
        git push origin "$branch" "$new_tag"
    fi
}

# -----------------------------------------------------------------------------
function _help_ {
    printf "make.sh <task> [args]\n\nAvailable Tasks:\n"
    compgen -A function | while read -r name ; do
        [[ "$name" == "_"* ]] && continue
        echo && echo "  - $(echo "$name" | grep -v '^_')"
        echo "$(type "$name" | sed -nEe 's/^[[:space:]]*: ?"(.*)";/    \1/p')"
    done
}

# cd $MKWD
TIMEFORMAT=$'\nTask completed in %3lR'
time "${@:-_help_}"
exit 0
