# Copyright 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import binascii
import functools
import os
import shlex
import time


import netifaces
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log
from oslo_utils import strutils
from oslo_utils import units
import pint
import psutil
import pyudev
import six
import stevedore

from ironic_python_agent import encoding
from ironic_python_agent import errors
from ironic_python_agent import netutils
from ironic_python_agent import utils

_global_managers = None
LOG = log.getLogger()
CONF = cfg.CONF

UNIT_CONVERTER = pint.UnitRegistry(filename=None)
UNIT_CONVERTER.define('MB = []')
UNIT_CONVERTER.define('GB = 1024 MB')

NODE = None


def _get_device_vendor(dev):
    """Get the vendor name of a given device."""
    try:
        devname = os.path.basename(dev)
        with open('/sys/class/block/%s/device/vendor' % devname, 'r') as f:
            return f.read().strip()
    except IOError:
        LOG.warning("Can't find the device vendor for device %s", dev)


def _udev_settle():
    """Wait for the udev event queue to settle.

    Wait for the udev event queue to settle to make sure all devices
    are detected once the machine boots up.

    """
    try:
        utils.execute('udevadm', 'settle')
    except processutils.ProcessExecutionError as e:
        LOG.warning('Something went wrong when waiting for udev '
                    'to settle. Error: %s', e)
        return


def _check_for_iscsi():
    """Connect iSCSI shared connected via iBFT or OF.

    iscsistart -f will print the iBFT or OF info.
    In case such connection exists, we would like to issue
    iscsistart -b to create a session to the target.
    - If no connection is detected we simply return.
    """
    try:
        utils.execute('iscsistart', '-f')
    except (processutils.ProcessExecutionError, EnvironmentError) as e:
        LOG.debug("No iscsi connection detected. Skipping iscsi. "
                  "Error: %s", e)
        return
    try:
        utils.execute('iscsistart', '-b')
    except processutils.ProcessExecutionError as e:
        LOG.warning("Something went wrong executing 'iscsistart -b' "
                    "Error: %s", e)


def list_all_block_devices(block_type='disk'):
    """List all physical block devices

    The switches we use for lsblk: P for KEY="value" output, b for size output
    in bytes, d to exclude dependent devices (like md or dm devices), i to
    ensure ascii characters only, and o to specify the fields/columns we need.

    Broken out as its own function to facilitate custom hardware managers that
    don't need to subclass GenericHardwareManager.

    :param block_type: Type of block device to find
    :return: A list of BlockDevices
    """
    _udev_settle()

    columns = ['KNAME', 'MODEL', 'SIZE', 'ROTA', 'TYPE']
    report = utils.execute('lsblk', '-Pbdi', '-o{}'.format(','.join(columns)),
                           check_exit_code=[0])[0]
    lines = report.split('\n')
    context = pyudev.Context()

    devices = []
    for line in lines:
        device = {}
        # Split into KEY=VAL pairs
        vals = shlex.split(line)
        for key, val in (v.split('=', 1) for v in vals):
            device[key] = val.strip()
        # Ignore block types not specified
        if device.get('TYPE') != block_type:
            LOG.debug(
                "TYPE did not match. Wanted: {!r} but found: {!r}".format(
                    block_type, line))
            continue

        # Ensure all required columns are at least present, even if blank
        missing = set(columns) - set(device)
        if missing:
            raise errors.BlockDeviceError(
                '%s must be returned by lsblk.' % ', '.join(sorted(missing)))

        name = '/dev/' + device['KNAME']
        try:
            udev = pyudev.Device.from_device_file(context, name)
        # pyudev started raising another error in 0.18
        except (ValueError, EnvironmentError, pyudev.DeviceNotFoundError) as e:
            LOG.warning("Device %(dev)s is inaccessible, skipping... "
                        "Error: %(error)s", {'dev': name, 'error': e})
            extra = {}
        else:
            # TODO(lucasagomes): Since lsblk only supports
            # returning the short serial we are using
            # ID_SERIAL_SHORT here to keep compatibility with the
            # bash deploy ramdisk
            extra = {key: udev.get('ID_%s' % udev_key) for key, udev_key in
                     [('wwn', 'WWN'), ('serial', 'SERIAL_SHORT'),
                      ('wwn_with_extension', 'WWN_WITH_EXTENSION'),
                      ('wwn_vendor_extension', 'WWN_VENDOR_EXTENSION')]}

        devices.append(BlockDevice(name=name,
                                   model=device['MODEL'],
                                   size=int(device['SIZE']),
                                   rotational=bool(int(device['ROTA'])),
                                   vendor=_get_device_vendor(device['KNAME']),
                                   **extra))
    return devices


