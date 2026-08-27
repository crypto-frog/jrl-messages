# JRL Messages: Setup

A Windows app for your iPhone messages: full history, search, sending text and files, from anywhere. It works by talking to your Mac, which relays through the Messages app. The Mac must be powered on and awake (a dark display is fine, sleep is not).

The chain: iPhone, Apple's cloud, your Mac (Messages plus the BlueBubbles server), Tailscale, this app.

The app is two parts. A background agent starts at logon and keeps the connection, checking messages every 3 seconds, downloading, sending, self-healing, and waking the Mac automatically. The window is a fast viewer over what the agent has already collected. In 3.1, closing the window keeps it in the notification area by default, so alerts and sound continue while it is hidden or minimized.

## Part 1: iPhone (5 minutes)

1. Settings, your name at the top, iCloud, turn on Messages in iCloud.
2. Settings, Messages, Text Message Forwarding, enable your Mac. This is what carries green-bubble SMS.

## Part 2: Mac (30 to 45 minutes, one time)

Follow MAC-SETUP.md in this folder for the 3.1.0 keepalive installer, which detects dead relay processes, reduces idle-relay delays, and verifies that unattended Messages control works. The one-time basics remain:

1. Open Messages, sign in with your Apple ID, and in Messages Settings under iMessage enable Messages in iCloud. Let it finish syncing.
2. Download the BlueBubbles server from bluebubbles.app and install it.
   - Grant Full Disk Access when prompted. Required: it reads the Messages database.
   - Skip the Google or Firebase setup step. Not needed for this app.
   - Set a strong server password and write it down.
   - Leave Private API mode off.
   - Note the port, default 1234.
   - Turn on "Start on boot" and BlueBubbles' "Keep macOS Awake" setting.
3. Install Tailscale from tailscale.com and sign in. Note the Mac's tailnet name, something like jonathans-mac.tail1234.ts.net, or its 100.x.x.x address. Either works as the server address later.
4. Run the keepalive installer from MAC-SETUP.md: `bash install-jrl-keepalive.sh --with-power`. It requests an App Nap opt-out where macOS permits it, relaunches Messages or BlueBubbles if either dies, verifies Messages from launchd every 2 minutes, and applies the right power settings (system sleep off, display sleep fine). A protected App Nap preference produces a warning rather than aborting the essential installation. The local nightly restart is off by default because it cannot see the Windows outbox; the guarded Windows Auto Wake performs that repair safely instead.
5. Clamshell, meaning lid closed:
   - Lid closed while connected to power and an external display: fully supported. The Mac stays awake. This is the normal docked setup, and the app works with the Mac like this indefinitely.
   - Lid closed with no display attached: macOS forces sleep. To override, run in Terminal: sudo pmset -a disablesleep 1
     Warning: with that flag the Mac stays fully powered even closed. Never put it in a bag or backpack like this, it can overheat. Turn it off before travel with: sudo pmset -a disablesleep 0
   - Whenever the Mac is asleep, unplugged, or in transit, the app simply shows offline. Nothing is lost. Your iPhone keeps working, and the agent backfills everything the moment the Mac is back.
6. Recommended: System Settings, General, Sharing, turn on Screen Sharing. Over Tailscale you can then reach the Mac's screen from anywhere to restart things without being home.

## Part 3: Windows (15 minutes)

1. Install Python 3.12 from python.org. Use the real installer, not the Microsoft Store version.
2. Install Tailscale on this PC and sign into the same account as the Mac.
3. Put the jrl-messages folder somewhere permanent, for example C:\Tools\jrl-messages.
4. If you are upgrading from 3.0.0, restart Windows once first. That release used an incorrect 30-second stale-lock policy and may already have left more than one old background process alive. The one reboot clears those legacy copies; 3.1.0 uses indefinite singleton locks and an atomic sender claim so they cannot return.
5. Double-click install.bat once. It stops and waits for any prior agent, builds a private environment, installs the app's libraries, registers the background agent to start at every logon, starts it, and verifies that the running version matches this folder.
6. Start the app with JRL-Messages.bat. Pin it to the taskbar if you like.
7. On first run the connection window opens:
   - Server URL: http://your-mac-name.tail1234.ts.net:1234 using the name from Part 2 step 3.
   - Password: the BlueBubbles server password.
   - Click Test connection, then Save. The agent picks the settings up immediately.
