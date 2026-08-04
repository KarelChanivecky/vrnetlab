from enum import IntEnum, auto


class FOSCliState(IntEnum):
    PROVIDE_USERNAME = 0
    PROVIDE_PASSWORD = auto()
    CHANGE_PASSWORD = auto()
    CREDENTIAL_REJECTED = auto()
    CREDENTIAL_ACCEPTED = auto()
    LIC_FAIL = auto()
    CMD_PROMPT = auto()
    SHUTTING_DOWN = auto()
    REBOOTING = auto()
    UNKNOWN = auto()  # Non-patterns from here on.
    TN_TIMEOUT = auto()


DEFAULT_HOSTNAME_REGEX = rb"[A-Za-z0-9_.-]+(?:-VM64)?-KVM(?:-[A-Za-z0-9]*)?"
LICENSED_HOSTNAME_REGEX = rb"[A-Z][A-Z0-9]{7,}"
DEFAULT_HOSTNAME_PROMPT = rb"(?m)^\s*" + DEFAULT_HOSTNAME_REGEX + rb"(?:\s+\((?:STS|Interim)\))?\s*[#$]\s*"

FOS_CLI_STATE_PATTERNS = [None] * FOSCliState.UNKNOWN.value
FOS_CLI_STATE_PATTERNS[FOSCliState.PROVIDE_USERNAME.value] = (
        rb"\n" + DEFAULT_HOSTNAME_REGEX +
        rb"(?:\((?:Primary|Secondary)\))?"
        rb"\s+login:\s*"
)
FOS_CLI_STATE_PATTERNS[FOSCliState.CHANGE_PASSWORD.value] = b"(?m)^New Password:"
FOS_CLI_STATE_PATTERNS[FOSCliState.PROVIDE_PASSWORD.value] = b"(?m)^Password:"
FOS_CLI_STATE_PATTERNS[FOSCliState.CREDENTIAL_REJECTED.value] = rb"(?m)^Login incorrect\r?$"
FOS_CLI_STATE_PATTERNS[FOSCliState.CREDENTIAL_ACCEPTED.value] = rb"(?m)^Welcome ?!\r?$"
FOS_CLI_STATE_PATTERNS[FOSCliState.LIC_FAIL.value] = rb"(?m)^VM license install failed.\r$"
FOS_CLI_STATE_PATTERNS[FOSCliState.CMD_PROMPT.value] = DEFAULT_HOSTNAME_PROMPT
FOS_CLI_STATE_PATTERNS[FOSCliState.SHUTTING_DOWN.value] = b"system is going down"
FOS_CLI_STATE_PATTERNS[FOSCliState.REBOOTING.value] = b"stand by while rebooting"

DEF_POLICY_COMPLIANT_PASSWORD = "FortinetFOS1!"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = DEFAULT_USERNAME


class Credentials:
    def __init__(self, username, password) -> None:
        super().__init__()
        self.username = username
        self.password = password
