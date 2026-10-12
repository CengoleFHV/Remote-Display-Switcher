"""Monitor profile handling for PC Display Switch.

MultiMonitorTool stays an external sidecar application.  Monitor profiles are
machine-specific and are therefore stored in the user's local application data
directory instead of being bundled into the executable.
"""

from __future__ import annotations

from configparser import ConfigParser, Error as ConfigParserError, SectionProxy
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator, Sequence
from uuid import uuid4


APP_VERSION = "1.1.2"
LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


PROFILE_NAMES = {
    "desk": "desk.cfg",
    "tv": "tv.cfg",
}


@dataclass(frozen=True)
class MonitorProfileEntry:
    """One monitor entry read from a MultiMonitorTool profile."""

    name: str
    monitor_id: str
    bits_per_pixel: int
    width: int
    height: int
    display_flags: int
    display_frequency: int
    display_orientation: int
    position_x: int
    position_y: int

    @property
    def active(self) -> bool:
        return self.bits_per_pixel > 0 and self.width > 0 and self.height > 0

    def set_monitors_argument(self, *, primary: bool) -> str:
        values = [f"Name={self.monitor_id}"]
        if primary:
            values.append("Primary=1")
        values.extend(
            (
                f"BitsPerPixel={self.bits_per_pixel}",
                f"Width={self.width}",
                f"Height={self.height}",
                f"DisplayFlags={self.display_flags}",
                f"DisplayFrequency={self.display_frequency}",
                f"DisplayOrientation={self.display_orientation}",
                f"PositionX={self.position_x}",
                f"PositionY={self.position_y}",
            )
        )
        return " ".join(values)

    def settings_signature(self) -> tuple[int, ...]:
        return (
            self.bits_per_pixel,
            self.width,
            self.height,
            self.display_flags,
            self.display_frequency,
            self.display_orientation,
            self.position_x,
            self.position_y,
        )


class DisplayControlError(RuntimeError):
    """Raised when a display command cannot be completed."""


class DisplayControlBusyError(DisplayControlError):
    """Raised when another display command is already running."""


