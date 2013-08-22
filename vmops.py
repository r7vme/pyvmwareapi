# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2012 VMware, Inc.
# Copyright (c) 2011 Citrix Systems, Inc.
# Copyright 2011 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Class for VM tasks like spawn, snapshot, suspend, resume etc.
"""

import base64
import os
import time
import urllib
import urllib2
import uuid

from oslo.config import cfg

from nova import block_device
from nova.compute import api as compute
from nova.compute import power_state
from nova.compute import task_states
from nova import context as nova_context
from nova import exception
from nova.openstack.common import excutils
from nova.openstack.common import log as logging
from nova.virt import driver
from nova.virt.vmwareapi import network_util
from nova.virt.vmwareapi import vif as vmwarevif
from nova.virt.vmwareapi import vim_util
from nova.virt.vmwareapi import vm_util
from nova.virt.vmwareapi import vmware_images


vmware_vif_opts = [
    cfg.StrOpt('integration_bridge',
               default='br-int',
               help='Name of Integration Bridge'),
    ]

vmware_group = cfg.OptGroup(name='vmware',
                            title='VMware Options')

CONF = cfg.CONF
CONF.register_group(vmware_group)
CONF.register_opts(vmware_vif_opts, vmware_group)
CONF.import_opt('base_dir_name', 'nova.virt.libvirt.imagecache')
CONF.import_opt('vnc_enabled', 'nova.vnc')

LOG = logging.getLogger(__name__)

VMWARE_POWER_STATES = {
                   'poweredOff': power_state.SHUTDOWN,
                    'poweredOn': power_state.RUNNING,
                    'suspended': power_state.SUSPENDED}
VMWARE_PREFIX = 'vmware'


RESIZE_TOTAL_STEPS = 4


class VMwareVMOps(object):
    """Management class for VM-related tasks."""

    def __init__(self, session, virtapi, volumeops, cluster_name=None):
        """Initializer."""
        self.compute_api = compute.API()
        self._session = session
        self._virtapi = virtapi
        self._volumeops = volumeops
        if not cluster_name:
            self._cluster = None
        else:
            self._cluster = vm_util.get_cluster_ref_from_name(
                                        self._session, cluster_name)
        self._instance_path_base = VMWARE_PREFIX + CONF.base_dir_name
        self._default_root_device = 'vda'
        self._rescue_suffix = '-rescue'
        self._poll_rescue_last_ran = None

    def list_instances(self):
        """Lists the VM instances that are registered with the ESX host."""
        LOG.debug(_("Getting list of instances"))
        vms = self._session._call_method(vim_util, "get_objects",
                     "VirtualMachine",
                     ["name", "runtime.connectionState"])
        lst_vm_names = []
        for vm in vms:
            vm_name = None
            conn_state = None
            for prop in vm.propSet:
                if prop.name == "name":
                    vm_name = prop.val
                elif prop.name == "runtime.connectionState":
                    conn_state = prop.val
            # Ignoring the orphaned or inaccessible VMs
            if conn_state not in ["orphaned", "inaccessible"]:
                lst_vm_names.append(vm_name)
        LOG.debug(_("Got total of %s instances") % str(len(lst_vm_names)))
        return lst_vm_names

    def spawn(self, context, instance, image_meta, network_info,
              block_device_info=None):
        """
        Creates a VM instance.

        Steps followed are:

        1. Create a VM with no disk and the specifics in the instance object
           like RAM size.
        2. For flat disk
          2.1. Create a dummy vmdk of the size of the disk file that is to be
               uploaded. This is required just to create the metadata file.
          2.2. Delete the -flat.vmdk file created in the above step and retain
               the metadata .vmdk file.
          2.3. Upload the disk file.
        3. For sparse disk
          3.1. Upload the disk file to a -sparse.vmdk file.
          3.2. Copy/Clone the -sparse.vmdk file to a thin vmdk.
          3.3. Delete the -sparse.vmdk file.
        4. Attach the disk to the VM by reconfiguring the same.
        5. Power on the VM.
        """
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref:
            raise exception.InstanceExists(name=instance['name'])

        client_factory = self._session._get_vim().client.factory
        service_content = self._session._get_vim().get_service_content()
        ds = vm_util.get_datastore_ref_and_name(self._session, self._cluster)
        data_store_ref = ds[0]
        data_store_name = ds[1]

        def _get_image_properties():
            """
            Get the Size of the flat vmdk file that is there on the storage
            repository.
            """
            _image_info = vmware_images.get_vmdk_size_and_properties(context,
                                                        instance['image_ref'],
                                                        instance)
            image_size, image_properties = _image_info
            vmdk_file_size_in_kb = int(image_size) / 1024
            os_type = image_properties.get("vmware_ostype", "otherGuest")
            adapter_type = image_properties.get("vmware_adaptertype",
                                                "lsiLogic")
            disk_type = image_properties.get("vmware_disktype",
                                             "preallocated")
            return vmdk_file_size_in_kb, os_type, adapter_type, disk_type

        (vmdk_file_size_in_kb, os_type, adapter_type,
         disk_type) = _get_image_properties()

        vm_folder_ref = self._get_vmfolder_ref()
        res_pool_ref = self._get_res_pool_ref()

        def _check_if_network_bridge_exists(network_name):
            network_ref = network_util.get_network_with_the_name(
                          self._session, network_name, self._cluster)
            if network_ref is None:
                raise exception.NetworkNotFoundForBridge(bridge=network_name)
            return network_ref

        def _get_vif_infos():
            vif_infos = []
            if network_info is None:
                return vif_infos
            for vif in network_info:
                mac_address = vif['address']
                network_name = vif['network']['bridge'] or \
                               CONF.vmware.integration_bridge
                if vif['network'].get_meta('should_create_vlan', False):
                    network_ref = vmwarevif.ensure_vlan_bridge(
                                                        self._session, vif,
                                                        self._cluster)
                else:
                    network_ref = _check_if_network_bridge_exists(network_name)
                vif_infos.append({'network_name': network_name,
                                  'mac_address': mac_address,
                                  'network_ref': network_ref,
                                  'iface_id': vif.get_meta('iface_id'),
                                 })
            return vif_infos

        vif_infos = _get_vif_infos()

        # Get the create vm config spec
        config_spec = vm_util.get_vm_create_spec(
                            client_factory, instance,
                            data_store_name, vif_infos, os_type)

        def _execute_create_vm():
            """Create VM on ESX host."""
            LOG.debug(_("Creating VM on the ESX host"), instance=instance)
            # Create the VM on the ESX host
            vm_create_task = self._session._call_method(
                                    self._session._get_vim(),
                                    "CreateVM_Task", vm_folder_ref,
                                    config=config_spec, pool=res_pool_ref)
            self._session._wait_for_task(instance['uuid'], vm_create_task)

            LOG.debug(_("Created VM on the ESX host"), instance=instance)

        _execute_create_vm()
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])

        # Set the machine.id parameter of the instance to inject
        # the NIC configuration inside the VM
        if CONF.flat_injected:
            self._set_machine_id(client_factory, instance, network_info)

        # Set the vnc configuration of the instance, vnc port starts from 5900
        if CONF.vnc_enabled:
            vnc_port = self._get_vnc_port(vm_ref)
            vnc_pass = CONF.vnc_password or ''
            self._set_vnc_config(client_factory, instance, vnc_port, vnc_pass)

        def _create_virtual_disk():
            """Create a virtual disk of the size of flat vmdk file."""
            # Create a Virtual Disk of the size of the flat vmdk file. This is
            # done just to generate the meta-data file whose specifics
            # depend on the size of the disk, thin/thick provisioning and the
            # storage adapter type.
            # Here we assume thick provisioning and lsiLogic for the adapter
            # type
            LOG.debug(_("Creating Virtual Disk of size  "
                      "%(vmdk_file_size_in_kb)s KB and adapter type "
                      "%(adapter_type)s on the ESX host local store "
                      "%(data_store_name)s") %
                       {"vmdk_file_size_in_kb": vmdk_file_size_in_kb,
                        "adapter_type": adapter_type,
                        "data_store_name": data_store_name},
                      instance=instance)
            vmdk_create_spec = vm_util.get_vmdk_create_spec(client_factory,
                                    vmdk_file_size_in_kb, adapter_type,
                                    disk_type)
            vmdk_create_task = self._session._call_method(
                self._session._get_vim(),
                "CreateVirtualDisk_Task",
                service_content.virtualDiskManager,
                name=uploaded_vmdk_path,
                datacenter=dc_ref,
                spec=vmdk_create_spec)
            self._session._wait_for_task(instance['uuid'], vmdk_create_task)
            LOG.debug(_("Created Virtual Disk of size %(vmdk_file_size_in_kb)s"
                        " KB and type %(disk_type)s on "
                        "the ESX host local store %(data_store_name)s") %
                        {"vmdk_file_size_in_kb": vmdk_file_size_in_kb,
                         "disk_type": disk_type,
                         "data_store_name": data_store_name},
                      instance=instance)

        def _delete_disk_file(vmdk_path):
            LOG.debug(_("Deleting the file %(vmdk_path)s "
                        "on the ESX host local"
                        "store %(data_store_name)s") %
                        {"vmdk_path": vmdk_path,
                         "data_store_name": data_store_name},
                      instance=instance)
            # Delete the vmdk file.
            vmdk_delete_task = self._session._call_method(
                        self._session._get_vim(),
                        "DeleteDatastoreFile_Task",
                        service_content.fileManager,
                        name=vmdk_path,
                        datacenter=dc_ref)
            self._session._wait_for_task(instance['uuid'], vmdk_delete_task)
            LOG.debug(_("Deleted the file %(vmdk_path)s on the "
                        "ESX host local store %(data_store_name)s") %
                        {"vmdk_path": vmdk_path,
                         "data_store_name": data_store_name},
                      instance=instance)

        def _fetch_image_on_esx_datastore():
            """Fetch image from Glance to ESX datastore."""
            LOG.debug(_("Downloading image file data %(image_ref)s to the ESX "
                        "data store %(data_store_name)s") %
                        {'image_ref': instance['image_ref'],
                         'data_store_name': data_store_name},
                      instance=instance)
            # For flat disk, upload the -flat.vmdk file whose meta-data file
            # we just created above
            # For sparse disk, upload the -sparse.vmdk file to be copied into
            # a flat vmdk
            upload_vmdk_name = sparse_uploaded_vmdk_name \
                if disk_type == "sparse" else flat_uploaded_vmdk_name
            vmware_images.fetch_image(
                context,
                instance['image_ref'],
                instance,
                host=self._session._host_ip,
                data_center_name=self._get_datacenter_ref_and_name()[1],
                datastore_name=data_store_name,
                cookies=cookies,
                file_path=upload_vmdk_name)
            LOG.debug(_("Downloaded image file data %(image_ref)s to "
                        "%(upload_vmdk_name)s on the ESX data store "
                        "%(data_store_name)s") %
                        {'image_ref': instance['image_ref'],
                         'upload_vmdk_name': upload_vmdk_name,
                         'data_store_name': data_store_name},
                      instance=instance)

        def _copy_virtual_disk():
            """Copy a sparse virtual disk to a thin virtual disk."""
            # Copy a sparse virtual disk to a thin virtual disk. This is also
            # done to generate the meta-data file whose specifics
            # depend on the size of the disk, thin/thick provisioning and the
            # storage adapter type.
            LOG.debug(_("Copying Virtual Disk of size "
                      "%(vmdk_file_size_in_kb)s KB and adapter type "
                      "%(adapter_type)s on the ESX host local store "
                      "%(data_store_name)s to disk type %(disk_type)s") %
                       {"vmdk_file_size_in_kb": vmdk_file_size_in_kb,
                        "adapter_type": adapter_type,
                        "data_store_name": data_store_name,
                        "disk_type": disk_type},
                      instance=instance)
            vmdk_copy_spec = vm_util.get_vmdk_create_spec(client_factory,
                                    vmdk_file_size_in_kb, adapter_type,
                                    disk_type)
            vmdk_copy_task = self._session._call_method(
                self._session._get_vim(),
                "CopyVirtualDisk_Task",
                service_content.virtualDiskManager,
                sourceName=sparse_uploaded_vmdk_path,
                sourceDatacenter=self._get_datacenter_ref_and_name()[0],
                destName=uploaded_vmdk_path,
                destSpec=vmdk_copy_spec)
            self._session._wait_for_task(instance['uuid'], vmdk_copy_task)
            LOG.debug(_("Copied Virtual Disk of size %(vmdk_file_size_in_kb)s"
                        " KB and type %(disk_type)s on "
                        "the ESX host local store %(data_store_name)s") %
                        {"vmdk_file_size_in_kb": vmdk_file_size_in_kb,
                         "disk_type": disk_type,
                         "data_store_name": data_store_name},
                        instance=instance)

        ebs_root = block_device.volume_in_mapping(
                self._default_root_device, block_device_info)

        if not ebs_root:
            linked_clone = CONF.use_linked_clone
            if linked_clone:
                upload_folder = self._instance_path_base
                upload_name = instance['image_ref']
            else:
                upload_folder = instance['name']
                upload_name = instance['name']

            # The vmdk meta-data file
            uploaded_vmdk_name = "%s/%s.vmdk" % (upload_folder, upload_name)
            uploaded_vmdk_path = vm_util.build_datastore_path(data_store_name,
                                                uploaded_vmdk_name)

            if not (linked_clone and self._check_if_folder_file_exists(
                                        data_store_ref, data_store_name,
                                        upload_folder, upload_name + ".vmdk")):

                # Naming the VM files in correspondence with the VM instance
                # The flat vmdk file name
                flat_uploaded_vmdk_name = "%s/%s-flat.vmdk" % (
                                            upload_folder, upload_name)
                # The sparse vmdk file name for sparse disk image
                sparse_uploaded_vmdk_name = "%s/%s-sparse.vmdk" % (
                                            upload_folder, upload_name)

                flat_uploaded_vmdk_path = vm_util.build_datastore_path(
                                                    data_store_name,
                                                    flat_uploaded_vmdk_name)
                sparse_uploaded_vmdk_path = vm_util.build_datastore_path(
                                                    data_store_name,
                                                    sparse_uploaded_vmdk_name)
                dc_ref = self._get_datacenter_ref_and_name()[0]

                if disk_type != "sparse":
                   # Create a flat virtual disk and retain the metadata file.
                    _create_virtual_disk()
                    _delete_disk_file(flat_uploaded_vmdk_path)

                cookies = \
                    self._session._get_vim().client.options.transport.cookiejar
                _fetch_image_on_esx_datastore()

                if disk_type == "sparse":
                    # Copy the sparse virtual disk to a thin virtual disk.
                    disk_type = "thin"
                    _copy_virtual_disk()
                    _delete_disk_file(sparse_uploaded_vmdk_path)
            else:
                # linked clone base disk exists
                if disk_type == "sparse":
                    disk_type = "thin"

            # Attach the vmdk uploaded to the VM.
            self._volumeops.attach_disk_to_vm(
                                vm_ref, instance,
                                adapter_type, disk_type, uploaded_vmdk_path,
                                vmdk_file_size_in_kb, linked_clone)
        else:
            # Attach the root disk to the VM.
            root_disk = driver.block_device_info_get_mapping(
                           block_device_info)[0]
            connection_info = root_disk['connection_info']
            self._volumeops.attach_volume(connection_info, instance['name'],
                                          self._default_root_device)

        def _power_on_vm():
            """Power on the VM."""
            LOG.debug(_("Powering on the VM instance"), instance=instance)
            # Power On the VM
            power_on_task = self._session._call_method(
                               self._session._get_vim(),
                               "PowerOnVM_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], power_on_task)
            LOG.debug(_("Powered on the VM instance"), instance=instance)
        _power_on_vm()

    def snapshot(self, context, instance, snapshot_name, update_task_state):
        """Create snapshot from a running VM instance.

        Steps followed are:

        1. Get the name of the vmdk file which the VM points to right now.
           Can be a chain of snapshots, so we need to know the last in the
           chain.
        2. Create the snapshot. A new vmdk is created which the VM points to
           now. The earlier vmdk becomes read-only.
        3. Call CopyVirtualDisk which coalesces the disk chain to form a single
           vmdk, rather a .vmdk metadata file and a -flat.vmdk disk data file.
        4. Now upload the -flat.vmdk file to the image store.
        5. Delete the coalesced .vmdk and -flat.vmdk created.
        """
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        client_factory = self._session._get_vim().client.factory
        service_content = self._session._get_vim().get_service_content()

        def _get_vm_and_vmdk_attribs():
            # Get the vmdk file name that the VM is pointing to
            hardware_devices = self._session._call_method(vim_util,
                        "get_dynamic_property", vm_ref,
                        "VirtualMachine", "config.hardware.device")
            (vmdk_file_path_before_snapshot, controller_key, adapter_type,
             disk_type, unit_number) = vm_util.get_vmdk_path_and_adapter_type(
                                        hardware_devices)
            datastore_name = vm_util.split_datastore_path(
                                        vmdk_file_path_before_snapshot)[0]
            os_type = self._session._call_method(vim_util,
                        "get_dynamic_property", vm_ref,
                        "VirtualMachine", "summary.config.guestId")
            return (vmdk_file_path_before_snapshot, adapter_type, disk_type,
                    datastore_name, os_type)

        (vmdk_file_path_before_snapshot, adapter_type, disk_type,
         datastore_name, os_type) = _get_vm_and_vmdk_attribs()

        def _create_vm_snapshot():
            # Create a snapshot of the VM
            LOG.debug(_("Creating Snapshot of the VM instance"),
                      instance=instance)
            snapshot_task = self._session._call_method(
                        self._session._get_vim(),
                        "CreateSnapshot_Task", vm_ref,
                        name="%s-snapshot" % instance['name'],
                        description="Taking Snapshot of the VM",
                        memory=False,
                        quiesce=True)
            self._session._wait_for_task(instance['uuid'], snapshot_task)
            LOG.debug(_("Created Snapshot of the VM instance"),
                      instance=instance)

        _create_vm_snapshot()
        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)

        def _check_if_tmp_folder_exists():
            # Copy the contents of the VM that were there just before the
            # snapshot was taken
            ds_ref_ret = vim_util.get_dynamic_property(
                                    self._session._get_vim(),
                                    vm_ref,
                                    "VirtualMachine",
                                    "datastore")
            if ds_ref_ret is None:
                raise exception.DatastoreNotFound()
            ds_ref = ds_ref_ret.ManagedObjectReference[0]
            ds_browser = vim_util.get_dynamic_property(
                                       self._session._get_vim(),
                                       ds_ref,
                                       "Datastore",
                                       "browser")
            # Check if the vmware-tmp folder exists or not. If not, create one
            tmp_folder_path = vm_util.build_datastore_path(datastore_name,
                                                           "vmware-tmp")
            if not self._path_exists(ds_browser, tmp_folder_path):
                self._mkdir(vm_util.build_datastore_path(datastore_name,
                                                         "vmware-tmp"))

        _check_if_tmp_folder_exists()

        # Generate a random vmdk file name to which the coalesced vmdk content
        # will be copied to. A random name is chosen so that we don't have
        # name clashes.
        random_name = str(uuid.uuid4())
        dest_vmdk_file_location = vm_util.build_datastore_path(datastore_name,
                   "vmware-tmp/%s.vmdk" % random_name)
        dc_ref = self._get_datacenter_ref_and_name()[0]

        def _copy_vmdk_content():
            # Copy the contents of the disk ( or disks, if there were snapshots
            # done earlier) to a temporary vmdk file.
            copy_spec = vm_util.get_copy_virtual_disk_spec(client_factory,
                                                           adapter_type,
                                                           disk_type)
            LOG.debug(_('Copying disk data before snapshot of the VM'),
                      instance=instance)
            copy_disk_task = self._session._call_method(
                self._session._get_vim(),
                "CopyVirtualDisk_Task",
                service_content.virtualDiskManager,
                sourceName=vmdk_file_path_before_snapshot,
                sourceDatacenter=dc_ref,
                destName=dest_vmdk_file_location,
                destDatacenter=dc_ref,
                destSpec=copy_spec,
                force=False)
            self._session._wait_for_task(instance['uuid'], copy_disk_task)
            LOG.debug(_("Copied disk data before snapshot of the VM"),
                      instance=instance)

        _copy_vmdk_content()

        cookies = self._session._get_vim().client.options.transport.cookiejar

        def _upload_vmdk_to_image_repository():
            # Upload the contents of -flat.vmdk file which has the disk data.
            LOG.debug(_("Uploading image %s") % snapshot_name,
                      instance=instance)
            vmware_images.upload_image(
                context,
                snapshot_name,
                instance,
                os_type=os_type,
                adapter_type=adapter_type,
                image_version=1,
                host=self._session._host_ip,
                data_center_name=self._get_datacenter_ref_and_name()[1],
                datastore_name=datastore_name,
                cookies=cookies,
                file_path="vmware-tmp/%s-flat.vmdk" % random_name)
            LOG.debug(_("Uploaded image %s") % snapshot_name,
                      instance=instance)

        update_task_state(task_state=task_states.IMAGE_UPLOADING,
                          expected_state=task_states.IMAGE_PENDING_UPLOAD)
        _upload_vmdk_to_image_repository()

        def _clean_temp_data():
            """
            Delete temporary vmdk files generated in image handling
            operations.
            """
            # Delete the temporary vmdk created above.
            LOG.debug(_("Deleting temporary vmdk file %s")
                        % dest_vmdk_file_location, instance=instance)
            remove_disk_task = self._session._call_method(
                self._session._get_vim(),
                "DeleteVirtualDisk_Task",
                service_content.virtualDiskManager,
                name=dest_vmdk_file_location,
                datacenter=dc_ref)
            self._session._wait_for_task(instance['uuid'], remove_disk_task)
            LOG.debug(_("Deleted temporary vmdk file %s")
                        % dest_vmdk_file_location, instance=instance)

        _clean_temp_data()

    def reboot(self, instance, network_info):
        """Reboot a VM instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.plug_vifs(instance, network_info)

        lst_properties = ["summary.guest.toolsStatus", "runtime.powerState",
                          "summary.guest.toolsRunningStatus"]
        props = self._session._call_method(vim_util, "get_object_properties",
                           None, vm_ref, "VirtualMachine",
                           lst_properties)
        pwr_state = None
        tools_status = None
        tools_running_status = False
        for elem in props:
            for prop in elem.propSet:
                if prop.name == "runtime.powerState":
                    pwr_state = prop.val
                elif prop.name == "summary.guest.toolsStatus":
                    tools_status = prop.val
                elif prop.name == "summary.guest.toolsRunningStatus":
                    tools_running_status = prop.val

        # Raise an exception if the VM is not powered On.
        if pwr_state not in ["poweredOn"]:
            reason = _("instance is not powered on")
            raise exception.InstanceRebootFailure(reason=reason)

        # If latest vmware tools are installed in the VM, and that the tools
        # are running, then only do a guest reboot. Otherwise do a hard reset.
        if (tools_status == "toolsOk" and
                tools_running_status == "guestToolsRunning"):
            LOG.debug(_("Rebooting guest OS of VM"), instance=instance)
            self._session._call_method(self._session._get_vim(), "RebootGuest",
                                       vm_ref)
            LOG.debug(_("Rebooted guest OS of VM"), instance=instance)
        else:
            LOG.debug(_("Doing hard reboot of VM"), instance=instance)
            reset_task = self._session._call_method(self._session._get_vim(),
                                                    "ResetVM_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], reset_task)
            LOG.debug(_("Did hard reboot of VM"), instance=instance)

    def _delete(self, instance, network_info):
        """
        Destroy a VM instance. Steps followed are:
        1. Power off the VM, if it is in poweredOn state.
        2. Destroy the VM.
        """
        try:
            vm_ref = vm_util.get_vm_ref_from_name(self._session,
                                                  instance['name'])
            if vm_ref is None:
                LOG.debug(_("instance not present"), instance=instance)
                return

            self.power_off(instance)

            try:
                LOG.debug(_("Destroying the VM"), instance=instance)
                destroy_task = self._session._call_method(
                    self._session._get_vim(),
                    "Destroy_Task", vm_ref)
                self._session._wait_for_task(instance['uuid'], destroy_task)
                LOG.debug(_("Destroyed the VM"), instance=instance)
            except Exception, excep:
                LOG.warn(_("In vmwareapi:vmops:delete, got this exception"
                           " while destroying the VM: %s") % str(excep))

            if network_info:
                self.unplug_vifs(instance, network_info)
        except Exception, exc:
            LOG.exception(exc, instance=instance)

    def destroy(self, instance, network_info, destroy_disks=True):
        """
        Destroy a VM instance. Steps followed are:
        1. Power off the VM, if it is in poweredOn state.
        2. Un-register a VM.
        3. Delete the contents of the folder holding the VM related data.
        """
        try:
            vm_ref = vm_util.get_vm_ref_from_name(self._session,
                                                  instance['name'])
            if vm_ref is None:
                LOG.debug(_("instance not present"), instance=instance)
                return
            lst_properties = ["config.files.vmPathName", "runtime.powerState"]
            props = self._session._call_method(vim_util,
                        "get_object_properties",
                        None, vm_ref, "VirtualMachine", lst_properties)
            pwr_state = None
            for elem in props:
                vm_config_pathname = None
                for prop in elem.propSet:
                    if prop.name == "runtime.powerState":
                        pwr_state = prop.val
                    elif prop.name == "config.files.vmPathName":
                        vm_config_pathname = prop.val
            if vm_config_pathname:
                _ds_path = vm_util.split_datastore_path(vm_config_pathname)
                datastore_name, vmx_file_path = _ds_path
            # Power off the VM if it is in PoweredOn state.
            if pwr_state == "poweredOn":
                LOG.debug(_("Powering off the VM"), instance=instance)
                poweroff_task = self._session._call_method(
                       self._session._get_vim(),
                       "PowerOffVM_Task", vm_ref)
                self._session._wait_for_task(instance['uuid'], poweroff_task)
                LOG.debug(_("Powered off the VM"), instance=instance)

            # Un-register the VM
            try:
                LOG.debug(_("Unregistering the VM"), instance=instance)
                self._session._call_method(self._session._get_vim(),
                                           "UnregisterVM", vm_ref)
                LOG.debug(_("Unregistered the VM"), instance=instance)
            except Exception, excep:
                LOG.warn(_("In vmwareapi:vmops:destroy, got this exception"
                           " while un-registering the VM: %s") % str(excep))

            if network_info:
                self.unplug_vifs(instance, network_info)

            # Delete the folder holding the VM related content on
            # the datastore.
            if destroy_disks:
                try:
                    dir_ds_compliant_path = vm_util.build_datastore_path(
                                     datastore_name,
                                     os.path.dirname(vmx_file_path))
                    LOG.debug(_("Deleting contents of the VM from "
                                "datastore %(datastore_name)s") %
                               {'datastore_name': datastore_name},
                              instance=instance)
                    vim = self._session._get_vim()
                    delete_task = self._session._call_method(
                        vim,
                        "DeleteDatastoreFile_Task",
                        vim.get_service_content().fileManager,
                        name=dir_ds_compliant_path,
                        datacenter=self._get_datacenter_ref_and_name()[0])
                    self._session._wait_for_task(instance['uuid'], delete_task)
                    LOG.debug(_("Deleted contents of the VM from "
                                "datastore %(datastore_name)s") %
                               {'datastore_name': datastore_name},
                              instance=instance)
                except Exception, excep:
                    LOG.warn(_("In vmwareapi:vmops:destroy, "
                                 "got this exception while deleting"
                                 " the VM contents from the disk: %s")
                                 % str(excep))
        except Exception, exc:
            LOG.exception(exc, instance=instance)

    def pause(self, instance):
        msg = _("pause not supported for vmwareapi")
        raise NotImplementedError(msg)

    def unpause(self, instance):
        msg = _("unpause not supported for vmwareapi")
        raise NotImplementedError(msg)

    def suspend(self, instance):
        """Suspend the specified instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        pwr_state = self._session._call_method(vim_util,
                    "get_dynamic_property", vm_ref,
                    "VirtualMachine", "runtime.powerState")
        # Only PoweredOn VMs can be suspended.
        if pwr_state == "poweredOn":
            LOG.debug(_("Suspending the VM"), instance=instance)
            suspend_task = self._session._call_method(self._session._get_vim(),
                    "SuspendVM_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], suspend_task)
            LOG.debug(_("Suspended the VM"), instance=instance)
        # Raise Exception if VM is poweredOff
        elif pwr_state == "poweredOff":
            reason = _("instance is powered off and cannot be suspended.")
            raise exception.InstanceSuspendFailure(reason=reason)
        else:
            LOG.debug(_("VM was already in suspended state. So returning "
                      "without doing anything"), instance=instance)

    def resume(self, instance):
        """Resume the specified instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        pwr_state = self._session._call_method(vim_util,
                                     "get_dynamic_property", vm_ref,
                                     "VirtualMachine", "runtime.powerState")
        if pwr_state.lower() == "suspended":
            LOG.debug(_("Resuming the VM"), instance=instance)
            suspend_task = self._session._call_method(
                                        self._session._get_vim(),
                                       "PowerOnVM_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], suspend_task)
            LOG.debug(_("Resumed the VM"), instance=instance)
        else:
            reason = _("instance is not in a suspended state")
            raise exception.InstanceResumeFailure(reason=reason)

    def rescue(self, context, instance, network_info, image_meta):
        """Rescue the specified instance.

            - shutdown the instance VM.
            - spawn a rescue VM (the vm name-label will be instance-N-rescue).

        """
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.power_off(instance)
        instance['name'] = instance['name'] + self._rescue_suffix
        self.spawn(context, instance, image_meta, network_info)

        # Attach vmdk to the rescue VM
        hardware_devices = self._session._call_method(vim_util,
                        "get_dynamic_property", vm_ref,
                        "VirtualMachine", "config.hardware.device")
        vmdk_path, controller_key, adapter_type, disk_type, unit_number \
            = vm_util.get_vmdk_path_and_adapter_type(hardware_devices)
        # Figure out the correct unit number
        unit_number = unit_number + 1
        rescue_vm_ref = vm_util.get_vm_ref_from_name(self._session,
                                                     instance['name'])
        self._volumeops.attach_disk_to_vm(
                                rescue_vm_ref, instance,
                                adapter_type, disk_type, vmdk_path,
                                controller_key=controller_key,
                                unit_number=unit_number)

    def unrescue(self, instance):
        """Unrescue the specified instance."""
        instance_orig_name = instance['name']
        instance['name'] = instance['name'] + self._rescue_suffix
        self.destroy(instance, None)
        instance['name'] = instance_orig_name
        self.power_on(instance)

    def power_off(self, instance):
        """Power off the specified instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        pwr_state = self._session._call_method(vim_util,
                    "get_dynamic_property", vm_ref,
                    "VirtualMachine", "runtime.powerState")
        # Only PoweredOn VMs can be powered off.
        if pwr_state == "poweredOn":
            LOG.debug(_("Powering off the VM"), instance=instance)
            poweroff_task = self._session._call_method(
                                        self._session._get_vim(),
                                        "PowerOffVM_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], poweroff_task)
            LOG.debug(_("Powered off the VM"), instance=instance)
        # Raise Exception if VM is suspended
        elif pwr_state == "suspended":
            reason = _("instance is suspended and cannot be powered off.")
            raise exception.InstancePowerOffFailure(reason=reason)
        else:
            LOG.debug(_("VM was already in powered off state. So returning "
                        "without doing anything"), instance=instance)

    def power_on(self, instance):
        """Power on the specified instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        pwr_state = self._session._call_method(vim_util,
                                     "get_dynamic_property", vm_ref,
                                     "VirtualMachine", "runtime.powerState")
        if pwr_state == "poweredOn":
            LOG.debug(_("VM was already in powered on state. So returning "
                      "without doing anything"), instance=instance)
        # Only PoweredOff and Suspended VMs can be powered on.
        else:
            LOG.debug(_("Powering on the VM"), instance=instance)
            poweron_task = self._session._call_method(
                                        self._session._get_vim(),
                                        "PowerOnVM_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], poweron_task)
            LOG.debug(_("Powered on the VM"), instance=instance)

    def _get_orig_vm_name_label(self, instance):
        return instance['name'] + '-orig'

    def _update_instance_progress(self, context, instance, step, total_steps):
        """Update instance progress percent to reflect current step number
        """
        # Divide the action's workflow into discrete steps and "bump" the
        # instance's progress field as each step is completed.
        #
        # For a first cut this should be fine, however, for large VM images,
        # the clone disk step begins to dominate the equation. A
        # better approximation would use the percentage of the VM image that
        # has been streamed to the destination host.
        progress = round(float(step) / total_steps * 100)
        instance_uuid = instance['uuid']
        LOG.debug(_("Updating instance '%(instance_uuid)s' progress to"
                    " %(progress)d") % locals(), instance=instance)
        self._virtapi.instance_update(context, instance_uuid,
                                      {'progress': progress})

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   instance_type):
        """
        Transfers the disk of a running instance in multiple phases, turning
        off the instance before the end.
        """
        # 0. Zero out the progress to begin
        self._update_instance_progress(context, instance,
                                       step=0,
                                       total_steps=RESIZE_TOTAL_STEPS)

        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['name'])
        host_ref = self._get_host_ref_from_name(dest)
        if host_ref is None:
            raise exception.HostNotFound(host=dest)

        # 1. Power off the instance
        self.power_off(instance)
        self._update_instance_progress(context, instance,
                                       step=1,
                                       total_steps=RESIZE_TOTAL_STEPS)

        # 2. Rename the original VM with suffix '-orig'
        name_label = self._get_orig_vm_name_label(instance)
        LOG.debug(_("Renaming the VM to %s") % name_label,
                  instance=instance)
        rename_task = self._session._call_method(
                            self._session._get_vim(),
                            "Rename_Task", vm_ref, newName=name_label)
        self._session._wait_for_task(instance['uuid'], rename_task)
        LOG.debug(_("Renamed the VM to %s") % name_label,
                  instance=instance)
        self._update_instance_progress(context, instance,
                                       step=2,
                                       total_steps=RESIZE_TOTAL_STEPS)

        # Get the clone vm spec
        ds_ref = vm_util.get_datastore_ref_and_name(
                            self._session, None, dest)[0]
        client_factory = self._session._get_vim().client.factory
        rel_spec = vm_util.relocate_vm_spec(client_factory, ds_ref, host_ref)
        clone_spec = vm_util.clone_vm_spec(client_factory, rel_spec)
        vm_folder_ref = self._get_vmfolder_ref()

        # 3. Clone VM on ESX host
        LOG.debug(_("Cloning VM to host %s") % dest, instance=instance)
        vm_clone_task = self._session._call_method(
                                self._session._get_vim(),
                                "CloneVM_Task", vm_ref,
                                folder=vm_folder_ref,
                                name=instance['name'],
                                spec=clone_spec)
        self._session._wait_for_task(instance['uuid'], vm_clone_task)
        LOG.debug(_("Cloned VM to host %s") % dest, instance=instance)
        self._update_instance_progress(context, instance,
                                       step=3,
                                       total_steps=RESIZE_TOTAL_STEPS)

    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM."""
        instance_name = self._get_orig_vm_name_label(instance)
        # Destroy the original VM.
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance_name)
        if vm_ref is None:
            LOG.debug(_("instance not present"), instance=instance)
            return

        try:
            LOG.debug(_("Destroying the VM"), instance=instance)
            destroy_task = self._session._call_method(
                                        self._session._get_vim(),
                                        "Destroy_Task", vm_ref)
            self._session._wait_for_task(instance['uuid'], destroy_task)
            LOG.debug(_("Destroyed the VM"), instance=instance)
        except Exception, excep:
            LOG.warn(_("In vmwareapi:vmops:confirm_migration, got this "
                     "exception while destroying the VM: %s") % str(excep))

        if network_info:
            self.unplug_vifs(instance, network_info)

    def finish_revert_migration(self, instance):
        """Finish reverting a resize, powering back on the instance."""
        # The original vm was suffixed with '-orig'; find it using
        # the old suffix, remove the suffix, then power it back on.
        name_label = self._get_orig_vm_name_label(instance)
        vm_ref = vm_util.get_vm_ref_from_name(self._session, name_label)
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=name_label)

        LOG.debug(_("Renaming the VM from %s") % name_label,
                  instance=instance)
        rename_task = self._session._call_method(
                            self._session._get_vim(),
                            "Rename_Task", vm_ref, newName=instance['name'])
        self._session._wait_for_task(instance['uuid'], rename_task)
        LOG.debug(_("Renamed the VM from %s") % name_label,
                  instance=instance)
        self.power_on(instance)

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance=False):
        """Completes a resize, turning on the migrated instance."""
        # 4. Start VM
        self.power_on(instance)
        self._update_instance_progress(context, instance,
                                       step=4,
                                       total_steps=RESIZE_TOTAL_STEPS)

    def live_migration(self, context, instance_ref, dest,
                       post_method, recover_method, block_migration=False):
        """Spawning live_migration operation for distributing high-load."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance_ref.name)
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance_ref.name)
        host_ref = self._get_host_ref_from_name(dest)
        if host_ref is None:
            raise exception.HostNotFound(host=dest)

        LOG.debug(_("Migrating VM to host %s") % dest, instance=instance_ref)
        try:
            vm_migrate_task = self._session._call_method(
                                    self._session._get_vim(),
                                    "MigrateVM_Task", vm_ref,
                                    host=host_ref,
                                    priority="defaultPriority")
            self._session._wait_for_task(instance_ref['uuid'], vm_migrate_task)
        except Exception:
            with excutils.save_and_reraise_exception():
                recover_method(context, instance_ref, dest, block_migration)
        post_method(context, instance_ref, dest, block_migration)
        LOG.debug(_("Migrated VM to host %s") % dest, instance=instance_ref)

    def poll_rebooting_instances(self, timeout, instances):
        """Poll for rebooting instances."""
        ctxt = nova_context.get_admin_context()

        instances_info = dict(instance_count=len(instances),
                timeout=timeout)

        if instances_info["instance_count"] > 0:
            LOG.info(_("Found %(instance_count)d hung reboots "
                    "older than %(timeout)d seconds") % instances_info)

        for instance in instances:
            LOG.info(_("Automatically hard rebooting %d") % instance['uuid'])
            self.compute_api.reboot(ctxt, instance, "HARD")

    def get_info(self, instance):
        """Return data about the VM instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['name'])

        lst_properties = ["summary.config.numCpu",
                    "summary.config.memorySizeMB",
                    "runtime.powerState"]
        vm_props = self._session._call_method(vim_util,
                    "get_object_properties", None, vm_ref, "VirtualMachine",
                    lst_properties)
        max_mem = None
        pwr_state = None
        num_cpu = None
        for elem in vm_props:
            for prop in elem.propSet:
                if prop.name == "summary.config.numCpu":
                    num_cpu = int(prop.val)
                elif prop.name == "summary.config.memorySizeMB":
                    # In MB, but we want in KB
                    max_mem = int(prop.val) * 1024
                elif prop.name == "runtime.powerState":
                    pwr_state = VMWARE_POWER_STATES[prop.val]

        return {'state': pwr_state,
                'max_mem': max_mem,
                'mem': max_mem,
                'num_cpu': num_cpu,
                'cpu_time': 0}

    def get_diagnostics(self, instance):
        """Return data about VM diagnostics."""
        msg = _("get_diagnostics not implemented for vmwareapi")
        raise NotImplementedError(msg)

    def get_console_output(self, instance):
        """Return snapshot of console."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])
        param_list = {"id": str(vm_ref)}
        base_url = "%s://%s/screen?%s" % (self._session._scheme,
                                         self._session._host_ip,
                                         urllib.urlencode(param_list))
        request = urllib2.Request(base_url)
        base64string = base64.encodestring(
                        '%s:%s' % (
                        self._session._host_username,
                        self._session._host_password)).replace('\n', '')
        request.add_header("Authorization", "Basic %s" % base64string)
        result = urllib2.urlopen(request)
        if result.code == 200:
            return result.read()
        else:
            return ""

    def get_vnc_console(self, instance):
        """Return connection info for a vnc console."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        return {'host': CONF.vmwareapi_host_ip,
                'port': self._get_vnc_port(vm_ref),
                'internal_access_path': None}

    @staticmethod
    def _get_vnc_port(vm_ref):
        """Return VNC port for an VM."""
        vm_id = int(vm_ref.value.replace('vm-', ''))
        port = CONF.vnc_port + vm_id % CONF.vnc_port_total

        return port

    @staticmethod
    def _get_machine_id_str(network_info):
        machine_id_str = ''
        for vif in network_info:
            # TODO(vish): add support for dns2
            # TODO(sateesh): add support for injection of ipv6 configuration
            network = vif['network']
            ip_v4 = netmask_v4 = gateway_v4 = broadcast_v4 = dns = None
            subnets_v4 = [s for s in network['subnets'] if s['version'] == 4]
            if len(subnets_v4[0]['ips']) > 0:
                ip_v4 = subnets_v4[0]['ips'][0]
            if len(subnets_v4[0]['dns']) > 0:
                dns = subnets_v4[0]['dns'][0]['address']

            netmask_v4 = str(subnets_v4[0].as_netaddr().netmask)
            gateway_v4 = subnets_v4[0]['gateway']['address']
            broadcast_v4 = str(subnets_v4[0].as_netaddr().broadcast)

            interface_str = ";".join([vif['address'],
                                      ip_v4 and ip_v4['address'] or '',
                                      netmask_v4 or '',
                                      gateway_v4 or '',
                                      broadcast_v4 or '',
                                      dns or ''])
            machine_id_str = machine_id_str + interface_str + '#'
        return machine_id_str

    def _set_machine_id(self, client_factory, instance, network_info):
        """
        Set the machine id of the VM for guest tools to pick up and reconfigure
        the network interfaces.
        """
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        machine_id_change_spec = vm_util.get_machine_id_change_spec(
                                 client_factory,
                                 self._get_machine_id_str(network_info))

        LOG.debug(_("Reconfiguring VM instance to set the machine id"),
                  instance=instance)
        reconfig_task = self._session._call_method(self._session._get_vim(),
                           "ReconfigVM_Task", vm_ref,
                           spec=machine_id_change_spec)
        self._session._wait_for_task(instance['uuid'], reconfig_task)
        LOG.debug(_("Reconfigured VM instance to set the machine id"),
                  instance=instance)

    def _set_vnc_config(self, client_factory, instance, port, password):
        """
        Set the vnc configuration of the VM.
        """
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        vnc_config_spec = vm_util.get_vnc_config_spec(
                                      client_factory, port, password)

        LOG.debug(_("Reconfiguring VM instance to enable vnc on "
                  "port - %(port)s") % {'port': port},
                  instance=instance)
        reconfig_task = self._session._call_method(self._session._get_vim(),
                           "ReconfigVM_Task", vm_ref,
                           spec=vnc_config_spec)
        self._session._wait_for_task(instance['uuid'], reconfig_task)
        LOG.debug(_("Reconfigured VM instance to enable vnc on "
                  "port - %(port)s") % {'port': port},
                  instance=instance)

    def _get_datacenter_ref_and_name(self):
        """Get the datacenter name and the reference."""
        dc_obj = self._session._call_method(vim_util, "get_objects",
                "Datacenter", ["name"])
        return dc_obj[0].obj, dc_obj[0].propSet[0].val

    def _get_host_ref_from_name(self, host_name):
        """Get reference to the host with the name specified."""
        host_objs = self._session._call_method(vim_util, "get_objects",
                    "HostSystem", ["name"])
        for host in host_objs:
            if host.propSet[0].val == host_name:
                return host.obj
        return None

    def _get_vmfolder_ref(self):
        """Get the Vm folder ref from the datacenter."""
        dc_objs = self._session._call_method(vim_util, "get_objects",
                                             "Datacenter", ["vmFolder"])
        # There is only one default datacenter in a standalone ESX host
        vm_folder_ref = dc_objs[0].propSet[0].val
        return vm_folder_ref

    def _get_res_pool_ref(self):
        # Get the resource pool. Taking the first resource pool coming our
        # way. Assuming that is the default resource pool.
        if self._cluster is None:
            res_pool_ref = self._session._call_method(vim_util, "get_objects",
                                                      "ResourcePool")[0].obj
        else:
            res_pool_ref = self._session._call_method(vim_util,
                                                      "get_dynamic_property",
                                                      self._cluster,
                                                      "ClusterComputeResource",
                                                      "resourcePool")
        return res_pool_ref

    def _path_exists(self, ds_browser, ds_path):
        """Check if the path exists on the datastore."""
        search_task = self._session._call_method(self._session._get_vim(),
                                   "SearchDatastore_Task",
                                   ds_browser,
                                   datastorePath=ds_path)
        # Wait till the state changes from queued or running.
        # If an error state is returned, it means that the path doesn't exist.
        while True:
            task_info = self._session._call_method(vim_util,
                                       "get_dynamic_property",
                                       search_task, "Task", "info")
            if task_info.state in ['queued', 'running']:
                time.sleep(2)
                continue
            break
        if task_info.state == "error":
            return False
        return True

    def _path_file_exists(self, ds_browser, ds_path, file_name):
        """Check if the path and file exists on the datastore."""
        client_factory = self._session._get_vim().client.factory
        search_spec = vm_util.search_datastore_spec(client_factory, file_name)
        search_task = self._session._call_method(self._session._get_vim(),
                                   "SearchDatastore_Task",
                                   ds_browser,
                                   datastorePath=ds_path,
                                   searchSpec=search_spec)
        # Wait till the state changes from queued or running.
        # If an error state is returned, it means that the path doesn't exist.
        while True:
            task_info = self._session._call_method(vim_util,
                                       "get_dynamic_property",
                                       search_task, "Task", "info")
            if task_info.state in ['queued', 'running']:
                time.sleep(2)
                continue
            break
        if task_info.state == "error":
            return False, False

        file_exists = (getattr(task_info.result, 'file', False) and
                       task_info.result.file[0].path == file_name)
        return True, file_exists

    def _mkdir(self, ds_path):
        """
        Creates a directory at the path specified. If it is just "NAME",
        then a directory with this name is created at the topmost level of the
        DataStore.
        """
        LOG.debug(_("Creating directory with path %s") % ds_path)
        dc_ref = self._get_datacenter_ref_and_name()[0]
        self._session._call_method(self._session._get_vim(), "MakeDirectory",
                    self._session._get_vim().get_service_content().fileManager,
                    name=ds_path, datacenter=dc_ref,
                    createParentDirectories=False)
        LOG.debug(_("Created directory with path %s") % ds_path)

    def _check_if_folder_file_exists(self, ds_ref, ds_name,
                                     folder_name, file_name):
        ds_browser = vim_util.get_dynamic_property(
                                self._session._get_vim(),
                                ds_ref,
                                "Datastore",
                                "browser")
        # Check if the folder exists or not. If not, create one
        # Check if the file exists or not.
        folder_path = vm_util.build_datastore_path(ds_name, folder_name)
        folder_exists, file_exists = self._path_file_exists(ds_browser,
                                                            folder_path,
                                                            file_name)
        if not folder_exists:
            self._mkdir(vm_util.build_datastore_path(ds_name, folder_name))

        return file_exists

    def inject_network_info(self, instance, network_info):
        """inject network info for specified instance."""
        # Set the machine.id parameter of the instance to inject
        # the NIC configuration inside the VM
        client_factory = self._session._get_vim().client.factory
        self._set_machine_id(client_factory, instance, network_info)

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        pass

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        pass

    def list_interfaces(self, instance_name):
        """
        Return the IDs of all the virtual network interfaces attached to the
        specified instance, as a list.  These IDs are opaque to the caller
        (they are only useful for giving back to this layer as a parameter to
        interface_stats).  These IDs only need to be unique for a given
        instance.
        """
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance_name)
        if vm_ref is None:
            raise exception.InstanceNotFound(instance_id=instance_name)

        interfaces = []
        # Get the virtual network interfaces attached to the VM
        hardware_devices = self._session._call_method(vim_util,
                    "get_dynamic_property", vm_ref,
                    "VirtualMachine", "config.hardware.device")

        for device in hardware_devices:
            if device.__class__.__name__ in ["VirtualE1000", "VirtualE1000e",
                "VirtualPCNet32", "VirtualVmxnet"]:
                interfaces.append(device.key)

        return interfaces
