#!/usr/bin/python
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


import jsonschema

from metalsmith import sources


_IMAGE_SCHEMA = {
    'type': 'object',
    'properties': {
        'href': {'type': 'string'},
        'checksum': {'type': 'string'},
        'kernel': {'type': 'string'},
        'ramdisk': {'type': 'string'},
    },
    'required': ['href'],
    'additionalProperties': False,
}

_NIC_SCHEMA = {
    'type': 'object',
    'properties': {
        'network': {'type': 'string'},
        'port': {'type': 'string'},
        'fixed_ip': {'type': 'string'},
        'subnet': {'type': 'string'},
    },
    'additionalProperties': False
}

_INSTANCE_SCHEMA = {
    'type': 'object',
    'properties': {
        'capabilities': {'type': 'object'},
        'conductor_group': {'type': 'string'},
        'hostname': {
            'type': 'string',
            'minLength': 2,
            'maxLength': 255
        },
        'image': _IMAGE_SCHEMA,
        'name': {'type': 'string'},
        'netboot': {'type': 'boolean'},
        'nics': {
            'type': 'array',
            'items': _NIC_SCHEMA
        },
        'passwordless_sudo': {'type': 'boolean'},
        'profile': {'type': 'string'},
        'provisioned': {'type': 'boolean'},
        'resource_class': {'type': 'string'},
        'root_size_gb': {'type': 'integer', 'minimum': 4},
        'ssh_public_keys': {'type': 'string'},
        'swap_size_mb': {'type': 'integer', 'minimum': 64},
        'traits': {
            'type': 'array',
            'items': {'type': 'string'}
        },
        'user_name': {'type': 'string'},
    },
    'additionalProperties': False,
}


_INSTANCES_SCHEMA = {
    'type': 'array',
    'items': _INSTANCE_SCHEMA
}
"""JSON schema of the instances list."""


_ROLES_INPUT_SCHEMA = {
    'type': 'array',
    'items': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'hostname_format': {'type': 'string'},
            'count': {'type': 'integer', 'minimum': 0},
            'defaults': _INSTANCE_SCHEMA,
            'instances': _INSTANCES_SCHEMA,
        },
        'additionalProperties': False,
        'required': ['name'],
    }
}
"""JSON schema of the roles list."""


class BaremetalDeployException(Exception):
    pass


