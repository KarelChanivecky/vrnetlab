#!/usr/bin/env python3

import datetime
import logging
import re
import signal
import sys
import time
import os

import vrnetlab

STARTUP_CONFIG_FILE = "/config/startup-config.xsf"


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class VOSS_vm(vrnetlab.VM):
    def __init__(self, username, password, hostname, conn_mode):
        disk_image = None
        for e in sorted(os.listdir("/")):
            if not disk_image and re.search(".qcow2$", e):
                disk_image = "/" + e

        super(VOSS_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=2048,
            driveif="ide",
        )

        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 33
        self.nic_type = "e1000"

    def wait_write_config(self, cmd, wait="#", retries=30, delay=5, error_patterns=None):
        """Execute command with retry logic for transient VOSS boot states.

        Retries when the response contains any of the strings in error_patterns
        or the generic 'configuration load' rejection message.
        """
        retry_patterns = ["configuration load"]
        if error_patterns:
            retry_patterns.extend(error_patterns)

        for attempt in range(retries):
            self.tn.write(f"{cmd}\r".encode())
            try:
                res = self.tn.read_until(wait.encode(), timeout=30)
                response_str = res.decode(errors="ignore")

                matched = next(
                    (p for p in retry_patterns if p.lower() in response_str.lower()),
                    None,
                )
                if matched:
                    self.logger.info(
                        f"Retryable response ({matched!r}) for '{cmd}', attempt {attempt + 1}/{retries}, waiting {delay}s..."
                    )
                    time.sleep(delay)
                    continue

                return
            except Exception as e:
                self.logger.warning(f"Timeout waiting for prompt after '{cmd}': {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
                continue

        self.logger.error(f"Command '{cmd}' failed after {retries} retries")

    def bootstrap_spin(self):
        """ This function should be called periodically to do work.
        """

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        (_, match, res) = self.tn.expect([rb'ogin:'], 1)

        if match:  # got a match!
            time.sleep(1)
            self.wait_write(cmd="", wait=None)
            self.wait_write(cmd="rwa", wait="ogin:")
            self.wait_write(cmd="rwa", wait="assword")
            # First login with the default rwa/rwa forces a password change
            self.wait_write(cmd=self.password, wait="assword")
            self.wait_write(cmd=self.password, wait="assword")
            self.wait_write(cmd="", wait=">")
            self.logger.info("Found login prompt")
            # run main config!
            self.logger.info("Running bootstrap_config()")
            self.bootstrap_config()
            self.startup_config()
            time.sleep(1)
            # close telnet connection
            self.tn.close()
            # startup time?
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info("Startup complete in: %s" % startup_time)
            # mark as running
            self.running = True
            return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return


    def bootstrap_config(self):
        """ Do the actual bootstrap config
        """
        self.wait_write_config(cmd="enable", wait="#")
        self.wait_write_config(cmd="configure terminal", wait="#")
        self.wait_write_config(cmd=f"sys name {self.hostname}", wait="#")
        # VOSS boots in ZTP mode with "mgmt dhcp-client cycle", which keeps
        # requesting DHCP on the oob and onboarding VLAN interfaces. Without a
        # DHCP server (transparent mgmt) it puts a 169.254.x.x link-local address
        # on oob every cycle, so disable it before configuring oob statically.
        self.wait_write_config(cmd="no mgmt dhcp-client", wait="#")
        # Recreate the oob instance, then configure 10.0.0.15/24 (qemu user-mode
        # network) or the container's address (transparent mgmt) statically.
        self.wait_write_config(cmd="no mgmt oob", wait="(y/n) ?")
        self.wait_write_config(cmd="y", wait="#")
        self.wait_write_config(cmd="mgmt oob", wait="#")
        self.wait_write_config(cmd=f"ip address {self.mgmt_address_ipv4}", wait="#")
        self.wait_write_config(
            cmd=f"ip route 0.0.0.0/0 next-hop {self.mgmt_gw_ipv4}", wait="#"
        )
        self.wait_write_config(cmd="enable", wait="#")
        self.wait_write_config(cmd="exit", wait="#")
        self.wait_write_config(
            cmd=f"username add {self.username} level rwa enable", wait="assword"
        )
        self.wait_write_config(cmd=self.password, wait="assword")
        self.wait_write_config(cmd=self.password, wait="#")
        self.wait_write_config(cmd="exit", wait="#")
        self.wait_write_config(cmd="save config", wait="#")

    def startup_config(self):
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} not found")
            return
        if not self.mgmt_passthrough:
            # qemu user-mode network serves /tftpboot on 10.0.0.2.
            # "source" always runs the file from privileged exec mode, so it must
            # enter config mode itself, like a saved VOSS config does.
            with open(STARTUP_CONFIG_FILE) as file:
                config = file.read()
            commands = [line.strip() for line in config.splitlines()]
            commands = [line for line in commands if line and not line.startswith("#")]
            if not (commands and commands[0].startswith("conf")):
                config = f"configure terminal\n{config}\nend\n"
            with open("/tftpboot/containerlab.cfg", "w") as file:
                file.write(config)
            self.wait_write_config(
                cmd="copy 10.0.0.2:containerlab.cfg /intflash/containerlab.cfg", wait="#"
            )
            self.wait_write_config(cmd="source /intflash/containerlab.cfg", wait="#")
            self.wait_write_config(cmd="save config", wait="#")
            return

        # Transparent mgmt: no TFTP server reachable, push the config over the
        # serial console instead.
        with open(STARTUP_CONFIG_FILE) as file:
            config_lines = [line.rstrip() for line in file.readlines()]

        self.logger.info(f"Writing lines from {STARTUP_CONFIG_FILE}")
        self.wait_write_config(cmd="configure terminal", wait="#")
        for line in config_lines:
            if not line or line.startswith("#"):
                continue
            self.wait_write_config(cmd=line, wait="#")
        self.wait_write_config(cmd="end", wait="#")
        self.wait_write_config(cmd="save config", wait="#")


class VOSS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(VOSS, self).__init__(username, password)
        self.vms = [VOSS_vm(username, password, hostname, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument("--trace", action="store_true", help="enable trace level logging")
    parser.add_argument("--hostname", default="vr-voss", help="Router hostname")
    parser.add_argument('--username', default='vrnetlab', help='Username')
    parser.add_argument('--password', default='VR-netlab9', help='Password')
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

    vr = VOSS(args.hostname, args.username, args.password, conn_mode=args.connection_mode)
    vr.start()
