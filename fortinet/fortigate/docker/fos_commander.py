import datetime
import os
import re
import time
from collections import deque
from contextlib import contextmanager

from common import FOSCliState, LICENSED_HOSTNAME_REGEX, DEFAULT_HOSTNAME_REGEX, Credentials
from config_diff import diff_config, normalize_line
from terminal import Terminal

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"
INIT_CONFIG_FILE = "/tmp/initial.conf"
CURRENT_CONFIG_FILE = "/config/current.conf"
CURRENT_RAW_FILE = "/tmp/current.raw"
CURRENT_CLEAN_FILE = "/tmp/current.clean"
LIC_FILE = "appliance.lic"
CONFIG_CAPTURE_TIMEOUT = 60
CURRENT_PASSWORD_PATTERN = rb"(?mi)^(?:Please enter current administrator password|Current Password):?\s*$"
LICENSE_STATUS_PATTERN = rb"(?mi)^License Status:\s*(.+?)\s*\r?$"
LEGACY_LICENSE_STATUS_PATTERN = rb"(?mi)^License:\s*(.+?)\s*\r?$"
LICENSE_STATUS_PENDING = "pending"
FOS_LICENSE_STATUS_TIMEOUT_SECONDS = 180
LICENSE_STATUS_POLL_INTERVAL_SECONDS = 2
ADMIN_SESSIONS_REMOVED_PATTERN = (
    rb"\*ATTENTION\*: Admin sessions removed because license registration status changed.*"
)
COMMAND_PARSE_ERROR_PATTERN = rb"(?mi)command parse error.*"


