import os
import pytest

from virt_backup.groups import BackupGroup, groups_from_dict
from virt_backup.groups.pattern import (
    pattern_matching_domains_in_libvirt,
    matching_libvirt_domains_from_config
)
from virt_backup.backups import DomBackup, DomExtSnapshotCallbackRegistrer
from virt_backup.exceptions import BackupsFailureInGroupError

from helper.virt_backup import MockDomain, build_backup_group, build_dombackup


class TestBackupGroup():
    def test_self(self):
        backup_group = BackupGroup()

        assert len(backup_group.backups) == 0

    def test_self_with_domain(self, build_mock_domain, build_mock_libvirtconn):
        dom = build_mock_domain
        backup_group = build_backup_group(
            build_mock_libvirtconn, domlst=((dom, None),)
        )

        assert len(backup_group.backups) == 1
        assert backup_group.backups[0].dom == dom

    def test_add_domain(self, build_mock_domain, build_mock_libvirtconn):
        dom = build_mock_domain
        backup_group = build_backup_group(build_mock_libvirtconn)

        backup_group.add_domain(dom)

        assert len(backup_group.backups) == 1
        assert backup_group.backups[0].dom == dom

    def test_dedup_add_domain(self, build_mock_domain, get_backup_group):
        """
        Test to add 2 times the same backup and check that it's not duplicated
        """
        dom = build_mock_domain
        backup_group = get_backup_group

        backup_group.add_domain(dom)
        assert len(backup_group.backups) == 1

    def test_add_dombackup(self, get_backup_group, get_dombackup):
        backup_group = get_backup_group

        backup_group.add_dombackup(get_dombackup)
        assert len(backup_group.backups) == 1

    def test_add_dombackup_dedup(self, build_mock_domain, get_backup_group):
        dom = build_mock_domain
        backup_group = get_backup_group

        backup_group.add_dombackup(build_dombackup(dom, dev_disks=("vda", )))
        backup_group.add_dombackup(build_dombackup(dom, dev_disks=("vdb", )))
        assert len(backup_group.backups) == 1
        assert len(backup_group.backups[0].disks.keys()) == 2

    def test_search(self, build_mock_domain, get_backup_group):
        dom = build_mock_domain
        backup_group = get_backup_group

        dombak = next(backup_group.search(dom))
        assert dombak == backup_group.backups[0]

    def test_search_not_found(self, build_mock_domain, build_mock_libvirtconn):
        dom = build_mock_domain
        backup_group = build_backup_group(build_mock_libvirtconn)

        with pytest.raises(StopIteration):
            next(backup_group.search(dom))

    def test_start(self, get_backup_group, mocker):
        backup_group = get_backup_group
        backup_group.backups[0].start = mocker.stub()

        backup_group.start()
        assert backup_group.backups[0].start.called

    def test_start_with_dir_by_domain(
            self, build_mock_libvirtconn, build_mock_domain, mocker
    ):
        backup_group = build_backup_group(
            build_mock_libvirtconn, domlst=(build_mock_domain, ),
            target_dir="/tmp"
        )
        dombackup = backup_group.backups[0]
        dombackup.start = mocker.stub()

        expected_target_dir = os.path.join("/tmp", dombackup.dom.name())
        backup_group.start()
        assert dombackup.target_dir == expected_target_dir

    def test_start_with_err(self, build_mock_libvirtconn, mocker):
        conn = build_mock_libvirtconn
        backup_group = build_backup_group(
            conn, domlst=(
                MockDomain(_conn=conn),
                MockDomain(_conn=conn, name="test_error")
            )
        )

        def error_start(*args, **kwargs):
            raise Exception()

        backup_group.backups[0].start = error_start
        backup_group.backups[1].start = mocker.stub()

        with pytest.raises(BackupsFailureInGroupError):
            backup_group.start()

        assert backup_group.backups[1].start.called

    def test_start_multithread(self, build_mock_libvirtconn, mocker):
        conn = build_mock_libvirtconn
        backup_group = build_backup_group(
            conn, domlst=(
                MockDomain(_conn=conn),
                MockDomain(_conn=conn),
            )
        )
        for b in backup_group.backups:
            b.start = mocker.stub()

        backup_group.start_multithread(2)

        for b in backup_group.backups:
            assert b.start.called

    def test_start_multithead_with_err(self, build_mock_libvirtconn, mocker):
        conn = build_mock_libvirtconn
        backup_group = build_backup_group(
            conn, domlst=(
                MockDomain(_conn=conn),
                MockDomain(_conn=conn, name="test_error")
            )
        )

        def error_start(*args, **kwargs):
            raise Exception()

        backup_group.backups[0].start = error_start
        backup_group.backups[1].start = mocker.stub()

        with pytest.raises(BackupsFailureInGroupError):
            backup_group.start_multithread(2)

        assert backup_group.backups[1].start.called

    def test_propagate_attr(self, build_mock_libvirtconn, build_mock_domain):
        backup_group = build_backup_group(
            conn=build_mock_libvirtconn, domlst=(build_mock_domain, ),
            compression="xz"
        )
        assert backup_group.backups[0].compression == "xz"

        backup_group.default_bak_param["target_dir"] = "/test"
        assert backup_group.backups[0].target_dir is None
        backup_group.propagate_default_backup_attr()
        assert backup_group.backups[0].target_dir == "/test"

    def test_propagate_attr_multiple_domains(
            self, build_mock_libvirtconn, mocker
    ):
        conn = build_mock_libvirtconn
        backup_group = build_backup_group(
            conn, domlst=(
                MockDomain(_conn=conn),
                MockDomain(_conn=conn),
            ), compression="xz"
        )

        for b in backup_group.backups:
            assert b.compression == "xz"

        backup_group.default_bak_param["target_dir"] = "/test"
        for b in backup_group.backups:
            assert b.target_dir is None

        backup_group.propagate_default_backup_attr()
        for b in backup_group.backups:
            assert b.target_dir == "/test"


