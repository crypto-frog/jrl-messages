# AGENT-INSTRUCTIONS: publish this folder to GitHub

You are an automation agent. Your task is to publish this folder as a professional public GitHub repository named **jrl-messages**, with Issues enabled, a GitHub Pages site served from `/docs`, a tagged v3.6.0 release with a downloadable zip, and one seed tracking issue. Follow the steps in order. **If any step fails, stop and show the human the exact command and output.** The human can supply screenshots when a step needs the web UI.

## Rules

1. Do not modify any application code (`app/`, `tests/`, `tools/`, `mac/`, `*.bat`, `*.py`, `requirements.txt`, `SETUP.md`, `MAC-SETUP.md`, `RELEASE-NOTES-*.txt`). Your only edits are the OWNER placeholder substitution in step 4.
2. Never commit `.venv/`, `__pycache__/`, databases, logs, or zip files. The `.gitignore` already handles this; do not weaken it.
3. Never store or echo credentials. Authentication happens through `gh auth login`, driven by the human.
4. Prefer `gh` (GitHub CLI) for everything. Where an API call is needed, the exact call is given below.
5. Report progress after each numbered step in one short line.

## Step 0: preflight

Run and verify:

```
git --version
gh --version
gh auth status
```

- If `git` or `gh` is missing, ask the human to install them (git-scm.com, cli.github.com) and pause.
- If `gh auth status` shows not logged in, ask the human to run `gh auth login` interactively (HTTPS, browser auth) and pause until it succeeds. The human may show you screenshots; confirm from them that auth completed.
- Record the authenticated username; call it `OWNER` from here on. Confirm with the human: repository will be created as `OWNER/jrl-messages`, public. If the human wants an organization or a different name, substitute it consistently everywhere below.

## Step 1: verify the folder

From the folder containing this file, confirm all of these exist:

```
README.md  LICENSE  CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  CHANGELOG.md
RELEASE-NOTES-3.5.3.txt  RELEASE-NOTES-3.6.0.txt
.gitignore  AGENT-INSTRUCTIONS.md  SETUP.md  MAC-SETUP.md  install.bat  requirements.txt
run.py  run_agent.py  agent_supervisor.pyw
app/constants.py  app/agent/core.py  app/ui/main_window.py  app/phone/link.py
tests/test_reliability.py  tests/test_phone_link.py  tests/test_desktop_upgrades.py
docs/_config.yml  docs/index.md  docs/architecture.md  docs/installation.md  docs/known-issues.md
.github/ISSUE_TEMPLATE/bug_report.yml  .github/ISSUE_TEMPLATE/pairing_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml  .github/ISSUE_TEMPLATE/config.yml
.github/PULL_REQUEST_TEMPLATE.md  .github/seed-issues/001-bluetooth-mirroring-tracking.md
```

Confirm `VERSION = "3.6.0"` appears in `app/constants.py`.

## Step 2: privacy gate (mandatory)

The tree was scrubbed before packaging. Re-verify; every command must return nothing:

```
grep -rn "58783""96301" .
grep -rniE "password\s*=\s*['\"][^'\"]{4,}" app
grep -rn "tail[0-9]*\.ts\.net" app
```

(Fictional example numbers like +15875550123 and +1555... are expected and fine; documentation mentions of the word Tailscale are fine.) If any command returns a hit, stop and show the human.

## Step 3: initialize git and commit

```
git init -b main
git add -A
git status
```

Inspect the status output: `.venv`, `__pycache__`, `*.zip`, and `*.db` must not appear. Then:

```
git commit -m "JRL Messages v3.6.0: initial public release

A reliability-first iMessage client for Windows (bring your own Mac).
Python 3.12 + PySide6 client, background sync agent, BlueBubbles +
Tailscale relay architecture, 234-test regression battery, full
release-note history preserved."
```

## Step 4: substitute the OWNER placeholder

Documentation links use the literal placeholder `OWNER`. Replace it with the actual account name in these five files only:

```
docs/index.md  docs/installation.md  docs/known-issues.md
.github/ISSUE_TEMPLATE/config.yml  .github/seed-issues/001-bluetooth-mirroring-tracking.md
```

