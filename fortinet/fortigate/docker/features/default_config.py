"""Default FortiOS settings feature."""

from cli_commands import ConfigBlock

from .base import StaticFeature


class DefaultConfig(StaticFeature):
    def __init__(self, vm, commander):
        self._hostname_line = f"set hostname {vm.hostname}"
        super().__init__(vm, commander, "default-config", [
            ConfigBlock("system global", [
                "set admin-scp enable",
                self._hostname_line,
            ]),
            ConfigBlock("system fortiguard", [
                "set interface-select-method specify",
                "set interface port1",
            ])
        ])

    def on_command_executed(self, command, state):
        if command.spec.line == self._hostname_line:
            self.vm.driver.set_prompt_patterns(self.vm.hostname)