8. Conversations appear within seconds. Full history is indexed in durable 100-row ROWID windows in the background. A large archive can take a while; the app is fully usable during it, and an interrupted window safely replays when the agent resumes. Historical messages stay quiet during this first baseline so an upgrade does not produce a flood of old notifications.

## The background agent

- It starts from the Startup folder at logon (no administrator rights needed), is watched by a tiny supervisor that restarts it if it ever crashes, and an hourly failsafe task relaunches the supervisor itself if possible. Opening the app window also restarts a stopped agent automatically.
- The window connects to it over a private per-user channel. If the footer ever says "Background agent offline", the window is already restarting it; nothing needs your attention.
- Collection continues with the window closed: history, read receipts, attachments prefetched on demand, unread state, and held-back-text recovery all continue around the clock. By default the close button hides the viewer to the tray rather than exiting it, so notifications and their explicit sound also continue. Use the tray menu's Quit command to exit the presenter, or turn close-to-tray off in Settings.
- Sending is crash-safe across both processes: a message you send is written durably first, so if the agent has not begun transmitting it, the message goes out when the channel returns. Only one worker can claim it. If a crash or timeout makes the remote outcome uncertain, it turns red and waits for you to verify before retrying; the app never risks an automatic double-text.
- Agent-Console.bat runs the agent with visible output for debugging. Stop-Agent.bat stops background collection until next logon. Uninstall-Agent.bat removes the logon registration entirely.

## Daily use

- Unread: an accent pill beside the search box shows "N new"; click it to jump to the newest unread conversation. Hiding a conversation marks it read; reactions (like a thumbs-up on your message) never count as unread. Right-click the list for Mark all as read.
- Every conversation row is two actions: the left side opens it, the right edge hides it. On a conversation with unread messages, hovering shows the full round Hide control, a crossed-out eye in your accent color that grows with your text-size setting and matches the labelled Hidden button below. On an already-read conversation the list stays calm on purpose: hovering raises only a soft accent shade along the right edge, deepening with a faint eye-off mark when your cursor is over the zone itself, just enough to say that a click there hides. Nothing is deleted anywhere; Undo appears for a few seconds. Use the Hidden button in the bottom bar (or right-click the list) to open Hidden conversations, where Restore or Restore all brings threads back. Any sign of life also restores a hidden conversation automatically, even while the window is closed: a new incoming message however delayed, a message you send into it (from this app or your iPhone), or opening it through New. Ctrl+H hides the current conversation; the footer flashes Sent when a message goes out.
- Groups: the people icon in a group's header shows every member. Click a conversation with Ctrl+Tab and Ctrl+Shift+Tab to cycle, right-click a conversation for mark-as-read, refresh, or copy address.
- Photos: thumbnails honor the phone's orientation (portrait shots stand upright) and render with rounded corners. Click a photo for the built-in viewer with Copy, Save As, and Open; right-click a photo for the same actions in place. Drag files from Explorer onto a conversation to attach them.
- Emoji: the smiley button in the composer opens the picker. Type in its search box (thumbs, heart, coffee) and the matching emoji appear as buttons; click to insert at your cursor, click several to insert several, Enter takes the first match. A Recent row remembers what you use. Emoji are plain Unicode, so iPhones and Androids each render their own artwork; the set is curated to characters that display on effectively every phone in service. Country flags are excluded because Windows shows them as letter pairs; they can be added on request. A message that is only one to three emoji renders large in the conversation.
- New message: the accent New button with its filled pencil at the top left, or Ctrl+N. Pick several people to address a group; an existing group with exactly those members opens directly. Search your contacts or type any number or email. Existing conversations open instantly; new people get a first message and a service choice (iMessage or SMS). A new iMessage thread requires the recipient to be reachable on iMessage; if it fails red, retry as SMS.
- Bubbles size themselves. They use a comfortable share of the conversation pane and reflow live as you resize the window or drag the divider, with a readability cap so lines never get too long on a wide monitor. There is nothing to configure.

- The search box finds any message ever. Click a result to jump to it in context.
- Enter sends. Shift+Enter starts a new line.
- Attach files with the plus button, by dragging them onto the conversation, or Ctrl+V for a copied screenshot. Any file type.
- A failed send turns red and says Click to retry. It never resends on its own, so you cannot double-text a client by accident.
- Images show inline. Other files open in their normal apps when clicked.

