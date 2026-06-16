import datetime
import os
import re
from collections import deque

import vrnetlab
from fos_state import FOSCliState, OLD_LIC_HOSTNAME_REGEX, DEFAULT_HOSTNAME_REGEX

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"
LIC_FILE = "appliance.lic"


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

        self._cmd_queue.append(self._configure_sys_if)
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

    def _configure_sys_if(self):
        if self._mgmt_address_ipv4 == "dhcp":
            self._logger.info("MGMT IP is in DHCP mode")
            return
        self._logger.info(f"Setting mgmt IPv4={self._mgmt_address_ipv4} and IPv6={self._mgmt_address_ipv6}")
        self._terminal.wait_write("config system interface\r"
                                  "edit port1\r"
                                  "set mode static\r"
                                  f"set ip {self._mgmt_address_ipv4}\r"
                                  "set allowaccess ping https ssh http\r",
                                  wait=None)
        if self._mgmt_address_ipv6 is not None:
            self._terminal.wait_write("config ipv6\r"
                                      "set ip6-mode static\r"
                                      f"set ip6-address {self._mgmt_address_ipv6}\r"
                                      "set ip6-allowaccess ping https ssh http\r"
                                      "end",
                                      wait=None)
        self._terminal.wait_write("next\r"
                                  "end",
                                  wait=None)

        self._terminal.wait_write("config system fortiguard\r"
                                  "set interface-select-method specify\r"
                                  "set interface port1\r"
                                  "end", wait=None)
        self._terminal.wait_write("config router static\r"
                                  "ed 9999\r"
                                  f"set gateway {self._mgmt_gw_ipv4}\r"
                                  "set device port1\r"
                                  "set vrf 1\r"
                                  "end",
                                  wait=None)
        if self._mgmt_address_ipv6 is not None:
            self._terminal.wait_write("config router static6\r"
                                      "ed 9999\r"
                                      f"set gateway {self._mgmt_gw_ipv6}\r"
                                      "set device port1\r"
                                      "set vrf 1\r"
                                      "end",
                                      wait=None)

    def _update_now(self):
        self._terminal.wait_write("exe update-now\r", wait=None)

    def _move_if1_to_vrf1(self):
        self._terminal.wait_write("config system interface\r"
                                  "edit port1",
                                  wait=None)
        self._terminal.wait_write("set vrf 1\r"
                                  "next\r"
                                  "end", wait="(port1)")

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
        self._cmd_queue.appendleft(self._move_if1_to_vrf1)
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
