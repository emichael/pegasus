"""
memcachekv.py: Memcache style distributed key-value store.
"""

import random
import enum
from sortedcontainers import SortedList
from sortedcontainers import SortedDict
from sortedcontainers import SortedSet
import pyhash

import pegasus.message
import pegasus.config
import pegasus.applications.kv as kv

class MemcacheKVRequest(pegasus.message.Message):
    """
    Request message used by MemcacheKV.
    """
    class MigrationRequest(object):
        def __init__(self, keys, dst):
            self.keys = keys
            self.dst = dst

    def __init__(self, src, req_id, operation, migration_requests=None):
        super().__init__(kv.REQ_ID_LEN + operation.len())
        self.src = src
        self.req_id = req_id
        self.operation = operation
        self.migration_requests = migration_requests


class MemcacheKVReply(pegasus.message.Message):
    """
    Reply message used by MemcacheKV.
    """
    def __init__(self, src, req_id, result, value):
        super().__init__(kv.REQ_ID_LEN + kv.RES_LEN + len(value))
        self.src = src
        self.req_id = req_id
        self.result = result
        self.value = value


class MemcacheMigrationRequest(pegasus.message.Message):
    """
    Migration request sent between cache nodes
    """
    def __init__(self, ops):
        super().__init__(sum(map(lambda x: x.len(), ops)))
        self.ops = ops


class WriteMode(enum.Enum):
    ANYNODE = 1
    UPDATE = 2
    INVALIDATE = 3


class MappedNodes(object):
    def __init__(self, dst_nodes, migration_requests):
        self.dst_nodes = dst_nodes
        self.migration_requests = migration_requests


class KeyRate(object):
    def __init__(self, count=0, time=0):
        self.count = count
        self.time = time

    def rate(self):
        if self.time == 0 or self.count <= 1:
            return 0
        return self.count / (self.time / 1000000)

    def __eq__(self, other):
        if isinstance(other, KeyRate):
            return self.rate() == other.rate()
        else:
            return False

    def __lt__(self, other):
        return self.rate() < other.rate()


class MemcacheKVConfiguration(pegasus.config.Configuration):
    """
    Abstract configuration class. Subclass of ``MemcacheKVConfiguration``
    should implement ``key_to_nodes``.
    """
    def __init__(self, cache_nodes, db_node, write_mode):
        super().__init__()
        self.cache_nodes = cache_nodes
        self.db_node = db_node
        self.write_mode = write_mode
        self.report_load = False
        self.report_interval = 0

    def run(self, end_time):
        pass

    def key_to_nodes(self, key, op_type):
        """
        Return node/nodes the ``key`` is mapped to.
        Return type should be ``MappedNodes``.
        """
        raise NotImplementedError

    def report_op_send(self, node, op, time):
        """
        (Client) reporting that it is sending an op to ``node``.
        """
        pass

    def report_op_receive(self, node):
        """
        (Client) reporting that it has received a reply from ``node``.
        """
        pass

    def report_migration(self, key, dst):
        """
        (Server) reporting that ``key`` has migrated to ``dst``.
        ``dst`` is node id.
        """
        pass


class StaticConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode):
        super().__init__(cache_nodes, db_node, write_mode)

    def key_to_nodes(self, key, op_type):
        return MappedNodes([self.cache_nodes[hash(key) % len(self.cache_nodes)]],
                           None)


