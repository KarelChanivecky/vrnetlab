import os
import re
import time

import vrnetlab
from common import FOSCliState, FOS_CLI_STATE_PATTERNS, Credentials, DEFAULT_USERNAME, DEF_POLICY_COMPLIANT_PASSWORD, \
    DEFAULT_PASSWORD, LineBuffer
from fos_commander import FOSCommander


class FOSCliDriver:
    """
    Drives the CLI through boot, login, and ready-for-cmd states.
    """

    def __init__(self, terminal: vrnetlab.VM, mgmt_passthrough, username, password, logger, mgmt_address_ipv4,
                 mgmt_gw_ipv4, mgmt_address_ipv6, mgmt_gw_ipv6, hostname) -> None:
        super().__init__()
        self._idle_spins = 0
        self._logger = logger
        pwd = password
        if pwd is None:  # emtpy string is falsy, so the or trick doesn't work.
            pwd = DEFAULT_PASSWORD
        self._desired_credentials = Credentials(username or DEFAULT_USERNAME, pwd)
        self._credentials = Credentials(DEFAULT_USERNAME, DEF_POLICY_COMPLIANT_PASSWORD)
        self._username = DEFAULT_USERNAME
        self._terminal = terminal
        self._mgmt_passthrough = mgmt_passthrough
        self._log_encoded = os.getenv("FOS_LOG_ENCODED", "false").lower() == "true"
        self._state_handlers = {
            FOSCliState.PROVIDE_USERNAME: self._provide_username,
            FOSCliState.PROVIDE_PASSWORD: self._provide_password,
            FOSCliState.CHANGE_PASSWORD: self._change_password,
            FOSCliState.CREDENTIAL_REJECTED: self._credential_rejected,
            FOSCliState.CREDENTIAL_ACCEPTED: self._credential_accepted,
            FOSCliState.LIC_FAIL: self._license_fail,
            FOSCliState.CMD_PROMPT: self._cmd_prompt,
            FOSCliState.SHUTTING_DOWN: self._shutting_down,
            FOSCliState.REBOOTING: self._rebooting,
            FOSCliState.UNKNOWN: self._unknown_state,
            FOSCliState.TN_TIMEOUT: self._tn_timeout,
        }

        self._state_patterns = FOS_CLI_STATE_PATTERNS.copy()
        self._line_buffer = LineBuffer()
        self.tn_out = b""
        self._last_known_state = FOSCliState.UNKNOWN
        self._waiting_for = []
        self._commander = FOSCommander(
            terminal=terminal,
            logger=logger,
            mgmt_address_ipv4=mgmt_address_ipv4,
            mgmt_gw_ipv4=mgmt_gw_ipv4,
            mgmt_address_ipv6=mgmt_address_ipv6,
            mgmt_gw_ipv6=mgmt_gw_ipv6,
            mgmt_passthrough=mgmt_passthrough,
            hostname=hostname,
            state_patterns=self._state_patterns,
            waiting_for_state=self._waiting_for,
            credentials=self._credentials,
            desired_credentials=self._desired_credentials,
        )

        self._tried_v7_default_password = False
        self._cred_rejected = False

    def process_state(self):
        spin_start = time.time()
        # Running signals health state. Stopped signals we stopped before reaching healthy state
        while not self._terminal.stopped and time.time() < spin_start + 5:
            if self._idle_spins > 300:
                # too many spins without appropriate communication
                self._logger.warning("no output from serial console, restarting VCP")
                self._terminal.stop()
                self._idle_spins = 0
                raise RuntimeError("VM node malfunction")
            cur_state = self._next_state()

            # The FSM is actually moving along
            if cur_state.value < FOSCliState.UNKNOWN.value:
                self._idle_spins = 0
                self._last_known_state = cur_state

            # Continue to show current state while reducing verbosity
            if cur_state != FOSCliState.UNKNOWN and cur_state != FOSCliState.TN_TIMEOUT:
                self._logger.debug(f"ST: {cur_state.name}")

            if len(self.tn_out) > 0:
                log_out = self.tn_out
                if not self._log_encoded:
                    log_out = log_out.decode()
                self._logger.debug(f"OUT: {log_out}")

            if self._waiting_for and cur_state not in self._waiting_for:
                return

            self._waiting_for.clear()
            self._state_handlers[cur_state]()
            if self._terminal.running:
                return  # If reached running state, then we allow vrnetlab.VM to be more responsive to system state

    def _next_state(self):
        (_, _, res) = self._terminal.tn.expect(self._state_patterns, 1)
        self._line_buffer.put(res)

        ridx, match = self._match_buffered_state()
        self.tn_out = res
        if match:
            self.tn_out = self._line_buffer.data[:match.end()]
            self._line_buffer.clear()
            return FOSCliState(ridx)
        if res == b"":
            return FOSCliState.TN_TIMEOUT
        return FOSCliState.UNKNOWN

    def _match_buffered_state(self):
        for ridx, pattern in enumerate(self._state_patterns):
            match = re.search(pattern, self._line_buffer.data)
            if match:
                return ridx, match
        return -1, None

    def _provide_username(self):
        self._terminal.wait_write(self._credentials.username, wait=None)

    def _provide_password(self):
        if self._cred_rejected:
            self._tried_v7_default_password = True
            self._terminal.wait_write("", wait=None)
            return
        self._terminal.wait_write(self._credentials.password, wait=None)

    def _change_password(self):
        # At this time, this would be the default password.
        self._terminal.wait_write(self._credentials.password, wait=None)
        self._terminal.wait_write(self._credentials.password, wait="Confirm Password")
        # FOS 7.4 needs log out before you can use the password with ssh
        self._commander.logout()

    def save_config(self):
        self._commander.save_config()
        self._reactivate()

    def _reactivate(self):
        self._last_known_state = FOSCliState.UNKNOWN
        self._line_buffer.clear()

    def _credential_accepted(self):
        self._cred_rejected = False
        self._tried_v7_default_password = False

    def _cmd_prompt(self):
        self._commander.run_cmd()

    def _shutting_down(self):
        pass

    def _rebooting(self):
        pass

    def _credential_rejected(self):
        if not self._tried_v7_default_password:
            self._logger.debug("Credential rejected. Possibly never configured. Trying default password next time.")
            self._cred_rejected = True
            return
        additional_info = ""
        if b"pasword policy" in self.tn_out:
            additional_info = "Min password policy not met. Check the logs for the password policy"
        self._logger.error(f"Credential rejected. {additional_info}")

        self._terminal.stop()
        raise RuntimeError("Credential rejected")

    def _license_fail(self):
        self._logger.error("Failed to setup license.")
        self._terminal.stop()
        raise RuntimeError("License setup failed")

    def _unknown_state(self):
        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if self._last_known_state == FOSCliState.REBOOTING:
            self._idle_spins += 1
            # It could be that the FGT image was defective. In this case it has been observed that
            # the FGT would endlessly reboot.
        else:
            self._idle_spins = 0

    def _tn_timeout(self):
        if self._last_known_state == FOSCliState.CMD_PROMPT:
            self._cmd_prompt()
        self._idle_spins += 1
