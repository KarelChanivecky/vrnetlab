"""VM license installation feature."""

import os
import re
import time

from cli_commands import CommandSequence, CommandSpec, SessionLossAction
from common import FOSCliState

from .base import Feature


DEFAULT_LICENSE_STATUS_TIMEOUT_SECONDS = 2 * 60
LICENSE_STATUS_POLL_INTERVAL_SECONDS = 2


def license_status_timeout_seconds():
    value = os.getenv("FOS_LICENSE_STATUS_TIMEOUT_SECONDS")
    if not value:
        return DEFAULT_LICENSE_STATUS_TIMEOUT_SECONDS
    return int(value)


class SetLicense(Feature):
    """Install a license without waiting for online validation.

    FortiOS license restore reboots the VM and can reset parts of management
    networking.  Online validation must be polled only after post-license
    management repair has run.
    """

    def __init__(self, vm, commander):
        super().__init__(vm, commander, "setup-license")
        self._enabled = os.path.exists("/tftpboot/appliance.lic")
        self._tftp_server_ip = vm.mgmt_gw_ipv4
        self._phase = "restore"
        self._wait_for_prompt = False

    def activate(self, commander):
        if not self._enabled:
            commander.feature_complete(self)
            return
        self.vm.driver.set_license_prompt_patterns()
        self._submit_restore(commander)

    def _submit_restore(self, commander):
        commander.submit_block(self, CommandSequence("restore-license", [
            CommandSpec(
                f"exe restore vmlicense tftp appliance.lic {self._tftp_server_ip}",
                completion_states=(FOSCliState.CONFIRMATION,),
                session_loss=SessionLossAction.CONTINUE,
            ),
        ]))

    def on_command_result(self, commander, attempt, state, output):
        if self._phase == "restore" and state == FOSCliState.CONFIRMATION:
            self._phase = "restore-confirmed"
            commander.submit_block(self, CommandSequence("confirm-license", [
                CommandSpec(
                    "y",
                    completion_states=(FOSCliState.REBOOTING,),
                    session_loss=SessionLossAction.CONTINUE,
                ),
            ]))
        elif self._phase == "restore-confirmed" and state == FOSCliState.REBOOTING:
            self._phase = "wait-prompt"
            self._wait_for_prompt = True

    def on_block_complete(self, commander):
        if self._phase in ("restore", "restore-confirmed"):
            return
        if self._phase == "wait-prompt":
            if self._wait_for_prompt:
                self._wait_for_prompt = False
                return
            self._phase = "done"
        if self._phase == "done":
            commander.feature_complete(self)

    def on_session_loss(self, commander, attempt):
        if attempt.spec.session_loss == SessionLossAction.CONTINUE:
            self._phase = "wait-prompt"
            self._wait_for_prompt = True
        return attempt.spec.session_loss


class WaitForLicenseValidation(Feature):
    """Poll FortiOS until the restored license validates or times out."""

    def __init__(self, vm, commander):
        super().__init__(vm, commander, "license-validation")
        self._enabled = os.path.exists("/tftpboot/appliance.lic")
        self._logger = commander.logger
        self._deadline = None
        self._next_poll = None
        self._phase = "idle"
        self._standard_output_active = False

    def activate(self, commander):
        if not self._enabled:
            commander.feature_complete(self)
            return
        self._deadline = time.monotonic() + license_status_timeout_seconds()
        self._phase = "polling"
        self._next_poll = time.monotonic()

    def on_command_result(self, commander, attempt, state, output):
        status = self._license_status(output)
        if status and status.lower() != "pending":
            self._logger.info(f"License status changed to {status}")
            self._phase = "done"
        else:
            self._next_poll = time.monotonic() + LICENSE_STATUS_POLL_INTERVAL_SECONDS

    @staticmethod
    def _license_status(output):
        match = re.search(rb"(?mi)^License Status:\s*(.+?)\s*\r?$", output)
        if not match:
            match = re.search(rb"(?mi)^License:\s*(.+?)\s*\r?$", output)
        return match.group(1).decode(errors="replace").strip() if match else None

    def on_block_complete(self, commander):
        if self._phase == "done":
            commander.feature_complete(self)

    def tick(self, commander):
        if self._phase != "polling" or self._next_poll is None:
            return
        if time.monotonic() >= self._deadline:
            self._logger.warning("License status remained Pending.")
            self._phase = "done"
            commander.feature_complete(self)
            return
        if time.monotonic() >= self._next_poll and not commander.busy:
            self._next_poll = None
            if self._standard_output_active:
                self._submit_status_poll(commander)
            else:
                self._standard_output_active = True
                commander.with_standard_output(self, lambda: self._submit_status_poll(commander))

    def _submit_status_poll(self, commander):
        commander.submit_block(self, CommandSequence("license-validation", [
            CommandSpec("get system status", capture_output=True, suppress_output=True),
        ]))