class LoadBalanceConfig(MemcacheKVConfiguration):
    class KeyRequestRate(object):
        def __init__(self, key, request_rate):
            self.key = key
            self.request_rate = request_rate

        def __eq__(self, other):
            if isinstance(other, KeyRequestRate):
                return self.key == other.key and self.request_rate == other.request_rate
            else:
                return False

        def __lt__(self, other):
            return self.request_rate < other.request_rate

    class NodeRequestRate(object):
        def __init__(self, id, request_rate):
            self.id = id
            self.request_rate = request_rate

        def __eq__(self, other):
            if isinstance(other, NodeRequestRate):
                return self.id == other.id and self.request_rate == other.request_rate
            else:
                return False

        def __lt__(self, other):
            return self.request_rate < other.request_rate

    def __init__(self, cache_nodes, db_node, write_mode, max_request_rate, report_interval):
        super().__init__(cache_nodes, db_node, write_mode)
        self.key_node_map = {} # key -> nodes
        self.agg_key_request_rate = {}
        self.max_request_rate = max_request_rate
        self.report_load = True
        self.report_interval = report_interval
        self.last_load_rebalance_time = 0

    def run(self, end_time):
        if end_time - self.last_load_rebalance_time >= self.report_interval:
            self.collect_load(end_time - self.last_load_rebalance_time)
            self.rebalance_load()
            self.last_load_rebalance_time = end_time

    def key_to_nodes(self, key, op_type):
        return MappedNodes(self.key_node_map.setdefault(key, [self.cache_nodes[hash(key) % len(self.cache_nodes)]]),
                           None)

    def collect_load(self, interval):
        for node in self.cache_nodes:
            for key, count in node._app._key_request_counter.items():
                rate = self.agg_key_request_rate.get(key, 0)
                self.agg_key_request_rate[key] = rate + round(count / (interval / 1000000))
            node._app._key_request_counter.clear()

    def rebalance_load(self):
        # Construct sorted key request rates and node request rates
        sorted_krr = SortedList()
        sorted_nrr = SortedList()
        for key, rate in self.agg_key_request_rate.items():
            sorted_krr.add(self.KeyRequestRate(key, rate))
        for node in self.cache_nodes:
            sorted_nrr.add(self.NodeRequestRate(node.id, 0))

        # Try to add the most loaded key to the least loaded node.
        # If not possible, replicate the key.
        while len(sorted_krr) > 0:
            krr = sorted_krr.pop()
            nrr = sorted_nrr.pop(0)

            if nrr.request_rate + krr.request_rate <= self.max_request_rate:
                self.key_node_map[krr.key] = [self.cache_nodes[nrr.id]]
                nrr.request_rate += krr.request_rate
                sorted_nrr.add(nrr)
            else:
                # Replicate the key until the request rate fits (or reaches max replication)
                nrrs = [nrr]
                while len(sorted_nrr) > 0:
                    nrrs.append(sorted_nrr.pop(0))
                    request_rate = krr.request_rate // len(nrrs)
                    fit = True
                    for nrr in nrrs:
                        if nrr.request_rate + request_rate > self.max_request_rate:
                            fit = False
                            break
                    if fit:
                        break
                # Update sorted_nrr and key node map
                nodes = []
                for nrr in nrrs:
                    nodes.append(self.cache_nodes[nrr.id])
                    nrr.request_rate += (krr.request_rate // len(nrrs))
                    sorted_nrr.add(nrr)
                self.key_node_map[krr.key] = nodes

        # Clear aggregate request rate
        self.agg_key_request_rate.clear()


class BoundedLoadConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode, c):
        super().__init__(cache_nodes, db_node, write_mode)
        self.c = c
        self.outstanding_requests = {} # node id -> number of outstanding requests
        self.key_node_map = {} # key -> node
        for node in self.cache_nodes:
            self.outstanding_requests[node.id] = 0

    def key_hash(self, key):
        return hash(key)

    def key_to_nodes(self, key, op_type):
        if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
            node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
            return MappedNodes([self.cache_nodes[node_id]], None)
        else:
            # For GET requests, migrate the key if the mapped node is
            # above the bounded load
            total_load = sum(self.outstanding_requests.values())
            expected_load = (self.c * total_load) / len(self.cache_nodes)
            node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
            if self.outstanding_requests[node_id] <= expected_load:
                return MappedNodes([self.cache_nodes[node_id]], None)

            # Current mapped node is over-loaded, find the next
            # node that is below the bounded load (migrate key)
            next_node_id = (node_id + 1) % len(self.cache_nodes)
            while self.outstanding_requests[next_node_id] > expected_load:
                next_node_id = (next_node_id + 1) % len(self.cache_nodes)
            assert node_id != next_node_id
            self.key_node_map[key] = next_node_id
            return MappedNodes([self.cache_nodes[node_id]],
                               [MemcacheKVRequest.MigrationRequest([key], self.cache_nodes[next_node_id])])

    def report_op_send(self, node, op, time):
        self.outstanding_requests[node.id] += 1

    def report_op_receive(self, node):
        self.outstanding_requests[node.id] -= 1


