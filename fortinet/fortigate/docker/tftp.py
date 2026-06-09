# In pass-through mode, we also spin up a tftp server, but in this case we create a new namespace
# inside the container that simulates the IP addressing of the host.
# we redirect traffic to this ns by using tc flower filters
import logging
from abc import abstractmethod, ABCMeta

import vrnetlab

TFTP_FAKEHOST_VETH_MAC_ADDR = "3a:3a:3a:3a:3a:3a"


class _TFTPLauncher(metaclass=ABCMeta):
    @abstractmethod
    def launch(self, addr, port, directory): ...


class _HostForwardedLauncher(_TFTPLauncher):
    def launch(self, addr, port, directory):
        logger = logging.getLogger()
        logger.info("Launching TFTP Server in Host-Forwarded mode")
        vrnetlab.run_command(
            [
                "in.tftpd",
                "--listen",
                "--user",
                "root",
                "-a",
                f"{addr}:{port}",
                "-s",
                "-c",
                "-v",
                "-p",
                directory,
            ]
        )

        # make tftpboot writable for saving SR OS config
        vrnetlab.run_command(["chmod", "-R", "777", directory])


class _PassthroughLauncher(_TFTPLauncher):

    def launch(self, addr, port, directory):
        logger = logging.getLogger()
        logger.info("Launching TFTP Server in Passthrough mode")
        # In management pass-through mode the container runs a tftp server in a dedicated namepace.
        # This namespace will use the IPv4 default gateway of the container as interface
        # tc flower rules will intercept tftp traffic and redirect it to this namespace
        # create namespace
        vrnetlab.run_command("ip netns add fakehost".split())
        # create vethts: FA in fakehost ns, RA in "root" ns
        vrnetlab.run_command("ip link add FA type veth peer name RA".split())
        # assign FA veth to ns
        vrnetlab.run_command("ip link set FA netns fakehost".split())
        # enable veth root ns
        vrnetlab.run_command("ip link set RA up".split())
        # enable loop in ns
        vrnetlab.run_command("ip netns exec fakehost ip link set dev lo up".split())
        # enable veth in fakehost ns
        vrnetlab.run_command("ip netns exec fakehost ip link set FA up".split())
        # assign a dummy mac that will not collide with the real docker bridge mac address
        vrnetlab.run_command(
            f"ip netns exec fakehost  ip link set dev FA address {TFTP_FAKEHOST_VETH_MAC_ADDR}".split()
        )
        # configure a temporary ip address so the tftp server can start.
        # modified later in the startup process in the create_tc_tap_mgmt_ifup function
        vrnetlab.run_command(
            "ip netns exec fakehost ip addr add 169.254.254.254/16 dev FA".split()
        )
        # block arp responses in fakehost namespace so it doesn't interfere with root namespace
        vrnetlab.run_command(
            "ip netns exec fakehost sysctl -w net.ipv4.conf.all.arp_ignore=8".split()
        )
        # start tftp in ns, assign ports to server so it's easier to track it with flower filters
        vrnetlab.run_command(
            [
                "ip",
                "netns",
                "exec",
                "fakehost",
                "in.tftpd",
                "--listen",
                "--user",
                "root",
                "-a",
                f"{addr}:{port}",
                "-R",
                "52400:52500",
                "-s",
                "-c",
                "-v",
                "-p",
                directory,
            ]
        )


class TFTPServer(_TFTPLauncher):
    def __init__(self, mgmt_passthrough=False):
        if mgmt_passthrough:
            self.launcher = _PassthroughLauncher()
        else:
            self.launcher = _HostForwardedLauncher()

    def launch(self, addr="0.0.0.0", port=69, directory="/tftpboot"):
        self.launcher.launch(addr, port, directory)