class HardwareSupport(object):
    """Example priorities for hardware managers.

    Priorities for HardwareManagers are integers, where largest means most
    specific and smallest means most generic. These values are guidelines
    that suggest values that might be returned by calls to
    `evaluate_hardware_support()`. No HardwareManager in mainline IPA will
    ever return a value greater than MAINLINE. Third party hardware managers
    should feel free to return values of SERVICE_PROVIDER or greater to
    distinguish between additional levels of hardware support.
    """
    NONE = 0
    GENERIC = 1
    MAINLINE = 2
    SERVICE_PROVIDER = 3


class HardwareType(object):
    MAC_ADDRESS = 'mac_address'


class BlockDevice(encoding.SerializableComparable):
    serializable_fields = ('name', 'model', 'size', 'rotational',
                           'wwn', 'serial', 'vendor', 'wwn_with_extension',
                           'wwn_vendor_extension')

    def __init__(self, name, model, size, rotational, wwn=None, serial=None,
                 vendor=None, wwn_with_extension=None,
                 wwn_vendor_extension=None):
        self.name = name
        self.model = model
        self.size = size
        self.rotational = rotational
        self.wwn = wwn
        self.serial = serial
        self.vendor = vendor
        self.wwn_with_extension = wwn_with_extension
        self.wwn_vendor_extension = wwn_vendor_extension


class NetworkInterface(encoding.SerializableComparable):
    serializable_fields = ('name', 'mac_address', 'switch_port_descr',
                           'switch_chassis_descr', 'ipv4_address',
                           'has_carrier', 'lldp')

    def __init__(self, name, mac_addr, ipv4_address=None, has_carrier=True,
                 lldp=None):
        self.name = name
        self.mac_address = mac_addr
        self.ipv4_address = ipv4_address
        self.has_carrier = has_carrier
        self.lldp = lldp
        # TODO(sambetts) Remove these fields in Ocata, they have been
        # superseded by self.lldp
        self.switch_port_descr = None
        self.switch_chassis_descr = None


class CPU(encoding.SerializableComparable):
    serializable_fields = ('model_name', 'frequency', 'count', 'architecture',
                           'flags')

    def __init__(self, model_name, frequency, count, architecture,
                 flags=None):
        self.model_name = model_name
        self.frequency = frequency
        self.count = count
        self.architecture = architecture
        self.flags = flags or []


class Memory(encoding.SerializableComparable):
    serializable_fields = ('total', 'physical_mb')
    # physical = total + kernel binary + reserved space

    def __init__(self, total, physical_mb=None):
        self.total = total
        self.physical_mb = physical_mb


class CPUInfo(encoding.SerializableComparable):
    serializable_fields = ('version', 'cpu_count', 'core_count', 'thread_count')

    def __init__(self, version, cpu_count, core_count, thread_count):
        self.version = version
        self.cpu_count = cpu_count
        self.core_count = core_count
        self.thread_count = thread_count


class SystemVendorInfo(encoding.SerializableComparable):
    serializable_fields = ('product_name', 'serial_number', 'manufacturer', 'asset_tag')

    def __init__(self, product_name, serial_number, manufacturer, asset_tag):
        self.product_name = product_name
        self.serial_number = serial_number
        self.manufacturer = manufacturer
        self.asset_tag = asset_tag


class BootInfo(encoding.SerializableComparable):
    serializable_fields = ('current_boot_mode', 'pxe_interface')

    def __init__(self, current_boot_mode, pxe_interface=None):
        self.current_boot_mode = current_boot_mode
        self.pxe_interface = pxe_interface


