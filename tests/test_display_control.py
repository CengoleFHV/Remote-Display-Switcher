from pathlib import Path
import subprocess
import tempfile
import unittest

from display_control import (
    DisplayControlBusyError,
    DisplayControlError,
    DisplayController,
)


MAIN_ID = r"MONITOR\MAIN123\{driver}\0001"
DESK_SECONDARY_ID = r"MONITOR\AOC456\{driver}\0003"
TV_ID = r"MONITOR\TV789\{driver}\0006"

DESK_PROFILE = rf"""
[Monitor0]
Name=\\.\DISPLAY1
MonitorID={MAIN_ID}
SerialNumber=
BitsPerPixel=32
Width=5120
Height=1440
DisplayFlags=0
DisplayFrequency=120
DisplayOrientation=0
PositionX=0
PositionY=0
[Monitor1]
Name=\\.\DISPLAY2
MonitorID={DESK_SECONDARY_ID}
SerialNumber=
BitsPerPixel=32
Width=1920
Height=1080
DisplayFlags=0
DisplayFrequency=60
DisplayOrientation=0
PositionX=1606
PositionY=-1080
[Monitor2]
Name=\\.\DISPLAY3
MonitorID={TV_ID}
SerialNumber=
BitsPerPixel=0
Width=0
Height=0
DisplayFlags=0
DisplayFrequency=0
DisplayOrientation=0
PositionX=0
PositionY=0
""".strip()

TV_PROFILE = rf"""
[Monitor0]
Name=\\.\DISPLAY1
MonitorID=
SerialNumber=
BitsPerPixel=0
Width=0
Height=0
DisplayFlags=0
DisplayFrequency=0
DisplayOrientation=0
PositionX=0
PositionY=0
[Monitor1]
Name=\\.\DISPLAY2
MonitorID={TV_ID}
SerialNumber=
BitsPerPixel=32
Width=3840
Height=2160
DisplayFlags=0
DisplayFrequency=144
DisplayOrientation=0
PositionX=0
PositionY=0
""".strip()


class RecordingRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode
        self.snapshot_content = None
        self.snapshot_contents = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if "/SaveConfig" in command and self.returncode == 0:
            profile_path = Path(command[command.index("/SaveConfig") + 1])
            if self.snapshot_contents:
                content = self.snapshot_contents.pop(0)
            else:
                content = self.snapshot_content or "saved monitor configuration"
            profile_path.write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout="",
            stderr="simulated error" if self.returncode else "",
        )


class DisplayControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.app_directory = self.root / "app with spaces"
        self.profile_directory = self.root / "profile data"
        self.app_directory.mkdir()
        self.tool = self.app_directory / "MultiMonitorTool.exe"
        self.tool.write_bytes(b"test executable")
        self.display_switch = self.root / "DisplaySwitch.exe"
        self.display_switch.write_bytes(b"test executable")
        self.runner = RecordingRunner()
        self.controller = DisplayController(
            app_directory=self.app_directory,
            profile_directory=self.profile_directory,
            display_switch_path=self.display_switch,
            runner=self.runner,
            sleeper=lambda _seconds: None,
            verification_attempts=1,
            verification_stable_samples=1,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_profile(self, name):
        path = self.controller.profile_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("monitor configuration", encoding="utf-8")
        return path

    def write_monitor_profile(self, name, content):
        path = self.controller.profile_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_valid_profiles(self):
        self.write_monitor_profile("desk", DESK_PROFILE)
        self.write_monitor_profile("tv", TV_PROFILE)

    def recorded_commands(self):
        return [call[0] for call in self.runner.calls]

    def assert_safe_subprocess_options(self, call):
        _, options = call
        self.assertFalse(options["check"])
        self.assertTrue(options["capture_output"])
        self.assertTrue(options["text"])
        self.assertEqual(options["timeout"], 30)

    def test_finds_tool_next_to_application(self):
        self.assertEqual(self.controller.find_multimonitor_tool(), self.tool.resolve())

    def test_missing_tool_has_actionable_error(self):
        self.tool.unlink()

        with self.assertRaisesRegex(DisplayControlError, "neben PCDisplaySwitch.exe"):
            self.controller.find_multimonitor_tool()

    def test_save_desk_profile_uses_absolute_safe_argument_list(self):
        profile = self.controller.save_profile("desk")

        self.assertEqual(profile, (self.profile_directory / "desk.cfg").resolve())
        self.assertTrue(profile.is_file())
        command, _ = self.runner.calls[0]
        self.assertEqual(command[:2], [str(self.tool.resolve()), "/SaveConfig"])
        temporary_profile = Path(command[2])
        self.assertEqual(temporary_profile.parent, profile.parent)
        self.assertNotEqual(temporary_profile, profile)
        self.assertFalse(temporary_profile.exists())
        self.assert_safe_subprocess_options(self.runner.calls[0])

    def test_desk_switch_uses_ids_when_display_two_changes_identity(self):
        self.write_valid_profiles()
        self.runner.snapshot_content = DESK_PROFILE

        result = self.controller.switch("desk")

        commands = self.recorded_commands()
        self.assertIn("feste Monitor-IDs", result)
        self.assertEqual(
            commands[0],
            [str(self.display_switch), "/extend"],
        )
        self.assertEqual(
            commands[1],
            [str(self.tool.resolve()), "/enable", MAIN_ID, DESK_SECONDARY_ID],
        )
        self.assertEqual(
            commands[2],
            [str(self.tool.resolve()), "/SetPrimary", MAIN_ID],
        )
        self.assertEqual(
            commands[5],
            [str(self.tool.resolve()), "/disable", TV_ID],
        )
        self.assertEqual(commands[3][1], "/SetMonitors")
        self.assertIn(f"Name={MAIN_ID} Primary=1", commands[3][2])
        self.assertIn("Width=5120", commands[3][2])
        self.assertIn(f"Name={DESK_SECONDARY_ID}", commands[3][3])
        self.assertIn("PositionX=1606 PositionY=-1080", commands[3][3])
        self.assertEqual(commands[6], commands[3])
        self.assertEqual(
            commands[7],
            [str(self.tool.resolve()), "/SetPrimary", MAIN_ID],
        )
        self.assertEqual(commands[4][1], "/SaveConfig")
        self.assertEqual(commands[8][1], "/SaveConfig")
        self.assertTrue(all("/LoadConfig" not in command for command in commands))
        self.assertNotIn(r"\\.\DISPLAY2", " ".join(sum(commands, [])))
        self.assertEqual(list(self.profile_directory.glob(".verify-*.cfg")), [])

    def test_tv_switch_requires_a_return_profile(self):
        with self.assertRaisesRegex(DisplayControlError, "zuerst.*PC-Profil"):
            self.controller.switch("tv")

        self.assertEqual(self.runner.calls, [])

    def test_tv_switch_uses_windows_fallback_when_tv_profile_is_missing(self):
        self.write_monitor_profile("desk", DESK_PROFILE)
        self.runner.snapshot_content = DESK_PROFILE

        result = self.controller.switch("tv")

        self.assertIn("Windows-Standardmodus", result)
        self.assertEqual(
            self.recorded_commands()[-1],
            [str(self.display_switch), "/external"],
        )

    def test_tv_fallback_requires_restore_tool_to_still_be_available(self):
        self.write_monitor_profile("desk", DESK_PROFILE)
        self.tool.unlink()

        with self.assertRaisesRegex(DisplayControlError, "MultiMonitorTool.exe"):
            self.controller.switch("tv")

        self.assertEqual(self.runner.calls, [])

    def test_tv_switch_prefers_saved_tv_profile(self):
        self.write_valid_profiles()
        self.runner.snapshot_content = TV_PROFILE

        result = self.controller.switch("tv")

        commands = self.recorded_commands()
        self.assertEqual(result, "TV-Profil über die feste Monitor-ID angewendet.")
        self.assertEqual(
            commands[0],
            [str(self.display_switch), "/extend"],
        )
        self.assertEqual(
            commands[1],
            [str(self.tool.resolve()), "/enable", TV_ID],
        )
        self.assertEqual(
            commands[5],
            [str(self.tool.resolve()), "/disable", MAIN_ID, DESK_SECONDARY_ID],
        )
        self.assertIn(f"Name={TV_ID} Primary=1", commands[3][2])
        self.assertIn("Width=3840 Height=2160", commands[3][2])
        self.assertNotIn(r"\\.\DISPLAY2", " ".join(sum(commands, [])))

    def test_wrong_first_snapshot_retries_then_succeeds(self):
        self.write_valid_profiles()
        self.runner.snapshot_contents = [
            DESK_PROFILE,
            TV_PROFILE,
            DESK_PROFILE,
            DESK_PROFILE,
        ]

        result = self.controller.switch("desk")

        commands = self.recorded_commands()
        self.assertIn("feste Monitor-IDs", result)
        self.assertEqual(
            commands.count([str(self.display_switch), "/extend"]),
            2,
        )
        self.assertEqual(
            sum(command[1] == "/SaveConfig" for command in commands),
            4,
        )
        self.assertEqual(list(self.profile_directory.glob(".verify-*.cfg")), [])

    def test_snapshot_id_comparison_is_case_insensitive(self):
        self.write_valid_profiles()
        lowercase_ids_snapshot = (
            DESK_PROFILE.replace(MAIN_ID, MAIN_ID.lower())
            .replace(DESK_SECONDARY_ID, DESK_SECONDARY_ID.lower())
            .replace(TV_ID, TV_ID.lower())
        )
        self.runner.snapshot_content = lowercase_ids_snapshot

        result = self.controller.switch("desk")

        self.assertIn("feste Monitor-IDs", result)
        self.assertEqual(
            self.recorded_commands().count([str(self.display_switch), "/extend"]),
            1,
        )

    def test_persistent_wrong_snapshot_recovers_to_extend(self):
        self.write_valid_profiles()
        self.runner.snapshot_contents = [
            DESK_PROFILE,
            TV_PROFILE,
            DESK_PROFILE,
            TV_PROFILE,
        ]

        with self.assertRaisesRegex(
            DisplayControlError,
            "sichere Zwischenlösung.*Anzeigen erweitert",
        ):
            self.controller.switch("desk")

        commands = self.recorded_commands()
        self.assertEqual(
            commands.count([str(self.display_switch), "/extend"]),
            3,
        )
        self.assertEqual(list(self.profile_directory.glob(".verify-*.cfg")), [])

    def test_wrong_resolution_is_not_reported_as_success(self):
        self.write_valid_profiles()
        wrong_resolution = DESK_PROFILE.replace("Width=1920", "Width=1280")
        self.runner.snapshot_contents = [
            DESK_PROFILE,
            wrong_resolution,
            DESK_PROFILE,
            wrong_resolution,
        ]

        with self.assertRaisesRegex(DisplayControlError, "falsche Einstellungen"):
            self.controller.switch("desk")

        self.assertEqual(
            self.recorded_commands().count([str(self.display_switch), "/extend"]),
            3,
        )

    def test_missing_target_monitor_never_disables_working_displays(self):
        self.write_valid_profiles()
        self.runner.snapshot_contents = [TV_PROFILE, TV_PROFILE]

        with self.assertRaisesRegex(
            DisplayControlError,
            "Zielmonitore wurden nicht vollständig aktiviert",
        ):
            self.controller.switch("desk")

        commands = self.recorded_commands()
        self.assertFalse(any(command[1] == "/disable" for command in commands))

    def test_preflight_polls_until_target_monitors_are_visible(self):
        self.write_valid_profiles()
        self.controller.verification_attempts = 3
        self.runner.snapshot_contents = [TV_PROFILE, DESK_PROFILE, DESK_PROFILE]

        result = self.controller.switch("desk")

        self.assertIn("feste Monitor-IDs", result)
        commands = self.recorded_commands()
        self.assertEqual(
            commands.count([str(self.display_switch), "/extend"]),
            1,
        )
        self.assertEqual(
            sum(command[1] == "/SaveConfig" for command in commands),
            3,
        )

    def test_extend_is_given_time_to_settle_before_id_based_commands(self):
        self.write_valid_profiles()
        self.runner.snapshot_content = DESK_PROFILE
        waits = []
        self.controller._sleeper = lambda seconds: waits.append(
            (seconds, len(self.runner.calls))
        )

        self.controller.switch("desk")

        self.assertEqual(waits[0], (1, 1))
        self.assertEqual(self.recorded_commands()[1][1], "/enable")

    def test_final_verification_requires_consecutive_matches(self):
        self.write_valid_profiles()
        self.controller.verification_attempts = 6
        self.controller.verification_stable_samples = 3
        self.runner.snapshot_contents = (
            [DESK_PROFILE] * 3
            + [DESK_PROFILE, TV_PROFILE, DESK_PROFILE, DESK_PROFILE, DESK_PROFILE]
        )

        result = self.controller.switch("desk")

        self.assertIn("feste Monitor-IDs", result)
        commands = self.recorded_commands()
        self.assertEqual(sum(command[1] == "/SaveConfig" for command in commands), 8)
        self.assertEqual(commands.count([str(self.display_switch), "/extend"]), 1)

    def test_transient_profile_matches_are_not_reported_as_success(self):
        self.write_valid_profiles()
        self.controller.verification_attempts = 3
        self.controller.verification_stable_samples = 3
        self.runner.snapshot_contents = (
            [DESK_PROFILE] * 3 + [DESK_PROFILE, TV_PROFILE, DESK_PROFILE]
        ) * 2

        with self.assertRaisesRegex(DisplayControlError, "noch nicht stabil"):
            self.controller.switch("desk")

        commands = self.recorded_commands()
        self.assertEqual(sum(command[1] == "/SaveConfig" for command in commands), 12)
        self.assertEqual(commands[-1], [str(self.display_switch), "/extend"])

    def test_transient_activation_does_not_disable_working_displays(self):
        self.write_valid_profiles()
        self.controller.verification_attempts = 3
        self.controller.verification_stable_samples = 3
        self.runner.snapshot_contents = [TV_PROFILE, DESK_PROFILE, DESK_PROFILE] * 2

        with self.assertRaisesRegex(DisplayControlError, "noch nicht stabil"):
            self.controller.switch("desk")

        self.assertFalse(
            any(command[1] == "/disable" for command in self.recorded_commands())
        )

    def test_invalid_snapshot_resets_verification_streak(self):
        self.write_valid_profiles()
        self.controller.verification_attempts = 4
        self.controller.verification_stable_samples = 2
        self.runner.snapshot_contents = (
            [DESK_PROFILE] * 2
            + [DESK_PROFILE, "invalid snapshot", DESK_PROFILE, DESK_PROFILE]
        )

        result = self.controller.switch("desk")

        self.assertIn("feste Monitor-IDs", result)
        self.assertEqual(
            sum(command[1] == "/SaveConfig" for command in self.recorded_commands()),
            6,
        )

    def test_active_monitor_without_hardware_id_is_rejected(self):
        broken_profile = DESK_PROFILE.replace(
            f"MonitorID={DESK_SECONDARY_ID}",
            "MonitorID=",
        )
        self.write_monitor_profile("desk", broken_profile)

        with self.assertRaisesRegex(DisplayControlError, "ohne stabile Monitor-ID"):
            self.controller.switch("desk")

        self.assertEqual(self.runner.calls, [])

    def test_native_extend_mode_uses_argument_list(self):
        self.controller.switch("extend")

        self.assertEqual(
            self.runner.calls[0][0],
            [str(self.display_switch), "/extend"],
        )

    def test_nonzero_exit_code_is_reported(self):
        self.write_monitor_profile("desk", DESK_PROFILE)
        self.controller._runner = RecordingRunner(returncode=7)

        with self.assertRaisesRegex(DisplayControlError, "Code 7.*simulated error"):
            self.controller.switch("desk")

    def test_silent_save_failure_does_not_replace_previous_profile(self):
        profile = self.write_profile("desk")
        self.controller._runner = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr=""
        )

        with self.assertRaisesRegex(DisplayControlError, "keine gültige Profildatei"):
            self.controller.save_profile("desk")

        self.assertEqual(profile.read_text(encoding="utf-8"), "monitor configuration")

    def test_resave_keeps_previous_profile_as_backup(self):
        profile = self.write_profile("desk")

        self.controller.save_profile("desk")

        self.assertEqual(
            self.controller.backup_path("desk").read_text(encoding="utf-8"),
            "monitor configuration",
        )
        self.assertEqual(
            profile.read_text(encoding="utf-8"),
            "saved monitor configuration",
        )

    def test_previous_profile_can_be_restored(self):
        profile = self.write_profile("desk")
        self.controller.save_profile("desk")

        restored = self.controller.restore_profile_backup("desk")

        self.assertEqual(restored, profile)
        self.assertEqual(
            restored.read_text(encoding="utf-8"),
            "monitor configuration",
        )

    def test_missing_backup_is_reported(self):
        with self.assertRaisesRegex(DisplayControlError, "keine Sicherung"):
            self.controller.restore_profile_backup("desk")

    def test_unwritable_profile_location_is_reported_as_display_error(self):
        blocking_file = self.root / "not a directory"
        blocking_file.write_text("blocking file", encoding="utf-8")
        self.controller.profile_directory = blocking_file / "profiles"

        with self.assertRaisesRegex(DisplayControlError, "konnte nicht gespeichert"):
            self.controller.save_profile("desk")

    def test_parallel_command_is_rejected(self):
        self.controller._command_lock.acquire()
        try:
            with self.assertRaises(DisplayControlBusyError):
                self.controller.switch("extend")
        finally:
            self.controller._command_lock.release()

    def test_unknown_profile_name_is_rejected(self):
        with self.assertRaisesRegex(DisplayControlError, "Unbekanntes Monitorprofil"):
            self.controller.profile_path("arbitrary")


if __name__ == "__main__":
    unittest.main()
