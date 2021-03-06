"""
simulator.py: Top level simulator.
"""

import copy

import pegasus.node
import pegasus.application
import pegasus.param as param

class Simulator(object):
    def __init__(self, stats, progress=False):
        self._nodes = []
        self._stats = stats
        self._progress = progress
        self._config = None

    def add_node(self, node):
        self._nodes.append(node)

    def add_nodes(self, nodes):
        self._nodes.extend(nodes)

    def register_config(self, config):
        self._config = config

    def run(self, duration):
        """
        Run the simulator for ``duration`` usecs.
        """
        timer = param.MIN_PROPG_DELAY
        while timer <= duration:
            for node in self._nodes:
                node.run(timer)
            self._config.run(timer)
            self._stats.run(timer)
            timer += param.MIN_PROPG_DELAY

        self._stats.report_end_time(timer)