@six.add_metaclass(abc.ABCMeta)
class HardwareManager(object):
    @abc.abstractmethod
    def evaluate_hardware_support(self):
        pass

    def list_network_interfaces(self):
        raise errors.IncompatibleHardwareMethodError

    def get_cpus(self):
        raise errors.IncompatibleHardwareMethodError

    def list_block_devices(self):
        raise errors.IncompatibleHardwareMethodError

    def get_memory(self):
        raise errors.IncompatibleHardwareMethodError

    def get_cpu_info(self):
        raise errors.IncompatibleHardwareMethodError

    def get_os_install_device(self):
        raise errors.IncompatibleHardwareMethodError

    def get_bmc_address(self):
        raise errors.IncompatibleHardwareMethodError()

    def get_boot_info(self):
        raise errors.IncompatibleHardwareMethodError()

    def erase_block_device(self, node, block_device):
        """Attempt to erase a block device.

        Implementations should detect the type of device and erase it in the
        most appropriate way possible.  Generic implementations should support
        common erase mechanisms such as ATA secure erase, or multi-pass random
        writes. Operators with more specific needs should override this method
        in order to detect and handle "interesting" cases, or delegate to the
        parent class to handle generic cases.

        For example: operators running ACME MagicStore (TM) cards alongside
        standard SSDs might check whether the device is a MagicStore and use a
        proprietary tool to erase that, otherwise call this method on their
        parent class. Upstream submissions of common functionality are
        encouraged.

        :param node: Ironic node object
        :param block_device: a BlockDevice indicating a device to be erased.
        :raises IncompatibleHardwareMethodError: when there is no known way to
                erase the block device
        :raises BlockDeviceEraseError: when there is an error erasing the
                block device
        """
        raise errors.IncompatibleHardwareMethodError

    def erase_devices(self, node, ports):
        """Erase any device that holds user data.

        By default this will attempt to erase block devices. This method can be
        overridden in an implementation-specific hardware manager in order to
        erase additional hardware, although backwards-compatible upstream
        submissions are encouraged.

        :param node: Ironic node object
        :param ports: list of Ironic port objects
        :return: a dictionary in the form {device.name: erasure output}
        """
        erase_results = {}
        block_devices = self.list_block_devices()
        for block_device in block_devices:
            result = dispatch_to_managers(
                'erase_block_device', node=node, block_device=block_device)
            erase_results[block_device.name] = result
        return erase_results

    def list_hardware_info(self):
        """Return full hardware inventory as a serializable dict.

        This inventory is sent to Ironic on lookup and to Inspector on
        inspection.

        :return: a dictionary representing inventory
        """
        # NOTE(dtantsur): don't forget to update docs when extending inventory
        hardware_info = {}
        hardware_info['interfaces'] = self.list_network_interfaces()
        hardware_info['cpu'] = self.get_cpus()
        hardware_info['disks'] = self.list_block_devices()
        hardware_info['memory'] = self.get_memory()
        hardware_info['bmc_address'] = self.get_bmc_address()
        hardware_info['system_vendor'] = self.get_system_vendor_info()
        hardware_info['boot'] = self.get_boot_info()
        hardware_info['cpu_info'] = self.get_cpu_info()
        return hardware_info

    def get_clean_steps(self, node, ports):
        """Get a list of clean steps with priority.

        Returns a list of steps. Each step is represented by a dict::

          {
           'step': the HardwareManager function to call.
           'priority': the order steps will be run in. Ironic will sort all
                       the clean steps from all the drivers, with the largest
                       priority step being run first. If priority is set to 0,
                       the step will not be run during cleaning, but may be
                       run during zapping.
           'reboot_requested': Whether the agent should request Ironic reboots
                               the node via the power driver after the
                               operation completes.
           'abortable': Boolean value. Whether the clean step can be
                        stopped by the operator or not. Some clean step may
                        cause non-reversible damage to a machine if interrupted
                        (i.e firmware update), for such steps this parameter
                        should be set to False. If no value is set for this
                        parameter, Ironic will consider False (non-abortable).
          }


        If multiple hardware managers return the same step name, the following
        logic will be used to determine which manager's step "wins":

            * Keep the step that belongs to HardwareManager with highest
              HardwareSupport (larger int) value.
            * If equal support level, keep the step with the higher defined
              priority (larger int).
            * If equal support level and priority, keep the step associated
              with the HardwareManager whose name comes earlier in the
              alphabet.

        The steps will be called using `hardware.dispatch_to_managers` and
        handled by the best suited hardware manager. If you need a step to be
        executed by only your hardware manager, ensure it has a unique step
        name.

        `node` and `ports` can be used by other hardware managers to further
        determine if a clean step is supported for the node.

        :param node: Ironic node object
        :param ports: list of Ironic port objects
        :return: a list of cleaning steps, where each step is described as a
                 dict as defined above

        """
        return [
            {
                'step': 'erase_devices',
                'priority': 10,
                'interface': 'deploy',
                'reboot_requested': False,
                'abortable': True
            }
        ]

    def get_version(self):
        """Get a name and version for this hardware manager.

        In order to avoid errors and make agent upgrades painless, cleaning
        will check the version of all hardware managers during get_clean_steps
        at the beginning of cleaning and before executing each step in the
        agent.

        The agent isn't aware of the steps being taken before or after via
        out of band steps, so it can never know if a new step is safe to run.
        Therefore, we default to restarting the whole process.

        :returns: a dictionary with two keys: `name` and
            `version`, where `name` is a string identifying the hardware
            manager and `version` is an arbitrary version string. `name` will
            be a class variable called HARDWARE_MANAGER_NAME, or default to
            the class name and `version` will be a class variable called
            HARDWARE_MANAGER_VERSION or default to '1.0'.
        """
        return {
            'name': getattr(self, 'HARDWARE_MANAGER_NAME',
                            type(self).__name__),
            'version': getattr(self, 'HARDWARE_MANAGER_VERSION', '1.0')
        }


