# Agent Rules Kit: step-by-step setup guide

This kit copies the most useful ideas from your coworker's Herdforge system
into something small enough to understand line by line. It runs **two
agents from two companies**: Claude (Claude Code) and OpenAI's Codex, which
comes with your ChatGPT Plus plan.

1. Every task gets its own isolated workspace, so agents can't trample each
   other or your main copy of the code.
2. An agent isn't "done" until an automated check passes.
3. **The other company's agent reviews every change before it's merged.**
   Codex reviews Claude's work, and Claude reviews Codex's. Either agent can
   also ask the other for a second opinion mid-task.
4. Only **you** merge to `main`, and `main` is what gets deployed (GitOps).

The rules are written down in `CLAUDE.md` (Claude reads it) and `AGENTS.md`
(Codex reads it, and it points back to `CLAUDE.md`). They're **enforced**
by small Python scripts called hooks. Both Claude Code and Codex run the
same hook scripts, so neither agent can forget or skip the rules.

Read Part 1 once. Do Part 2 once. Part 3 is the loop you'll repeat every day.

---

## Part 1: The ideas, in plain English

### Words you'll see

| Word | What it means |
|---|---|
| **Repository (repo)** | A project folder that git tracks. Git remembers every saved version of every file. |
| **Commit** | One saved snapshot of the project, with a message saying what changed. |
| **Branch** | A separate line of commits. You can experiment on a branch without touching `main`. |
| **`main`** | The official branch. In GitOps, whatever is on `main` is what runs on your cluster. |
| **Merge** | Bringing a branch's commits into another branch, usually into `main`. |
| **Worktree** | A second (third, fourth...) folder for the same repo, each on its own branch. This is how several agents work at once without colliding. |
| **Hook** | A script the agent's tool (Claude Code or Codex) runs automatically at certain moments, such as right before a shell command or right when the agent says it's finished. A hook can **block** the action. |
| **`CLAUDE.md`** | A file Claude reads at the start of every session. It's the "house rules" document for every agent in the repo. |
| **`AGENTS.md`** | The file Codex reads at the start of every session. In this kit it points Codex to `CLAUDE.md`, so both agents follow one rulebook. |
| **Codex** | OpenAI's coding agent for the terminal, the counterpart to Claude Code. Included with ChatGPT plans. |
| **GitOps** | You never change the running system by hand. You change files in git; a tool (ArgoCD) notices and makes the cluster match. |
| **kubeconform** | A tool that checks Kubernetes YAML files for mistakes before they reach the cluster. |

### How one task flows

```
 you: scripts/new-task.sh sar-alerts
        │
        ▼
 .worktrees/sar-alerts/   ← an isolated copy on branch task/sar-alerts
        │
 you: cd there and start `claude` (or `codex`), describe the task
        │
        ▼
 The agent edits and commits ── hooks block anything against the rules
        │
 The agent says "done" ── Stop hook runs scripts/check.sh
        │                  fail → the agent is sent back to fix it
        ▼
 The agent runs scripts/review.sh ── the other company's agent reviews read-only
        │
        ▼
 you: read the summary and review, then run scripts/finish-task.sh sar-alerts
        │   (re-checks, asks you to confirm, merges, pushes, cleans up)
        ▼
 main on GitHub ── ArgoCD sees the change and deploys it
```

### Why each rule exists

- **Worktrees**: if two agents edit the same folder at once, they overwrite
  each other. Separate folders make that impossible, and your main copy stays
  clean no matter what an agent does.
- **Checks before "done"**: agents sometimes claim success when something is
  broken. A script that must pass removes the guesswork.
- **A second reviewer**: the model that wrote the code is the worst judge of
  it. A fresh session on a different model catches things the author misses.
- **Only you merge**: merging to `main` means deploying. That decision stays
  with a human.
- **No direct `kubectl apply`**: if an agent changes the cluster by hand, git
  no longer describes reality, and ArgoCD will undo or fight the change.

