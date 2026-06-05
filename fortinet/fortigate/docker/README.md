# vrnetlab / Fortinet Fortigaste v7
=======================
Experimental support for Fortinet fortigate launched by containerlab.

## Building the docker image
Add your qcow2 image to the root of this folder.
Naming format: fortios-vX.Y.Z.qcow2

`make docker-build-fortigate`

## Running the docker image
`make docker-run-fortigate`


## Usage notes

### host-forwarded management interface

If using this mode with DHCP, a default route will be created using the DHCP 
configuration. This can collide with your need to have a default route in your
lab. You can resolve this by applying this config:

config router static
    edit <route-id>
        set dst <mgmt subnet>
        set gateway <QEMU gateway>
        set port port1
    next
end

config system interface
    edit port1
       set defaultgw disable
    next
end

**Setting defaultgw disable before the route is ready will cause connection-loss**

The QEMU Gateway you can find like this:

While still having defaultgw enabled run:

```
get router info routing-table 

br-fgt (Interim)# get router info routing-table details 
Codes: K - kernel, C - connected, S - static, R - RIP, B - BGP
       O - OSPF, IA - OSPF inter area
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
       E1 - OSPF external type 1, E2 - OSPF external type 2
       i - IS-IS, L1 - IS-IS level-1, L2 - IS-IS level-2, ia - IS-IS inter area
       V - BGP VPNv4, E - BGP EVPN, L - Leaked, D - Directly leaked
       * - candidate default

Routing table for VRF=0
S*      0.0.0.0/0 [5/0] via 10.0.0.2, port1, [1/0] <-------THIS ONE
C       10.0.0.0/24 is directly connected, port1
C       10.10.49.0/24 is directly connected, port2
C       198.18.0.0/30 is directly connected, port3

```