class GenericHardwareManager(HardwareManager):
    HARDWARE_MANAGER_NAME = 'generic_hardware_manager'
    HARDWARE_MANAGER_VERSION = '1.0'

    def __init__(self):
        self.sys_path = '/sys'
        self.lldp_data = {}

    def evaluate_hardware_support(self):
        # Do some initialization before we declare ourself ready
        _check_for_iscsi()
        self._wait_for_disks()
        return HardwareSupport.GENERIC

    def _wait_for_disks(self):
        """Wait for disk to appear

        Wait for at least one suitable disk to show up, otherwise neither
        inspection not deployment have any chances to succeed.

        """

        for attempt in range(CONF.disk_wait_attempts):
            try:
                block_devices = self.list_block_devices()
                utils.guess_root_disk(block_devices)
            except errors.DeviceNotFound:
                LOG.debug('Still waiting for at least one disk to appear, '
                          'attempt %d of %d', attempt + 1,
                          CONF.disk_wait_attempts)
                time.sleep(CONF.disk_wait_delay)
            else:
                break
        else:
            LOG.warning('No disks detected in %d seconds',
                        CONF.disk_wait_delay * CONF.disk_wait_attempts)

    def _cache_lldp_data(self, interface_names):
        interface_names = [name for name in interface_names if name != 'lo']
        try:
            raw_lldp_data = netutils.get_lldp_info(interface_names)
        except Exception:
            # NOTE(sambetts) The get_lldp_info function will log this exception
            # and we don't invalidate any existing data in the cache if we fail
            # to get data to replace it so just return.
            return
        for ifname, tlvs in raw_lldp_data.items():
            # NOTE(sambetts) Convert each type-length-value (TLV) value to hex
            # so that it can be serialised safely
            processed_tlvs = []
            for typ, data in tlvs:
                try:
                    processed_tlvs.append((typ,
                                           binascii.hexlify(data).decode()))
                except (binascii.Error, binascii.Incomplete) as e:
                    LOG.warning('An error occurred while processing TLV type '
                                '%s for interface %s: %s', (typ, ifname, e))
            self.lldp_data[ifname] = processed_tlvs

    def _get_lldp_data(self, interface_name):
        if self.lldp_data:
            return self.lldp_data.get(interface_name)

    def _get_interface_info(self, interface_name):
        addr_path = '{0}/class/net/{1}/address'.format(self.sys_path,
                                                       interface_name)
        with open(addr_path) as addr_file:
            mac_addr = addr_file.read().strip()

        return NetworkInterface(
            interface_name, mac_addr,
            ipv4_address=self.get_ipv4_addr(interface_name),
            has_carrier=self._interface_has_carrier(interface_name),
            lldp=self._get_lldp_data(interface_name))

    def get_ipv4_addr(self, interface_id):
        try:
            addrs = netifaces.ifaddresses(interface_id)
            return addrs[netifaces.AF_INET][0]['addr']
        except (ValueError, IndexError, KeyError):
            # No default IPv4 address found
            return None

    def _interface_has_carrier(self, interface_name):
        path = '{0}/class/net/{1}/carrier'.format(self.sys_path,
                                                  interface_name)
        try:
            with open(path, 'rt') as fp:
                return fp.read().strip() == '1'
        except EnvironmentError:
            LOG.debug('No carrier information for interface %s',
                      interface_name)
            return False

    def _is_device(self, interface_name):
        device_path = '{0}/class/net/{1}/device'.format(self.sys_path,
                                                        interface_name)
        return os.path.exists(device_path)

    def list_network_interfaces(self):
        iface_names = os.listdir('{0}/class/net'.format(self.sys_path))
        iface_names = [name for name in iface_names if self._is_device(name)]

        if CONF.collect_lldp:
            self._cache_lldp_data(iface_names)

        return [self._get_interface_info(name) for name in iface_names]

    def get_cpus(self):
        lines = utils.execute('lscpu')[0]
        cpu_info = {k.strip().lower(): v.strip() for k, v in
                    (line.split(':', 1)
                     for line in lines.split('\n')
                     if line.strip())}
        # Current CPU frequency can be different from maximum one on modern
        # processors
        freq = cpu_info.get('cpu max mhz', cpu_info.get('cpu mhz'))

        flags = []
        out = utils.try_execute('grep', '-Em1', '^flags', '/proc/cpuinfo')
        if out:
            try:
                # Example output (much longer for a real system):
                # flags           : fpu vme de pse
                flags = out[0].strip().split(':', 1)[1].strip().split()
            except (IndexError, ValueError):
                LOG.warning('Malformed CPU flags information: %s', out)
        else:
            LOG.warning('Failed to get CPU flags')

        return CPU(model_name=cpu_info.get('model name'),
                   frequency=freq,
                   # this includes hyperthreading cores
                   count=int(cpu_info.get('cpu(s)')),
                   architecture=cpu_info.get('architecture'),
                   flags=flags)

    def get_memory(self):
        # psutil returns a long, so we force it to an int
        if psutil.version_info[0] == 1:
            total = int(psutil.TOTAL_PHYMEM)
        elif psutil.version_info[0] == 2:
            total = int(psutil.phymem_usage().total)

        try:
            out, _e = utils.execute("dmidecode --type 17 | grep Size",
                                    shell=True)
        except (processutils.ProcessExecutionError, OSError) as e:
            LOG.warning("Cannot get real physical memory size: %s", e)
            physical = None
        else:
            physical = 0
            for line in out.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue

                if 'Size:' not in line:
                    continue

                value = None
                try:
                    value = line.split('Size: ', 1)[1]
                    physical += int(UNIT_CONVERTER(value).to_base_units())
                except Exception as exc:
                    if (value == "No Module Installed" or
                            value == "Not Installed"):
                        LOG.debug('One memory slot is empty')
                    else:
                        LOG.error('Cannot parse size expression %s: %s',
                                  line, exc)

            if not physical:
                LOG.warning('failed to get real physical RAM, dmidecode '
                            'returned %s', out)

        return Memory(total=total, physical_mb=physical)

    def get_cpu_info(self):
        try:
            out, _e = utils.execute("dmidecode --type processor | grep 'Processor Information'",
                                    shell=True)
        except (processutils.ProcessExecutionError, OSError) as e:
            LOG.warning("Cannot get cpu count info: %s", e)
            cpu_count = 0
        else:
            cpu_count = len(out.strip().split('\n'))

        version = ''
        out, _e = utils.try_execute("dmidecode --type processor | grep Version", shell=True)
        if out:
            try:
                for line in out.strip().split('\n'):
                    line = line.strip()
                    version = line.split('Version: ', 1)[1]
                    if not version:
                        LOG.debug('One cpu version is empty')
                    else:
                        break

            except (IndexError, ValueError):
                LOG.warning('Malformed CPU version information: %s', out)
        else:
            LOG.warning('Failed to get CPU version')

        core_count = 0
        thread_count = 0
        out, _e = utils.try_execute("dmidecode --type processor | grep 'Count'", shell=True)
        if out:
            try:
                for line in out.strip().split('\n'):
                    line = line.strip()
                    if core_count and thread_count:
                        break

                    if 'Core Count' in line:
                        value = line.split('Core Count: ', 1)[1]
                        core_count = int(value)

                    if 'Thread Count' in line:
                        value = line.split('Thread Count: ', 1)[1]
                        thread_count = int(value)

            except (IndexError, ValueError):
                LOG.warning('Malformed CPU core count and thread count information: %s', out)
        else:
            LOG.warning('Failed to get CPU core count and thread count')

        return CPUInfo(version=version, cpu_count=cpu_count,
                       core_count=core_count, thread_count=thread_count)


    def list_block_devices(self):
        return list_all_block_devices()

    def get_os_install_device(self):
        cached_node = get_cached_node()
        root_device_hints = None
        if cached_node is not None:
            root_device_hints = cached_node['properties'].get('root_device')

        block_devices = self.list_block_devices()
        if not root_device_hints:
            return utils.guess_root_disk(block_devices).name
        else:

            def match(hint, current_value, device):
                hint_value = root_device_hints[hint]

                if hint == 'rotational':
                    hint_value = strutils.bool_from_string(hint_value)

                elif hint == 'size':
                    try:
                        hint_value = int(hint_value)
                    except (ValueError, TypeError):
                        LOG.warning(
                            'Root device hint "size" is not an integer. '
                            'Current value: "%(value)s"; and type: "%(type)s"',
                            {'value': hint_value, 'type': type(hint_value)})
                        return False

                if hint_value != current_value:
                    LOG.debug("Root device hint %(hint)s=%(value)s does not "
                              "match the device %(device)s value of "
                              "%(current)s", {
                                  'hint': hint,
                                  'value': hint_value, 'device': device,
                                  'current': current_value})
                    return False
                return True

            def check_device_attrs(device):
                for key in ('model', 'wwn', 'serial', 'vendor',
                            'wwn_with_extension', 'wwn_vendor_extension',
                            'name', 'rotational', 'size'):
                    if key not in root_device_hints:
                        continue

                    value = getattr(device, key)
                    if value is None:
                        return False

                    if isinstance(value, six.string_types):
                        value = utils.normalize(value)

                    if key == 'size':
                        # Since we don't support units yet we expect the size
                        # in GiB for now
                        value = value / units.Gi

                    if not match(key, value, device.name):
                        return False

                return True

            for dev in block_devices:
                if check_device_attrs(dev):
                    return dev.name

            else:
                raise errors.DeviceNotFound(
                    "No suitable device was found for "
                    "deployment using these hints %s" % root_device_hints)

    def get_system_vendor_info(self):
        product_name = None
        serial_number = None
        manufacturer = None
        try:
            out, _e = utils.execute("dmidecode --type system",
                                    shell=True)
        except (processutils.ProcessExecutionError, OSError) as e:
            LOG.warning("Cannot get system vendor information: %s", e)
        else:
            for line in out.split('\n'):
                line_arr = line.split(':', 1)
                if len(line_arr) != 2:
                    continue
                if line_arr[0].strip() == 'Product Name':
                    product_name = line_arr[1].strip()
                elif line_arr[0].strip() == 'Serial Number':
                    serial_number = line_arr[1].strip()
                elif line_arr[0].strip() == 'Manufacturer':
                    manufacturer = line_arr[1].strip()
                    
        try:
            out, _e = utils.execute("dmidecode -s chassis-asset-tag",
                                    shell=True)
        except (processutils.ProcessExecutionError, OSError) as e:
            LOG.warning("Cannot get system vendor asset tag: %s", e)
        else:
            out_list = out.strip().split('\n')
            asset_tag = out_list[len(out_list)-1]
            
        return SystemVendorInfo(product_name=product_name,
                                serial_number=serial_number,
                                manufacturer=manufacturer,
                                asset_tag=asset_tag)

    def get_boot_info(self):
        boot_mode = 'uefi' if os.path.isdir('/sys/firmware/efi') else 'bios'
        LOG.debug('The current boot mode is %s', boot_mode)
        pxe_interface = utils.get_agent_params().get('BOOTIF')
        return BootInfo(current_boot_mode=boot_mode,
                        pxe_interface=pxe_interface)

    def erase_block_device(self, node, block_device):

        # Check if the block device is virtual media and skip the device.
        if self._is_virtual_media_device(block_device):
            LOG.info("Skipping the erase of virtual media device %s",
                     block_device.name)
            return

        # Note(TheJulia) Use try/except to capture and log the failure
        # and then revert to attempting to shred the volume if enabled.
        try:
            if self._ata_erase(block_device):
                return
        except errors.BlockDeviceEraseError as e:
            info = node.get('driver_internal_info', {})
            execute_shred = info.get(
                'agent_continue_if_ata_erase_failed', False)
            if execute_shred:
                LOG.warning('Failed to invoke ata_erase, '
                            'falling back to shred: %(err)s'
                            % {'err': e})
            else:
                msg = ('Failed to invoke ata_erase, '
                       'fallback to shred is not enabled: %(err)s'
                       % {'err': e})
                LOG.error(msg)
                raise errors.IncompatibleHardwareMethodError(msg)

        if self._shred_block_device(node, block_device):
            return

        msg = ('Unable to erase block device {0}: device is unsupported.'
               ).format(block_device.name)
        LOG.error(msg)
        raise errors.IncompatibleHardwareMethodError(msg)

    def _shred_block_device(self, node, block_device):
        """Erase a block device using shred.

        :param node: Ironic node info.
        :param block_device: a BlockDevice object to be erased
        :returns: True if the erase succeeds, False if it fails for any reason
        """
        info = node.get('driver_internal_info', {})
        npasses = info.get('agent_erase_devices_iterations', 1)
        args = ('shred', '--force')

        if info.get('agent_erase_devices_zeroize', True):
            args += ('--zero', )

        args += ('--verbose', '--iterations', str(npasses), block_device.name)

        try:
            utils.execute(*args)
        except (processutils.ProcessExecutionError, OSError) as e:
            msg = ("Erasing block device %(dev)s failed with error %(err)s ",
                   {'dev': block_device.name, 'err': e})
            LOG.error(msg)
            return False

        return True

    def _is_virtual_media_device(self, block_device):
        """Check if the block device corresponds to Virtual Media device.

        :param block_device: a BlockDevice object
        :returns: True if it's a virtual media device, else False
        """
        vm_device_label = '/dev/disk/by-label/ir-vfd-dev'
        if os.path.exists(vm_device_label):
            link = os.readlink(vm_device_label)
            device = os.path.normpath(os.path.join(os.path.dirname(
                                                   vm_device_label), link))
            if block_device.name == device:
                return True
        return False

    def _get_ata_security_lines(self, block_device):
        output = utils.execute('hdparm', '-I', block_device.name)[0]

        if '\nSecurity: ' not in output:
            return []

        # Get all lines after the 'Security: ' line
        security_and_beyond = output.split('\nSecurity: \n')[1]
        security_and_beyond_lines = security_and_beyond.split('\n')

        security_lines = []
        for line in security_and_beyond_lines:
            if line.startswith('\t'):
                security_lines.append(line.strip().replace('\t', ' '))
            else:
                break

        return security_lines

    def _ata_erase(self, block_device):
        security_lines = self._get_ata_security_lines(block_device)

        # If secure erase isn't supported return False so erase_block_device
        # can try another mechanism. Below here, if secure erase is supported
        # but fails in some way, error out (operators of hardware that supports
        # secure erase presumably expect this to work).
        if 'supported' not in security_lines:
            return False

        if 'enabled' in security_lines:
            # Attempt to unlock the drive in the event it has already been
            # locked by a previous failed attempt.
            try:
                utils.execute('hdparm', '--user-master', 'u',
                              '--security-unlock', 'NULL', block_device.name)
                security_lines = self._get_ata_security_lines(block_device)
            except processutils.ProcessExecutionError as e:
                raise errors.BlockDeviceEraseError('Security password set '
                                                   'failed for device '
                                                   '%(name)s: %(err)s' %
                                                   {'name': block_device.name,
                                                    'err': e})

        if 'enabled' in security_lines:
            raise errors.BlockDeviceEraseError(
                ('Block device {0} already has a security password set'
                 ).format(block_device.name))

        if 'not frozen' not in security_lines:
            raise errors.BlockDeviceEraseError(
                ('Block device {0} is frozen and cannot be erased'
                 ).format(block_device.name))

        try:
            utils.execute('hdparm', '--user-master', 'u',
                          '--security-set-pass', 'NULL', block_device.name)
        except processutils.ProcessExecutionError as e:
            raise errors.BlockDeviceEraseError('Security password set '
                                               'failed for device '
                                               '%(name)s: %(err)s' %
                                               {'name': block_device.name,
                                                'err': e})

        # Use the 'enhanced' security erase option if it's supported.
        erase_option = '--security-erase'
        if 'not supported: enhanced erase' not in security_lines:
            erase_option += '-enhanced'

        try:
            utils.execute('hdparm', '--user-master', 'u', erase_option,
                          'NULL', block_device.name)
        except processutils.ProcessExecutionError as e:
            raise errors.BlockDeviceEraseError('Erase failed for device '
                                               '%(name)s: %(err)s' %
                                               {'name': block_device.name,
                                                'err': e})

        # Verify that security is now 'not enabled'
        security_lines = self._get_ata_security_lines(block_device)
        if 'not enabled' not in security_lines:
            raise errors.BlockDeviceEraseError(
                ('An unknown error occurred erasing block device {0}'
                 ).format(block_device.name))

        return True

    def get_bmc_address(self):
        # These modules are rarely loaded automatically
        utils.try_execute('modprobe', 'ipmi_msghandler')
        utils.try_execute('modprobe', 'ipmi_devintf')
        utils.try_execute('modprobe', 'ipmi_si')

        try:
            out, _e = utils.execute(
                "ipmitool lan print | grep -e 'IP Address [^S]' "
                "| awk '{ print $4 }'", shell=True)
        except (processutils.ProcessExecutionError, OSError) as e:
            # Not error, because it's normal in virtual environment
            LOG.warning("Cannot get BMC address: %s", e)
            return

        return out.strip()


