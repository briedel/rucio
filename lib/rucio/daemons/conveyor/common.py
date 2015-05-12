# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Mario Lassnig, <mario.lassnig@cern.ch>, 2014-2015
# - Cedric Serfon, <cedric.serfon@cern.ch>, 2014
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2014
# - Wen Guan, <wen.guan@cern.ch>, 2014-2015
# - Martin Barisits, <martin.barisits@cern.ch>, 2014

"""
Methods common to different conveyor daemons.
"""

import datetime
import logging
import os
import sys
import time
import traceback

from dogpile.cache import make_region
from dogpile.cache.api import NoValue

from re import match
from requests.exceptions import RequestException
from sqlalchemy.exc import DatabaseError
from urlparse import urlparse

from rucio.common import exception
from rucio.common.exception import DatabaseException, UnsupportedOperation, ReplicaNotFound
from rucio.core import replica as replica_core, request as request_core, rse as rse_core
from rucio.core.message import add_message
from rucio.core.monitor import record_timer, record_counter
from rucio.db.constants import DIDType, RequestState, ReplicaState, RequestType
from rucio.db.session import read_session, transactional_session
from rucio.rse import rsemanager

region = make_region().configure('dogpile.cache.memory', expiration_time=3600)


@transactional_session
def update_requests_states(responses, session=None):
    """
    Bulk version used by poller and consumer to update the internal state of requests,
    after the response by the external transfertool.

    :param reqs: List of (req, response) tuples.
    :param session: The database session to use.
    """

    for response in responses:
        update_request_state(response=response, session=session)


@transactional_session
def update_request_state(response, session=None):
    """
    Used by poller and consumer to update the internal state of requests,
    after the response by the external transfertool.

    :param response: The transfertool response dictionary, retrieved via request.query_request().
    :param session: The database session to use.
    :returns commit_or_rollback: Boolean.
    """

    try:
        if not response['new_state']:
            request_core.touch_request(response['request_id'], session=session)
            return False
        else:
            request = request_core.get_request(response['request_id'], session=session)
            if request and request['external_id'] == response['transfer_id'] and request['state'] != response['new_state']:
                response['external_host'] = request['external_host']
                transfer_id = response['transfer_id'] if 'transfer_id' in response else None
                logging.debug('UPDATING REQUEST %s FOR TRANSFER %s STATE %s' % (str(response['request_id']), transfer_id, str(response['new_state'])))
                request_core.set_request_state(response['request_id'], response['new_state'], transfer_id, transferred_at=response.get('transferred_at', None), session=session)

                add_monitor_message(response, session=session)
                return True
            elif not request:
                logging.debug("Request %s doesn't exist, will not update" % (response['request_id']))
                return False
            elif request['external_id'] != response['transfer_id']:
                logging.debug("Reponse %s with transfer id %s is different from the request transfer id %s, will not update" % (response['request_id'], response['transfer_id'], request['external_id']))
                return False
            else:
                logging.debug("Request %s is already in %s state, will not update" % (response['request_id'], response['new_state']))
                return False
    except exception.UnsupportedOperation, e:
        logging.warning("Request %s doesn't exist - Error: %s" % (response['request_id'], str(e).replace('\n', '')))
        return False


def touch_transfer(external_host, transfer_id):
    """
    Used by poller and consumer to update the internal state of requests,
    after the response by the external transfertool.

    :param request_host: Name of the external host.
    :param transfer_id: external transfer job id as a string.
    :returns commit_or_rollback: Boolean.
    """

    try:
        request_core.touch_transfer(external_host, transfer_id)
    except exception.UnsupportedOperation, e:
        logging.warning("Transfer %s on %s doesn't exist - Error: %s" % (transfer_id, external_host, str(e).replace('\n', '')))
        return False


