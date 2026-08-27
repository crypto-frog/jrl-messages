"""Settings persistence. The server password lives in the OS credential store
(Windows Credential Manager via keyring); the JSON config holds everything else.
If no keyring backend is available, we fall back to the config file and log it."""
import json
import logging
from dataclasses import dataclass, asdict

from . import constants

log = logging.getLogger(__name__)

_KEYRING_SERVICE = "jrl-messages"
_KEYRING_USER = "server"


@dataclass
class Settings:
    server_url: str = ""
    backfill_horizon_days: int = 0   # 0 means sync everything
    notifications: bool = True          # legacy; superseded by notify_mode
    notify_mode: str = "popup"          # alert STYLE: popup | system
    # The two master switches, each its own checkbox in Settings. Style
    # only chooses how a popup alert looks; these choose whether alerts
    # appear and whether they are heard, independently.
    popups_enabled: bool = True
    notification_sound: bool = True      # explicit sound for new messages
    # Texts you send to your own number or email are marked sent-by-you by
    # Apple on every device, but they are arrivals here and should alert.
    # self_addresses may list extra own numbers/emails, comma separated;
    # the account the Mac reports is always included automatically.
    self_chat_alerts: bool = True
    self_addresses: str = ""
    # The in-app notification center (the bell). Off hides the bell and
    # its panel entirely; the durable feed keeps collecting quietly so
    # turning it back on shows recent history.
    alert_center_enabled: bool = True
    # iPhone notification mirroring over Bluetooth (ANCS), experimental.
    # Off by default: it needs the phone paired with Windows and within
    # Bluetooth range. The address/name identify the chosen iPhone;
    # phone_ignore_apps mutes noisy apps by bundle-id substring (texts
    # via com.apple.MobileSMS are always muted, the Mac relay owns them).
    phone_link_enabled: bool = False
    phone_ble_address: str = ""
    phone_ble_name: str = ""
    phone_ignore_apps: str = ""
    interactive_codes: bool = True       # code alerts always use rich actions
    tooltip_mode: str = "limited"        # limited | always | off
    tooltip_seen: dict = None             # stable tip id -> display count
    close_to_tray: bool = True            # keep presenter alive after window X
    hidden_migration_done: bool = False
    font_scale: float = 1.0
    accent: str = "Blue"
    # Minutes of incoming silence after which the agent restarts Messages on
    # the Mac by itself (the automatic Wake Mac). 0 disables the policy.
    auto_wake_minutes: int = constants.AUTO_WAKE_DEFAULT_MIN
    win_geometry: str = ""           # saved window geometry, hex
    splitter_sizes: str = ""         # "340,840"
    _password_fallback: str = ""     # only used when keyring is unavailable

    def __post_init__(self):
        # Avoid a mutable dataclass default while keeping JSON simple and
        # backward-compatible with every pre-3.1 settings file.
        if not isinstance(self.tooltip_seen, dict):
            self.tooltip_seen = {}
        else:
            cleaned = {}
            for key, value in list(self.tooltip_seen.items())[:200]:
                if not isinstance(key, str) or not key:
                    continue
                try:
                    cleaned[key] = min(2, max(0, int(value)))
                except (TypeError, ValueError):
                    continue
            self.tooltip_seen = cleaned

    def base_url(self) -> str:
        return self.server_url.strip().rstrip("/")


def load() -> Settings:
    try:
        if constants.CONFIG_PATH.exists():
            raw = json.loads(constants.CONFIG_PATH.read_text(encoding="utf-8"))
            s = Settings()
            for k, v in raw.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            s.__post_init__()
            if "notify_mode" not in raw and raw.get("notifications") is False:
                s.popups_enabled = False
                s.notification_sound = False
            # Pre-3.1.4 files stored Off as a third style. Off is now the
            # popups master switch, so the style stays a two-way choice.
            if s.notify_mode == "off":
                s.notify_mode = "popup"
                s.popups_enabled = False
            if s.notify_mode not in ("popup", "system"):
                s.notify_mode = "popup"
            if s.tooltip_mode not in ("limited", "always", "off"):
                s.tooltip_mode = "limited"
            return s
    except Exception:
        log.exception("Failed to read config; starting fresh")
    return Settings()


def save(s: Settings) -> None:
    constants.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = constants.CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    # os.replace semantics through pathlib: either the old complete settings
    # or the new complete settings survive an interruption, never half JSON.
    tmp.replace(constants.CONFIG_PATH)


def get_password(s: Settings) -> str:
    try:
        import keyring
        v = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if v:
            return v
    except Exception:
        log.warning("Keyring unavailable; using config-file fallback")
    return s._password_fallback or ""


def set_password(s: Settings, value: str) -> None:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, value)
        s._password_fallback = ""
        save(s)
        return
    except Exception:
        log.warning("Keyring unavailable; storing password in config file")
    s._password_fallback = value
    save(s)