def _compare_extensions(ext1, ext2):
    mgr1 = ext1.obj
    mgr2 = ext2.obj
    return mgr2.evaluate_hardware_support() - mgr1.evaluate_hardware_support()


def _get_managers():
    """Get a list of hardware managers in priority order.

    Use stevedore to find all eligible hardware managers, sort them based on
    self-reported (via evaluate_hardware_support()) priorities, and return them
    in a list. The resulting list is cached in _global_managers.

    :returns: Priority-sorted list of hardware managers
    :raises HardwareManagerNotFound: if no valid hardware managers found
    """
    global _global_managers

    if not _global_managers:
        extension_manager = stevedore.ExtensionManager(
            namespace='ironic_python_agent.hardware_managers',
            invoke_on_load=True)

        # There will always be at least one extension available (the
        # GenericHardwareManager).
        if six.PY2:
            extensions = sorted(extension_manager, _compare_extensions)
        else:
            extensions = sorted(extension_manager,
                                key=functools.cmp_to_key(_compare_extensions))

        preferred_managers = []

        for extension in extensions:
            if extension.obj.evaluate_hardware_support() > 0:
                preferred_managers.append(extension.obj)
                LOG.info('Hardware manager found: {0}'.format(
                    extension.entry_point_target))

        if not preferred_managers:
            raise errors.HardwareManagerNotFound

        _global_managers = preferred_managers

    return _global_managers


