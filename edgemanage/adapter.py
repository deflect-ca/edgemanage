"""
Non-commandline adapter, work with Django
"""
import logging
import logging.handlers
import os
import yaml
import uuid

from edgemanage import util
from datetime import datetime


class EdgemanageAdapter(object):

    def __init__(self, config_path, dnet=None):
        """
        Init adapter with `config`, `edge_list`
        """

        # load config
        with open(config_path) as config_f:
            self.config = yaml.safe_load(config_f.read())

        # load edge list
        if dnet is not None:
            with open(os.path.join(self.config["edgelist_dir"], dnet)) as edge_f:
                self.edge_list = [i.strip() for i in edge_f.read().split("\n")
                                  if i.strip() and not i.startswith("#")]

        # init var
        self.lock_f = None

    def get_config(self, config_str):
        return self.config[config_str] if config_str in self.config else None

    def edge_data_exist(self, edgename):
        return os.path.exists(os.path.join(self.config["healthdata_store"],
                                           "%s.edgestore" % edgename))

    def log_edge_conf(self, edgename, mode, comment):
        """
        edge_conf logger wrap
        """
        handler = logging.handlers.SysLogHandler(
            facility=logging.handlers.SysLogHandler.LOG_DAEMON)
        logger = logging.getLogger('edge_conf')
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.info("Edge %s changed mode to %s with comment %s",
                    edgename, mode, comment)

    def lock_edge_conf(self):
        """
        Create a lock file for edge_conf
        """
        self.lock_f = open(self.config["lockfile"], "w")

        if not util.acquire_lock(self.lock_f):
            return False, "Couldn't acquire edge_conf lockfile"

        return True

    def unlock_edge_conf(self):
        """
        Close the lock file
        """
        self.lock_f.close()

    def dnet_query(self):
        """
        ls edgelist_dir
        """
        dnets = []
        listdir = os.listdir(self.config["edgelist_dir"])
        for dnet in listdir:
            if not dnet.startswith('.'):
                dnets.append(dnet)

        return dnets

    def dump_dnet_and_edges(self, mapping):
        """ Dump data into file

            mapping: {
                'dnet1': [
                    'host1',
                    'host2
                ],
                'dnet2': [
                    'host3'
                ]
            }
        """
        dnets = list(mapping.keys())
        ret = {
            'existing_dnets': self.dnet_query(),
            'created': [],
            'deleted': [],
            'updated': [],
            'error': []
        }

        # diffs
        for opcode, dnet in self.set_reconcile(ret['existing_dnets'], dnets):
            if opcode == 'create':
                content, ref_id = self.edgelist_to_file_str(mapping[dnet])
                print(f"#{ref_id} Attempt to create dnet [{dnet}]:", mapping[dnet])

                status, err = self.safe_file_write(dnet, content)
                if status:
                    print(f"#{ref_id} dnet [{dnet}] create success")
                    ret['created'].append(dnet)
                else:
                    print(f"#{ref_id} dnet [{dnet}] create FAILED")
                    ret['error'].append(dnet)

            elif opcode == 'delete':
                try:
                    os.remove(f"{self.config['edgelist_dir']}/{dnet}")
                    ret['deleted'].append(dnet)
                    print(f"dnet [{dnet}] delete success")
                except OSError:
                    print(f"dnet [{dnet}] delete FAILED")

        # update common
        common_dnets = list(set(ret['existing_dnets']).intersection(set(dnets)))
        for dnet in common_dnets:
            content, ref_id = self.edgelist_to_file_str(mapping[dnet])
            print(f"#{ref_id} Attempt to update dnet [{dnet}]:", mapping[dnet])

            status, err = self.safe_file_write(dnet, content)
            if status:
                print(f"#{ref_id} dnet [{dnet}] update success")
                ret['updated'].append(dnet)
            else:
                print(f"#{ref_id} dnet [{dnet}] update FAILED")
                ret['error'].append(dnet)

        return ret

    def safe_file_write(self, filename, content):
        try:
            path = f"{self.config['edgelist_dir']}/{filename}"
            with open(path, "w") as outfile:
                outfile.write(content)
                outfile.close()
        except IOError as err:
            return False, str(err)
        return True, None

    def gen_ref_id(self):
        """ Generate a ref id to be written in file and log """
        return str(uuid.uuid4())[:8]

    def edgelist_to_file_str(self, edgelist):
        """ Convert list of edge into file content """
        ref_id = self.gen_ref_id()
        file_str = f"# Generated by deflect-core on {str(datetime.now())}\n"
        file_str += f"# Ref ID: {ref_id}\n"

        for edge in edgelist:
            file_str += f"{edge}\n"

        return file_str, ref_id

    def set_reconcile(self, src_seq, dst_seq):
        """ Return required operations to mutate src_seq into dst_seq """
        src_set = set(src_seq)
        dst_set = set(dst_seq)

        for item in src_set - dst_set:
            yield 'delete', item

        for item in dst_set - src_set:
            yield 'create', item
