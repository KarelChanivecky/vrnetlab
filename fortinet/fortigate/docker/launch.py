#!/usr/bin/env python3
import datetime
import logging
import os
import re
import signal
import sys
import uuid
import vrnetlab
from collections import deque
from enum import auto, IntEnum

from tftp import TFTP_FAKEHOST_VETH_MAC_ADDR, TFTPServer


def handle_SIGCHLD(_unused_signal, _unused_frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_unused_signal, _unused_frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self.log(TRACE_LEVEL_NUM, message, *args, **kws)


logging.Logger.trace = trace


class FOSCliState(IntEnum):
    PROVIDE_USERNAME = 0
    PROVIDE_PASSWORD = auto()
    CHANGE_PASSWORD = auto()
    CREDENTIAL_REJECTED = auto()
    CREDENTIAL_ACCEPTED = auto()
    CMD_PROMPT = auto()
    SHUTTING_DOWN = auto()
    REBOOTING = auto()
    UNKNOWN = auto()  # Non-patterns from here on.
    TN_TIMEOUT = auto()


DEFAULT_HOSTNAME_REGEX = rb"[A-Za-z0-9_.-]+(?:-VM64)?-KVM(?:-[A-Za-z0-9]*)?"
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
FOS_CLI_STATE_PATTERNS[FOSCliState.CMD_PROMPT.value] = DEFAULT_HOSTNAME_PROMPT
FOS_CLI_STATE_PATTERNS[FOSCliState.SHUTTING_DOWN.value] = b"system is going down"
FOS_CLI_STATE_PATTERNS[FOSCliState.REBOOTING.value] = b"stand by while rebooting"

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"


class FortiOS_vm(vrnetlab.VM):
    def __init__(self, hostname: str, username, password, conn_mode):
        disk_image = None
        for e in os.listdir("."):
            if re.search(".qcow2$", e):
                disk_image = "./" + e
        if disk_image is None:
            raise RuntimeError("Could not find image to boot")
        super(FortiOS_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=2048,
            driveif="virtio",
            # fortios fails to respond to network requests if the pci bus is setup :D
            provision_pci_bus=False,
            mgmt_passthrough=True
        )
        self.conn_mode = conn_mode
        self.hostname = hostname
        self.num_nics = 12
        self.nic_type = "virtio-net-pci"
        self.highest_port = 0
        self.qemu_args.extend(["-uuid", os.getenv("FORTIGATE_UUID") or str(uuid.uuid4())])
        self.spins = 0
        self.running = None

        # set up the extra empty disk image
        # for fortigate logs
        vrnetlab.run_command(
            ["qemu-img", "create", "-f", "qcow2", "empty.qcow2", "30G"]
        )

        self.qemu_args.extend(
            [
                "-drive",
                "if=virtio,format=qcow2,file=empty.qcow2,index=1",
            ]
        )
        self._state_patterns = FOS_CLI_STATE_PATTERNS.copy()
        self._state_handlers = {
            FOSCliState.PROVIDE_USERNAME: self._provide_username,
            FOSCliState.PROVIDE_PASSWORD: self._provide_password,
            FOSCliState.CHANGE_PASSWORD: self._change_password,
            FOSCliState.CREDENTIAL_REJECTED: self._credential_rejected,
            FOSCliState.CREDENTIAL_ACCEPTED: self._credential_accepted,
            FOSCliState.CMD_PROMPT: self._cmd_prompt,
            FOSCliState.SHUTTING_DOWN: self._shutting_down,
            FOSCliState.REBOOTING: self._rebooting,
            FOSCliState.UNKNOWN: self._unknown_state,
            FOSCliState.TN_TIMEOUT: self._tn_timeout,
        }
        self.tn_out = b""
        self._last_known_state = FOSCliState.UNKNOWN
        self._cmd_queue = deque()
        if self.mgmt_passthrough:
            self._cmd_queue.append(self._apply_mgmt_ip_passthrough)
        else:
            self._cmd_queue.append(self._apply_mgmt_ip_host_forwarded)
        self._cmd_queue.append(self._update_hostname)
        self._cmd_queue.append(self._apply_startup_config)
        self._tried_v7_default_password = False
        self._cred_rejected = False

    def bootstrap_spin(self):
        """This function should be called periodically to do work.

        returns False when it has failed and given up, otherwise True
        """
        if self.spins > 300:
            # too many spins without appropriate communication
            self.logger.warning("no output from serial console, restarting VCP")
            self.stop()
            self.spins = 0
            raise RuntimeError("VM node malfunction")
        cur_state = self._next_state()

        # The FSM is actually moving along
        if cur_state.value < FOSCliState.UNKNOWN.value:
            self.spins = 0
            self._last_known_state = cur_state

        # Continue to show current state while reducing verbosity
        if cur_state != FOSCliState.UNKNOWN and cur_state != FOSCliState.TN_TIMEOUT:
            self.logger.debug(f"ST: {cur_state.name}")

        if len(self.tn_out) > 0:
            self.logger.debug(f"OUT: {self.tn_out}")

        self._state_handlers[cur_state]()

    def _next_state(self):
        (ridx, match, res) = self.tn.expect(self._state_patterns, 1)
        self.tn_out = res
        if not match:
            if res == b"":
                return FOSCliState.TN_TIMEOUT
            return FOSCliState.UNKNOWN
        return FOSCliState(ridx)

    # === STATE HANDLERS ===

    def _provide_username(self):
        self.wait_write(self.username, wait=None)

    def _provide_password(self):
        if self._cred_rejected:
            self._tried_v7_default_password = True
            self.wait_write("", wait=None)
            return
        self.wait_write(self.password, wait=None)

    def _change_password(self):
        self.wait_write(self.password, wait=None)
        self.wait_write(self.password, wait="Confirm Password")
        self._password_changed = True
        # FOS 7.4 needs log out before you can use the password with ssh
        self._cmd_queue.appendleft(lambda: self.wait_write("exit", wait=None))

    def _credential_accepted(self):
        self._cred_rejected = False
        self._tried_v7_default_password = False

    def _cmd_prompt(self):
        try:
            next_cmd = self._cmd_queue.popleft()
        except IndexError:
            self._cmd_queue.append(self._ready)
            return
        next_cmd()

    def _shutting_down(self):
        pass

    def _rebooting(self):
        pass

    def _credential_rejected(self):
        if not self._tried_v7_default_password:
            self.logger.debug("Credential rejected. Possibly never configured. Trying default password next time.")
            self._cred_rejected = True
            return
        additional_info = ""
        if b"pasword policy" in self.tn_out:
            additional_info = "Min password policy not met. Check the logs for the password policy"
        self.logger.error(f"Credential rejected. {additional_info}")

        self.running = False
        self.tn.close()
        self.stop()
        raise RuntimeError("Credential rejected")

    def _unknown_state(self):
        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if self._last_known_state == FOSCliState.REBOOTING:
            self.spins += 1
            # It could be that the FGT image was defective. In this case it has been observed that
            # the FGT would endlessly reboot.
        else:
            self.spins = 0

    def _tn_timeout(self):
        if self._last_known_state == FOSCliState.CMD_PROMPT:
            self._cmd_prompt()
        self.spins += 1

    # === COMMANDS ===

    def _apply_mgmt_ip_passthrough(self):
        if self.mgmt_address_ipv4 == "dhcp":
            self.logger("MGMT IP is in DHCP mode")
            return
        self.logger.info(f"Setting mgmt IPv4={self.mgmt_address_ipv4} and IPv6={self.mgmt_address_ipv6}")
        self.wait_write("config system interface\r"
                        "edit port1\r"
                        "set mode static\r"
                        f"set ip {self.mgmt_address_ipv4}\r"
                        "set vrf 1",
                        wait=None)
        if self.mgmt_address_ipv6 is not None:
            self.wait_write("config ipv6\r"
                            "set ip6-mode static\r"
                            f"set ip6-address {self.mgmt_address_ipv6}\r"
                            "set ip6-allowaccess ping https ssh http\r"
                            "end",
                            wait=None)
        self.wait_write("next\r"
                        "end",
                        wait=None)

    def _apply_mgmt_ip_host_forwarded(self):
        self.wait_write("config system interface\r"
                        "edit port1"
                        , wait=None)
        self.wait_write("set vrf 1\r"
                        "next\r"
                        "end", wait="(port1)")

    def _update_hostname(self):
        self.wait_write("config system global", wait=None)
        hostname_command = "set hostname " + self.hostname
        self.wait_write(hostname_command, wait="global")
        self.wait_write("end", wait=hostname_command)
        self._state_patterns[FOSCliState.PROVIDE_USERNAME.value] = (
                rb"\n" + self.hostname.encode("utf-8") +
                rb"(?:\((?:Primary|Secondary)\))?" +
                rb"\s+login:\s*")
        self._state_patterns[FOSCliState.CMD_PROMPT.value] = (
                rb"(?m)^ ?"
                + re.escape(self.hostname.encode("utf-8"))
                + rb" ?"
                + rb"(?:\s+\((?:STS|Interim)\))?"
                + rb" ?[#$] ?"
        )

    def _apply_startup_config(self):
        """Load additional config provided by user."""

        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} is not found")
            return

        self.logger.trace(f"Configuring with startup-config from file: {STARTUP_CONFIG_FILE}")
        config_lines = []
        with open(STARTUP_CONFIG_FILE) as file:
            config_lines = file.readlines()

        config_stack = deque()

        for line in config_lines:
            sline = line.strip()
            if sline.startswith("config"):
                config_stack.append("c")
            if sline.startswith("edit"):
                config_stack.append("e")
            if sline.startswith("next") or sline.startswith("end"):
                top = config_stack.pop()
                if sline.startswith("next") and top != "e":
                    self.logger.error("Startup config malformed. \"next\" command outside of edit scope.")
                    raise ValueError("Startup config malformed. \"next\" command outside of edit scope.")
                if sline.startswith("end") and top != "c":
                    self.logger.error("Startup config malformed. \"end\" command outside of config scope.")
                    raise ValueError("Startup config malformed. \"end\" command outside of config scope.")
            # Maintaining nesting produces a more appealing debug log.
            self.wait_write(line, wait=None)

        if len(config_stack) > 0:
            raise ValueError("Startup config malformed. Unmatched config or edit brackets.")

    def _ready(self):
        self.running = True
        self.tn.close()
        # calc startup time
        startup_time = datetime.datetime.now() - self.start_time
        self.logger.info(f"Startup complete in {startup_time}")

    # === vrnetlab.VM overrides ===

    def create_tc_tap_mgmt_ifup(self):
        # override the parent's function with sros requirements
        # this is used when using pass-through mode for mgmt connectivity
        """Create tap ifup script that is used in tc datapath mode, specifically for the management interface"""
        ifup_script = """#!/bin/bash

        ip link set tap0 up
        ip link set tap0 mtu 65000

        # create tc eth<->tap redirect rules

        tc qdisc add dev eth0 clsact
        # exception for TCP ports 5000-5007
        tc filter add dev eth0 ingress prio 1 protocol ip flower ip_proto tcp dst_port 5000-5007 action pass
        # mirror ARP traffic to container
        tc filter add dev eth0 ingress prio 2 protocol arp flower action mirred egress mirror dev tap0
        # redirect rest of ingress traffic of eth0 to egress of tap0
        tc filter add dev eth0 ingress prio 3 flower action mirred egress redirect dev tap0

        tc qdisc add dev tap0 clsact
        # redirect tftp traffic to fakehost ns
        tc filter add dev tap0 ingress protocol ip prio 1	\
            flower ip_proto udp dst_port 69 dst_ip {MGMT_CONTAINER_GW} 	\
            action pedit ex munge eth dst set {TFTP_FAKEHOST_VETH_MAC_ADDR} pipe \
            action mirred egress redirect dev RA

        tc filter add dev tap0 ingress protocol ip prio 2	\
            flower ip_proto udp dst_port 52400-52500 dst_ip {MGMT_CONTAINER_GW} 	\
            action pedit ex munge eth dst set {TFTP_FAKEHOST_VETH_MAC_ADDR} pipe \
            action mirred egress redirect dev RA

        # redirect all ingress traffic of tap0 to egress of eth0
        tc filter add dev tap0 ingress flower action mirred egress redirect dev eth0

        # redirect tftp traffic coming from ns to the mgmt address of the sros VM
        # Mac rewrite because by default dst mac will be that of the RA link
        tc qdisc add dev RA clsact
            tc filter add dev RA ingress protocol ip prio 1	\
            flower ip_proto udp src_port 69 dst_ip {MGMT_IP_ADDRESS} 	\
            action pedit ex munge eth dst set {MGMT_MAC} pipe \
            action mirred egress redirect dev tap0
        
        tc filter add dev RA ingress protocol ip prio 2	\
            flower ip_proto udp src_port 52400-52500 dst_ip {MGMT_IP_ADDRESS} 	\
            action pedit ex munge eth dst set {MGMT_MAC} pipe \
            action mirred egress redirect dev tap0

        # clone management MAC of the VM
        ip link set dev eth0 address {MGMT_MAC}

        # configure the ip address of the namespace as it was the host and remove the temporary one
        ip netns exec fakehost ip addr add {MGMT_CONTAINER_GW}/{MGMT_IP_PREFIXLEN} dev FA
        ip netns exec fakehost ip addr del  169.254.254.254/16 dev FA
        """

        mgmt_ip_v4_address, mgmt_ip_v4_prefixlen = self.mgmt_address_ipv4.split("/")

        ifup_script = ifup_script.replace("{MGMT_MAC}", self.mgmt_mac)
        ifup_script = ifup_script.replace(
            "{TFTP_FAKEHOST_VETH_MAC_ADDR}", TFTP_FAKEHOST_VETH_MAC_ADDR
        )
        ifup_script = ifup_script.replace("{MGMT_CONTAINER_GW}", self.mgmt_gw_ipv4)
        ifup_script = ifup_script.replace("{MGMT_IP_PREFIXLEN}", mgmt_ip_v4_prefixlen)
        ifup_script = ifup_script.replace("{MGMT_IP_ADDRESS}", mgmt_ip_v4_address)
        self.logger.info(f"TFTP Traffic towards {self.mgmt_gw_ipv4} redirected towards fakehost mac: "
                    f"{TFTP_FAKEHOST_VETH_MAC_ADDR} and return traffic directed to MAC: {self.mgmt_mac}")

        with open("/etc/tc-tap-mgmt-ifup", "w") as f:
            f.write(ifup_script)
        os.chmod("/etc/tc-tap-mgmt-ifup", 0o777)


class FortiOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(FortiOS, self).__init__(username, password)
        self.logger.debug("Hostname")
        self.logger.debug(hostname)
        self.vms = [FortiOS_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="vr-fortinet", help="Fortinet hostname")
    parser.add_argument("--username", default="admin", help="Username")
    parser.add_argument("--password", default="admin", help="Password")
    parser.add_argument(
        "--connection-mode",
        default="tc",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = FortiOS(
        args.hostname, args.username, args.password, conn_mode=args.connection_mode
    )
    tftp_server = TFTPServer(vr.vms[0].mgmt_passthrough)
    tftp_server.launch()
    vrnetlab.boot_delay()
    vr.start()
