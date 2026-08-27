# JRL Messages 3.1.0: what to do on the Mac

This is the complete Mac-side upgrade for 3.1.0. Its goal is to prevent and automatically repair the "texts exist on the iPhone but not on Windows" state that Wake Mac usually cures. Fifteen minutes, one time.

Background, so the steps make sense. The chain is iPhone → Apple's cloud → Messages on the Mac → BlueBubbles → Tailscale → your PC. When Wake Mac releases a missing text, the delay is upstream of Windows: Messages had not yet exposed that row to BlueBubbles. App Nap or an idle relay can contribute to that state. The checks below reduce it, and the Windows agent can perform a verified repair automatically when ordinary checks find nothing.

## Step 1: copy the mac folder to the Mac

Copy the `mac` folder from this ZIP to the Mac (AirDrop, USB stick, or iCloud Drive all work). Anywhere is fine, for example the Desktop.

## Step 2: run the installer

Open Terminal (Cmd+Space, type Terminal), then:

```
cd ~/Desktop/mac
bash install-jrl-keepalive.sh --with-power
```

Two prompts will appear, both expected:

1. Your password in Terminal. That is `sudo` applying the power settings (system sleep off, display sleep allowed, wake for network access, and auto restart after a power failure). A repeating pre-wake is added only with the explicit `--daily-restart` option.
2. A macOS dialog asking whether Terminal or bash may control Messages. Click Allow. The installer does not stop at that foreground check: it kickstarts the installed launchd job and requires a fresh success marker from the unattended context. If you click Don't Allow, use System Settings → Privacy & Security → Automation, enable Messages, then rerun the installer.

With the safe default, verification does not restart Messages. If you opt into `--daily-restart`, the installer performs one controlled restart test, so run that variant only when you are not sending.

If you would rather review the power commands yourself, run it without `--with-power` and the installer prints them instead of applying them.

## What is now installed

1. Every 2 minutes (launchd job `com.jrl.messages.keepalive`): if Messages or the main BlueBubbles process is not running it is relaunched, then a small AppleScript liveness probe must succeed. The probe proves Messages is reachable; it does not falsely promise that Apple has released every cloud-held row.
2. The local daily restart is off by default. The Windows agent's Auto Wake is safer because it fences queued and in-flight sends before restarting Messages. On a dedicated relay where you explicitly accept that a Mac-only schedule cannot see the Windows outbox, opt in at 4:15 with `bash install-jrl-keepalive.sh --daily-restart --with-power`, or choose a quiet time with `--daily-restart --restart-hour 5 --restart-minute 30`. The optional job captures the old PID, waits for it to disappear, requires a different new PID, and verifies the new process before recording success. Reinstalling with `--no-daily-restart` unloads an existing restart job.
3. The installer requests an App Nap opt-out for Messages and BlueBubbles where macOS permits it. Current macOS versions may protect the Messages `com.apple.MobileSMS` preference domain; that now produces a warning and does not abort the essential watchdog, power, or Automation setup.
4. With `--with-power`: the Mac never system-sleeps (the display still sleeps, which is fine and saves the panel), wakes for network access, and restarts itself after a power failure. A repeating pre-wake is added only when you explicitly opt into the local daily restart.

Everything is user-level, logged, and reversible with `bash uninstall-jrl-keepalive.sh`.

## Step 3: check the BlueBubbles settings once

Open the BlueBubbles server app on the Mac and confirm two switches:

1. "Start on boot" (or Login Item) is ON, so a reboot brings the whole chain back with no keyboard attached.
2. "Keep macOS Awake" is ON. This is BlueBubbles' own supported power control and complements the launchd watchdog.
3. Leave everything else as you had it. Private API stays off unless you want group management from Windows.

## Step 4: verify it works

On the Mac:

```
bash "$HOME/Library/Application Support/jrl-keepalive/jrl-keepalive.sh" --status
```

You should see Messages running: yes, BlueBubbles running: yes, the keepalive launchd job listed, and a Last verified tick. The restart job and Last verified restart appear only if you opted into the local daily restart or ran one manually. Any unresolved tick or restart error is shown separately and is not erased by success in the other action. The log lives at `~/Library/Logs/jrl-keepalive.log`.

Then the end-to-end test from Windows: open JRL Messages, Settings → Verify line. A sub-10-second round trip means the whole chain is healthy.

## How this pairs with the Windows agent

The Mac side checks relay liveness every 2 minutes. The Windows agent watches from the other end: after a configurable quiet period with no incoming messages it restarts Messages remotely, exactly like the button. A durable maintenance lease blocks Wake when a send is active and holds any newly composed send until the restart is safe. A held-back text can be released by automatic Wake Mac or the manual button; the Mac liveness check prevents dead relay processes from remaining unnoticed. A local nightly restart is available only as the explicit, less-coordinated opt-in described above.

## Clamshell and travel rules (unchanged from before)

Lid closed on power with an external display: fully supported. Lid closed with no display: macOS forces sleep unless you run `sudo pmset -a disablesleep 1`, and a Mac in that state must never go into a bag. When the Mac is asleep or off, nothing is lost: your iPhone keeps every message, and the moment the Mac is back the agent backfills Windows automatically.

## Undo everything

```
cd ~/Desktop/mac
bash uninstall-jrl-keepalive.sh
sudo pmset -a sleep 1 displaysleep 10
sudo pmset repeat cancel
```