@transactional_session
def set_transfer_state(external_host, transfer_id, state, session=None):
    """
    Used by poller to update the internal state of transfer,
    after the response by the external transfertool.

    :param request_host: Name of the external host.
    :param transfer_id: external transfer job id as a string.
    :param state: request state as a string.
    :param session: The database session to use.
    :returns commit_or_rollback: Boolean.
    """

    try:
        request_core.set_transfer_state(external_host, transfer_id, state, session=session)
        if state == RequestState.LOST:
            reqs = request_core.get_requests_by_transfer(external_host, transfer_id, session=session)
            for req in reqs:
                logging.debug('REQUEST %s OF TRANSFER %s ON %s STATE %s' % (str(req['request_id']), external_host, transfer_id, str(state)))
                response = {'new_state': state,
                            'transfer_id': transfer_id,
                            'job_state': state,
                            'src_url': None,
                            'dst_url': req['dest_url'],
                            'duration': 0,
                            'reason': "The FTS job lost",
                            'scope': req.get('scope', None),
                            'name': req.get('name', None),
                            'src_rse': req.get('src_rse', None),  # Todo for multiple source replicas
                            'dst_rse': req.get('dst_rse', None),
                            'request_id': req.get('request_id', None),
                            'activity': req.get('activity', None),
                            'dest_rse_id': req.get('dest_rse_id', None),
                            'previous_attempt_id': req.get('previous_attempt_id', None),
                            'adler32': req.get('adler32', None),
                            'md5': req.get('md5', None),
                            'filesize': req.get('filesize', None),
                            'external_host': external_host,
                            'job_m_replica': None,
                            'details': None}

                add_monitor_message(response, session=session)
        return True
    except exception.UnsupportedOperation, e:
        logging.warning("Transfer %s on %s doesn't exist - Error: %s" % (transfer_id, external_host, str(e).replace('\n', '')))
        return False


def get_undeterministic_rses():
    key = 'undeterministic_rses'
    result = region.get(key)
    if type(result) is NoValue:
        rses_list = rse_core.list_rses(filters={'deterministic': 0})
        result = [rse['id'] for rse in rses_list]
        try:
            region.set(key, result)
        except:
            logging.warning("Failed to set dogpile cache, error: %s" % (rse['id'], traceback.format_exc()))
    return result


def handle_requests(reqs):
    """
    used by finisher to handle terminated requests,

    :param reqs: List of requests.
    """

    undeterministic_rses = get_undeterministic_rses()
    rses_info, protocols = {}, {}
    replicas = {}
    for req in reqs:
        try:
            replica = {'scope': req['scope'], 'name': req['name'], 'rse_id': req['dest_rse_id'], 'bytes': req['bytes'], 'adler32': req['adler32'], 'request_id': req['request_id']}

            replica['pfn'] = req['dest_url']
            replica['request_type'] = req['request_type']

            if req['request_type'] not in replicas:
                replicas[req['request_type']] = {}
            if req['rule_id'] not in replicas[req['request_type']]:
                replicas[req['request_type']][req['rule_id']] = []

            if req['state'] == RequestState.DONE:
                replica['state'] = ReplicaState.AVAILABLE
                replica['archived'] = False
                replicas[req['request_type']][req['rule_id']].append(replica)

                # for TAPE, replica path is needed
                if req['request_type'] == RequestType.TRANSFER and req['dest_rse_id'] in undeterministic_rses:
                    if req['dest_rse_id'] not in rses_info:
                        dest_rse = rse_core.get_rse_name(rse_id=req['dest_rse_id'])
                        rses_info[req['dest_rse_id']] = rsemanager.get_rse_info(dest_rse)
                    pfn = req['dest_url']
                    scheme = urlparse(pfn).scheme
                    dest_rse_id_scheme = '%s_%s' % (req['dest_rse_id'], scheme)
                    if dest_rse_id_scheme not in protocols:
                        protocols[dest_rse_id_scheme] = rsemanager.create_protocol(rses_info[req['dest_rse_id']], 'write', scheme)
                    path = protocols[dest_rse_id_scheme].parse_pfns([pfn])[pfn]['path']
                    replica['path'] = os.path.join(path, os.path.basename(pfn))
            elif req['state'] == RequestState.FAILED or req['state'] == RequestState.LOST:
                if request_core.should_retry_request(req):
                    tss = time.time()
                    new_req = request_core.requeue_and_archive(req['request_id'])
                    record_timer('daemons.conveyor.common.update_request_state.request-requeue_and_archive', (time.time()-tss)*1000)
                    logging.warn('REQUEUED DID %s:%s REQUEST %s AS %s TRY %s' % (req['scope'],
                                                                                 req['name'],
                                                                                 req['request_id'],
                                                                                 new_req['request_id'],
                                                                                 new_req['retry_count']))
                else:
                    logging.warn('EXCEEDED DID %s:%s REQUEST %s' % (req['scope'], req['name'], req['request_id']))
                    replica['state'] = ReplicaState.UNAVAILABLE
                    replica['archived'] = False
                    replicas[req['request_type']][req['rule_id']].append(replica)
            elif req['state'] == RequestState.SUBMITTING:
                if req['updated_at'] < (datetime.datetime.utcnow()-datetime.timedelta(seconds=1800)) and request_core.should_retry_request(req):
                    tss = time.time()
                    new_req = request_core.requeue_and_archive(req['request_id'])
                    record_timer('daemons.conveyor.common.update_request_state.request-requeue_and_archive', (time.time()-tss)*1000)
                    logging.warn('REQUEUED SUBMITTING DID %s:%s REQUEST %s AS %s TRY %s' % (req['scope'],
                                                                                            req['name'],
                                                                                            req['request_id'],
                                                                                            new_req['request_id'],
                                                                                            new_req['retry_count']))
                else:
                    logging.warn('EXCEEDED SUBMITTING DID %s:%s REQUEST %s' % (req['scope'], req['name'], req['request_id']))
                    replica['state'] = ReplicaState.UNAVAILABLE
                    replica['archived'] = False
                    replicas[req['request_type']][req['rule_id']].append(replica)
        except:
            logging.error("Something unexpected happened when handling request %s(%s:%s) at %s: %s" % (req['request_id'],
                                                                                                       req['scope'],
                                                                                                       req['name'],
                                                                                                       req['dest_rse_id'],
                                                                                                       traceback.format_exc()))

    handle_terminated_replicas(replicas)