---

## Part 2: One-time setup on the tower

These steps assume a fresh Ubuntu Server tower that you reach over SSH.
Every command goes in the tower's terminal.

### Step 1. Install the basic tools

```bash
sudo apt update
sudo apt install -y git python3 python3-pytest curl unzip
```

- `sudo` runs a command as administrator. It asks for your password.
- `apt` is Ubuntu's app store for the command line.
- `git` tracks versions; `python3` runs the hooks; `python3-pytest` runs
  Python tests; `curl` downloads files; `unzip` opens the kit.

Tell git who you are. It stamps this on every commit:

```bash
git config --global user.name "Chris"
git config --global user.email "you@example.com"
```

Use whatever email you use for your personal GitHub account.

### Step 2. Install kubeconform

```bash
curl -sSL -o /tmp/kc.tgz https://github.com/yannh/kubeconform/releases/latest/download/kubeconform-linux-amd64.tar.gz
tar -xzf /tmp/kc.tgz -C /tmp kubeconform
sudo mv /tmp/kubeconform /usr/local/bin/
kubeconform -v
```

1. Downloads the latest release from GitHub into `/tmp` (a scratch folder).
2. Unpacks just the `kubeconform` program from the archive.
3. Moves it into `/usr/local/bin`, where Linux looks for programs.
4. Prints the version, which proves it works.

If your tower has an ARM processor, replace `amd64` with `arm64`.

The first time kubeconform checks a file, it downloads the official
Kubernetes schemas from the internet and saves them in `~/.cache/kubeconform`.
After that it works offline.

### Step 2b. Install gitleaks

gitleaks scans for leaked passwords, API keys and tokens. It recognizes
hundreds of real key formats (AWS, Slack, GitHub, Okta and more).
`scripts/check.sh` fails if it isn't installed.

```bash
V=8.30.1
cd /tmp
curl -sSLO https://github.com/gitleaks/gitleaks/releases/download/v$V/gitleaks_${V}_linux_x64.tar.gz
curl -sSLO https://github.com/gitleaks/gitleaks/releases/download/v$V/gitleaks_${V}_checksums.txt
sha256sum -c --ignore-missing gitleaks_${V}_checksums.txt
tar -xzf gitleaks_${V}_linux_x64.tar.gz gitleaks
sudo mv gitleaks /usr/local/bin/
cd ~
gitleaks version
```

1. Downloads the program and its official list of checksums.
2. `sha256sum -c` confirms the download matches the checksum. It must say
   `OK`. If it says `FAILED`, stop: the file is damaged or not genuine.
3. Unpacks the program and moves it to `/usr/local/bin`.
4. Prints the version (`8.30.1`), which proves it works.

For an ARM machine, replace `linux_x64` with `linux_arm64`. To use a newer
version later, change `V=` to the version shown on the gitleaks releases page.

In a task worktree, check.sh scans only that task's commits plus any files
not committed yet. On main, it scans the whole history.

### Step 3. Install Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Then **open a new terminal** (or log out of SSH and back in) and run:

```bash
claude --version
```

A version number means it worked. The first time you run `claude`, it walks
you through logging in with your personal account.

### Step 3b. Install Codex (your second agent)

Codex is OpenAI's coding agent for the terminal. It's included with every
ChatGPT plan, so your Plus subscription covers it at no extra cost. The kit
uses it three ways: as the reviewer of Claude's work, as a second opinion
Claude can ask mid-task, and as a worker you can hand tasks to directly.

Install it with OpenAI's official installer:

```bash
sudo apt install -y bubblewrap
curl -fsSL https://chatgpt.com/codex/install.sh | sh
hash -r
codex --version
```

1. `bubblewrap` is what Codex uses to sandbox the commands it runs, so a
   read-only review really is read-only.
2. The installer needs no `sudo`. It checks the download's checksum,
   installs the complete Codex package under `~/.codex/packages`, and puts
   the `codex` command in `~/.local/bin`.
