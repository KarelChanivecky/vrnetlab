import datetime
import os
import re
import time
from collections import deque

import vrnetlab
from fos_state import FOSCliState, OLD_LIC_HOSTNAME_REGEX, DEFAULT_HOSTNAME_REGEX

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"
LIC_FILE = "appliance.lic"
CURRENT_PASSWORD_PATTERN = rb"(?mi)^(?:Please enter current administrator password|Current Password):?\s*$"
LICENSE_STATUS_PATTERN = rb"(?mi)^License(?: Status)?:\s*(.+?)\s*\r?$"
LICENSE_STATUS_PENDING = "pending"
LICENSE_STATUS_TIMEOUT_SECONDS = int(os.getenv("FOS_LICENSE_STATUS_TIMEOUT_SECONDS", "90"))
LICENSE_STATUS_POLL_INTERVAL_SECONDS = 2
ADMIN_SESSIONS_REMOVED_PATTERN = (
    rb"(?m)^\*ATTENTION\*: Admin sessions removed because license registration status changed.*\r?$"
)


class FOSCommander:
    """
    Dispatches commands after login to FOS CLI.
    """

    def __init__(self, terminal: vrnetlab.VM, logger, mgmt_address_ipv4, mgmt_gw_ipv4, mgmt_address_ipv6, mgmt_gw_ipv6,
                 mgmt_passthrough, hostname, waiting_for_state, state_patterns) -> None:
        super().__init__()
        self._state_patterns = state_patterns
        self._waiting_for_state: list = waiting_for_state
        self._hostname = hostname
        self._mgmt_passthrough = mgmt_passthrough
        self._mgmt_gw_ipv6 = mgmt_gw_ipv6
        self._mgmt_address_ipv6 = mgmt_address_ipv6
        self._mgmt_gw_ipv4 = mgmt_gw_ipv4
        self._mgmt_address_ipv4 = mgmt_address_ipv4
        self._logger = logger
        self._terminal = terminal
        self._start_time = datetime.datetime.now()
        self._cmd_queue = deque()
        self._disks_formatted = 0
        # The first additional disk is always automatically formatted.
        self._disks_to_format = max(0, len(os.getenv("FOS_DISK_SPECS", "").split(",")) - 1)
        if self._disks_to_format > self._disks_formatted:
            self._cmd_queue.appendleft(self._format_next_disk)
        self._cmd_queue.append(self._configure_sys_if)
        self._cmd_queue.append(self._setup_default_dns)
        self.check_license_exists()
        self._cmd_queue.append(self._update_hostname)
        self._cmd_queue.append(self._apply_startup_config)

    def run_cmd(self):
        try:
            next_cmd = self._cmd_queue.popleft()
        except IndexError:
            self._cmd_queue.append(self._ready)
            return
        next_cmd()

    def logout(self):
        self._cmd_queue.appendleft(lambda: self._terminal.wait_write("exit", wait=None))

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
            self._terminal.stop()
            raise RuntimeError("Timed out waiting for disk list output.")

        disk_ref = self._disk_ref_from_list(disk_list_output, disk_number)
        self._terminal.wait_write(f"exe disk format {disk_ref}", wait=None)
        self._terminal.wait_write(f"y", wait="continue")
        self._disks_formatted += 1
        if self._disks_to_format > self._disks_formatted:
            self._cmd_queue.appendleft(self._format_next_disk)
        self._waiting_for_state.clear()
        self._waiting_for_state.extend([FOSCliState.REBOOTING])

    def _disk_ref_from_list(self, disk_list_output, disk_number):
        disk_name = f"Virtual-Disk{disk_number}".encode()
        disk_line_match = re.search(
            rb"(?m)^Disk\s+" + re.escape(disk_name) + rb"\s+ref:\s+(\d+)\b.*$",
            disk_list_output,
        )
        if not disk_line_match:
            self._logger.error(
                f"Could not find {disk_name.decode()} in disk list output: {disk_list_output.decode(errors='replace')}"
            )
            self._terminal.stop()
            raise RuntimeError(f"Could not find {disk_name.decode()} in disk list output.")
        return disk_line_match.group(1).decode()

    def _setup_default_dns(self):
        self._terminal.wait_write("config system dns\r"
                                  "set primary 1.1.1.1\r"
                                  "set secondary 8.8.8.8\r"
                                  "end",
                                  wait=None)

    def _unset_default_dns(self):
        self._terminal.wait_write("config system dns\r"
                                  "set primary 1.1.1.1\r"
                                  "set secondary 8.8.8.8\r"
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
        echo_result = self._wait_for_command_echo("exe update-now", time.monotonic() + 5)
        if echo_result != "echo":
            self._logger.warn("Timed out waiting for update-now command echo.")
            return
        res = self._enter_current_password_if_asked()
        if res == "session_lost":
            self._logger.warn("Timed out waiting for update-now to return to the command prompt.")
            return

        if not self._wait_for_license_status_ready():
            self._logger.warn("Timed out waiting for license status to change.")
            return
        self._logger.info("License status changed")

    def _wait_for_license_status_ready(self):
        deadline = time.monotonic() + LICENSE_STATUS_TIMEOUT_SECONDS
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

            session_removed = re.search(ADMIN_SESSIONS_REMOVED_PATTERN, status_output)
            status = self._license_status_from_system_status(status_output)
            if status:
                last_status = status
                self._logger.info(f"License status is {status}")
                if session_removed:
                    self._handle_license_session_removed()
                    return True
                if status.lower() != LICENSE_STATUS_PENDING:
                    self._terminal.wait_write("", wait=None)
                    return True
            else:
                self._logger.warn("Could not find License field in system status output.")

            if ridx > 0 or session_removed:
                self._handle_license_session_removed()
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(LICENSE_STATUS_POLL_INTERVAL_SECONDS, remaining))

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
                self._handle_license_session_removed()
                return "session_lost"
            return "echo"
        return "timeout"

    def _license_session_loss_patterns(self):
        return [
            ADMIN_SESSIONS_REMOVED_PATTERN,
            self._state_patterns[FOSCliState.PROVIDE_USERNAME.value],
            self._state_patterns[FOSCliState.PROVIDE_PASSWORD.value],
        ]

    def _handle_license_session_removed(self):
        self._logger.info("License registration removed the admin session; waiting for login.")
        self._clear_stale_license_poll_login()
        self._waiting_for_state.clear()
        self._waiting_for_state.extend([FOSCliState.PROVIDE_USERNAME])

    def _clear_stale_license_poll_login(self):
        # The poll command can race with FortiOS removing the admin session. In
        # that case "get system status" is accepted as a username, and FortiOS
        # waits at Password:. Clear that failed login before returning to the FSM.
        deadline = time.monotonic() + 5
        saw_password = False
        patterns = [
            self._state_patterns[FOSCliState.PROVIDE_PASSWORD.value],
            self._state_patterns[FOSCliState.PROVIDE_USERNAME.value],
        ]

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ridx, match, _ = self._expect(patterns, min(1, remaining))
            if not match:
                continue
            if ridx == 0:
                self._terminal.wait_write("", wait=None)
                saw_password = True
                break

        if not saw_password:
            self._terminal.wait_write("", wait=None)

    def _license_status_from_system_status(self, status_output):
        match = re.search(LICENSE_STATUS_PATTERN, status_output)
        if not match:
            return None
        return match.group(1).decode(errors="replace").strip()

    def _expect(self, patterns, timeout):
        result = self._terminal.tn.expect(patterns, timeout)
        output = result[2]
        self._logger.debug(f"OUT: {output.decode(errors='replace')}")
        return result

    def _move_mgmt_to_vrf1(self):
        self._terminal.wait_write("config system interface\r"
                                  "edit port1",
                                  wait=None)
        self._terminal.wait_write("set vrf 1\r"
                                  "next\r"
                                  "end", wait="port1")
        self._terminal.wait_write("config router static\r"
                                  "edit 9999\r"
                                  "set vrf 1\r"
                                  "next\r"
                                  "end", wait=None)
        if self._mgmt_address_ipv6 is not None:
            self._terminal.wait_write("config router static6\r"
                                      "edit 9999\r"
                                      "set vrf 1\r"
                                      "next\r"
                                      "end", wait=None)

    def _update_hostname(self):
        self._terminal.wait_write("config system global", wait=None)
        hostname_command = "set hostname " + self._hostname
        self._terminal.wait_write(hostname_command, wait="global")
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
                rb"\n(?:" + OLD_LIC_HOSTNAME_REGEX + b"|" + DEFAULT_HOSTNAME_REGEX + rb") ?" +
                rb"(?:\((?:Primary|Secondary)\))?" +
                rb"\s+login:\s*")
        self._state_patterns[FOSCliState.CMD_PROMPT.value] = (
                rb"(?m)^ ?"
                + rb"(?:" + OLD_LIC_HOSTNAME_REGEX + b"|" + DEFAULT_HOSTNAME_REGEX + b")"
                + rb" ?"
                + rb"(?:\s+\((?:STS|Interim)\))?"
                + rb" ?[#$] ?"
        )
        self._waiting_for_state.clear()
        self._waiting_for_state.extend([FOSCliState.REBOOTING, FOSCliState.LIC_FAIL])
        self._cmd_queue.appendleft(self._unset_default_dns)
        self._cmd_queue.appendleft(self._move_mgmt_to_vrf1)
        self._cmd_queue.appendleft(self._update_now)
        self._cmd_queue.appendleft(self._configure_sys_if)  # Was seeing static route disappear.

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

    def check_license_exists(self):
        try:
            os.stat(f"/tftpboot/{LIC_FILE}")
            self._cmd_queue.append(self._setup_license)
        except FileNotFoundError:
            pass

    def _ready(self):
        self._terminal.running = True
        self._terminal.tn.close()
        # calc startup time
        startup_time = datetime.datetime.now() - self._start_time
        self._logger.info(f"Startup complete in {startup_time}")
