#!/usr/bin/env bash
# The single definition of "the work is OK". Agents must pass this before
# they finish, and finish-task.sh runs it again before merging.
#
# Each check only runs if the repo contains that kind of file, so a new,
# nearly empty repo still passes. Add your own checks at the bottom.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"
failed=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; failed=1; }
skip() { echo "  skip  $1"; }

echo "Running checks in $(pwd)"

# Files to check: everything git tracks, plus new files that aren't ignored
# yet, so a brand-new test counts before it's staged or committed.
project_files() { git ls-files --cached --others --exclude-standard -- "$@" | sort -u; }

# 1. Shell scripts must at least be valid bash syntax.
shell_files=$(project_files '*.sh')
if [[ -n "$shell_files" ]]; then
  bad=0
  for f in $shell_files; do bash -n "$f" || bad=1; done
  [[ $bad -eq 0 ]] && pass "shell syntax" || fail "shell syntax"
else
  skip "shell syntax (no .sh files)"
fi

# 2. Python tests, if there are any.
if [[ -n "$(project_files 'test_*.py' '*/test_*.py' '*_test.py' '*/*_test.py')" ]]; then
  if python3 -m pytest --version >/dev/null 2>&1; then
    # The suite is thousands of small tests (git, subprocesses), so one run takes ~9 minutes.
    # Every test builds its own temporary FLEET_HOME, so test FILES are independent: run them in
    # parallel (CHECK_JOBS, default 8), one pytest per file, and show the output of any that fail.
    # A file pytest cannot collect or run counts as a failure like any other.
    out_dir="$(mktemp -d)"
    export out_dir
    run_one() { python3 -m pytest -q -p no:cacheprovider "$1" > "$out_dir/$(echo "$1" | tr / _).txt" 2>&1 \
                  || echo "$1" >> "$out_dir/FAILED"; }
    export -f run_one
    project_files 'test_*.py' '*/test_*.py' '*_test.py' '*/*_test.py' \
      | xargs -P "${CHECK_JOBS:-8}" -I{} bash -c 'run_one {}'
    if [[ -f "$out_dir/FAILED" ]]; then
      while read -r f; do tail -n 40 "$out_dir/$(echo "$f" | tr / _).txt"; done < <(sort "$out_dir/FAILED")
      fail "python tests ($(wc -l < "$out_dir/FAILED") file(s) failed)"
    else
      echo "  $(cat "$out_dir"/*.txt | grep -Eo '[0-9]+ passed' | awk '{n+=$1} END{print n}') tests passed"
      pass "python tests"
    fi
    rm -rf "$out_dir"
  else
    fail "python tests (pytest is not installed: pip install pytest)"
  fi
else
  skip "python tests (none found)"
fi

# 3. Kubernetes manifests in k8s/ must be valid.
if [[ -d k8s ]] && [[ -n "$(find k8s -name '*.yaml' -o -name '*.yml' | head -1)" ]]; then
  if command -v kubeconform >/dev/null 2>&1; then
    # -strict: reject unknown fields (catches typos like "replicaz").
    # -ignore-missing-schemas: skip custom resources (e.g. ArgoCD Applications)
    #   that kubeconform has no schema for, instead of failing on them.
    #   NOTE: this means Application/ApplicationSet CRs are NOT validated
    #   (only built-in Kubernetes kinds are). Add CRD schemas via
    #   KUBECONFORM_SCHEMA_LOCATION to cover them.
    # -cache: keep downloaded schemas so later runs work offline and faster.
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/kubeconform"
    mkdir -p "$cache"
    kc_args=(-strict -summary -ignore-missing-schemas -cache "$cache")
    # Optional: point at a local copy of the schemas instead of downloading.
    if [[ -n "${KUBECONFORM_SCHEMA_LOCATION:-}" ]]; then
      kc_args+=(-schema-location "$KUBECONFORM_SCHEMA_LOCATION")
    fi
    kubeconform "${kc_args[@]}" k8s/ \
      && pass "kubernetes manifests (kubeconform)" || fail "kubernetes manifests (kubeconform)"
  else
    fail "kubernetes manifests (kubeconform is not installed)"
  fi