3. `hash -r` makes the terminal find the new command; `codex --version`
   proves it works.

Don't install the bare `codex` program from GitHub's release page instead.
Since version 0.157, Codex needs its complete package to start, and the
bare program fails with "this CLI has no complete local package".

Then sign in once with your ChatGPT account:

```bash
codex login
```

It prints a link. Open it on any device, sign in to ChatGPT, and approve.
Check it worked with `codex login status`.

If you skip this step, the kit still works: reviews fall back to a second
Claude model.

### Step 4. Make a repository for a personal project

Pick one of your own projects. The SAR drone alert agent is a good first
candidate. If it doesn't exist yet:

```bash
mkdir -p ~/projects/sar-drone-agent
cd ~/projects/sar-drone-agent
git init -b main
```

- `mkdir -p` makes the folder (and any missing parent folders).
- `cd` moves you into it.
- `git init -b main` turns the folder into a git repo whose first branch is
  `main`.

If the project already exists as a git repo, just `cd` into it.

### Step 5. Copy the kit into the repo

Get the zip onto the tower. From the computer where you downloaded it:

```bash
scp agent-rules-kit.zip chris@TOWER-IP:~/
```

`scp` copies a file over SSH. Replace `chris` with your tower username and
`TOWER-IP` with its address (or its Tailscale name).

Then, on the tower, inside your project folder:

```bash
unzip ~/agent-rules-kit.zip -d /tmp/kit
cp -rn /tmp/kit/agent-rules-kit/. .
chmod +x scripts/*.sh .claude/hooks/*.py
```

- `unzip ... -d /tmp/kit` unpacks into a scratch folder.
- `cp -rn` copies everything into your project. `-r` means "folders too";
  `-n` means "never overwrite a file that's already here", so an existing
  `.gitignore` or `CLAUDE.md` is kept. If you already had one of those,
  open both copies and merge them by hand. Newer Ubuntu versions print
  "warning: behavior of -n is non-portable". That warning is harmless.
- `chmod +x` marks the scripts as runnable programs.

Check that the files arrived:

```bash
ls -la . .claude .claude/hooks scripts
```

You should see `CLAUDE.md`, `AGENTS.md`, `.gitignore`, `.claude/settings.json`,
an `ask-codex` folder in `.claude/skills`, four Python files in
`.claude/hooks`, `hooks.json` and `config.toml` in `.codex`, and six `.sh`
files in `scripts`. Check the new folders with:

```bash
ls -la .claude/skills/ask-codex .codex
```

### Step 6. Run the checks once by hand

```bash
bash scripts/check.sh
```

You should see a few `PASS` or `skip` lines and `ALL CHECKS PASSED` at the
end. `skip` just means "this repo has no files of that kind yet."

### Step 7. Commit the kit to `main`

```bash
git add -A
git commit -m "Add agent rules kit"
```

- `git add -A` tells git "include every new and changed file in the next
  snapshot."
- `git commit -m "..."` saves the snapshot with a message.

**This step matters.** A worktree is a copy of what's committed on `main`.
If the kit isn't committed, new task folders won't contain the hooks, and
nothing will be enforced there.

### Step 8. (Recommended) Put the repo on GitHub

ArgoCD watches a git server, and GitHub is the easiest one.

1. On github.com, create a new **private** repository with the same name.
   Don't add a README; your repo already has files.
2. Connect and push:

```bash
git remote add origin git@github.com:YOUR-USERNAME/sar-drone-agent.git
git push -u origin main
```

- `git remote add origin ...` records GitHub as the place called `origin`.
- `git push -u origin main` uploads `main` and remembers the pairing.

If the push asks for credentials or fails with "permission denied", you
need an SSH key on GitHub. Ask me and I'll walk you through that step.

Then, in the GitHub repo's **Settings → Branches**, add a rule for `main`
that blocks force-pushes and deletion. That's a second lock behind the
hooks, enforced by GitHub itself.