class BoundedIPLoadConfig(MemcacheKVConfiguration):
    class Mode(enum.Enum):
        ILOAD = 1
        PLOAD = 2
        IPLOAD = 3

    def __init__(self, cache_nodes, db_node, write_mode, c, mode):
        super().__init__(cache_nodes, db_node, write_mode)
        self.c = c
        self.mode = mode
        self.key_node_map = {} # key -> node id
        self.key_rates = {} # key -> KeyRate
        self.iloads = {} # node id -> instantaneous load
        self.ploads = {} # node id -> projected load
        for node in self.cache_nodes:
            self.iloads[node.id] = 0
            self.ploads[node.id] = 0

    def key_hash(self, key):
        return hash(key)

    def key_to_nodes(self, key, op_type):
        if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
            node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
            return MappedNodes([self.cache_nodes[node_id]], None)
        else:
            # For GET requests, migrate the key if the mapped node is
            # exceeding the bounded iload and/or the bounded pload,
            # depending on the mode.
            node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
            total_iload = sum(self.iloads.values())
            expected_iload = (self.c * total_iload) / len(self.cache_nodes)
            total_pload = sum(self.ploads.values())
            expected_pload = (self.c * total_pload) / len(self.cache_nodes)

            if self.mode == self.Mode.ILOAD or self.mode == self.Mode.IPLOAD:
                if self.iloads[node_id] <= expected_iload:
                    return MappedNodes([self.cache_nodes[node_id]], None)

            if self.mode == self.Mode.PLOAD or self.mode == self.Mode.IPLOAD:
                if self.ploads[node_id] <= expected_pload:
                    return MappedNodes([self.cache_nodes[node_id]], None)

            # Current mapped node is overloaded, find a node
            # to migrate to.
            if self.mode == self.Mode.ILOAD:
                next_node_id = min(self.iloads, key=self.iloads.get)
            elif self.mode == self.Mode.PLOAD:
                next_node_id = min(self.ploads, key=self.ploads.get)
            elif self.mode == self.Mode.IPLOAD:
                # For IPLOAD, we need to find a node that has both
                # iload and pload below the bounded load.
                node_found = False
                for next_node_id in sorted(self.ploads, key=self.ploads.get):
                    if self.ploads[next_node_id] > expected_pload:
                        break
                    if self.iloads[next_node_id] <= expected_iload:
                        node_found = True
                        break
                if not node_found:
                    return MappedNodes([self.cache_nodes[node_id]], None)

            assert node_id != next_node_id
            self.key_node_map[key] = next_node_id
            # Update ploads on both nodes
            key_rate = self.key_rates.get(key, KeyRate())
            self.ploads[node_id] -= key_rate.rate()
            self.ploads[next_node_id] += key_rate.rate()

            return MappedNodes([self.cache_nodes[node_id]],
                               [MemcacheKVRequest.MigrationRequest([key], self.cache_nodes[next_node_id])])

    def report_op_send(self, node, op, time):
        self.iloads[node.id] += 1
        key_rate = self.key_rates.setdefault(op.key, KeyRate())
        old_rate = key_rate.rate()
        key_rate.count += 1
        key_rate.time = time
        node_id = self.key_node_map.get(op.key, self.key_hash(op.key) % len(self.cache_nodes))
        self.ploads[node_id] += (key_rate.rate() - old_rate)

    def report_op_receive(self, node):
        self.iloads[node.id] -= 1


class BoundedAverageLoadConfig(MemcacheKVConfiguration):
    class AverageLoad(object):
        def __init__(self):
            self.count = 0
            self.time = 0

        def load(self):
            if self.time == 0 or self.count <= 1:
                return 0
            return self.count / (self.time / 1000000)

    def __init__(self, cache_nodes, db_node, write_mode, c):
        super().__init__(cache_nodes, db_node, write_mode)
        self.c = c
        self.key_node_map = {} # key -> node id
        self.average_load = {} # node id -> AverageLoad
        for node in self.cache_nodes:
            self.average_load[node.id] = self.AverageLoad()

    def key_hash(self, key):
        return hash(key)

    def key_to_nodes(self, key, op_type):
        if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
            node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
            return MappedNodes([self.cache_nodes[node_id]], None)
        else:
            # For GET requests, migrate the key if the mapped node is
            # exceeding bounded average load
            node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
            total_load = sum(item.load() for item in self.average_load.values())
            expected_load = (self.c * total_load) / len(self.cache_nodes)
            if self.average_load[node_id].load() <= expected_load:
                return MappedNodes([self.cache_nodes[node_id]], None)

            # Current mapped node is overloaded, migrate the key to
            # the node with the lowest average load
            next_node_id = min(self.average_load, key=lambda x: self.average_load.get(x).load())

            assert node_id != next_node_id
            self.key_node_map[key] = next_node_id

            return MappedNodes([self.cache_nodes[node_id]],
                               [MemcacheKVRequest.MigrationRequest([key], self.cache_nodes[next_node_id])])

    def report_op_send(self, node, op, time):
        self.average_load[node.id].count += 1
        self.average_load[node.id].time = time


class RoutingConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode, c):
        super().__init__(cache_nodes, db_node, write_mode)
        self.c = c
        self.key_node_map = {} # key -> node id
        self.key_rates = {} # key -> KeyRate
        self.iloads = {} # node id -> instantaneous load
        self.ploads = {} # node id -> projected load
        for node in self.cache_nodes:
            self.iloads[node.id] = 0
            self.ploads[node.id] = 0

    def key_hash(self, key):
        return hash(key)

    def key_to_nodes(self, key, op_type):
        node_id = self.key_node_map.get(key, self.key_hash(key) % len(self.cache_nodes))
        return MappedNodes([self.cache_nodes[node_id]], None)

    def report_op_send(self, node, op, time):
        self.iloads[node.id] += 1
        key_rate = self.key_rates.setdefault(op.key, KeyRate())
        old_rate = key_rate.rate()
        key_rate.count += 1
        key_rate.time = time
        node_id = self.key_node_map.get(op.key, self.key_hash(op.key) % len(self.cache_nodes))
        self.ploads[node_id] += (key_rate.rate() - old_rate)

    def report_op_receive(self, node):
        self.iloads[node.id] -= 1

    def report_migration(self, key, dst):
        self.key_node_map[key] = dst


class DynamicCHConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode, c, hash_space):
        super().__init__(cache_nodes, db_node, write_mode)
        self.c = c
        self.hash_space = hash_space
        self.key_hash_ring = SortedDict()
        self.node_hash_ring = SortedDict()
        self.node_hashes = {}
        self.key_rates = {}
        self.iloads = {}
        self.ploads = {}
        for node in self.cache_nodes:
            self.iloads[node.id] = 0
            self.ploads[node.id] = 0
            node_hash = node.id * (hash_space // len(cache_nodes))
            self.node_hashes[node.id] = node_hash
            self.node_hash_ring[node_hash] = node.id

    def key_hash_fn(self, key):
        return hash(key)

    def lookup_node(self, key):
        key_hash = self.key_hash_fn(key) % self.hash_space
        for (node_hash, node_id) in self.node_hash_ring.items():
            if node_hash >= key_hash:
                return node_id
        return self.node_hash_ring.peekitem(0)[1]

    def install_key(self, key):
        key_hash = self.key_hash_fn(key) % self.hash_space
        keys = self.key_hash_ring.setdefault(key_hash, set())
        keys.add(key)

    def remove_key(self, key):
        key_hash = self.key_hash_fn(key) % self.hash_space
        keys = self.key_hash_ring.get(key_hash, set())
        keys.discard(key)

    def search_migration_keys(self, node_id, starting_hash, agg_pload, target_pload, migration_keys):
        mapped_key_hashes = list(self.key_hash_ring.irange(minimum=0, maximum=starting_hash, reverse=True))
        new_node_hash = None
        for key_hash in mapped_key_hashes:
            if key_hash in self.node_hash_ring:
                assert self.node_hash_ring[key_hash] == node_id

            prev_hash = (key_hash - 1) % self.hash_space
            if prev_hash in self.node_hash_ring:
                # Never collide two nodes on the hash ring!
                assert self.node_hash_ring[prev_hash] != node_id
                new_node_hash = key_hash
                break

            for key in self.key_hash_ring[key_hash]:
                agg_pload += self.key_rates[key].rate()
                if agg_pload >= target_pload:
                    new_node_hash = prev_hash
                migration_keys.append(key)
            if new_node_hash is not None:
                break
        return (agg_pload, new_node_hash)

    def rehash_node(self, node_id, target_pload):
        node_hash = self.node_hashes[node_id]
        migration_dst = None
        # Find next node in the hash ring to migrate to
        for (next_node_hash, next_node_id) in self.node_hash_ring.items():
            if next_node_hash > node_hash:
                migration_dst = next_node_id
                break
        if migration_dst is None:
            migration_dst = self.node_hash_ring.peekitem(0)[1]
        assert migration_dst != node_id

        # Find set of keys to migrate, and the new node hash
        migration_keys = []
        (agg_pload, new_node_hash) = self.search_migration_keys(node_id, node_hash, 0, target_pload, migration_keys)
        if new_node_hash is None:
            # Search beyond hash 0
            (agg_pload, new_node_hash) = self.search_migration_keys(node_id, self.hash_space - 1, agg_pload, target_pload, migration_keys)
            assert new_node_hash is not None

        if new_node_hash == node_hash:
            # We cannot find a new hash. This is due to
            # the next hash on the ring already occupied
            # by another node.
            assert len(migration_keys) == 0
            return None

        # Update node hash
        self.node_hashes[node_id] = new_node_hash
        self.node_hash_ring.pop(node_hash)
        self.node_hash_ring[new_node_hash] = node_id

        # Update pload
        self.ploads[node_id] -= agg_pload
        self.ploads[migration_dst] += agg_pload

        return [MemcacheKVRequest.MigrationRequest(keys = migration_keys,
                                                   dst = self.cache_nodes[migration_dst])]

    def key_to_nodes(self, key, op_type):
        if op_type == kv.Operation.Type.PUT or op_type == kv.Operation.Type.DEL:
            if op_type == kv.Operation.Type.PUT:
                self.install_key(key)
            elif op_type == kv.Operation.Type.DEL:
                self.remove_key(key)
            node_id = self.lookup_node(key)
            return MappedNodes([self.cache_nodes[node_id]], None)
        else:
            node_id = self.lookup_node(key)
            total_iload = sum(self.iloads.values())
            expected_iload = (self.c * total_iload) / len(self.cache_nodes)
            total_pload = sum(self.ploads.values())
            expected_pload = (self.c * total_pload) / len(self.cache_nodes)

            if self.iloads[node_id] <= expected_iload or self.ploads[node_id] <= expected_pload:
                return MappedNodes([self.cache_nodes[node_id]], None)

            pload_diff = self.ploads[node_id] - expected_pload
            migration_requests = self.rehash_node(node_id, pload_diff)
            return MappedNodes([self.cache_nodes[node_id]], migration_requests)

    def report_op_send(self, node, op, time):
        self.iloads[node.id] += 1
        node_id = self.lookup_node(op.key)
        key_rate = self.key_rates.setdefault(op.key, KeyRate())
        old_rate = key_rate.rate()
        key_rate.count += 1
        key_rate.time = time
        self.ploads[node_id] += (key_rate.rate() - old_rate)

    def report_op_receive(self, node):
        self.iloads[node.id] -= 1


class CoTConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode):
        super().__init__(cache_nodes, db_node, write_mode)
        self.node_loads = {}
        self.hash_fn = pyhash.fnv1_32()
        self.hash_seed_a = 0
        self.hash_seed_b = 1
        for node in self.cache_nodes:
            self.node_loads[node.id] = 0

    def key_hash_fn_a(self, key):
        return self.hash_fn(key, seed=self.hash_seed_a)

    def key_hash_fn_b(self, key):
        return self.hash_fn(key, seed=self.hash_seed_b)

    def key_to_nodes(self, key, op_type):
        dst_node_ids = set([self.key_hash_fn_a(key) % len(self.cache_nodes),
                            self.key_hash_fn_b(key) % len(self.cache_nodes)])

        if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
            dst_nodes = [self.cache_nodes[node_id] for node_id in dst_node_ids]
            return MappedNodes(dst_nodes, None)
        else:
            # For GET requests, pick one of the two nodes which has the least load
            dst_node = [self.cache_nodes[min(dst_node_ids, key=self.node_loads.get)]]
            return MappedNodes(dst_node, None)

    def report_op_send(self, node, op, time):
        self.node_loads[node.id] += 1

    def report_op_receive(self, node):
        self.node_loads[node.id] -= 1


class CoNConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode, n):
        super().__init__(cache_nodes, db_node, write_mode)
        self.node_loads = {}
        self.n = n
        for node in self.cache_nodes:
            self.node_loads[node.id] = 0

    def key_hash_fn(self, key):
        return hash(key)

    def key_to_nodes(self, key, op_type):
        key_hash = self.key_hash_fn(key)
        dst_node_ids = set(map(lambda x: x % len(self.cache_nodes),
                               range(key_hash, key_hash + self.n)))
        if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
            dst_nodes = [self.cache_nodes[node_id] for node_id in dst_node_ids]
            return MappedNodes(dst_nodes, None)
        else:
            # For GET requests, pick the node which has the least load
            dst_node = [self.cache_nodes[min(dst_node_ids, key=self.node_loads.get)]]
            return MappedNodes(dst_node, None)

    def report_op_send(self, node, op, time):
        self.node_loads[node.id] += 1

    def report_op_receive(self, node):
        self.node_loads[node.id] -= 1


class TopKeysReplicatedConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode, nrkeys, nreplicas):
        super().__init__(cache_nodes, db_node, write_mode)
        self.nrkeys = nrkeys
        self.nreplicas = nreplicas
        self.node_loads = {}
        self.key_rates = {}
        self.replicated_keys = SortedSet(key=lambda x: self.key_rates[x].rate())
        for node in self.cache_nodes:
            self.node_loads[node.id] = 0

    def key_hash_fn(self, key):
        return hash(key)

    def key_to_nodes(self, key, op_type):
        key_hash = self.key_hash_fn(key)
        if key in self.replicated_keys:
            dst_node_ids = set(map(lambda x: x % len(self.cache_nodes),
                                   range(key_hash, key_hash + self.nreplicas)))
        else:
            dst_node_ids = [key_hash % len(self.cache_nodes)]

        if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
            dst_nodes = [self.cache_nodes[node_id] for node_id in dst_node_ids]
        else:
            # For GET requests, pick the node which has the least load
            dst_nodes = [self.cache_nodes[min(dst_node_ids, key=self.node_loads.get)]]

        return MappedNodes(dst_nodes, None)

    def update_replicated_keys(self, key, rate):
        if self.nrkeys <= 0:
            return
        if key in self.replicated_keys:
            # already replicated
            return
        if len(self.replicated_keys) < self.nrkeys:
            self.replicated_keys.add(key)
        else:
            if rate > self.key_rates[self.replicated_keys[0]].rate():
                del self.replicated_keys[0]
                self.replicated_keys.add(key)

    def report_op_send(self, node, op, time):
        self.node_loads[node.id] += 1
        key_rate = self.key_rates.setdefault(op.key, KeyRate())
        key_rate.count += 1
        key_rate.time = time
        self.update_replicated_keys(op.key, key_rate.rate())

    def report_op_receive(self, node):
        self.node_loads[node.id] -= 1


