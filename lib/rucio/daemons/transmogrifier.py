# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Cedric Serfon, <cedric.serfon@cern.ch>, 2013


import re
import time

from copy import copy
from json import loads, dumps
from logging import getLogger, FileHandler, Formatter, INFO, DEBUG
from os import getpid, fork, kill
from sys import exc_info, exit
from traceback import format_exception

from gearman import GearmanWorker, GearmanClient, GearmanAdminClient

from rucio.api.did import list_new_identifier, set_new_identifier, get_metadata
from rucio.api.rule import add_replication_rule
from rucio.api.subscription import list_subscriptions
from rucio.common.config import config_get, config_get_int
from rucio.common.exception import InvalidReplicationRule


logger = getLogger('rucio.daemons.Transmogrifier')
hdlr = FileHandler('/tmp/Transmogrifier.log')
formatter = Formatter('%(asctime)s %(levelname)s %(process)d %(message)s')
hdlr.setFormatter(formatter)
logger.addHandler(hdlr)
logger.setLevel(DEBUG)
logger.setLevel(INFO)


class Supervisor(object):
    def __init__(self, chunksize=400):
        """
        Create a Supervisor agent that sends chunks of new DIDs to Workers that identify the ones that match subscriptions.
        It polls regularly the state of the jobs processed by the workers and resubmit the jobs that failed.

        :param chunksize: The size of the chunks that are send as input to the Workers.
        """

        self.__gearman_server_host = 'localhost'
        self.__gearman_server_port = 4730
        self.__chunksize = chunksize
        self.__sleep_time = 4
        self.__maxdids = 10000

        try:
            self.__maxdids = config_get_int('transmogrifier', 'maxdids')
            self.__chunksize = config_get_int('transmogrifier', 'chunksize')
            self.__sleep_time = config_get_int('transmogrifier', 'sleep_time')
            self.__gearman_server_host = config_get('transmogrifier', 'gearman_server_host')
            self.__gearman_server_port = config_get('transmogrifier', 'gearman_server_port')
        except:
            pass

        self.__gm_client = GearmanClient(['%s:%i' % (self.__gearman_server_host, self.__gearman_server_port), ])
        self.__gm_admin_client = GearmanAdminClient(['%s:%i' % (self.__gearman_server_host, self.__gearman_server_port), ])

    def get_new_dids(self):
        """
        List all the new DIDs.

        :return nbdids, chunks: Return a list of chunks (list) of new DIDs and the number of these chunks
        """
        chunks = []
        chunk = []
        nbdids = 0
        for did in list_new_identifier():
            nbdids += 1
            logger.debug(did)
            if len(chunk) < self.__chunksize:
                chunk.append(did)
            else:
                chunks.append(chunk)
                chunk = []
            if nbdids >= self.__maxdids:
                break
        if chunk != []:
            chunks.append(chunk)
        return nbdids, chunks

    def submit_tasks(self, chunks):
        """
        Submit a list of tasks to the gearman server.

        :param chunks: A list of chunks (list) of new DIDs.
        :return submitted_requests: List of submitted requests to the gearman server.
        """
        list_of_jobs = []
        submitted_requests = []
        for chunk in chunks:
            list_of_jobs.append(dict(task='evaluate_subscriptions', data=dumps(chunk)))
        if list_of_jobs != []:
            submitted_requests = self.__gm_client.submit_multiple_jobs(list_of_jobs, background=False, wait_until_complete=False, max_retries=4)
            return submitted_requests
        else:
            logger.warning('No new DIDS.')
            return submitted_requests

    def query_requests_simple(self, requests):
        """
        Simple method to poll the state of the requests submitted to the gearman server.

        :param requests: A list of request.
        :return: 0
        """
        queued = 10
        while(queued != 0):
            status = self.__gm_admin_client.get_status()
            logger.info('************************', status)
            time.sleep(self.__sleep_time)
            for task in status:
                if task['task'] == 'evaluate_subscriptions':
                    queued = task['queued']
        return 0

    def query_requests(self, requests):
        """
        Improved method to poll the state of the requests submitted to the gearman server.
        Resubmit the failed requests.

        :param requests: A list of request.
        :return: 0
        """
        nb_requests_to_process = 0
        notCompletedRequests = requests
        start_time = time.time()
        end_time = time.time()
        failedJobs = -99
        nbQueuedJobs = 999
        nbRunningJobs = 0
        deeperCheck = 0
        firstpass = 1
        while notCompletedRequests != []:
            # If nbQueuedJobs > nbRunningJobs we just check the overall status of all jobs.
            if nbQueuedJobs > nbRunningJobs:
                for item in self.__gm_admin_client.get_status():
                    if item['task'] == 'evaluate_subscriptions':
                        nbQueuedJobs = item['queued']
                        nbRunningJobs = item['running']
                logger.info('Time elapsed %f : Still %i requests to complete' % (end_time - start_time, nbQueuedJobs))
            # If nbQueuedJobs <= nbRunningJobs individually we check individually each job
            else:
                if deeperCheck:
                    logger.info('Time elapsed %f : --- Failed requests : %i --- Not completed requests : %i' % (end_time - start_time, failedJobs, len(notCompletedRequests)))
                    firstpass = 0
                else:
                    logger.info('Checking individually all submited tasks')
                    deeperCheck = 1
                    firstpass = 1
                if firstpass != 0:
                    logger.info('Failed requests : %i --- Not completed requests : %i' % (failedJobs, len(notCompletedRequests)))
                # If all remaining jobs are failed, resubmitting them
                if failedJobs == len(notCompletedRequests):
                    logger.warning('%i tasks failed. They will be resubmited' % (failedJobs))
                    jobsToResubmit = []
                    for request in notCompletedRequests:
                        jobsToResubmit.append(dict(task=request.job.task, data=str(request.job.data)))
                    logger.warning('List of jobs to resubmit')
                    logger.warning(jobsToResubmit)
                    notCompletedRequests = self.__gm_client.submit_multiple_jobs(jobsToResubmit, background=False, wait_until_complete=False, max_retries=4)
                    logger.debug(notCompletedRequests)
                failedJobs = 0
                # Else get the status of each job
                if nb_requests_to_process != len(notCompletedRequests):
                    logger.info('Time elapsed %f : Still %i requests to complete' % (end_time - start_time, len(notCompletedRequests)))
                nb_requests_to_process = len(notCompletedRequests)
                notCompletedRequests2 = copy(notCompletedRequests)
                for request in notCompletedRequests2:
                    status = self.get_job_status(request)
                    if status == 'COMPLETE':
                        notCompletedRequests.remove(request)
                    elif status == 'FAILED':
                        failedJobs += 1
            end_time = time.time()
            time.sleep(self.__sleep_time)
        end_time = time.time()
        return 0

    def get_job_status(self, request):
        """
        Method to get the status of a job.

        param request: A job request
        return status: The status of the request (COMPLETE, FAILED, ...)
        """
        status = None
        try:
            status = self.__gm_client.get_job_status(request)
            status = status.state
        except KeyError, e:
            logger.debug('Problem getting the job state with get_job_status', e)
            status = request.state
        return status

    def run(self):
        """
        Loop that call run_once.
        """

        while(True):
            self.run_once()

    def run_once(self):
        """
        Method to start the Supervisor agent. Loop over all new DIDs. Generate chunks of new DIDs that are sent to the Workers that identify the ones that match subscriptions.
        """

        nbdids, chunks = self.get_new_dids()
        if chunks != []:
            logger.info('##################### Submitting %i chunks representing %s new DIDs' % (len(chunks), nbdids))
            submitted_requests = self.submit_tasks(chunks)
            logger.info(submitted_requests)
            #self.query_requests_simple(submitted_requests)
            self.query_requests(submitted_requests)
        else:
            logger.info('##################### No new DIDs to submit in this cycle')
            time.sleep(self.__sleep_time)


