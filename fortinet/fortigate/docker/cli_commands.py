"""Command and scope data used by the FortiOS CLI scheduler."""

from dataclasses import dataclass, field
from enum import Enum, auto


class SessionLossAction(Enum):
    RESTART_BLOCK = auto()
    COMPLETE_BLOCK = auto()
    CONTINUE = auto()
    VALIDATE = auto()
    FAIL = auto()


@dataclass(frozen=True, init=False)
class CommandSpec:
    line: str
    completion_states: tuple = ()
    capture_output: bool = False
    suppress_output: bool = False
    session_loss: SessionLossAction = SessionLossAction.RESTART_BLOCK

    def __init__(self, line, completion_states=(), capture_output=False,
                 suppress_output=False, session_loss=SessionLossAction.RESTART_BLOCK):
        object.__setattr__(self, "line", line)
        object.__setattr__(self, "completion_states", completion_states)
        object.__setattr__(self, "capture_output", capture_output)
        object.__setattr__(self, "suppress_output", suppress_output)
        object.__setattr__(self, "session_loss", session_loss)
        self.__post_init__()

    def __post_init__(self):
        if "\r" in self.line or "\n" in self.line:
            raise ValueError("CLI commands must contain exactly one line")


@dataclass
class CommandAttempt:
    spec: CommandSpec
    number: int
    session_epoch: int
    output: bytearray = field(default_factory=bytearray)

    def on_output(self, output):
        if not self.spec.capture_output:
            return False
        data = bytes(output)
        self.output.extend(data)
        output.discard(data)
        return True


class Scope:
    def __init__(self, value, children=()):
        self.value = value
        self.children = list(children)

    @property
    def open_line(self):
        raise NotImplementedError

    @property
    def close_line(self):
        raise NotImplementedError

    def flatten(self):
        commands = [CommandSpec(self.open_line)]
        for child in self.children:
            commands.extend(flatten_commands(child))
        commands.append(CommandSpec(self.close_line))
        return commands


class ConfigBlock(Scope):
    @property
    def open_line(self):
        return f"config {self.value}"

    @property
    def close_line(self):
        return "end"


class EditBlock(Scope):
    @property
    def open_line(self):
        return f"edit {self.value}"

    @property
    def close_line(self):
        return "next"


class SetValue:
    """A ``set <field> "<value>"`` command whose quoted value spans CLI lines.

    ``CommandSpec`` rejects embedded newlines, and FortiOS accepts a quoted
    multi-line value typed across CLI lines: the first line opens the quote,
    following body lines are typed verbatim, and the last body line closes it.
    """

    def __init__(self, field, value):
        self.field = field
        self.value = value

    def flatten(self):
        lines = str(self.value).strip().split("\n")
        if len(lines) == 1:
            return [CommandSpec(f'set {self.field} "{lines[0]}"')]
        return [
            CommandSpec(f'set {self.field} "{lines[0]}"'),
            *[CommandSpec(line) for line in lines[1:-1]],
            CommandSpec(f'{lines[-1]}"'),
        ]


class CommandSequence:
    """An unscoped workflow container; it does not emit CLI framing."""

    def __init__(self, name, children=()):
        self.name = name
        self.children = list(children)

    def flatten(self):
        commands = []
        for child in self.children:
            commands.extend(flatten_commands(child))
        return commands


def flatten_commands(value):
    """Flatten command containers and wrap plain CLI lines with defaults."""
    if isinstance(value, str):
        return [CommandSpec(value)]
    if isinstance(value, CommandSpec):
        return [value]
    if isinstance(value, (Scope, CommandSequence, SetValue)):
        return value.flatten()
    raise TypeError(
        "Commands must be strings, CommandSpec instances, or command containers"
    )


@dataclass(frozen=True)
class CleanupAction:
    name: str
    block: object
    on_completion: bool = True
    on_interruption: bool = True
