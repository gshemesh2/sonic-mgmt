import datetime
import time
import socket
import random
import struct
import ipaddress
import logging
import jinja2
import json
import os
import six
import scapy.all as scapyall
import ptf.testutils as testutils
from itertools import groupby

from tests.common.dualtor.dual_tor_common import CableType
from tests.common.utilities import wait_until, convert_scapy_packet_to_bytes
from natsort import natsorted
from collections import defaultdict

TCP_DST_PORT = 5000
SOCKET_RECV_BUFFER_SIZE = 10 * 1024 * 1024
PTFRUNNER_QLEN = 1000
VLAN_INDEX = 0
VLAN_HOSTS = 100
VLAN_BASE_MAC_PATTERN = "72060001{:04}"
LAG_BASE_MAC_PATTERN = '5c010203{:04}'
TEMPLATES_DIR = "templates/"
SUPERVISOR_CONFIG_DIR = "/etc/supervisor/conf.d/"
DUAL_TOR_SNIFFER_CONF_TEMPL = "dual_tor_sniffer.conf.j2"
DUAL_TOR_SNIFFER_CONF = "dual_tor_sniffer.conf"

logger = logging.getLogger(__name__)


class DualTorIO:
    """Class to conduct IO over ports in `active-standby` mode."""

    def __init__(self, activehost, standbyhost, ptfhost, ptfadapter, vmhost, tbinfo,
                 io_ready, tor_vlan_port=None, send_interval=0.01, cable_type=CableType.active_standby,
                 random_dst=None):
        self.tor_pc_intf = None
        self.tor_vlan_intf = tor_vlan_port
        self.duthost = activehost
        self.ptfadapter = ptfadapter
        self.ptfhost = ptfhost
        self.vmhost = vmhost
        self.tbinfo = tbinfo
        self.io_ready_event = io_ready
        self.dut_mac = self.duthost.facts["router_mac"]
        self.active_mac = self.dut_mac
        self.standby_mac = standbyhost.facts["router_mac"]
        self.tcp_sport = 1234

        self.cable_type = cable_type

        if random_dst is None:
            # if random_dst is not set, default to true for active standby dualtor.
            self.random_dst = (self.cable_type == CableType.active_standby)
        else:
            self.random_dst = random_dst

        self.dataplane = self.ptfadapter.dataplane
        self.dataplane.flush()
        self.test_results = dict()
        self.stop_early = False
        self.ptf_sniffer = "/root/dual_tor_sniffer.py"

        # Calculate valid range for T1 src/dst addresses
        mg_facts = self.duthost.get_extended_minigraph_facts(self.tbinfo)
        prefix_len = mg_facts['minigraph_vlan_interfaces'][VLAN_INDEX]['prefixlen'] - 3
        test_network = ipaddress.ip_address(
            mg_facts['minigraph_vlan_interfaces'][VLAN_INDEX]['addr']) +\
            (1 << (32 - prefix_len))
        self.default_ip_range = str(ipaddress.ip_interface((str(test_network) + '/{0}'.format(prefix_len))
                                                           .encode().decode()).network)
        self.src_addr, mask = self.default_ip_range.split('/')
        self.n_hosts = 2**(32 - int(mask))

        self.tor_to_ptf_intf_map = mg_facts['minigraph_ptf_indices']

        portchannel_info = mg_facts['minigraph_portchannels']
        self.tor_pc_intfs = list()
        for pc in list(portchannel_info.values()):
            for member in pc['members']:
                self.tor_pc_intfs.append(member)

        self.vlan_interfaces = list(mg_facts["minigraph_vlans"].values())[VLAN_INDEX]["members"]

        config_facts = self.duthost.get_running_config_facts()
        vlan_table = config_facts['VLAN']
        vlan_name = list(vlan_table.keys())[0]
        self.vlan_mac = vlan_table[vlan_name]['mac']
        self.mux_cable_table = config_facts['MUX_CABLE']

        self.test_interfaces = self._select_test_interfaces()

        self.ptf_intf_to_server_ip_map = self._generate_vlan_servers()
        self.__configure_arp_responder()

        self.ptf_intf_to_soc_ip_map = self._generate_soc_ip_map()

        self.ptf_intf_to_mac_map = {}
        for ptf_intf in list(self.ptf_intf_to_server_ip_map.keys()):
            self.ptf_intf_to_mac_map[ptf_intf] = self.ptfadapter.dataplane.get_mac(0, ptf_intf)

        logger.info("VLAN interfaces: {}".format(str(self.vlan_interfaces)))
        logger.info("PORTCHANNEL interfaces: {}".format(str(self.tor_pc_intfs)))
        logger.info("Selected testing interfaces: %s", self.test_interfaces)
        logger.info("Selected ToR vlan interfaces: %s", self.tor_vlan_intf)

        self.time_to_listen = 300.0
        self.sniff_time_incr = 0
        # Inter-packet send-interval (minimum interval 3.5ms)
        if send_interval < 0.0035:
            if send_interval is not None:
                logger.warning("Minimum packet send-interval is .0035s. \
                    Ignoring user-provided interval {}".format(send_interval))
            self.send_interval = 0.0035
        else:
            self.send_interval = send_interval
        # How many packets to be sent by sender thread
        logger.info("Using send interval {}".format(self.send_interval))
        self.packets_to_send = min(int(self.time_to_listen / (self.send_interval * 2)), 45000)
        self.packets_sent_per_server = dict()

        if self.tor_vlan_intf:
            self.packets_per_server = self.packets_to_send
        else:
            self.packets_per_server = self.packets_to_send // len(self.test_interfaces)

        self.all_packets = []

    def setup_ptf_sniffer(self):
        """Setup ptf sniffer supervisor config."""
        ptf_sniffer_args = '-f "%s" -p %s -l %s -t %s' % (
            self.sniff_filter,
            self.capture_pcap,
            self.capture_log,
            self.sniff_timeout
        )
        templ = jinja2.Template(open(os.path.join(TEMPLATES_DIR, DUAL_TOR_SNIFFER_CONF_TEMPL)).read())
        self.ptfhost.copy(
            content=templ.render(ptf_sniffer=self.ptf_sniffer, ptf_sniffer_args=ptf_sniffer_args),
            dest=os.path.join(SUPERVISOR_CONFIG_DIR, DUAL_TOR_SNIFFER_CONF)
        )
        self.ptfhost.copy(src='scripts/dual_tor_sniffer.py', dest=self.ptf_sniffer)
        self.ptfhost.shell("supervisorctl update")

    def start_ptf_sniffer(self):
        """Start the ptf sniffer."""
        self.ptfhost.shell("supervisorctl start dual_tor_sniffer")

    def stop_ptf_sniffer(self):
        """Stop the ptf sniffer."""
        self.ptfhost.shell("supervisorctl stop dual_tor_sniffer", module_ignore_errors=True)

    def force_stop_ptf_sniffer(self):
        """Force stop the ptf sniffer by sending SIGTERM."""
        logger.info("Force stop the ptf sniffer process by sending SIGTERM")
        self.ptfhost.command("pkill -SIGTERM -f %s" % self.ptf_sniffer, module_ignore_errors=True)

    def _generate_vlan_servers(self):
        """
        Create mapping of server IPs to PTF interfaces
        """
        server_ip_list = []

        for _, config in natsorted(list(self.mux_cable_table.items())):
            server_ip_list.append(str(config['server_ipv4'].split("/")[0]))
        logger.info("ALL server address:\n {}".format(server_ip_list))

        ptf_to_server_map = dict()
        for intf in natsorted(self.test_interfaces):
            ptf_intf = self.tor_to_ptf_intf_map[intf]
            server_ip = str(self.mux_cable_table[intf]['server_ipv4'].split("/")[0])
            ptf_to_server_map[ptf_intf] = server_ip

        logger.debug('VLAN intf to server IP map: {}'.format(json.dumps(ptf_to_server_map, indent=4, sort_keys=True)))
        return ptf_to_server_map

    def _generate_soc_ip_map(self):
        """
        Create mapping of soc IPs to PTF interfaces
        """
        if self.cable_type == CableType.active_standby:
            return {}

        soc_ip_list = []
        for _, config in natsorted(list(self.mux_cable_table.items())):
            if "soc_ipv4" in config:
                soc_ip_list.append(str(config['soc_ipv4'].split("/")[0]))
        logger.info("All soc address:\n {}".format(soc_ip_list))

        ptf_to_soc_map = dict()
        for intf in natsorted(self.test_interfaces):
            ptf_intf = self.tor_to_ptf_intf_map[intf]
            soc_ip = str(self.mux_cable_table[intf]['soc_ipv4'].split('/')[0])
            ptf_to_soc_map[ptf_intf] = soc_ip

        logger.debug('VLAN intf to soc IP map: {}'.format(json.dumps(ptf_to_soc_map, indent=4, sort_keys=True)))
        return ptf_to_soc_map

    def _select_test_interfaces(self):
        """Select DUT interfaces that is in `active-standby` cable type."""
        test_interfaces = []
        for port, port_config in natsorted(list(self.mux_cable_table.items())):
            if port_config.get("cable_type", CableType.active_standby) == self.cable_type:
                test_interfaces.append(port)
        return test_interfaces

    def __configure_arp_responder(self):
        """
        @summary: Generate ARP responder configuration using vlan_host_map.
        Copy this configuration to PTF and restart arp_responder
        """
        arp_responder_conf = {}
        for intf, ip in list(self.ptf_intf_to_server_ip_map.items()):
            arp_responder_conf['eth{}'.format(intf)] = [ip]
        with open("/tmp/from_t1.json", "w") as fp:
            json.dump(arp_responder_conf, fp, indent=4, sort_keys=True)
        self.ptfhost.copy(src="/tmp/from_t1.json", dest="/tmp/from_t1.json", force=True)
        self.ptfhost.shell("supervisorctl reread && supervisorctl update")
        self.ptfhost.shell("supervisorctl restart arp_responder")
        logger.info("arp_responder restarted")

    def generate_traffic(self, traffic_direction=None):
        # Check in a conditional for better readability
        self.traffic_direction = traffic_direction
        if traffic_direction == "server_to_t1":
            self.generate_upstream_traffic()
        elif traffic_direction == "t1_to_server":
            self.generate_downstream_traffic()
        elif traffic_direction == "soc_to_t1":
            self.generate_upstream_traffic(src="soc")
        elif traffic_direction == "t1_to_soc":
            self.generate_downstream_traffic(dst="soc")
        elif traffic_direction == "server_to_server":
            self.generate_server_to_server_traffic()
        else:
            logger.error("Traffic direction not provided or invalid")
            return

    def start_io_test(self):
        """
        @summary: The entry point to start the TOR dataplane I/O test.
        """
        self.send_and_sniff()

    def generate_downstream_traffic(self, dst='server'):
        """
        @summary: Generate (not send) the packets to be sent from T1 to server/soc
        """
        logger.info("Generating T1 to {} packets".format(dst))
        eth_dst = self.dut_mac
        ip_ttl = 255

        if self.tor_pc_intf and self.tor_pc_intf in self.tor_pc_intfs:
            # If a source portchannel intf is specified,
            # get the corresponding PTF info
            ptf_t1_src_intf = self.tor_to_ptf_intf_map[self.tor_pc_intf]
            eth_src = self.ptfadapter.dataplane.get_mac(0, ptf_t1_src_intf)
            random_source = False
        else:
            # If no source portchannel specified, randomly choose one
            # during packet generation
            logger.info('Using random T1 source intf')
            ptf_t1_src_intf = None
            eth_src = None
            random_source = True

        ptf_intf_to_ip_map = self.ptf_intf_to_server_ip_map if dst == 'server' else self.ptf_intf_to_soc_ip_map

        if self.tor_vlan_intf:
            # If destination VLAN intf is specified,
            # use only the connected server/soc
            ptf_port = self.tor_to_ptf_intf_map[self.tor_vlan_intf]
            server_ip_list = [
                ptf_intf_to_ip_map[ptf_port]
            ]
        else:
            # Otherwise send packets to all servers/soc
            server_ip_list = list(ptf_intf_to_ip_map.values())

        logger.info("-"*20 + "T1 to {} packet".format(dst) + "-"*20)
        logger.info("PTF source intf: {}".format('random' if random_source else ptf_t1_src_intf))
        logger.info("Ethernet address: dst: {} src: {}".format(eth_dst, 'random' if random_source else eth_src))
        logger.info("IP address: dst: {} src: random".format('all' if len(server_ip_list) > 1
                                                             else server_ip_list[0]))
        logger.info("TCP port: dst: {}".format(TCP_DST_PORT))
        logger.info("DUT mac: {}".format(self.dut_mac))
        logger.info("VLAN mac: {}".format(self.vlan_mac))
        logger.info("-"*50)

        self.packets_list = []

        # Create packet #1 for each server/soc and append to the list,
        # then packet #2 for each server/soc, etc.
        # This way, when sending packets we continuously send for all servers/soc
        # instead of sending all packets for server/soc #1, then all packets for
        # server/soc #2, etc.
        tcp_tx_packet_orig = testutils.simple_tcp_packet(
            eth_dst=eth_dst,
            eth_src=eth_src,
            ip_ttl=ip_ttl,
            tcp_dport=TCP_DST_PORT,
            tcp_sport=self.tcp_sport
        )
        tcp_tx_packet_orig = scapyall.Ether(convert_scapy_packet_to_bytes(tcp_tx_packet_orig))
        payload_suffix = "X" * 60
        for i in range(self.packets_per_server):
            for server_ip in server_ip_list:
                packet = tcp_tx_packet_orig.copy()
                if random_source:
                    tor_pc_src_intf = random.choice(
                        self.tor_pc_intfs
                    )
                    ptf_t1_src_intf = self.tor_to_ptf_intf_map[tor_pc_src_intf]
                    eth_src = self.ptfadapter.dataplane.get_mac(
                        0, ptf_t1_src_intf
                    )
                packet[scapyall.Ether].src = eth_src
                packet[scapyall.IP].src = self.random_host_ip()
                packet[scapyall.IP].dst = server_ip
                payload = str(i) + payload_suffix
                packet.load = payload
                packet[scapyall.TCP].chksum = None
                packet[scapyall.IP].chksum = None
                self.packets_list.append((ptf_t1_src_intf, convert_scapy_packet_to_bytes(packet)))

        self.sent_pkt_dst_mac = self.dut_mac
        self.received_pkt_src_mac = [self.vlan_mac]

    def generate_upstream_traffic(self, src='server'):
        """
        @summary: Generate (not send) the packets to be sent from server/soc to T1
        """
        logger.info("Generating {} to T1 packets".format(src))
        if self.tor_vlan_intf:
            vlan_src_intfs = [self.tor_vlan_intf]
            # If destination VLAN intf is specified,
            # use only the connected server/soc
        else:
            # Otherwise send packets to all servers/soc
            vlan_src_intfs = self.test_interfaces

        ptf_intf_to_ip_map = self.ptf_intf_to_server_ip_map if src == 'server' else self.ptf_intf_to_soc_ip_map

        logger.info("-"*20 + "{} to T1 packet".format(src) + "-"*20)
        if self.tor_vlan_intf is None:
            src_mac = 'random'
            src_ip = 'random'
        else:
            ptf_port = self.tor_to_ptf_intf_map[self.tor_vlan_intf]
            src_mac = self.ptf_intf_to_mac_map[ptf_port]
            src_ip = ptf_intf_to_ip_map[ptf_port]
        logger.info(
            "Ethernet address: dst: {} src: {}".format(
                self.vlan_mac, src_mac
            )
        )
        logger.info(
            "IP address: dst: {} src: {}".format(
                'random', src_ip
            )
        )
        logger.info("TCP port: dst: {} src: {}".format(TCP_DST_PORT, self.tcp_sport))
        logger.info("DUT ToR MAC: {}, PEER ToR MAC: {}".format(self.active_mac, self.standby_mac))
        logger.info("VLAN MAC: {}".format(self.vlan_mac))
        logger.info("-"*50)

        self.packets_list = []

        # Create packet #1 for each server/soc and append to the list,
        # then packet #2 for each server/soc, etc.
        # This way, when sending packets we continuously send for all servers/soc
        # instead of sending all packets for server/soc #1, then all packets for
        # server/soc #2, etc.
        tcp_tx_packet_orig = testutils.simple_tcp_packet(
            eth_dst=self.vlan_mac,
            tcp_dport=TCP_DST_PORT,
            tcp_sport=self.tcp_sport
        )
        tcp_tx_packet_orig = scapyall.Ether(convert_scapy_packet_to_bytes(tcp_tx_packet_orig))
        payload_suffix = "X" * 60

        # use the same dst ip to ensure that packets from one server are always forwarded
        # to the same active ToR by the server NiC
        dst_ips = {vlan_intf: self.random_host_ip() for vlan_intf in vlan_src_intfs}
        for i in range(self.packets_per_server):
            for vlan_intf in vlan_src_intfs:
                ptf_src_intf = self.tor_to_ptf_intf_map[vlan_intf]
                server_ip = ptf_intf_to_ip_map[ptf_src_intf]
                eth_src = self.ptf_intf_to_mac_map[ptf_src_intf]
                payload = str(i) + payload_suffix
                packet = tcp_tx_packet_orig.copy()
                packet[scapyall.Ether].src = eth_src
                packet[scapyall.IP].src = server_ip
                if self.random_dst:
                    packet[scapyall.IP].dst = self.random_host_ip()
                else:
                    packet[scapyall.IP].dst = dst_ips[vlan_intf]
                packet.load = payload
                packet[scapyall.TCP].chksum = None
                packet[scapyall.IP].chksum = None
                self.packets_list.append((ptf_src_intf, convert_scapy_packet_to_bytes(packet)))
        self.sent_pkt_dst_mac = self.vlan_mac
        self.received_pkt_src_mac = [self.active_mac, self.standby_mac]

    def _generate_upstream_packet_to_target_duthost(self, vlan_src_intf, tcp_packet):
        """Generate a packet to the target duthost."""
        packet = tcp_packet.copy()
        if self.cable_type == CableType.active_active:
            # for active-active, the upstream packet is ECMPed. So let's increase
            # the tcp source port till we get one packet that is determined to be
            # forwarded to the target duthost
            src_ptf_port_index = self.tor_to_ptf_intf_map[vlan_src_intf]

            # get the bridge that the vlan source interface is connected to
            active_active_vmhost_bridge = "baa-%s-%d" % (self.tbinfo["group-name"], src_ptf_port_index)

            # get the ptf port that is connected to the bridge
            active_active_vmhost_ptf_port = "iaa-%s-%d" % (self.tbinfo["group-name"], src_ptf_port_index)

            # get the dut ports that is connected to the bridge
            list_ports_res = self.vmhost.shell("ovs-vsctl list-ports %s" % active_active_vmhost_bridge)
            active_active_vmhost_dut_ports = [port for port in list_ports_res["stdout_lines"] if "." in port]

            # NOTE: Let's assume that the upper ToR's port is always ending with
            # smaller vlan suffix, so active_active_vmhost_dut_ports[0] is connected
            # to the upper ToR and active_active_vmhost_dut_ports[1] is connected
            # to the lower ToR.
            active_active_vmhost_dut_ports.sort()
            for dut, vmhost_dut_port in zip(self.tbinfo["duts"], active_active_vmhost_dut_ports):
                if self.duthost.hostname == dut:
                    vmhost_target_dut_port = vmhost_dut_port
                    break
            else:
                raise ValueError(
                    "Failed to find the port connected to DUT %s in bridge %s" %
                    (self.duthost.hostname, active_active_vmhost_bridge)
                )

            get_ovs_port_no_res = self.vmhost.shell("ovs-vsctl get Interface %s ofport" % vmhost_target_dut_port)
            vmhost_target_dut_port_no = get_ovs_port_no_res["stdout"].strip()

            trace_command = ("ovs-appctl ofproto/trace {bridge} in_port={port},tcp,"
                             "eth_src={eth_src},eth_dst={eth_dst},ip_src={ip_src},"
                             "ip_dst={ip_dst},tp_src={{tp_src}},tp_dst={tp_dst}").format(
                                 bridge=active_active_vmhost_bridge,
                                 port=active_active_vmhost_ptf_port,
                                 eth_src=packet[scapyall.Ether].src,
                                 eth_dst=packet[scapyall.Ether].dst,
                                 ip_src=packet[scapyall.IP].src,
                                 ip_dst=packet[scapyall.IP].dst,
                                 tp_dst=packet[scapyall.TCP].dport)
            sport_upper = min(65535, self.tcp_sport + 100)
            for tcp_sport in range(tcp_packet[scapyall.TCP].sport, sport_upper):
                trace_res = self.vmhost.shell(trace_command.format(tp_src=tcp_sport))
                if "output:%s" % vmhost_target_dut_port_no in trace_res["stdout"]:
                    packet[scapyall.TCP].sport = tcp_sport
                    self.tcp_sport = tcp_sport
                    packet[scapyall.TCP].chksum = None
                    packet[scapyall.IP].chksum = None
                    break
            else:
                raise ValueError("Failed to generate packet destinated to target DUT %s" % self.duthost.hostname)

        return packet

    def generate_server_to_server_traffic(self):
        """
        @summary: Generate (not send) the packets to be sent from server to server
        """
        logger.info("Generate server to server packets")
        if not self.tor_vlan_intf or len(self.tor_vlan_intf) != 2:
            raise ValueError("No vlan interfaces specified.")

        vlan_src_intf, vlan_dst_intf = self.tor_vlan_intf
        ptf_intf_to_ip_map = self.ptf_intf_to_server_ip_map

        logger.info("-"*20 + "server to server packet" + "-"*20)
        src_ptf_port = self.tor_to_ptf_intf_map[vlan_src_intf]
        src_mac = self.ptf_intf_to_mac_map[src_ptf_port]
        src_ip = ptf_intf_to_ip_map[src_ptf_port]
        dst_ptf_port = self.tor_to_ptf_intf_map[vlan_dst_intf]
        dst_ip = ptf_intf_to_ip_map[dst_ptf_port]
        logger.info("Ethernet address: dst: {} src: {}".format(self.vlan_mac, src_mac))
        logger.info("IP address: dst: {} src: {}".format(dst_ip, src_ip))
        logger.info("TCP port: dst: {} src: {}".format(TCP_DST_PORT, self.tcp_sport))
        logger.info("DUT ToR MAC: {}, PEER ToR MAC: {}".format(self.active_mac, self.standby_mac))
        logger.info("VLAN MAC: {}".format(self.vlan_mac))
        logger.info("-"*50)

        self.packets_list = []
        tcp_tx_packet_orig = testutils.simple_tcp_packet(
            eth_dst=self.vlan_mac,
            tcp_dport=TCP_DST_PORT,
            tcp_sport=self.tcp_sport
        )
        tcp_tx_packet_orig = scapyall.Ether(convert_scapy_packet_to_bytes(tcp_tx_packet_orig))
        tcp_tx_packet_orig[scapyall.Ether].src = src_mac
        tcp_tx_packet_orig[scapyall.IP].src = src_ip
        tcp_tx_packet_orig[scapyall.IP].dst = dst_ip
        tcp_tx_packet_orig = self._generate_upstream_packet_to_target_duthost(vlan_src_intf, tcp_tx_packet_orig)

        payload_suffix = "X" * 60
        for i in range(self.packets_per_server):
            payload = str(i) + payload_suffix
            packet = tcp_tx_packet_orig.copy()
            packet.load = payload
            packet[scapyall.TCP].chksum = None
            packet[scapyall.IP].chksum = None
            self.packets_list.append((src_ptf_port, convert_scapy_packet_to_bytes(packet)))

        self.sent_pkt_dst_mac = self.vlan_mac
        self.received_pkt_src_mac = [self.vlan_mac]

    def random_host_ip(self):
        """
        @summary: Helper method to find a random host IP for generating a random src/dst IP address
        Returns:
            host_ip (str): Random IP address
        """
        host_number = random.randint(2, self.n_hosts - 2)
        if host_number > (self.n_hosts - 2):
            raise Exception("host number {} is greater than number of hosts {}\
                in the network {}".format(
                    host_number, self.n_hosts - 2, self.default_ip_range))
        src_addr_n = struct.unpack(">I", socket.inet_aton(self.src_addr))[0]
        net_addr_n = src_addr_n & (2**32 - self.n_hosts)
        host_addr_n = net_addr_n + host_number
        host_ip = socket.inet_ntoa(struct.pack(">I", host_addr_n))

        return host_ip

    def send_and_sniff(self):
        """Start the I/O sender/sniffer."""
        try:
            self.start_sniffer()
            self.send_packets()
            self.stop_sniffer()
        except Exception:
            self.force_stop_ptf_sniffer()
            raise

        self.fetch_captured_packets()

    def _get_ptf_sniffer_status(self):
        """Get the ptf sniffer status."""
        # the output should be like
        # $ supervisorctl status dual_tor_sniffer
        # dual_tor_sniffer                 EXITED    Oct 29 01:11 PM
        stdout_text = self.ptfhost.command(
            "supervisorctl status dual_tor_sniffer", module_ignore_errors=True
        )["stdout"]
        if "no such process" in stdout_text:
            return None
        else:
            return stdout_text.split()[1]

    def _is_ptf_sniffer_running(self):
        """Check if the ptf sniffer is running."""
        status = self._get_ptf_sniffer_status()
        return ((status is not None) and ("RUNNING" in status))

    def _is_ptf_sniffer_stopped(self):
        status = self._get_ptf_sniffer_status()
        return ((status is None) or ("EXITED" in status or "STOPPED" in status))

    def start_sniffer(self):
        """Start ptf sniffer."""
        self.sniff_timeout = self.time_to_listen + self.sniff_time_incr
        self.sniffer_start = datetime.datetime.now()
        logger.info("Sniffer started at {}".format(str(self.sniffer_start)))
        self.sniff_filter = "tcp and tcp dst port {} and tcp src port {} and not icmp".\
            format(TCP_DST_PORT, self.tcp_sport)

        # We run a PTF script on PTF to sniff traffic. The PTF script calls
        # scapy.sniff which by default capture the backplane interface for
        # announcing routes from PTF to VMs. On VMs, the PTF backplane is the
        # next hop for the annoucned routes. So, packets sent by DUT to VMs
        # are forwarded to the PTF backplane interface as well. Then on PTF,
        # the packets sent by DUT to VMs can be captured on both the PTF interfaces
        # tapped to VMs and on the backplane interface. This will result in
        # packet duplication and fail the test. Below change is to add capture
        # filter to filter out all the packets destined to the PTF backplane interface.
        output = self.ptfhost.shell('cat /sys/class/net/backplane/address',
                                    module_ignore_errors=True)
        if not output['failed']:
            ptf_bp_mac = output['stdout']
            self.sniff_filter = '({}) and (not ether dst {})'.format(self.sniff_filter, ptf_bp_mac)

        self.capture_pcap = '/tmp/capture.pcap'
        self.capture_log = '/tmp/capture.log'

        # Do some cleanup first
        self.ptfhost.file(path=self.capture_pcap, state="absent")
        if os.path.exists(self.capture_pcap):
            os.unlink(self.capture_pcap)

        self.setup_ptf_sniffer()
        self.start_ptf_sniffer()

        # Let the scapy sniff initialize completely.
        if not wait_until(20, 5, 10, self._is_ptf_sniffer_running):
            self.stop_sniffer()
            raise RuntimeError("Could not start ptf sniffer.")

    def stop_sniffer(self):
        """Stop the ptf sniffer."""
        if self._is_ptf_sniffer_running():
            self.stop_ptf_sniffer()

        # The pcap write might take some time, add some waiting here.
        if not wait_until(30, 5, 0, self._is_ptf_sniffer_stopped):
            raise RuntimeError("Could not stop ptf sniffer.")
        logger.info("Sniffer finished running after {}".
                    format(str(datetime.datetime.now() - self.sniffer_start)))

    def fetch_captured_packets(self):
        """Fetch the captured packet file generated by the ptf sniffer."""
        logger.info('Fetching pcap file from ptf')
        self.ptfhost.fetch(src=self.capture_pcap, dest='/tmp/', flat=True, fail_on_missing=False)
        self.all_packets = scapyall.rdpcap(self.capture_pcap)
        logger.info("Number of all packets captured: {}".format(len(self.all_packets)))

    def send_packets(self):
        """Send packets generated."""
        logger.info("Sender waiting to send {} packets".format(len(self.packets_list)))

        sender_start = datetime.datetime.now()
        logger.info("Sender started at {}".format(str(sender_start)))

        # Signal data_plane_utils that sender and sniffer threads have begun
        self.io_ready_event.set()

        sent_packets_count = 0
        for entry in self.packets_list:
            _, packet = entry
            server_addr = self.get_server_address(scapyall.Ether(convert_scapy_packet_to_bytes(packet)))
            time.sleep(self.send_interval)
            # the stop_early flag can be set to True by data_plane_utils to stop prematurely
            if self.stop_early:
                break
            testutils.send_packet(self.ptfadapter, *entry)
            self.packets_sent_per_server[server_addr] =\
                self.packets_sent_per_server.get(server_addr, 0) + 1
            sent_packets_count = sent_packets_count + 1

        # wait 10s so all packets could be forwarded
        time.sleep(10)
        logger.info(
            "Sender finished running after %s, %s packets sent",
            datetime.datetime.now() - sender_start,
            sent_packets_count
        )
        if not self._is_ptf_sniffer_running():
            raise RuntimeError("ptf sniffer is not running enough time to cover packets sending.")

    def get_server_address(self, packet):
        if self.traffic_direction == "t1_to_server":
            server_addr = packet[scapyall.IP].dst
        elif self.traffic_direction == "server_to_t1":
            server_addr = packet[scapyall.IP].src
        elif self.traffic_direction == "t1_to_soc":
            server_addr = packet[scapyall.IP].dst
        elif self.traffic_direction == "soc_to_t1":
            server_addr = packet[scapyall.IP].src
        elif self.traffic_direction == "server_to_server":
            server_addr = packet[scapyall.IP].src
        return server_addr

    def get_test_results(self):
        return self.test_results

    def examine_flow(self):
        """
        @summary: This method examines packets collected by sniffer thread
            The method compares TCP payloads of the packets one by one (assuming all
            payloads are consecutive integers), and the losses if found - are treated
            as disruptions in Dataplane forwarding. All disruptions are saved to
            self.lost_packets dictionary, in format:
            disrupt_start_id = (missing_packets_count, disrupt_time,
            disrupt_start_timestamp, disrupt_stop_timestamp)
        """
        examine_start = datetime.datetime.now()
        logger.info("Packet flow examine started {}".format(str(examine_start)))

        if not self.all_packets:
            logger.error("self.all_packets not defined.")
            return None

        # Filter out packets:
        filtered_packets = [pkt for pkt in self.all_packets if
                            scapyall.TCP in pkt and
                            scapyall.ICMP not in pkt and
                            pkt[scapyall.TCP].sport == self.tcp_sport and
                            pkt[scapyall.TCP].dport == TCP_DST_PORT and
                            self.check_tcp_payload(pkt) and
                            (
                                pkt[scapyall.Ether].dst == self.sent_pkt_dst_mac or
                                pkt[scapyall.Ether].src in self.received_pkt_src_mac
                            )]
        logger.info("Number of filtered packets captured: {}".format(len(filtered_packets)))
        if not filtered_packets or len(filtered_packets) == 0:
            logger.error("Sniffer failed to capture any traffic")

        server_to_packet_map = defaultdict(list)

        # Split packets into separate lists based on server IP
        for packet in filtered_packets:
            server_addr = self.get_server_address(packet)
            server_to_packet_map[server_addr].append(packet)

        # E731 Use a def instead of a lambda
        def get_packet_sort_key(packet):
            payload_bytes = convert_scapy_packet_to_bytes(packet[scapyall.TCP].payload)
            if six.PY2:
                payload_int = int(payload_bytes.replace('X', ''))
            else:
                payload_int = int(payload_bytes.decode().replace('X', ''))
            return (payload_int, packet.time)

        # For each server's packet list, sort by payload then timestamp
        # (in case of duplicates)
        for server in list(server_to_packet_map.keys()):
            server_to_packet_map[server].sort(key=get_packet_sort_key)

        logger.info("Measuring traffic disruptions...")
        for server_ip, packet_list in list(server_to_packet_map.items()):
            filename = '/tmp/capture_filtered_{}.pcap'.format(server_ip)
            scapyall.wrpcap(filename, packet_list)
            logger.info("Filtered pcap dumped to {}".format(filename))

        self.test_results = {}

        for server_ip in natsorted(list(server_to_packet_map.keys())):
            result = self.examine_each_packet(server_ip, server_to_packet_map[server_ip])
            logger.info("Server {} results:\n{}"
                        .format(server_ip, json.dumps(result, indent=4)))
            self.test_results[server_ip] = result

    def examine_each_packet(self, server_ip, packets):
        num_sent_packets = 0
        received_packet_list = list()
        duplicate_packet_list = list()
        disruption_ranges = list()
        disruption_before_traffic = False
        disruption_after_traffic = False
        duplicate_ranges = []

        for packet in packets:
            if packet[scapyall.Ether].dst == self.sent_pkt_dst_mac:
                # This is a sent packet
                num_sent_packets += 1
                continue
            if packet[scapyall.Ether].src in self.received_pkt_src_mac:
                # This is a received packet.
                # scapy 2.4.5 will use Decimal to calulcate time, but json.dumps
                # can't recognize Decimal, transform to float here
                curr_time = float(packet.time)
                curr_payload_bytes = convert_scapy_packet_to_bytes(packet[scapyall.TCP].payload)
                if six.PY2:
                    curr_payload = int(curr_payload_bytes.replace('X', ''))
                else:
                    curr_payload = int(curr_payload_bytes.decode().replace('X', ''))

                # Look back at the previous received packet to check for gaps/duplicates
                # Only if we've already received some packets
                if len(received_packet_list) > 0:
                    prev_payload, prev_time = received_packet_list[-1]

                    if prev_payload == curr_payload:
                        # Duplicate packet detected, increment the counter
                        duplicate_packet_list.append((curr_payload, curr_time))
                    if prev_payload + 1 < curr_payload:
                        # Non-sequential packets indicate a disruption
                        disruption_dict = {
                            'start_time': prev_time,
                            'end_time': curr_time,
                            'start_id': prev_payload,
                            'end_id': curr_payload
                        }
                        disruption_ranges.append(disruption_dict)

                # Save packets as (payload_id, timestamp) tuples
                # for easier timing calculations later
                received_packet_list.append((curr_payload, curr_time))

        if len(received_packet_list) == 0:
            logger.error("Sniffer failed to filter any traffic from DUT")
        else:
            # Find ranges of consecutive packets that have been duplicated
            # All consecutive packets with the same payload will be grouped as one
            # duplication group.
            # For example, for the duplication list as the following:
            # [(70, 1744253633.499116), (70, 1744253633.499151), (70, 1744253633.499186),
            #  (81, 1744253635.49922), (81, 1744253635.499255)]
            # two duplications will be reported:
            # "duplications": [
            #     {
            #         "start_time": 1744253633.499116,
            #         "end_time": 1744253633.499186,
            #         "start_id": 70,
            #         "end_id": 70,
            #         "duplication_count": 3
            #     },
            #     {
            #         "start_time": 1744253635.49922,
            #         "end_time": 1744253635.499255,
            #         "start_id": 81,
            #         "end_id": 81,
            #         "duplication_count": 2
            #     }
            # ]
            for _, grouper in groupby(duplicate_packet_list, lambda d: d[0]):
                duplicates = list(grouper)
                duplicate_start, duplicate_end = duplicates[0], duplicates[-1]
                duplicate_dict = {
                    'start_time': duplicate_start[1],
                    'end_time': duplicate_end[1],
                    'start_id': duplicate_start[0],
                    'end_id': duplicate_end[0],
                    'duplication_count': len(duplicates)
                }
                duplicate_ranges.append(duplicate_dict)

            # If the first packet we received is not #0, some disruption started
            # before traffic started. Store the id of the first received packet
            if received_packet_list[0][0] != 0:
                disruption_before_traffic = received_packet_list[0][0]
            # If the last packet we received does not match the number of packets
            # sent, some disruption continued after the traffic finished.
            # Store the id of the last received packet
            if received_packet_list[-1][0] != self.packets_sent_per_server.get(server_ip) - 1:
                disruption_after_traffic = received_packet_list[-1][0]

        result = {
            'sent_packets': num_sent_packets,
            'received_packets': len(received_packet_list),
            'disruption_before_traffic': disruption_before_traffic,
            'disruption_after_traffic': disruption_after_traffic,
            'duplications': duplicate_ranges,
            'disruptions': disruption_ranges
        }

        if num_sent_packets < self.packets_sent_per_server.get(server_ip):
            server_addr = self.get_server_address(packet)
            logger.error('Not all sent packets were captured. '
                         'Something went wrong!')
            logger.error('Dumping server {} results and continuing:\n{}'
                         .format(server_addr, json.dumps(result, indent=4)))

        return result

    def check_tcp_payload(self, packet):
        """
        @summary: Helper method

        Returns: Bool: True if a packet is not corrupted and has a valid TCP
            sequential TCP Payload
        """
        try:
            payload_bytes = convert_scapy_packet_to_bytes(packet[scapyall.TCP].payload)
            if six.PY2:
                int(payload_bytes.replace('X', '')) in range(
                    self.packets_to_send)
            else:
                int(payload_bytes.decode().replace('X', '')) in range(
                    self.packets_to_send)
            return True
        except Exception:
            return False