def handle_terminated_replicas(replicas):
    """
    Used by finisher to handle available and unavailable replicas.

    :param replicas: List of replicas.
    """

    for req_type in replicas:
        for rule_id in replicas[req_type]:
            try:
                handle_bulk_replicas(replicas[req_type][rule_id], req_type, rule_id)
            except (UnsupportedOperation, ReplicaNotFound):
                # one replica in the bulk cannot be found. register it one by one
                for replica in replicas[req_type][rule_id]:
                    try:
                        handle_one_replica(replica, req_type, rule_id)
                    except (DatabaseException, DatabaseError), e:
                        if isinstance(e.args[0], tuple) and (match('.*ORA-00054.*', e.args[0][0]) or ('ERROR 1205 (HY000)' in e.args[0][0])):
                            logging.warn("Locks detected when handling replica %s:%s at RSE %s" % (replica['scope'], replica['name'], replica['rse_id']))
                        else:
                            logging.error("Could not finish handling replicas %s:%s at RSE %s (%s)" % (replica['scope'], replica['name'], replica['rse_id'], traceback.format_exc()))
                    except:
                        logging.error("Something unexpected happened when updating replica state for transfer %s:%s at %s (%s)" % (replica['scope'],
                                                                                                                                   replica['name'],
                                                                                                                                   replica['rse_id'],
                                                                                                                                   traceback.format_exc()))
            except (DatabaseException, DatabaseError), e:
                if isinstance(e.args[0], tuple) and (match('.*ORA-00054.*', e.args[0][0]) or ('ERROR 1205 (HY000)' in e.args[0][0])):
                    logging.warn("Locks detected when handling replicas on %s rule %s, update updated time." % (req_type, rule_id))
                    try:
                        request_core.touch_requests_by_rule(rule_id)
                    except (DatabaseException, DatabaseError), e:
                        logging.error("Failed to touch requests by rule(%s): %" % (rule_id, traceback.format_exc()))
                else:
                    logging.error("Could not finish handling replicas on %s rule %s: %s" % (req_type, rule_id, traceback.format_exc()))
            except:
                logging.error("Something unexpected happened when handling replicas on %s rule %s: %s" % (req_type, rule_id, traceback.format_exc()))


@transactional_session
def handle_bulk_replicas(replicas, req_type, rule_id, session=None):
    """
    Used by finisher to handle available and unavailable replicas blongs to same rule in bulk way.

    :param replicas: List of replicas.
    :param req_type: Request type: STAGEIN, STAGEOUT, TRANSFER.
    :param rule_id: RULE id.
    :param session: The database session to use.
    :returns commit_or_rollback: Boolean.
    """
    try:
        replica_core.update_replicas_states(replicas, nowait=True, session=session)
    except ReplicaNotFound, ex:
        logging.warn('Failed to bulk update replicas, will do it one by one: %s' % str(ex))
        raise ReplicaNotFound(ex)

    for replica in replicas:
        if not replica['archived']:
            request_core.archive_request(replica['request_id'], session=session)
        logging.info("HANDLED REQUEST %s DID %s:%s AT RSE %s STATE %s" % (replica['request_id'], replica['scope'], replica['name'], replica['rse_id'], str(replica['state'])))
    return True