def application_directory() -> Path:
    """Return the source directory or the directory of the frozen executable."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_profile_directory() -> Path:
    """Return a persistent, user-writable directory for monitor profiles."""

    configured_directory = os.environ.get("PC_DISPLAY_SWITCH_PROFILE_DIR")
    if configured_directory:
        return Path(configured_directory).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (
            Path(local_app_data).expanduser().resolve()
            / "PCDisplaySwitch"
            / "profiles"
        )

    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "PCDisplaySwitch" / "profiles"

    # This fallback is mainly useful when running the source outside Windows.
    return application_directory() / "profiles"


class DisplayController:
    """Save and restore explicit monitor topologies with MultiMonitorTool."""

    def __init__(
        self,
        *,
        app_directory: Path | None = None,
        profile_directory: Path | None = None,
        tool_path: Path | None = None,
        display_switch_path: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 30,
        sleeper: Callable[[float], None] = time.sleep,
        verification_attempts: int = 12,
        verification_interval_seconds: float = 0.5,
        verification_stable_samples: int = 3,
        topology_settle_seconds: float = 1,
    ) -> None:
        self.app_directory = (app_directory or application_directory()).resolve()
        self.profile_directory = (
            profile_directory or default_profile_directory()
        ).resolve()
        self._configured_tool_path = tool_path
        self.display_switch_path = (
            display_switch_path or self._default_display_switch_path()
        )
        self._runner = runner
        self.timeout_seconds = timeout_seconds
        self._sleeper = sleeper
        self.verification_attempts = max(1, verification_attempts)
        self.verification_interval_seconds = verification_interval_seconds
        self.verification_stable_samples = max(1, verification_stable_samples)
        self.topology_settle_seconds = topology_settle_seconds
        self._command_lock = threading.Lock()

    @staticmethod
    def _default_display_switch_path() -> Path:
        windows_directory = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return windows_directory / "System32" / "DisplaySwitch.exe"

    def profile_path(self, profile_name: str) -> Path:
        try:
            filename = PROFILE_NAMES[profile_name]
        except KeyError as error:
            raise DisplayControlError(
                f"Unbekanntes Monitorprofil: {profile_name}"
            ) from error
        return self.profile_directory / filename

    def profile_exists(self, profile_name: str) -> bool:
        profile = self.profile_path(profile_name)
        try:
            return profile.is_file() and profile.stat().st_size > 0
        except OSError as error:
            raise DisplayControlError(
                f"Monitorprofil kann nicht gelesen werden: {profile} ({error})"
            ) from error

    def backup_path(self, profile_name: str) -> Path:
        profile = self.profile_path(profile_name)
        return profile.with_name(f"{profile.stem}.backup{profile.suffix}")

    def find_multimonitor_tool(self) -> Path:
        """Find MultiMonitorTool only in explicitly trusted local locations."""

        configured = self._configured_tool_path
        if configured is None:
            configured_value = os.environ.get("PC_DISPLAY_SWITCH_MULTIMONITOR_TOOL")
            if configured_value:
                configured = Path(configured_value).expanduser()

        if configured is not None:
            configured = Path(configured).expanduser()
            if not configured.is_absolute():
                configured = self.app_directory / configured
            configured = configured.resolve()
            if configured.is_file():
                return configured
            raise DisplayControlError(
                "Der konfigurierte MultiMonitorTool-Pfad existiert nicht: "
                f"{configured}"
            )

        candidates = (
            self.app_directory / "MultiMonitorTool.exe",
            self.app_directory / "MultiMonitorTool" / "MultiMonitorTool.exe",
            self.app_directory / "tools" / "MultiMonitorTool.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()

        raise DisplayControlError(
            "MultiMonitorTool.exe wurde nicht gefunden. Lege die Datei neben "
            "PCDisplaySwitch.exe oder in den Unterordner 'MultiMonitorTool'."
        )

    @contextmanager
    def _exclusive_command(self) -> Iterator[None]:
        if not self._command_lock.acquire(blocking=False):
            raise DisplayControlBusyError(
                "Eine Monitor-Umschaltung läuft bereits. Bitte kurz warten."
            )
        try:
            yield
        finally:
            self._command_lock.release()

    def _run(self, command: Sequence[Path | str]) -> None:
        arguments = [str(argument) for argument in command]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        LOGGER.info("Display command: %s", arguments)

        try:
            completed = self._runner(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                creationflags=creation_flags,
            )
        except FileNotFoundError as error:
            raise DisplayControlError(
                f"Programm nicht gefunden: {arguments[0]}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise DisplayControlError(
                "Die Monitor-Umschaltung hat zu lange gedauert und wurde abgebrochen."
            ) from error
        except OSError as error:
            raise DisplayControlError(
                f"Monitor-Befehl konnte nicht gestartet werden: {error}"
            ) from error

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            suffix = f" ({details})" if details else ""
            raise DisplayControlError(
                f"Monitor-Befehl fehlgeschlagen (Code {completed.returncode}){suffix}"
            )
        LOGGER.info("Display command finished: %s", arguments[1:])

    def _save_profile_unlocked(self, profile_name: str) -> Path:
        tool = self.find_multimonitor_tool()
        profile = self.profile_path(profile_name)
        temporary_profile = profile.with_name(
            f".{profile.stem}-{uuid4().hex}.cfg"
        )

        try:
            profile.parent.mkdir(parents=True, exist_ok=True)
            self._run((tool, "/SaveConfig", temporary_profile))

            if (
                not temporary_profile.is_file()
                or temporary_profile.stat().st_size == 0
            ):
                raise DisplayControlError(
                    "MultiMonitorTool hat keine gültige Profildatei angelegt."
                )

            if self.profile_exists(profile_name):
                self._atomic_copy(profile, self.backup_path(profile_name))
            temporary_profile.replace(profile)
        except DisplayControlError:
            raise
        except OSError as error:
            raise DisplayControlError(
                f"Monitorprofil konnte nicht gespeichert werden: {profile} ({error})"
            ) from error
        finally:
            try:
                temporary_profile.unlink(missing_ok=True)
            except OSError:
                pass

        return profile

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        temporary_copy = destination.with_name(
            f".{destination.stem}-{uuid4().hex}{destination.suffix}"
        )
        try:
            shutil.copy2(source, temporary_copy)
            temporary_copy.replace(destination)
        finally:
            try:
                temporary_copy.unlink(missing_ok=True)
            except OSError:
                pass

    def save_profile(self, profile_name: str) -> Path:
        with self._exclusive_command():
            return self._save_profile_unlocked(profile_name)

    def restore_profile_backup(self, profile_name: str) -> Path:
        with self._exclusive_command():
            profile = self.profile_path(profile_name)
            backup = self.backup_path(profile_name)
            try:
                if not backup.is_file() or backup.stat().st_size == 0:
                    raise DisplayControlError(
                        f"Für das Profil '{profile_name}' ist keine Sicherung vorhanden."
                    )
                profile.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_copy(backup, profile)
            except DisplayControlError:
                raise
            except OSError as error:
                raise DisplayControlError(
                    f"Profilsicherung konnte nicht wiederhergestellt werden: {error}"
                ) from error
            return profile

    @staticmethod
    def _profile_integer(
        section: SectionProxy,
        key: str,
        profile: Path,
    ) -> int:
        value = section.get(key, "0").strip()
        try:
            return int(value)
        except ValueError as error:
            raise DisplayControlError(
                f"Ungültiger Wert '{key}={value}' im Monitorprofil {profile}."
            ) from error

    def _read_profile_unlocked(
        self,
        profile_name: str,
    ) -> list[MonitorProfileEntry]:
        profile = self.profile_path(profile_name)
        if not self.profile_exists(profile_name):
            label = "PC" if profile_name == "desk" else "TV"
            raise DisplayControlError(
                f"Das {label}-Profil fehlt: {profile}. Speichere zuerst die aktuell "
                "korrekte Monitor-Anordnung über das Tray-Menü."
            )

        return self._read_profile_file(profile)

    def _read_profile_file(self, profile: Path) -> list[MonitorProfileEntry]:
        """Parse a MultiMonitorTool profile or verification snapshot."""

        parser = ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        try:
            parser.read_string(profile.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ConfigParserError) as error:
            raise DisplayControlError(
                f"Monitorprofil kann nicht gelesen werden: {profile} ({error})"
            ) from error

        entries = []
        for section_name in parser.sections():
            if not section_name.startswith("Monitor"):
                continue
            section = parser[section_name]
            entries.append(
                MonitorProfileEntry(
                    name=section.get("Name", "").strip(),
                    monitor_id=section.get("MonitorID", "").strip(),
                    bits_per_pixel=self._profile_integer(
                        section, "BitsPerPixel", profile
                    ),
                    width=self._profile_integer(section, "Width", profile),
                    height=self._profile_integer(section, "Height", profile),
                    display_flags=self._profile_integer(
                        section, "DisplayFlags", profile
                    ),
                    display_frequency=self._profile_integer(
                        section, "DisplayFrequency", profile
                    ),
                    display_orientation=self._profile_integer(
                        section, "DisplayOrientation", profile
                    ),
                    position_x=self._profile_integer(section, "PositionX", profile),
                    position_y=self._profile_integer(section, "PositionY", profile),
                )
            )

        if not entries:
            raise DisplayControlError(
                f"Monitorprofil enthält keine Monitore: {profile}"
            )
        return entries

    def _profile_switch_plan_unlocked(
        self,
        profile_name: str,
    ) -> tuple[list[MonitorProfileEntry], list[str], MonitorProfileEntry]:
        target_entries = self._read_profile_unlocked(profile_name)

        inventory_entries = list(target_entries)
        for other_profile_name in PROFILE_NAMES:
            if (
                other_profile_name != profile_name
                and self.profile_exists(other_profile_name)
            ):
                inventory_entries.extend(
                    self._read_profile_unlocked(other_profile_name)
                )

        desired_entries = [entry for entry in target_entries if entry.active]
        if not desired_entries:
            raise DisplayControlError(
                f"Das Profil '{profile_name}' enthält keinen aktiven Monitor."
            )

        missing_ids = [
            entry.name or "unbekannt"
            for entry in desired_entries
            if not entry.monitor_id
        ]
        if missing_ids:
            raise DisplayControlError(
                "Aktive Monitore ohne stabile Monitor-ID im Profil: "
                f"{', '.join(missing_ids)}. Speichere das Profil erneut, während "
                "alle betreffenden Geräte von Windows erkannt werden."
            )

        desired_by_id = {}
        for entry in desired_entries:
            key = entry.monitor_id.casefold()
            if key in desired_by_id:
                raise DisplayControlError(
                    f"Doppelte Monitor-ID im Profil '{profile_name}': "
                    f"{entry.monitor_id}"
                )
            desired_by_id[key] = entry

        known_ids = {}
        for entry in inventory_entries:
            if entry.monitor_id:
                known_ids.setdefault(entry.monitor_id.casefold(), entry.monitor_id)

        inactive_ids = [
            monitor_id
            for key, monitor_id in known_ids.items()
            if key not in desired_by_id
        ]
        primary = next(
            (
                entry
                for entry in desired_entries
                if entry.position_x == 0 and entry.position_y == 0
            ),
            desired_entries[0],
        )
        return desired_entries, inactive_ids, primary

    def _apply_profile_once_unlocked(
        self,
        tool: Path,
        desired_entries: list[MonitorProfileEntry],
        inactive_ids: list[str],
        primary: MonitorProfileEntry,
    ) -> None:
        desired_ids = [entry.monitor_id for entry in desired_entries]
        monitor_settings = [
            entry.set_monitors_argument(primary=entry is primary)
            for entry in desired_entries
        ]

        # In a TV-only topology Windows may stop exposing the hardware IDs of
        # both desk monitors. Extending once makes every connected device
        # enumerable before the ID-based commands establish the final layout.
        self._run((self.display_switch_path, "/extend"))
        # DisplaySwitch can exit while Windows is still rebuilding the display
        # topology. ID-based commands issued immediately can be ignored or
        # overwritten by that pending change.
        self._sleeper(self.topology_settle_seconds)

        # DISPLAY1/2/3 changes when displays are disabled. Every command below
        # therefore uses the full hardware MonitorID stored in the profiles.
        self._run((tool, "/enable", *desired_ids))
        self._run((tool, "/SetPrimary", primary.monitor_id))
        self._run((tool, "/SetMonitors", *monitor_settings))

        self._wait_for_desired_monitors_unlocked(tool, desired_entries)

        if inactive_ids:
            self._run((tool, "/disable", *inactive_ids))

        # Windows can move monitors after another display is disabled. Apply
        # the target positions once more after the final topology is active.
        self._run((tool, "/SetMonitors", *monitor_settings))
        self._run((tool, "/SetPrimary", primary.monitor_id))

    def _snapshot_active_entries_unlocked(
        self,
        tool: Path,
    ) -> dict[str, MonitorProfileEntry]:
        snapshot = self.profile_directory / f".verify-{uuid4().hex}.cfg"
        try:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            self._run((tool, "/SaveConfig", snapshot))
            if not snapshot.is_file() or snapshot.stat().st_size == 0:
                raise DisplayControlError(
                    "MultiMonitorTool hat keinen Prüfsnapshot angelegt."
                )

            active_entries = [
                entry for entry in self._read_profile_file(snapshot) if entry.active
            ]
            missing_ids = [
                entry.name or "unbekannt"
                for entry in active_entries
                if not entry.monitor_id
            ]
            if missing_ids:
                raise DisplayControlError(
                    "Aktive Monitore ohne Hardware-ID im Prüfsnapshot: "
                    f"{', '.join(missing_ids)}"
                )

            entries_by_id = {}
            for entry in active_entries:
                key = entry.monitor_id.casefold()
                if key in entries_by_id:
                    raise DisplayControlError(
                        "Doppelte aktive Hardware-ID im Prüfsnapshot: "
                        f"{entry.monitor_id}"
                    )
                entries_by_id[key] = entry
            return entries_by_id
        except DisplayControlError:
            raise
        except OSError as error:
            raise DisplayControlError(
                f"Monitorzustand konnte nicht geprüft werden: {error}"
            ) from error
        finally:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass

    def _wait_for_desired_monitors_unlocked(
        self,
        tool: Path,
        desired_entries: list[MonitorProfileEntry],
    ) -> None:
        desired_ids = {entry.monitor_id.casefold() for entry in desired_entries}
        last_problem = "kein Prüfsnapshot verfügbar"
        stable_samples = 0

        for attempt in range(self.verification_attempts):
            try:
                actual_entries = self._snapshot_active_entries_unlocked(tool)
                actual_ids = set(actual_entries)
                if desired_ids.issubset(actual_ids):
                    stable_samples += 1
                    if stable_samples >= self.verification_stable_samples:
                        return
                    last_problem = "Aktivierung der Zielmonitore ist noch nicht stabil"
                else:
                    stable_samples = 0
                    last_problem = (
                        f"erwartet: {sorted(desired_ids)}, erkannt: {sorted(actual_ids)}"
                    )
            except DisplayControlError as error:
                stable_samples = 0
                last_problem = str(error)

            LOGGER.info("Waiting for target monitors: %s", last_problem)

            if attempt + 1 < self.verification_attempts:
                self._sleeper(self.verification_interval_seconds)

        raise DisplayControlError(
            "Die Zielmonitore wurden nicht vollständig aktiviert; die bisher "
            f"funktionierenden Anzeigen bleiben eingeschaltet ({last_problem})."
        )

    @staticmethod
    def _profile_mismatch(
        desired_entries: list[MonitorProfileEntry],
        actual_entries: dict[str, MonitorProfileEntry],
    ) -> str | None:
        desired_by_id = {
            entry.monitor_id.casefold(): entry for entry in desired_entries
        }
        if set(actual_entries) != set(desired_by_id):
            return (
                "aktive Hardware-IDs stimmen nicht mit dem Zielprofil überein "
                f"(erwartet: {sorted(desired_by_id)}, "
                f"erkannt: {sorted(actual_entries)})"
            )

        for monitor_id, desired in desired_by_id.items():
            actual = actual_entries[monitor_id]
            if actual.settings_signature() != desired.settings_signature():
                return (
                    f"Anzeige {desired.monitor_id} hat falsche Einstellungen "
                    f"(erwartet: {desired.settings_signature()}, "
                    f"erkannt: {actual.settings_signature()})"
                )
        return None

    def _wait_for_profile_match_unlocked(
        self,
        tool: Path,
        desired_entries: list[MonitorProfileEntry],
    ) -> str | None:
        last_problem = "kein Prüfsnapshot verfügbar"
        stable_samples = 0

        for attempt in range(self.verification_attempts):
            try:
                actual_entries = self._snapshot_active_entries_unlocked(tool)
                mismatch = self._profile_mismatch(desired_entries, actual_entries)
                if mismatch is None:
                    stable_samples += 1
                    if stable_samples >= self.verification_stable_samples:
                        return None
                    last_problem = "Zielprofil stimmt überein, ist aber noch nicht stabil"
                else:
                    stable_samples = 0
                    last_problem = mismatch
            except DisplayControlError as error:
                stable_samples = 0
                last_problem = str(error)

            LOGGER.info("Waiting for final profile: %s", last_problem)

            if attempt + 1 < self.verification_attempts:
                self._sleeper(self.verification_interval_seconds)

        return last_problem

    def _apply_profile_unlocked(self, profile_name: str) -> None:
        desired_entries, inactive_ids, primary = (
            self._profile_switch_plan_unlocked(profile_name)
        )
        tool = self.find_multimonitor_tool()
        last_problem = "unbekannter Monitorzustand"

        for attempt in range(2):
            LOGGER.info("Applying profile %s, attempt %s", profile_name, attempt + 1)
            try:
                self._apply_profile_once_unlocked(
                    tool,
                    desired_entries,
                    inactive_ids,
                    primary,
                )
                mismatch = self._wait_for_profile_match_unlocked(
                    tool,
                    desired_entries,
                )
            except DisplayControlError as error:
                last_problem = str(error)
                LOGGER.info("Profile attempt failed: %s", last_problem)
                continue

            if mismatch is None:
                LOGGER.info("Profile %s verified and stable", profile_name)
                return

            last_problem = mismatch
            LOGGER.info("Profile attempt did not settle: %s", last_problem)

        recovery_message = "Als sichere Zwischenlösung wurden alle Anzeigen erweitert."
        try:
            self._run((self.display_switch_path, "/extend"))
        except DisplayControlError as error:
            recovery_message = (
                "Auch der Erweitern-Notfallmodus schlug fehl: "
                f"{error}"
            )

        raise DisplayControlError(
            f"Profil '{profile_name}' konnte nach zwei Versuchen nicht korrekt "
            f"hergestellt werden: {last_problem}. {recovery_message}"
        )

    def switch(self, command_name: str) -> str:
        """Execute one of the display commands exposed by the application."""

        with self._exclusive_command():
            LOGGER.info("Requested display mode: %s", command_name)
            if command_name == "desk":
                self._apply_profile_unlocked("desk")
                return (
                    "PC-Profil über feste Monitor-IDs angewendet. Beide "
                    "Schreibtisch-Monitore sollten jetzt aktiv sein."
                )

            if command_name == "tv":
                # Never switch away before a known-good return configuration exists.
                if not self.profile_exists("desk"):
                    raise DisplayControlError(
                        "Bitte zuerst im Tray-Menü die aktuelle Anordnung als "
                        "PC-Profil speichern. So ist der Rückweg zu beiden "
                        "Monitoren gesichert."
                    )

                if self.profile_exists("tv"):
                    self._apply_profile_unlocked("tv")
                    return "TV-Profil über die feste Monitor-ID angewendet."

                # The fallback can switch all PC monitors off. Apply the return
                # profile once before leaving so a missing/broken tool fails
                # while the desk monitors are still active.
                self._apply_profile_unlocked("desk")
                self._run((self.display_switch_path, "/external"))
                return "Windows-Standardmodus angefordert (ohne TV-Profil nicht geprüft)."

            native_modes = {
                "extend": "/extend",
                "clone": "/clone",
            }
            try:
                mode = native_modes[command_name]
            except KeyError as error:
                raise DisplayControlError(
                    f"Unbekannter Befehl: {command_name}"
                ) from error

            self._run((self.display_switch_path, mode))
            return f"Befehl ausgeführt: {command_name}"
