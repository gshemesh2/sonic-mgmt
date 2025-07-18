import logging
import pytest
import random
import time

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import skip_release
from tests.common.platform.transceiver_utils import parse_sfp_eeprom_infos
from tests.platform_tests.sfp.software_control.helpers import check_sc_sai_attribute_value
from tests.common.utilities import wait_until

pytestmark = [
    pytest.mark.disable_loganalyzer,  # disable automatic loganalyzer
    pytest.mark.topology('any')
]

SUPPORTED_PLATFORMS = ["arista_7060x6", "nvidia_sn5640", "nvidia_sn5600"]
cmd_sfp_presence = "sudo sfpshow presence"


class TestMACFault(object):
    @pytest.fixture(autouse=True)
    def is_supported_platform(self, duthost, tbinfo):
        if 'ptp' not in tbinfo['topo']['name']:
            pytest.skip("Skipping test: Not applicable for non-PTP topology")

        if any(platform in duthost.facts['platform'] for platform in SUPPORTED_PLATFORMS):
            skip_release(duthost, ["201811", "201911", "202012", "202205", "202211", "202305", "202405"])
        else:
            pytest.skip("DUT has platform {}, test is not supported".format(duthost.facts['platform']))

        if not check_sc_sai_attribute_value(duthost):
            pytest.skip("SW control feature is not enabled")

    @staticmethod
    def get_mac_fault_count(dut, interface, fault_type):
        output = dut.show_and_parse("show int errors {}".format(interface))
        logging.info("Raw output for show int errors on {}: {}".format(interface, output))

        fault_count = 0
        for error_info in output:
            if error_info['port errors'] == fault_type:
                fault_count = int(error_info['count'])
                break

        logging.info("{} count on {}: {}".format(fault_type, interface, fault_count))
        return fault_count

    @staticmethod
    def get_interface_status(dut, interface):
        return dut.show_and_parse("show interfaces status {}".format(interface))[0].get("oper", "unknown")

    # TODO: Reboot the switch before test as a WA of #22205, remove this after the issue is fixed
    @pytest.fixture(scope="class", autouse=True)
    def reboot_dut(self, duthosts, localhost, enum_rand_one_per_hwsku_frontend_hostname):
        from tests.common.reboot import reboot
        reboot(duthosts[enum_rand_one_per_hwsku_frontend_hostname],
               localhost, safe_reboot=True, check_intf_up_ports=True)

    @pytest.fixture(scope="class")
    def select_random_interfaces(self, duthosts, enum_rand_one_per_hwsku_frontend_hostname):
        dut = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

        sfp_presence = dut.command(cmd_sfp_presence)
        parsed_presence = {line.split()[0]: line.split()[1] for line in sfp_presence["stdout_lines"][2:]}

        eeprom_infos = dut.shell("sudo sfputil show eeprom -d")['stdout']
        eeprom_infos = parse_sfp_eeprom_infos(eeprom_infos)

        available_optical_interfaces = []
        for port_name, eeprom_info in eeprom_infos.items():
            if parsed_presence.get(port_name) == "Present" and \
                    "SFP EEPROM detected" in eeprom_info[port_name] \
                    and "COPPER" not in eeprom_info.get("Media Interface Technology", "COPPER").upper():
                try:
                    is_cmis_supported = float(eeprom_info.get("CMIS Revision", "0")) >= 5.0
                except ValueError:
                    is_cmis_supported = False

                if not is_cmis_supported:
                    logging.info(f"Port {port_name} skipped: CMIS not supported on this port.")
                    continue
                available_optical_interfaces.append(port_name)

        pytest_assert(available_optical_interfaces, "No interfaces with SFP detected. Cannot proceed with tests.")
        logging.info("Available Optical interfaces for tests: {}".format(available_optical_interfaces))

        # Select 5 random interfaces (or fewer if not enough available)
        selected_interfaces = random.sample(available_optical_interfaces, min(5, len(available_optical_interfaces)))
        logging.info("Selected interfaces for tests: {}".format(selected_interfaces))

        return dut, selected_interfaces

    def shutdown_and_startup_interfaces(self, dut, interface):
        dut.command("sudo config interface shutdown {}".format(interface))
        pytest_assert(wait_until(30, 2, 0, lambda: self.get_interface_status(dut, interface) == "down"),
                     "Interface {} did not go down after shutdown".format(interface))

        dut.command("sudo config interface startup {}".format(interface))
        pytest_assert(wait_until(30, 2, 0, lambda: self.get_interface_status(dut, interface) == "up"),
                     "Interface {} did not come up after startup".format(interface))

    def test_mac_local_fault_increment(self, select_random_interfaces):
        dut, interfaces = select_random_interfaces

        for interface in interfaces:
            self.shutdown_and_startup_interfaces(dut, interface)

            pytest_assert(self.get_interface_status(dut, interface) == "up",
                          "Interface {} was not up before disabling/enabling rx-output using sfputil".format(interface))

            local_fault_before = self.get_mac_fault_count(dut, interface, "mac local fault")
            logging.info("Initial MAC local fault count on {}: {}".format(interface, local_fault_before))

            dut.shell("sudo sfputil debug rx-output {} disable".format(interface))
            time.sleep(5)
            pytest_assert(self.get_interface_status(dut, interface) == "down",
                          "Interface {iface} did not go down after 'sudo sfputil debug rx-output {iface} disable'"
                          .format(iface=interface))

            dut.shell("sudo sfputil debug rx-output {} enable".format(interface))
            time.sleep(20)
            pytest_assert(self.get_interface_status(dut, interface) == "up",
                          "Interface {iface} did not come up after 'sudo sfputil debug rx-output {iface} enable'"
                          .format(iface=interface))

            local_fault_after = self.get_mac_fault_count(dut, interface, "mac local fault")
            logging.info("MAC local fault count after disabling/enabling rx-output using sfputil {}: {}".format(
                interface, local_fault_after))

            pytest_assert(local_fault_after > local_fault_before,
                          "MAC local fault count did not increment after disabling/enabling rx-output on the device")

    def test_mac_remote_fault_increment(self, select_random_interfaces):
        dut, interfaces = select_random_interfaces

        for interface in interfaces:
            self.shutdown_and_startup_interfaces(dut, interface)

            pytest_assert(self.get_interface_status(dut, interface) == "up",
                          "Interface {} was not up before disabling/enabling tx-output using sfputil".format(interface))

            remote_fault_before = self.get_mac_fault_count(dut, interface, "mac remote fault")
            logging.info("Initial MAC remote fault count on {}: {}".format(interface, remote_fault_before))

            dut.shell("sudo sfputil debug tx-output {} disable".format(interface))
            time.sleep(5)
            pytest_assert(self.get_interface_status(dut, interface) == "down",
                          "Interface {iface} did not go down after 'sudo sfputil debug tx-output {iface} disable'"
                          .format(iface=interface))

            dut.shell("sudo sfputil debug tx-output {} enable".format(interface))
            time.sleep(20)

            pytest_assert(self.get_interface_status(dut, interface) == "up",
                          "Interface {iface} did not come up after 'sudo sfputil debug tx-output {iface} enable'"
                          .format(iface=interface))

            remote_fault_after = self.get_mac_fault_count(dut, interface, "mac remote fault")
            logging.info("MAC remote fault count after disabling/enabling tx-output using sfputil {}: {}".format(
                interface, remote_fault_after))

            pytest_assert(remote_fault_after > remote_fault_before,
                          "MAC remote fault count did not increment after disabling/enabling tx-output on the device")
