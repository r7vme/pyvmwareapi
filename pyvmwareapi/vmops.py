# vim: tabstop=4 shiftwidth=4 softtabstop=4

"""
Class for VM tasks like spawn, snapshot, suspend, resume etc.
"""

import os
import time
import logging

import network_util
import vif as vmwarevif
import vim_util
import vm_util

LOG = logging.getLogger()
LOG.addHandler(logging.StreamHandler())

VMWARE_PREFIX = 'vmware'
RESIZE_TOTAL_STEPS = 4


class VMwareVMOps(object):
    """Management class for VM-related tasks."""

    def __init__(self, session, volumeops):
        """Initializer."""
        self._session = session
        self._volumeops = volumeops
        self._cluster = None
        self._instance_path_base = VMWARE_PREFIX
        self._default_root_device = 'vda'
        self._rescue_suffix = '-rescue'
        self._poll_rescue_last_ran = None

    def list_instances(self):
        """Lists the VM instances that are registered with the ESX host."""
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
        return lst_vm_names

    def spawn(self, instance, disk_size, network_info,
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
            raise Exception('VM "%s" exists' % instance['name'])

        client_factory = self._session._get_vim().client.factory
        service_content = self._session._get_vim().get_service_content()
        ds = vm_util.get_datastore_ref_and_name(self._session, self._cluster)
        data_store_ref = ds[0]
        data_store_name = ds[1]

        vmdk_file_size_in_kb = int(disk_size) / 1024
        os_type = "otherGuest"
        adapter_type = "lsiLogic"
        disk_type = "preallocated"

        vm_folder_ref = self._get_vmfolder_ref()
        res_pool_ref = self._get_res_pool_ref()

        def _get_vif_infos():
            vif_infos = []
            if network_info is None:
                return vif_infos
            for vif in network_info:
                mac_address = vif['address']
                network_name = vif['pg']
                network_ref = vmwarevif.ensure_vlan_bridge(
                                                        self._session, vif,
                                                        self._cluster)
                vif_infos.append({'network_name': network_name,
                                  'mac_address': mac_address,
                                  'network_ref': network_ref,
                                 })
            return vif_infos

        vif_infos = _get_vif_infos()

        # Get the create vm config spec
        config_spec = vm_util.get_vm_create_spec(
                            client_factory, instance,
                            data_store_name, vif_infos, os_type)

        def _execute_create_vm():
            """Create VM on ESX host."""
            # Create the VM on the ESX host
            vm_create_task = self._session._call_method(
                                    self._session._get_vim(),
                                    "CreateVM_Task", vm_folder_ref,
                                    config=config_spec, pool=res_pool_ref)
            self._session._wait_for_task(instance['name'], vm_create_task)


        _execute_create_vm()
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])

        def _create_virtual_disk():
            """Create a virtual disk of the size of flat vmdk file."""
            # Create a Virtual Disk of the size of the flat vmdk file. This is
            # done just to generate the meta-data file whose specifics
            # depend on the size of the disk, thin/thick provisioning and the
            # storage adapter type.
            # Here we assume thick provisioning and lsiLogic for the adapter
            # type
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
            self._session._wait_for_task(instance['name'], vmdk_create_task)

        def _delete_disk_file(vmdk_path):
            # Delete the vmdk file.
            vmdk_delete_task = self._session._call_method(
                        self._session._get_vim(),
                        "DeleteDatastoreFile_Task",
                        service_content.fileManager,
                        name=vmdk_path,
                        datacenter=dc_ref)
            self._session._wait_for_task(instance[''], vmdk_delete_task)

        def _copy_virtual_disk():
            """Copy a sparse virtual disk to a thin virtual disk."""
            # Copy a sparse virtual disk to a thin virtual disk. This is also
            # done to generate the meta-data file whose specifics
            # depend on the size of the disk, thin/thick provisioning and the
            # storage adapter type.
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
            self._session._wait_for_task(instance['name'], vmdk_copy_task)

        ebs_root = None

        if not ebs_root:
            upload_folder = instance['name']
            upload_name = instance['name']

            # The vmdk meta-data file
            uploaded_vmdk_name = "%s/%s.vmdk" % (upload_folder, upload_name)
            uploaded_vmdk_path = vm_util.build_datastore_path(data_store_name,
                                                uploaded_vmdk_name)

            if not self._check_if_folder_file_exists(
                                        data_store_ref, data_store_name,
                                        upload_folder, upload_name + ".vmdk"):

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

                if disk_type == "sparse":
                    # Copy the sparse virtual disk to a thin virtual disk.
                    disk_type = "thin"
                    _copy_virtual_disk()
            else:
                # linked clone base disk exists
                if disk_type == "sparse":
                    disk_type = "thin"

            # Attach the vmdk uploaded to the VM.
            self._volumeops.attach_disk_to_vm(
                                vm_ref, instance,
                                adapter_type, disk_type, uploaded_vmdk_path,
                                vmdk_file_size_in_kb, False)
        else:
            # Attach the root disk to the VM.
            pass

        def _power_on_vm():
            """Power on the VM."""
            # Power On the VM
            power_on_task = self._session._call_method(
                               self._session._get_vim(),
                               "PowerOnVM_Task", vm_ref)
            self._session._wait_for_task(instance['name'], power_on_task)
        _power_on_vm()

    def reboot(self, instance):
        """Reboot a VM instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise Exception('VM "%s" not found.' % instance['name'])

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
            raise Exception("Instance not powered on.")

        # If latest vmware tools are installed in the VM, and that the tools
        # are running, then only do a guest reboot. Otherwise do a hard reset.
        if (tools_status == "toolsOk" and
                tools_running_status == "guestToolsRunning"):
            self._session._call_method(self._session._get_vim(), "RebootGuest",
                                       vm_ref)
        else:
            reset_task = self._session._call_method(self._session._get_vim(),
                                                    "ResetVM_Task", vm_ref)
            self._session._wait_for_task(instance['name'], reset_task)

    def destroy(self, instance, destroy_disks=True):
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
                poweroff_task = self._session._call_method(
                       self._session._get_vim(),
                       "PowerOffVM_Task", vm_ref)
                self._session._wait_for_task(instance['name'], poweroff_task)

            # Un-register the VM
            try:
                self._session._call_method(self._session._get_vim(),
                                           "UnregisterVM", vm_ref)
            except Exception, excep:
                LOG.warn("In vmwareapi:vmops:destroy, got this exception"
                           " while un-registering the VM: %s" % str(excep))

            # Delete the folder holding the VM related content on
            # the datastore.
            if destroy_disks:
                try:
                    dir_ds_compliant_path = vm_util.build_datastore_path(
                                     datastore_name,
                                     os.path.dirname(vmx_file_path))
                    vim = self._session._get_vim()
                    delete_task = self._session._call_method(
                        vim,
                        "DeleteDatastoreFile_Task",
                        vim.get_service_content().fileManager,
                        name=dir_ds_compliant_path,
                        datacenter=self._get_datacenter_ref_and_name()[0])
                    self._session._wait_for_task(instance['name'], delete_task)
                except Exception, excep:
                    LOG.warn("In vmwareapi:vmops:destroy, "
                                 "got this exception while deleting"
                                 " the VM contents from the disk: %s"
                                 % str(excep))
        except Exception, exc:
            raise exc

    def _get_orig_vm_name_label(self, instance):
        return instance['name'] + '-orig'

    def get_info(self, instance):
        """Return data about the VM instance."""
        vm_ref = vm_util.get_vm_ref_from_name(self._session, instance['name'])
        if vm_ref is None:
            raise Exception('VM "%s" not found.' % instance['name'])

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
                    pwr_state = prop.val

        return {'state': pwr_state,
                'max_mem': max_mem,
                'mem': max_mem,
                'num_cpu': num_cpu,
                'cpu_time': 0}

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
        dc_ref = self._get_datacenter_ref_and_name()[0]
        self._session._call_method(self._session._get_vim(), "MakeDirectory",
                    self._session._get_vim().get_service_content().fileManager,
                    name=ds_path, datacenter=dc_ref,
                    createParentDirectories=False)

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
