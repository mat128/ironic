# Copyright 2015 FUJITSU LIMITED
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
iRMC Boot Driver
"""

import os
import shutil
import tempfile

from ironic_lib import utils as ironic_utils
from oslo_log import log as logging
from oslo_utils import importutils

from ironic.common import boot_devices
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _, _LE, _LI
from ironic.common import images
from ironic.common import states
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.irmc import common as irmc_common


scci = importutils.try_import('scciclient.irmc.scci')

try:
    if CONF.debug:
        scci.DEBUG = True
except Exception:
    pass

LOG = logging.getLogger(__name__)

REQUIRED_PROPERTIES = {
    'irmc_deploy_iso': _("Deployment ISO image file name. "
                         "Required."),
}

COMMON_PROPERTIES = REQUIRED_PROPERTIES


def _parse_config_option():
    """Parse config file options.

    This method checks config file options validity.

    :raises: InvalidParameterValue, if config option has invalid value.
    """
    error_msgs = []
    if not os.path.isdir(CONF.irmc.remote_image_share_root):
        error_msgs.append(
            _("Value '%s' for remote_image_share_root isn't a directory "
              "or doesn't exist.") %
            CONF.irmc.remote_image_share_root)
    if error_msgs:
        msg = (_("The following errors were encountered while parsing "
                 "config file:%s") % error_msgs)
        raise exception.InvalidParameterValue(msg)


def _parse_driver_info(node):
    """Gets the driver specific Node deployment info.

    This method validates whether the 'driver_info' property of the
    supplied node contains the required or optional information properly
    for this driver to deploy images to the node.

    :param node: a target node of the deployment
    :returns: the driver_info values of the node.
    :raises: MissingParameterValue, if any of the required parameters are
        missing.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    """
    d_info = node.driver_info
    deploy_info = {}

    deploy_info['irmc_deploy_iso'] = d_info.get('irmc_deploy_iso')
    error_msg = _("Error validating iRMC virtual media deploy. Some parameters"
                  " were missing in node's driver_info")
    deploy_utils.check_for_missing_params(deploy_info, error_msg)

    if service_utils.is_image_href_ordinary_file_name(
            deploy_info['irmc_deploy_iso']):
        deploy_iso = os.path.join(CONF.irmc.remote_image_share_root,
                                  deploy_info['irmc_deploy_iso'])
        if not os.path.isfile(deploy_iso):
            msg = (_("Deploy ISO file, %(deploy_iso)s, "
                     "not found for node: %(node)s.") %
                   {'deploy_iso': deploy_iso, 'node': node.uuid})
            raise exception.InvalidParameterValue(msg)

    return deploy_info


def _parse_instance_info(node):
    """Gets the instance specific Node deployment info.

    This method validates whether the 'instance_info' property of the
    supplied node contains the required or optional information properly
    for this driver to deploy images to the node.

    :param node: a target node of the deployment
    :returns:  the instance_info values of the node.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    """
    i_info = node.instance_info
    deploy_info = {}

    if i_info.get('irmc_boot_iso'):
        deploy_info['irmc_boot_iso'] = i_info['irmc_boot_iso']

        if service_utils.is_image_href_ordinary_file_name(
                deploy_info['irmc_boot_iso']):
            boot_iso = os.path.join(CONF.irmc.remote_image_share_root,
                                    deploy_info['irmc_boot_iso'])

            if not os.path.isfile(boot_iso):
                msg = (_("Boot ISO file, %(boot_iso)s, "
                         "not found for node: %(node)s.") %
                       {'boot_iso': boot_iso, 'node': node.uuid})
                raise exception.InvalidParameterValue(msg)

    return deploy_info


def _parse_deploy_info(node):
    """Gets the instance and driver specific Node deployment info.

    This method validates whether the 'instance_info' and 'driver_info'
    property of the supplied node contains the required information for
    this driver to deploy images to the node.

    :param node: a target node of the deployment
    :returns: a dict with the instance_info and driver_info values.
    :raises: MissingParameterValue, if any of the required parameters are
        missing.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    """
    deploy_info = {}
    deploy_info.update(deploy_utils.get_image_instance_info(node))
    deploy_info.update(_parse_driver_info(node))
    deploy_info.update(_parse_instance_info(node))

    return deploy_info


def _setup_deploy_iso(task, ramdisk_options):
    """Attaches virtual media and sets it as boot device.

    This method attaches the given deploy ISO as virtual media, prepares the
    arguments for ramdisk in virtual media floppy.

    :param task: a TaskManager instance containing the node to act on.
    :param ramdisk_options: the options to be passed to the ramdisk in virtual
        media floppy.
    :raises: ImageRefValidationFailed if no image service can handle specified
       href.
    :raises: ImageCreationFailed, if it failed while creating the floppy image.
    :raises: IRMCOperationError, if some operation on iRMC failed.
    :raises: InvalidParameterValue if the validation of the
        PowerInterface or ManagementInterface fails.
    """
    d_info = task.node.driver_info

    deploy_iso_href = d_info['irmc_deploy_iso']
    if service_utils.is_image_href_ordinary_file_name(deploy_iso_href):
        deploy_iso_file = deploy_iso_href
    else:
        deploy_iso_file = _get_deploy_iso_name(task.node)
        deploy_iso_fullpathname = os.path.join(
            CONF.irmc.remote_image_share_root, deploy_iso_file)
        images.fetch(task.context, deploy_iso_href, deploy_iso_fullpathname)

    _setup_vmedia_for_boot(task, deploy_iso_file, ramdisk_options)
    manager_utils.node_set_boot_device(task, boot_devices.CDROM)


def _get_deploy_iso_name(node):
    """Returns the deploy ISO file name for a given node.

    :param node: the node for which ISO file name is to be provided.
    """
    return "deploy-%s.iso" % node.uuid


def _get_boot_iso_name(node):
    """Returns the boot ISO file name for a given node.

    :param node: the node for which ISO file name is to be provided.
    """
    return "boot-%s.iso" % node.uuid


def _prepare_boot_iso(task, root_uuid):
    """Prepare a boot ISO to boot the node.

    :param task: a TaskManager instance containing the node to act on.
    :param root_uuid: the uuid of the root partition.
    :raises: MissingParameterValue, if any of the required parameters are
        missing.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    :raises: ImageCreationFailed, if creating boot ISO
       for BIOS boot_mode failed.
    """
    deploy_info = _parse_deploy_info(task.node)
    driver_internal_info = task.node.driver_internal_info

    # fetch boot iso
    if deploy_info.get('irmc_boot_iso'):
        boot_iso_href = deploy_info['irmc_boot_iso']
        if service_utils.is_image_href_ordinary_file_name(boot_iso_href):
            driver_internal_info['irmc_boot_iso'] = boot_iso_href
        else:
            boot_iso_filename = _get_boot_iso_name(task.node)
            boot_iso_fullpathname = os.path.join(
                CONF.irmc.remote_image_share_root, boot_iso_filename)
            images.fetch(task.context, boot_iso_href, boot_iso_fullpathname)

            driver_internal_info['irmc_boot_iso'] = boot_iso_filename

    # create boot iso
    else:
        image_href = deploy_info['image_source']
        image_props = ['kernel_id', 'ramdisk_id']
        image_properties = images.get_image_properties(
            task.context, image_href, image_props)
        kernel_href = (task.node.instance_info.get('kernel') or
                       image_properties['kernel_id'])
        ramdisk_href = (task.node.instance_info.get('ramdisk') or
                        image_properties['ramdisk_id'])

        deploy_iso_filename = _get_deploy_iso_name(task.node)
        deploy_iso = ('file://' + os.path.join(
            CONF.irmc.remote_image_share_root, deploy_iso_filename))
        boot_mode = deploy_utils.get_boot_mode_for_deploy(task.node)
        kernel_params = CONF.pxe.pxe_append_params

        boot_iso_filename = _get_boot_iso_name(task.node)
        boot_iso_fullpathname = os.path.join(
            CONF.irmc.remote_image_share_root, boot_iso_filename)

        images.create_boot_iso(task.context, boot_iso_fullpathname,
                               kernel_href, ramdisk_href,
                               deploy_iso, root_uuid,
                               kernel_params, boot_mode)

        driver_internal_info['irmc_boot_iso'] = boot_iso_filename

    # save driver_internal_info['irmc_boot_iso']
    task.node.driver_internal_info = driver_internal_info
    task.node.save()


def _get_floppy_image_name(node):
    """Returns the floppy image name for a given node.

    :param node: the node for which image name is to be provided.
    """
    return "image-%s.img" % node.uuid


def _prepare_floppy_image(task, params):
    """Prepares the floppy image for passing the parameters.

    This method prepares a temporary vfat filesystem image, which
    contains the parameters to be passed to the ramdisk.
    Then it uploads the file NFS or CIFS server.

    :param task: a TaskManager instance containing the node to act on.
    :param params: a dictionary containing 'parameter name'->'value' mapping
        to be passed to the deploy ramdisk via the floppy image.
    :returns: floppy image filename
    :raises: ImageCreationFailed, if it failed while creating the floppy image.
    :raises: IRMCOperationError, if copying floppy image file failed.
    """
    floppy_filename = _get_floppy_image_name(task.node)
    floppy_fullpathname = os.path.join(
        CONF.irmc.remote_image_share_root, floppy_filename)

    with tempfile.NamedTemporaryFile() as vfat_image_tmpfile_obj:
        images.create_vfat_image(vfat_image_tmpfile_obj.name,
                                 parameters=params)
        try:
            shutil.copyfile(vfat_image_tmpfile_obj.name,
                            floppy_fullpathname)
        except IOError as e:
            operation = _("Copying floppy image file")
            raise exception.IRMCOperationError(
                operation=operation, error=e)

    return floppy_filename


def attach_boot_iso_if_needed(task):
    """Attaches boot ISO for a deployed node if it exists.

    This method checks the instance info of the bare metal node for a
    boot ISO. If the instance info has a value of key 'irmc_boot_iso',
    it indicates that 'boot_option' is 'netboot'. Threfore it attaches
    the boot ISO on the bare metal node and then sets the node to boot from
    virtual media cdrom.

    :param task: a TaskManager instance containing the node to act on.
    :raises: IRMCOperationError if attaching virtual media failed.
    :raises: InvalidParameterValue if the validation of the
        ManagementInterface fails.
    """
    d_info = task.node.driver_internal_info
    node_state = task.node.provision_state

    if 'irmc_boot_iso' in d_info and node_state == states.ACTIVE:
        _setup_vmedia_for_boot(task, d_info['irmc_boot_iso'])
        manager_utils.node_set_boot_device(task, boot_devices.CDROM)


def _setup_vmedia_for_boot(task, bootable_iso_filename, parameters=None):
    """Sets up the node to boot from the boot ISO image.

    This method attaches a boot_iso on the node and passes
    the required parameters to it via a virtual floppy image.

    :param task: a TaskManager instance containing the node to act on.
    :param bootable_iso_filename: a bootable ISO image to attach to.
        The iso file should be present in NFS/CIFS server.
    :param parameters: the parameters to pass in a virtual floppy image
        in a dictionary.  This is optional.
    :raises: ImageCreationFailed, if it failed while creating a floppy image.
    :raises: IRMCOperationError, if attaching a virtual media failed.
    """
    LOG.info(_LI("Setting up node %s to boot from virtual media"),
             task.node.uuid)

    _detach_virtual_cd(task.node)
    _detach_virtual_fd(task.node)

    if parameters:
        floppy_image_filename = _prepare_floppy_image(task, parameters)
        _attach_virtual_fd(task.node, floppy_image_filename)

    _attach_virtual_cd(task.node, bootable_iso_filename)


def _cleanup_vmedia_boot(task):
    """Cleans a node after a virtual media boot.

    This method cleans up a node after a virtual media boot.
    It deletes floppy and cdrom images if they exist in NFS/CIFS server.
    It also ejects both the virtual media cdrom and the virtual media floppy.

    :param task: a TaskManager instance containing the node to act on.
    :raises: IRMCOperationError if ejecting virtual media failed.
    """
    LOG.debug("Cleaning up node %s after virtual media boot", task.node.uuid)

    node = task.node
    _detach_virtual_cd(node)
    _detach_virtual_fd(node)

    _remove_share_file(_get_floppy_image_name(node))
    _remove_share_file(_get_deploy_iso_name(node))


def _remove_share_file(share_filename):
    """Remove given file from the share file system.

    :param share_filename: a file name to be removed.
    """
    share_fullpathname = os.path.join(
        CONF.irmc.remote_image_share_name, share_filename)
    ironic_utils.unlink_without_raise(share_fullpathname)


def _attach_virtual_cd(node, bootable_iso_filename):
    """Attaches the given url as virtual media on the node.

    :param node: an ironic node object.
    :param bootable_iso_filename: a bootable ISO image to attach to.
        The iso file should be present in NFS/CIFS server.
    :raises: IRMCOperationError if attaching virtual media failed.
    """
    try:
        irmc_client = irmc_common.get_irmc_client(node)

        cd_set_params = scci.get_virtual_cd_set_params_cmd(
            CONF.irmc.remote_image_server,
            CONF.irmc.remote_image_user_domain,
            scci.get_share_type(CONF.irmc.remote_image_share_type),
            CONF.irmc.remote_image_share_name,
            bootable_iso_filename,
            CONF.irmc.remote_image_user_name,
            CONF.irmc.remote_image_user_password)

        irmc_client(cd_set_params, async=False)
        irmc_client(scci.MOUNT_CD, async=False)

    except scci.SCCIClientError as irmc_exception:
        LOG.exception(_LE("Error while inserting virtual cdrom "
                          "into node %(uuid)s. Error: %(error)s"),
                      {'uuid': node.uuid, 'error': irmc_exception})
        operation = _("Inserting virtual cdrom")
        raise exception.IRMCOperationError(operation=operation,
                                           error=irmc_exception)

    LOG.info(_LI("Attached virtual cdrom successfully"
                 " for node %s"), node.uuid)


def _detach_virtual_cd(node):
    """Detaches virtual cdrom on the node.

    :param node: an ironic node object.
    :raises: IRMCOperationError if eject virtual cdrom failed.
    """
    try:
        irmc_client = irmc_common.get_irmc_client(node)

        irmc_client(scci.UNMOUNT_CD)

    except scci.SCCIClientError as irmc_exception:
        LOG.exception(_LE("Error while ejecting virtual cdrom "
                          "from node %(uuid)s. Error: %(error)s"),
                      {'uuid': node.uuid, 'error': irmc_exception})
        operation = _("Ejecting virtual cdrom")
        raise exception.IRMCOperationError(operation=operation,
                                           error=irmc_exception)

    LOG.info(_LI("Detached virtual cdrom successfully"
                 " for node %s"), node.uuid)


def _attach_virtual_fd(node, floppy_image_filename):
    """Attaches virtual floppy on the node.

    :param node: an ironic node object.
    :raises: IRMCOperationError if insert virtual floppy failed.
    """
    try:
        irmc_client = irmc_common.get_irmc_client(node)

        fd_set_params = scci.get_virtual_fd_set_params_cmd(
            CONF.irmc.remote_image_server,
            CONF.irmc.remote_image_user_domain,
            scci.get_share_type(CONF.irmc.remote_image_share_type),
            CONF.irmc.remote_image_share_name,
            floppy_image_filename,
            CONF.irmc.remote_image_user_name,
            CONF.irmc.remote_image_user_password)

        irmc_client(fd_set_params, async=False)
        irmc_client(scci.MOUNT_FD, async=False)

    except scci.SCCIClientError as irmc_exception:
        LOG.exception(_LE("Error while inserting virtual floppy "
                          "into node %(uuid)s. Error: %(error)s"),
                      {'uuid': node.uuid, 'error': irmc_exception})
        operation = _("Inserting virtual floppy")
        raise exception.IRMCOperationError(operation=operation,
                                           error=irmc_exception)

    LOG.info(_LI("Attached virtual floppy successfully"
                 " for node %s"), node.uuid)


def _detach_virtual_fd(node):
    """Detaches virtual media floppy on the node.

    :param node: an ironic node object.
    :raises: IRMCOperationError if eject virtual media floppy failed.
    """
    try:
        irmc_client = irmc_common.get_irmc_client(node)

        irmc_client(scci.UNMOUNT_FD)

    except scci.SCCIClientError as irmc_exception:
        LOG.exception(_LE("Error while ejecting virtual floppy "
                          "from node %(uuid)s. Error: %(error)s"),
                      {'uuid': node.uuid, 'error': irmc_exception})
        operation = _("Ejecting virtual floppy")
        raise exception.IRMCOperationError(operation=operation,
                                           error=irmc_exception)

    LOG.info(_LI("Detached virtual floppy successfully"
                 " for node %s"), node.uuid)


def check_share_fs_mounted():
    """Check if Share File System (NFS or CIFS) is mounted.

    :raises: InvalidParameterValue, if config option has invalid value.
    :raises: IRMCSharedFileSystemNotMounted, if shared file system is
        not mounted.
    """
    _parse_config_option()
    if not os.path.ismount(CONF.irmc.remote_image_share_root):
        raise exception.IRMCSharedFileSystemNotMounted(
            share=CONF.irmc.remote_image_share_root)


class IRMCVirtualMediaBoot(base.BootInterface):
    """iRMC Virtual Media boot-related actions."""

    def __init__(self):
        """Constructor of IRMCVirtualMediaBoot.

        :raises: IRMCSharedFileSystemNotMounted, if shared file system is
            not mounted.
        :raises: InvalidParameterValue, if config option has invalid value.
        """
        check_share_fs_mounted()
        super(IRMCVirtualMediaBoot, self).__init__()

    def get_properties(self):
        return COMMON_PROPERTIES

    def validate(self, task):
        """Validate the deployment information for the task's node.

        :param task: a TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue, if config option has invalid value.
        :raises: IRMCSharedFileSystemNotMounted, if shared file system is
            not mounted.
        :raises: InvalidParameterValue, if some information is invalid.
        :raises: MissingParameterValue if 'kernel_id' and 'ramdisk_id' are
            missing in the Glance image, or if 'kernel' and 'ramdisk' are
            missing in the Non Glance image.
        """
        check_share_fs_mounted()

        d_info = _parse_deploy_info(task.node)
        if task.node.driver_internal_info.get('is_whole_disk_image'):
            props = []
        elif service_utils.is_glance_image(d_info['image_source']):
            props = ['kernel_id', 'ramdisk_id']
        else:
            props = ['kernel', 'ramdisk']
        deploy_utils.validate_image_properties(task.context, d_info,
                                               props)

    def prepare_ramdisk(self, task, ramdisk_params):
        """Prepares the deploy ramdisk using virtual media.

        Prepares the options for the deployment ramdisk, sets the node to boot
        from virtual media cdrom.

        :param task: a TaskManager instance containing the node to act on.
        :param ramdisk_params: the options to be passed to the deploy ramdisk.
        :raises: ImageRefValidationFailed if no image service can handle
                 specified href.
        :raises: ImageCreationFailed, if it failed while creating the floppy
                 image.
        :raises: InvalidParameterValue if the validation of the
                 PowerInterface or ManagementInterface fails.
        :raises: IRMCOperationError, if some operation on iRMC fails.
        """

        # NOTE(TheJulia): If this method is being called by something
        # aside from deployment and clean, such as conductor takeover, we
        # should treat this as a no-op and move on otherwise we would modify
        # the state of the node due to virtual media operations.
        if (task.node.provision_state != states.DEPLOYING and
                task.node.provision_state != states.CLEANING):
            return

        deploy_nic_mac = deploy_utils.get_single_nic_with_vif_port_id(task)
        ramdisk_params['BOOTIF'] = deploy_nic_mac

        _setup_deploy_iso(task, ramdisk_params)

    def clean_up_ramdisk(self, task):
        """Cleans up the boot of ironic ramdisk.

        This method cleans up the environment that was setup for booting the
        deploy ramdisk.

        :param task: a task from TaskManager.
        :returns: None
        :raises: IRMCOperationError if iRMC operation failed.
        """
        _cleanup_vmedia_boot(task)

    def prepare_instance(self, task):
        """Prepares the boot of instance.

        This method prepares the boot of the instance after reading
        relevant information from the node's database.

        :param task: a task from TaskManager.
        :returns: None
        """
        _cleanup_vmedia_boot(task)

        node = task.node
        iwdi = node.driver_internal_info.get('is_whole_disk_image')
        if deploy_utils.get_boot_option(node) == "local" or iwdi:
            manager_utils.node_set_boot_device(task, boot_devices.DISK,
                                               persistent=True)
        else:
            driver_internal_info = node.driver_internal_info
            root_uuid_or_disk_id = driver_internal_info['root_uuid_or_disk_id']
            self._configure_vmedia_boot(task, root_uuid_or_disk_id)

    def clean_up_instance(self, task):
        """Cleans up the boot of instance.

        This method cleans up the environment that was setup for booting
        the instance.

        :param task: a task from TaskManager.
        :returns: None
        :raises: IRMCOperationError if iRMC operation failed.
        """
        _remove_share_file(_get_boot_iso_name(task.node))
        driver_internal_info = task.node.driver_internal_info
        driver_internal_info.pop('irmc_boot_iso', None)
        driver_internal_info.pop('root_uuid_or_disk_id', None)
        task.node.driver_internal_info = driver_internal_info
        task.node.save()
        _cleanup_vmedia_boot(task)

    def _configure_vmedia_boot(self, task, root_uuid_or_disk_id):
        """Configure vmedia boot for the node."""
        node = task.node
        _prepare_boot_iso(task, root_uuid_or_disk_id)
        _setup_vmedia_for_boot(
            task, node.driver_internal_info['irmc_boot_iso'])
        manager_utils.node_set_boot_device(task, boot_devices.CDROM,
                                           persistent=True)