class DynamicTKRConfig(MemcacheKVConfiguration):
    def __init__(self, cache_nodes, db_node, write_mode, nrkeys, c):
        super().__init__(cache_nodes, db_node, write_mode)
        self.nrkeys = nrkeys
        self.c = c
        self.node_loads = {}
        self.key_rates = {}
        self.replicated_keys = SortedSet(key=lambda x: self.key_rates[x].rate())
        self.key_node_map = {}
        self.hash_fn = pyhash.fnv1_32()
        for node in self.cache_nodes:
            self.node_loads[node.id] = 0

    def key_hash_fn(self, key):
        return self.hash_fn(key)

    def key_to_nodes(self, key, op_type):
        key_hash = self.key_hash_fn(key)
        migration_requests = None
        if key in self.replicated_keys:
            if op_type == kv.Operation.Type.DEL or op_type == kv.Operation.Type.PUT:
                # For PUT and DEL requests, forward to ANY node that
                # has the least load, and update the key mapping
                dst_node_id = min(self.node_loads, key=self.node_loads.get)
                self.key_node_map[key] = set([dst_node_id])
            else:
                # For GET requests, among all updated nodes, pick the one with the least
                # load. If min load exceeds the bound, replicate on one more node (if not
                # already replicated on all nodes)
                dst_node_id = min(self.key_node_map[key], key=self.node_loads.get)
                if len(self.key_node_map[key]) < len(self.cache_nodes):
                    bounded_load = self.c * (sum(self.node_loads.values()) / len(self.cache_nodes))
                    if self.node_loads[dst_node_id] > bounded_load:
                        min_node_id = min(self.node_loads, key=self.node_loads.get)
                        assert min_node_id != dst_node_id
                        self.key_node_map[key].add(min_node_id)
                        migration_requests = [MemcacheKVRequest.MigrationRequest([key],
                                                                                 self.cache_nodes[min_node_id])]
        else:
            # Non-replicated keys, forward to consistent hashing mapped node
            dst_node_id = key_hash % len(self.cache_nodes)

        return MappedNodes([self.cache_nodes[dst_node_id]], migration_requests)

    def add_replicated_key(self, key):
        self.replicated_keys.add(key)
        self.key_node_map[key] = set([self.key_hash_fn(key) % len(self.cache_nodes)])

    def update_replicated_keys(self, key, rate):
        if self.nrkeys <= 0:
            return
        if key in self.replicated_keys:
            # already replicated
            return
        assert key not in self.key_node_map
        if len(self.replicated_keys) < self.nrkeys:
            self.add_replicated_key(key)
        else:
            if rate > self.key_rates[self.replicated_keys[0]].rate():
                del self.key_node_map[self.replicated_keys[0]]
                del self.replicated_keys[0]
                self.add_replicated_key(key)

    def report_op_send(self, node, op, time):
        self.node_loads[node.id] += 1
        key_rate = self.key_rates.setdefault(op.key, KeyRate())
        key_rate.count += 1
        key_rate.time = time
        self.update_replicated_keys(op.key, key_rate.rate())

    def report_op_receive(self, node):
        self.node_loads[node.id] -= 1


