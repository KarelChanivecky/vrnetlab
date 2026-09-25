# Fortinet FortiProxy

FortiProxy support uses the shared FortiOS launcher in `fortinet/common/fos`.
Place a FortiProxy `qcow2` image in this directory, preferably named
`fortiproxy-vX.Y.Z.qcow2`, then run:

```text
make docker-build-fortiproxy
```

The launcher rejects images that report the wrong product family. Set
`FOS_SKIP_PRODUCT_CHECK=true` only when that validation is intentionally not
possible. `FOS_PRODUCT_VERSION` accepts a release prefix (`8`, `8.0`,
`7.2.6`), an exact four-part build (`7.2.6.465`), or an inclusive range such
as `7.2-8.0.1`.

The common `FOS_*` environment variables documented by the FortiGate image
are supported here as well.