def dispatch_to_all_managers(method, *args, **kwargs):
    """Dispatch a method to all hardware managers.

    Dispatches the given method in priority order as sorted by
    `_get_managers`. If the method doesn't exist or raises
    IncompatibleHardwareMethodError, it continues to the next hardware manager.
    All managers that have hardware support for this node will be called,
    and their responses will be added to a dictionary of the form
    {HardwareManagerClassName: response}.

    :param method: hardware manager method to dispatch
    :param *args: arguments to dispatched method
    :param **kwargs: keyword arguments to dispatched method
    :raises errors.HardwareManagerMethodNotFound: if all managers raise
        IncompatibleHardwareMethodError.
    :returns: a dictionary with keys for each hardware manager that returns
        a response and the value as a list of results from that hardware
        manager.
    """
    responses = {}
    managers = _get_managers()
    for manager in managers:
        if getattr(manager, method, None):
            try:
                response = getattr(manager, method)(*args, **kwargs)
            except errors.IncompatibleHardwareMethodError:
                LOG.debug('HardwareManager {0} does not support {1}'
                          .format(manager, method))
                continue
            except Exception as e:
                LOG.exception('Unexpected error dispatching %(method)s to '
                              'manager %(manager)s: %(e)s',
                              {'method': method, 'manager': manager, 'e': e})
                raise
            responses[manager.__class__.__name__] = response
        else:
            LOG.debug('HardwareManager {0} does not have method {1}'
                      .format(manager, method))

    if responses == {}:
        raise errors.HardwareManagerMethodNotFound(method)

    return responses