class MemcacheKVClient(kv.KV):
    """
    Implementation of a memcache style distributed key-value store client.
    """
    class PendingRequest(kv.KV.PendingRequest):
        def __init__(self, operation, time):
            super().__init__(operation, time)
            self.received_acks = 0
            self.expected_acks = 0

    def __init__(self, generator, stats):
        super().__init__(generator, stats)

    def _execute(self, op, time):
        """
        Always send the operation to a remote node. Client nodes
        in a MemcacheKV are stateless, and do not store kv pairs.
        If the key is replicated on multiple cache nodes
        (key_to_nodes returns multiple nodes), pick one node in
        random for GET requests, and send to all replicated nodes
        for DEL requests. PUT request handling depends on the
        write_mode.
        """
        mapped_nodes = self._config.key_to_nodes(op.key, op.op_type)
        pending_req = self.PendingRequest(operation = op, time = time)
        msg = MemcacheKVRequest(src = self._node,
                                req_id = self._next_req_id,
                                operation = op,
                                migration_requests = mapped_nodes.migration_requests)
        if op.op_type == kv.Operation.Type.GET:
            node = random.choice(mapped_nodes.dst_nodes)
            self._node.send_message(msg, node, time)
            self._config.report_op_send(node, op, time)
        elif op.op_type == kv.Operation.Type.PUT:
            write_nodes = []
            inval_nodes = []
            if self._config.write_mode == WriteMode.ANYNODE:
                write_nodes = [random.choice(mapped_nodes.dst_nodes)]
            elif self._config.write_mode == WriteMode.UPDATE:
                write_nodes = mapped_nodes.dst_nodes
            elif self._config.write_mode == WriteMode.INVALIDATE:
                write_nodes = mapped_nodes.dst_nodes[:1]
                inval_nodes = mapped_nodes.dst_nodes[1:]
            else:
                raise ValueError("Invalid write mode")
            for node in write_nodes:
                self._node.send_message(msg, node, time)
                self._config.report_op_send(node, op, time)
            inval_msg = MemcacheKVRequest(src = self._node,
                                          req_id = self._next_req_id,
                                          operation = kv.Operation(op_type = kv.Operation.Type.DEL,
                                                                   key = op.key))
            for node in inval_nodes:
                self._node.send_message(inval_msg, node, time)
                self._config.report_op_send(node, inval_msg.operation, time)
            pending_req.expected_acks = len(write_nodes) + len(inval_nodes)
        elif op.op_type == kv.Operation.Type.DEL:
            for node in mapped_nodes.dst_nodes:
                self._node.send_message(msg, node, time)
                self._config.report_op_send(node, op, time)
            pending_req.expected_acks = len(mapped_nodes.dst_nodes)
        else:
            raise ValueError("Invalid operation type")

        self._pending_requests[self._next_req_id] = pending_req
        self._next_req_id += 1

    def _process_message(self, message, time):
        if isinstance(message, MemcacheKVReply):
            self._config.report_op_receive(message.src)
            request = self._pending_requests[message.req_id]
            if request.operation.op_type == kv.Operation.Type.GET:
                self._complete_request(message.req_id, message.result, time)
            else:
                request.received_acks += 1
                if request.received_acks >= request.expected_acks:
                    self._complete_request(message.req_id, kv.Result.OK, time)
        else:
            raise ValueError("Invalid message type")

    def _complete_request(self, req_id, result, time):
        request = self._pending_requests.pop(req_id)
        self._stats.report_op(request.operation.op_type,
                              time - request.time,
                              result == kv.Result.OK)


