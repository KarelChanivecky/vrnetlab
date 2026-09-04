"""PKI certificate installation feature.

Installs CA certificates (trust), local certificate/key pairs, remote
certificates, and certificate revocation lists at bootstrap. Env variables
carry paths only; this feature reads the file contents and never logs or
stores the certificate, key, or password bytes themselves.

Entries are ``;``-separated and may carry a leading reference name that
becomes the FortiOS object name (``refname:``); when the refname is omitted,
the object is named after the certificate CN. Path values containing ``:``
must therefore carry a refname.

Env variables:

- ``FOS_PKI_CA_CERTS``: ``[refname:]path`` — CAs imported with
  ``execute vpn certificate ca import tftp`` and named by refname or CN.
- ``FOS_PKI_LOCAL_CERTS``: ``[refname:]key_path:cert_path`` or
  ``[refname:]cert_path`` — installed as local certificate entries named by
  refname or CN, including the SSL deep-inspection CA pair (which is just a
  local cert).
- ``FOS_PKI_LOCAL_CERT_PASS_FILES``: ``path;path;...`` — positionally paired
  with encrypted-key ``[refname:]key_path:cert_path`` entries; the contents
  are typed as ``set password``.
- ``FOS_PKI_REMOTE_CERTS``: ``[refname:]path`` — remote certificates imported
  with ``execute vpn certificate remote import tftp`` and named by refname
  or CN.
- ``FOS_PKI_CRLS``: ``[refname:]path`` — CRLs installed as ``config vpn
  certificate crl`` entries with base64-encoded CRL bodies, named by
  refname or file basename.
"""

import base64
import os
import re
import shutil
import ssl

from cli_commands import (
    CommandSequence,
    CommandSpec,
    ConfigBlock,
    EditBlock,
    SessionLossAction,
    SetValue,
)
from common import FOSCliState

from .base import Feature


CA_CERTS_ENV = "FOS_PKI_CA_CERTS"
LOCAL_CERTS_ENV = "FOS_PKI_LOCAL_CERTS"
LOCAL_CERT_PASS_FILES_ENV = "FOS_PKI_LOCAL_CERT_PASS_FILES"
REMOTE_CERTS_ENV = "FOS_PKI_REMOTE_CERTS"
CRLS_ENV = "FOS_PKI_CRLS"

TFTP_PKI_DIRECTORY = "/tftpboot/pki"
# The config tree moved back and forth between these names across releases.
VPN_CERTIFICATE_CRL_SCOPES = ("vpn certificate crl", "certificate crl")


def _split_entries(variable, value):
    """Split ``value`` on ``;`` and strip whitespace, dropping empty entries."""
    if value is None:
        return []
    return [entry for raw in value.split(";") if (entry := raw.strip())]


def _require_path(variable, path):
    if not os.path.exists(path):
        raise ValueError(f"{variable}: path does not exist: {path}")


def parse_ca_certs(value, variable=CA_CERTS_ENV):
    entries = _split_entries(variable, value)
    for entry in entries:
        _require_path(variable, entry)
    return entries


def _parse_refname(entry, variable):
    """Split an optional leading ``refname:`` from an entry.

    Returns ``(refname_or_None, path)``. A ``None`` refname means the
    object name is implied by the certificate CN.
    """
    refname, separator, path = entry.partition(":")
    if not separator:
        _require_path(variable, entry)
        return None, entry
    if not path:
        raise ValueError(
            f"{variable}: entry '{entry}' must carry a path after the refname"
        )
    _require_path(variable, path)
    return refname or None, path


def parse_ca_certs(value, variable=CA_CERTS_ENV):
    """Return ``(refname_or_None, path)`` pairs from ``value``."""
    return [
        _parse_refname(entry, variable)
        for entry in _split_entries(variable, value)
    ]


def parse_local_certs(value, variable=LOCAL_CERTS_ENV):
    """Return ``(refname_or_None, key_path_or_None, cert_path)`` triples.

    Entries are ``[refname:]key_path:cert_path`` or ``[refname:]cert_path``.
    The refname splits on the first colon, so paths containing ``:`` must
    carry a refname; an empty refname (``:key:cert``) implies the CN. A
    single-colon entry is a named cert-only entry, so a bare ``key:cert``
    pair must be spelled ``:key:cert``.
    """
    parsed = []
    for entry in _split_entries(variable, value):
        refname, separator, remainder = entry.partition(":")
        if not separator:
            _require_path(variable, entry)
            parsed.append((None, None, entry))
            continue
        refname = refname or None
        key_path, separator, cert_path = remainder.partition(":")
        if separator:
            _require_path(variable, key_path)
            _require_path(variable, cert_path)
            parsed.append((refname, key_path, cert_path))
        elif not refname:
            _require_path(variable, remainder)
            parsed.append((None, None, remainder))
        elif os.path.exists(refname) and os.path.exists(remainder):
            raise ValueError(
                f"{variable}: entry '{entry}' is ambiguous; a bare "
                "key:cert pair must be spelled ':key:cert' to imply the "
                "certificate CN"
            )
        else:
            _require_path(variable, remainder)
            parsed.append((refname, None, remainder))
    return parsed