class FOSCommander:
    """
    Dispatches commands after login to FOS CLI.
    """

    def __init__(self, terminal: Terminal, logger, mgmt_address_ipv4, mgmt_gw_ipv4, mgmt_address_ipv6, mgmt_gw_ipv6,
                 mgmt_passthrough, hostname, state_patterns, credentials: Credentials,
                 desired_credentials: Credentials) -> None:
        super().__init__()
        self._desired_credentials = desired_credentials
        self._credentials = credentials
        self._state_patterns = state_patterns
        self._waiting_for_state = []
        self._hostname = hostname
        self._mgmt_passthrough = mgmt_passthrough
        self._mgmt_gw_ipv6 = mgmt_gw_ipv6
        self._mgmt_address_ipv6 = mgmt_address_ipv6
        self._mgmt_gw_ipv4 = mgmt_gw_ipv4
        self._mgmt_address_ipv4 = mgmt_address_ipv4
        self._logger = logger
        self._terminal = terminal
        self._start_time = datetime.datetime.now()
        self._ready_for_traffic = False
        self._cmd_queue = deque()
        self._disks_formatted = 0
        self._mgmt_dns_primary = os.getenv("FOS_MGMT_DNS_PRIMARY", "1.1.1.1")
        self._mgmt_dns_secondary = os.getenv(
            "FOS_MGMT_DNS_SECONDARY",
            # Keep the originally documented misspelling working for existing labs.
            os.getenv("FOS_MGMG_DNS_SECONDARY", "8.8.8.8"),
        )
        self._no_enc_config = os.getenv("FOS_NO_ENC_CONFIG", "false").lower() in (
            "1", "true", "yes", "on"
        )
        # The first additional disk is always automatically formatted.
        self._disks_to_format = max(0, len(os.getenv("FOS_DISK_SPECS", "").split(",")) - 1)
        if self._disks_to_format > self._disks_formatted:
            self._cmd_queue.appendleft(self._format_next_disk)
        self._cmd_queue.append(self._configure_sys_if)
        self._cmd_queue.append(self._set_mgmt_dns)
        self.check_license_exists()
        self._cmd_queue.append(self._update_hostname)
        self._cmd_queue.append(self._add_admin)
        self._cmd_queue.append(self._unset_mgmt_dns)
        self._cmd_queue.append(self._capture_blank_config)
        self._cmd_queue.append(self._apply_startup_config)
        # Keep bootstrap/config capture output unpaginated, then leave the
        # interactive console in FortiOS's normal paginated mode.
        self._cmd_queue.append(self._restore_paging)


    def cli_state_seen(self, cli_state):
        if cli_state in self._waiting_for_state:
            self._logger.info(
                f"Observed awaited CLI state {cli_state.name}; clearing wait guard."
            )
            self._waiting_for_state.clear()

    def _set_wait_for(self, *states):
        self._waiting_for_state = list(dict.fromkeys(states))
        state_names = ", ".join(state.name for state in self._waiting_for_state)
        self._logger.info(f"Waiting for CLI state: {state_names}")

    def run_cmd(self):
        if len(self._waiting_for_state) > 0:
            return

        try:
            next_cmd = self._cmd_queue.popleft()
        except IndexError:
            self._cmd_queue.append(self._ready)
            return
        next_cmd()

    def logout(self):
        self._cmd_queue.appendleft(lambda: self._terminal.wait_write("exit", wait=None))

    def save_config(self):
        try:
            self._wait_for_prompt_sync("Timed out waiting for command prompt before config capture.")
            self._save_config()
        finally:
            self._terminal.close()

    def _format_next_disk(self):
        if self._disks_to_format == self._disks_formatted:
            self._logger.info("Done formatting disks.")
            return

        disk_number = self._disks_formatted + 2
        self._logger.info(f"Formatting disk #{disk_number}")
        self._terminal.wait_write("exe disk list", wait=None)
        disk_list_output, complete = self._read_until_pattern(
            self._state_patterns[FOSCliState.CMD_PROMPT.value],
            time.monotonic() + 10,
        )
        # We took the cmd prompt from the buffer, this regenerates it so the FOSCliDriver can detect it.
        self._terminal.wait_write("", wait=None)
        if not complete:
            self._logger.error("Timed out waiting for disk list output.")
            raise RuntimeError("Timed out waiting for disk list output.")

        disk_ref = self._disk_ref_from_list(disk_list_output, disk_number)
        self._terminal.wait_write(f"exe disk format {disk_ref}", wait=None)
        self._terminal.wait_write(f"y", wait="continue")
        self._disks_formatted += 1
        if self._disks_to_format > self._disks_formatted:
            self._cmd_queue.appendleft(self._format_next_disk)
        self._set_wait_for(FOSCliState.REBOOTING)

    def _disk_ref_from_list(self, disk_list_output, disk_number):
        disk_names = [
            f"Virtual-Disk{disk_number}".encode(),
            f"HD{disk_number}".encode(),
        ]
        disk_line_match = re.search(
            rb"(?m)^Disk\s+(?:"
            + rb"|".join(re.escape(name) for name in disk_names)
            + rb")\s+ref:\s+(\d+)\b.*$",
            disk_list_output,
        )
        if not disk_line_match:
            expected_names = " or ".join(name.decode() for name in disk_names)
            self._logger.error(
                f"Could not find {expected_names} in disk list output: {disk_list_output.decode(errors='replace')}"
            )
            raise RuntimeError(f"Could not find {expected_names} in disk list output.")
        return disk_line_match.group(1).decode()

    def _set_mgmt_dns(self):
        self._terminal.wait_write("config system dns\r"
                                  f"set primary {self._mgmt_dns_primary}\r"
                                  f"set secondary {self._mgmt_dns_secondary}\r"
                                  "end",
                                  wait=None)

    def _toggle_paging(self, enabled):
        output_mode = "more" if enabled else "standard"
        command = "config system console\r" f"set output {output_mode}\r" "end"
        self._terminal.write((command + "\r").encode())

    def _restore_paging(self):
        self._toggle_paging(True)

    @contextmanager
    def _pagination_disabled(self, restore_paging=True, wait_for_transition=True):
        """Temporarily disable console pagination for machine-readable output."""
        try:
            if restore_paging:
                self._toggle_paging(False)
                if wait_for_transition:
                    self._wait_for_prompt_sync("Timed out disabling console pagination.")
            yield
        finally:
            if restore_paging:
                try:
                    self._toggle_paging(True)
                    if wait_for_transition:
                        self._wait_for_prompt_sync("Timed out restoring console pagination.")
                except Exception as exc:
                    # License changes can invalidate the session while this
                    # context is active. The next login/bootstrap pass will
                    # restore the final console mode.
                    self._logger.warning(f"Unable to restore console pagination: {exc}")

    def _console_paging_enabled(self):
        self._terminal.write(b"show full-configuration system console\r")
        output, complete = self._read_until_pattern(
            self._state_patterns[FOSCliState.CMD_PROMPT.value],
            time.monotonic() + 10,
        )
        if not complete:
            raise RuntimeError("Timed out reading console output mode.")
        match = re.search(rb"(?m)^\s*set output (more|standard)\s*\r?$", output)
        if not match:
            self._logger.warn("Could not determine console output mode; not restoring pagination after capture.")
            return False
        return match.group(1) == b"more"

    def _wait_for_prompt_sync(self, timeout_message):
        deadline = time.monotonic() + 30
        patterns = [
            self._state_patterns[FOSCliState.CMD_PROMPT.value],
            self._state_patterns[FOSCliState.PROVIDE_USERNAME.value],
            self._state_patterns[FOSCliState.PROVIDE_PASSWORD.value],
        ]
        while time.monotonic() < deadline:
            ridx, match, _output = self._expect(patterns, deadline - time.monotonic())
            if not match:
                break
            if ridx == 0:
                return
            if ridx == 1:
                self._terminal.wait_write(self._credentials.username, wait=None)
            elif ridx == 2:
                self._terminal.wait_write(self._credentials.password, wait=None)
        raise RuntimeError(timeout_message)

    def _unset_mgmt_dns(self):
        self._terminal.wait_write("config system dns\r"
                                  "unset primary\r"
                                  "unset secondary\r"
                                  "end",
                                  wait=None)

    def _configure_sys_if(self):
        if self._mgmt_address_ipv4 == "dhcp":
            self._logger.info("MGMT IP is in DHCP mode")
            return
        self._logger.info(f"Setting mgmt IPv4={self._mgmt_address_ipv4} and IPv6={self._mgmt_address_ipv6}")
        self._terminal.wait_write("config system interface\r"
                                  "edit port1\r"
                                  "set mode static\r"
                                  f"set ip {self._mgmt_address_ipv4}\r"
                                  "set allowaccess ping https ssh http",
                                  wait=None)
        if self._mgmt_address_ipv6 is not None:
            self._terminal.wait_write("config ipv6\r"
                                      "set ip6-mode static\r"
                                      f"set ip6-address {self._mgmt_address_ipv6}\r"
                                      "set ip6-allowaccess ping https ssh http\r"
                                      "end",
                                      wait="allowaccess")
        self._terminal.wait_write("next\r"
                                  "end",
                                  wait="http")

        self._terminal.wait_write("config system fortiguard\r"
                                  "set interface-select-method specify\r"
                                  "set interface port1\r"
                                  "set auto-join-forticloud disable\r"
                                  "end", wait="end")
        self._terminal.wait_write("config router static\r"
                                  "ed 9999\r"
                                  f"set gateway {self._mgmt_gw_ipv4}\r"
                                  "set device port1\r"
                                  "next\r"
                                  "end",
                                  wait="end")
        if self._mgmt_address_ipv6 is not None:
            self._terminal.wait_write("config router static6\r"
                                      "ed 9999\r"
                                      f"set gateway {self._mgmt_gw_ipv6}\r"
                                      "set device port1\r"
                                      "next\r"
                                      "end",
                                      wait=None)
        self._terminal.wait_write("", wait="end")

    def _update_now(self):
        self._terminal.wait_write("exe update-now", wait=None)
        echo_result = self._wait_for_command_echo(
            "exe update-now",
            time.monotonic() + 5,
            session_loss_is_terminal=True,
        )
        if echo_result == "session_lost":
            return
        if echo_result != "echo":
            self._logger.warn("Timed out waiting for update-now command echo.")
            return
        if self._enter_current_password_if_asked():
            self._logger.info("Update changed the admin session; returning to the CLI.")
            return

        if not self._wait_for_license_status_ready():
            self._logger.warn("Timed out waiting for license status to change.")
            return
        self._logger.info("License status changed")

    def _wait_for_license_status_ready(self):
        # License polling is machine-readable traffic. Keep it out of the
        # mirrored serial console while retaining it in the expect buffer.
        with self._terminal.suppress_output():
            # Status output can itself be paginated on FortiProxy. Bootstrap
            # finishes in ``more`` mode, so force the temporary transition.
            with self._pagination_disabled(restore_paging=True, wait_for_transition=False):
                return self._poll_license_status()

    def _poll_license_status(self):
        deadline = time.monotonic() + FOS_LICENSE_STATUS_TIMEOUT_SECONDS
        last_status = None

        while time.monotonic() < deadline:
            self._terminal.wait_write("get system status", wait=None)
            echo_result = self._wait_for_command_echo(
                "get system status",
                deadline,
                session_loss_is_terminal=True,
            )
            if echo_result == "session_lost":
                return True
            if echo_result != "echo":
                self._logger.warn("Timed out waiting for system status command echo.")
                return False
            status_output, ridx, complete = self._read_until_patterns(
                [self._state_patterns[FOSCliState.CMD_PROMPT.value]],
                deadline,
                extra_patterns=self._license_session_loss_patterns(),
            )
            if not complete:
                self._logger.warn("Timed out waiting for system status output.")
                return False

            if ridx > 0:
                self._wait_for_cli_after_license_session_loss()
                return True

            status = self._license_status_from_system_status(status_output)
            if status:
                last_status = status
                self._logger.info(f"License status is {status}")
                if status.lower() != LICENSE_STATUS_PENDING:
                    self._terminal.wait_write("", wait=None)
                    return True
            else:
                self._logger.warn("Could not find License field in system status output.")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _, session_lost, _ = self._expect(
                self._license_session_loss_patterns(),
                min(LICENSE_STATUS_POLL_INTERVAL_SECONDS, remaining),
            )
            if session_lost:
                self._wait_for_cli_after_license_session_loss()
                return True

        if last_status:
            self._logger.warn(f"License status remained {last_status}.")
        self._terminal.wait_write("", wait=None)
        return False

    def _read_until_pattern(self, pattern, deadline):
        output, _, complete = self._read_until_patterns([pattern], deadline)
        return output, complete

    def _read_until_patterns(self, end_patterns, deadline, extra_patterns=None):
        output = b""
        extra_patterns = extra_patterns or []
        patterns = list(end_patterns) + list(extra_patterns)
        end_pattern_count = len(end_patterns)

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            (ridx, match, chunk) = self._expect(patterns, min(10, remaining))
            output += chunk
            if not match:
                return output, -1, False
            if ridx < end_pattern_count:
                return output, ridx, True
            return output, ridx, True

        return output, -1, False

    def _wait_for_command_echo(self, command, deadline, session_loss_is_terminal=False):
        command_pattern = re.escape(command.encode())
        session_loss_patterns = self._license_session_loss_patterns() if session_loss_is_terminal else []
        patterns = session_loss_patterns + [command_pattern]
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            (ridx, match, _) = self._expect(patterns, min(5, remaining))
            if not match:
                continue
            if ridx < len(session_loss_patterns):
                self._wait_for_cli_after_license_session_loss()
                return "session_lost"
            return "echo"
        return "timeout"

    def _license_session_loss_patterns(self):
        return [ADMIN_SESSIONS_REMOVED_PATTERN]

    def _wait_for_cli_after_license_session_loss(self):
        self._logger.info(
            "License registration changed the admin session; "
            "waiting for the next CLI prompt."
        )
        self._set_wait_for(
            FOSCliState.PROVIDE_USERNAME,
            FOSCliState.PROVIDE_PASSWORD,
            FOSCliState.CREDENTIAL_ACCEPTED,
        )

    def _license_status_from_system_status(self, status_output):
        match = re.search(LICENSE_STATUS_PATTERN, status_output)
        if not match:
            match = re.search(LEGACY_LICENSE_STATUS_PATTERN, status_output)
        if not match:
            return None
        return match.group(1).decode(errors="replace").strip()

    def _expect(self, patterns, timeout):
        return self._terminal.expect(patterns, timeout)

    def _move_mgmt_to_vrf1(self):
        self._terminal.wait_write("config system interface\r"
                                  "edit port1",
                                  wait=None)
        prompt_pattern = self._state_patterns[FOSCliState.CMD_PROMPT.value]
        self._expect([prompt_pattern], 10)
        self._terminal.wait_write("set vrf 1", wait=None)
        vrf_supported = self._vrf_command_succeeded()
        if vrf_supported is None:
            return
        self._terminal.wait_write("next\r"
                                  "end", wait=None)

        route = ("config router static\r"
                 "edit 9999\r")
        if vrf_supported:
            route += "set vrf 1\r"
        else:
            # FortiProxy has no interface VRF. Keep the management route
            # specific so it cannot replace a lab-provided default route.
            route += f"set dst {self._mgmt_gateway_destination(self._mgmt_gw_ipv4, self._mgmt_address_ipv4)}\r"
        route += "next\rend"
        self._terminal.wait_write(route, wait=None)
        if self._mgmt_address_ipv6 is not None:
            route6 = ("config router static6\r"
                      "edit 9999\r")
            if vrf_supported:
                route6 += "set vrf 1\r"
            else:
                route6 += f"set dst {self._mgmt_gateway_destination(self._mgmt_gw_ipv6, self._mgmt_address_ipv6)}\r"
            self._terminal.wait_write(route6 + "next\rend", wait=None)

    @staticmethod
    def _mgmt_gateway_destination(gateway, mgmt_address):
        """Apply the management address prefix to a gateway route."""
        _address, separator, prefix = str(mgmt_address).partition("/")
        if separator and prefix:
            return f"{gateway}/{prefix}"
        default_prefix = "128" if ":" in str(gateway) else "32"
        return f"{gateway}/{default_prefix}"

    def _vrf_command_succeeded(self):
        prompt_pattern = self._state_patterns[FOSCliState.CMD_PROMPT.value]
        ridx, _match, _output = self._expect(
            [
                ADMIN_SESSIONS_REMOVED_PATTERN,
                COMMAND_PARSE_ERROR_PATTERN,
                prompt_pattern,
            ],
            10,
        )
        if ridx == 0:
            self._wait_for_cli_after_license_session_loss()
            return None
        if ridx == 1:
            self._logger.info("VRF is unsupported; using a management-gateway host route.")
            return False
        if ridx == 2:
            return True
        self._logger.warning("Timed out checking VRF support; using a management-gateway host route.")
        return False

    def _update_hostname(self):
        self._terminal.wait_write("config system global", wait=None)
        self._terminal.wait_write("set admin-scp enable", wait="global")
        hostname_command = "set hostname " + self._hostname
        self._terminal.wait_write(hostname_command, wait="admin-scp")
        self._terminal.wait_write("end", wait=hostname_command)
        self._state_patterns[FOSCliState.PROVIDE_USERNAME.value] = (
                rb"\n" + self._hostname.encode("utf-8") +
                rb"(?:\((?:Primary|Secondary)\))?" +
                rb"\s+login:\s*")
        self._state_patterns[FOSCliState.CMD_PROMPT.value] = (
                rb"(?m)^ ?"
                + re.escape(self._hostname.encode("utf-8"))
                + rb" ?"
                + rb"(?:\s+\((?:STS|Interim)\))?"
                + rb" ?[#$] ?"
        )

    def _setup_license(self):
        if self._mgmt_passthrough:
            tftp_server_ip = self._mgmt_gw_ipv4
        else:
            tftp_server_ip = self._mgmt_gw_ipv4

        self._logger.info(f"Setting up license {LIC_FILE} from server {tftp_server_ip}")
        self._terminal.wait_write(f"exe restore vmlicense tftp {LIC_FILE} {tftp_server_ip}", wait=None)
        self._terminal.wait_write("y", wait="Do you want to continue?")
        self._state_patterns[FOSCliState.PROVIDE_USERNAME.value] = (
                rb"\n(?:" + LICENSED_HOSTNAME_REGEX + b"|" + DEFAULT_HOSTNAME_REGEX + rb") ?" +
                rb"(?:\((?:Primary|Secondary)\))?" +
                rb"\s+login:\s*")
        self._state_patterns[FOSCliState.CMD_PROMPT.value] = (
                rb"(?m)^ ?"
                + rb"(?:" + LICENSED_HOSTNAME_REGEX + b"|" + DEFAULT_HOSTNAME_REGEX + b")"
                + rb" ?"
                + rb"(?:\s+\((?:STS|Interim)\))?"
                + rb" ?[#$] ?"
        )
        self._set_wait_for(FOSCliState.REBOOTING, FOSCliState.LIC_FAIL)
        self._cmd_queue.appendleft(self._move_mgmt_to_vrf1)
        # Reapply management config after the license update, which can remove
        # the static route while changing registration state.
        self._cmd_queue.appendleft(self._configure_sys_if)
        self._cmd_queue.appendleft(self._update_now)

    def _apply_startup_config(self):
        """Load additional config provided by user."""

        if not os.path.exists(STARTUP_CONFIG_FILE):
            self._logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} is not found")
            return

        self._logger.trace(f"Configuring with startup-config from file: {STARTUP_CONFIG_FILE}")
        config_lines = []
        with open(STARTUP_CONFIG_FILE) as file:
            config_lines = file.readlines()

        config_stack = deque()
        wait_for = None
        for line in config_lines:
            r_stripped = line.rstrip()
            full_stripped = r_stripped.lstrip()
            _wait_for = None
            if full_stripped.startswith("config"):
                config_stack.append("c")
            if full_stripped.startswith("edit"):
                config_stack.append("e")
            if full_stripped.startswith("next") or full_stripped.startswith("end"):
                top = config_stack.pop()
                _wait_for = full_stripped
                if full_stripped.startswith("next") and top != "e":
                    self._logger.error("Startup config malformed. \"next\" command outside of edit scope.")
                    raise ValueError("Startup config malformed. \"next\" command outside of edit scope.")
                if full_stripped.startswith("end") and top != "c":
                    self._logger.error("Startup config malformed. \"end\" command outside of config scope.")
                    raise ValueError("Startup config malformed. \"end\" command outside of config scope.")
            # Maintaining nesting produces a more appealing debug log.
            self._terminal.wait_write(r_stripped, wait=wait_for)
            wait_for = _wait_for

        if len(config_stack) > 0:
            raise ValueError("Startup config malformed. Unmatched config or edit brackets.")

    def capture_config(self):
        self._logger.info("Capturing FortiOS config over serial with plain show")
        with self._terminal.suppress_output():
            with self._pagination_disabled():
                return self._capture_config_from_session(self._terminal)

    def _capture_blank_config(self):
        config = self.capture_config()
        self._write_config_file(INIT_CONFIG_FILE, config)

    def _save_config(self):
        blank_config = self._read_config_file(INIT_CONFIG_FILE)
        current_config = self.capture_config()
        self._write_config_file(CURRENT_CLEAN_FILE, current_config)
        changed_config = self._current_side_config_delta(blank_config, current_config)
        self._write_config_file(CURRENT_CONFIG_FILE, changed_config)
        self._logger.debug("Current config written")

    def _current_side_config_delta(self, blank_config, current_config):
        return diff_config(
            blank_config,
            current_config,
            track_encrypted_changes=not getattr(self, "_no_enc_config", False),
        )

    def _normalize_config_line(self, line):
        return normalize_line(line)

    def _clean_show_output(self, output):
        config = output.replace("\r\n", "\n")
        config = config.replace("\r", "\n")
        config = config.replace("^H", "")
        config = re.sub(r"\x08+", "", config)
        config = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", config)
        # FortiOS metadata/comments are not replayable configuration and must
        # not enter either the baseline or the generated delta.
        lines = [line for line in config.splitlines() if not line.strip().startswith("#")]
        if lines and lines[0].strip() == "show":
            lines = lines[1:]
        return "\n".join(lines).strip()

    def _capture_config_from_session(self, session):
        prompt_pattern = self._state_patterns[FOSCliState.CMD_PROMPT.value]
        session.write(b"show\r")
        output = self._read_show_output(session, prompt_pattern)
        self._write_config_file(CURRENT_RAW_FILE, output.decode(errors="replace"))
        return self._clean_show_output(output.decode(errors="replace"))

    def _read_show_output(self, session, prompt_pattern):
        _, match, output = session.expect([prompt_pattern], CONFIG_CAPTURE_TIMEOUT)
        if not match:
            raise RuntimeError("Timed out waiting for config capture output.")
        return output[:match.start()]

    def _read_config_file(self, path):
        with open(path) as config_file:
            return config_file.read()

    def _write_config_file(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as config_file:
            config_file.write(content)
            if content and not content.endswith("\n"):
                config_file.write("\n")

    def check_license_exists(self):
        try:
            os.stat(f"/tftpboot/{LIC_FILE}")
            self._cmd_queue.append(self._setup_license)
        except FileNotFoundError:
            pass

    def _add_admin(self):
        username = self._desired_credentials.username
        password = self._desired_credentials.password
        self._logger.info(f"Configuring admin '{username}'")

        try:
            self._terminal.wait_write("config system password-policy\r"
                                      "set status disable\r"
                                      "end",
                                      wait=None)

            self._terminal.wait_write("config system admin", wait=None)
            self._terminal.wait_write(f"edit {username}", wait=None)
            self._terminal.wait_write("set accprofile super_admin", wait=None)
            if len(password) > 0:
                self._terminal.wait_write(f"set password {password}", wait="super_admin")
                if self._enter_current_password_if_asked():
                    return
            elif username == "admin":
                self._terminal.wait_write(f"unset password", wait="super_admin")
                if self._enter_current_password_if_asked():
                    return

            self._terminal.wait_write("next", wait=None)
            if self._enter_current_password_if_asked():
                return

            # Leave the resulting prompt buffered for the CLI state machine.
            self._terminal.wait_write("end", wait=None)
        finally:
            self._activate_desired_credentials()

    def _activate_desired_credentials(self):
        self._credentials.username = self._desired_credentials.username
        self._credentials.password = self._desired_credentials.password

    def _enter_current_password_if_asked(self):
        patterns = [
            CURRENT_PASSWORD_PATTERN,
            ADMIN_SESSIONS_REMOVED_PATTERN,
        ]
        (ridx, _, _) = self._expect(patterns, 2)
        if ridx == 1:
            self._logger.info("Admin configuration dropped the session; waiting for login.")
            self._set_wait_for(FOSCliState.CREDENTIAL_ACCEPTED)
            return True
        if ridx == 0:
            # We are still authenticated with the current credentials at this point.
            self._terminal.wait_write(self._credentials.password, wait=None)
        return False

    def _ready(self):
        self._ready_for_traffic = True
        self._terminal.close()
        # calc startup time
        startup_time = datetime.datetime.now() - self._start_time
        self._logger.info(f"Startup complete in {startup_time}")

    @property
    def ready(self):
        return self._ready_for_traffic
