# vim: tabstop=4 shiftwidth=4 softtabstop=4

"""VIF drivers for VMware."""

import network_util


def ensure_vlan_bridge(session, vif, cluster=None):
    """Create a vlan and bridge unless they already exist."""
    vlan_num = vif['vlan']
    bridge = vif['pg']
    vlan_interface = 'vmnic0'
    if 'promiscuous_mode' in vif:
        promiscuous_mode = vif['promiscuous_mode']
    else:
        promiscuous_mode = False

    # Check if the vlan_interface physical network adapter exists on the
    # host.
    if not network_util.check_if_vlan_interface_exists(session,
                                                       vlan_interface,
                                                       cluster):
        raise Exception("Interface %s not found" % vlan_interface)

    # Get the vSwitch associated with the Physical Adapter
    vswitch_associated = network_util.get_vswitch_for_vlan_interface(
                                        session, vlan_interface, cluster)
    if vswitch_associated is None:
        raise Exception("vSwitch associated with %s not found" % vlan_interface)
    # Check whether bridge already exists and retrieve the the ref of the
    # network whose name_label is "bridge"
    network_ref = network_util.get_network_with_the_name(session, bridge,
                                                         cluster)
    if network_ref is None:
        # Create a port group on the vSwitch associated with the
        # vlan_interface corresponding physical network adapter on the ESX
        # host.
        network_util.create_port_group(session, bridge,
                                       vswitch_associated, vlan_num,
                                       cluster, promiscuous_mode)
