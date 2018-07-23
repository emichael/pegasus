import argparse
import json
import time
import socket
import threading
import struct
import signal
import os

from sortedcontainers import SortedDict
from sortedcontainers import SortedList

from res_pd_rpc.ttypes import *
from ptf.thriftutils import *
from pegasus.p4_pd_rpc.ttypes import *
import pegasus.p4_pd_rpc.pegasus
import conn_mgr_pd_rpc.conn_mgr

from thrift.transport import TSocket
from thrift.transport import TTransport
from thrift.protocol import TBinaryProtocol
from thrift.protocol import TMultiplexedProtocol

HASH_MASK = 0x3
RNODE_NONE = 0x7F
MAX_RSET_SIZE = 0x4
BUF_SIZE = 4096

CTRL_ADDR = ("10.100.1.6", 24680)

IDENTIFIER = 0xDEAC
IDENTIFIER_SIZE = 2
TYPE_ERROR = -1
TYPE_RESET_REQ = 0x0
TYPE_RESET_REPLY = 0x1
TYPE_HK_REPORT = 0x2
TYPE_SIZE = 1
NNODES_SIZE = 2
NKEYS_SIZE = 2
KEYHASH_SIZE = 4
LOAD_SIZE = 2
MAX_NRKEYS = 8

controller = None

def signal_handler(signum, frame):
    print "Received INT/TERM signal...Exiting"
    #controller.stop()
    os._exit(1)

# Messages
class ResetRequest(object):
    def __init__(self, num_nodes):
        self.num_nodes = num_nodes

class ResetReply(object):
    def __init__(self, ack):
        self.ack = ack

class Report(object):
    def __init__(self, keyhash, load):
        self.keyhash = keyhash
        self.load = load

class HKReport(object):
    def __init__(self):
        # sorted list of Reports
        self.reports = SortedList(key=lambda x : x.load)

class ReplicatedKey(object):
    def __init__(self, index, load):
        self.index = index
        self.load = load
        self.nodes = set()


# Message handler
class MessageHandler(threading.Thread):
    def __init__(self, ctrl_addr):
        threading.Thread.__init__(self)
        self.ctrl_sk = socket.socket(type=socket.SOCK_DGRAM)
        self.ctrl_sk.bind(ctrl_addr)

    def decode_msg(self, buf):
        index = 0
        identifier = struct.unpack('<H', buf[index:index+IDENTIFIER_SIZE])[0]
        if identifier != IDENTIFIER:
            return (TYPE_ERROR, None)
        index += IDENTIFIER_SIZE
        msg_type = struct.unpack('<B', buf[index:index+TYPE_SIZE])[0]
        index += TYPE_SIZE
        if msg_type == TYPE_RESET_REQ:
            num_nodes = struct.unpack('<H', buf[index:index+NNODES_SIZE])[0]
            msg = ResetRequest(num_nodes)
        elif msg_type == TYPE_HK_REPORT:
            nkeys = struct.unpack('<H', buf[index:index+NKEYS_SIZE])[0]
            index += NKEYS_SIZE
            msg = HKReport()
            for _ in range(nkeys):
                keyhash = struct.unpack('I', buf[index:index+KEYHASH_SIZE])[0]
                index += KEYHASH_SIZE
                load = struct.unpack('H', buf[index:index+LOAD_SIZE])[0]
                index += LOAD_SIZE
                msg.reports.add(Report(keyhash, load))
        else:
            msg_type = TYPE_ERROR
        return (msg_type, msg)

    def handle_reset_request(self, addr, msg):
        global controller
        print "Reset request with", msg.num_nodes, "nodes"
        controller.clear_rkeys()

    def handle_hk_report(self, addr, msg):
        controller.handle_hk_report(msg.reports)

    def run(self):
        while True:
            (buf, addr) = self.ctrl_sk.recvfrom(BUF_SIZE)
            (msg_type, msg) = self.decode_msg(buf)
            if msg_type == TYPE_RESET_REQ:
                self.handle_reset_request(addr, msg)
            elif msg_type == TYPE_HK_REPORT:
                self.handle_hk_report(addr, msg)


