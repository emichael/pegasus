"""
node.py: Contains classes and functions for managing nodes in the simulator.
"""

from pegasus.config import *
from sortedcontainers import SortedList

def size_distance_to_time(size, distance):
    """
    Calculate message latency based on message length and
    distance between sender and receiver. Currently assume
    propagation delay is 10 * distance, and bandwidth is
    uniformly 10Gbps.
    """
    propg_delay = distance * MIN_PROPG_DELAY
    trans_delay = (size * 8) // (10*10**3)
    return propg_delay + trans_delay


class Rack(object):
    """
    Object representing a single rack in the simulator. Each rack
    contains a set of nodes.
    """
    def __init__(self):
        self._nodes = set()

    def distance(self, rack):
        """
        Distance within the same rack is 1;
        Distance across racks is 2.
        """
        if self is rack:
            return 1
        else:
            return 2

    def add_node(self, node):
        self._nodes.add(node)


class QueuedMessage(object):
    """
    Object representing a queued message in a node's
    message queue.
    """
    def __init__(self, message, time):
        self.message = message
        self.time = time

    def __lt__(self, other):
        return self._time < other._time

    @property
    def message(self):
        return self._message

    @message.setter
    def message(self, message):
        self._message = message

    @property
    def time(self):
        return self._time

    @time.setter
    def time(self, time):
        self._time = time


class Node(object):
    """
    Object representing a single node in the simulator. Each node
    belongs to a single rack.
    """
    def __init__(self, parent):
        self._parent = parent
        self._message_queue = SortedList()
        self._time = 0
        self._app = None

    def _add_to_message_queue(self, message, time):
        self._message_queue.add(QueuedMessage(message, time))

    def register_app(self, app):
        self._app = app

    def distance(self, node):
        return self._parent.distance(node._parent)

    def send_message(self, message, node):
        arrival_time = self._time + size_distance_to_time(message.length(),
                                                          self.distance(node))
        node._add_to_message_queue(message, arrival_time)

    def process_messages(self, end_time):
        """
        Process all queued messages up to ```end_time```
        """
        timer = self._time
        while len(self._message_queue) > 0:
            message = self._message_queue[0]
            if message.time > timer:
                timer = message.time
            timer += PKT_PROC_LTC
            if timer > end_time:
                break

            self._app.process_message(message)
            del self._message_queue[0]

    def execute_app(self, end_time):
        """
        Execute registered application up to ```end_time```
        """
        assert self._app != None
        self._app.execute(end_time)

    def run(self, end_time):
        """
        Run this node up to ```end_time```
        """
        self.process_messages(end_time)
        self.execute_app(end_time)
        self._time = end_time