else
  skip "kubernetes manifests (no k8s/ folder)"
fi

# 4. No obvious secrets in the project's files (new, unstaged files included).
if git grep --untracked -nIE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}' \
     -- ':!scripts/check.sh' ':!.claude/hooks/*' >/dev/null 2>&1; then
  fail "secret scan (something that looks like a key or token is in the repo)"
else
  pass "secret scan"
fi

# 5. gitleaks: a dedicated scanner that knows hundreds of real key formats.
#    It checks two things:
#    a) commits: in a task worktree, only the commits on this branch; on main,
#       the whole history.
#    b) files changed but not committed yet (new, edited or staged).
#    --redact keeps the secret itself out of the output.
if command -v gitleaks >/dev/null 2>&1; then
  gl_base="main"
  git rev-parse --verify --quiet main >/dev/null || gl_base="master"
  if [[ "$(git rev-parse --absolute-git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
    gl_range=(--log-opts="$gl_base..HEAD")   # task worktree: this branch only
  else
    gl_range=()                              # main checkout: full history
  fi
  gl_ok=1
  if git rev-parse --verify --quiet HEAD >/dev/null; then
    gitleaks git --no-banner --redact --verbose --log-level warn "${gl_range[@]}" . || gl_ok=0
  fi
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    gitleaks dir --no-banner --redact --verbose --log-level warn "$f" || gl_ok=0
  done < <({ git ls-files --modified --others --exclude-standard; git diff --cached --name-only --diff-filter=d; } | sort -u)
  [[ $gl_ok -eq 1 ]] && pass "gitleaks" || fail "gitleaks (a likely secret was found, see above)"
else
  fail "gitleaks (not installed: see SETUP-GUIDE.md, 'Install gitleaks')"
fi

# 6. CHANGELOG.md is written only by the release tagger, on the base branch.
#    A task branch that edits it collides with the next Release commit (both
#    insert right after [Unreleased]) and breaks every stacked branch. Compare
#    with each merge-base, so a branch that merely merged main in passes, and
#    a criss-cross with several merge-bases passes if it matches any of them.
cl_branch="$(git branch --show-current)"
if [[ "$cl_branch" == task/* ]]; then
  cl_base="main"
  git rev-parse --verify --quiet main >/dev/null || cl_base="master"
  cl_bases="$(git merge-base --all HEAD "$cl_base" 2>/dev/null || true)"
  if [[ -z "$cl_bases" ]]; then
    skip "changelog guard (no merge-base with $cl_base)"
  else
    # Both the committed copy and the working copy must match, so an
    # uncommitted restore cannot hide an edit the branch still carries.
    cl_work="$(git hash-object CHANGELOG.md 2>/dev/null || echo none)"
    cl_head="$(git rev-parse -q --verify "HEAD:CHANGELOG.md" 2>/dev/null || echo none)"
    cl_same=0
    # A restoring commit (copy of the base tip file) counts as untouched too.
    for mb in $cl_bases "$cl_base"; do
      cl_then="$(git rev-parse -q --verify "$mb:CHANGELOG.md" 2>/dev/null || echo none)"
      [[ "$cl_then" == "$cl_work" && "$cl_then" == "$cl_head" ]] && cl_same=1
    done
    if [[ $cl_same -eq 1 ]]; then
      pass "changelog guard (CHANGELOG.md untouched)"
    else
      echo "  CHANGELOG.md differs from $cl_base. Task branches must not edit it: the entry"
      echo "  is written at ship from the ticket title. Restore it with:"
      echo "    git checkout \$(git merge-base HEAD $cl_base) -- CHANGELOG.md"
      echo "  (or delete the file if $cl_base does not have one)"
      fail "changelog guard (CHANGELOG.md is written only at ship)"
    fi
  fi
else
  skip "changelog guard (not a task/* branch)"
fi

# 7. Add project-specific checks here, e.g.:
#    ruff check . && pass "lint" || fail "lint"

echo
if [[ $failed -ne 0 ]]; then
  echo "CHECKS FAILED"
  exit 1
fi
echo "ALL CHECKS PASSED"