def expand(roles, stack_name, expand_provisioned=True, default_image=None,
           default_network=None, user_name=None, ssh_public_keys=None):

    for role in roles:
        role.setdefault('defaults', {})
        if default_image:
            role['defaults'].setdefault('image', default_image)
        if default_network:
            role['defaults'].setdefault('nics', default_network)
        for inst in role.get('instances', []):
            for k, v in role['defaults'].items():
                inst.setdefault(k, v)

            # Set the default hostname now for duplicate hostname
            # detection during validation
            if 'hostname' not in inst and 'name' in inst:
                inst['hostname'] = inst['name']

    validate_roles(roles)

    instances = []
    hostname_map = {}
    parameter_defaults = {'HostnameMap': hostname_map}
    for role in roles:
        name = role['name']
        hostname_format = build_hostname_format(
            role.get('hostname_format'), name)
        count = role.get('count', 1)
        unprovisioned_indexes = []

        # build a map of all potential generated names
        # with the index number which generates the name
        potential_gen_names = {}
        for index in range(count + len(role.get('instances', []))):
            potential_gen_names[build_hostname(
                hostname_format, index, stack_name)] = index

        # build a list of instances from the specified
        # instances list
        role_instances = []
        for instance in role.get('instances', []):
            inst = {}
            inst.update(instance)

            # create a hostname map entry now if the specified hostname
            # is a valid generated name
            if inst.get('hostname') in potential_gen_names:
                hostname_map[inst['hostname']] = inst['hostname']

            if ssh_public_keys:
                inst['ssh_public_keys'] = ssh_public_keys
            if user_name:
                inst['user_name'] = user_name

            role_instances.append(inst)

        # add generated instance entries until the desired count of
        # provisioned instances is reached
        while len([i for i in role_instances
                   if i.get('provisioned', True)]) < count:
            inst = {}
            inst.update(role['defaults'])
            role_instances.append(inst)

        # NOTE(dtantsur): our hostname format may differ from THT defaults,
        # so override it in the resulting environment
        parameter_defaults['%sDeployedServerHostnameFormat' % name] = (
            hostname_format)

        # ensure each instance has a unique non-empty hostname
        # and a hostname map entry. Also build a list of indexes
        # for unprovisioned instances
        index = 0
        for inst in role_instances:
            provisioned = inst.get('provisioned', True)
            gen_name = None
            hostname = inst.get('hostname')

            if hostname not in hostname_map:
                while (not gen_name
                        or gen_name in hostname_map):
                    gen_name = build_hostname(
                        hostname_format, index, stack_name)
                    index += 1
                inst.setdefault('hostname', gen_name)
                hostname = inst.get('hostname')
                hostname_map[gen_name] = inst['hostname']

            if not provisioned:
                if gen_name:
                    unprovisioned_indexes.append(
                        potential_gen_names[gen_name])
                elif hostname in potential_gen_names:
                    unprovisioned_indexes.append(
                        potential_gen_names[hostname])

        if unprovisioned_indexes:
            parameter_defaults['%sRemovalPolicies' % name] = [{
                'resource_list': unprovisioned_indexes
            }]

        provisioned_count = 0
        for inst in role_instances:
            provisioned = inst.pop('provisioned', True)

            if provisioned:
                provisioned_count += 1

            # Only add instances which match the desired provisioned state
            if provisioned == expand_provisioned:
                instances.append(inst)

        parameter_defaults['%sDeployedServerCount' % name] = (
            provisioned_count)

    validate_instances(instances)
    if expand_provisioned:
        env = {'parameter_defaults': parameter_defaults}
    else:
        env = {}
    return instances, env


def build_hostname_format(hostname_format, role_name):
    if not hostname_format:
        hostname_format = '%stackname%-{}-%index%'.format(
            'novacompute' if role_name == 'Compute' else role_name.lower())
    return hostname_format


def build_hostname(hostname_format, index, stack):
    gen_name = hostname_format.replace('%index%', str(index))
    gen_name = gen_name.replace('%stackname%', stack)
    return gen_name


def validate_instances(instances):
    jsonschema.validate(instances, _INSTANCES_SCHEMA)
    hostnames = set()
    names = set()
    for inst in instances:
        # NOTE(dtantsur): validate image parameters
        get_source(inst)

        if inst.get('hostname'):
            if inst['hostname'] in hostnames:
                raise ValueError('Hostname %s is used more than once' %
                                 inst['hostname'])
            hostnames.add(inst['hostname'])

        if inst.get('name'):
            if inst['name'] in names:
                raise ValueError('Node %s is requested more than once' %
                                 inst['name'])
            names.add(inst['name'])


def validate_roles(roles):
    jsonschema.validate(roles, _ROLES_INPUT_SCHEMA)

    for item in roles:
        count = item.get('count', 1)
        instances = item.get('instances', [])
        instances = [i for i in instances if i.get('provisioned', True)]
        name = item.get('name')
        if len(instances) > count:
            raise ValueError(
                "%s: number of instance entries %s "
                "cannot be greater than count %s" %
                (name, len(instances), count)
            )

        defaults = item.get('defaults', {})
        if 'hostname' in defaults:
            raise ValueError("%s: cannot specify hostname in defaults"
                             % name)
        if 'name' in defaults:
            raise ValueError("%s: cannot specify name in defaults"
                             % name)
        if 'provisioned' in defaults:
            raise ValueError("%s: cannot specify provisioned in defaults"
                             % name)
        if 'instances' in item:
            validate_instances(item['instances'])


def get_source(instance):
    image = instance.get('image', {})
    return sources.detect(image=image.get('href'),
                          kernel=image.get('kernel'),
                          ramdisk=image.get('ramdisk'),
                          checksum=image.get('checksum'))