### Step 9. Prove the hooks work (2 minutes)

Still in the main project folder, start Claude:

```bash
claude
```

The first time, Claude Code asks whether you **trust this folder**. Say yes.
Project hooks only run in folders you've trusted.

Type `/hooks` and press Enter. You should see `PreToolUse` with 2 hooks and
`Stop` with 1 hook, all from **Project Settings**. Press Escape to close it.

Now ask Claude:

> Create a file called hello.txt that says hi.

You should see it **blocked** with a message saying you're in the main
checkout and should use `scripts/new-task.sh`. That's the rule working.
Type `/exit` to quit Claude.

If Claude created the file anyway, the hooks aren't running. See
"Troubleshooting" below.

Now the same test for Codex, if you installed it. Still in the main folder:

```bash
codex
```

1. Codex asks whether you **trust this project**. Say yes. Codex only reads
   the project's `.codex/` settings and hooks after you trust it.
2. Type `/hooks` and press Enter. Codex lists the project's hooks as new and
   asks you to review them. Trust all three. (Codex asks again whenever a
   hook file changes, which is a nice safety feature.)
3. Ask Codex: *Create a file called hello.txt that says hi.*

It should be **blocked** with the same main-checkout message Claude got.
Type `/exit` to quit.

---

## Part 3: The daily loop

### 1. Start a task

From the main project folder:

```bash
scripts/new-task.sh weather-check
```

This creates the folder `.worktrees/weather-check/` on a new branch
`task/weather-check`, starting from the latest `main`. Task names use
lowercase letters, numbers and dashes.

### 2. Put Claude to work in that folder

```bash
cd .worktrees/weather-check
claude
```

Describe the task the way you'd write a good ticket: what you want, why, and
how you'll know it works. For example:

> Add a Python function that fetches the NWS forecast for a lat/lon and
> returns wind speed in mph. Add a pytest test that uses a saved sample
> response instead of the live API. When you're done, run scripts/review.sh.

While Claude works:

- Commands that break the rules are blocked, and Claude sees why.
- When Claude says it's finished, the **Stop hook** runs `scripts/check.sh`.
  If anything fails, or work is left uncommitted, Claude is sent back to fix
  it. After three failed tries in a row it stops and shows **you** a warning
  instead of looping forever.

**Or give the task to Codex instead.** Same folder, same rules, same hooks:

```bash
cd .worktrees/weather-check
codex
```

Codex can only edit files inside that task folder (its sandbox enforces
that on top of the hooks). A good way to use your Plus plan is to split
tasks: Claude on one worktree, Codex on another, each reviewed by the other.

### 2b. Second opinions mid-task

Either agent can consult the other without handing over the task:

- Tell Claude **"check with Codex"**, or it will ask on its own for hard
  design choices and stubborn bugs. Claude uses its built-in `ask-codex`
  skill, which runs `scripts/ask-codex.sh`.
- Codex uses `scripts/ask-claude.sh` (instructions in `AGENTS.md`). This one
  needs network access for Codex; see `.codex/config.toml`.
- You can run either script yourself, from anywhere in the repo:

```bash
scripts/ask-codex.sh "Is the retry logic in weather.py safe for the NWS API?"
git diff main...HEAD | scripts/ask-claude.sh
```

The consulted agent is always read-only. It can look at the code but can't
change anything.

### 3. Get the second opinion

The agent runs `scripts/review.sh` (or you can, from the task folder). It
picks a reviewer from the **other company** automatically: Codex when
Claude did the work, Claude when Codex did. If you run it yourself, it uses
Codex if it's installed and signed in, otherwise Claude. The reviewer is
read-only and prints:

```
Reviewer: Codex
VERDICT: PASS or FAIL
MUST FIX: ...
SHOULD FIX: ...
SUMMARY: ...
```

It also saves the review in `.reviews/` inside the task folder. Claude should
fix every MUST FIX item, commit, and re-run the review.