# Main controller
class Controller(object):
    def __init__(self, thrift_server):
        self.transport = TSocket.TSocket(thrift_server, 9090)
        self.transport = TTransport.TBufferedTransport(self.transport)
        bprotocol = TBinaryProtocol.TBinaryProtocol(self.transport)
        conn_mgr_protocol = TMultiplexedProtocol.TMultiplexedProtocol(bprotocol, "conn_mgr")
        self.conn_mgr = conn_mgr_pd_rpc.conn_mgr.Client(conn_mgr_protocol)
        p4_protocol = TMultiplexedProtocol.TMultiplexedProtocol(bprotocol, "pegasus")
        self.client = pegasus.p4_pd_rpc.pegasus.Client(p4_protocol)
        self.transport.open()

        self.sess_hdl = self.conn_mgr.client_init()
        self.dev = 0
        self.dev_tgt = DevTarget_t(self.dev, hex_to_i16(0xFFFF))

        self.write_reg_rnode_fns = []
        self.write_reg_rnode_fns.append(self.client.register_write_reg_rnode_1)
        self.write_reg_rnode_fns.append(self.client.register_write_reg_rnode_2)
        self.write_reg_rnode_fns.append(self.client.register_write_reg_rnode_3)
        self.write_reg_rnode_fns.append(self.client.register_write_reg_rnode_4)
        self.read_reg_rnode_fns = []
        self.read_reg_rnode_fns.append(self.client.register_read_reg_rnode_1)
        self.read_reg_rnode_fns.append(self.client.register_read_reg_rnode_2)
        self.read_reg_rnode_fns.append(self.client.register_read_reg_rnode_3)
        self.read_reg_rnode_fns.append(self.client.register_read_reg_rnode_4)

        # keyhash -> ReplicatedKey (sorted in ascending load)
        self.replicated_keys = SortedDict(lambda x : self.replicated_keys[x].load)
        self.node_load = {} # index -> load

    def install_table_entries(self, tables):
        # tab_l2_forward
        self.client.tab_l2_forward_set_default_action__drop(
            self.sess_hdl, self.dev_tgt)
        for (mac, port) in tables["tab_l2_forward"].items():
            self.client.tab_l2_forward_table_add_with_l2_forward(
                self.sess_hdl, self.dev_tgt,
                pegasus_tab_l2_forward_match_spec_t(
                    ethernet_dstAddr = macAddr_to_string(mac)),
                pegasus_l2_forward_action_spec_t(
                    action_port = port))
        # tab_node_forward
        self.client.tab_node_forward_set_default_action__drop(
            self.sess_hdl, self.dev_tgt)
        for (node, attrs) in tables["tab_node_forward"].items():
            self.client.tab_node_forward_table_add_with_node_forward(
                self.sess_hdl, self.dev_tgt,
                pegasus_tab_node_forward_match_spec_t(
                    pegasus_node = int(node)),
                pegasus_node_forward_action_spec_t(
                    action_mac_addr = macAddr_to_string(attrs["mac"]),
                    action_ip_addr = ipv4Addr_to_i32(attrs["ip"]),
                    action_udp_addr = attrs["udp"],
                    action_port = attrs["port"]))
            self.node_load[int(node)] = 0
        # tab_replicated_keys, reg_rnode (1-4)
        for (keyhash, rkey_index) in tables["tab_replicated_keys"].items():
            self.add_rkey(keyhash, rkey_index, 0)
        # reg_node_id (1-4)
        for i in range(4):
            self.client.register_write_reg_node_id_1(
                self.sess_hdl, self.dev_tgt, i,
                pegasus_reg_node_id_1_value_t(
                    f0 = i,
                    f1 = 0))
            self.client.register_write_reg_node_id_2(
                self.sess_hdl, self.dev_tgt, i,
                pegasus_reg_node_id_2_value_t(
                    f0 = i,
                    f1 = 0))
            self.client.register_write_reg_node_id_3(
                self.sess_hdl, self.dev_tgt, i,
                pegasus_reg_node_id_3_value_t(
                    f0 = i,
                    f1 = 0))
            self.client.register_write_reg_node_id_4(
                self.sess_hdl, self.dev_tgt, i,
                pegasus_reg_node_id_4_value_t(
                    f0 = i,
                    f1 = 0))

        self.conn_mgr.complete_operations(self.sess_hdl)

    def read_registers(self):
        flags = pegasus_register_flags_t(read_hw_sync=True)
        # read node load
        for index in self.node_load.keys():
            read_value = self.client.register_read_reg_node_load_1(
                self.sess_hdl, self.dev_tgt, index, flags)
            self.node_load[index] = read_value[1]
        # read replicated key nodes
        for rkey in self.replicated_keys.values():
            rkey.nodes = set()
            for i in range(4):
                read_value = self.read_reg_rnode_fns[i](
                    self.sess_hdl, self.dev_tgt, rkey.index, flags)
                node = read_value[1]
                if node == RNODE_NONE:
                    break
                rkey.nodes.add(node)

    def try_expand_rset(self):
        # calculate average load
        avg_load = sum(self.node_load.values()) / len(self.node_load)
        min_node = min(self.node_load, key=self.node_load.get)
        switch_update = False
        # check if we need to expand any replication set
        for rkey in self.replicated_keys.values():
            if len(rkey.nodes) < MAX_RSET_SIZE:
                min_load = min(map(self.node_load.get, rkey.nodes))
                if min_load > avg_load:
                    # XXX should send a migration message to
                    # a node in rset
                    self.write_reg_rnode_fns[len(rkey.nodes)](
                        self.sess_hdl, self.dev_tgt, rkey.index, min_node)
                    switch_update = True
        if switch_update:
            self.conn_mgr.complete_operations(self.sess_hdl)

    def clear_rkeys(self):
        for keyhash in self.replicated_keys.keys():
            self.client.tab_replicated_keys_table_delete_by_match_spec(
                self.sess_hdl, self.dev_tgt,
                pegasus_tab_replicated_keys_match_spec_t(
                    pegasus_keyhash = keyhash))
        self.replicated_keys.clear()
        self.conn_mgr.complete_operations(self.sess_hdl)

    def add_rkey(self, keyhash, rkey_index, load):
        self.client.tab_replicated_keys_table_add_with_lookup_rkey(
            self.sess_hdl, self.dev_tgt,
            pegasus_tab_replicated_keys_match_spec_t(
                pegasus_keyhash = int(keyhash)),
            pegasus_lookup_rkey_action_spec_t(
                action_rkey_index = rkey_index))
        for i in range(4):
            if i == 0:
                node = int(keyhash) & HASH_MASK
            else:
                node = RNODE_NONE
            self.write_reg_rnode_fns[i](
                self.sess_hdl, self.dev_tgt, rkey_index, node)
        self.replicated_keys.setdefault(keyhash, ReplicatedKey(index=int(rkey_index),
                                                               load=load))

    def handle_hk_report(self, reports):
        switch_update = False
        for report in list(reversed(reports)):
            # Iterate reports in reverse order (highest load first).
            if report.keyhash in self.replicated_keys:
                # XXX For now, just update the load if the key
                # is already replicated
                if report.load > self.replicated_keys[report.keyhash].load:
                    self.replicated_keys[report.keyhash].load = report.load
            else:
                if len(self.replicated_keys) < MAX_NRKEYS:
                    print "Adding key", report.keyhash, "to rkeys, load", report.load, ", size now:", len(self.replicated_keys)
                    self.add_rkey(report.keyhash, len(self.replicated_keys), report.load)
                    switch_update = True
                else:
                    (target_keyhash, target_rkey) = self.replicated_keys.peekitem(0)
                    if report.load > target_rkey.load:
                        # Replace the least loaded key in rkeys
                        print "Replacing key", target_keyhash, "load", target_rkey.load, " with key", report.keyhash, "load", report.load
                        self.client.tab_replicated_keys_table_delete_by_match_spec(
                            self.sess_hdl, self.dev_tgt,
                            pegasus_tab_replicated_keys_match_spec_t(
                                pegasus_keyhash = target_keyhash))
                        self.replicated_keys.popitem(0)
                        self.add_rkey(report.keyhash, target_rkey.index, report.load)
                        switch_update = True
                    else:
                        # Can skip the rest of the reports, because we
                        # are iterating in descending order
                        break
        if switch_update:
            self.conn_mgr.complete_operations(self.sess_hdl)

    def print_registers(self):
        print "node load:", self.node_load
        print "replicated keys:"
        for (keyhash, rkey) in self.replicated_keys.items():
            print "keyhash", keyhash, "nodes", rkey.nodes

    def run(self):
        while True:
            self.read_registers()
            self.try_expand_rset()
            time.sleep(1)

    def stop(self):
        self.transport.close()


def main():
    global controller
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",
                        required=True,
                        help="configuration (JSON) file")
    args = parser.parse_args()
    with open(args.config) as fp:
        tables = json.load(fp)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    controller = Controller("oyster")
    handler = MessageHandler(CTRL_ADDR)

    controller.install_table_entries(tables)
    handler.start()
    controller.run()
    handler.join()
    controller.stop()


if __name__ == "__main__":
    main()