@transactional_session
def handle_one_replica(replica, req_type, rule_id, session=None):
    """
    Used by finisher to handle a replica.

    :param replica: replica as a dictionary.
    :param req_type: Request type: STAGEIN, STAGEOUT, TRANSFER.
    :param rule_id: RULE id.
    :param session: The database session to use.
    :returns commit_or_rollback: Boolean.
    """

    try:
        replica_core.update_replicas_states([replica], nowait=True, session=session)
        if not replica['archived']:
            request_core.archive_request(replica['request_id'], session=session)
        logging.info("HANDLED REQUEST %s DID %s:%s AT RSE %s STATE %s" % (replica['request_id'], replica['scope'], replica['name'], replica['rse_id'], str(replica['state'])))
    except (UnsupportedOperation, ReplicaNotFound), ex:
        logging.warn("ERROR WHEN HANDLING REQUEST %s DID %s:%s AT RSE %s STATE %s: %s" % (replica['request_id'], replica['scope'], replica['name'], replica['rse_id'], str(replica['state']), str(ex)))
        # replica cannot be found. register it and schedule it for deletion
        try:
            if replica['state'] == ReplicaState.AVAILABLE and replica['request_type'] != RequestType.STAGEIN:
                logging.info("Replica cannot be found. Adding a replica %s:%s AT RSE %s with tombstone=utcnow" % (replica['scope'], replica['name'], replica['rse_id']))
                rse = rse_core.get_rse(rse=None, rse_id=replica['rse_id'], session=session)
                replica_core.add_replica(rse['rse'],
                                         replica['scope'],
                                         replica['name'],
                                         replica['bytes'],
                                         pfn=replica['pfn'] if 'pfn' in replica else None,
                                         account='root',  # it will deleted immediately, do we need to get the accurate account from rule?
                                         adler32=replica['adler32'],
                                         tombstone=datetime.datetime.utcnow(),
                                         session=session)
            if not replica['archived']:
                request_core.archive_request(replica['request_id'], session=session)
            logging.info("HANDLED REQUEST %s DID %s:%s AT RSE %s STATE %s" % (replica['request_id'], replica['scope'], replica['name'], replica['rse_id'], str(replica['state'])))
        except:
            logging.error('Cannot register replica for DID %s:%s at RSE %s - potential dark data' % (replica['scope'],
                                                                                                     replica['name'],
                                                                                                     replica['rse_id']))
            raise

    return True


@transactional_session
def handle_submitting_requests(older_than=1800, process=None, total_processes=None, thread=None, total_threads=None, session=None):
    """
    used by finisher to handle submitting  requests

    :param older_than: Only select requests older than this DateTime.
    :param process: Identifier of the caller process as an integer.
    :param total_processes: Maximum number of processes as an integer.
    :param thread: Identifier of the caller thread as an integer.
    :param total_threads: Maximum number of threads as an integer.
    :param session: The database session to use.
    """

    reqs = request_core.get_next(request_type=[RequestType.TRANSFER, RequestType.STAGEIN, RequestType.STAGEOUT],
                                 state=RequestState.SUBMITTING,
                                 older_than=datetime.datetime.utcnow()-datetime.timedelta(seconds=older_than),
                                 process=process, total_processes=total_processes,
                                 thread=thread, total_threads=total_threads,
                                 session=session)
    for req in reqs:
        logging.info("Requeue SUBMITTING request %s" % (req['request_id']))
        request_core.requeue_and_archive(req['request_id'], session=session)


@read_session
def get_source_rse(scope, name, src_url, session=None):
    try:
        scheme = src_url.split(":")[0]
        replications = replica_core.list_replicas([{'scope': scope, 'name': name, 'type': DIDType.FILE}], schemes=[scheme], unavailable=True, session=session)
        for source in replications:
            for source_rse in source['rses']:
                for pfn in source['rses'][source_rse]:
                    if pfn == src_url:
                        return source_rse
        # cannot find matched surl
        logging.warn('Cannot get correct RSE for source url: %s' % (src_url))
        return None
    except:
        logging.error('Cannot get correct RSE for source url: %s(%s)' % (src_url, sys.exc_info()[1]))
        return None