To choose the reviewer yourself:

```bash
REVIEWER=codex scripts/review.sh
REVIEWER=claude REVIEW_MODEL=opus scripts/review.sh
REVIEWER=both scripts/review.sh      # Codex, then Claude (sonnet after a Codex FAIL, else opus); high risk needs both PASS
```

Each review is saved with the full id of the commit it covers. A review of an
older commit does not count once you commit again: review again.

### 4. You merge

When Claude reports it's done, **leave that folder** and go back to the main
checkout:

```bash
cd ~/projects/sar-drone-agent
scripts/finish-task.sh weather-check
```

The script:

1. Re-runs the checks in the task folder.
2. Works out the **risk level** (low, medium or high, from the files changed
   and the ticket; rules in `risk-rules.json`) and shows why. **Refuses to
   merge** unless the exact commit has a PASS review. High risk (Kubernetes,
   `.claude/`, the scripts, secrets/auth, dependency files...) needs PASS
   reviews from both Codex and Claude (`REVIEWER=both`).
3. Lists the commits and files that will change.
4. **Asks you to type `y`** before touching anything (for high risk you type
   the ticket id instead). Anything else cancels.
5. Merges into `main` and runs the checks again on the merged result.
6. Pushes `main` to GitHub, where ArgoCD picks it up, then deletes the task
   folder and branch.

Real emergency with no review? `scripts/finish-task.sh NAME --emergency "why"`
asks you to type `OVERRIDE`, then merges and records it in
`~/.fleet/override.log` and the merge commit. Use it rarely.

To make a ticket stricter by hand: `ticket risk ID --raise high` (levels only
go up).

If the checks fail after the merge, it stops *before* pushing, so nothing is
deployed, and prints the command to undo the merge.

### Running several tasks at once

Each task is its own folder, so you can have three agents on three tasks:
make three worktrees and open a terminal (or a herdr pane) in each. Merge
them one at a time with `finish-task.sh`. If two tasks touched the same
lines, git will say there's a conflict. Ask Claude (in the main checkout,
with the escape hatch below) or me to help resolve it.

---

## Optional: phone notifications

The ticket tools can push a short message to your phone when a review is ready,
an urgent ticket appears, or a ticket fails. It uses an ntfy server you run
yourself, reachable only over Tailscale. It is off until you create
`notify.json` next to `kaneo.json` in the fleet home (`chmod 600`). Installing
the server is a manual step for you; the full steps (ntfy bound to the
tailscale IP or `tailscale serve`, never `funnel`; `auth-default-access:
deny-all`; no upstream relay; Android battery exemption) and the file format
are in `docs/notify-spec.md`.

---

## What's in the kit, file by file

| File | What it does |
|---|---|
| `CLAUDE.md` | The house rules, in plain English. Claude reads it every session, and `AGENTS.md` sends Codex to it too. |
| `AGENTS.md` | Codex's instructions: follow `CLAUDE.md`, have Claude review your work, and how to ask Claude for a second opinion. |
| `.claude/settings.json` | Tells Claude Code which hook runs when. Commit this; it applies to everyone who uses the repo. |
| `.codex/hooks.json` | Tells Codex to run the same three hooks. |
| `.codex/config.toml` | Codex project settings: can only edit inside the task folder, and network access for its commands (off by default). |
| `.claude/skills/ask-codex/SKILL.md` | Teaches Claude when and how to ask Codex for a second opinion. |
| `.claude/hooks/guard_bash.py` | Runs before every shell command. Blocks git changes from the main checkout, pushes to `main`, force-pushes, direct cluster changes (`kubectl apply`, `helm upgrade`, `argocd app sync`...), broad `rm -rf`, writing `.env` files, running `finish-task.sh`, and starting an agent with its safety switched off. |
| `.claude/hooks/guard_edits.py` | Runs before every file edit, from either agent. Blocks edits in the main checkout, edits to `.env` files, and content that looks like a real key or token. |
| `.claude/hooks/check_before_stop.py` | Runs when an agent says it's done. Runs the checks and insists on a commit. |
| `.claude/hooks/_common.py` | Shared helper code for the three hooks. |
| `scripts/new-task.sh` | You: creates a task worktree. |
| `scripts/check.sh` | The definition of "working": shell syntax, Python tests, kubeconform on `k8s/`, a quick secret scan, and gitleaks. Add your own checks at the bottom. |
| `scripts/review.sh` | An agent or you: gets a read-only review from the other company's model. |
| `scripts/ask-codex.sh` | Claude or you: asks Codex a question, read-only. |
| `scripts/ask-claude.sh` | Codex or you: asks Claude a question, read-only. |
| `scripts/finish-task.sh` | You only: checks, confirms, merges, pushes, cleans up. |
| `.gitignore` | Keeps task folders, review notes, `.env` files and caches out of git. |

