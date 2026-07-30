# Fortinet FortiGate

Fortinet FortiGate/FortiOS support for vrnetlab and Containerlab.

The launcher supports recent FortiGate VM images, including FortiOS 8.0, and
similar Fortinet CLI families such as FortiProxy where the boot and prompt
patterns are compatible.

## Build

Place one FortiOS `qcow2` image in this directory. The Makefile expects the
image name to use this format:

```text
fortios-vX.Y.Z.qcow2
```

Build the image:

```bash
make docker-build-fortigate
```

Run the image manually:

```bash
make docker-run-fortigate
```

## Containerlab

Use `kind: fortinet_fortigate`.

```yaml
name: fgt-lab

topology:
  nodes:
    fgt:
      kind: fortinet_fortigate
      image: vr-fortios:8.0.0
      enforce-startup-config: true
      startup-config: configs/fgt.conf
      license: licenses/appliance.lic
      credentials:
        username: admin
        password: admin
      env:
        CLAB_MGMT_PASSTHROUGH: "true"
```

### Node Options

`startup-config` mounts a FortiOS config file that the launcher applies after
bootstrap, hostname setup, license handling, admin setup, and baseline config
capture. The file is available inside the container as
`/config/startup-config.cfg`.

`license` mounts a FortiGate VM license. The launcher expects it inside the
container as `/tftpboot/appliance.lic`, installs it with
`execute restore vmlicense tftp`, handles the reboot, and waits for license
status to leave `Pending`.

`credentials` sets the desired final administrator account. If omitted, the
final account is `admin` / `admin`. The bootstrap flow handles first-login
password change prompts and FortiOS versions that initially accept a blank
default password.

`enforce-startup-config: true` is recommended so Containerlab always mounts and
applies the intended startup config.

## Environment Variables

| Variable | Default | Values | Description |
| --- | --- | --- | --- |
| `CLAB_MGMT_PASSTHROUGH` | `true` | `true`, `false` | Selects management wiring. `true` uses tap/tc passthrough so the FortiGate management interface participates directly in the Containerlab management network. `false` uses a host-forwarded bridge inside the vrnetlab container. |
| `FOS_DISK_SPECS` | unset | comma-separated `qemu-img create` sizes, for example `10g` or `10g,10g` | Adds extra virtio disks. One disk becomes the FortiGate log disk. Additional disks are formatted during bootstrap; the second disk is expected to become WAN optimization storage on FortiOS versions that support it. |
| `FORTIGATE_UUID` | random UUID | UUID string | Sets the QEMU VM UUID. If unset, a new UUID is generated for each launch. |
| `FOS_LOG_ENCODED` | `false` | `true`, `false` | Logs encoded serial bytes instead of decoded text when enabled. Useful for debugging prompt or terminal parsing issues. |

Containerlab also passes the usual vrnetlab launch arguments such as hostname,
username, password, and connection mode. For manual runs these are available as
launcher arguments:

```text
--hostname
--username
--password
--connection-mode
--trace
```

## Management Modes

### Passthrough Management

`CLAB_MGMT_PASSTHROUGH=true` is the default. The launcher creates a tap device
for `port1` and uses tc rules to redirect management traffic between the
FortiGate VM and the container management interface. TCP serial ports
`5000-5007` are passed through to the container instead of being redirected to
the VM management interface.

The TFTP server used for license installation runs in a dedicated namespace and
is reachable from the FortiGate through the management gateway address.

### Host-Forwarded Management

`CLAB_MGMT_PASSTHROUGH=false` creates an internal `br-mgmt` bridge and configures
FortiGate `port1` with:

```text
172.31.255.30/30 via 172.31.255.29
200::1/127 via 200::
```

TCP traffic that enters the container, except the serial console on port `5000`,
is DNATed to the FortiGate management address. UDP traffic is also DNATed so
license TFTP can work.

If FortiOS later receives DHCP on the management interface, disable the
FortiGate default gateway only after adding a route back to the management
subnet. Disabling it first can cut off management access.

## Startup Config

The startup config is applied line by line after the launcher has finished its
own bootstrap commands. Keep it as ordinary FortiOS CLI config:

```text
config system global
    set alias "lab-fgt"
end
```

The importer validates basic `config` / `edit` / `next` / `end` nesting and
fails startup on malformed structure.

The launcher also sets baseline system configuration needed for lab operation,
including management interface addressing, FortiGuard interface selection, DNS,
hostname, and the final administrator account.

## Licensing

When `/tftpboot/appliance.lic` exists, the launcher installs it during startup.
License installation may reboot the VM and may remove the active admin session
when the status changes to `VALID`; the launcher handles re-login and continues
bootstrap.

After installation, the launcher polls `get system status` until the license
field is no longer `Pending` or until the internal
`FOS_LICENSE_STATUS_TIMEOUT_SECONDS` constant expires.

## Extra Disks

Set `FOS_DISK_SPECS` to add disks:

```yaml
env:
  FOS_DISK_SPECS: "10g,10g"
```

This creates `empty1.qcow2`, `empty2.qcow2`, and so on, and attaches them as
virtio drives. FortiOS normally formats the first additional disk as log
storage. The launcher formats remaining configured disks during bootstrap.

Expected FortiOS storage usage for common test cases:

```text
FOS_DISK_SPECS unset      -> no configured storage usage
FOS_DISK_SPECS="10g"     -> order 1 usage log
FOS_DISK_SPECS="10g,10g" -> order 1 usage log, order 2 usage wanopt
```

## Saving Config

Touch `/get-config` inside a running container to ask the launcher to capture the
current FortiOS config:

```bash
docker exec clab-<lab>-<node> touch /get-config
```

The launcher reconnects to the serial console, runs `show`, compares the result
with the baseline captured before startup config application, and writes the
changed config to:

```text
/config/current.conf
```

The serial connection is closed after capture so the console remains available
for external use. If console pagination was enabled before capture, it is
temporarily disabled and then restored.

## Boot Features

The FortiOS launcher uses a CLI finite-state machine rather than fixed sleeps.
User-visible behavior includes:

- detection of login, password, forced password-change, rejected credentials,
  welcome banner, reboot, shutdown, and command prompts
- buffered prompt matching for fragmented serial output
- hostname update and prompt-pattern update during bootstrap
- default credential handling across FortiOS 6.4, 7.x, and 8.x behavior
- password-policy failure surfaced as a startup error
- explicit failure if no `qcow2` image is present
- full serial output logging at debug level
- trace logging with `--trace`

## Tested Versions

Commit `be1df131b2c000d1ffb79eb941ab8ce4eee07e31` introduced the FortiOS 8.0
support and was tested with:

- FortiGate 8.0.0 build 0167 GA debug image
- FortiGate 7.6.6 build 3652 GA debug image
- FortiGate 7.4.12 build 2902 GA
- FortiGate 7.0.19 build 0696 GA
- FortiGate 6.4.16 build 2098 GA
- FortiProxy 7.6.6 build 1628 GA
- FortiProxy 7.4.13 build 0722 GA debug image
- FortiProxy 7.2.16 build 0465 GA
- FortiProxy 7.0.23 build 0222 GA