## Away from home

Same app, same address, from the courthouse or another country, as long as Tailscale is signed in on the laptop. The iPhone does not need to be anywhere near the Mac. One dependency: green-bubble SMS physically routes through your iPhone, so SMS needs the phone powered on with service somewhere in the world. iMessage does not care.

## Settings

The gear at the bottom left opens Settings, organized into three tabs since 3.2.0. Connection holds the server address, password, connection test, and Auto Wake Mac. Alerts holds the Popups and Sound master switches, Self-texts, the alert style with Test alert and Verify line, code actions, and the bell (the in-app notification center). Look holds the text size, the tint suite, help tips, and the close button behavior. The tint suite offers sixteen named colors, each shown as a real color patch fading into its message-bubble shade; click any patch and the whole app previews it live behind the dialog, Save keeps it, Cancel puts everything back. Help tips default to showing each stable tip only twice; choose Always or Off at any time. Tips are suppressed while you click or select text.

Keyboard: Ctrl+F jumps to search, Escape clears it, Enter opens the selected conversation, right-clicking a message copies it, Ctrl+N starts a new message, Ctrl+B opens the notification center, Ctrl+L opens the Activity panel, and Ctrl+, opens Settings.

## The bell: in-app notification center

The bell beside the gear opens a quiet, theme-matched feed of everything the app has alerted on: every message alert, verification code, Mac wake, line repair, connection loss and recovery, and test alert. Entries show who, what, and how long ago, in your tint and at your text size. The unseen count is drawn on the bell itself and clears when you open the panel. Click an entry to jump to its conversation, click the small eye on any entry to hide just that one, or Clear all to empty the list. Messages that arrived while no window was running appear here with their true arrival time, so after a day away the bell tells you exactly what happened. The whole center can be switched off under Alerts in Settings; single entries can always be hidden from the list itself.

Since 3.3.0 the bell can also carry your iPhone's own app notifications, mirrored directly over Bluetooth; see the next section.

## iPhone notifications over Bluetooth (experimental)

Apple never passes general iPhone app notifications (banking, social, calendars, and so on) through the Mac; it only hands them to Bluetooth devices paired directly to the phone, the way a smartwatch receives them. Since 3.3.0 this app can be that device itself, so nothing else needs to run. When enabled, new iPhone notifications appear as normal popups marked "· iPhone", ring the same sound, and land in the bell, all obeying the Popups and Sound switches. Texts are never mirrored, on purpose: they already arrive through the Mac with full history and richer popups, so you are never alerted twice for one message.

Setting it up, once. Since 3.5.0 this app never creates or removes Bluetooth pairings itself; pairing an iPhone correctly is Microsoft Phone Link's home turf, because its QR code starts Microsoft's own Link to Windows app on the phone, which pairs from inside iOS with the correct prompts and pages every time. This app then attaches to that pairing (Bluetooth trusts belong to Windows, not to the app that made them) and takes over the presentation: your popups, your bell, your tint, your sounds.

1. In this app: Settings → Alerts → turn on the iPhone switch → Choose iPhone… → press "Phone Link pairing…". Microsoft's QR dialog opens; scan the code with the iPhone camera, allow Link to Windows, and finish its steps. Confirm a notification appears in Phone Link once (send yourself an email); that proves the pairing AND the phone's notification permission end to end.
2. Back in the picker, press Connect my iPhone. It finds the phone by proof, attaches to the pairing, runs a live round-trip test, and shows the timing. Press Save. Done permanently.
3. Optional tidiness: inside Phone Link's settings you can turn off its own notification banners so arrivals show only through this app; the pairing it created stays either way. Machines without Phone Link can use the advanced direct pairing the wizard offers after a refusal, but the Phone Link route is the reliable one.

Plain truths about this feature. It works over Bluetooth, so it works when the phone is within roughly a room's distance of the PC, which is exactly the courtroom-laptop-and-phone-in-pocket situation; it is not the long-distance path (your messages still travel the world through the Mac). It is marked experimental because Bluetooth adapters, drivers, and iOS versions vary; the link reports everything it does to the Activity panel and the bell ("iPhone link connected", "iPhone link lost... retrying"), reconnects itself with patient backoff, and can be switched off at any time without touching anything else in the app. The Mute apps box silences noisy apps by name. If it ever misbehaves on your hardware, run `python tools\phone_link_probe.py` from the install folder and send the output; it shows exactly which step your adapter or phone objected to.

