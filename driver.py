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
A connection to the VMware ESX platform.

"""

import time
import logging

from eventlet import event

import vim
import vim_util
import vm_util
import vmops
import utils
import volumeops

LOG = logging.getLogger()
LOG.addHandler(logging.StreamHandler())

TIME_BETWEEN_API_CALL_RETRIES = 2.0
API_RETRY_COUNT = 10
TASK_POLL_INTERVAL = 5.0

class VMwareESXDriver:
    """The ESX host connection object."""

    def __init__(self, host, user, password, read_only=False, scheme="https"):

        self._host_ip = host
        host_username = user
        host_password = password
        api_retry_count = API_RETRY_COUNT

        self._session = VMwareAPISession(self._host_ip,
                                         host_username, host_password,
                                         api_retry_count, scheme=scheme)
        self._volumeops = volumeops.VMwareVolumeOps(self._session) 
        self._vmops = vmops.VMwareVMOps(self._session, self._volumeops)

    def list_instances(self):
        """List VM instances."""
        return self._vmops.list_instances()

    def spawn(self, instance, disk_size,
              network_info=None, block_device_info=None):
        """Create VM instance."""
        self._vmops.spawn(instance, disk_size, network_info,
                          block_device_info)

    def destroy(self, instance, network_info, block_device_info=None,
                destroy_disks=True):
        """Destroy VM instance."""
        self._vmops.destroy(instance, network_info, destroy_disks)

    def get_info(self, instance):
        """Return info about the VM instance."""
        return self._vmops.get_info(instance)


class VMwareAPISession(object):
    """
    Sets up a session with the ESX host and handles all
    the calls made to the host.
    """

    def __init__(self, host_ip, host_username, host_password,
                 api_retry_count, scheme="https"):
        self._host_ip = host_ip
        self._host_username = host_username
        self._host_password = host_password
        self.api_retry_count = api_retry_count
        self._scheme = scheme
        self._session_id = None
        self.vim = None
        self._create_session()

    def _get_vim_object(self):
        """Create the VIM Object instance."""
        return vim.Vim(protocol=self._scheme, host=self._host_ip)

    def _create_session(self):
        """Creates a session with the ESX host."""
        while True:
            try:
                # Login and setup the session with the ESX host for making
                # API calls
                self.vim = self._get_vim_object()
                session = self.vim.Login(
                               self.vim.get_service_content().sessionManager,
                               userName=self._host_username,
                               password=self._host_password)
                # Terminate the earlier session, if possible ( For the sake of
                # preserving sessions as there is a limit to the number of
                # sessions we can have )
                if self._session_id:
                    try:
                        self.vim.TerminateSession(
                                self.vim.get_service_content().sessionManager,
                                sessionId=[self._session_id])
                    except Exception, excep:
                        # This exception is something we can live with. It is
                        # just an extra caution on our side. The session may
                        # have been cleared. We could have made a call to
                        # SessionIsActive, but that is an overhead because we
                        # anyway would have to call TerminateSession.
                        LOG.debug(excep)
                self._session_id = session.key
                return
            except Exception, excep:
                LOG.critical("In vmwareapi:_create_session, "
                             "got this exception: %s" % excep)
                raise Exception(excep)

    def __del__(self):
        """Logs-out the session."""
        # Logout to avoid un-necessary increase in session count at the
        # ESX host
        try:
            self.vim.Logout(self.vim.get_service_content().sessionManager)
        except Exception, excep:
            # It is just cautionary on our part to do a logout in del just
            # to ensure that the session is not left active.
            LOG.debug(excep)

    def _is_vim_object(self, module):
        """Check if the module is a VIM Object instance."""
        return isinstance(module, vim.Vim)

    def _call_method(self, module, method, *args, **kwargs):
        """
        Calls a method within the module specified with
        args provided.
        """
        args = list(args)
        retry_count = 0
        exc = None
        last_fault_list = []
        while True:
            try:
                if not self._is_vim_object(module):
                    # If it is not the first try, then get the latest
                    # vim object
                    if retry_count > 0:
                        args = args[1:]
                    args = [self.vim] + args
                retry_count += 1
                temp_module = module

                for method_elem in method.split("."):
                    temp_module = getattr(temp_module, method_elem)

                return temp_module(*args, **kwargs)
            except Exception, excep:
                # If it is a proper exception, say not having furnished
                # proper data in the SOAP call or the retry limit having
                # exceeded, we raise the exception
                exc = excep
                break
            # If retry count has been reached then break and
            # raise the exception
            if retry_count > self.api_retry_count:
                break
            time.sleep(TIME_BETWEEN_API_CALL_RETRIES)

        LOG.critical("In vmwareapi:_call_method, "
                     "got this exception: %s" % exc)
        raise

    def _get_vim(self):
        """Gets the VIM object reference."""
        if self.vim is None:
            self._create_session()
        return self.vim

    def _wait_for_task(self, instance_uuid, task_ref):
        """
        Return a Deferred that will give the result of the given task.
        The task is polled until it completes.
        """
        done = event.Event()
        loop = utils.FixedIntervalLoopingCall(self._poll_task, instance_uuid,
                                              task_ref, done)
        loop.start(TASK_POLL_INTERVAL)
        ret_val = done.wait()
        loop.stop()
        return ret_val

    def _poll_task(self, instance_uuid, task_ref, done):
        """
        Poll the given task, and fires the given Deferred if we
        get a result.
        """
        try:
            task_info = self._call_method(vim_util, "get_dynamic_property",
                            task_ref, "Task", "info")
            task_name = task_info.name
            if task_info.state in ['queued', 'running']:
                return
            elif task_info.state == 'success':
                done.send("success")
            else:
                error_info = str(task_info.error.localizedMessage)
                LOG.warn("Task [%(task_name)s] %(task_ref)s "
                         "status: error %(error_info)s" % locals())
                done.send_exception(Exception(error_info))
        except Exception, excep:
            LOG.warn("In vmwareapi:_poll_task, Got this error %s" % excep)
            done.send_exception(excep)
