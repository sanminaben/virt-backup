
import os
import pytest

from virt_backup.virt_backup import DomBackup


CUR_PATH = os.path.dirname(os.path.realpath(__file__))


class MockDomain():
    """
    Simulate a libvirt domain
    """
    def XMLDesc(self):
        """
        Return the definition of a testing domain
        """
        with open(os.path.join(CUR_PATH, "testdomain.xml")) as dom_xmlfile:
            dom_xml = "".join(dom_xmlfile.readlines())
        return dom_xml

    def __init__(self, _conn, *args, **kwargs):
        self._conn = _conn


@pytest.fixture
def fixture_build_mock_domain(mocker):
    return MockDomain(_conn=mocker.stub())


def test_get_disks(fixture_build_mock_domain):
    dombkup = DomBackup(dom=fixture_build_mock_domain)
    expected_disks = {
        "vda": {
            "src": "/var/lib/libvirt/images/test-disk-1.qcow2",
            "type": "qcow2",
        },
        "vdb": {
            "src": "/var/lib/libvirt/images/test-disk-2.qcow2",
            "type": "qcow2",
        }
    }

    assert dombkup._get_disks() == expected_disks


def test_get_disks_with_filter(fixture_build_mock_domain):
    dombkup = DomBackup(dom=fixture_build_mock_domain)
    expected_disks = {
        "vda": {
            "src": "/var/lib/libvirt/images/test-disk-1.qcow2",
            "type": "qcow2",
        },
    }

    assert dombkup._get_disks("vda") == expected_disks


def test_add_disks(fixture_build_mock_domain):
    """
    Create a DomBackup with only one disk (vda) and test to add vdb
    """
    disks = {
        "vda": {
            "src": "/var/lib/libvirt/images/test-disk-1.qcow2",
            "type": "qcow2",
        },
    }
    dombkup = DomBackup(dom=fixture_build_mock_domain, _disks=disks)
    expected_disks = {
        "vda": {
            "src": "/var/lib/libvirt/images/test-disk-1.qcow2",
            "type": "qcow2",
        },
    }
    assert dombkup.disks == expected_disks

    dombkup.add_disks("vdb")
    expected_disks = {
        "vda": {
            "src": "/var/lib/libvirt/images/test-disk-1.qcow2",
            "type": "qcow2",
        },
        "vdb": {
            "src": "/var/lib/libvirt/images/test-disk-2.qcow2",
            "type": "qcow2",
        }
    }
    assert dombkup.disks == expected_disks


def test_add_not_existing_disk(fixture_build_mock_domain):
    """
    Create a DomBackup and test to add vdc
    """
    dombkup = DomBackup(dom=fixture_build_mock_domain)
    with pytest.raises(KeyError):
        dombkup.add_disks("vdc")


def test_get_snapshot_xml(fixture_build_mock_domain):
    dombkup = DomBackup(dom=fixture_build_mock_domain)
    expected_xml = (
        "<domainsnapshot>\n"
        "  <description>Pre-backup external snapshot</description>\n"
        "  <disks>\n"
        "    <disk name=\"vda\" snapshot=\"external\"/>\n"
        "    <disk name=\"vdb\" snapshot=\"external\"/>\n"
        "  </disks>\n"
        "</domainsnapshot>\n"
    )
    assert dombkup.gen_snapshot_xml() == expected_xml