## Reliability

The agent listens for pushed messages and, independently, verifies the Mac's durable message ROWIDs every 3 seconds. Numeric windows are read twice when a response is suspiciously short, the newest ROWID tail is re-audited every 30 seconds, and a durable rolling archive audit revisits an older 100-row window every minute so even an omission outside the recent tail is eventually repaired. Known recent messages are re-upserted to repair edits or completed attachments, and rejected ROWID support is periodically re-probed. A cursor-independent scan of the newest 250 messages runs about every minute, and a wall-clock 24-hour audit runs periodically as a further backstop. Separately, the viewer sweeps the durable notification ledger every 2.5 seconds, so a dropped local IPC hint cannot strand an alert while minimized. Test alert uses the currently selected notification style and sound. The circular arrow in a conversation's header, or F5, checks selected and global pages immediately; if those checks find no new Mac row, it safely escalates once to Wake Mac.

## Self-healing

A watchdog inside the agent checks its own workers every 15 seconds. If the machine wakes from sleep, a worker dies, or polling stalls, the agent performs the equivalent of closing and reopening itself automatically, then backfills anything missed. If the agent process itself is ever killed, its supervisor restarts it with backoff; if that is gone too, the hourly failsafe or opening the window brings everything back. The accent Recover button performs a staged repair on demand; Ctrl+Shift+R is its shortcut. It first creates fresh network workers and checks all recent messages, then invokes guarded Wake Mac because a Windows-only reset cannot retrieve a row Apple has not delivered to Messages. It never deletes local messages, settings, history, attachments, hidden state, or queued outgoing work.

## Wake Mac: held-back texts, now handled automatically

There is one gap no amount of Windows-side rescanning can cross. When Messages on the Mac goes quiet, Apple can hold incoming texts back from the Mac entirely. They show on the iPhone, but BlueBubbles has nothing to hand over. Restarting Messages on the Mac makes Apple deliver everything it was holding, with original timestamps and in correct order.

In 3.1.0 protection and recovery are layered, so you should rarely need to diagnose which link paused:

1. The Mac keepalive verifies Messages every 2 minutes and relaunches either relay app if it has stopped (MAC-SETUP.md).
2. The Windows agent watches for silence: after the Auto Wake Mac interval passes with nothing incoming (default 30 minutes), it restarts Messages on the Mac remotely, then re-checks everything and logs how many held-back texts arrived. Wake first acquires a durable maintenance lease: an already queued send blocks it, and a send composed immediately afterward stays queued until Messages is safe again.
3. The Wake Mac button (Ctrl+Shift+M) remains for on-demand use, with the same live footer report: watching for about a minute, forced re-checks, and an honest count including "the Mac was holding nothing back".
4. A local launchd restart is available only as an explicit `--daily-restart` opt-in for a dedicated relay Mac. It cannot coordinate with the Windows outbox, so the guarded Windows Auto Wake is the safer default.

The Mac must still be powered and awake for any of these; a wake cannot cure real sleep, only an idle Messages app.

## Notifications and verification codes

Every fresh incoming text raises its popup and plays its sound, full stop: window closed to the tray, minimized, open beside your work, even with that very conversation on screen. An open window is not treated as proof you are watching it. Two plain switches in Settings control this independently: Popups on or off, and Sound on or off. Turn popups off and the sound still announces every arrival; turn both off and the app is silent. The sound is deliberately independent of the popup machinery, so even if a card cannot be shown for any reason you still hear the arrival, and the popup and Windows-toast channels fall back to each other rather than showing nothing. Every alert attempt writes its outcome to the Activity panel, so if anything ever seems quiet, the panel says exactly what happened. The conversation list also steers itself so the conversation that just received a text scrolls into view with its unread badge, without touching your selection or the thread you are reading.

