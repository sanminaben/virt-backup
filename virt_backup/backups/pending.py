import arrow
import defusedxml.lxml
import json
import libvirt
import logging
import lxml.etree
import os
import subprocess
import tarfile

import virt_backup
from virt_backup.domains import get_xml_block_of_disk
from virt_backup.tools import copy_file
from . import _BaseDomBackup
from .snapshot import DomExtSnapshot


logger = logging.getLogger("virt_backup")


def build_dom_backup_from_pending_info(
        pending_info, backup_dir, conn, callbacks_registrer
):
    backup = DomBackup(
        dom=conn.lookupByName(pending_info["domain_name"]),
        target_dir=backup_dir,
        dev_disks=tuple(pending_info.get("disks", {}).keys()),
        compression=pending_info.get("compression", "tar"),
        compression_lvl=pending_info.get("compression_lvl", None),
        callbacks_registrer=callbacks_registrer
    )
    backup.pending_info = pending_info

    return backup


class DomBackup(_BaseDomBackup):
    """
    Libvirt domain backup
    """
    def __init__(self, dom, target_dir=None, dev_disks=None, compression="tar",
                 compression_lvl=None, conn=None, timeout=None, disks=None,
                 ext_snapshot_helper=None, callbacks_registrer=None):
        """
        :param dev_disks: list of disks dev names to backup. Disks will be
                          searched in the domain to pull more informations, and
                          an exception will be thrown if one of them is not
                          found
        :param disks: dictionary of disks to backup, in this form:
                      `{"src": disk_path, "type": disk_format}`. Prefer
                      using dev disks when possible.
        """
        #: domain to backup. Has to be a libvirt.virDomain object
        self.dom = dom

        #: directory where backups will be saved
        self.target_dir = target_dir

        #: disks to backups. If None, will backup every vm disks
        self.disks = {}
        if dev_disks:
            self.disks.update(self._get_self_domain_disks(*dev_disks))
        if disks:
            self.disks.update(self._get_self_domain_disks(*disks))
        if not self.disks:
            self.disks = self._get_self_domain_disks()

        #: string indicating how to compress the backups:
        #    * None: no compression, backups will be only copied
        #    * "tar": backups will not be compressed, but packaged in a tar
        #    * "gz"/"bz2"/"xz": backups will be compressed in a tar +
        #        compression selected. For more informations, read the
        #        documentation about the mode argument of tarfile.open
        self.compression = compression

        #: If compression, indicates the lvl to use
        self.compression_lvl = compression_lvl

        #: libvirt connection to use. If not sent, will use the connection used
        #  for self.domain
        self.conn = self.dom._conn if conn is None else conn

        #: timeout when waiting for the block pivot to end. Infinite wait if
        #  timeout is None
        self.timeout = timeout

        #: droppable helper to take and clean external snapshots. Can be
        #  construct with an ext_snapshot_helper to clean the snapshots of an
        #  aborted backup. Starting a backup will erase this helper.
        self._ext_snapshot_helper = ext_snapshot_helper

        #: used to redistribute events received by libvirt, as one event cannot
        #  be registered for multiple times. Necessary if no
        #  `ext_snapshot_helper` is given.
        self._callbacks_registrer = callbacks_registrer

        if not (ext_snapshot_helper or callbacks_registrer):
            raise AttributeError(
                "callbacks_registrer needed if no ext_snapshot_helper is given"
            )

        #: useful info collected during a pending backup, allowing to clean
        #  the backup if anything goes wrong
        self.pending_info = {}

        #: Used as lock when the backup is already running
        self._running = False

    @property
    def running(self):
        return self._running

    def add_disks(self, *dev_disks):
        """
        Add disk by dev name

        .. warning::

            Adding a disk during a backup is not recommended, as the current
            disks list could be inaccurate. It will pull the informations
            about the current disks attached to the domain, but the backup
            process creates temporary external snapshots, changing the current
            disks attached. This should not be an issue when the backingStore
            property will be correctly handled, but for now it is.

        :param dev_disk: dev name of the new disk to backup. If not indicated,
                         will add all disks.
        """
        dom_all_disks = self._get_self_domain_disks()
        if not dev_disks:
            self.disks = dom_all_disks
        for dev in dev_disks:
            if dev in self.disks:
                continue
            self.disks[dev] = dom_all_disks[dev]

    def start(self):
        """
        Start the entire backup process for all disks in self.disks
        """
        assert not self.running
        assert self.dom and self.target_dir

        backup_target = None
        logger.info("%s: Backup started", self.dom.name())
        definition = self.get_definition()
        definition["disks"] = {}

        if not os.path.exists(self.target_dir):
            logger.debug("%s: create dir %s", self.dom.name(), self.target_dir)
            os.mkdir(self.target_dir)
        try:
            self._running = True
            self._ext_snapshot_helper = DomExtSnapshot(
                self.dom, self.disks, self._callbacks_registrer, self.conn,
                self.timeout
            )

            snapshot_date, definition = (
                self._snapshot_and_save_date(definition)
            )

            backup_target = (
                self.target_dir if self.compression is None
                else self.get_new_tar(self.target_dir, snapshot_date)
            )

            # TODO: handle backingStore cases
            for disk, prop in self.disks.items():
                self._backup_disk(disk, prop, backup_target, definition)
                self._ext_snapshot_helper.clean_for_disk(disk)

            self._dump_json_definition(definition)
            self.post_backup(backup_target)
            self._clean_pending_info()
        except:
            self.clean_aborted()
            raise
        finally:
            self._running = False
        logger.info("%s: Backup finished", self.dom.name())

    def _snapshot_and_save_date(self, definition):
        """
        Take a snapshot of all disks to backup and mark date into definition

        All disks are frozen when external snapshots have been taken, so we
        consider this step to be the backup date.

        :return snapshot_date, definition: return snapshot_date as `arrow`
            type, and the updated definition
        """
        snapshot_metadatas = self._ext_snapshot_helper.start()

        # all of our disks are snapshot, so the backup date is right now
        definition["date"] = snapshot_metadatas["date"].timestamp

        self.pending_info = definition.copy()
        self.pending_info["disks"] = {
            disk: {
                "src": prop["src"],
                "snapshot": snapshot_metadatas["disks"][disk]["snapshot"]
            } for disk, prop in self.disks.items()
        }
        self._dump_pending_info()

        return snapshot_metadatas["date"], definition

    def get_definition(self):
        """
        Get a json defining this backup
        """
        return {
            "compression": self.compression,
            "compression_lvl": self.compression_lvl,
            "domain_id": self.dom.ID(), "domain_name": self.dom.name(),
            "domain_xml": self.dom.XMLDesc(), "version": virt_backup.VERSION
        }

    def get_new_tar(self, target, snapshot_date):
        """
        Get a new tar for this backup

        self._main_backup_name_format will be used to generate a new tar name

        :param target: directory path that will contain our tar. If not exists,
                       will be created.
        """
        extra_args = {}
        if self.compression not in (None, "tar"):
            mode = "w:{}".format(self.compression)
            extension = "tar.{}".format(self.compression)
            if self.compression == "xz":
                extra_args["preset"] = self.compression_lvl
            else:
                extra_args["compresslevel"] = self.compression_lvl
        else:
            mode = "w"
            extension = "tar"

        if not os.path.isdir(target):
            os.makedirs(target)

        complete_path = os.path.join(
            target,
            "{}.{}".format(
                self._main_backup_name_format(snapshot_date), extension
            )
        )
        if os.path.exists(complete_path):
            raise FileExistsError()
        return tarfile.open(complete_path, mode, **extra_args)

    def _backup_disk(self, disk, disk_properties, backup_target, definition):
        """
        Backup a disk and complete the definition by adding this disk

        :param disk: diskname to backup
        :param disk_properties: dictionary discribing our disk (typically
                                contained in self.disks[disk])
        :param backup_target: target path of our backup
        :param definition: dictionary representing the domain backup
        """
        snapshot_date = arrow.get(definition["date"]).to("local")
        logger.info("%s: Backup disk %s", self.dom.name(), disk)
        target_img = "{}.{}".format(
            self._disk_backup_name_format(snapshot_date, disk),
            disk_properties["type"]
        )
        self.pending_info["disks"][disk]["target"] = target_img
        self._dump_pending_info()

        if definition.get("disks", None) is None:
            definition["disks"] = {}
        definition["disks"][disk] = target_img

        backup_path = self.backup_img(
            disk_properties["src"], backup_target, target_img
        )
        if self.compression:
            if not definition.get("tar", None):
                # all disks will be compacted in the same tar, so already
                # store it in definition if it was not set before
                definition["tar"] = os.path.basename(backup_path)

    def _disk_backup_name_format(self, snapdate, disk_name, *args, **kwargs):
        """
        Backup name format for each disk when no compression/compacting is set

        :param snapdate: date when external snapshots have been created
        :param disk_name: disk name currently being backup
        """
        return (
            "{}_{}".format(self._main_backup_name_format(snapdate), disk_name)
        )

    def backup_img(self, disk, target, target_filename=None):
        """
        Backup a disk image

        :param disk: path of the image to backup
        :param target: dir or filename to copy into/as. If self.compress,
                       target has to be a tarfile.TarFile
        :param target_filename: destination file will have this name, or keep
                                the original one. target has to be a dir
                                (if not exists, will be created)
        :returns backup_path: complete path of our backup
        """
        if self.compression:
            backup_path = self._add_img_to_tarfile(
                disk, target, target_filename
            )
        else:
            backup_path = self._copy_img_to_file(disk, target, target_filename)

        logger.debug("%s: %s successfully copied", self.dom.name(), disk)
        return os.path.abspath(backup_path)

    def _add_img_to_tarfile(self, img, target, target_filename):
        """
        :param img: source img path
        :param target: tarfile.TarFile where img will be added
        :param target_filename: img name in the tarfile
        """
        logger.debug("%s: Copy %s", self.dom.name(), img)
        if self.compression == "xz":
            backup_path = target.fileobj._fp.name
        else:
            backup_path = target.fileobj.name
        self.pending_info["tar"] = os.path.basename(backup_path)
        self._dump_pending_info()

        target.add(img, arcname=target_filename)

        return backup_path

    def _copy_img_to_file(self, img, target, target_filename=None):
        """
        :param img: source img path
        :param target: target is a directory if target_filename is set, or an
                       existing directory or a destination file
        :param target_filename: name of the img copy
        """
        if target_filename is not None:
            if not os.path.isdir(target):
                os.makedirs(target)
        target = os.path.join(target, target_filename or img)
        logger.debug("%s: Copy %s as %s", self.dom.name(), img, target)
        copy_file(img, target)

        return target

    def post_backup(self, backup_target):
        """
        Post backup process

        Unregister callback and close backup_target if is tarfile
        """
        if self._ext_snapshot_helper is not None:
            self._ext_snapshot_helper.clean()
            self._ext_snapshot_helper = None
        if isinstance(backup_target, tarfile.TarFile):
            backup_target.close()
        self._running = False

    def _parse_dom_xml(self):
        """
        Parse the domain's definition
        """
        return defusedxml.lxml.fromstring(self.dom.XMLDesc())

    def _dump_json_definition(self, definition):
        """
        Dump the backup definition as json

        Definition will describe our backup, with the date, backuped
        disks names and other informations
        """
        backup_date = arrow.get(definition["date"]).to("local")
        definition_path = os.path.join(
            self.target_dir,
            "{}.{}".format(self._main_backup_name_format(backup_date), "json")
        )
        with open(definition_path, "w") as json_definition:
            json.dump(definition, json_definition, indent=4)

    def _dump_pending_info(self):
        """
        Dump the temporary changes done, as json

        Useful
        """
        json_path = self._get_pending_info_json_path()
        with open(json_path, "w") as json_pending_info:
            json.dump(self.pending_info, json_pending_info, indent=4)

    def _clean_pending_info(self):
        os.remove(self._get_pending_info_json_path())
        self.pending_info = {}

    def _get_pending_info_json_path(self):
        backup_date = arrow.get(self.pending_info["date"]).to("local")
        json_path = os.path.join(
            self.target_dir,
            "{}.{}.pending".format(
                self._main_backup_name_format(backup_date), "json"
            )
        )
        return json_path

    def _main_backup_name_format(self, snapdate, *args, **kwargs):
        """
        Main backup name format

        Extracted in its own function so it can be easily override

        :param snapdate: date when external snapshots have been created
        """
        str_snapdate = snapdate.strftime("%Y%m%d-%H%M%S")
        return "{}_{}_{}".format(str_snapdate, self.dom.ID(), self.dom.name())

    def clean_aborted(self):
        is_ext_snap_helper_needed = (
            not self._ext_snapshot_helper and
            self.pending_info.get("disks", None)
        )
        if is_ext_snap_helper_needed:
            self._ext_snapshot_helper = DomExtSnapshot(
                self.dom, self.disks, self._callbacks_registrer, self.conn,
                self.timeout
            )
            self._ext_snapshot_helper.metadatas = {
                "disks": {
                    disk: {"src": val["src"], "snapshot": val["snapshot"]}
                    for disk, val in self.pending_info["disks"].items()
                }
            }

        if self._ext_snapshot_helper:
            self._ext_snapshot_helper.clean()

        if self.pending_info.get("tar", None):
            self._clean_aborted_tar()
        else:
            self._clean_aborted_non_tar_img()
        try:
            self._clean_pending_info()
        except KeyError:
            # Pending info had no time to be filled, so had not be dumped
            pass

    def _clean_aborted_tar(self):
        tar_path = self.get_complete_path_of(self.pending_info["tar"])
        if os.path.exists(tar_path):
            self._delete_with_error_printing(tar_path)

    def _clean_aborted_non_tar_img(self):
        for disk in self.pending_info.get("disks", {}).values():
            if not disk.get("target", None):
                continue
            target_path = self.get_complete_path_of(disk["target"])
            if os.path.exists(target_path):
                self._delete_with_error_printing(target_path)

    def compatible_with(self, dombackup):
        """
        Is compatible with dombackup ?

        If the target is the same for both dombackup and self, same thing for
        compression and compression_lvl, self and dombackup are considered
        compatibles.
        """
        def same_dombackup_and_self_attr(attr):
            return getattr(self, attr) == getattr(dombackup, attr)

        attributes_to_compare = (
            "target_dir", "compression", "compression_lvl"
        )
        for a in attributes_to_compare:
            if not same_dombackup_and_self_attr(a):
                return False

        same_domain = dombackup.dom.ID() == self.dom.ID()
        return same_domain

    def merge_with(self, dombackup):
        self.add_disks(*dombackup.disks.keys())
        timeout = self.timeout or dombackup.timeout
        self.timeout = timeout

    def get_complete_path_of(self, filename):
        return os.path.join(self.target_dir, filename)