class Worker(GearmanWorker):
    def __init__(self, listservers):
        """
        Creates a Transmogrifier Worker that gets a list of new DIDs, identifies the subscriptions matching the DIDs and submit a replication rule for each DID matching a subscription.

        param listservers: A list of gearman servers from which the Worker gets payload.
        """
        super(Worker, self).__init__(listservers)
        self.__pid = getpid()

    def run(self):
        """
        Starts the worker.
        """
        logger.info('Creating a new GearmanWorker, process %i' % (self.__pid))
        self.register_task('evaluate_subscriptions', self.evaluate_subscriptions)
        self.work()

    def is_matching_subscription(self, subscription, did, metadata):
        """
        Method to identify if a DID matches a subscription.

        param subscription: The subscription dictionnary.
        param did: The DID dictionnary
        param metadata: The metadata dictionnary for the DID
        return: True/False
        """
        filter = {}
        try:
            filter = loads(subscription['filter'])
        except ValueError, e:
            logger.error('%s : Subscription will be skipped' % e)
            return False
        # Loop over the keys of filter for subscription
        for key in filter:
            values = filter[key]
            if key == 'pattern':
                if not re.match(values, did['name']):
                    return False
            elif key == 'scope':
                if not did['scope'] in values:
                    logger.debug('Bad scope %s != %s' % (values, did['scope']))
                    return False
            else:
                if type(values) is str or type(values) is unicode:
                    values = [values, ]
                has_metadata = 0
                for meta in metadata:
                    if str(meta) == str(key):
                        has_metadata = 1
                        if not metadata[meta] in values:
                            logger.debug('Metadata not matching %s not in %s' % (metadata[meta], str(values)))
                            return False
                if has_metadata == 0:
                    return False
        return True

    def evaluate_subscriptions(self, worker, job):
        """
        This is the method where the actual work is done : It gets a chunk of new DIDs, query the subscription table to get the ACTIVE subscriptions.
        Loop over the list of DIDs and find for each DID which subscription(s) match and finally submit the replication rules.
        If an exception is raised it is caught, the traceback is sent and a raise is issued to fail the job.
        """
        try:
            results = {}
            start_time = time.time()
            logger.debug('Process %s' % (self.__pid))
            subscriptions = [sub for sub in list_subscriptions(None, None, 'ACTIVE')]
            logger.debug('In transmogrifier worker')
            #dids, subscriptions = loads(job.data)
            dids = loads(job.data)
            for did in dids:
                if did['type'] != 'file':
                    results['%s:%s' % (did['scope'], did['name'])] = []
                    metadata = get_metadata(did['scope'], did['name'])
                    for subscription in subscriptions:
                        if self.is_matching_subscription(subscription, did, metadata) is True:
                            results['%s:%s' % (did['scope'], did['name'])].append(subscription['id'])
                            logger.info('%s:%s matches subscription %s' % (did['scope'], did['name'], subscription['id']))
                            for rule in loads(subscription['replication_rules']):
                                try:
                                    grouping = rule['grouping']
                                except:
                                    grouping = 'NONE'
                                try:
                                    add_replication_rule(dids=[{'scope': did['scope'], 'name': did['name']}], account=subscription['account'], copies=int(rule['copies']), rse_expression=rule['rse_expression'],
                                                         grouping=grouping, weight=None, lifetime=None, locked=False, subscription_id=subscription['id'], issuer='root')
                                except InvalidReplicationRule, e:
                                    logger.error(e)
                set_new_identifier(did['scope'], did['name'], 0)
            logger.debug('Matching subscriptions '+dumps(results))
            logger.info('It took %f seconds to process %i DIDs by worker %s' % (time.time() - start_time, len(dids), self.__pid))
            return dumps(results)
        except:
            exc_type, exc_value, exc_traceback = exc_info()
            logger.critical(''.join(format_exception(exc_type, exc_value, exc_traceback)).strip())
            raise


def stop(signum, frame):
    print "Kaboom Baby!"
    exit()


def launch_transmogrifier(once=False):
    """
    This method can be used to start a Transmogrifier Supervisor and 4 Workers on the localhost (5 processes).
    In production, they should be launch via supervisord.
    """
    workers_pid = []
    for i in xrange(0, 4):
        newpid = fork()
        if newpid == 0:
            worker = Worker(['127.0.0.1', ])
            worker.run()
        else:
            workers_pid.append(newpid)
    s = Supervisor()
    if once:
        s.run_once()
        # Then kill all the workers
        for pid in workers_pid:
            kill(pid, 9)
    else:
        s.run()
