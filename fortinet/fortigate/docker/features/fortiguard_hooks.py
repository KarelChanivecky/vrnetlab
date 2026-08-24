"""FortiGuard fortiguard-hooks bootstrap feature."""

import os

from cli_commands import CommandSequence, CommandSpec, ConfigBlock

from .base import StaticFeature


def fortiguard_hooks_enabled():
    return os.getenv("FOS_FORTIGUARD_HOOKS", "false").lower() == "true"


class ConfigureFortiGuardHooks(StaticFeature):
    def __init__(self, vm, commander):
        blocks = []
        if fortiguard_hooks_enabled():
            blocks = [
                CommandSequence("hooks-guard", [
                    CommandSpec(
                        "diagnostic-1",
                    ),
                ]),
                ConfigBlock("system fortiguard", [
                    CommandSpec("set fortiguard-anycast disable"),
                    CommandSpec("set fortiguard-server-location automatic"),
                ]),
            ]
        super().__init__(vm, commander, "fortiguard-hooks", blocks)