On Linux/macOS: `sed -i 's/OWNER/<actual-owner>/g' <file>` for each. On Windows PowerShell: `(Get-Content <file>) -replace 'OWNER','<actual-owner>' | Set-Content <file>`.

Verify no placeholder remains: `grep -rn "OWNER/jrl-messages" docs .github` must return nothing. Commit:

```
git add -A
git commit -m "Docs: point links at the published repository"
```

## Step 5: create the repository and push

```
gh repo create jrl-messages --public --source=. --remote=origin --push \
  --description "A reliability-first iMessage client for Windows. Bring your own Mac. Python + PySide6, BlueBubbles + Tailscale relay."
```

Then set topics:

```
gh repo edit --add-topic imessage --add-topic windows --add-topic bluebubbles --add-topic tailscale --add-topic pyside6 --add-topic python --add-topic desktop-app --add-topic messaging
```

Issues are enabled by default on new repositories; verify with `gh repo view --json hasIssuesEnabled` and enable via `gh repo edit --enable-issues` if false.

## Step 6: enable GitHub Pages from /docs

```
gh api -X POST repos/OWNER/jrl-messages/pages -f "source[branch]=main" -f "source[path]=/docs"
```

(Substitute the real owner.) If the API call fails (a 409 means Pages already exists; that is success), fall back to the web UI: ask the human to open Settings, Pages, set Source to "Deploy from a branch", Branch `main`, folder `/docs`, and Save; screenshots from the human confirm it. Then poll until built:

```
gh api repos/OWNER/jrl-messages/pages --jq '.html_url + "  status: " + .status'
```

Record the Pages URL (it will look like `https://<owner>.github.io/jrl-messages/`). Set it as the repository homepage:

```
gh repo edit --homepage "<pages-url>"
```

## Step 7: tag and release v3.6.0

```
git tag -a v3.6.0 -m "JRL Messages 3.6.0"
git push origin v3.6.0
git archive --format=zip --prefix=jrl-messages/ -o /tmp/jrl-messages-v3.6.0.zip v3.6.0
```

Note the zip's exact byte size (`ls -l` or `Get-Item`), then:

```
gh release create v3.6.0 /tmp/jrl-messages-v3.6.0.zip \
  --title "JRL Messages 3.6.0" \
  --notes "First public release. See README for what this is, SETUP.md for installation, and RELEASE-NOTES-3.6.0.txt for this version's changes. Zip size: <EXACT BYTES> bytes. Extract to a fresh folder and run install.bat (requires Python 3.12; see the Installation page)."
```

Replace `<EXACT BYTES>` with the real number.

## Step 8: labels and the seed issue

Create labels (ignore "already exists" errors):

```
gh label create bluetooth -c 0052CC -d "The experimental ANCS notification mirroring stack"
gh label create field-report -c 5319E7 -d "Observation from a real machine"
gh label create tracking -c C2E0C6 -d "Index issue for a long-running effort"
gh label create help-wanted-hard -c D93F0B -d "Genuinely difficult; expertise sought"
```

Open the seed issue using the prepared body. The file `.github/seed-issues/001-bluetooth-mirroring-tracking.md` contains a `Title:` line and a `Labels:` line followed by `---` and the body. Use the title from that file, the body below the `---` separator, and labels `bluetooth,tracking,help-wanted-hard`:

```
gh issue create --title "Tracking: make iPhone notification mirroring over Bluetooth dependable" \
  --label "bluetooth,tracking,help-wanted-hard" \
  --body-file <a temp file containing only the part below the --- separator>
```

## Step 9: final verification

Confirm each item and report the result to the human as a checklist:

1. `gh repo view --web` opens; README renders with the architecture diagram intact.
2. The Issues tab shows the seed issue and, under New Issue, three templates plus the private security-report link.
3. The Pages site loads at the recorded URL; index, architecture, installation, and known-issues pages all render and interlink.
4. The v3.6.0 release exists with the zip attached; the stated byte size matches the asset.
5. `git status` is clean and `git log --oneline` shows the two commits plus the tag.

Then hand the human: the repository URL, the Pages URL, the release URL, and the seed issue URL.

## Done

Do not perform any further actions (no extra commits, no settings changes) beyond this list unless the human asks.
