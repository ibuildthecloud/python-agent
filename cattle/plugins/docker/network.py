import logging

from cattle.plugins.docker.util import add_to_env, add_label, \
    is_nonrancher_container
from cattle.utils import get_or_create_map, get_or_create_list

log = logging.getLogger('docker')


def setup_network_mode(instance, compute, client, create_config, start_config):
    """
    Based on the network configuration we choose the network mode to set in
    Docker.  We only really look for none, host, or container.  For all
    all other configurations we assume bridge mode
    """
    try:
        kind = instance.nics[0].network.kind
        if kind == 'dockerHost':
            start_config['network_mode'] = 'host'
        elif kind == 'dockerNone':
            create_config['network_disabled'] = True
        elif kind == 'dockerContainer':
            id = instance.networkContainer.uuid
            other = compute.get_container(client, instance.networkContainer)
            if other is not None:
                id = other['Id']
            start_config['network_mode'] = 'container:{}'.format(id)
    except (KeyError, AttributeError, IndexError):
        pass



def setup_mac_and_ip(instance, create_config):
    """
    Configures the mac address and primary ip address for the the supplied
    container. The mac_address is configured directly as part of the native
    docker API. The primary IP address is set as an environment variable on the
    container. Another Rancher micro-service will detect this environment
    variable when the container is started and inject the IP into the
    container.

    Note: while an instance can technically have more than one nic based on the
    resource schema, this implementation assumes a single nic for the purpose
    of configuring the mac address and IP.
    """
    mac_address = None
    device_number = None
    for nic in instance.nics:
        if device_number is None:
            mac_address = nic.macAddress
            device_number = nic.deviceNumber
        elif device_number > nic.deviceNumber:
            mac_address = nic.macAddress
            device_number = nic.deviceNumber
    create_config["mac_address"] = mac_address

    try:
        if instance.nics and instance.nics[0].ipAddresses:
            # Assume one nic
            nic = instance.nics[0]
            ip_address = None
            for ip in nic.ipAddresses:
                if ip.role == 'primary':
                    ip_address = '{0}/{1}'.format(ip.address,
                                                  ip.subnet.cidrSize)
                    break

            if ip_address:
                add_label(create_config,
                          {'io.rancher.container.ip': ip_address})
    except (KeyError, AttributeError):
        pass


def setup_ports(instance, create_config, start_config):
    """
    Sets up a container's config for rancher-managed ports by removing the
    native docker port configuration. We do this because rancher emulates ports
    outside of Docker's direct purview to allow for multi-host networking.

    Note that a non-rancher container (one created and started outside the
    rancher API) will not have its port configuration manipulated. This is
    because on a container restart, we would not be able to properly rebuild
    the port config because it depends on manipulating the create_config.
    """
    if not _has_service(instance, 'portService') or is_nonrancher_container(
            instance):
        return

    if 'ports' in create_config:
        del create_config['ports']

    start_config['publish_all_ports'] = False


def setup_links(instance, create_config, start_config):
    """
    Sets up a container's config for rancher-managed links by removing the
    docker native link configuration and emulating links through environment
    variables.

    Note that a non-rancher container (one created and started outside the
    rancher API) container will not have its link configuration manipulated.
    This is because on a container restart, we would not be able to properly
    rebuild the link configuration because it depends on manipulating the
    create_config.
    """
    if not _has_service(instance, 'linkService') or is_nonrancher_container(
            instance):
        return

    if 'links' in start_config:
        del start_config['links']

    result = {}
    for link in instance.instanceLinks:
        name = link.linkName

        try:
            for link_port in link.data.fields.ports:
                proto = link_port.protocol
                ip = name.lower()
                dst = link_port.privatePort
                port = link_port.privatePort

                full_port = '{0}://{1}:{2}'.format(proto, ip, dst)

                data = {
                    'NAME': '/cattle/{0}'.format(name),
                    'PORT': full_port,
                    'PORT_{0}_{1}'.format(port, proto): full_port,
                    'PORT_{0}_{1}_ADDR'.format(port, proto): ip,
                    'PORT_{0}_{1}_PORT'.format(port, proto): dst,
                    'PORT_{0}_{1}_PROTO'.format(port, proto): proto,
                }

                for k, v in data.items():
                    result['{0}_{1}'.format(name, k).upper()] = v
        except AttributeError:
            pass

    if len(result) > 0:
        add_to_env(create_config, **result)


def setup_ipsec(instance, host, create_config, start_config):
    """
    If the supplied instance is a network agent, configures the ports needed
    to achieve multi-host networking.
    """
    if instance.get('agentId') is None:
        network_agent = False
    elif instance.get('systemContainer') is None or \
            instance.get('systemContainer') == 'NetworkAgent':
        network_agent = True
    else:
        network_agent = False

    if not network_agent or \
            not _has_service(instance, 'ipsecTunnelService'):
        return

    try:
        host_id = str(host.id)
        nat = instance.data.ipsec[host_id]['nat']
        isakmp = instance.data.ipsec[host_id]['isakmp']

        ports = get_or_create_list(create_config, 'ports')
        binding = get_or_create_map(start_config, 'port_bindings')

        ports.append((500, 'udp'))
        ports.append((4500, 'udp'))
        binding['500/udp'] = ('0.0.0.0', isakmp)
        binding['4500/udp'] = ('0.0.0.0', nat)
    except (KeyError, AttributeError):
        pass


def _has_service(instance, kind):
    try:
        for nic in instance.nics:
            if nic.deviceNumber != 0:
                continue

            for service in nic.network.networkServices:
                if service.kind == kind:
                    return True
    except (KeyError, AttributeError):
        pass

    return False
