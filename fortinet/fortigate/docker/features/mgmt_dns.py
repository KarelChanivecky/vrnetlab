"""Temporary DNS bootstrap feature."""

import re

from cli_commands import CommandSequence, CommandSpec, ConfigBlock

from .base import Feature


class ConfigureMgmtDns(Feature):
    def __init__(self, vm, commander):
        super().__init__(vm, commander, "bootstrap-dns")
        self._phase = "inspect"
        self._previous_protocol = None

    def activate(self, commander):
        commander.with_standard_output(self, lambda: commander.submit_block(
            self,
            CommandSequence("dns-protocol-inspect", [
                CommandSpec("show full-configuration system dns", capture_output=True, suppress_output=True),
            ]),
        ))

    def on_command_result(self, commander, attempt, state, output):
        if self._phase != "inspect":
            return
        self._previous_protocol = self._protocol_from(output)
        if self._previous_protocol is None:
            commander.logger.warning("Could not determine current DNS protocol; undo will unset it")

    def on_block_complete(self, commander):
        if self._phase == "inspect":
            self._phase = "apply"
            commander.submit_block(self, ConfigBlock("system dns", [
                CommandSpec("set protocol cleartext"),
                CommandSpec(f"set primary {self.vm.mgmt_dns_primary}"),
                CommandSpec(f"set secondary {self.vm.mgmt_dns_secondary}"),
            ]))
            return
        commander.feature_complete(self)

    @staticmethod
    def _protocol_from(output):
        match = re.search(rb"(?mi)^\s*set protocol\s+(\S+)\s*\r?$", output)
        return match.group(1).decode(errors="replace") if match else None

    def reversal_blocks(self):
        protocol_restore = (
            CommandSpec(f"set protocol {self._previous_protocol}")
            if self._previous_protocol else CommandSpec("unset protocol")
        )
        return [ConfigBlock("system dns", [
            protocol_restore,
            CommandSpec("unset primary"),
            CommandSpec("unset secondary"),
        ])]
