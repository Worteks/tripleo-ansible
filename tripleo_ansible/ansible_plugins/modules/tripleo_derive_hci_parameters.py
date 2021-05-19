#!/usr/bin/env python
# Copyright 2020 Red Hat, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Derive paramters for HCI (hyper-converged) deployments"""

import os
import re
import yaml

from ansible.module_utils.basic import AnsibleModule


ANSIBLE_METADATA = {
    'metadata_version': '0.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: tripleo_derive_hci_parameters
short_description: Tune Nova scheduler parameters to reserve resources for collocated Ceph OSDs
description:
    - "When collocating Ceph OSDs on Nova Compute hosts (hyperconverged or hci) the Nova Scheduler does not take into account the CPU/Memory needs of the OSDs. This module returns  recommended NovaReservedHostMemory and NovaCPUAllocationRatio parmaters so that the host reseves memory and CPU for Ceph. The values are based on workload description, deployment configuration, and Ironic data. The workload description is the expected average_guest_cpu_utilization_percentage and average_guest_memory_size_in_mb."
options:
    tripleo_environment_parameters:
        description: Map from key environment_parameters from stack_data. Used to determine number of OSDs in deployment per role
        required: True
        type: map
    tripleo_role_name:
        description: TripleO role name whose parameters are being derived
        required: True
        type: string
    introspection_data:
        description: Output of the tripleo_get_introspected_data module. Used to determine available memory and CPU of each instance from any role with the CephOSD service
        required: True
        type: map
    average_guest_cpu_utilization_percentage:
        description: Percentage of CPU utilization expected for average guest, e.g. 99 means 99% and 10 means 10%
        required: False
        type: int
        default: 0
    average_guest_memory_size_in_mb:
        description: Amount of memory in MB required by the average guest
        required: False
        type: int
        default: 0
    derived_parameters:
        description: any previously derived parameters which should be included in the final result
        required: False
        type: map
    new_heat_environment_path:
        description: Path to file new where resultant derived parameters will be written; result will be valid input to TripleO client, e.g. /home/stack/derived_paramaters.yaml
        required: False
        type: str
    append_new_heat_environment_path:
        description: If new_heat_environment_path already exists and append_new_heat_environment_path is true, then append new content to the existing new_heat_environment_path instead of overwriting a new version of that file.
        required: False
        type: bool
    report_path:
        description: Path to file new where a report on how HCI paramters were derived be written, e.g. /home/stack/hci_derived_paramaters.txt
        required: False
        type: str
author:
    - John Fulton (fultonj)
'''

EXAMPLES = '''
- name: Add Derived HCI parameters to existing derived parameters for ComputeHCI role
  tripleo_derive_hci_parameters:
    tripleo_environment_parameters: "{{ tripleo_environment_parameters }}"
    introspection_data: "{{ hw_data }}"
    derived_parameters: "{{ derived_parameters }}"
    tripleo_role_name: "ComputeHCI"
    average_guest_cpu_utilization_percentage: 90
    average_guest_memory_size_in_mb: 8192
    new_heat_environment_path: "/home/stack/hci_result.yaml"
    report_path: "/home/stack/hci_report.txt"
  register: derived_parameters_result

- name: Show derived HCI Memory result
  debug:
    msg: "{{ derived_parameters_result['derived_parameters']['ComputeHCIParameters']['NovaReservedHostMemory'] }}"

- name: Show derived HCI CPU result
  debug:
    msg: "{{ derived_parameters_result['derived_parameters']['ComputeHCIParameters']['NovaCPUAllocationRatio'] }}"

'''

RETURN = '''
message:
    description: A description of the HCI derived parameters calculation or an error message
    type: str
    returned: always
derived_parameters:
    description: map with derived hci paramters and any previously derived parameters
    required: False
    type: map
'''

MB_PER_GB = 1024


def derive(mem_gb, vcpus, osds, average_guest_memory_size_in_mb=0,
           average_guest_cpu_utilization_percentage=0,
           mem_gb_per_osd=5, vcpus_per_osd=1, total_memory_threshold=0.8):
    """
    Determines the recommended Nova scheduler values based on Ceph needs
    and described average Nova guest workload in CPU and Memory utilization.
    If expected guest utilization is not provided result is less accurate.
    Returns dictionary containing the keys: cpu_allocation_ratio (float),
    nova_reserved_mem_mb (int), message (string), failed (boolean).
    """
    gb_overhead_per_guest = 0.5  # based on measurement in test environment

    # seed the result
    derived = {}
    derived['failed'] = False
    derived['message'] = ""
    messages = []

    if average_guest_memory_size_in_mb == 0 and \
       average_guest_cpu_utilization_percentage == 0:
        workload = False
    else:
        workload = True

    # catch possible errors in parameters
    if mem_gb < 1:
        messages.append("Unable to determine the amount of physical memory "
                        "(no 'memory_mb' found in introspection_data).")
        derived['failed'] = True

    if vcpus < 1:
        messages.append("Unable to determine the number of CPU cores. "
                        "Either no 'cpus' found in introspection_data or "
                        "NovaVcpuPinSet is not correctly set.")
        derived['failed'] = True

    if osds < 1:
        messages.append("No OSDs were found in the deployment definition. ")
        derived['failed'] = True

    if average_guest_memory_size_in_mb < 0 and workload:
        messages.append("If average_guest_memory_size_in_mb "
                        "is used it must be greater than 0")
        derived['failed'] = True

    if average_guest_cpu_utilization_percentage < 0 and workload:
        messages.append("If average_guest_cpu_utilization_percentage is "
                        "used it must be greater than 0")
        derived['failed'] = True

    left_over_mem = mem_gb - (mem_gb_per_osd * osds)

    if left_over_mem < 0:
        messages.append(("There is not enough memory to run %d OSDs. "
                         "%d GB RAM - (%d GB per OSD * %d OSDs) is < 0")
                        % (osds, mem_gb, mem_gb_per_osd, osds))
        derived['failed'] = True

    if derived['failed']:
        derived['message'] = " ".join(messages)
        return derived

    # perform the calculation
    if workload:
        average_guest_size = average_guest_memory_size_in_mb / float(MB_PER_GB)
        average_guest_util = average_guest_cpu_utilization_percentage * 0.01
        number_of_guests = int(left_over_mem
                               / (average_guest_size + gb_overhead_per_guest))
        nova_reserved_mem_mb = MB_PER_GB * ((mem_gb_per_osd * osds)
                                            + (number_of_guests * gb_overhead_per_guest))
        nonceph_vcpus = vcpus - (vcpus_per_osd * osds)
        guest_vcpus = nonceph_vcpus / average_guest_util
        cpu_allocation_ratio = guest_vcpus / vcpus
    else:
        nova_reserved_mem_mb = MB_PER_GB * (mem_gb_per_osd * osds)

    # save calculation results
    derived['nova_reserved_mem_mb'] = int(nova_reserved_mem_mb)
    if workload:
        derived['cpu_allocation_ratio'] = cpu_allocation_ratio

    # capture derivation details in message
    messages.append(("Derived Parameters results"
                     "\n Inputs:"
                     "\n - Total host RAM in GB: %d"
                     "\n - Total host vCPUs: %d"
                     "\n - Ceph OSDs per host: %d")
                    % (mem_gb, vcpus, osds))
    if workload:
        messages.append(("\n - Average guest memory size in GB: %d"
                         "\n - Average guest CPU utilization: %.0f%%") %
                        (average_guest_size, average_guest_cpu_utilization_percentage))
    messages.append("\n Outputs:")
    if workload:
        messages.append(("\n - number of guests allowed based on memory = %d"
                         "\n - number of guest vCPUs allowed = %d"
                         "\n - nova.conf cpu_allocation_ratio = %2.2f") %
                        (number_of_guests, int(guest_vcpus), cpu_allocation_ratio))
    messages.append(("\n - nova.conf reserved_host_memory = %d MB"
                     % nova_reserved_mem_mb))
    if workload:
        messages.append("\nCompare \"guest vCPUs allowed\" to "
                        "\"guests allowed based on memory\" "
                        "for actual guest count.")

    warnings = []
    if nova_reserved_mem_mb > (MB_PER_GB * mem_gb * total_memory_threshold):
        warnings.append(("ERROR: %d GB is not enough memory to "
                         "run hyperconverged\n") % mem_gb)
        derived['failed'] = True
    if workload:
        if cpu_allocation_ratio < 0.5:
            warnings.append("ERROR: %d is not enough vCPU to run hyperconverged\n" % vcpus)
            derived['failed'] = True
        if cpu_allocation_ratio > 16.0:
            warnings.append("WARNING: do not increase vCPU overcommit ratio beyond 16:1\n")
    else:
        warnings.append("WARNING: the average guest workload was not provided. \n"
                        "Both average_guest_cpu_utilization_percentage and \n"
                        "average_guest_memory_size_in_mb are defaulted to 0. \n"
                        "The HCI derived parameter calculation cannot set the \n"
                        "Nova cpu_allocation_ratio. The Nova reserved_host_memory_mb \n"
                        "will be set based on the number of OSDs but the Nova \n"
                        "guest memory overhead will not be taken into account. \n")
    derived['message'] = " ".join(warnings) + " ".join(messages)

    return derived


def count_osds(tripleo_environment_parameters):
    """
    Counts the requested OSDs in the tripleo_environment_parameters.
    Returns an integer representing the count.
    """
    total = 0
    if 'CephAnsibleDisksConfig' in tripleo_environment_parameters:
        disks_config = tripleo_environment_parameters['CephAnsibleDisksConfig']
        for key in ['devices', 'lvm_volumes']:
            if key in disks_config:
                total = total + len(disks_config[key])
    return total


def count_memory(ironic):
    """
    Counts the memory found in the ironic introspection data. If
    memory_mb is 0, uses ['inventory']['memory']['total'] in bytes.
    Returns integer of memory in GB.
    """
    memory = 0
    if 'data' in ironic:
        if 'memory_mb' in ironic['data']:
            if int(ironic['data']['memory_mb']) > 0:
                memory = int(ironic['data']['memory_mb']) / float(MB_PER_GB)
            elif 'inventory' in ironic['data']:
                if 'memory' in ironic['data']['inventory']:
                    if 'total' in ironic['data']['inventory']['memory']:
                        memory = int(ironic['data']['inventory']['memory']['total']) \
                            / float(MB_PER_GB) / float(MB_PER_GB) / float(MB_PER_GB)
    return memory


def convert_range_to_number_list(range_list):
    """
    Returns list of numbers from descriptive range input list
    E.g. ['12-14', '^13', '17'] is converted to [12, 14, 17]
    Returns string with error message if unable to parse input
    """
    # borrowed from jpalanis@redhat.com
    num_list = []
    exclude_num_list = []
    try:
        for val in range_list:
            val = val.strip(' ')
            if '^' in val:
                exclude_num_list.append(int(val[1:]))
            elif '-' in val:
                split_list = val.split("-")
                range_min = int(split_list[0])
                range_max = int(split_list[1])
                num_list.extend(range(range_min, (range_max + 1)))
            else:
                num_list.append(int(val))
    except ValueError as exc:
        return "Parse Error: Invalid number in input param 'num_list': %s" % exc
    return [num for num in num_list if num not in exclude_num_list]


def count_nova_vcpu_pins(module):
    """
    Returns the number of CPUs defined in NovaVcpuPinSet as set in
    the environment or derived parameters. If multiple NovaVcpuPinSet
    parameters are defined, priority is given to role, then the default
    value for all roles, and then what's in previously derived_parameters
    """
    tripleo_role_name = module.params['tripleo_role_name']
    tripleo_environment_parameters = module.params['tripleo_environment_parameters']
    derived_parameters = module.params['derived_parameters']
    # NovaVcpuPinSet can be defined in multiple locations, and it's
    # important to select the value in order of precedence:
    # 1) User specified value for this role
    # 2) User specified default value for all roles
    # 3) Value derived by a previous derived parameters playbook run
    #
    # Set an exclusive prioritized possible_location to get the NovaVcpuPinSet
    if tripleo_role_name + 'Parameters' in tripleo_environment_parameters:  # 1
        possible_location = tripleo_environment_parameters[tripleo_role_name + 'Parameters']
    elif 'NovaVcpuPinSet' in tripleo_environment_parameters:  # 2
        possible_location = tripleo_environment_parameters
    elif tripleo_role_name + 'Parameters' in derived_parameters:  # 3
        possible_location = derived_parameters[tripleo_role_name + 'Parameters']
    else:  # default the possible_location to an empty dictionary
        possible_location = {}
    if 'NovaVcpuPinSet' in possible_location:
        converted = convert_range_to_number_list(possible_location['NovaVcpuPinSet'])
        if isinstance(converted, str):
            module.fail_json(converted)
        if isinstance(converted, list):
            return len(converted)
    return 0


def count_vcpus(module):
    # if only look at ironic data if NovaVcpuPinSet is not used
    vcpus = count_nova_vcpu_pins(module)
    if vcpus == 0:
        try:
            vcpus = module.params['introspection_data']['data']['cpus']
        except KeyError:
            vcpus = 0
    return vcpus


def get_vcpus_per_osd_from_ironic(ironic, tripleo_environment_parameters, num_osds):
    """
    Dynamically sets the vCPU to OSD ratio based the OSD type to:
      HDD  | OSDs per device: 1 | vCPUs per device: 1
      SSD  | OSDs per device: 1 | vCPUs per device: 4
      NVMe | OSDs per device: 4 | vCPUs per device: 3
    Gets requested OSD list from tripleo_environment_parameters input
    and looks up the device type in ironic input. Returns the vCPUs
    per OSD, an explanation message.
    """
    cpus = 1
    nvme_re = re.compile('.*nvme.*')
    type_map = {}
    hdd_count = ssd_count = nvme_count = 0
    warning = False
    messages = []
    try:
        devices = tripleo_environment_parameters['CephAnsibleDisksConfig']['devices']
    except KeyError:
        devices = []
        messages.append("No devices defined in CephAnsibleDisksConfig")
        warning = True
    try:
        ironic_disks = ironic['data']['inventory']['disks']
    except KeyError:
        ironic_disks = []
        messages.append("No disks found in introspection data inventory")
        warning = True
    if len(devices) != num_osds:
        messages.append("Not all OSDs are in the devices list. Unable to "
                        "determine hardware type for all OSDs. This might be "
                        "because lvm_volumes was used to define some OSDs. ")
        warning = True
    elif len(devices) > 0 and len(ironic_disks) > 0:
        disks_config = tripleo_environment_parameters['CephAnsibleDisksConfig']
        for osd_dev in disks_config['devices']:
            for ironic_dev in ironic_disks:
                for key in ('name', 'by_path', 'wwn'):
                    if key in ironic_dev:
                        if osd_dev == ironic_dev[key]:
                            if 'rotational' in ironic_dev:
                                if ironic_dev['rotational']:
                                    type_map[osd_dev] = 'hdd'
                                    hdd_count += 1
                                elif nvme_re.search(osd_dev):
                                    type_map[osd_dev] = 'nvme'
                                    nvme_count += 1
                                else:
                                    type_map[osd_dev] = 'ssd'
                                    ssd_count += 1
        messages.append(("HDDs %i | Non-NVMe SSDs %i | NVMe SSDs %i \n" %
                         (hdd_count, ssd_count, nvme_count)))
        if hdd_count > 0 and ssd_count == 0 and nvme_count == 0:
            cpus = 1  # default
            messages.append(("vCPU to OSD ratio: %i" % cpus))
        elif hdd_count == 0 and ssd_count > 0 and nvme_count == 0:
            cpus = 4
            messages.append(("vCPU to OSD ratio: %i" % cpus))
        elif hdd_count == 0 and ssd_count == 0 and nvme_count > 0:
            # did they set OSDs per device?
            if 'osds_per_device' in disks_config:
                osds_per_device = disks_config['osds_per_device']
            else:
                osds_per_device = 1  # default defined in ceph-ansible
            if osds_per_device == 4:
                # All NVMe OSDs so 12 vCPUs per OSD for optimal IO performance
                cpus = 3
            else:
                cpus = 4  # use standard SSD default
                messages.append("\nWarning: osds_per_device not set to 4 "
                                "but all OSDs are of type NVMe. \n"
                                "Recomentation to improve IO: "
                                "set osds_per_device to 4 and re-run \n"
                                "so that vCPU to OSD ratio is 3 "
                                "for 12 vCPUs per OSD device.")
                warning = True
            messages.append(("vCPU to OSD ratio: %i"
                             " (found osds_per_device set to: %i)") %
                            (cpus, osds_per_device))
        elif hdd_count == 0 and ssd_count == 0 and nvme_count == 0:
            cpus = 1  # default
            messages.append(("vCPU to OSD ratio: %i \nWarning: "
                             "unable to determine OSD types. "
                             "Unable to recommend optimal ratio "
                             "so using default.") % cpus)
            warning = True
        else:
            cpus = 1  # default
            messages.append(("vCPU to OSD ratio: %i \nWarning: Requested "
                             "OSDs are of mixed type. Unable to recommend "
                             "optimal ratio so using default.") % cpus)
            warning = True

    msg = "".join(["\nOSD type distribution:\n"] + messages)
    if warning:
        msg = "WARNING: " + msg

    return cpus, msg


def get_vcpus_per_osd(tripleo_environment_parameters, osd_count, osd_type, osd_spec):
    """
    Dynamically sets the vCPU to OSD ratio based the OSD type to:
      HDD  | OSDs per device: 1 | vCPUs per device: 1
      SSD  | OSDs per device: 1 | vCPUs per device: 4
      NVMe | OSDs per device: 4 | vCPUs per device: 3
    Relies on parameters from tripleo_environment_parameters input.
    Returns the vCPUs per OSD and an explanation message.
    """
    cpus = 1
    messages = []
    warning = False

    # This module can analyze a THT file even when it is not called from
    # within Heat. Thus, we cannot assume THT validations are enforced.
    if osd_type not in ['hdd', 'ssd', 'nvme']:
        warning = True
        messages.append(("'%s' is not a valid osd_type so "
                         "defaulting to 'hdd'. ") % osd_type)
        osd_type = 'hdd'
    messages.append(("CephHciOsdType: %s\n") % osd_type)

    if osd_type == 'hdd':
        cpus = 1
    elif osd_type == 'ssd':
        cpus = 4
    elif osd_type == 'nvme':
        # If they set it to NVMe and used a manual spec, then 3 is also valid
        cpus = 3
        if type(osd_spec) is not dict:
            messages.append("\nNo valid CephOsdSpec was not found. Unable "
                            "to determine if osds_per_device is being used. "
                            "osds_per_device: 4 is recommended for 'nvme'. ")
            warning = True
        if 'osds_per_device' in osd_spec:
            if osd_spec['osds_per_device'] == 4:
                cpus = 3
            else:
                cpus = 4
                messages.append("\nosds_per_device not set to 4 "
                                "but all OSDs are of type NVMe. \n"
                                "Recommendation to improve IO: "
                                "set osds_per_device to 4 and re-run \n"
                                "so that vCPU to OSD ratio is 3 "
                                "for 12 vCPUs per OSD device.")
                warning = True

    messages.append(("vCPU to OSD ratio: %i\n" % cpus))
    if osd_spec != 0 and 'osds_per_device' in osd_spec:
        messages.append(" (found osds_per_device set to: %i)" %
                        osd_spec['osds_per_device'])
    msg = "".join(messages)
    if warning:
        msg = "WARNING: " + msg

    return cpus, msg


def find_parameter(env, param, role=""):
    """
    Find a parameter in an environment map and return it.
    If paramter is not found return 0.

    Supports role parameters too. E.g. given the following
    inside of env, with param=CephHciOsdCount and role="",
    this function returns 3. But if role=ComputeHCI, then
    it would return 4.

      CephHciOsdCount: 3
      ComputeHCIParameters:
        CephHciOsdCount: 4
    """
    role_parameters = role + 'Parameters'
    if role_parameters in env and param in env[role_parameters]:
        return env[role_parameters][param]
    elif param in env:
        return env[param]
    return 0


def main():
    """Main method of Ansible module
    """
    result = dict(
        changed=False,
        message=''
    )
    module_args = dict(
        tripleo_environment_parameters=dict(type=dict, required=True),
        tripleo_role_name=dict(type=str, required=True),
        introspection_data=dict(type=dict, required=True),
        average_guest_cpu_utilization_percentage=dict(type=int, required=False, default=0),
        average_guest_memory_size_in_mb=dict(type=int, required=False, default=0),
        derived_parameters=dict(type=dict, required=False),
        new_heat_environment_path=dict(type=str, required=False),
        append_new_heat_environment_path=dict(type=bool, required=False),
        report_path=dict(type=str, required=False),
    )
    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )
    if module.params['derived_parameters'] is None:
        module.params['derived_parameters'] = {}

    vcpus = count_vcpus(module)
    mem_gb = count_memory(module.params['introspection_data'])
    num_osds = find_parameter(module.params['tripleo_environment_parameters'],
                              'CephHciOsdCount', module.params['tripleo_role_name'])
    if num_osds > 0:
        osd_type = find_parameter(module.params['tripleo_environment_parameters'],
                                  'CephHciOsdType', module.params['tripleo_role_name'])
        osd_spec = find_parameter(module.params['tripleo_environment_parameters'],
                                  'CephOsdSpec', module.params['tripleo_role_name'])
        vcpu_ratio, vcpu_ratio_msg = get_vcpus_per_osd(
            module.params['tripleo_environment_parameters'],
            num_osds, osd_type, osd_spec)
    else:
        num_osds = count_osds(module.params['tripleo_environment_parameters'])
        vcpu_ratio, vcpu_ratio_msg = get_vcpus_per_osd_from_ironic(
            module.params['introspection_data'],
            module.params['tripleo_environment_parameters'],
            num_osds)

    # Derive HCI parameters
    mem_gb_per_osd = 5
    derivation = derive(mem_gb, vcpus, num_osds,
                        module.params['average_guest_memory_size_in_mb'],
                        module.params['average_guest_cpu_utilization_percentage'],
                        mem_gb_per_osd, vcpu_ratio)

    # directly set failed status and message
    result['failed'] = derivation['failed']
    result['message'] = derivation['message'] + "\n" + vcpu_ratio_msg

    # make a copy of the existing derived_parameters (e.g. perhaps from NFV)
    existing_params = module.params['derived_parameters']
    # add HCI derived paramters for Nova scheduler
    if not derivation['failed']:
        role_derivation = {}
        role_derivation['NovaReservedHostMemory'] = derivation['nova_reserved_mem_mb']
        if 'cpu_allocation_ratio' in derivation:
            role_derivation['NovaCPUAllocationRatio'] = derivation['cpu_allocation_ratio']
        role_name_parameters = module.params['tripleo_role_name'] + 'Parameters'
        existing_params[role_name_parameters] = role_derivation
        # write out to file if requested
        if module.params['new_heat_environment_path'] and not module.check_mode:
            if module.params['append_new_heat_environment_path'] and \
               os.path.exists(module.params['new_heat_environment_path']):
                with open(module.params['new_heat_environment_path'], 'r') as stream:
                    try:
                        output = yaml.safe_load(stream)
                        if 'parameter_defaults' in output:
                            output['parameter_defaults'][role_name_parameters] = \
                                role_derivation
                        else:
                            result['failed'] = True
                            result['message'] = ("tripleo_derive_hci_parameters module "
                                                 "cannot append to environment file %s. "
                                                 "It is missing the 'parameter_defaults' "
                                                 "key. Try again with the parameter "
                                                 "append_new_heat_environment_path set "
                                                 "False") \
                            % module.params['new_heat_environment_path']
                    except yaml.YAMLError as exc:
                        result['failed'] = True
                        result['message'] = exec
            else:
                output = {}
                output['parameter_defaults'] = existing_params
            with open(module.params['new_heat_environment_path'], 'w') as outfile:
                yaml.safe_dump(output, outfile, default_flow_style=False)
            # because we wrote a file we're making a change on the target system
            result['changed'] = True
        if module.params['report_path'] and not module.check_mode:
            with open(module.params['report_path'], 'w') as outfile:
                outfile.write(result['message'])
            # because we wrote a file we're making a change on the target system
            result['changed'] = True

    # return existing derived parameters with the new HCI parameters too
    result['derived_parameters'] = existing_params

    # Exit and pass the key/value results
    module.exit_json(**result)


if __name__ == '__main__':
    main()