def dispatch_to_managers(method, *args, **kwargs):
    """Dispatch a method to best suited hardware manager.

    Dispatches the given method in priority order as sorted by
    `_get_managers`. If the method doesn't exist or raises
    IncompatibleHardwareMethodError, it is attempted again with a more generic
    hardware manager. This continues until a method executes that returns
    any result without raising an IncompatibleHardwareMethodError.

    :param method: hardware manager method to dispatch
    :param *args: arguments to dispatched method
    :param **kwargs: keyword arguments to dispatched method

    :returns: result of successful dispatch of method
    :raises HardwareManagerMethodNotFound: if all managers failed the method
    :raises HardwareManagerNotFound: if no valid hardware managers found
    """
    managers = _get_managers()
    for manager in managers:
        if getattr(manager, method, None):
            try:
                return getattr(manager, method)(*args, **kwargs)
            except(errors.IncompatibleHardwareMethodError):
                LOG.debug('HardwareManager {0} does not support {1}'
                          .format(manager, method))
            except Exception as e:
                LOG.exception('Unexpected error dispatching %(method)s to '
                              'manager %(manager)s: %(e)s',
                              {'method': method, 'manager': manager, 'e': e})
                raise
        else:
            LOG.debug('HardwareManager {0} does not have method {1}'
                      .format(manager, method))

    raise errors.HardwareManagerMethodNotFound(method)


def load_managers():
    """Preload hardware managers into the cache.

    This method is to help warm up the cache for hardware managers when
    called. Used to resolve bug 1490008, where agents can crash the first
    time a hardware manager is needed.

    :raises HardwareManagerNotFound: if no valid hardware managers found
    """
    _get_managers()


def cache_node(node):
    """Store the node object in the hardware module.

    Stores the node object in the hardware module to facilitate the
    access of a node information in the hardware extensions.

    :param node: Ironic node object
    """
    global NODE
    NODE = node


def get_cached_node():
    """Guard function around the module variable NODE."""
    return NODE