Incoming texts raise a small accent-styled popup near the bottom-right of the monitor without stealing focus, including while the viewer is minimized or hidden to the tray. Click its body to open the conversation. Notification and unread state is committed with the message by the agent, so a crash between storage and presentation cannot silently discard it. Cards that do not fit are queued instead of acknowledged unseen, and a transient database acknowledgement failure is retried. The app plays one explicit configurable sound per accepted alert burst and flashes the taskbar as a fallback. An alert that Windows first saw more than 30 minutes ago expires instead of surprising you much later; an old-dated message newly released by the Mac still alerts when first discovered. When a message carries a verification code, the popup displays it prominently with Copy code and Fill code buttons. Code popups stay for 35 seconds and are never discarded merely because three cards are already visible.

A message consisting only of four to eight digits also counts as a code. Every detected code continues to show a Copy chip under its message inside the conversation. Settings offers two alert styles, Interactive popup (Copy/Fill) or Windows notification (Focus Assist applies); the two master switches above them decide whether alerts appear or are heard at all. Interactive popup is the strongest in-app presentation path; Windows notification hands final display to Windows and its notification/Focus Assist policy. When "Always show Copy and Fill for verification codes" is enabled, code messages use the interactive popup even if ordinary messages use Windows notifications.

### Texts you send to yourself

A text to your own number or email is the natural way to test the line, and Apple marks it "sent by you" on every device even though it arrives here. Since 3.1.5 these self-conversation texts raise the normal popup and sound like any other arrival. The account your Mac reports is recognized automatically; if you also text yourself at a phone number, add that number in Settings under Self-texts (for example +15875550123) so its conversation counts as yours too. Texts sent from this app's own composer never alert, in any conversation, so replying to yourself from the PC stays quiet while the phone-typed side still announces itself. The whole behavior can be turned off with the Self-texts checkbox.

## Group management and Private API

Sending and receiving in existing groups always works. Creating a new group, adding or removing members, and renaming a group are functions Apple only exposes through BlueBubbles Private API mode (BlueBubbles Settings on the Mac; it requires SIP changes). The app detects this automatically: with Private API off those controls explain themselves and stay disabled, and with it on they simply work, no app update needed.

## Known limits

- Tapbacks display but cannot be sent from Windows.
- Reading here does not clear the unread badge on the iPhone.
- Read times appear only for contacts who have read receipts turned on; Apple reports nothing for the rest.
- Apple stamps a read time on your latest message only. The app marks every earlier message of yours Read as well, since reading the last requires reading the rest; hover the Read label for the by-time it derives from. Messages sent after their last read stay Delivered until the next receipt.
- Videos and documents open in their default apps rather than playing inline.
- The viewer must still be running in the tray for a popup or sound. Choosing Quit from the tray exits the presenter; the background agent continues collecting and the durable list remains complete.

## If something breaks

- A still red broken-ring indicator with "Cannot reach server": confirm Tailscale is connected on both machines, the Mac is awake, and the BlueBubbles window on the Mac shows the server running. (The connection indicator in the bottom-left chip animates while things work: an orbiting arc means connected and checking, a slow breathing ring means degraded or catching up, and a still broken ring means offline.)
- Quitting and relaunching: the round power button beside the Settings gear quits the window completely, guaranteed, so nothing is ever left for Task Manager (background collection by the agent continues). The ✕ still hides to the notification area by default so alerts keep coming; change that under Close button in Settings. And launching the app while a copy is already running now simply brings the running window forward instead of showing an error.
- The Activity panel: click the connection chip (the animated indicator or the status text beside it) to open a live view of everything the app is doing: every connection attempt, error, refusal, reset, wake, and recovery of this session as it happens, plus the current connection details. Copy all puts the whole session on the clipboard for sending to your assistant; Open log folder jumps to the full on-disk logs. A built-in window warden also reports here: if any unexpected window ever tries to appear, it is named in the Activity panel and removed from the screen automatically.
- "Server rejected the password": retype it in Settings, the gear at the bottom left.
- "Background agent offline": the window restarts it automatically within seconds. If it persists, run Agent-Console.bat to see the agent's output directly, or install.bat to re-register everything.
- "Compatibility checks only": update the BlueBubbles server on the Mac. The app detected that the installed server cannot honor bounded message-ROWID queries, which are what make late iCloud insertions discoverable without trusting their old date.
- Logs live at `%LOCALAPPDATA%\jrl-messages\logs`: jrl-agent.log is the collector, jrl-messages.log is the window, and agent-supervisor.log is the babysitter. On the Mac: `~/Library/Logs/jrl-keepalive.log`.
