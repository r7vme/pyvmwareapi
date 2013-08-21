#!/usr/bin/env python2

import vim_util
import suds

class VIMMessagePlugin(suds.plugin.MessagePlugin):

    def addAttributeForValue(self, node):
        # suds does not handle AnyType properly.
        # VI SDK requires type attribute to be set when AnyType is used
        if node.name == 'value':
            node.set('xsi:type', 'xsd:string')

    def marshalled(self, context):
        """suds will send the specified soap envelope.
        Provides the plugin with the opportunity to prune empty
        nodes and fixup nodes before sending it to the server.
        """
        # suds builds the entire request object based on the wsdl schema.
        # VI SDK throws server errors if optional SOAP nodes are sent
        # without values, e.g. <test/> as opposed to <test>test</test>
        context.envelope.prune()
        context.envelope.walk(self.addAttributeForValue)


class Vim:
    """The VIM Object."""

    def __init__(self,
                 protocol="https",
                 host="localhost"):

        self._protocol = protocol
        self._host_name = host
        wsdl_url = "https://%s/sdk/vimService.wsdl" % self._host_name
        url = '%s://%s/sdk' % (self._protocol, self._host_name)
        self.client = suds.client.Client(wsdl_url, location=url,
                            plugins=[VIMMessagePlugin()])
        self._service_content = self.RetrieveServiceContent("ServiceInstance")

    def get_service_content(self):
        """Gets the service content object."""
        return self._service_content

    def __getattr__(self, attr_name):
        """Makes the API calls and gets the result."""
        def vim_request_handler(managed_object, **kwargs):
            """
            Builds the SOAP message and parses the response for fault
            checking and other errors.

            managed_object    : Managed Object Reference or Managed
                                Object Name
            **kwargs          : Keyword arguments of the call
            """
            # Dynamic handler for VI SDK Calls
            request_mo = self._request_managed_object_builder(
                         managed_object)
            request = getattr(self.client.service, attr_name)
            response = request(request_mo, **kwargs)
            return response
        return vim_request_handler

    def _request_managed_object_builder(self, managed_object):
        """Builds the request managed object."""
        # Request Managed Object Builder
        if isinstance(managed_object, str):
            mo = suds.sudsobject.Property(managed_object)
            mo._type = managed_object
        else:
            mo = managed_object
        return mo

    def __repr__(self):
        return "VIM Object"

    def __str__(self):
        return "VIM Object"

class VMwareAPISession(object):
    """
    Sets up a session with the ESX host and handles all
    the calls made to the host.
    """

    def __init__(self, host_ip, host_username, host_password,
                 scheme="https"):
        self._host_ip = host_ip
        self._host_username = host_username
        self._host_password = host_password
        self._scheme = scheme
        self._session_id = None
        self.vim = Vim(protocol=self._scheme, host=self._host_ip)
        self._create_session()

    def _create_session(self):
        """Creates a session with the ESX host."""
        session = self.vim.Login(
                       self.vim.get_service_content().sessionManager,
                       userName=self._host_username,
                       password=self._host_password)

    def _call_method(self, module, method, *args, **kwargs):
        """
        Calls a method within the module specified with
        args provided.
        """
        args = list(args)
        temp_module = module
        args = [self.vim] + args

        for method_elem in method.split("."):
            temp_module = getattr(temp_module, method_elem)

        return temp_module(*args, **kwargs)

class ESXI:

    def __init__(self, host, user, password ,scheme="https"):

        self._host_ip = host
        self._session = VMwareAPISession(self._host_ip,
                                         user, password,
                                         scheme=scheme)

    def get_vms(self):
        vms = self._session._call_method(vim_util, "get_objects",
                                              "VirtualMachine", ["name"])
        return vms

def main():
   esxi = ESXI("172.18.210.165", "root", "Mirantis01")
   print esxi.get_vms()

if __name__ == "__main__":
    main()
