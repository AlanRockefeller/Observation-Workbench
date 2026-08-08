#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./build-release.sh [--tag v1.0.0-build3] [--target origin/main] [--dry-run] [--skip-ruff]

Build Observation Workbench release binaries by creating and pushing a v* tag.
GitHub Actions builds the Windows, macOS (x64 + arm64), and Linux artifacts
and publishes them to the GitHub Release for that tag.

Defaults:
  --target origin/main
  --tag    next unused v<pyproject version>-buildN tag

Examples:
  ./build-release.sh
  ./build-release.sh --tag v1.0.1-build1
  ./build-release.sh --dry-run
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

note() {
  echo "==> $*"
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
cd "$repo_root"

tag=""
target_ref="origin/main"
dry_run=false
skip_ruff=false
temp_dir=""

cleanup() {
  if [ -n "$temp_dir" ] && [ -d "$temp_dir" ]; then
    rm -rf "$temp_dir"
  fi
}
trap cleanup EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      [ "$#" -ge 2 ] || die "--tag requires a value"
      tag="$2"
      shift 2
      ;;
    --target)
      [ "$#" -ge 2 ] || die "--target requires a value"
      target_ref="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --skip-ruff)
      skip_ruff=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

find_venv_python() {
  local candidate
  for candidate in \
    ".venv/Scripts/python.exe" \
    ".venv/bin/python" \
    "venv/Scripts/python.exe" \
    "venv/bin/python"
  do
    if [ -x "$candidate" ] || [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

show_target_file() {
  local path="$1"
  git show "${target_sha}:${path}" 2>/dev/null \
    || die "target commit does not contain required file: $path"
}

target_file_contains() {
  local path="$1"
  local needle="$2"
  show_target_file "$path" | grep -Fq "$needle"
}

if [ -n "$(git status --porcelain)" ]; then
  die "working tree has uncommitted changes; commit or stash before building a release"
fi

git remote get-url origin >/dev/null 2>&1 || die "remote 'origin' is not configured"

note "Fetching origin/main and tags"
git fetch origin +main:refs/remotes/origin/main --tags

git rev-parse --verify "$target_ref^{commit}" >/dev/null 2>&1 \
  || die "target ref does not resolve to a commit: $target_ref"
target_sha="$(git rev-parse "$target_ref^{commit}")"

version="$(show_target_file pyproject.toml | sed -n 's/^version = "\([^"]*\)"/\1/p' | head -n 1)"
[ -n "$version" ] || die "could not read project version from target pyproject.toml"

if [ -z "$tag" ]; then
  prefix="v${version}-build"
  max_build=0
  while IFS= read -r existing_tag; do
    case "$existing_tag" in
      ${prefix}[0-9]*)
        build_number="${existing_tag#$prefix}"
        case "$build_number" in
          ''|*[!0-9]*) continue ;;
        esac
        if [ "$build_number" -gt "$max_build" ]; then
          max_build="$build_number"
        fi
        ;;
    esac
  done < <(git tag --list "${prefix}*" | sort -u)
  tag="${prefix}$((max_build + 1))"
fi

case "$tag" in
  v*) ;;
  *) die "tag must start with 'v' so it triggers the build workflow: $tag" ;;
esac

git check-ref-format "refs/tags/$tag" || die "invalid git tag name: $tag"

if git rev-parse --verify "refs/tags/$tag" >/dev/null 2>&1; then
  die "local tag already exists: $tag"
fi

if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
  die "remote tag already exists: $tag"
fi

target_file_contains main.py 'from observation_workbench.ui.main_window import MainWindow' \
  || die "target main.py must import MainWindow from observation_workbench for PyInstaller"
target_file_contains packaging/observation_workbench.spec 'ROOT = Path(SPECPATH).parent' \
  || die "target packaging/observation_workbench.spec has an unexpected ROOT path"
target_file_contains packaging/observation_workbench.spec 'tomllib.load(f)["project"]["version"]' \
  || die "target packaging/observation_workbench.spec must derive bundle version from pyproject.toml"
target_file_contains .github/workflows/build-executables.yml 'gh release create' \
  || die "target build workflow is not configured to publish GitHub Release assets"

if [ "$skip_ruff" = false ]; then
  python_bin="$(find_venv_python)" || python_bin=""
  if [ -z "$python_bin" ]; then
    note "No project virtualenv Python found; skipping Ruff"
  elif ! "$python_bin" -m ruff --version >/dev/null 2>&1; then
    note "Ruff is not installed in $python_bin; skipping Ruff"
  else
    head_sha="$(git rev-parse HEAD^{commit})"
    if [ "$target_sha" = "$head_sha" ]; then
      ruff_dir="$repo_root"
    else
      temp_dir="$(mktemp -d)"
      git archive "$target_sha" | tar -x -C "$temp_dir"
      ruff_dir="$temp_dir"
    fi
    note "Running Ruff with $python_bin against $target_sha"
    (cd "$ruff_dir" && "$repo_root/$python_bin" -m ruff check observation_workbench/ main.py)
  fi
else
  note "Skipping Ruff"
fi

remote_url="$(git remote get-url origin)"
repo_slug="$(printf '%s\n' "$remote_url" \
  | sed -E 's#^git@github.com:##; s#^https://github.com/##; s#\.git$##')"
repo_url="https://github.com/${repo_slug}"

note "Release tag: $tag"
note "Target ref:  $target_ref"
note "Target SHA:  $target_sha"
note "Version:     $version"

if [ "$dry_run" = true ]; then
  note "Dry run complete; no tag was created or pushed"
  exit 0
fi

git tag -a "$tag" "$target_sha" -m "Build Observation Workbench $version ($tag)"
git push origin "refs/tags/$tag"

cat <<EOF

Pushed $tag.

GitHub Actions will build:
  - ObservationWorkbench-windows-x64.zip
  - ObservationWorkbench-macos-x64.zip
  - ObservationWorkbench-macos-arm64.zip
  - ObservationWorkbench-linux-x64.zip

Actions:
  ${repo_url}/actions/workflows/build-executables.yml

Release assets will appear here after the workflow completes:
  ${repo_url}/releases/tag/${tag}
EOF