def parse_pass_files(value, variable=LOCAL_CERT_PASS_FILES_ENV):
    entries = _split_entries(variable, value)
    for entry in entries:
        _require_path(variable, entry)
    return entries


def parse_remote_certs(value, variable=REMOTE_CERTS_ENV):
    """Return ``(refname_or_None, path)`` pairs from ``value``."""
    return [
        _parse_refname(entry, variable)
        for entry in _split_entries(variable, value)
    ]


def parse_crls(value, variable=CRLS_ENV):
    """Return ``(refname_or_None, path)`` pairs from ``value``."""
    return [
        _parse_refname(entry, variable)
        for entry in _split_entries(variable, value)
    ]


def read_certificate_cn(path):
    """Read the subject CN from a PEM certificate file.

    Uses ``ssl._ssl._test_decode_cert``; a minimal DER header scan is the
    fallback for builds where the private API is unavailable. Raises
    ``ValueError`` naming the file when the CN cannot be determined.
    """
    try:
        if hasattr(ssl._ssl, "_test_decode_cert"):
            decoded = ssl._ssl._test_decode_cert(path)
            for rdn in decoded.get("subject", ()):
                for attribute, value in rdn:
                    if attribute == "commonName":
                        return value
        raise ValueError(f"Could not read certificate CN from {path}")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Could not read certificate CN from {path}: {error}")


def read_pass_file(path):
    with open(path, "r", encoding="utf-8") as password:
        contents = password.read().strip()
    if not contents:
        raise ValueError(f"{LOCAL_CERT_PASS_FILES_ENV}: empty password file: {path}")
    return contents


def read_crl_body(path):
    """Return the CRL PEM file contents as base64 of the body bytes."""
    with open(path, "r", encoding="utf-8") as crl:
        contents = crl.read()
    match = re.search(
        r"-----BEGIN [^-]+-----\n(.*?)-----END [^-]+-----",
        contents,
        re.DOTALL,
    )
    body = match.group(1) if match else contents
    return base64.b64encode("".join(body.split()).encode("utf-8")).decode("ascii")


