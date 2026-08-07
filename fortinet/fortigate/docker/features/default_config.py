"""Default FortiOS settings feature."""

from cli_commands import CommandSpec, ConfigBlock

from .base import StaticFeature


class DefaultConfig(StaticFeature):
    def __init__(self, vm, commander):
        self._hostname_line = f"set hostname {vm.hostname}"
        super().__init__(vm, commander, "default-config", [ConfigBlock("system global", [
            CommandSpec("set admin-scp enable"),
            CommandSpec(self._hostname_line),
        ])])

    def on_command_result(self, commander, attempt, state, output):
        if attempt.spec.line == self._hostname_line:
            self.vm.driver.set_hostname_prompt_patterns(self.vm.hostname)