def test_pattern_matching_domains_in_libvirt_regex(
        build_mock_libvirtconn_filled
):
    conn = build_mock_libvirtconn_filled
    matches = pattern_matching_domains_in_libvirt("r:^matching.?$", conn)
    domains = tuple(sorted(matches["domains"]))
    exclude = matches["exclude"]

    assert domains == ("matching", "matching2")
    assert not exclude


def test_pattern_matching_domains_in_libvirt_direct_name(
        build_mock_libvirtconn_filled
):
    """
    Test parse_host_pattern directly with a domain name
    """
    conn = build_mock_libvirtconn_filled
    matches = pattern_matching_domains_in_libvirt("matching", conn)
    domains = tuple(sorted(matches["domains"]))
    exclude = matches["exclude"]

    assert domains == ("matching",)
    assert not exclude


def test_pattern_matching_domains_in_libvirt_exclude(
        build_mock_libvirtconn_filled
):
    """
    Test parse_host_pattern with a pattern excluding a domain
    """
    conn = build_mock_libvirtconn_filled
    matches = pattern_matching_domains_in_libvirt("!matching", conn)
    domains = tuple(sorted(matches["domains"]))
    exclude = matches["exclude"]

    assert domains == ("matching",)
    assert exclude


def test_matching_libvirt_domains_from_config(build_mock_libvirtconn_filled):
    conn = build_mock_libvirtconn_filled
    host_config = {"host": "matching", "disks": ["vda", "vdb"]}

    matches = matching_libvirt_domains_from_config(host_config, conn)
    domains = tuple(sorted(matches["domains"]))
    exclude, disks = matches["exclude"], tuple(sorted(matches["disks"]))

    assert domains == ("matching",)
    assert not exclude
    assert disks == ("vda", "vdb")


def test_matching_libvirt_domains_from_config_unexisting(
        build_mock_libvirtconn_filled
):
    """
    Test match_domains_from_config with a non existing domain
    """
    conn = build_mock_libvirtconn_filled
    host_config = {"host": "nonexisting", "disks": ["vda", "vdb"]}

    matches = matching_libvirt_domains_from_config(host_config, conn)
    domains = tuple(sorted(matches["domains"]))
    exclude = matches["exclude"]

    assert domains == tuple()
    assert not exclude


def test_matching_libvirt_domains_from_config_str(
        build_mock_libvirtconn_filled
):
    """
    Test match_domains_from_config with a str pattern
    """
    conn = build_mock_libvirtconn_filled
    host_config = "r:matching\d?"

    matches = matching_libvirt_domains_from_config(host_config, conn)
    domains = tuple(sorted(matches["domains"]))
    exclude = matches["exclude"]

    assert domains == ("matching", "matching2")
    assert not exclude


def test_groups_from_dict(build_mock_libvirtconn_filled):
    """
    Test groups_from_dict with only one group
    """
    conn = build_mock_libvirtconn_filled
    callbacks_registrer = DomExtSnapshotCallbackRegistrer(conn)
    groups_config = {
        "test": {
            "target": "/mnt/test",
            "compression": "tar",
            "hosts": [
                {"host": "r:^matching\d?$", "disks": ["vda", "vdb"]},
                "!matching2", "nonexisting"
            ],
        },
    }

    groups = tuple(groups_from_dict(groups_config, conn, callbacks_registrer))
    assert len(groups) == 1
    test_group = groups[0]

    target, compression = (
        test_group.default_bak_param[k] for k in ("target_dir", "compression")
    )
    assert target == "/mnt/test"
    assert compression == "tar"

    dombackups = test_group.backups
    assert len(dombackups) == 1

    matching_backup = dombackups[0]
    assert matching_backup.dom.name() == "matching"
    assert tuple(sorted(matching_backup.disks.keys())) == ("vda", "vdb")


def test_groups_from_sanitize_dict_all_config_group_param(
        build_mock_libvirtconn_filled
):
    """
    Test with the example config, containing every possible parameter

    Related to issue #13
    """
    conn = build_mock_libvirtconn_filled
    callbacks_registrer = DomExtSnapshotCallbackRegistrer(conn)
    groups_config = {
        "test": {
            "target": "/mnt/test",
            "compression": "tar",
            "autostart": True,
            "hourly": 1,
            "daily": 3,
            "weekly": 2,
            "monthly": 5,
            "yearly": 1,
            "hosts": [
                {"host": "r:^matching\d?$", "disks": ["vda", "vdb"]},
                "!matching2", "nonexisting"
            ],
        },
    }
    group = next(iter(
        groups_from_dict(groups_config, conn, callbacks_registrer)
    ))

    for prop in ("hourly", "daily", "weekly", "monthly", "yearly"):
        assert prop not in group.default_bak_param


def test_groups_from_dict_multiple_groups(
        build_mock_libvirtconn_filled
):
    """
    Test match_domains_from_config with a str pattern
    """
    conn = build_mock_libvirtconn_filled
    callbacks_registrer = DomExtSnapshotCallbackRegistrer(conn)
    groups_config = {
        "test0": {
            "target": "/mnt/test0",
            "compression": "tar",
            "hosts": ["matching2", ],
        },
        "test1": {
            "target": "/mnt/test1",
            "hosts": ["matching", "a"],
        },
    }

    groups = tuple(groups_from_dict(groups_config, conn, callbacks_registrer))
    assert len(groups) == 2
    group0, group1 = groups

    assert sorted((group0.name, group1.name)) == ["test0", "test1"]