class InstallPkiCertificates(Feature):
    """Install certificates and CRLs staged by the plugin before startup config.

    Must run after the factory baseline capture and before the user startup
    config so the config can reference the installed certificate names.
    """

    def __init__(self, vm, commander):
        super().__init__(vm, commander, "pki-certificates")
        self._logger = commander.logger
        self._tftp_server_ip = vm.mgmt_gw_ipv4
        self._ca_paths = parse_ca_certs(os.getenv(CA_CERTS_ENV))
        self._local_entries = parse_local_certs(os.getenv(LOCAL_CERTS_ENV))
        self._pass_files = parse_pass_files(os.getenv(LOCAL_CERT_PASS_FILES_ENV))
        self._remote_paths = parse_remote_certs(os.getenv(REMOTE_CERTS_ENV))
        self._crl_paths = parse_crls(os.getenv(CRLS_ENV))
        self._paired_locals = self._pair_pass_files()
        self._staging_directory = TFTP_PKI_DIRECTORY
        self._phase = "idle"
        self._crl_config = None

    def _pair_pass_files(self):
        """Pair pass files positionally with local entries that carry a key."""
        if len(self._pass_files) > len(self._local_entries):
            raise ValueError(
                f"{LOCAL_CERT_PASS_FILES_ENV} has more entries than "
                f"{LOCAL_CERTS_ENV}; cannot pair passwords"
            )
        paired = []
        pass_index = 0
        for refname, key_path, cert_path in self._local_entries:
            if key_path is None:
                paired.append((refname, key_path, cert_path, None))
                continue
            if pass_index >= len(self._pass_files):
                paired.append((refname, key_path, cert_path, None))
                continue
            paired.append((refname, key_path, cert_path, self._pass_files[pass_index]))
            pass_index += 1
        if pass_index < len(self._pass_files):
            raise ValueError(
                f"{LOCAL_CERT_PASS_FILES_ENV} provides passwords for entries "
                f"without private keys in {LOCAL_CERTS_ENV}"
            )
        return paired

    def activate(self):
        if not self._blocks_ready():
            self.commander.feature_complete(self)
            return
        self._phase = "stage"
        self._submit_next()

    def _blocks_ready(self):
        return bool(self._ca_paths or self._local_entries or self._remote_paths or self._crl_paths)

    def _local_with_password(self):
        return self._paired_locals

    def _submit_next(self):
        if self._phase == "stage":
            self._stage_import_files()
            self._phase = "ca-imports"
            self._submit_imports("ca-imports", self._ca_paths, "ca")
            return
        if self._phase == "ca-imports":
            self._phase = "local-certs"
            self._submit_local_certs()
            return
        if self._phase == "local-certs":
            self._phase = "remote-imports"
            self._submit_imports("remote-imports", self._remote_paths, "remote")
            return
        if self._phase == "remote-imports":
            self._phase = "crl-detect"
            self._submit_crl_detection()
            return
        if self._phase == "crl-detect":
            self._phase = "crl-config"
            self._submit_crls()
            return
        self.commander.feature_complete(self)

    def _stage_import_files(self):
        """Copy CA and remote source files under collision-free TFTP names."""
        if not (self._ca_paths or self._remote_paths):
            return
        os.makedirs(self._staging_directory, exist_ok=True)
        for index, (_refname, path) in enumerate(self._ca_paths, start=1):
            staged = os.path.join(self._staging_directory, f"pki-ca-{index:03d}.pem")
            shutil.copyfile(path, staged)
        for index, (_refname, path) in enumerate(self._remote_paths, start=1):
            staged = os.path.join(self._staging_directory, f"pki-remote-{index:03d}.pem")
            shutil.copyfile(path, staged)

    def _staged_name(self, index, kind):
        return f"pki-{kind}-{index:03d}.pem"

    def _submit_imports(self, name, paths, kind):
        if not paths:
            self.on_block_complete()
            return
        commands = []
        for index in range(1, len(paths) + 1):
            commands.append(CommandSpec(
                f"exe vpn certificate {kind} import tftp {self._staged_name(index, kind)} {self._tftp_server_ip}",
                completion_states=(FOSCliState.CONFIRMATION,),
                session_loss=SessionLossAction.CONTINUE,
            ))
        self.commander.submit_block(self, CommandSequence(name, commands))

    def _submit_local_certs(self):
        paired = self._local_with_password()
        if not paired:
            self.on_block_complete()
            return
        warnings = []
        seen = {}
        blocks = []
        for refname, key_path, cert_path, pass_file in paired:
            cn = read_certificate_cn(cert_path)
            name = refname or cn
            if name in seen:
                warnings.append(
                    f"Duplicate local certificate name '{name}'; last entry wins"
                )
            seen[name] = True
            lines = []
            if key_path:
                with open(key_path, "r", encoding="utf-8") as key:
                    lines.append(SetValue("private-key", key.read()))
            with open(cert_path, "r", encoding="utf-8") as cert:
                lines.append(SetValue("certificate", cert.read()))
            if pass_file:
                lines.append(f"set password {read_pass_file(pass_file)}")
            blocks.append(ConfigBlock("vpn certificate local", [
                EditBlock(f'"{name}"', lines),
            ]))
        for warning in warnings:
            self._logger.warning(warning)
        self.commander.submit_block(self, CommandSequence("local-certs", blocks))

    def _submit_crl_detection(self):
        if not self._crl_paths:
            self.on_block_complete()
            return
        self.commander.submit_block(self, CommandSequence("crl-detect", [
            CommandSpec("get system status", capture_output=True, suppress_output=True),
        ]))

    def _submit_crls(self):
        if not self._crl_paths:
            self.on_block_complete()
            return
        warnings = []
        seen = {}
        blocks = []
        for refname, path in self._crl_paths:
            crl_base64 = read_crl_body(path)
            name = refname or self._crl_basename(path)
            if name in seen:
                warnings.append(f"Duplicate CRL name '{name}'; last entry wins")
            seen[name] = True
            blocks.append(ConfigBlock(self._crl_config, [
                EditBlock(f'"{name}"', [f"set crl {crl_base64}"]),
            ]))
        for warning in warnings:
            self._logger.warning(warning)
        self.commander.submit_block(self, CommandSequence("crl-config", blocks))

    @staticmethod
    def _crl_basename(path):
        return os.path.splitext(os.path.basename(path))[0]

    def on_command_executed(self, command, state):
        if self._phase == "crl-detect" and command.spec.capture_output:
            self._crl_config = self._crl_config_from(bytes(command.output))
            if self._crl_config is None:
                raise RuntimeError(
                    "Could not determine the CRL config tree from get system status"
                )

    @staticmethod
    def _crl_config_from(output):
        text = output.decode(errors="replace")
        version = re.search(r"Version:\s*v?(\d+)\.", text)
        major = int(version.group(1)) if version else None
        if major is None:
            return None
        # ``config certificate crl`` was the tree before FortiOS 7.0.
        if major < 7:
            return VPN_CERTIFICATE_CRL_SCOPES[1]
        return VPN_CERTIFICATE_CRL_SCOPES[0]

    def on_block_complete(self):
        self._submit_next()

    @property
    def completion_message(self):
        return None