class MemcacheKVServer(kv.KV):
    """
    Implementation of a memcache style distributed key-value store server.
    """
    def __init__(self, generator, stats):
        super().__init__(generator, stats)
        self._key_request_counter = {}
        self._last_load_report_time = 0

    def _process_message(self, message, time):
        if isinstance(message, MemcacheKVRequest):
            if self._config.report_load:
                count = self._key_request_counter.get(message.operation.key, 0) + 1
                self._key_request_counter[message.operation.key] = count
            result, value = self._execute_op(message.operation)
            reply = MemcacheKVReply(src = self._node,
                                    req_id = message.req_id,
                                    result = result,
                                    value = value)
            self._node.send_message(reply, message.src, time)

            if message.migration_requests is not None:
                for request in message.migration_requests:
                    if len(request.keys) > 0:
                        ops = []
                        for key in request.keys:
                            ops.append(kv.Operation(op_type = kv.Operation.Type.PUT,
                                                    key = key,
                                                    value = self._store.get(key, "")))
                        msg = MemcacheMigrationRequest(ops)
                        self._node.send_message(msg, request.dst, time)
        elif isinstance(message, MemcacheMigrationRequest):
            for op in message.ops:
                self._execute_op(op)
        else:
            raise ValueError("Invalid message type")


class MemcacheKVMigrationServer(kv.KV):
    """
    Implementation of a memcache style distributed key-value store server.
    Server actively migrates keys when overloaded.
    """
    def __init__(self, generator, stats):
        super().__init__(generator, stats)
        self._migrated_keys = set() # keys that have already migrated

    def _check_load_and_migrate(self, key, time):
        # If key is already migrated (but the routing is not updated yet),
        # do not migrate again
        if key in self._migrated_keys:
            return

        # Check if node is overloaded. If yes, migrate key value pair
        # to another node.
        total_iload = sum(self._config.iloads.values())
        expected_iload = (self._config.c * total_iload) / len(self._config.cache_nodes)
        total_pload = sum(self._config.ploads.values())
        expected_pload = (self._config.c * total_pload) / len(self._config.cache_nodes)

        if self._config.iloads[self._node.id] <= expected_iload:
            return
        if self._config.ploads[self._node.id] <= expected_pload:
            return

        for dst_node_id in sorted(self._config.ploads, key=self._config.ploads.get):
            if self._config.ploads[dst_node_id] > expected_pload:
                return
            if self._config.iloads[dst_node_id] <= expected_iload:
                break

        assert dst_node_id != self._node.id
        key_rate = self._config.key_rates.get(key, KeyRate())
        self._config.ploads[self._node.id] -= key_rate.rate()
        self._config.ploads[dst_node_id] += key_rate.rate()
        request = MemcacheMigrationRequest(ops = [kv.Operation(op_type = kv.Operation.Type.PUT,
                                                               key = key,
                                                               value = self._store.get(key, ""))])
        self._node.send_message(request, self._config.cache_nodes[dst_node_id], time)
        self._migrated_keys.add(key)

    def _process_message(self, message, time):
        if isinstance(message, MemcacheKVRequest):
            # Always execute the request regardless
            # of node's load.
            result, value = self._execute_op(message.operation)

            reply = MemcacheKVReply(src = self._node,
                                    req_id = message.req_id,
                                    result = result,
                                    value = value)
            self._node.send_message(reply, message.src, time)

            # Only do migration for GET and PUT requests.
            if message.operation.op_type == kv.Operation.Type.GET or \
                    message.operation.op_type == kv.Operation.Type.PUT:
                self._check_load_and_migrate(message.operation.key, time)
        elif isinstance(message, MemcacheMigrationRequest):
            for op in message.ops:
                self._execute_op(op)
                # Notify config that migration is completed
                self._config.report_migration(op.key,
                                              self._node.id)
                # If we have migrated this key away before, remove it
                # from the migrated keys list
                self._migrated_keys.discard(op.key)
        else:
            raise ValueError("Invalid message type")
