"""Unit tests for dwipe.SataTool hdparm output parsing and verdict logic.

These use ``SataTool.__new__`` to avoid the constructor (which reads sysfs and
touches the real system) and exercise the pure regex-based parsing.
"""
from types import SimpleNamespace

import pytest

from dwipe.SataTool import SataTool


def parse(text):
    tool = SataTool.__new__(SataTool)  # bypass __init__
    return tool._parse_output_to_secures(text)


HEALTHY_OUTPUT = """ATA device, with non-removable media
\tModel Number:       ST1000LM035-1RK172
\tSerial Number:      WDE12345
\tFirmware Revision:  SDM1
Security: 
\tMaster password revision code = 65534
\t\tsupported
\tnot\tenabled
\tnot\tlocked
\tnot\tfrozen
\tnot\texpired: security count
\t\tsupported: enhanced erase
\t2min for SECURITY ERASE UNIT. 2min for ENHANCED SECURITY ERASE UNIT. 
Commands/features:
\tEnabled\tSupported:
\t   *\tSMART feature set
\t   *\tSANITIZE feature set
\t   *\tBLOCK_ERASE_EXT command
\t   *\tCRYPTO_SCRAMBLE_EXT command
\t   *\tOVERWRITE_EXT command
"""

FROZEN_OUTPUT = """ATA device
Security: 
\tMaster password revision code = 65534
\t\tsupported
\tnot\tenabled
\tnot\tlocked
\tfrozen
\tnot\texpired: security count
\t2min for SECURITY ERASE UNIT. 
Commands/features:
"""


def test_parse_healthy_security_block():
    secures = parse(HEALTHY_OUTPUT)
    assert secures.has_security_feature is True
    assert secures.supported is True
    assert secures.enabled is False
    assert secures.locked is False
    assert secures.frozen is False
    assert secures.expired is False
    assert secures.enhanced_erase_supported is True
    assert secures.erase_est_secs == [120, 120]


def test_parse_sanitize_capabilities():
    secures = parse(HEALTHY_OUTPUT)
    assert secures.sanitize_supported is True
    assert secures.sanitize_crypto_supported is True
    assert secures.sanitize_block_supported is True
    assert secures.sanitize_overwrite_supported is True


def test_parse_frozen_drive():
    secures = parse(FROZEN_OUTPUT)
    assert secures.frozen is True
    assert secures.enabled is False  # "not enabled" must not match


def test_parse_no_security_block():
    secures = parse("ATA device\n\tModel Number: THUMB DRIVE\n")
    assert secures.has_security_feature is False


# ---------------------------------------------------------------------------
# get_wipe_verdict
# ---------------------------------------------------------------------------
def verdict_for(**attrs):
    tool = SataTool.__new__(SataTool)
    base = dict(has_security_feature=True, supported=True, frozen=False,
                expired=False, locked=False)
    base.update(attrs)
    tool.secures = SimpleNamespace(**base)
    return tool.get_wipe_verdict()


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"has_security_feature": False}, "DumbDevice"),
        ({"supported": False}, "Unsupported"),
        ({"frozen": True}, "Frozen"),
        ({"expired": True}, "SecurityExpired"),
        ({"locked": True}, "Locked"),
        ({}, "OK"),
    ],
)
def test_get_wipe_verdict(attrs, expected):
    assert verdict_for(**attrs) == expected


# ---------------------------------------------------------------------------
# "not supported" detection (tab-separated hdparm tokens)
# ---------------------------------------------------------------------------
def test_parse_drive_that_is_not_supported():
    output = "Security: \n\tMaster password revision code = 65534\n\tnot\tsupported\n"
    secures = parse(output)
    assert secures.has_security_feature is True
    assert secures.supported is False
