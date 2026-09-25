"""Validate the product family and optional FortiOS version constraint."""

import os
import re
from dataclasses import dataclass

from .base import Feature
from .image_info import FortiOSVersion


VERSION_CONSTRAINT = re.compile(r"^\d+(?:\.\d+){0,3}(?:-\d+(?:\.\d+){0,3})?$")


@dataclass(frozen=True)
class VersionBound:
    values: tuple[int, ...]
    is_upper: bool = False

    def matches(self, version: FortiOSVersion) -> bool:
        actual = (version.major, version.minor, version.patch, version.build)
        if self.is_upper:
            return actual <= self.as_tuple(fill=999999)
        return actual >= self.as_tuple(fill=0)

    def as_tuple(self, fill):
        return self.values + (fill,) * (4 - len(self.values))


@dataclass(frozen=True)
class VersionConstraint:
    minimum: VersionBound
    maximum: VersionBound | None = None

    def matches(self, version):
        return self.minimum.matches(version) and (
            self.maximum is None or self.maximum.matches(version)
        )


def parse_version_constraint(value):
    if not value:
        return None
    value = value.strip()
    if not VERSION_CONSTRAINT.fullmatch(value):
        raise ValueError(
            "FOS_PRODUCT_VERSION must contain one to four numeric components "
            "or an inclusive range such as 7.2-8.0.1"
        )
    parts = value.split("-")
    parsed = [tuple(int(component) for component in part.split(".")) for part in parts]
    minimum = VersionBound(parsed[0])
    if len(parsed) == 2:
        maximum = VersionBound(parsed[1], is_upper=True)
    else:
        # A missing component is a wildcard for a single-version constraint:
        # 8 means 8.x, 8.0 means 8.0.x, and 7.2.6 means any build of 7.2.6.
        maximum = VersionBound(parsed[0], is_upper=True)
    constraint = VersionConstraint(minimum, maximum)
    if maximum is not None and minimum.as_tuple(0) > maximum.as_tuple(999999):
        raise ValueError("FOS_PRODUCT_VERSION range minimum must not exceed maximum")
    return constraint


class ValidateProduct(Feature):
    """Reject an image whose reported product or version is not expected."""

    def __init__(self, vm, commander, expected_product):
        super().__init__(vm, commander, "product-validation")
        self.expected_product = expected_product
        self.skip = os.getenv("FOS_SKIP_PRODUCT_CHECK", "").strip().lower() == "true"
        self.constraint = None if self.skip else parse_version_constraint(
            os.getenv("FOS_PRODUCT_VERSION")
        )

    def activate(self):
        if self.skip:
            self.commander.feature_complete(self)
            return
        product = self.vm.fos_product
        version = self.vm.fos_version
        if product != self.expected_product:
            raise RuntimeError(
                f"Image product mismatch: expected {self.expected_product}, got {product}"
            )
        if self.constraint is not None and (version is None or not self.constraint.matches(version)):
            expected = os.getenv("FOS_PRODUCT_VERSION")
            actual = str(version) if version is not None else "unknown"
            raise RuntimeError(f"Image version mismatch: expected {expected}, got {actual}")
        self.commander.logger.info("Validated product %s (%s)", product, version)
        self.commander.feature_complete(self)

    def on_block_complete(self):
        self.commander.feature_complete(self)
