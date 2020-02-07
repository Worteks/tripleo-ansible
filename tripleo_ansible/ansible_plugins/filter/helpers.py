#!/usr/bin/env python
# Copyright 2019 Red Hat, Inc.
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

import ast
import json
import six

from collections import OrderedDict
from operator import itemgetter


# cmp() doesn't exist on python3
if six.PY3:
    def cmp(a, b):
        return 0 if a == b else 1


class FilterModule(object):
    def filters(self):
        return {
            'singledict': self.singledict,
            'subsort': self.subsort,
            'needs_delete': self.needs_delete,
            'haskey': self.haskey,
            'list_of_keys': self.list_of_keys,
            'container_exec_cmd': self.container_exec_cmd
        }

    def subsort(self, dict_to_sort, attribute, null_value=0):
        """Sort a hash from a sub-element.

        This filter will return an dictionary ordered by the attribute
        part of each item.
        """
        for k, v in dict_to_sort.items():
            if attribute not in v:
                dict_to_sort[k][attribute] = null_value

        data = {}
        for d in dict_to_sort.items():
            if d[1][attribute] not in data:
                data[d[1][attribute]] = []
            data[d[1][attribute]].append({d[0]: d[1]})

        sorted_list = sorted(
            data.items(),
            key=lambda x: x[0]
        )
        ordered_dict = {}
        for o, v in sorted_list:
            ordered_dict[o] = v
        return ordered_dict

    def singledict(self, list_to_convert):
        """Generate a single dictionary from a list of dictionaries.

        This filter will return a single dictionary from a list of
        dictionaries.
        """
        return_dict = {}
        for i in list_to_convert:
            return_dict.update(i)
        return return_dict

    def needs_delete(self, container_infos, config, config_id):
        """Returns a list of containers which need to be removed.

        This filter will check which containers need to be removed for these
        reasons: no config_data, updated config_data or container not
        part of the global config.
        """
        to_delete = []
        to_skip = []
        installed_containers = []
        for c in container_infos:
            c_name = c['Name']
            installed_containers.append(c_name)

            # Don't delete containers not managed by tripleo-ansible
            if (c['Config']['Labels'] is None
                    or c['Config']['Labels'].get(
                        'managed_by') != 'tripleo_ansible'):
                to_skip += [c_name]
                continue

            # Only remove containers managed in this config_id
            if (c['Config']['Labels'] is None
                    or c['Config']['Labels'].get('config_id') != config_id):
                to_skip += [c_name]
                continue

            # Remove containers with no config_data
            # e.g. broken config containers
            if (c['Config']['Labels'] is not None
                    and 'config_data' not in c['Config']['Labels']):
                to_delete += [c_name]
                continue

        for c_name, config_data in config.items():
            # don't try to remove a container which doesn't exist
            if c_name not in installed_containers:
                continue

            # already tagged to be removed
            if c_name in to_delete:
                continue

            if c_name in to_skip:
                continue

            # Remove containers managed by tripleo-ansible when config_data
            # changed. Since we already cleaned the containers not in config,
            # this check needs to be in that loop.
            # e.g. new TRIPLEO_CONFIG_HASH during a minor update
            try:
                c_facts = [c['Config']['Labels']['config_data']
                           for c in container_infos if c_name == c['Name']]
            except KeyError:
                continue

            # Build c_facts so it can be compared later with config_data
            c_facts = ast.literal_eval(c_facts[0]) if (
                                       len(c_facts)) == 1 else dict()

            if cmp(c_facts, config_data) != 0:
                to_delete += [c_name]

        return to_delete

    def haskey(self, batched_container_data, attribute, value=None,
               reverse=False, any=False):
        """Return container data with a specific config key.

        This filter will take a list of dictionaries (batched_container_data)
        and will return the dictionnaries which have a certain key given
        in parameter with 'attribute'.
        If reverse is set to True, the returned list won't contain dictionaries
        which have the attribute.
        If any is set to True, the returned list will match any value in
        the list of values for "value" parameter which has to be a list.
        """
        return_list = []
        for container in batched_container_data:
            for k, v in json.loads(json.dumps(container)).items():
                if attribute in v and not reverse:
                    if value is None:
                        return_list.append({k: v})
                    else:
                        if isinstance(value, list) and any:
                            if v[attribute] in value:
                                return_list.append({k: v})
                        elif any:
                            raise TypeError("value has to be a list if any is "
                                            "set to True.")
                        else:
                            if v[attribute] == value:
                                return_list.append({k: v})
                if attribute not in v and reverse:
                    return_list.append({k: v})
        return return_list

    def list_of_keys(self, keys_to_list):
        """Return a list of keys from a list of dictionaries.

        This filter takes in input a list of dictionaries and for each of them
        it will add the key to list_of_keys and returns it.
        """
        list_of_keys = []
        for i in keys_to_list:
            for k, v in i.items():
                list_of_keys.append(k)
        return list_of_keys

    def list_or_dict_arg(self, data, cmd, key, arg):
        """Utility to build a command and its argument with list or dict data.

        The key can be a dictionary or a list, the returned arguments will be
        a list where each item is the argument name and the item data.
        """
        if key not in data:
            return
        value = data[key]
        if isinstance(value, dict):
            for k, v in sorted(value.items()):
                if v:
                    cmd.append('%s=%s=%s' % (arg, k, v))
                elif k:
                    cmd.append('%s=%s' % (arg, k))
        elif isinstance(value, list):
            for v in value:
                if v:
                    cmd.append('%s=%s' % (arg, v))

    def container_exec_cmd(self, data, cli='podman'):
        """Return a list of all the arguments to execute a container exec.

        This filter takes in input the container exec data and the cli name
        to return the full command in a list of arguments that will be used
        by Ansible command module.
        """
        cmd = [cli, 'exec']
        cmd.append('--user=%s' % data.get('user', 'root'))
        if 'privileged' in data:
            cmd.append('--privileged=%s' % str(data['privileged']).lower())
        self.list_or_dict_arg(data, cmd, 'environment', '--env')
        cmd.extend(data['command'])
        return cmd
