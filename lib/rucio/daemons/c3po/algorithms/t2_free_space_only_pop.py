# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Thomas Beermann, <thomas.beermann@cern.ch>, 2016

import logging
from operator import itemgetter

from rucio.common.exception import DataIdentifierNotFound
from rucio.core.did import get_did
from rucio.core.replica import list_dataset_replicas
from rucio.core.rse import list_rse_attributes
from rucio.core.rse_expression_parser import parse_expression
from rucio.daemons.c3po.collectors.free_space import FreeSpaceCollector
from rucio.daemons.c3po.utils.popularity import get_popularity
from rucio.db.sqla.constants import ReplicaState


class PlacementAlgorithm:
    """
    Placement algorithm that focusses on free space on T2 DATADISK RSEs.
    """
    def __init__(self):
        self._fsc = FreeSpaceCollector()

        rse_expr = "tier=2&type=DATADISK"
        rse_attrs = parse_expression(rse_expr)

        self._rses = []
        for rse in rse_attrs:
            self._rses.append(rse['rse'])

        self.__setup_penalties()

    def __setup_penalties(self):
        self._penalties = {}
        for rse in self._rses:
            self._penalties[rse] = 1.0

    def __update_penalties(self):
        for rse, penalty in self._penalties.items():
            if penalty > 1.0:
                self._penalties[rse] = penalty - 1

    def place(self, did):
        self.__update_penalties()
        decision = {'did': ':'.join(did)}
        if (not did[0].startswith('data')) and (not did[0].startswith('mc')):
            decision['error_reason'] = 'not a data or mc dataset'
            return decision

        try:
            meta = get_did(did[0], did[1])
        except DataIdentifierNotFound:
            decision['error_reason'] = 'did does not exist'
            return decision
        if meta['length'] is None:
            meta['length'] = 0
        if meta['bytes'] is None:
            meta['bytes'] = 0
        logging.debug('got %s:%s, num_files: %d, bytes: %d' % (did[0], did[1], meta['length'], meta['bytes']))

        decision['length'] = meta['length']
        decision['bytes'] = meta['bytes']

        pop = get_popularity(did)
        decision['popularity'] = pop or 0.0

        if (pop < 10.0):
            decision['error_reason'] = 'did not popular enough'
            return decision

        free_rses = self._rses
        available_reps = []
        reps = list_dataset_replicas(did[0], did[1])
        num_reps = 0
        for rep in reps:
            rse_attr = list_rse_attributes(rep['rse'])
            if 'type' not in rse_attr:
                continue
            if rse_attr['type'] != 'DATADISK':
                continue
            if rep['state'] == ReplicaState.AVAILABLE:
                if rep['rse'] in free_rses:
                    free_rses.remove(rep['rse'])
                available_reps.append(rep['rse'])
                num_reps += 1

        decision['replica_rses'] = available_reps
        decision['num_replicas'] = num_reps
        if num_reps >= 5:
            decision['error_reason'] = 'more than 4 replicas already exist'
            return decision

        rse_ratios = {}
        space_info = self._fsc.get_rse_space()
        for rse in free_rses:
            rse_space = space_info[rse]
            penalty = self._penalties[rse]
            rse_ratios[rse] = float(rse_space['free']) / float(rse_space['total']) * 100.0 / penalty

        sorted_rses = sorted(rse_ratios.items(), key=itemgetter(1), reverse=True)
        decision['destination_rse'] = sorted_rses[0][0]
        decision['rse_ratios'] = sorted_rses
        self._penalties[sorted_rses[0][0]] = 10.0

        return decision