---

## Troubleshooting

**Claude wasn't blocked in Step 9.**
Run `/hooks` inside Claude. If nothing is listed, check that
`.claude/settings.json` exists in the folder where you started `claude`, and
that you answered yes to the trust prompt. Then run `claude doctor` for
settings errors.

**A hook blocked something you actually want to do.**
You can do it yourself in a normal terminal; hooks only affect the agents.
Or, to turn the rules off for one agent session, start it like this:

```bash
AGENT_RULES_OFF=1 claude
AGENT_RULES_OFF=1 codex
```

Only you can do this. An agent can't set that variable for its own hooks.

**`check.sh` says kubeconform failed downloading a schema.**
The tower needs internet access the first time. After one successful run,
the schemas are cached.

**`check.sh` says pytest isn't installed.**
`sudo apt install -y python3-pytest`.

**Codex reviews say "Codex isn't signed in".**
Run `codex login` once. Or use `REVIEWER=claude scripts/review.sh` for now.

**Codex wasn't blocked in Step 9.**
Type `/hooks` inside Codex. Hooks marked as new or changed need your review
before they run: trust them there. If no project hooks show up at all, you
probably didn't trust the project when Codex first asked; exit and start
`codex` in the main folder again.

**Codex says `ask-claude.sh` or the Claude review failed with a network error.**
Codex blocks internet access for its commands by default. To allow it, set
`network_access = true` in `.codex/config.toml`, commit that on `main`, and
start a new task.

**Claude keeps getting sent back by the Stop hook.**
Read the output it's shown (it's the end of `scripts/check.sh`). You can run
`bash scripts/check.sh` in the task folder yourself to see the same thing.

---

## Honest limits

- **Hooks are guardrails, not a vault.** They match command text with
  patterns. They reliably stop mistakes and bad habits, but a determined
  agent could word a command in a way the patterns don't catch. The GitHub
  branch rule from Step 8 and your own `finish-task.sh` review are the real
  final gates.
- **Don't run with `--dangerously-skip-permissions` yet.** Keep Claude Code's
  normal permission prompts on while you learn how it behaves. The hooks add
  to those prompts; they don't replace them.
- **Codex and Claude share your usage limits with everything else.** Every
  review or consult uses some of your ChatGPT plan's Codex allowance, and
  every Claude task uses some of your Claude plan. Watch both for the first
  few weeks.
- **Codex's network switch is all or nothing.** Turning on `network_access`
  so Codex can reach Claude also lets Codex's commands reach any website.
  Leave it off until you want Codex asking Claude for help.
- **`check.sh` only knows what you teach it.** If your project uses another
  language or linter, add a line for it at the bottom of the script.

---

## Next steps

1. Do one tiny task end to end (Part 3) so you've seen every step once.
2. Install k3s and ArgoCD, and point an ArgoCD Application at the `k8s/`
   folder of this repo on GitHub. From then on, `finish-task.sh` is a deploy.
3. Add herdr so you can watch several task agents from one screen.