@read_session
def add_monitor_message(response, session=None):
    if response['new_state'] == RequestState.DONE:
        transfer_status = 'transfer-done'
    elif response['new_state'] == RequestState.FAILED:
        transfer_status = 'transfer-failed'
    elif response['new_state'] == RequestState.LOST:
        transfer_status = 'transfer-lost'

    activity = response.get('activity', None)
    src_rse = response.get('src_rse', None)
    src_url = response.get('src_url', None)
    dst_rse = response.get('dst_rse', None)
    dst_url = response.get('dst_url', None)
    dst_protocol = dst_url.split(':')[0] if dst_url else None
    reason = response.get('reason', None)
    duration = response.get('duration', -1)
    filesize = response.get('filesize', None)
    md5 = response.get('md5', None)
    adler32 = response.get('adler32', None)
    scope = response.get('scope', None)
    name = response.get('name', None)
    job_m_replica = response.get('job_m_replica', None)
    if job_m_replica and str(job_m_replica) == str('true') and src_url:
        try:
            rse_name = get_source_rse(scope, name, src_url, session=session)
        except:
            logging.warn('Cannot get correct RSE for source url: %s(%s)' % (src_url, sys.exc_info()[1]))
            rse_name = None
        if rse_name and rse_name != src_rse:
            src_rse = rse_name
            logging.info('find RSE: %s for source surl: %s' % (src_rse, src_url))

    if response['external_host']:
        transfer_link = '%s/fts3/ftsmon/#/job/%s' % (response['external_host'].replace('8446', '8449'), response['transfer_id'])
    else:
        # for LOST request, response['external_host'] maybe is None
        transfer_link = None

    add_message(transfer_status, {'activity': activity,
                                  'request-id': response['request_id'],
                                  'duration': duration,
                                  'checksum-adler': adler32,
                                  'checksum-md5': md5,
                                  'file-size': filesize,
                                  'guid': None,
                                  'previous-request-id': response['previous_attempt_id'],
                                  'protocol': dst_protocol,
                                  'scope': response['scope'],
                                  'name': response['name'],
                                  'src-rse': src_rse,
                                  'src-url': src_url,
                                  'dst-rse': dst_rse,
                                  'dst-url': dst_url,
                                  'reason': reason,
                                  'transfer-endpoint': response['external_host'],
                                  'transfer-id': response['transfer_id'],
                                  'transfer-link': transfer_link,
                                  'tool-id': 'rucio-conveyor'},
                session=session)


def poll_transfers(external_host, xfers, process=0, thread=0):
    try:
        try:
            ts = time.time()
            logging.debug('%i:%i - polling %i transfers against %s' % (process, thread, len(xfers), external_host))
            resps = request_core.bulk_query_transfers(external_host, xfers, 'fts3')
            record_timer('daemons.conveyor.poller.bulk_query_transfers', (time.time()-ts)*1000/len(xfers))
        except RequestException, e:
            logging.error("Failed to contact FTS server: %s" % (str(e)))

        for transfer_id in resps:
            try:
                transf_resp = resps[transfer_id]
                # transf_resp is None: Lost.
                #             is Exception: Failed to get fts job status.
                #             is {}: No terminated jobs.
                #             is {request_id: {file_status}}: terminated jobs.
                if transf_resp is None:
                    set_transfer_state(external_host, transfer_id, RequestState.LOST)
                    record_counter('daemons.conveyor.poller.transfer_lost')
                elif isinstance(transf_resp, Exception):
                    logging.warning("Failed to poll FTS(%s) job (%s): %s" % (external_host, transfer_id, transf_resp))
                    record_counter('daemons.conveyor.poller.query_transfer_exception')
                else:
                    for request_id in transf_resp:
                        ret = update_request_state(transf_resp[request_id])
                        # if True, really update request content; if False, only touch request
                        record_counter('daemons.conveyor.poller.update_request_state.%s' % ret)

                # should touch transfers.
                # Otherwise if one bulk transfer includes many requests and one is not terminated, the transfer will be poll again.
                touch_transfer(external_host, transfer_id)
            except (DatabaseException, DatabaseError), e:
                if isinstance(e.args[0], tuple) and (match('.*ORA-00054.*', e.args[0][0]) or ('ERROR 1205 (HY000)' in e.args[0][0])):
                    logging.warn("Lock detected when handling request %s - skipping" % request_id)
                else:
                    logging.critical(traceback.format_exc())
    except:
        logging.critical(traceback.format_exc())
