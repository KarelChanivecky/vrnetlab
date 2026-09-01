"""FortiGuard fortiguard-hooks bootstrap feature."""

import os

from cli_commands import CommandSequence, CommandSpec, ConfigBlock

from .base import StaticFeature


def fortiguard_hooks_enabled():
    return os.getenv("FOS_FORTIGUARD_HOOKS", "false").lower() == "true"


class ConfigureFortiGuardHooks(StaticFeature):
    _DIAG_1_COMMAND = "diagnostic-1"
    _DIAG_2_COMMAND = "diagnostic-2"
    _UNKNOWN_ACTION = b"Unknown action 0"

    def __init__(self, vm, commander):
        blocks = []
        if fortiguard_hooks_enabled():
            blocks = [
                CommandSequence("hooks-guard", [
                    CommandSpec(
                        self._DIAG_2_COMMAND,
                        capture_output=True,
                    ),
                ]),
                ConfigBlock("system fortiguard", [
                    CommandSpec("set fortiguard-anycast disable"),
                    CommandSpec("set fortiguard-server-location automatic"),
                ]),
            ]
        super().__init__(vm, commander, "fortiguard-hooks", blocks)

    def on_command_executed(self, command, state):
        if (
            command.spec.line == self._DIAG_2_COMMAND
            and self._UNKNOWN_ACTION in bytes(command.output)
        ):
            self.commander.submit_block(self, CommandSequence(
                "hooks-guard-fallback",
                [CommandSpec(
                    self._DIAG_1_COMMAND,
                    capture_output=True,
                )],
            ))
