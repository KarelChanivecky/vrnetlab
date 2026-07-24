import os
import time

import vrnetlab
from fos_commander import FOSCommander
from fos_state import FOSCliState, FOS_CLI_STATE_PATTERNS


class FOSCliDriver:
    """
    Drives the CLI through boot, login, and ready-for-cmd states.
    """

    def __init__(self, terminal: vrnetlab.VM, mgmt_passthrough, username, password, logger, mgmt_address_ipv4,
                 mgmt_gw_ipv4, mgmt_address_ipv6, mgmt_gw_ipv6, hostname) -> None:
        super().__init__()
        self._idle_spins = 0
        self._logger = logger
        self._password = password
        self._username = username
        self._terminal = terminal
        self._mgmt_passthrough = mgmt_passthrough
        self._log_bin = os.getenv("FOS_LOG_BIN", "false").lower() == "true"
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
            waiting_for_state=self._waiting_for
        )

        self._tried_v7_default_password = False
        self._cred_rejected = False

    def process_state(self):
        spin_start = time.time()
        # Running signals health state. Stopped signals we stopped before reaching healthy state
        while not self._terminal.stopped and not self._terminal.running and time.time() < spin_start + 5:
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
                if not self._log_bin:
                    log_out = log_out.decode()
                self._logger.debug(f"OUT: {log_out}")

            if self._waiting_for and cur_state not in self._waiting_for:
                return

            self._waiting_for.clear()
            self._state_handlers[cur_state]()

    def _next_state(self):
        (ridx, match, res) = self._terminal.tn.expect(self._state_patterns, 1)
        self.tn_out = res
        if not match:
            if res == b"":
                return FOSCliState.TN_TIMEOUT
            return FOSCliState.UNKNOWN
        return FOSCliState(ridx)

    def _provide_username(self):
        self._terminal.wait_write(self._username, wait=None)

    def _provide_password(self):
        if self._cred_rejected:
            self._tried_v7_default_password = True
            self._terminal.wait_write("", wait=None)
            return
        self._terminal.wait_write(self._password, wait=None)

    def _change_password(self):
        self._terminal.wait_write(self._password, wait=None)
        self._terminal.wait_write(self._password, wait="Confirm Password")
        self._password_changed = True
        # FOS 7.4 needs log out before you can use the password with ssh
        self._commander.logout()

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
